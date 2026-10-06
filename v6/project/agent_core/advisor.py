"""Two event-driven model roles. Network waits run off the decision path."""
from __future__ import annotations

import hashlib
import json
import math
import queue
import threading
import time

DIRECTIONS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}


def _fingerprint(value) -> str:
    """Return a short, reproducible id for a submitted model context."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _notice_directions(notices) -> set[str]:
    """Return only explicit local compass sectors; ``ALL`` is never a sector."""
    return {str(n.get("direction", "")).upper() for n in notices
            if isinstance(n, dict) and str(n.get("direction", "")).upper() in DIRECTIONS}


def _notice_kinds(notices) -> list[str]:
    return sorted({str(n.get("event_kind", "")) for n in notices
                   if isinstance(n, dict) and n.get("event_kind")})


class EventAdvisor:
    def __init__(self, client, trace, log):
        self.client, self.trace, self.log = client, trace, log
        self.jobs = queue.Queue(maxsize=2)
        self.answers = queue.Queue()
        self.pending = set()
        self.signatures = {}
        self.questions = 0
        self.applied = {"notice_interpretation": 0, "feedback_adaptation": 0}
        self.request_ids = {}
        self.request_sequence = 0
        self.worker = None

    def _run(self):
        while True:
            role, night, prompt, context, deadline, request_id = self.jobs.get()
            started = time.monotonic()
            answer = None
            failure_reason = None
            client_outcome = None
            try:
                left = deadline - time.monotonic()
                if left <= 302:
                    failure_reason = "wall_reserve"
                else:
                    answer = self.client.ask_json(prompt, context, left)
                    client_outcome = dict(getattr(self.client, "last_outcome", None) or {})
                    if answer is None:
                        failure_reason = ((client_outcome or {}).get("reason")
                                          or (client_outcome or {}).get("status")
                                          or "no_answer")
            except Exception as exc:  # model failures must never enter the decision path
                failure_reason = f"worker_{type(exc).__name__}"
                client_outcome = {"status": "worker_error", "reason": failure_reason}
            if answer is not None and not isinstance(answer, dict):
                failure_reason = "non_object_answer"
                answer = None
            result = {
                "answer": answer,
                "failure_reason": failure_reason,
                "client_outcome": client_outcome,
                "latency_seconds": round(time.monotonic() - started, 6),
            }
            self.answers.put((role, night, result, context, request_id))

    def _trace_result(self, role, night, request_id, context, result, fresh=None):
        answer = result.get("answer")
        outcome = result.get("client_outcome") or {}
        self.trace.write({
            "event": "llm_result",
            "role": role,
            "night": night,
            "request_id": request_id,
            "context_id": _fingerprint(context),
            "succeeded": isinstance(answer, dict),
            "answer_keys": sorted(answer) if isinstance(answer, dict) else [],
            "answer_summary": self._answer_summary(role, answer),
            "failure_reason": result.get("failure_reason"),
            "client_status": outcome.get("status"),
            "attempts": outcome.get("attempts"),
            "latency_seconds": result.get("latency_seconds"),
            "fresh": fresh,
        })

    @staticmethod
    def _answer_summary(role, answer):
        """Keep trace useful to a reviewer without recording provider text."""
        if not isinstance(answer, dict):
            return {}
        if role == "notice_interpretation":
            return {"avoid_directions": answer.get("avoid_directions"),
                    "confidence": answer.get("confidence")}
        return {"priority": answer.get("priority"), "risk_mode": answer.get("risk_mode")}

    def _trace_fallback(self, role, night, request_id, context, reason, fresh):
        self.trace.write({"event": "llm_fallback", "role": role, "night": night,
                          "request_id": request_id, "context_id": _fingerprint(context),
                          "reason": reason, "fresh": fresh})

    def _submit(self, role, night, context, wall_left, allowance):
        signature = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        skip_reason = None
        if role in self.pending:
            skip_reason = "request_pending"
        elif self.signatures.get(role) == signature:
            skip_reason = "same_context"
        elif self.questions >= allowance:
            skip_reason = "question_allowance"
        elif wall_left < 320:
            skip_reason = "wall_reserve"
        elif self.client.calls_made >= self.client.max_calls - 4:
            skip_reason = "call_reserve"
        if skip_reason:
            # These records distinguish a deliberate deterministic fallback
            # from a provider failure in the reviewer-facing trace.
            self.trace.write({"event": "llm_skipped", "role": role, "night": night,
                              "reason": skip_reason, "context_id": _fingerprint(context)})
            return
        if role == "notice_interpretation":
            prompt = (
                'Interpret the public telescope notices in the supplied JSON. Return exactly one JSON '
                '{"avoid_directions":[],"confidence":0.0}. The list may contain only compass codes '
                'N, NE, E, SE, S, SW, W, NW that are explicitly present as local directions in the input; '
                'never output ALL or invent a sector. For an explicit local rocket_launch or '
                'terrain_obstruction, include its direction with confidence at least 0.9. For a local '
                'severe weather/closure notice, include its direction; otherwise return an empty list. '
                'This is a safety interpretation only, not a choice of targets.')
        else:
            prompt = ('Adapt survey planning from public requests and actual feedback. Return JSON '
                      '{"priority":"science|balanced|required|request","risk_mode":"balanced|conservative"}. '
                      'Use request only when active. Prefer conservative after invalidation or unstable quality. '
                      'Do not infer instrument faults from missing fiber hits.')
        self.request_sequence += 1
        request_id = f"{role}:{night}:{self.request_sequence}:{signature[:10]}"
        try:
            self.jobs.put_nowait((role, night, prompt, context,
                                  time.monotonic() + wall_left, request_id))
        except queue.Full:
            self.trace.write({"event": "llm_skipped", "role": role, "night": night,
                              "reason": "worker_queue_full"})
            return
        self.pending.add(role)
        self.request_ids[role] = request_id
        self.signatures[role] = signature
        self.questions += 1
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, daemon=True, name="survey-advisor")
            self.worker.start()
        self.trace.write({"event": "llm_submitted", "role": role, "night": night,
                          "request_id": request_id, "question": self.questions,
                          "context_id": _fingerprint(context),
                          "input_directions": sorted(_notice_directions(context.get("notices", [])))
                          if role == "notice_interpretation" else None,
                          "input_event_kinds": _notice_kinds(context.get("notices", []))
                          if role == "notice_interpretation" else None})

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
        requests = [{"remaining": r.get("remaining_count"), "deadline": r.get("deadline_utc"),
                     "reward": r.get("completion_reward")} for r in payload.get("active_requests", [])
                    if int(r.get("remaining_count", 1)) > 0]
        feedback_context = {"season_quarter": min(3, int(fraction * 4)), "requests": requests,
                            "quality_level": round(state.scale * 4) / 4,
                            "invalidated_actions": len(state.invalidated_actions),
                            "hit_rate_band": round(4 * state.hit_rate) / 4}
        while True:
            try:
                role, night, result, context, request_id = self.answers.get_nowait()
            except queue.Empty:
                break
            self.pending.discard(role)
            answer = result.get("answer")
            fresh = ((night == night_index and context.get("notices") == selected)
                     if role == "notice_interpretation" else context == feedback_context)
            self._trace_result(role, night, request_id, context, result, fresh)
            parsed, changed, apply_reason = False, False, None
            before = sorted(state.extra_avoid) if role == "notice_interpretation" else {
                "priority": state.advice_priority, "risk_mode": state.advice_risk}
            if not fresh:
                apply_reason = "stale_context"
            elif not isinstance(answer, dict):
                apply_reason = result.get("failure_reason") or "no_answer"
            elif role == "notice_interpretation":
                supported = _notice_directions(context.get("notices", []))
                avoid = answer.get("avoid_directions")
                try:
                    confidence = float(answer.get("confidence", 1.0 if avoid else 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0
                if not isinstance(avoid, list):
                    apply_reason = "invalid_avoid_directions"
                elif not math.isfinite(confidence) or not 0.65 <= confidence <= 1:
                    apply_reason = "invalid_confidence"
                elif not supported:
                    # Empty context is a valid interpretation, but there is no
                    # state effect to claim and no local direction to apply.
                    parsed = True
                    apply_reason = "no_local_direction"
                else:
                    normalized = {str(direction).strip().upper() for direction in avoid}
                    invalid = sorted(normalized - DIRECTIONS)
                    new = normalized & supported
                    # Unknown values (including ALL) cannot affect planning;
                    # retain valid local sectors so a minor model formatting
                    # error does not erase a useful safety interpretation.
                    if not new and (invalid or normalized) and not (normalized & supported):
                        apply_reason = "unsupported_direction"
                    else:
                        parsed = True
                        changed = new != state.extra_avoid
                        state.extra_avoid = new
                        apply_reason = ("applied_with_ignored_tokens" if changed and invalid
                                        else "applied" if changed else "no_state_change")
            elif role == "feedback_adaptation":
                priority, risk = answer.get("priority"), answer.get("risk_mode")
                if priority not in {"science", "balanced", "required", "request"}:
                    apply_reason = "invalid_priority"
                elif risk not in {"balanced", "conservative"}:
                    apply_reason = "invalid_risk_mode"
                else:
                    if priority == "request" and not any(
                            int(r.get("remaining_count", 1)) > 0
                            for r in payload.get("active_requests", [])):
                        priority = "balanced"
                    changed = (priority, risk) != (state.advice_priority, state.advice_risk)
                    state.advice_priority, state.advice_risk = priority, risk
                    parsed = True
                    apply_reason = "applied" if changed else "no_state_change"
            if parsed:
                self.applied[role] += 1
            applied = parsed and fresh
            after = sorted(state.extra_avoid) if role == "notice_interpretation" else {
                "priority": state.advice_priority, "risk_mode": state.advice_risk}
            self.log(f"advisor: role={role} request_id={request_id} succeeded={isinstance(answer, dict)} "
                     f"parsed={parsed} applied={applied} changed={changed} fresh={fresh} reason={apply_reason}")
            self.trace.write({"event": "llm_applied", "role": role, "night": night,
                              "request_id": request_id, "succeeded": isinstance(answer, dict),
                              "parsed": parsed, "applied": applied, "changed": changed,
                              "fresh": fresh, "reason": apply_reason,
                              "state_before": before, "state_after": after,
                              "state_effect": ("extra_avoid" if role == "notice_interpretation"
                                               else "advice_priority/advice_risk"),
                              "decision_consumer": ("Planner._direction_factor"
                                                    if role == "notice_interpretation"
                                                    else "JointSearch priority/value scoring")})
            if not applied:
                self._trace_fallback(role, night, request_id, context,
                                     apply_reason or "not_applied", fresh)
        allowance = min(28, 4 + int(24 * fraction))
        self._submit("notice_interpretation", night_index, {"notices": selected}, wall_left, allowance)
        self._submit("feedback_adaptation", night_index, feedback_context, wall_left, allowance)
