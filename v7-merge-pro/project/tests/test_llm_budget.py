import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from advisor import Advisor
from llm_client import LLMClient, ModelReply


class _Response:
    def __init__(self, answer):
        self.data = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


class LLMClientBudgetTests(unittest.TestCase):
    def setUp(self):
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.close(wait_seconds=1.0)

    def make_client(self, **kwargs):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-key", "KIMI_API_KEY": ""}):
            self.client = LLMClient(**kwargs)
        return self.client

    def test_keyless_client_constructs_disabled_and_does_not_submit(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "KIMI_API_KEY": ""}), \
                patch("urllib.request.urlopen") as urlopen:
            self.client = LLMClient()
            self.assertFalse(self.client.enabled)
            self.assertTrue(self.client.disabled)
            self.assertIsNone(self.client.submit("night_plan", "system", {}, 600.0))
            self.assertEqual(self.client.calls_made, 0)
            self.assertFalse(self.client.metrics_summary()["enabled"])
            self.assertEqual(self.client.metrics_summary()["no_key"], 1)
            urlopen.assert_not_called()

    def test_one_worker_bounds_in_flight_work_and_model_reply_is_future_like(self):
        self.client = self.make_client(max_in_flight=4)
        started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_urlopen(_request, timeout):
            nonlocal active, max_active
            self.assertLessEqual(timeout, 45.0)
            with lock:
                active += 1
                max_active = max(max_active, active)
            started.set()
            release.wait(1.0)
            with lock:
                active -= 1
            return _Response({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = self.client.submit("night_plan", "system", {"n": 1}, 600.0)
            self.assertIsInstance(first, ModelReply)
            self.assertTrue(started.wait(1.0))
            second = self.client.submit("fault_review", "system", {"n": 2}, 600.0)
            self.assertIsNotNone(second)
            third = self.client.submit("confirm_report", "system", {"n": 3}, 600.0)
            self.assertIsNotNone(third)
            fourth = self.client.submit("night_plan", "system", {"n": 4}, 600.0)
            self.assertIsNotNone(fourth)
            rejected = self.client.submit("operations_note", "system", {"n": 5}, 600.0)
            self.assertIsNone(rejected)
            self.assertEqual(self.client._jobs.maxsize, 4)
            release.set()
            for reply in (first, second, third, fourth):
                self.assertTrue(reply.wait(1.0))

        self.assertTrue(first.done())
        self.assertEqual(first.result(timeout=0.1), {"ok": True})
        self.assertEqual(max_active, 1)
        summary = self.client.metrics_summary()
        self.assertEqual(summary["attempts"], 4)
        self.assertEqual(summary["failure"], 0)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["queue_full"], 1)
        self.assertEqual(summary["by_stage"]["operations"]["rejected"], 1)

    def test_429_and_5xx_retries_are_bounded_and_counted(self):
        self.client = self.make_client(max_retries=3)
        statuses = [429, 503, None]
        observed_timeouts = []

        def fake_urlopen(_request, timeout):
            observed_timeouts.append(timeout)
            status = statuses.pop(0)
            if status is not None:
                raise urllib.error.HTTPError("https://model.invalid/chat/completions", status,
                                             "ignored", {}, io.BytesIO(b"private response"))
            return _Response({"ready": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), patch("time.sleep"):
            reply = self.client.submit("night_plan", "system", {"prompt": "private source"}, 600.0)
            self.assertTrue(reply.wait(1.0))

        self.assertEqual(reply.result(timeout=0.1), {"ready": True})
        summary = self.client.metrics_summary()
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["retries"], 2)
        self.assertEqual(summary["success"], 1)
        self.assertTrue(all(0 < timeout <= 45.0 for timeout in observed_timeouts))

    def test_network_timeout_is_sanitized_and_counted(self):
        logs = []
        self.client = self.make_client(log=logs.append, max_retries=1)

        def fake_urlopen(_request, timeout):
            self.assertLessEqual(timeout, 45.0)
            raise TimeoutError("private prompt and response must not be logged")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            reply = self.client.submit("confirm_report", "private prompt", {}, 600.0)
            self.assertTrue(reply.wait(1.0))
            self.client.collect(reply)

        self.assertEqual(reply.error, "timeout")
        summary = self.client.metrics_summary()
        self.assertEqual(summary["timeout"], 1)
        self.assertEqual(summary["failure"], 1)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["deadline_before_attempt"], 0)
        self.assertTrue(all("private prompt" not in message for message in logs))
        self.assertTrue(all("unit-test-key" not in message for message in logs))

    def test_deadline_expired_before_first_request_is_rejected_not_failed(self):
        self.client = self.make_client(max_retries=1)
        started = threading.Event()
        release = threading.Event()

        def fake_urlopen(_request, timeout):
            self.assertGreater(timeout, 0)
            started.set()
            release.wait(1.0)
            return _Response({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = self.client.submit("night_plan", "system", {}, 600.0)
            self.assertIsNotNone(first)
            self.assertTrue(started.wait(1.0))
            expired = self.client.submit("operations_note", "system", {}, 600.0)
            self.assertIsNotNone(expired)
            expired._deadline = time.monotonic() - 1.0
            release.set()
            self.assertTrue(first.wait(1.0))
            self.assertTrue(expired.wait(1.0))

        self.assertEqual(first.result(timeout=0.1), {"ok": True})
        self.assertEqual(expired.error, "timeout")
        summary = self.client.metrics_summary()
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["failure"], 0)
        self.assertEqual(summary["timeout"], 0)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["deadline_before_attempt"], 1)
        self.assertEqual(summary["by_stage"]["operations"]["deadline_before_attempt"], 1)

    def test_default_attempts_continue_past_sixty_and_call_history_is_bounded(self):
        self.client = self.make_client(max_retries=1)
        calls = 0
        block_next = threading.Event()
        started = threading.Event()
        release = threading.Event()

        def fake_urlopen(_request, timeout):
            nonlocal calls
            calls += 1
            if block_next.is_set():
                started.set()
                release.wait(1.0)
            raise urllib.error.HTTPError("https://model.invalid/chat/completions", 503,
                                         "ignored", {}, io.BytesIO(b"secret response"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            for index in range(65):
                reply = self.client.submit("operations_note", "private prompt", {"index": index}, 600.0)
                self.assertIsNotNone(reply)
                self.assertTrue(reply.wait(1.0))
                self.client.collect(reply)

            block_next.set()
            active = self.client.submit("operations_note", "private prompt", {}, 600.0)
            self.assertIsNotNone(active)
            self.assertTrue(started.wait(1.0))
            queued = [self.client.submit("operations_note", "private prompt", {}, 600.0)
                      for _ in range(3)]
            self.assertTrue(all(reply is not None for reply in queued))
            self.assertEqual(self.client.in_flight(), 4)
            self.assertEqual(len(self.client.calls), 68)
            release.set()
            for reply in [active, *queued]:
                self.assertTrue(reply.wait(1.0))

        summary = self.client.metrics_summary()
        self.assertEqual(calls, 69)
        self.assertEqual(summary["attempts"], 69)
        self.assertIsNone(summary["attempt_limit"])
        self.assertEqual(summary["retries"], 0)
        self.assertEqual(summary["failure"], 69)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["by_stage"]["operations"]["attempts"], 69)
        self.assertEqual(summary["by_stage"]["operations"]["failure"], 69)
        self.assertEqual(summary["by_stage"]["operations"]["rejected"], 0)
        self.assertEqual(summary["by_stage"]["operations"]["success"], 0)
        self.assertEqual(len(self.client.calls), 64)
        self.assertTrue(all(call.done() for call in self.client.calls))

    def test_optional_max_calls_limit_is_only_applied_when_explicit(self):
        self.client = self.make_client(max_calls=1, max_retries=1)

        def fake_urlopen(_request, timeout):
            return _Response({"ok": True})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = self.client.submit("operations_note", "system", {}, 600.0)
            self.assertTrue(first.wait(1.0))
            self.assertIsNone(self.client.submit("operations_note", "system", {}, 600.0))

        summary = self.client.metrics_summary()
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["attempt_limit"], 1)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["by_stage"]["operations"]["attempts"], 1)


class _ReadyReply:
    def __init__(self, answer):
        self.answer = answer

    def done(self):
        return True

    def wait(self, _seconds):
        return True


class _AdvisorClient:
    enabled = True

    def __init__(self):
        self.submissions = []

    def submit(self, tag, system, user, wallclock_left):
        self.submissions.append((tag, system, user, wallclock_left))
        answers = {
            "night_plan": {"bad_night": False, "avoid_directions": ["SW"], "reason": "local weather"},
            "fault_review": {"fault_likely": 0.2, "reason": "stable quality"},
            "confirm_report": {"report": False, "reason": "evidence is weak"},
        }
        return _ReadyReply(answers[tag])

    @staticmethod
    def collect(reply):
        return reply.answer


class AdvisorPromptTests(unittest.TestCase):
    def test_advisor_keeps_all_three_stages_and_uses_supplied_public_context(self):
        client = _AdvisorClient()
        advisor = Advisor(client)
        fault_table = {
            "reporting": {"correct_reward": 123, "false_penalty": 321,
                          "false_report_free_allowance": 2},
            "planning_context": {"min_exposure": 900, "max_exposure": 7200,
                                 "required_threshold": 0.8,
                                 "program": {"bands": {"silver": 0.5}, "multipliers": {"gold": 1.4}}},
        }

        plan, fault = advisor.start_night(
            "2030-01-01", [{"event_kind": "rain", "direction": "SW"}], [],
            fault_table, wallclock_left=600.0, wait_seconds=0.1,
        )
        verdict = advisor.confirm_report({"reporting": fault_table["reporting"]}, 600.0, 0.1)

        self.assertEqual([item[0] for item in client.submissions],
                         ["night_plan", "fault_review", "confirm_report"])
        self.assertEqual(plan["avoid_directions"], ["SW"])
        self.assertEqual(fault["fault_likely"], 0.2)
        self.assertFalse(verdict)
        night_system = client.submissions[0][1]
        self.assertNotIn("one-hour", night_system)
        self.assertNotIn("faint", night_system)
        self.assertEqual(client.submissions[0][2]["planning_context"], fault_table["planning_context"])
        self.assertIn('"correct_reward":123', client.submissions[1][1])
        self.assertIn('"false_penalty":321', client.submissions[2][1])

    def test_keyless_advisor_uses_rule_fallback_without_submitting(self):
        class DisabledClient:
            enabled = False

            def submit(self, *_args):
                raise AssertionError("disabled client must not submit")

        advisor = Advisor(DisabledClient())
        self.assertEqual(advisor.start_night("2030-01-01", [], [], {}, 600.0, 0.1), (None, None))
        self.assertIsNone(advisor.confirm_report({}, 600.0, 0.1))


if __name__ == "__main__":
    unittest.main()
