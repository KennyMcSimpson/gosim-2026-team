"""Integration contracts for the complete v7 JSONL agent, without model traffic."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import ObserverAgent
from skymath import local_sidereal_deg, parse_utc


def fixture(side=4):
    now = parse_utc("2026-10-06T02:00:00Z")
    lat, lon = -24.6157, -70.3976
    pitch = math.sqrt(0.4)
    init = {
        "site": {"latitude_deg": lat, "longitude_deg": lon,
                 "minimum_altitude_deg": 30.0, "utc_offset_hours": -4.0},
        "survey": {"start_utc": "2026-10-06T00:00:00Z", "end_utc": "2026-10-07T09:00:00Z",
                   "slot_seconds": 900,
                   "nights": [{"observing_start_utc": "2026-10-06T00:00:00Z",
                               "observing_end_utc": "2026-10-06T09:00:00Z"},
                              {"observing_start_utc": "2026-10-07T00:00:00Z",
                               "observing_end_utc": "2026-10-07T09:00:00Z"}]},
        "instrument": {"grid_side": side, "n_fibers": side * side,
                       "glass_side_deg": pitch, "pitch_deg": pitch, "fov_side_deg": side * pitch,
                       "exposure": {"min_duration_seconds": 60, "max_duration_seconds": 3600}},
        "scoring": {"q0": 0.68, "flux_zero_point": 0.5, "exposure_zero_point_seconds": 900,
                    "airmass_exponent": 0.6,
                    "lunar_model": {"maximum_penalty": 0.75, "altitude_exponent": 1.0,
                                    "angular_decay_scale_deg": 35.0},
                    "program": {"bands": {"DARK": 0.65, "BRIGHT": 0.4},
                                "multipliers": {"DARK": 1.2, "BRIGHT": 1.12, "BACKUP": 1.06},
                                "mismatch_multiplier": 1.0},
                    "required": {"penalty_per_missing": 50, "observed_factor_threshold": 0.7},
                    "uniformity": {"weight": 200, "ra_band_width_deg": 10,
                                   "observed_factor_threshold": 0.5},
                    "reporting": {"correct_reward": 100, "false_penalty": -150,
                                  "false_report_free_allowance": 2, "max_consecutive_reports": 32}},
        "targets": {"columns": ["target_id", "ra_deg", "dec_deg", "feature_flux", "science_weight", "required"],
                    "rows": [["synthetic-1", local_sidereal_deg(now, lon), lat + 8, 1, 2, True],
                             ["synthetic-2", local_sidereal_deg(now, lon) + 0.2, lat + 8.3, 1, 1, False]]}}
    payload = {"now_utc": "2026-10-06T02:00:00Z", "observe_action_index": 0, "last_result": None,
               "new_messages": [], "latest_bulletin": {"notices": []}, "active_requests": [],
               "wallclock": {"remaining_real_cpu_seconds": 120, "wall_remaining_seconds": 300}}
    return init, payload


class FakeOperations:
    def update(self, *args):
        return False

    def collect(self):
        pass

    def closed_until(self, now):
        from datetime import timedelta
        return now + timedelta(seconds=600)

    def avoid_now(self, now):
        return {"NW"}

    def report_evidence(self, now):
        return []


class IntegrationTests(unittest.TestCase):
    def test_keyless_actual_jsonl_entrypoint_all_grids(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PRO_", "V7_"))}
        for name in ("OPENAI_API_KEY", "KIMI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
            env.pop(name, None)
        env["OBSERVER_MODEL_DISABLED"] = "1"
        for side in (3, 4, 5, 10):
            init, payload = fixture(side)
            stream = [
                {"protocol_version": "participant-agent-protocol-v4", "message_type": "initialize", "payload": init},
                {"protocol_version": "participant-agent-protocol-v4", "message_type": "decision_request",
                 "decision_sequence": 1, "payload": payload},
                {"protocol_version": "participant-agent-protocol-v4", "message_type": "finish", "payload": {}}]
            run = subprocess.run([sys.executable, str(ROOT / "agent.py")],
                                 input="\n".join(map(json.dumps, stream)) + "\n", text=True,
                                 capture_output=True, timeout=20, env=env, cwd=ROOT)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn("internal error", run.stdout)
            self.assertNotIn("pro: error", run.stderr)
            answer = json.loads(run.stdout)
            self.assertEqual(answer["decision_sequence"], 1)
            self.assertEqual(answer["action"], "observe", run.stderr)
            self.assertTrue(all(0 <= int(f) < side * side for f in answer["assignments"]))
            self.assertIn("v7 audit", run.stderr)

    def test_public_closure_has_action_effect_and_ablation(self):
        init, payload = fixture()
        agent = ObserverAgent(init, rules_only=True)
        agent.operations = FakeOperations()
        with patch.dict(os.environ, {"V7_OPERATIONS_APPLY": "1"}):
            self.assertEqual(agent.respond(payload)["action"], "wait")
        with patch.dict(os.environ, {"V7_OPERATIONS_APPLY": "0"}):
            action = agent.respond(payload)
            self.assertEqual(action["action"], "observe")
        self.assertEqual(agent.audit["operation_waits"], 1)

    def test_instrument_note_cannot_bypass_weather_or_spacing(self):
        init, payload = fixture()
        agent = ObserverAgent(init, rules_only=True)
        agent.last_now = parse_utc(payload["now_utc"])
        agent.operation_report_evidence = [{"id": "source-1", "source_id": "public-note",
            "issued_at": "2026-10-06T00:00:00Z", "start_utc": "2026-10-06T00:00:00Z",
            "end_utc": "2026-10-06T04:00:00Z"}]
        agent.planner.notices = {("storm", "ALL")}
        self.assertIsNone(agent._maybe_report(2, payload))
        agent.planner.notices = set()
        agent.last_report_hours = 1
        self.assertIsNone(agent._maybe_report(2, payload))
        agent.last_report_hours = -100
        self.assertEqual(agent._maybe_report(2, payload)["action"], "report")
        self.assertIsNone(agent._fresh_instrument_evidence())

    def test_resync_invalidates_online_prediction_epoch(self):
        init, payload = fixture()
        agent = ObserverAgent(init, rules_only=True)
        first = agent.respond(payload)
        self.assertEqual(first["action"], "observe")
        self.assertIsNotNone(agent.calibration_pending)
        resync = copy.deepcopy(payload)
        resync["now_utc"] = "2026-10-06T03:00:00Z"
        resync["observe_action_index"] = 1
        resync["new_messages"] = [{"record_type": "state_resync", "best_scores": []}]
        resync["last_result"] = {"action": "observe", "observe_index": 0, "assigned_count": len(first["assignments"]),
                                 "hit_count": 1, "hits": [{"target_id": "synthetic-1", "score": 2}]}
        agent.respond(resync)
        self.assertEqual(agent.state_revision, 1)
        self.assertEqual(agent.calibrator.summary()["labeled_exposure_count"], 0)

    def test_executed_weighted_science_result_updates_gain_once(self):
        init, payload = fixture()
        agent = ObserverAgent(init, rules_only=True)
        action = agent.respond(payload)
        self.assertEqual(action["action"], "observe")
        ids = list(action["assignments"].values())
        self.assertIsNotNone(agent.calibration_pending)
        result = {"action": "observe", "observe_index": 0, "assigned_count": len(ids),
                  "hit_count": len(ids), "hits": [{"target_id": target,
                      "score": 2.4 if target == "synthetic-1" else 1.2} for target in ids]}
        next_time = parse_utc(payload["now_utc"])
        from datetime import timedelta
        next_time += timedelta(seconds=action["duration_seconds"])
        agent._calibration_feedback(result, next_time)
        summary = agent.gain_calibrator.summary()
        self.assertEqual(summary["labeled_exposure_count"], 1)
        self.assertAlmostEqual(summary["sum_labels"], sum(hit["score"] for hit in result["hits"]))
        agent._calibration_feedback(result, next_time)
        self.assertEqual(agent.gain_calibrator.summary()["labeled_exposure_count"], 1)


if __name__ == "__main__":
    unittest.main()
