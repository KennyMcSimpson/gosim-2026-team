"""Safety tests for the bounded Pi plan-review adapter.

These tests exercise selection and fallback behavior without starting Node or
making provider calls.  The numerical search remains owned by Python.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from agent_core.pro_advisor import PlanReviewer


class _Trace:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)


class _Client:
    def __init__(self, responder=None, *, enabled=True, has_key=True):
        self.enabled = enabled
        self._has_key = has_key
        self.calls_made = 0
        self.max_calls = 100
        self.responder = responder
        self.contexts = []

    def review_plan(self, context, _wall_left):
        self.calls_made += 1
        self.contexts.append(context)
        return self.responder(context) if self.responder else None


class _Planner:
    def __init__(self):
        self.clock = SimpleNamespace(wall_remaining=lambda: 1200)
        self.observe_count = 20
        self._request_views_now = []
        self._current_action_index = 20
        self.state = SimpleNamespace(invalidated_actions=set(), nights=[1, 2, 3], scale=1.0,
                                     notices={}, extra_avoid=set(), force_program=None,
                                     factor=[0.0], best_score=[0.0], misses=[0])
        self.token = "token-1"


class _Reviewer(PlanReviewer):
    """Keep tests focused on review protocol, not numerical-search fixtures."""

    @staticmethod
    def _signature(plan):
        return (plan["pointing"], plan["duration"], plan["program"],
                tuple(sorted((f, x["i"]) for f, x in plan["items"].items())))

    def _metrics(self, _planner, plan, _now, _night_index):
        return plan["metrics"]

    def _valid(self, _planner, plan, _now, _night_end):
        if isinstance(plan["valid"], list):
            return plan["valid"].pop(0)
        return plan["valid"]

    def _token(self, planner, _candidates, _now):
        return planner.token


def _plan(*, duration, rate=10.0, valid=True, program="DARK", az=180.0):
    metrics = {
        "science_gain": 10.0,
        "utility": 20.0,
        "utility_per_second": rate,
        "required_completed": 1,
        "required_last_window": 1,
        "required_value": 5.0,
        "request_reward": 0.0,
        "requests": [],
        "uniformity_gain": 0.0,
        "valid_probability": 0.9,
        "duration_seconds": duration,
        "program": program,
        "assigned_fibers": 2,
        "pointing": {"alt_deg": 60.0, "az_deg": az},
        "two_step_request": False,
    }
    return {"pointing": (60.0, az), "duration": duration,
            "program": program, "items": {1: {"i": 3}},
            "metrics": metrics, "valid": valid}


class PlanReviewerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-10-06T02:00:00+00:00")
        self.end = self.now + timedelta(hours=8)
        self.baseline = _plan(duration=600)
        self.shorter = _plan(duration=300, az=190.0)
        self.planner = _Planner()
        self.trace = _Trace()

    def _review(self, client):
        return _Reviewer(client, self.trace).select(
            self.planner, [self.baseline, self.shorter], self.baseline,
            self.now, self.end, 0)

    def test_missing_provider_uses_baseline_without_call(self):
        client = _Client(enabled=False, has_key=False)
        chosen = self._review(client)
        self.assertIs(chosen, self.baseline)
        self.assertEqual(client.calls_made, 0)

    def test_unknown_candidate_uses_baseline(self):
        client = _Client(lambda ctx: {"decision_token": ctx["decision_token"],
                                      "candidate_id": "invented",
                                      "rationale": "opportunity"})
        reviewer = _Reviewer(client, self.trace)
        chosen = reviewer.select(self.planner, [self.baseline, self.shorter],
                                 self.baseline, self.now, self.end, 0)
        self.assertIs(chosen, self.baseline)
        self.assertEqual(reviewer.last_outcome, "unknown_candidate")
        self.assertEqual(reviewer.changed, 0)

    def test_changed_decision_token_rejects_stale_choice(self):
        def answer(context):
            self.planner.token = "token-2"
            return {"decision_token": context["decision_token"],
                    "candidate_id": "c1", "rationale": "opportunity"}

        reviewer = _Reviewer(_Client(answer), self.trace)
        chosen = reviewer.select(self.planner, [self.baseline, self.shorter],
                                 self.baseline, self.now, self.end, 0)
        self.assertIs(chosen, self.baseline)
        self.assertEqual(reviewer.last_outcome, "stale")

    def test_unsupported_rationale_cannot_change_plan(self):
        client = _Client(lambda ctx: {"decision_token": ctx["decision_token"],
                                      "candidate_id": "c1", "rationale": "risk"})
        reviewer = _Reviewer(client, self.trace)
        chosen = reviewer.select(self.planner, [self.baseline, self.shorter],
                                 self.baseline, self.now, self.end, 0)
        self.assertIs(chosen, self.baseline)
        self.assertEqual(reviewer.last_outcome, "unsupported_tradeoff")

    def test_valid_opportunity_candidate_is_applied(self):
        client = _Client(lambda ctx: {"decision_token": ctx["decision_token"],
                                      "candidate_id": "c1", "rationale": "opportunity"})
        reviewer = _Reviewer(client, self.trace)
        chosen = reviewer.select(self.planner, [self.baseline, self.shorter],
                                 self.baseline, self.now, self.end, 0)
        self.assertIs(chosen, self.shorter)
        self.assertEqual(reviewer.last_outcome, "applied")
        self.assertEqual((reviewer.questions, reviewer.applied, reviewer.changed), (1, 1, 1))
        context = client.contexts[0]
        self.assertEqual([c["candidate_id"] for c in context["candidates"]], ["baseline", "c1"])

    def test_revalidation_failure_falls_back_after_model_selection(self):
        self.shorter["valid"] = [True, False]
        client = _Client(lambda ctx: {"decision_token": ctx["decision_token"],
                                      "candidate_id": "c1", "rationale": "opportunity"})
        reviewer = _Reviewer(client, self.trace)
        chosen = reviewer.select(self.planner, [self.baseline, self.shorter],
                                 self.baseline, self.now, self.end, 0)
        self.assertIs(chosen, self.baseline)
        self.assertEqual(reviewer.last_outcome, "invalid_plan")
        self.assertEqual(reviewer.changed, 0)

    def test_empty_or_exception_answer_uses_baseline(self):
        for client in (_Client(), _Client(lambda ctx: (_ for _ in ()).throw(TimeoutError()))):
            self.assertIs(self._review(client), self.baseline)

    def test_low_utility_candidate_not_sent(self):
        self.shorter["metrics"]["utility_per_second"] = 8.0
        client = _Client()
        self.assertIs(self._review(client), self.baseline)
        self.assertEqual(client.calls_made, 0)

    def test_wall_reserve_and_quota_prevent_calls(self):
        client = _Client()
        self.planner.clock.wall_remaining = lambda: 300
        self.assertIs(self._review(client), self.baseline)
        self.assertEqual(client.calls_made, 0)
        self.planner.clock.wall_remaining = lambda: 1500
        reviewer = _Reviewer(client, self.trace)
        reviewer.questions = reviewer.max_questions
        self.assertIs(reviewer.select(self.planner, [self.shorter], self.baseline,
                                     self.now, self.end, 0), self.baseline)
        self.assertEqual(client.calls_made, 0)

    def test_wall_time_consumed_before_apply_falls_back(self):
        def answer(ctx):
            self.planner.clock.wall_remaining = lambda: 299
            return {"candidate_id": "c1", "decision_token": ctx["decision_token"], "rationale": "opportunity"}
        reviewer = _Reviewer(_Client(answer), self.trace)
        self.assertIs(reviewer.select(self.planner, [self.shorter], self.baseline,
                                     self.now, self.end, 0), self.baseline)
        self.assertEqual(reviewer.last_outcome, "wall_reserve")

    def test_cadence_and_season_quota(self):
        client = _Client()
        reviewer = _Reviewer(client, self.trace)
        for _ in range(5):
            reviewer.select(self.planner, [self.shorter], self.baseline, self.now, self.end, 0)
        self.assertEqual(client.calls_made, 1)
        self.planner.observe_count += reviewer.min_interval
        reviewer.select(self.planner, [self.shorter], self.baseline, self.now, self.end, 0)
        self.assertEqual(client.calls_made, 2)
        self.planner.observe_count += reviewer.min_interval
        reviewer.select(self.planner, [self.shorter], self.baseline, self.now, self.end, 0)
        self.assertEqual(client.calls_made, 2)


if __name__ == "__main__":
    unittest.main()
