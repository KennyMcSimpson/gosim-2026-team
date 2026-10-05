"""Freshness and pending-state contracts using real v2 public-state search."""
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from public_support import public_input, check_action
from agent_core.planner import Planner
from agent_core.state import SurveyState


class Client:
    enabled, _has_key, calls_made, max_calls = True, True, 0, 64

    def __init__(self, mode, state):
        self.mode, self.state = mode, state
        self.context = None

    def review_plan(self, ctx, wall):
        self.calls_made += 1
        self.context = ctx
        p = ctx["candidates"][-1]
        answer = {"candidate_id": p["candidate_id"], "decision_token": ctx["decision_token"],
                  "rationale": p["allowed_rationales"][0]}
        if self.mode == "resync":
            self.state.progress_version += 1
        elif self.mode == "unknown":
            answer["candidate_id"] = "unknown"
        elif self.mode == "timeout":
            raise TimeoutError()
        return answer


class RealStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.init, cls.fiber, cls.targets = public_input("a")

    def make(self):
        with patch.dict(os.environ, {"AGENT_MODEL_BACKEND": "pi-review", "OPENAI_API_KEY": "", "KIMI_API_KEY": ""}):
            p = Planner(SurveyState(self.init))
        p.clock.update({"remaining_real_cpu_seconds": 500, "wall_remaining_seconds": 1600})
        p.state.fast_level = 0
        return p

    def compare(self, mode):
        baseline, pro = self.make(), self.make()
        try:
            night = len(pro.state.nights) // 2
            start, end = pro.state.nights[night]
            now = start + timedelta(seconds=900)
            hours = (now - pro.state.survey_start).total_seconds() / 3600
            baseline.plan_reviewer = None
            a = baseline.plan(now, end, night, hours)
            client = Client(mode, pro.state)
            pro.plan_reviewer.client = client
            # Numerical gates have separate tests; retain real geometry/commit checks.
            pro.plan_reviewer._tradeoff = lambda base, other, rationale: rationale == "uniformity"
            b = pro.plan(now, end, night, hours)
            self.assertIsNotNone(client.context, "fixture did not reach real review")
            check_action({"protocol_version": "participant-agent-protocol-v4", "message_type": "decision_response",
                          "decision_sequence": 0, **b}, now, self.fiber, self.targets)
            self.assertEqual(set(pro.state.pending), set(b["assignments"].values()))
            self.assertEqual(pro.state.pending_duration, b["duration_seconds"])
            if mode == "accept":
                self.assertNotEqual(a, b)
                self.assertEqual(pro.plan_reviewer.changed, 1)
                self.assertEqual(b["decision_source"], "v2-pi-fixed-review")
            else:
                self.assertEqual(a, b)
                self.assertEqual(dict(baseline.state.pending), dict(pro.state.pending))
                self.assertEqual(pro.plan_reviewer.changed, 0)
            return pro.plan_reviewer.last_outcome
        finally:
            baseline.close()
            pro.close()

    def test_real_candidate_changes_committed_action(self):
        self.assertEqual(self.compare("accept"), "applied")

    def test_progress_version_change_rejects_same_token(self):
        self.assertEqual(self.compare("resync"), "stale")

    def test_invalid_id_preserves_baseline_pending(self):
        self.assertEqual(self.compare("unknown"), "unknown_candidate")

    def test_timeout_preserves_baseline_action(self):
        self.assertEqual(self.compare("timeout"), "no_answer")


if __name__ == "__main__":
    unittest.main()
