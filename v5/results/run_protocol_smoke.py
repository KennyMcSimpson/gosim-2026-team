"""Run the v5 JSONL entry point without the local runner's socket layer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent / "project"
sys.path.insert(0, str(HERE))
from run_v5_regressions import init_payload  # noqa: E402


def main() -> None:
    trace_path = HERE / "protocol_smoke_llm_trace.jsonl"
    env = dict(os.environ)
    env["OPENAI_BASE_URL"] = "http://127.0.0.1:1"
    env["OPENAI_API_KEY"] = "local-smoke-only"
    env["AGENT_TRACE_PATH"] = str(trace_path)
    python = sys.executable
    messages = [
        {"protocol_version": "participant-agent-protocol-v4",
         "message_type": "initialize", "payload": init_payload()},
        {"protocol_version": "participant-agent-protocol-v4",
         "message_type": "decision_request", "decision_sequence": 0,
         "payload": {
             "now_utc": "2026-11-02T01:05:00+00:00",
             "wallclock": {"remaining_real_cpu_seconds": 20,
                           "remaining_wallclock_seconds": 1200},
             "latest_bulletin": {"notices": []},
             "new_messages": [], "active_requests": [], "last_result": None,
         }},
        {"protocol_version": "participant-agent-protocol-v4",
         "message_type": "finish", "payload": {"termination_reason": "smoke"}},
    ]
    data = "\n".join(json.dumps(message, separators=(",", ":")) for message in messages) + "\n"
    completed = subprocess.run([python, "agent.py"], cwd=PROJECT, env=env,
                               input=data, text=True, capture_output=True, timeout=30)
    stdout_lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    responses = [line for line in stdout_lines if line.get("message_type") == "decision_response"]
    assert completed.returncode == 0, completed.stderr
    assert len(responses) == 1, stdout_lines
    response = responses[0]
    assert response["protocol_version"] == "participant-agent-protocol-v4"
    assert response["decision_sequence"] == 0
    assert response["action"] in {"observe", "wait", "report", "finish"}
    result = {"returncode": completed.returncode,
              "stdout_messages": len(stdout_lines),
              "decision_response": response,
              "stderr_tail": completed.stderr.splitlines()[-10:],
              "trace_path": str(trace_path)}
    (HERE / "protocol_smoke.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
