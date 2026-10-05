from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agent_core.pi_client import PiClient, _role_for_prompt


class BridgeTests(unittest.TestCase):
    def test_role_mapping(self):
        self.assertEqual(_role_for_prompt("submit candidate_id"), "plan_review")
        self.assertEqual(_role_for_prompt("submit avoid_directions"), "notice_interpretation")

    def test_strict_candidate_schema(self):
        valid = {"candidate_id": "c1", "decision_token": "fresh", "rationale": "risk"}
        self.assertEqual(PiClient._validate("plan_review", valid), valid)
        self.assertIsNone(PiClient._validate("plan_review", dict(valid, action="observe")))
        self.assertIsNone(PiClient._validate("plan_review", dict(valid, rationale="do-anything")))

    def test_no_key_never_starts_process(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "KIMI_API_KEY": "", "AGENT_MODEL_BACKEND": "pi-pro"}):
            client = PiClient()
        with patch.object(client, "_start_worker", side_effect=AssertionError("process launched")):
            self.assertIsNone(client.review_plan({"candidates": []}, 1600))
        self.assertEqual(client.calls_made, 0)
        client.close()

    def test_role_question_has_one_turn_and_tool(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic", "AGENT_MODEL_BACKEND": "pi-pro", "PI_ENABLED": "1"}):
            client = PiClient()
        captured = []
        answer = {"candidate_id": "baseline", "decision_token": "fresh", "rationale": "baseline"}
        with patch.object(client, "_start_worker", return_value=True), \
                patch.object(client, "_write", side_effect=lambda value: captured.append(value) or True), \
                patch.object(client, "_wait_for", return_value=({"ok": True, "candidate": answer, "attempts": 1}, 1)):
            result = client.review_plan({"decision_token": "fresh", "candidates": [
                {"candidate_id": "baseline", "allowed_rationales": ["baseline"]}]}, 1600)
        self.assertEqual(result, answer)
        self.assertEqual(captured[0]["max_turns"], 1)
        self.assertEqual(captured[0]["max_tool_calls"], 1)
        self.assertEqual(client.calls_made, 1)
        client.close()

    def test_unknown_id_and_wrong_token_rejected(self):
        client = PiClient()
        ctx = {"decision_token": "fresh", "candidates": [
            {"candidate_id": "baseline", "allowed_rationales": ["baseline"]}]}
        for answer in ({"candidate_id": "invented", "decision_token": "fresh", "rationale": "baseline"},
                       {"candidate_id": "baseline", "decision_token": "stale", "rationale": "baseline"}):
            with patch.object(client, "ask_json", return_value=answer):
                self.assertIsNone(client.review_plan(ctx, 1600))
        client.close()

    def test_legacy_json_prompt_becomes_tool_instruction(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic", "AGENT_MODEL_BACKEND": "pi-fixed", "PI_ENABLED": "1"}):
            client = PiClient()
        captured = []
        with patch.object(client, "_start_worker", return_value=True), \
                patch.object(client, "_write", side_effect=lambda value: captured.append(value) or True), \
                patch.object(client, "_wait_for", return_value=({"ok": True, "candidate": {
                    "priority": "required", "risk_mode": "balanced"}, "attempts": 1}, 1)):
            client.ask_json('Return JSON {"priority":"required","risk_mode":"balanced"}.', {}, 1600)
        self.assertNotIn('Return JSON', captured[0]['prompt'])
        self.assertIn('emit_candidate', captured[0]['prompt'])
        client.close()


if __name__ == "__main__":
    unittest.main()
