"""Focused public-contract tests for planner scoring and geometry helpers."""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planner import (  # noqa: E402
    Planner,
    _all_or_nothing_request_reward,
    _central_fiber_ids,
    _fibers_to_search,
)
from skymath import FiberGrid, parse_utc  # noqa: E402
from test_agent_integration import fixture  # noqa: E402


class PlannerContractTests(unittest.TestCase):
    def make_planner(self, side=4):
        init, _ = fixture(side)
        return Planner(init, log=lambda _: None)

    def test_required_contract_comes_from_public_config(self):
        init, _ = fixture()
        init["scoring"]["required"] = {
            "penalty_per_missing": 73,
            "observed_factor_threshold": 0.72,
        }
        planner = Planner(init, log=lambda _: None)
        self.assertEqual(planner.required_penalty, 73.0)
        self.assertEqual(planner.required_threshold, 0.72)

    def test_request_reward_is_all_or_nothing_and_deadline_bounded(self):
        deadline = parse_utc("2026-10-06T03:00:00Z")
        request = {
            "target_indices": (1, 2),
            "remaining_count": 2,
            "threshold": 0.7,
            "reward": 25.0,
            "deadline": deadline,
        }
        self.assertEqual(_all_or_nothing_request_reward([request], {1: 0.71}, deadline), 0.0)
        self.assertEqual(_all_or_nothing_request_reward([request], {1: 0.71, 2: 0.70}, deadline), 25.0)
        self.assertEqual(
            _all_or_nothing_request_reward([request], {1: 0.71, 2: 0.70}, deadline + timedelta(seconds=1)),
            0.0,
        )

    def test_resync_rebuilds_best_scores_and_clears_learning_and_pending_state(self):
        planner = self.make_planner()
        target_id = planner.ids[0]
        retained_score = planner.weight[0] * max(planner.multipliers.values()) * 0.4
        planner.samples.append((1.0, 0.7))
        planner.band_obs.append((1.0, 0.8, "DARK", True, True))
        planner.misses[0] = 2
        planner.attempts[0] = 1
        planner.pending[target_id] = {"model": 1.0}
        planner.requests = [{"reward": 5.0}]
        planner.request_anchor_bonus = {0: 5.0}

        planner._resync({"best_scores": [{"target_id": target_id, "best_score": retained_score}]})

        self.assertAlmostEqual(planner.factor[0], 0.4)
        self.assertAlmostEqual(planner.cur[0], retained_score / planner.weight[0])
        self.assertEqual(planner.factor[1], 0.0)
        self.assertFalse(planner.samples)
        self.assertFalse(planner.band_obs)
        self.assertEqual(planner.misses, [0] * len(planner.ids))
        self.assertEqual(planner.attempts, [0] * len(planner.ids))
        self.assertEqual(planner.pending, {})
        self.assertEqual(planner.requests, [])
        self.assertEqual(planner.request_anchor_bonus, {})

    def test_fiber_search_scales_with_configured_grid_geometry(self):
        for side in (3, 4, 5, 10):
            with self.subTest(side=side):
                init, _ = fixture(side)
                grid = FiberGrid(init["instrument"])
                all_fibers = _fibers_to_search(grid, 0)
                central = _fibers_to_search(grid, 2)
                one_center = _fibers_to_search(grid, 3)
                self.assertEqual(len(all_fibers), side * side)
                self.assertEqual(len(central), min(4, side * side))
                self.assertEqual(len(one_center), 1)
                self.assertEqual(len(set(central)), len(central))
                self.assertTrue(all(0 <= fiber < side * side for fiber in central))
                self.assertEqual(_central_fiber_ids(grid, side * side), tuple(
                    sorted(range(side * side), key=lambda fiber: (
                        sum(value * value for value in grid.fiber_center(fiber)), fiber
                    ))
                ))

    def test_action_evaluation_is_read_only_and_emits_rerank_candidates(self):
        init, _ = fixture()
        planner = Planner(init, log=lambda _: None)
        planner.band_obs.extend((2.0, 0.8, "DARK", True, True) for _ in range(4))
        now = parse_utc("2026-10-06T02:00:00Z")
        night_end = parse_utc("2026-10-06T09:00:00Z")
        action = planner.plan(now, night_end, 0, 2.0)
        self.assertIsNotNone(action)
        self.assertTrue(planner.last_candidates)
        candidate = planner.last_candidates[0]
        self.assertTrue({
            "action", "utility", "science_gain", "base_science_gain", "required_gain",
            "request_gain", "predictions", "features", "estimated_scores",
        }.issubset(candidate))
        self.assertTrue(candidate["predictions"])
        for prediction in candidate["predictions"].values():
            self.assertTrue({"expected_score", "factor", "model", "band_model", "alt", "az", "fiber"}.issubset(prediction))

        calls = []

        class ReadOnlyCalibrator:
            def predict_gain(self, base_gain, features, *, record_gate=True):
                calls.append((record_gate, features))
                return base_gain

        planner.gain_calibrator = ReadOnlyCalibrator()
        planner.band_level = 123.456
        pending_before = {target_id: dict(value) for target_id, value in planner.pending.items()}
        pending_state = (planner.pending_program, planner.pending_duration, planner.pending_night, planner.pending_cmd)
        estimate = planner.evaluate_action(action, now, night_end, 0, 2.0)
        self.assertIsNotNone(estimate)
        self.assertTrue(calls)
        self.assertTrue(all(record_gate is False for record_gate, _ in calls))
        self.assertEqual(planner.band_level, 123.456)
        self.assertEqual(planner.pending, pending_before)
        self.assertEqual(
            (planner.pending_program, planner.pending_duration, planner.pending_night, planner.pending_cmd),
            pending_state,
        )


if __name__ == "__main__":
    unittest.main()
