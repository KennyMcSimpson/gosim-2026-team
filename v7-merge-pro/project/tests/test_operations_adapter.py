from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from operations import OperationsAdvisor  # noqa: E402


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _request(request_id: str, issued: str, reason: str) -> dict:
    return {"request_id": request_id, "issued_at_utc": issued, "reason": reason}


class _Call:
    def __init__(self, answer):
        self.answer = answer

    def done(self):
        return True


class _DelayedCall(_Call):
    def __init__(self, answer):
        super().__init__(answer)
        self.finished = False
        self.waited = None

    def done(self):
        return self.finished

    def wait(self, seconds):
        self.waited = seconds
        self.finished = True


class _Client:
    key = "mock-key"
    disabled = False
    max_calls = 100

    def __init__(self, handler=None, key="mock-key"):
        self.key = key
        self.handler = handler or (lambda _system, _payload: {"facts": []})
        self.calls = []

    def submit(self, tag, system, payload, wall_left):
        self.calls.append((tag, system, payload, wall_left))
        return _Call(self.handler(system, payload))

    @staticmethod
    def collect(call):
        return call.answer


def _drive(advisor, payload, now):
    night_end = now + timedelta(hours=6)
    request_count = sum(len(payload.get(field, [])) for field in ("active_requests", "new_messages")
                        if isinstance(payload.get(field), list))
    for _ in range(request_count + 8):
        submitted = advisor.update(payload, now, night_end, 3600.0)
        advisor.collect()
        if not submitted and not advisor.summary()["pending"]:
            return


class OperationsAdapterTests(unittest.TestCase):
    def test_closure_avoid_and_instrument_evidence_are_exposed_without_actions(self):
        notes = {
            "close": "Site closure: observatory cannot observe from 2026-10-06T01:00Z to 02:00Z.",
            "avoid": "Cloud blocks sector SW from 2026-10-06T01:00Z to 03:00Z.",
            "instrument": "Spectrograph efficiency degraded from 2026-10-06T01:00Z to 04:00Z.",
        }

        def handler(_system, payload):
            source = payload["source_id"]
            if source == "close":
                fact = {"scope": "closure", "direction": None, "start_utc": "2026-10-06T01:00:00Z",
                        "end_utc": "2026-10-06T02:00:00Z", "quote": "Site closure: observatory cannot observe"}
            elif source == "avoid":
                fact = {"scope": "avoid", "direction": "SW", "start_utc": "2026-10-06T01:00:00Z",
                        "end_utc": "2026-10-06T03:00:00Z", "quote": "Cloud blocks sector SW"}
            else:
                fact = {"scope": "instrument", "direction": None, "start_utc": "2026-10-06T01:00:00Z",
                        "end_utc": "2026-10-06T04:00:00Z", "quote": "Spectrograph efficiency degraded"}
            return {"facts": [{"status": "active", "reason": "explicit operations note",
                               "supersedes_source_ids": [], **fact}]}

        client = _Client(handler)
        advisor = OperationsAdvisor(client)
        payload = {"active_requests": [
            _request(source, "2026-10-06T00:30:00Z", note) for source, note in notes.items()
        ], "new_messages": []}
        now = _time("2026-10-06T01:30:00Z")
        _drive(advisor, payload, now)
        profile = advisor.operations_profile(now)

        self.assertTrue(profile["should_wait"])
        self.assertEqual(profile["operations_profile"]["state"], "closed")
        self.assertEqual(profile["avoid_directions"], ["SW"])
        self.assertEqual(profile["report_evidence"][0]["source_id"], "instrument")
        self.assertEqual(profile["report_evidence"][0]["scope"], "instrument")
        self.assertEqual(profile["report_evidence"][0]["issued_at"], "2026-10-06T00:30:00Z")
        self.assertIn(notes["close"], [call[2]["new_note"] for call in client.calls])
        self.assertNotIn("action", profile)
        self.assertEqual(len(client.calls), 3)

    def test_later_supersede_retraction_replaces_earlier_scope(self):
        texts = {
            "old": "Maintenance closure: the entire site closes from 2026-10-06T01:00Z to 02:00Z.",
            "correction": "Correction: closure moved to 03:00Z through 04:00Z.",
            "withdrawal": "Correction: the closure is withdrawn for 03:00Z through 04:00Z.",
        }

        def handler(_system, payload):
            source = payload["source_id"]
            start, end = ("2026-10-06T01:00:00Z", "2026-10-06T02:00:00Z") if source == "old" else (
                "2026-10-06T03:00:00Z", "2026-10-06T04:00:00Z")
            fact = {"scope": "closure", "direction": None, "start_utc": start, "end_utc": end,
                    "quote": texts[source], "reason": source}
            if source == "old":
                fact.update(status="active", supersedes_source_ids=[])
            elif source == "correction":
                fact.update(status="active", supersedes_source_ids=["old"])
            else:
                fact.update(status="cancel", supersedes_source_ids=["correction"])
            return {"facts": [fact]}

        advisor = OperationsAdvisor(_Client(handler))
        now = _time("2026-10-06T01:30:00Z")
        old = _request("old", "2026-10-06T00:10:00Z", texts["old"])
        correction = _request("correction", "2026-10-06T00:20:00Z", texts["correction"])
        withdrawal = _request("withdrawal", "2026-10-06T00:30:00Z", texts["withdrawal"])
        _drive(advisor, {"active_requests": [old]}, now)
        _drive(advisor, {"active_requests": [old, correction]}, now)
        self.assertFalse(advisor.should_wait(now))
        self.assertTrue(advisor.should_wait(_time("2026-10-06T03:30:00Z")))
        _drive(advisor, {"active_requests": [old, correction, withdrawal]}, now)
        self.assertFalse(advisor.should_wait(_time("2026-10-06T03:30:00Z")))

    def test_negated_contraction_does_not_cancel_an_operations_fact(self):
        old_note = "The observatory will close for maintenance from 2026-10-06T01:00Z to 02:00Z."
        note = "Correction: Don't cancel the scheduled closure; it remains active from 2026-10-06T01:00Z to 02:00Z."

        def handler(_system, payload):
            if payload["source_id"] == "old-close":
                return {"facts": [{
                    "scope": "closure", "status": "active", "direction": None,
                    "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-10-06T02:00:00Z",
                    "quote": "The observatory will close for maintenance",
                }]}
            return {"facts": [{
                "scope": "closure", "status": "cancel", "direction": None,
                "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-10-06T02:00:00Z",
                "quote": "Don't cancel the scheduled closure", "supersedes_source_ids": ["old-close"],
            }]}

        advisor = OperationsAdvisor(_Client(handler))
        now = _time("2026-10-06T01:30:00Z")
        old = _request("old-close", "2026-10-06T00:30:00Z", old_note)
        negative = _request("negated-cancel", "2026-10-06T01:00:00Z", note)
        _drive(advisor, {"active_requests": [old]}, now)
        _drive(advisor, {"active_requests": [old, negative]}, now)
        self.assertEqual(advisor.ledger_size, 1)
        self.assertTrue(advisor.should_wait(now))

    def test_cancel_without_explicit_source_citation_is_rejected(self):
        note = "The observatory closure is withdrawn for 2026-10-06T01:00Z to 02:00Z."

        def handler(_system, _payload):
            return {"facts": [{
                "scope": "closure", "status": "cancel", "direction": None,
                "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-10-06T02:00:00Z",
                "quote": "The observatory closure is withdrawn", "supersedes_source_ids": [],
            }]}

        advisor = OperationsAdvisor(_Client(handler))
        now = _time("2026-10-06T01:30:00Z")
        request = _request("uncited-cancel", "2026-10-06T01:00:00Z", note)
        _drive(advisor, {"active_requests": [request]}, now)
        self.assertEqual(advisor.ledger_size, 0)

    def test_non_list_request_containers_are_ignored(self):
        advisor = OperationsAdvisor(_Client())
        now = _time("2026-10-06T01:00:00Z")
        malformed_payloads = [
            {"active_requests": 7, "new_messages": []},
            {"active_requests": {"request_id": "not-a-list"}, "new_messages": 3},
        ]

        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                submitted = advisor.update(payload, now, now + timedelta(hours=6), 3600.0)
                advisor.collect()
                self.assertFalse(submitted)
                self.assertEqual(advisor.calls_made, 0)

        self.assertEqual(advisor.summary()["notes_seen"], 0)

    def test_scope_correction_can_replace_a_different_avoid_direction(self):
        texts = {
            "old-avoid": "Cloud blocks sector SW from 2026-10-06T01:00Z to 03:00Z.",
            "corrected-avoid": "Correction: cloud now blocks sector E from 2026-10-06T01:00Z to 03:00Z.",
        }

        def handler(_system, payload):
            source = payload["source_id"]
            direction = "SW" if source == "old-avoid" else "E"
            return {"facts": [{"scope": "avoid", "status": "active", "direction": direction,
                               "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-10-06T03:00:00Z",
                               "quote": texts[source],
                               "supersedes_source_ids": [] if source == "old-avoid" else ["old-avoid"]}]}

        advisor = OperationsAdvisor(_Client(handler))
        now = _time("2026-10-06T01:30:00Z")
        old = _request("old-avoid", "2026-10-06T00:10:00Z", texts["old-avoid"])
        correction = _request("corrected-avoid", "2026-10-06T00:20:00Z", texts["corrected-avoid"])
        _drive(advisor, {"active_requests": [old]}, now)
        _drive(advisor, {"active_requests": [old, correction]}, now)
        self.assertEqual(advisor.avoid_now(now), {"E"})

    def test_late_revised_old_source_does_not_resurrect_superseded_direction(self):
        notes = {
            "old-original": "Cloud blocks sector SW from 2026-10-06T01:00Z to 03:00Z.",
            "old-revised": "Cloud note revised: sector SW was listed from 2026-10-06T01:00Z to 03:00Z.",
            "correction": "Correction: cloud now blocks sector E from 2026-10-06T01:00Z to 03:00Z.",
        }

        def handler(_system, payload):
            source = payload["source_id"]
            if source == "correction":
                direction, supersedes = "E", ["old-avoid"]
                scope = "avoid"
            elif source == "old-avoid":
                direction, supersedes = "SW", []
                scope = "avoid"
            else:
                index = int(source.split("-")[-1])
                scope, direction = "instrument", None
                supersedes = [f"instrument-{index - 1}"] if index else []
            return {"facts": [{
                "scope": scope, "status": "active", "direction": direction,
                "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-10-06T03:00:00Z",
                "quote": payload["new_note"], "supersedes_source_ids": supersedes,
            }]}

        advisor = OperationsAdvisor(_Client(handler))
        now = _time("2026-10-06T01:30:00Z")
        old = _request("old-avoid", "2026-10-06T00:10:00Z", notes["old-original"])
        correction = _request("correction", "2026-10-06T00:20:00Z", notes["correction"])
        revised = _request("old-avoid", "2026-10-06T00:10:00Z", notes["old-revised"])
        instrument_notes = [
            _request(f"instrument-{index}",
                     ( _time("2026-10-06T00:21:00Z") + timedelta(seconds=index)).isoformat(),
                     f"Correction: instrument status update {index} remains degraded until 2026-10-06T03:00Z.")
            for index in range(260)
        ]

        _drive(advisor, {"active_requests": [old]}, now)
        _drive(advisor, {"active_requests": [old, correction]}, now)
        self.assertEqual(advisor.avoid_now(now), {"E"})
        _drive(advisor, {"active_requests": [old, correction, *instrument_notes]}, now)
        self.assertEqual(advisor.ledger_size, 256)
        _drive(advisor, {"active_requests": [old, correction, revised]}, now)

        self.assertEqual(advisor.avoid_now(now), {"E"})

    def test_expired_fact_disappears_from_profile(self):
        text = "The observatory closes from 2026-10-06T01:00Z until 2026-10-06T02:00Z."

        def handler(_system, _payload):
            return {"facts": [{"scope": "closure", "status": "active", "direction": None,
                               "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-10-06T02:00:00Z",
                               "quote": "The observatory closes"}]}

        advisor = OperationsAdvisor(_Client(handler))
        now = _time("2026-10-06T01:30:00Z")
        _drive(advisor, {"active_requests": [_request("close", "2026-10-06T00:30:00Z", text)]}, now)
        self.assertTrue(advisor.should_wait(_time("2026-10-06T01:30:00Z")))
        self.assertFalse(advisor.should_wait(_time("2026-10-06T02:01:00Z")))
        self.assertEqual(advisor.operations_profile(_time("2026-10-06T02:01:00Z"))["operations_profile"]["closures"], [])

    def test_invalid_quote_timezone_and_issued_cutoff_are_rejected(self):
        text = "The observatory closes for scheduled work until 2026-10-06T03:00Z."

        def handler(_system, _payload):
            return {"facts": [
                {"scope": "closure", "status": "active", "direction": None,
                 "start_utc": "2026-10-06T01:00:00", "end_utc": "2026-10-06T03:00:00Z",
                 "quote": "The observatory closes"},
                {"scope": "closure", "status": "active", "direction": None,
                 "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-10-06T03:00:00Z",
                 "quote": "fabricated quote"},
                {"scope": "closure", "status": "active", "direction": None,
                 "start_utc": "2026-10-06T01:00:00+08:00", "end_utc": "2026-10-06T03:00:00Z",
                 "quote": "The observatory closes"},
                {"scope": "closure", "status": "active", "direction": None,
                 "start_utc": "2026-10-04T23:00:00Z", "end_utc": "2026-10-06T03:00:00Z",
                 "quote": "The observatory closes"},
                {"scope": "closure", "status": "active", "direction": None,
                 "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-12-06T03:00:00Z",
                 "quote": "The observatory closes"},
            ]}

        client = _Client(handler)
        advisor = OperationsAdvisor(client)
        now = _time("2026-10-06T01:30:00Z")
        old_issue = _request("old-issued", "2026-07-01T00:00:00Z", text)
        _drive(advisor, {"active_requests": [_request("close", "2026-10-06T00:30:00Z", text), old_issue]}, now)
        self.assertFalse(advisor.should_wait(_time("2026-10-06T01:30:00Z")))
        self.assertEqual(advisor.ledger_size, 0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][2]["source_id"], "close")
        self.assertEqual(client.calls[0][0], "operations")
        missing_issue = {"request_id": "missing-issued", "reason": text}
        _drive(advisor, {"active_requests": [missing_issue]}, now)
        self.assertEqual(len(client.calls), 1)

    def test_new_messages_no_key_and_bad_json_fall_back_safely(self):
        note = "Maintenance closure: the entire site will close during the next observing window."
        no_key = _Client(key="")
        advisor = OperationsAdvisor(no_key)
        payload = {"active_requests": [], "new_messages": [
            {"record_type": "observation_request", **_request("no-key", "2026-10-06T00:30:00Z", note)}
        ]}
        now = _time("2026-10-06T01:00:00Z")
        _drive(advisor, payload, now)
        self.assertEqual(no_key.calls, [])
        self.assertFalse(advisor.should_wait(_time("2026-10-06T01:00:00Z")))

        malformed = _Client(lambda _system, _payload: "not JSON")
        bad_advisor = OperationsAdvisor(malformed)
        now = _time("2026-10-06T01:00:00Z")
        _drive(bad_advisor, {"active_requests": [_request("bad-json", "2026-10-06T00:30:00Z", note)]}, now)
        self.assertFalse(bad_advisor.should_wait(_time("2026-10-06T01:00:00Z")))


    def test_call_volume_exceeds_history_and_ledger_capacities(self):
        def handler(_system, payload):
            index = payload["source_id"].split("-")[-1]
            return {"facts": [{
                "scope": "instrument", "status": "active", "direction": None,
                "start_utc": "2026-10-06T01:00:00Z", "end_utc": "2026-10-06T02:00:00Z",
                "quote": f"Instrument report for note {index}",
            }]}

        client = _Client(handler)
        advisor = OperationsAdvisor(client)
        now = _time("2026-10-06T01:30:00Z")
        requests = [_request(
            f"note-{index}", "2026-10-06T00:30:00Z",
            f"Instrument report for note {index} remains degraded until 2026-10-06T02:00Z.",
        ) for index in range(260)]
        payload = {"active_requests": requests}

        _drive(advisor, payload, now)

        self.assertEqual(len(client.calls), 260)
        self.assertEqual(advisor.calls_made, 260)
        self.assertEqual(advisor.metrics["tracked_notes"], 32)
        self.assertEqual(advisor.ledger_size, 256)
        self.assertEqual(len(advisor.report_evidence(now)), 256)
        _drive(advisor, payload, now)
        self.assertEqual(len(client.calls), 260)
        self.assertLessEqual(len(advisor._seen_keys), 1024)

    def test_late_older_note_is_not_dropped_by_a_global_watermark(self):
        client = _Client()
        advisor = OperationsAdvisor(client)
        now = _time("2026-10-06T01:00:00Z")
        newer = _request("newer", "2026-10-06T00:50:00Z", "Staff note for the current observing shift.")
        older = _request("late-older", "2026-10-06T00:20:00Z", "Staff note for an earlier observing shift.")

        _drive(advisor, {"active_requests": [newer]}, now)
        _drive(advisor, {"active_requests": [newer, older]}, now)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(advisor.calls_made, 2)

    def test_only_one_shared_client_call_is_pending(self):
        client = _Client()
        calls = []

        def submit(tag, system, payload, wall_left):
            call = _DelayedCall({"facts": []})
            calls.append((tag, system, payload, wall_left, call))
            client.calls.append((tag, system, payload, wall_left))
            return call

        client.submit = submit
        advisor = OperationsAdvisor(client)
        now = _time("2026-10-06T01:00:00Z")
        payload = {"active_requests": [
            _request("first", "2026-10-06T00:10:00Z", "Staff note for the first observing shift.") ,
            _request("second", "2026-10-06T00:20:00Z", "Staff note for the second observing shift."),
        ]}
        self.assertTrue(advisor.update(payload, now, now + timedelta(hours=6), 3600.0))
        self.assertFalse(advisor.update(payload, now, now + timedelta(hours=6), 3600.0))
        self.assertEqual(len(calls), 1)
        calls[0][4].finished = True
        advisor.collect()
        self.assertTrue(advisor.update(payload, now, now + timedelta(hours=6), 3600.0))
        self.assertEqual(len(calls), 2)

    def test_shared_client_queue_rejection_keeps_note_ready_for_retry(self):
        client = _Client()
        attempts = []

        def submit(tag, system, payload, wall_left):
            attempts.append((tag, system, payload, wall_left))
            if len(attempts) == 1:
                return None
            return _Call({"facts": []})

        client.submit = submit
        advisor = OperationsAdvisor(client)
        now = _time("2026-10-06T01:00:00Z")
        payload = {"active_requests": [
            _request("retry-note", "2026-10-06T00:30:00Z", "Staff note for a temporary queue retry.")
        ]}

        self.assertFalse(advisor.update(payload, now, now + timedelta(hours=6), 3600.0))
        self.assertFalse(advisor.summary()["pending"])
        self.assertEqual(advisor.calls_made, 0)
        self.assertTrue(advisor.update(payload, now, now + timedelta(hours=6), 3600.0))
        advisor.collect()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(advisor.calls_made, 1)
        self.assertEqual(advisor.metrics["notes_succeeded"], 1)

    def test_wait_uses_only_pending_call_and_summary_redacts_quotes(self):
        text = "Spectrograph efficiency is degraded until 2026-10-06T02:00Z."
        quote = "Spectrograph efficiency is degraded"
        client = _Client(lambda _system, _payload: {"facts": [{
            "scope": "instrument", "status": "active", "direction": None,
            "start_utc": "2026-10-06T00:30:00Z", "end_utc": "2026-10-06T02:00:00Z",
            "quote": quote,
        }]})
        call = _DelayedCall({"facts": [{
            "scope": "instrument", "status": "active", "direction": None,
            "start_utc": "2026-10-06T00:30:00Z", "end_utc": "2026-10-06T02:00:00Z",
            "quote": quote,
        }]})
        client.submit = lambda tag, system, payload, wall_left: call
        advisor = OperationsAdvisor(client)
        now = _time("2026-10-06T01:00:00Z")
        payload = {"active_requests": [_request("instrument", "2026-10-06T00:30:00Z", text)]}
        self.assertTrue(advisor.update(payload, now, now + timedelta(hours=6), 3600.0))
        advisor.wait(240.0)
        evidence = advisor.report_evidence(now)
        self.assertEqual(evidence[0]["quote"], quote)
        self.assertNotIn(quote, repr(advisor.summary()))
        self.assertLessEqual(call.waited, 8.0)


if __name__ == "__main__":
    unittest.main()
