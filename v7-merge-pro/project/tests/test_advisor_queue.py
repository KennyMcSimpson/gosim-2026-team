import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from advisor import Advisor


class _ReadyReply:
    def __init__(self, answer):
        self.answer = answer

    def done(self):
        return True

    def wait(self, _seconds):
        return True


class _QueueClient:
    enabled = True

    def __init__(self, reject_counts=None, failed_tags=()):
        self.reject_counts = dict(reject_counts or {})
        self.failed_tags = set(failed_tags)
        self.submissions = []
        self.collected = []

    def submit(self, tag, system, user, wallclock_left):
        self.submissions.append((tag, system, user, wallclock_left))
        remaining = self.reject_counts.get(tag, 0)
        if remaining:
            self.reject_counts[tag] = remaining - 1
            return None
        if tag in self.failed_tags:
            return _ReadyReply(None)
        answers = {
            "night_plan": {"bad_night": False, "avoid_directions": [], "reason": "clear"},
            "fault_review": {"fault_likely": 0.1, "reason": "stable"},
            "confirm_report": {"report": False, "reason": "weak evidence"},
        }
        return _ReadyReply(answers[tag])

    def collect(self, reply):
        self.collected.append(reply)
        return reply.answer


class AdvisorQueueTests(unittest.TestCase):
    def test_rejected_stage_retries_when_poll_receives_fresh_wallclock(self):
        client = _QueueClient(reject_counts={"night_plan": 1})
        advisor = Advisor(client)

        plan, fault = advisor.start_night(
            "2030-01-01", [{"event_kind": "rain", "direction": "SW"}], [], {}, 600.0, 0.1,
        )
        self.assertIsNone(plan)
        self.assertEqual(fault["fault_likely"], 0.1)
        self.assertEqual([item[0] for item in client.submissions], ["night_plan", "fault_review"])

        self.assertEqual(advisor.poll(), (None, None))
        self.assertEqual(len(client.submissions), 2)

        plan, fault = advisor.poll(wallclock_left=500.0)
        self.assertFalse(plan["bad_night"])
        self.assertIsNone(fault)
        self.assertEqual([item[0] for item in client.submissions],
                         ["night_plan", "fault_review", "night_plan"])
        self.assertEqual(len(client.collected), 2)

        self.assertEqual(advisor.poll(wallclock_left=400.0), (None, None))
        self.assertEqual(len(client.submissions), 3)
        self.assertEqual(len(client.collected), 2)

    def test_new_night_replaces_unsubmitted_inputs_and_failed_calls_are_not_retried(self):
        client = _QueueClient(reject_counts={"night_plan": 2})
        advisor = Advisor(client)

        advisor.start_night(
            "2030-01-01", [{"event_kind": "rain", "direction": "SW"}], [], {}, 600.0, 0.1,
        )
        advisor.start_night(
            "2030-01-02", [{"event_kind": "haze", "direction": "NE"}], [], {}, 600.0, 0.1,
        )
        plan_calls = [item for item in client.submissions if item[0] == "night_plan"]
        self.assertEqual(len(plan_calls), 2)

        plan, _fault = advisor.poll(wallclock_left=500.0)
        self.assertFalse(plan["bad_night"])
        retried_input = [item for item in client.submissions if item[0] == "night_plan"][-1][2]
        self.assertEqual(retried_input["night"], "2030-01-02")
        self.assertEqual(retried_input["forecast_tonight"][0]["direction"], "NE")
        self.assertEqual(len([item for item in client.submissions if item[0] == "night_plan"]), 3)

        failed_client = _QueueClient(failed_tags={"night_plan"})
        failed_advisor = Advisor(failed_client)
        failed_advisor.start_night("2030-01-03", [], [], {}, 600.0, 0.1)
        self.assertEqual(failed_advisor.poll(wallclock_left=500.0), (None, None))
        self.assertEqual(len([item for item in failed_client.submissions if item[0] == "night_plan"]), 1)


if __name__ == "__main__":
    unittest.main()
