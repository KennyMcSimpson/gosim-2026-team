"""Two event-driven model roles. Network waits run off the decision path."""
from __future__ import annotations

import hashlib
import json
import math
import queue
import threading
import time

DIRECTIONS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}


class EventAdvisor:
    def __init__(self, client, trace, log):
        self.client, self.trace, self.log = client, trace, log
        self.jobs = queue.Queue(maxsize=2)
        self.answers = queue.Queue()
        self.pending = set()
        self.signatures = {}
        self.questions = 0
        self.applied = {"notice_interpretation": 0, "feedback_adaptation": 0}
        self.worker = None

    def _run(self):
        while True:
            role, night, prompt, context, deadline = self.jobs.get()
            try:
                left = deadline - time.monotonic()
                answer = self.client.ask_json(prompt, context, left) if left > 302 else None
            except Exception:
                answer = None
            self.answers.put((role, night, answer, context))

    def _submit(self, role, night, context, wall_left, allowance):
        signature = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        if (role in self.pending or self.signatures.get(role) == signature or
                self.questions >= allowance or wall_left < 320 or self.client.calls_made >= self.client.max_calls - 4):
            return
        if role == "notice_interpretation":
            prompt = ('Interpret public telescope notices. Return JSON {"avoid_directions":[],"confidence":0.0}. '
                      'Use compass directions explicitly supported by notices; never invent directions from ALL '
                      'or empty text. Recommend avoidance only for local obstruction or severe local weather.')
        else:
            prompt = ('Adapt survey planning from public requests and actual feedback. Return JSON '
                      '{"priority":"science|balanced|required|request","risk_mode":"balanced|conservative"}. '
                      'Use request only when active. Prefer conservative after invalidation or unstable quality. '
                      'Do not infer instrument faults from missing fiber hits.')
        try:
            self.jobs.put_nowait((role, night, prompt, context, time.monotonic() + wall_left))
        except queue.Full:
            return
        self.pending.add(role)
        self.signatures[role] = signature
        self.questions += 1
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, daemon=True, name="survey-advisor")
            self.worker.start()
        self.trace.write({"event": "llm_submitted", "role": role, "night": night, "question": self.questions})

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
                role, night, answer, context = self.answers.get_nowait()
            except queue.Empty:
                break
            self.pending.discard(role)
            parsed, changed = False, False
            fresh = ((night == night_index and context.get("notices") == selected)
                     if role == "notice_interpretation" else context == feedback_context)
            if isinstance(answer, dict) and fresh:
                if role == "notice_interpretation":
                    supported = {str(n.get("direction", "")).upper() for n in context["notices"]}
                    try:
                        confidence = float(answer.get("confidence", 0))
                    except (TypeError, ValueError):
                        confidence = 0
                    avoid = answer.get("avoid_directions")
                    if isinstance(avoid, list) and math.isfinite(confidence) and 0.65 <= confidence <= 1:
                        new = {str(d).upper() for d in avoid} & supported & DIRECTIONS
                        changed = new != state.extra_avoid
                        state.extra_avoid = new
                        parsed = True
                else:
                    priority, risk = answer.get("priority"), answer.get("risk_mode")
                    if priority in {"science", "balanced", "required", "request"} and risk in {"balanced", "conservative"}:
                        if priority == "request" and not any(int(r.get("remaining_count", 1)) > 0
                                                            for r in payload.get("active_requests", [])):
                            priority = "balanced"
                        changed = (priority, risk) != (state.advice_priority, state.advice_risk)
                        state.advice_priority, state.advice_risk = priority, risk
                        parsed = True
                if parsed:
                    self.applied[role] += 1
            self.log(f"advisor: role={role} succeeded={bool(answer)} parsed={parsed} applied={parsed} changed={changed} fresh={fresh}")
            self.trace.write({"event": "llm_applied", "role": role, "succeeded": bool(answer),
                              "parsed": parsed, "applied": parsed, "changed": changed, "fresh": fresh})
        allowance = min(28, 4 + int(24 * fraction))
        self._submit("notice_interpretation", night_index, {"notices": selected}, wall_left, allowance)
        self._submit("feedback_adaptation", night_index, feedback_context, wall_left, allowance)
