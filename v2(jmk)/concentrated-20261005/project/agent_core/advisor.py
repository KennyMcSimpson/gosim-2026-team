"""Two event-driven model roles. Network waits run off the decision path."""
from __future__ import annotations

import hashlib
import json
import math
import queue
import threading
import time

DIRECTIONS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
PRIORITY_RANK = {"science": 0, "balanced": 1, "required": 2, "request": 3}
TELEMETRY_FIELDS = ("submitted", "succeeded", "parsed", "applied", "fresh", "changed")


class EventAdvisor:
    def __init__(self, client, trace, log):
        self.client, self.trace, self.log = client, trace, log
        self.jobs = queue.Queue(maxsize=2)
        self.answers = queue.Queue()
        self.pending = set()
        self.signatures = {}
        self.questions = 0
        self.applied = {"notice_interpretation": 0, "feedback_adaptation": 0}
        self.telemetry = {field: 0 for field in TELEMETRY_FIELDS}
        self.telemetry_by_role = {
            role: {field: 0 for field in TELEMETRY_FIELDS}
            for role in self.applied
        }
        self.report_results = {}
        self.worker = None

    @staticmethod
    def _signature(context):
        return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()

    def _run(self):
        while True:
            role, night, prompt, context, deadline = self.jobs.get()
            answer = None
            outcome = {}
            try:
                left = deadline - time.monotonic()
                if left > 302:
                    answer = self.client.ask_json(prompt, context, left)
                    outcome = dict(getattr(self.client, "last_call", {}) or {})
                else:
                    outcome = {"failure": "wall_reserve"}
            except Exception as exc:  # noqa: BLE001 - advisor failure must not block planning
                answer = None
                outcome = {"failure": type(exc).__name__}
            self.answers.put((role, night, answer, context, outcome))

    @staticmethod
    def _active_request(payload):
        for request in payload.get("active_requests") or []:
            try:
                if int(request.get("remaining_count", 0) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _missing_required(state):
        threshold = state.scoring.required_threshold
        return sum(required and factor < threshold
                   for required, factor in zip(state.required, state.factor))

    def _hard_priority(self, state, payload):
        """Return the lowest legal advice priority for outstanding hard work.

        Search itself owns the hard-task candidate protections.  This floor only
        prevents an advisory response (or a stale previous response) from
        lowering the planning bias while a request or REQUIRED target remains.
        """
        if self._active_request(payload):
            return "request"
        if self._missing_required(state):
            return "required"
        return None

    def _enforce_hard_floor(self, state, payload):
        floor = self._hard_priority(state, payload)
        if floor and PRIORITY_RANK.get(state.advice_priority, 1) < PRIORITY_RANK[floor]:
            state.advice_priority = floor
        return floor

    def _record(self, role, event):
        role_stats = self.telemetry_by_role.setdefault(
            role, {field: 0 for field in TELEMETRY_FIELDS})
        for field in TELEMETRY_FIELDS:
            value = bool(event.get(field, False))
            if value:
                self.telemetry[field] += 1
                role_stats[field] += 1

    def summary(self):
        """Return immutable-style telemetry suitable for finish diagnostics."""
        return {
            "totals": dict(self.telemetry),
            "roles": {role: dict(values) for role, values in self.telemetry_by_role.items()},
        }

    def _submit(self, role, night, context, wall_left, allowance):
        signature = self._signature(context)
        if (role in self.pending or self.signatures.get(role) == signature or
                self.questions >= allowance or wall_left < 320 or
                getattr(self.client, "calls_made", 0) >= getattr(self.client, "max_calls", 64) - 4):
            return False
        if role == "notice_interpretation":
            prompt = ('Interpret public telescope notices. Return JSON {"avoid_directions":[],"confidence":0.0}. '
                      'Use compass directions explicitly supported by notices; never invent directions from ALL '
                      'or empty text. Recommend avoidance only for local obstruction or severe local weather.')
        elif role == "fault_report":
            prompt = ('Check instrument-fault evidence. Return JSON {"report":true|false}. '
                      'False reports cost points; missing fiber hits are not fault evidence. '
                      'Use only the supplied multi-night quality evidence.')
        else:
            prompt = ('Adapt survey planning from public requests and actual feedback. Return JSON '
                      '{"priority":"science|balanced|required|request","risk_mode":"balanced|conservative"}. '
                      'Use request only when active. Prefer conservative after invalidation or unstable quality. '
                      'Do not infer instrument faults from missing fiber hits.')
        try:
            self.jobs.put_nowait((role, night, prompt, context, time.monotonic() + wall_left))
        except queue.Full:
            return False
        self.pending.add(role)
        self.signatures[role] = signature
        self.questions += 1
        self.telemetry["submitted"] += 1
        self.telemetry_by_role.setdefault(
            role, {field: 0 for field in TELEMETRY_FIELDS})["submitted"] += 1
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, daemon=True, name="survey-advisor")
            self.worker.start()
        self.trace.write({"event": "llm_submitted", "role": role, "night": night, "question": self.questions})
        return True

    def request_fault_report(self, evidence, wall_left, allowance=28):
        """Request a report verdict without waiting on the network.

        Returns ``(True, answer)`` when a verdict or fallback is ready and
        ``(False, None)`` while the background worker is still processing it.
        The caller can therefore keep making ordinary decisions on every turn.
        """
        context = dict(evidence)
        signature = self._signature(context)
        if signature in self.report_results:
            return True, self.report_results.pop(signature)
        # A different report may still be running.  Let it finish; its result
        # is keyed by its own evidence signature and cannot be misapplied.
        if "fault_report" in self.pending:
            return False, None
        if wall_left < 320 or getattr(self.client, "calls_made", 0) >= getattr(self.client, "max_calls", 64) - 4:
            return True, None
        if self._submit("fault_report", -1, context, wall_left, allowance):
            return False, None
        return True, None

    def update(self, state, payload, night_index, wall_left, forecast_notices):
        from datetime import timedelta
        notices = (payload.get("latest_bulletin") or {}).get("notices", [])
        night_date = (state.nights[night_index][0] - timedelta(hours=12)).date().isoformat()
        forecasts = [n for n in forecast_notices if night_date in (n.get("nights") or [])]
        selected = [{"event_kind": n.get("event_kind"), "direction": n.get("direction"),
                     "text": str(n.get("text", n.get("reason", "")))[:600]} for n in notices + forecasts]
        if getattr(self, "notice_context", None) != selected:
            state.extra_avoid = set()
            self.notice_context = selected
        if getattr(self, "night", None) != night_index:
            self.night = night_index
            state.extra_avoid = set()
        fraction = max(0, min(1, night_index / max(1, len(state.nights) - 1)))
        requests = []
        for request in payload.get("active_requests") or []:
            try:
                remaining = int(request.get("remaining_count", 1) or 0)
            except (TypeError, ValueError):
                continue
            if remaining > 0:
                requests.append({"remaining": remaining,
                                 "deadline": request.get("deadline_utc"),
                                 "reward": request.get("completion_reward")})
        missing_required = self._missing_required(state)
        feedback_context = {"season_quarter": min(3, int(fraction * 4)), "requests": requests,
                            "quality_level": round(state.scale * 4) / 4,
                            "invalidated_actions": len(state.invalidated_actions),
                            "hit_rate_band": round(4 * state.hit_rate) / 4,
                            "required_missing": missing_required}
        self._enforce_hard_floor(state, payload)
        while True:
            try:
                result = self.answers.get_nowait()
            except queue.Empty:
                break
            # Keep compatibility with a manually injected four-field answer in
            # local smoke tests, while worker results carry client metadata.
            if len(result) == 4:
                role, night, answer, context = result
                outcome = {}
            else:
                role, night, answer, context, outcome = result
            self.pending.discard(role)
            succeeded = answer is not None
            parsed, applied, changed = False, False, False
            if role == "notice_interpretation":
                fresh = night == night_index and context.get("notices") == selected
            elif role == "fault_report":
                fresh = True
            else:
                fresh = context == feedback_context
            hard_floor = self._hard_priority(state, payload)
            if isinstance(answer, dict):
                if role == "notice_interpretation":
                    supported = {str(n.get("direction", "")).upper() for n in context["notices"]}
                    try:
                        confidence = float(answer.get("confidence", 0))
                    except (TypeError, ValueError):
                        confidence = 0
                    avoid = answer.get("avoid_directions")
                    if isinstance(avoid, list) and math.isfinite(confidence) and 0.65 <= confidence <= 1:
                        parsed = True
                        if fresh:
                            new = {str(d).upper() for d in avoid} & supported & DIRECTIONS
                            changed = new != state.extra_avoid
                            state.extra_avoid = new
                            applied = True
                elif role == "fault_report":
                    if isinstance(answer.get("report"), bool):
                        parsed = True
                        self.report_results[self._signature(context)] = answer
                        applied = True
                    else:
                        self.report_results[self._signature(context)] = None
                else:
                    priority, risk = answer.get("priority"), answer.get("risk_mode")
                    if priority in {"science", "balanced", "required", "request"} and risk in {"balanced", "conservative"}:
                        if priority == "request" and not self._active_request(payload):
                            priority = "balanced"
                        parsed = True
                        # A model suggestion is an overlay.  It may influence
                        # ordinary planning, but can never demote an active
                        # request or an unfinished REQUIRED target.
                        if hard_floor and PRIORITY_RANK[priority] < PRIORITY_RANK[hard_floor]:
                            priority = hard_floor
                        if fresh:
                            changed = (priority, risk) != (state.advice_priority, state.advice_risk)
                            state.advice_priority, state.advice_risk = priority, risk
                            applied = True
                if parsed:
                    if applied:
                        self.applied[role] = self.applied.get(role, 0) + 1
            elif role == "fault_report":
                # A timeout or malformed answer is a completed background
                # attempt whose caller should use the deterministic rule path.
                self.report_results[self._signature(context)] = None
            event = {"succeeded": succeeded, "parsed": parsed, "applied": applied,
                     "changed": changed, "fresh": fresh}
            self._record(role, event)
            self.log(f"advisor: role={role} request_id={outcome.get('request_id')} "
                     f"succeeded={succeeded} parsed={parsed} applied={applied} "
                     f"changed={changed} fresh={fresh}")
            self.trace.write({"event": "llm_applied", "role": role, **event,
                              "request_id": outcome.get("request_id"),
                              "attempts": outcome.get("attempts", 0),
                              "timed_out": bool(outcome.get("timed_out", False)),
                              "failure": outcome.get("failure")})
        self._enforce_hard_floor(state, payload)
        allowance = min(28, 4 + int(24 * fraction))
        self._submit("notice_interpretation", night_index, {"notices": selected}, wall_left, allowance)
        self._submit("feedback_adaptation", night_index, feedback_context, wall_left, allowance)
