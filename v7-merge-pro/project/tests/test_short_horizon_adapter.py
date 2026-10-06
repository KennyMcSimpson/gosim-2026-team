from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from short_horizon import _future_template, rerank  # noqa: E402
from skymath import altaz_to_radec, local_sidereal_deg, wrap180  # noqa: E402


NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)


def _action(name, target, *, duration=10, score=100.0, factor=0.8, request_gain=0.0):
    return {
        "action": "observe",
        "pointing": {"alt_deg": 45.0, "az_deg": float(ord(name[0]) % 360)},
        "assignments": {"0": target},
        "duration_seconds": duration,
        "program": "BACKUP",
        "test_name": name,
        "test_score": score,
        "test_factor": factor,
        "test_request_gain": request_gain,
    }


class _Planner:
    def __init__(self, candidates, evaluator=None, requests=()):
        self.last_candidates = [{"action": action} for action in candidates]
        self.evaluator = evaluator
        self.requests = list(requests)
        self.ids = ["t0", "t1", "t2"]
        self.index_of = {target: index for index, target in enumerate(self.ids)}
        self.weight = [1.0, 1.0, 1.0]
        self.cur = [0.0, 0.0, 0.0]
        self.factor = [0.0, 0.0, 0.0]
        self.scale = 1.0
        self.lat = 0.0
        self.lon = 0.0
        self.offset = (0.0, 0.0)
        self.pending = {"base": "already pending"}
        self.pending_cmd = (1.0, 2.0)
        self.pending_program = "BACKUP"
        self.pending_duration = 10
        self.pending_night = 0
        self.calls = []
        self.recorded = []

    def evaluate_action(self, action, now, night_end, night_index, hours, *,
                        scale_override=None, cur_override=None, factor_override=None):
        self.calls.append({"name": action["test_name"], "now": now, "hours": hours,
                           "scale": scale_override, "cur": dict(cur_override or {}),
                           "factor": dict(factor_override or {}),
                           "pointing": dict(action["pointing"])})
        if self.evaluator:
            return self.evaluator(action, now, scale_override, cur_override or {}, factor_override or {})
        target = action["assignments"]["0"]
        target_index = self.index_of[target]
        score = action["test_score"] * (1.0 if scale_override is None else scale_override)
        current = (cur_override or {}).get(target, self.cur[target_index])
        science = max(0.0, score - current)
        factor = action["test_factor"] * (1.0 if scale_override is None else scale_override)
        request_factor = factor * 0.97
        request_gain = action["test_request_gain"] if request_factor >= 0.5 else 0.0
        return {
            "predictions": {target: {"expected_score": score, "factor": factor,
                                     "request_factor": request_factor,
                                     "model": 1.0, "band_model": 1.0, "alt": 45.0,
                                     "az": 100.0, "fiber": 0}},
            "science_gain": science,
            "required_gain": 0.0,
            "request_gain": request_gain,
            "utility": science + request_gain,
        }

    def record_action(self, action, now, night_end, night_index, hours):
        self.recorded.append(action)
        self.pending = {"committed": action["test_name"]}
        return True


class ShortHorizonAdapterTests(unittest.TestCase):
    def test_future_template_preserves_equatorial_center_with_offset(self):
        action = _action("field", "t0")
        planner = _Planner([action])
        planner.offset = (0.7, -1.2)
        future = NOW + timedelta(hours=1)

        future_action = _future_template(planner, action, NOW, future)

        actual_now = (action["pointing"]["alt_deg"] + planner.offset[0],
                      action["pointing"]["az_deg"] + planner.offset[1])
        actual_future = (future_action["pointing"]["alt_deg"] + planner.offset[0],
                         future_action["pointing"]["az_deg"] + planner.offset[1])
        ra_now, dec_now = altaz_to_radec(*actual_now, local_sidereal_deg(NOW, planner.lon), planner.lat)
        ra_future, dec_future = altaz_to_radec(*actual_future, local_sidereal_deg(future, planner.lon), planner.lat)

        self.assertLess(abs(wrap180(ra_future - ra_now)), 1e-3)
        self.assertAlmostEqual(dec_future, dec_now, places=3)
        self.assertNotEqual(future_action["pointing"], action["pointing"])

    def test_future_action_is_re_evaluated_at_the_new_time(self):
        base = _action("base", "t0", score=100.0)
        alternative = _action("alternative", "t1", score=99.0)
        continuation = _action("continuation", "t0", score=1.0)

        def evaluate(action, now, scale, cur, _factor):
            target = action["assignments"]["0"]
            score = action["test_score"]
            if action["test_name"] == "continuation":
                score = 150.0 if now > NOW else 1.0
            science = max(0.0, score - cur.get(target, 0.0))
            return {"predictions": {target: {"expected_score": score, "factor": 0.8}},
                    "science_gain": science, "required_gain": 0.0,
                    "request_gain": 0.0, "utility": science}

        planner = _Planner([base, alternative, continuation], evaluate)
        chosen, summary = rerank(planner, base, NOW, NOW + timedelta(hours=1), 0, 10.0, float("inf"))

        self.assertIs(chosen, alternative)
        self.assertTrue(summary["used"])
        self.assertFalse(summary["fallback"])
        self.assertEqual(planner.pending, {"base": "already pending"})
        self.assertEqual(planner.recorded, [])
        later_calls = [call for call in planner.calls if call["name"] == "continuation" and call["now"] > NOW]
        self.assertTrue(later_calls)
        self.assertTrue(all(call["now"] == NOW + timedelta(seconds=10) for call in later_calls))
        self.assertTrue(any(abs(call["pointing"]["az_deg"] - continuation["pointing"]["az_deg"]) > 1e-3
                            for call in later_calls))

    def test_best_max_and_request_reward_are_counted_once_across_two_steps(self):
        first = _action("first", "t0", score=60.0, factor=0.63, request_gain=50.0)
        second = _action("second", "t0", score=90.0, factor=0.63, request_gain=50.0)
        request = {"target_indices": (0,), "remaining_count": 1, "threshold": 0.5,
                   "reward": 50.0, "deadline": NOW + timedelta(minutes=2)}

        def evaluate(action, now, scale, cur, _factor):
            if action["test_name"] == "second" and now == NOW:
                return None
            target = action["assignments"]["0"]
            score = action["test_score"]
            science = max(0.0, score - cur.get(target, 0.0))
            request_factor = action["test_factor"] * scale * 0.97
            request_gain = 50.0 if request_factor >= 0.5 else 0.0
            return {"predictions": {target: {"expected_score": score,
                                              "factor": action["test_factor"] * scale,
                                              "request_factor": request_factor}},
                    "science_gain": science, "required_gain": 0.0,
                    "request_gain": request_gain, "utility": science + request_gain}

        planner = _Planner([first, second], evaluate, [request])
        chosen, summary = rerank(planner, first, NOW, NOW + timedelta(hours=1), 0, 10.0, float("inf"))

        self.assertIs(chosen, first)
        self.assertEqual(summary["selected_steps"], 2)
        self.assertEqual(summary["scenario_utility"]["persistent"], 140.0)
        self.assertEqual(summary["scenario_utility"]["adverse"], 90.0)
        future_call = next(call for call in planner.calls if call["name"] == "second" and call["now"] > NOW)
        self.assertEqual(future_call["cur"]["t0"], 60.0)

    def test_required_gain_is_incremental_for_repeated_targets_and_full_for_new_targets(self):
        first = _action("required-first", "t0", score=0.0, factor=0.2)
        repeat = _action("required-repeat", "t0", score=0.0, factor=0.2)
        distinct = _action("required-distinct", "t1", score=0.0, factor=0.2)
        gains = {"required-first": 10.0, "required-repeat": 16.0, "required-distinct": 8.0}

        def evaluate(action, now, _scale, _cur, _factor):
            if action["test_name"] != "required-first" and now == NOW:
                return None
            target = action["assignments"]["0"]
            required_gain = gains[action["test_name"]]
            return {"predictions": {target: {"expected_score": 0.0, "factor": 0.2,
                                              "required_gain": required_gain}},
                    "science_gain": 0.0, "required_gain": required_gain,
                    "request_gain": 0.0, "utility": required_gain}

        planner = _Planner([first, repeat, distinct], evaluate)
        chosen, summary = rerank(planner, first, NOW, NOW + timedelta(hours=1), 0, 10.0, float("inf"))

        self.assertIs(chosen, first)
        self.assertEqual(summary["selected_steps"], 2)
        self.assertEqual(summary["scenario_utility"]["persistent"], 18.0)

    def test_cpu_cutoff_returns_original_action_and_preserves_pending(self):
        base = _action("base", "t0")
        planner = _Planner([base])
        pending_before = copy.deepcopy(planner.pending)

        chosen, summary = rerank(planner, base, NOW, NOW + timedelta(hours=1), 0, 10.0, 0.0)

        self.assertIs(chosen, base)
        self.assertEqual(summary["reason"], "cpu_deadline")
        self.assertTrue(summary["fallback"])
        self.assertEqual(planner.pending, pending_before)
        self.assertEqual(planner.recorded, [])

    def test_cpu_cutoff_during_candidate_evaluation_preserves_pending(self):
        base = _action("base", "t0")
        planner = _Planner([base])
        pending_before = copy.deepcopy(planner.pending)

        with patch("short_horizon.time.process_time", side_effect=(0.0, 0.0, 0.0, 2.0)):
            chosen, summary = rerank(
                planner, base, NOW, NOW + timedelta(hours=1), 0, 10.0, 1.0
            )

        self.assertIs(chosen, base)
        self.assertEqual(summary["reason"], "cpu_deadline")
        self.assertTrue(summary["fallback"])
        self.assertEqual(planner.pending, pending_before)
        self.assertEqual(planner.recorded, [])

    def test_evaluation_exception_falls_back_without_mutating_pending(self):
        base = _action("base", "t0")
        planner = _Planner([base], lambda *_args: (_ for _ in ()).throw(RuntimeError("fake")))
        pending_before = copy.deepcopy(planner.pending)

        chosen, summary = rerank(planner, base, NOW, NOW + timedelta(hours=1), 0, 10.0, float("inf"))

        self.assertIs(chosen, base)
        self.assertEqual(summary["reason"], "evaluation_error")
        self.assertTrue(summary["fallback"])
        self.assertEqual(planner.pending, pending_before)
        self.assertEqual(planner.recorded, [])

    def test_immediate_utility_floor_blocks_a_long_horizon_regression(self):
        base = _action("base", "t0", score=100.0)
        alternative = _action("alternative", "t1", score=97.0)
        continuation = _action("continuation", "t1", score=1000.0)

        def evaluate(action, now, _scale, cur, _factor):
            if action["test_name"] == "continuation" and now == NOW:
                return None
            target = action["assignments"]["0"]
            score = action["test_score"]
            if action["test_name"] == "continuation" and now > NOW:
                score = 1000.0 if cur.get("t1", 0.0) > 0 else 0.0
            science = max(0.0, score - cur.get(target, 0.0))
            return {"predictions": {target: {"expected_score": score, "factor": 0.8}},
                    "science_gain": science, "required_gain": 0.0,
                    "request_gain": 0.0, "utility": science}

        planner = _Planner([base, alternative, continuation], evaluate)
        chosen, summary = rerank(planner, base, NOW, NOW + timedelta(hours=1), 0, 10.0, float("inf"))

        self.assertIs(chosen, base)
        self.assertEqual(summary["reason"], "immediate_utility_floor")
        self.assertTrue(summary["fallback"])
        self.assertEqual(planner.recorded, [])


if __name__ == "__main__":
    unittest.main()
