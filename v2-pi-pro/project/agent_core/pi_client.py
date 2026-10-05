"""Bounded JSON adapter for the optional Pi worker.

The competition process remains Python-owned.  This client only sends the
public context used by an advisor role to a long-lived Node process and accepts
one of three small, validated candidate objects in return.  A missing key,
worker failure, timeout, cancellation, or schema mismatch returns ``None`` so
the caller can keep its deterministic rule path.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_MODEL = "k3"
WALL_RESERVE_SECONDS = 300.0
QUESTION_DEADLINE_SECONDS = 8.0
DEFAULT_MAX_CALLS = 64
DEFAULT_CALL_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_CONTEXT_BYTES = 12_000

_DIRECTIONS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "future",
    "ground_truth",
    "hidden",
    "password",
    "secret",
    "token",
    "truth",
}


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _positive_float(value: str | None, default: float) -> float:
    try:
        result = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _positive_int(value: str | None, default: int) -> int:
    try:
        result = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _attempt_count(value: Any, default: int = 1) -> int:
    """Read a worker attempt counter without allowing malformed values through."""
    try:
        count = int(value.get("attempts", default)) if isinstance(value, dict) else int(value)
    except (TypeError, ValueError):
        count = default
    return max(1, count)


def _role_for_prompt(prompt: str) -> str:
    if "candidate_id" in prompt:
        return "plan_review"
    if "avoid_directions" in prompt:
        return "notice_interpretation"
    if '"report"' in prompt:
        return "fault_confirmation"
    return "feedback_adaptation"


def _safe_context(value: Any, depth: int = 0) -> Any:
    """Copy public JSON while dropping obvious credentials/future truth fields."""
    if depth > 6:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:1_200]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key.lower().replace("-", "_") in _SENSITIVE_KEYS:
                continue
            result[key[:120]] = _safe_context(raw_value, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_context(item, depth + 1) for item in list(value)[:100]]
    return str(value)[:1_200]


def _bounded_context(value: Any, max_bytes: int) -> dict[str, Any]:
    safe = _safe_context(value)
    if not isinstance(safe, dict):
        safe = {"value": safe}
    try:
        encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return {"context_unavailable": True}
    if len(encoded) <= max_bytes:
        return safe

    # Keep the shape useful when a public request contains a large list.  The
    # worker also applies a bound, so this is a second boundary at the process
    # crossing rather than a claim that the full snapshot was supplied.
    compact: dict[str, Any] = {"context_truncated": True}
    for key, item in safe.items():
        compact[key] = item
        try:
            if len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_bytes:
                compact.pop(key, None)
                break
        except (TypeError, ValueError):
            compact.pop(key, None)
    return compact


class PiClient:
    """Thread-safe, persistent bridge to the bundled Pi Node worker."""

    def __init__(
        self,
        log=lambda text: None,
        call_timeout_seconds: float | None = None,
        max_calls: int | None = None,
    ):
        self.log = log
        self.base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
        self.model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
        configured_calls = max_calls if max_calls is not None else _positive_int(
            os.environ.get("PI_MAX_CALLS"), DEFAULT_MAX_CALLS
        )
        self.max_calls = min(DEFAULT_MAX_CALLS, max(1, int(configured_calls)))
        configured_timeout = call_timeout_seconds if call_timeout_seconds is not None else _positive_float(
            os.environ.get("PI_CALL_TIMEOUT_SECONDS"), DEFAULT_CALL_TIMEOUT_SECONDS
        )
        # Eight seconds is the adapter's per-model-attempt ceiling.  A smaller
        # explicit value remains useful for deterministic offline tests.
        self.call_timeout_seconds = min(DEFAULT_CALL_TIMEOUT_SECONDS, max(0.05, float(configured_timeout)))
        self.max_context_bytes = _positive_int(os.environ.get("PI_MAX_CONTEXT_BYTES"), DEFAULT_MAX_CONTEXT_BYTES)
        self.calls_made = 0
        self.last_outcome = {}
        self._closed = False
        self._state_lock = threading.RLock()
        # Only one request may use the JSONL worker at a time.  This lock is
        # deliberately distinct from _state_lock so close() can kill a blocked
        # process while ask_json is waiting for its response.
        self._request_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._request_counter = 0
        self.enabled = _enabled(os.environ.get("PI_ENABLED")) and os.environ.get(
            "AGENT_MODEL_BACKEND", "pi"
        ).strip().lower() not in {"disabled", "v2"}
        self._has_key = bool(
            os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("KIMI_API_KEY", "").strip()
        )
        self.worker_path = Path(__file__).resolve().parents[1] / "pi_adapter" / "worker.bundle.mjs"

    def _node_command(self) -> list[str] | None:
        custom = os.environ.get("PI_WORKER_COMMAND", "").strip()
        if custom:
            try:
                command = shlex.split(custom, posix=os.name != "nt")
            except ValueError:
                return None
            if os.name == "nt":
                # ``shlex`` keeps quote characters in non-posix mode; remove
                # only a matching wrapper so paths such as ``Program Files``
                # remain usable as subprocess argv entries.
                command = [
                    part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"} else part
                    for part in command
                ]
            return command or None

        node_override = os.environ.get("PI_NODE_EXECUTABLE", "").strip()
        if node_override:
            node = node_override
        else:
            adapter = self.worker_path.parent
            candidates = [
                adapter / ".runtime" / "node-v22.20.0-linux-x64" / "bin" / "node",
                adapter / ".runtime" / "node-v22.20.0-linux-x64" / "bin" / "node.exe",
            ]
            node = next((str(path) for path in candidates if path.is_file()), None) or shutil.which("node")
        if not node or not self.worker_path.is_file():
            return None
        return [node, str(self.worker_path)]

    def _start_worker(self) -> bool:
        with self._state_lock:
            process = self._process
        if process is not None and process.poll() is None:
            return True
        if process is not None:
            self._retire_worker(expected=process)
        command = self._node_command()
        if command is None:
            self.log("pi: Node worker or pinned runtime is unavailable; using rule-based path")
            return False
        env = dict(os.environ)
        env["PI_ADAPTER_BASE_URL"] = self.base_url
        env["PI_ADAPTER_MODEL"] = self.model
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.worker_path.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except (OSError, ValueError) as exc:
            self.log(f"pi: failed to start worker ({type(exc).__name__})")
            return False
        with self._state_lock:
            if self._closed:
                try:
                    process.kill()
                except OSError:
                    pass
                return False
            self._process = process
        self._stdout_thread = threading.Thread(target=self._read_stdout, args=(process,), daemon=True, name="pi-stdout")
        self._stderr_thread = threading.Thread(target=self._read_stderr, args=(process,), daemon=True, name="pi-stderr")
        self._stdout_thread.start()
        self._stderr_thread.start()
        return True

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    self.log("pi: ignored non-JSON worker output")
                    continue
                if isinstance(value, dict):
                    self._responses.put(value)
        except (OSError, ValueError):
            pass

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        key_values = [value for value in (os.environ.get("OPENAI_API_KEY"), os.environ.get("KIMI_API_KEY")) if value]
        try:
            for line in process.stderr:
                message = line.rstrip()
                for value in key_values:
                    message = message.replace(value, "<redacted>")
                if message:
                    self.log(f"pi-worker: {message[:800]}")
        except (OSError, ValueError):
            pass

    def _write(self, message: dict[str, Any]) -> bool:
        with self._state_lock:
            process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            return False
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def _wait_for(
        self, request_id: str, deadline: float, attempt_timeout_seconds: float
    ) -> tuple[dict[str, Any] | None, int]:
        attempts_seen = 0
        attempt_started: float | None = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            if attempt_started is not None and now - attempt_started >= attempt_timeout_seconds:
                return None, attempts_seen
            with self._state_lock:
                process = self._process
            if process is None or process.poll() is not None:
                return None, attempts_seen
            try:
                wait_for = deadline - now
                if attempt_started is not None:
                    wait_for = min(wait_for, attempt_timeout_seconds - (now - attempt_started))
                response = self._responses.get(timeout=max(0.01, min(0.2, wait_for)))
            except queue.Empty:
                continue
            if response.get("id") != request_id:
                continue
            if response.get("type") == "attempt":
                attempt_started = time.monotonic()
                try:
                    attempts_seen = max(attempts_seen, int(response.get("attempt", 0)))
                except (TypeError, ValueError):
                    pass
                continue
            # Final replies intentionally have no required `type` field; the
            # presence of ok/candidate/error distinguishes them from progress.
            attempts_seen = max(attempts_seen, _attempt_count(response, default=1))
            return response, attempts_seen
        return None, attempts_seen

    def _cancel(self, request_id: str) -> None:
        self._write({"type": "cancel", "id": request_id})

    def _retire_worker(self, expected: subprocess.Popen[str] | None = None) -> None:
        """Detach and terminate a worker so no timed-out request can continue."""
        with self._state_lock:
            process = self._process
            if expected is not None and process is not expected:
                return
            self._process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=0.3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=0.3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    @staticmethod
    def _validate(role: str, candidate: Any) -> dict[str, Any] | None:
        if not isinstance(candidate, dict):
            return None
        if role == "notice_interpretation":
            directions = candidate.get("avoid_directions")
            confidence = candidate.get("confidence")
            if not isinstance(directions, list) or len(directions) > 8:
                return None
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                return None
            if confidence != confidence or confidence < 0.0 or confidence > 1.0:
                return None
            normalized = sorted({str(direction).upper() for direction in directions} & _DIRECTIONS)
            if any(not isinstance(direction, str) or str(direction).upper() not in _DIRECTIONS for direction in directions):
                return None
            return {"avoid_directions": normalized, "confidence": confidence}
        if role == "feedback_adaptation":
            priority, risk = candidate.get("priority"), candidate.get("risk_mode")
            if priority not in {"science", "balanced", "required", "request"}:
                return None
            if risk not in {"balanced", "conservative"}:
                return None
            return {"priority": priority, "risk_mode": risk}
        if role == "fault_confirmation":
            report = candidate.get("report")
            return {"report": report} if isinstance(report, bool) else None
        if role == "plan_review":
            if set(candidate) != {"candidate_id", "decision_token", "rationale"}:
                return None
            if (not isinstance(candidate["candidate_id"], str) or
                    not isinstance(candidate["decision_token"], str) or
                    not isinstance(candidate["rationale"], str) or
                    candidate["rationale"] not in {"baseline", "required_deadline", "request_deadline",
                                                  "risk", "uniformity", "opportunity"}):
                return None
            return dict(candidate)
        return None

    def review_plan(self, context, wall_left_seconds):
        answer = self.ask_json(
            'Review current numerical plans and submit candidate_id, decision_token and rationale. '
            'Keep baseline unless an offered alternative has a concrete advantage in its allowed_rationales. '
            'Required last windows and expiring request rewards can justify a bounded rate tradeoff. '
            'Risk estimates and utility are uncertain; do not assume future weather. '
            'Copy the current decision_token exactly. Do not create actions or code.',
            context, wall_left_seconds)
        if answer is None:
            return None
        offered = {p.get("candidate_id"): p for p in context.get("candidates", [])}
        cid = answer["candidate_id"]
        if (cid not in offered or answer["decision_token"] != context.get("decision_token") or
                answer["rationale"] not in offered[cid].get("allowed_rationales", [])):
            self.last_outcome["reason"] = "invalid_plan_selection"
            return None
        return answer

    def ask_json(self, system_prompt: str, user_payload: dict[str, Any], wall_left_seconds: float) -> Optional[dict[str, Any]]:
        """Ask one bounded role question, returning ``None`` on every bridge failure."""
        role = _role_for_prompt(system_prompt)
        started = time.monotonic()
        self.last_outcome = {"role": role, "reason": "unavailable", "attempts": 0}
        available = min(QUESTION_DEADLINE_SECONDS, float(wall_left_seconds) - WALL_RESERVE_SECONDS)
        if self._closed or not self.enabled or not self._has_key or available < 2.0:
            return None

        with self._request_lock:
            with self._state_lock:
                if self._closed or self.calls_made >= self.max_calls:
                    return None
            if not self._start_worker():
                return None
            with self._state_lock:
                if self._closed or self.calls_made >= self.max_calls:
                    return None
                remaining = self.max_calls - self.calls_made
            max_turns = 1
            context = _bounded_context(user_payload, self.max_context_bytes)
            if role == "plan_review" and context.get("context_truncated"):
                self.last_outcome["reason"] = "context_too_large"
                return None
            self._request_counter += 1
            request_id = f"pi-{os.getpid()}-{self._request_counter}-{uuid.uuid4().hex[:8]}"
            attempt_timeout_ms = max(100, int(min(self.call_timeout_seconds, available) * 1000))
            question_timeout_ms = max(1_000, int(available * 1000))
            request = {
                "type": "ask",
                "id": request_id,
                "role": role,
                "prompt": str(system_prompt)[:4_000],
                "context": context,
                "max_turns": max_turns,
                "attempt_timeout_ms": attempt_timeout_ms,
                "timeout_ms": question_timeout_ms,
                "max_tool_calls": 1,
            }
            if not self._write(request):
                self._retire_worker()
                return None
            # Every question permits exactly one provider generation.
            self.calls_made += 1
            response, attempts_seen = self._wait_for(
                request_id,
                time.monotonic() + question_timeout_ms / 1000.0 + 0.25,
                attempt_timeout_seconds=self.call_timeout_seconds,
            )
            if response is None:
                self._cancel(request_id)
                # A cancel message cannot be trusted to interrupt a provider
                # stream.  Retire the process and conservatively charge the
                # whole bounded turn budget before the next request.
                self._retire_worker()
                self.calls_made = min(self.max_calls, self.calls_made - 1 + max_turns)
                self.last_outcome.update(reason="timeout", attempts=max(1, attempts_seen),
                                         latency_seconds=round(time.monotonic() - started, 4))
                self.log("pi: request timed out; using the rule-based path")
                return None
            attempts = max(attempts_seen, _attempt_count(response, default=1))
            self.calls_made = min(self.max_calls, self.calls_made + max(0, attempts - 1))
            candidate = self._validate(role, response.get("candidate")) if response.get("ok") else None
            error = str(response.get("error", "schema_rejected"))[:80]
            if error in {"question_timeout", "attempt_timeout"}:
                self._retire_worker()
                error = "timeout"
            self.last_outcome.update(reason="accepted" if candidate is not None else error,
                                     attempts=attempts, latency_seconds=round(time.monotonic() - started, 4))
            if candidate is None and response.get("error"):
                self.log(f"pi: {str(response['error'])[:240]}; using the rule-based path")
            return candidate

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
        if process is None:
            return
        # Do not wait for _request_lock: ask_json may be blocked in a provider
        # call.  Detach first, then give a healthy worker a short shutdown
        # window, falling back to bounded termination.
        try:
            self._write({"type": "shutdown"})
            process.wait(timeout=0.8)
        except (OSError, subprocess.TimeoutExpired):
            self._retire_worker(expected=process)
        else:
            with self._state_lock:
                if self._process is process:
                    self._process = None
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
