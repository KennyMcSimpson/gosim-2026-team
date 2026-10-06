"""Exercise the real standard-library HTTP LLM path against a local stub.

This is evidence for call/result/application wiring only.  The stub is not a
claim about hosted model quality or a formal platform evaluation.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent / "project"
sys.path.insert(0, str(PROJECT))
from agent_core.advisor import EventAdvisor  # noqa: E402
from agent_core.llm_client import LLMClient  # noqa: E402
from agent_core.state import SurveyState  # noqa: E402
from run_v5_regressions import init_payload  # noqa: E402


class StubHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_POST(self):  # noqa: N802 - stdlib handler API
        StubHandler.requests += 1
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size).decode("utf-8"))
        user = json.loads(body["messages"][1]["content"])
        if user.get("notices"):
            content = {"avoid_directions": ["S"], "confidence": 0.95}
        else:
            content = {"priority": "balanced", "risk_mode": "conservative"}
        response = {"choices": [{"message": {"content": json.dumps(content)}}]}
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


class Trace:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(dict(event))


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    old = {key: os.environ.get(key) for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY")}
    os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
    os.environ["OPENAI_API_KEY"] = "local-smoke-only"
    trace = Trace()
    try:
        state = SurveyState(init_payload())
        advisor = EventAdvisor(LLMClient(call_timeout_seconds=2), trace, lambda _text: None)
        payload = {"latest_bulletin": {"notices": [{
            "event_kind": "rocket_launch", "direction": "S", "text": "local south sector"
        }]}, "active_requests": []}
        for _ in range(100):
            advisor.update(state, payload, 0, 1000.0, [])
            if any(event.get("event") == "llm_applied" and
                   event.get("role") == "notice_interpretation" and
                   event.get("applied") for event in trace.events):
                break
            time.sleep(0.01)
        applied = next(event for event in trace.events
                        if event.get("event") == "llm_applied" and
                        event.get("role") == "notice_interpretation")
        assert state.extra_avoid == {"S"}
        assert applied["succeeded"] and applied["parsed"] and applied["changed"]
        assert applied["decision_consumer"] == "Planner._direction_factor"
        result = {
            "http_requests": StubHandler.requests,
            "state_extra_avoid": sorted(state.extra_avoid),
            "llm_result": next(event for event in trace.events
                                if event.get("event") == "llm_result" and
                                event.get("role") == "notice_interpretation"),
            "llm_applied": applied,
        }
    finally:
        server.shutdown()
        server.server_close()
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    (HERE / "llm_http_smoke.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                                encoding="utf-8")
    (HERE / "llm_http_smoke_trace.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in trace.events),
        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
