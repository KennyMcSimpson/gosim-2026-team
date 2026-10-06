"""Bounded OpenAI-compatible chat client for the pro agent (standard library only)."""
from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_MODEL = "k3"
STAGES = ("night_plan", "fault_review", "operations", "confirm_report")
MAX_RECENT_CALLS = 64
QUESTION_DEADLINE_SECONDS = 45.0
MAX_ATTEMPTS_PER_REQUEST = 3
MAX_BACKOFF_SECONDS = 1.0
WALLCLOCK_RESERVE_SECONDS = 30.0
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_SAFE_TAGS = {"night_plan", "fault_review", "confirm_report", "operations", "model_call"}


def api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("KIMI_API_KEY", "").strip()


def load_dotenv(path: str) -> None:
    """Fill missing environment variables from a local .env (for local runs only)."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name, value = name.strip(), value.strip().strip('"').strip("'")
                if name and value and not os.environ.get(name):
                    os.environ[name] = value
    except OSError:
        pass


def _bounded_float(value, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return min(high, max(low, parsed))


def _bounded_int(value, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return min(high, max(low, parsed))


def _safe_tag(tag: str) -> str:
    value = str(tag)
    return value if value in _SAFE_TAGS else "model_call"


def _stage_for_tag(tag: str) -> str:
    value = str(tag)
    if value in ("night_plan", "fault_review", "confirm_report"):
        return value
    return "operations"


class ModelReply:
    """Small Future-like result that also preserves the pro agent's Call interface."""

    def __init__(self, tag: str):
        self.tag = _safe_tag(tag)
        self.stage = _stage_for_tag(tag)
        self.answer = None
        self.error = None
        self.seconds = 0.0
        self.attempts_made = 0
        self._deadline = None
        self._logged = False
        self._done = threading.Event()

    def _finish(self, answer=None, error=None, seconds: float = 0.0) -> None:
        if self._done.is_set():
            return
        self.answer = answer
        self.error = error
        self.seconds = max(0.0, float(seconds))
        self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, seconds: float) -> bool:
        return self._done.wait(max(0.0, float(seconds)))

    def result(self, timeout=None):
        if not self._done.wait(None if timeout is None else max(0.0, float(timeout))):
            raise TimeoutError("model reply is not ready")
        if self.error == "timeout":
            raise TimeoutError("model request timed out")
        if self.error is not None:
            raise RuntimeError(f"model request failed ({self.error})")
        return self.answer

    def exception(self, timeout=None):
        if not self._done.wait(None if timeout is None else max(0.0, float(timeout))):
            raise TimeoutError("model reply is not ready")
        if self.error == "timeout":
            return TimeoutError("model request timed out")
        if self.error is not None:
            return RuntimeError(f"model request failed ({self.error})")
        return None


# Kept for code that imports the pro client's original handle name.
Call = ModelReply


class LLMClient:
    def __init__(self, log=lambda text: None, call_timeout: float = 90.0, max_calls: int | None = None,
                 max_retries: int = 3, max_in_flight: int = 4):
        self.log = log
        self.base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
        self.key = api_key()
        self.enabled = bool(self.key)
        self.disabled = not self.enabled
        self.model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
        self.call_timeout = _bounded_float(call_timeout, QUESTION_DEADLINE_SECONDS, 0.1,
                                           QUESTION_DEADLINE_SECONDS)
        try:
            self.max_calls = None if max_calls is None else max(0, int(max_calls))
        except (TypeError, ValueError, OverflowError):
            self.max_calls = None
        # max_retries historically acts as a total-attempt count in this client.
        self.max_retries = _bounded_int(max_retries, MAX_ATTEMPTS_PER_REQUEST, 1,
                                        MAX_ATTEMPTS_PER_REQUEST)
        self.max_in_flight = _bounded_int(max_in_flight, 4, 1, 4)
        self.calls: list[ModelReply] = []
        self.ok = 0
        self.failed = 0
        self.rejected = 0
        self.timeouts = 0
        self.deadline_before_attempt = 0
        self.retries = 0
        self.no_key = 0
        self.queue_full = 0
        self._attempts = 0
        self._by_stage = {
            stage: {"attempts": 0, "success": 0, "failure": 0, "rejected": 0,
                    "timeout": 0, "deadline_before_attempt": 0, "retries": 0}
            for stage in STAGES
        }
        self._lock = threading.RLock()
        self._jobs: queue.Queue = queue.Queue(maxsize=self.max_in_flight)
        self._worker = None
        self._closed = False

    @property
    def calls_made(self) -> int:
        """Number of outbound HTTP attempts, for Operations metrics."""
        with self._lock:
            return self._attempts

    def _request(self, system: str, user, timeout: float, max_tokens: int = 2000) -> dict:
        user_text = user if isinstance(user, str) else json.dumps(
            user, ensure_ascii=False, separators=(",", ":")
        )
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/chat/completions", data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + self.key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"] or ""
        match = _JSON_OBJECT.search(text)
        if not match:
            raise ValueError("no JSON object in the reply")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("reply is not a JSON object")
        return parsed

    def in_flight(self) -> int:
        with self._lock:
            return sum(1 for call in self.calls if not call.done())

    def _prune_calls_locked(self) -> None:
        completed = [call for call in self.calls if call.done()]
        keep_completed = {id(call) for call in completed[-MAX_RECENT_CALLS:]}
        self.calls[:] = [call for call in self.calls if not call.done() or id(call) in keep_completed]

    def _reject(self, error: str, stage: str) -> None:
        with self._lock:
            if error == "no_key":
                self.no_key += 1
            else:
                self.rejected += 1
                self._by_stage[stage]["rejected"] += 1
                if error == "deadline_before_attempt":
                    self.deadline_before_attempt += 1
                    self._by_stage[stage]["deadline_before_attempt"] += 1
                if error == "queue_full":
                    self.queue_full += 1

    def _start_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="llm-client", daemon=True)
        self._worker.start()

    def _submit(self, tag: str, system: str, user, timeout: float, max_tokens: int):
        stage = _stage_for_tag(tag)
        if not self.enabled:
            self._reject("no_key", stage)
            return None, "no_key"
        timeout = _bounded_float(timeout, self.call_timeout, 0.0, self.call_timeout)
        if timeout <= 0:
            self._reject("deadline_before_attempt", stage)
            return None, "timeout"
        max_tokens = _bounded_int(max_tokens, 2000, 1, 8192)
        with self._lock:
            if self._closed:
                self._reject("closed", stage)
                return None, "closed"
            if self.max_calls is not None and self._attempts >= self.max_calls:
                self._reject("attempt_limit", stage)
                return None, "attempt_limit"
            in_flight = sum(1 for call in self.calls if not call.done())
            if in_flight >= self.max_in_flight:
                self._reject("queue_full", stage)
                return None, "queue_full"
            reply = ModelReply(tag)
            reply._deadline = time.monotonic() + timeout
            try:
                self._jobs.put_nowait((reply, system, user, max_tokens))
            except queue.Full:
                self._reject("queue_full", stage)
                return None, "queue_full"
            self.calls.append(reply)
            self._prune_calls_locked()
            self._start_worker_locked()
            return reply, None

    def submit(self, tag: str, system: str, user: dict, wallclock_left: float):
        """Queue one background call; None when no key, time, or queue budget is available."""
        stage = _stage_for_tag(tag)
        if not self.enabled:
            self._reject("no_key", stage)
            return None
        wall_left = _bounded_float(wallclock_left, 0.0, 0.0, 86400.0)
        timeout = min(self.call_timeout, wall_left - WALLCLOCK_RESERVE_SECONDS)
        if timeout < 5.0:
            self._reject("deadline_before_attempt", stage)
            return None
        reply, _error = self._submit(tag, system, user, timeout, 2000)
        return reply

    def _claim_attempt(self, stage: str):
        with self._lock:
            if self.max_calls is not None and self._attempts >= self.max_calls:
                return "attempt_limit"
            self._attempts += 1
            self._by_stage[stage]["attempts"] += 1
            return None

    def _run(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=0.1)
            except queue.Empty:
                if self._closed:
                    return
                continue
            if job is None:
                self._jobs.task_done()
                return
            reply, system, user, max_tokens = job
            self._perform(reply, system, user, max_tokens)
            self._jobs.task_done()

    def _perform(self, reply: ModelReply, system: str, user, max_tokens: int) -> None:
        started = time.monotonic()
        error = None
        answer = None
        for attempt in range(self.max_retries):
            remaining = reply._deadline - time.monotonic()
            if remaining <= 0:
                error = "timeout"
                break
            claim_error = self._claim_attempt(reply.stage)
            if claim_error:
                error = claim_error
                break
            reply.attempts_made += 1
            try:
                answer = self._request(system, user, min(self.call_timeout, remaining), max_tokens)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                try:
                    exc.close()
                except Exception:
                    pass
                if status == 429:
                    error, retryable = "http_429", True
                elif 500 <= status <= 599:
                    error, retryable = "http_5xx", True
                else:
                    error, retryable = "http_error", False
            except TimeoutError:
                error, retryable = "timeout", True
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                error = "timeout" if isinstance(reason, TimeoutError) or time.monotonic() >= reply._deadline \
                    else "network_error"
                retryable = True
            except OSError:
                error = "timeout" if time.monotonic() >= reply._deadline else "network_error"
                retryable = True
            except Exception:
                error, retryable = "invalid_response", False
            else:
                if time.monotonic() > reply._deadline:
                    error = "timeout"
                    answer = None
                else:
                    error = None
                break

            if not retryable or attempt + 1 >= self.max_retries:
                break
            delay = min(MAX_BACKOFF_SECONDS, 0.25 * (2 ** attempt))
            remaining = reply._deadline - time.monotonic()
            if remaining <= delay:
                error = "timeout"
                break
            with self._lock:
                self.retries += 1
                self._by_stage[reply.stage]["retries"] += 1
            time.sleep(delay)

        elapsed = time.monotonic() - started
        if answer is not None and error is None:
            with self._lock:
                self.ok += 1
                self._by_stage[reply.stage]["success"] += 1
                reply._finish(answer=answer, seconds=elapsed)
                self._prune_calls_locked()
            return
        if error is None:
            error = "invalid_response"
        if reply.attempts_made == 0:
            rejected_as = "deadline_before_attempt" if error == "timeout" else error
            self._reject(rejected_as, reply.stage)
        else:
            with self._lock:
                self.failed += 1
                self._by_stage[reply.stage]["failure"] += 1
                if error == "timeout":
                    self.timeouts += 1
                    self._by_stage[reply.stage]["timeout"] += 1
        reply._finish(error=error, seconds=elapsed)
        with self._lock:
            self._prune_calls_locked()

    def collect(self, call):
        """Return a finished reply, logging only a static stage and safe error category."""
        if call is None or not call.done():
            return None
        if not call._logged:
            call._logged = True
            if call.answer is None:
                try:
                    self.log(f"llm: {call.tag} failed ({call.error}); rules decide")
                except Exception:
                    pass
        return call.answer

    def metrics_summary(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "success": self.ok,
                "failure": self.failed,
                "rejected": self.rejected,
                "timeout": self.timeouts,
                "deadline_before_attempt": self.deadline_before_attempt,
                "retries": self.retries,
                "no_key": self.no_key,
                "attempts": self._attempts,
                "attempt_limit": self.max_calls,
                "queue_full": self.queue_full,
                "by_stage": {stage: dict(values) for stage, values in self._by_stage.items()},
            }

    def close(self, wait_seconds: float = 0.0) -> None:
        """Stop the daemon worker after queued work, optionally waiting a bounded time."""
        with self._lock:
            self._closed = True
            worker = self._worker
            if worker is not None:
                try:
                    self._jobs.put_nowait(None)
                except queue.Full:
                    pass
        if worker is not None and wait_seconds > 0 and worker is not threading.current_thread():
            worker.join(min(QUESTION_DEADLINE_SECONDS, max(0.0, float(wait_seconds))))
