#!/usr/bin/env python3
"""Entry point for the python-agent example (participant-agent-protocol-v4).

Reads one JSON object per line on stdin, writes one JSON object per line on
stdout, logs only to stderr. The loop itself is deliberately thin: all the
decision-making lives in agent_core/ (protocol I/O, state tracking, geometry,
scoring, planning, the LLM client, memory/log, action validation) so this file
stays a readable map of "what happens for each message type".

Pi advice is optional. Without a configured API key, planning follows the v2
rules. AGENT_MODEL_BACKEND=v2 selects the original model client and startup
key check for comparison.

  initialize        -> build SurveyState + Planner from the public payload
  decision_request   -> Planner.decide(), validated, sent back as decision_response
  finish              -> Planner.on_finish() logs a summary and the process exits

Pacing uses the fair clock (agent_core/clock.py): the budget is CPU time, so the
agent measures its own cost with process CPU time, not with a wall clock.

Any planner exception is caught here and replaced with a safe fallback action --
a bug in the strategy must never end the run as agent_error or hang the process.
"""
from __future__ import annotations

import os
import sys

if sys.version_info < (3, 9):
    sys.stderr.write("agent: Python 3.9 or newer is required\n")
    raise SystemExit(3)

from agent_core.llm_client import MissingAPIKeyError, require_api_key
from agent_core.planner import Planner
from agent_core.protocol import log, read_messages, send_response
from agent_core.state import SurveyState
from agent_core.validation import ActionRejected, fallback_action, validate_action


def main() -> int:
    if os.environ.get("AGENT_MODEL_BACKEND", "pi-pro").strip().lower() == "v2":
        try:
            require_api_key()
        except MissingAPIKeyError as exc:
            log(f"agent: {exc}")
            return 1

    state = None
    planner = None
    try:
        for message in read_messages(sys.stdin):
            kind = message.get("message_type")

            if kind == "initialize":
                if planner is not None:
                    planner.close()
                try:
                    state = SurveyState(message["payload"])
                    planner = Planner(state, log=log)
                except Exception as exc:  # noqa: BLE001
                    log(f"agent: failed to initialize ({type(exc).__name__}: {exc}); will fall back on every decision")
                    state = None
                    planner = None

            elif kind == "decision_request":
                sequence = message["decision_sequence"]
                if planner is not None:
                    planner.clock.start_decision()
                consecutive_reports = planner.consecutive_reports if planner is not None else 0
                try:
                    action = planner.decide(message["payload"]) if planner is not None else fallback_action("not initialized")
                    action = validate_action(action, state, consecutive_reports)
                except ActionRejected as exc:
                    log(f"agent: planner produced an invalid action ({exc}); falling back")
                    action = fallback_action("validation-rejected")
                except Exception as exc:  # noqa: BLE001
                    log(f"agent: planner error ({type(exc).__name__}: {exc}); falling back")
                    action = fallback_action("planner-exception")
                if planner is not None:
                    planner.note_action(action)
                send_response(sequence, action)
                if planner is not None:
                    planner.clock.end_decision()

            elif kind == "finish":
                if planner is not None:
                    try:
                        planner.on_finish(message.get("payload", {}))
                    except Exception as exc:  # noqa: BLE001
                        log(f"agent: error during finish logging ({type(exc).__name__}: {exc})")
                break
    finally:
        if planner is not None:
            planner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
