"""Public-feedback planner with bounded joint search and two event-driven model roles."""
from __future__ import annotations

import math

from .advisor import EventAdvisor
from .calendar import RequiredCalendar
from .clock import Clock
from .geometry import format_utc, parse_utc, wrap180
from .llm_client import LLMClient
from .memory import TraceLog
from .planner_search import JointSearch

DIRECTION_AZ = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0,
                "SW": 225.0, "W": 270.0, "NW": 315.0}
BLOCKING_KINDS = {"terrain_obstruction", "rocket_launch"}
REPORT_DROP = 0.62
# Two separated exposure-level checks are enough to avoid reacting to a single
# bad exposure while leaving time to report more than one independent fault.
REPORT_CONFIRMATIONS = 2
REPORT_SPACING_HOURS = 6.0
# The protocol publishes a much higher consecutive-report limit (normally 32),
# but retain a smaller run-wide guard so a stale/malformed feedback stream cannot
# turn the report path into an unbounded source of score penalties.
MAX_REPORTS = 8
# A zero-score hit is local evidence, rather than a permanent sky mask.  Keep
# it long enough to avoid immediately repeating a bad pointing, then let the
# sector be reconsidered as the sky and instrument state change.
BLOCKED_MEMORY_HOURS = 6.0
BLOCKED_MEMORY_MAX = 512
# Recovery is a correctness obligation after Hard-mode data loss.  These
# values are heuristic utility units, deliberately much larger than ordinary
# science/request gains so a surviving recovery target remains in the bounded
# candidate beam and is not lost to anchor pruning.
RECOVERY_REQUIRED_BONUS = 1_000_000.0
RECOVERY_REQUEST_BONUS = 250_000.0


def _az_distance(a, b):
    return abs(wrap180(a - b))


class Planner(JointSearch):
    def __init__(self, state, log=lambda text: None):
        self.state, self.log, self.grid = state, log, state.fiber_grid
        self.clock = Clock()
        self.llm = LLMClient(log=log)
        self.trace = TraceLog(log=log)
        self.advisor = EventAdvisor(self.llm, self.trace, log)
        self.required_calendar = RequiredCalendar(state)
        self._top_multiplier = max(state.scoring.mismatch_multiplier,
                                   *state.scoring.program_multipliers.values())
        self.exposure_ema = 700.0
        self.observe_count = self.reports = self.consecutive_reports = 0
        self.total_assigned = self.total_hit = 0
        self.last_report_hours = float("-inf")
        self.suspicion_hours = []
        self._last_forecast_notices = []
        self._current_action_index = None
        self._active_requests_now, self._request_views_now = [], []
        self._request_thresholds_now, self._request_bonus_now = {}, {}
        self._uniformity_version = -1
        self._uniformity_totals, self._uniformity_observed = {}, {}
        self._uniformity_penalty_now = 0.0
        self._uniformity_gain_cache, self._quality_cache, self._risk_cache = {}, {}, {}
        self._planning_hours = 0.0
        self._blocked_seen = 0
        self._blocked_history = []
        self._notice_signature = None
        self._search_allowance = None
        self._decision_overhead = 0.0
        self._last_resync_token = None
        self._prepare_geometry()
        log(f"planner: {len(state.ids)} targets ({sum(state.required)} required), "
            f"{len(state.nights)} nights; event-driven model roles enabled")

    def decide(self, payload):
        state = self.state
        now = parse_utc(payload["now_utc"])
        hours = (now - state.survey_start).total_seconds() / 3600
        self._planning_hours = hours
        self.clock.update(payload.get("wallclock"))
        # Ingest the latest exposure first, then roll back the authoritative window.
        state.on_result(payload.get("last_result"), hours)
        state.on_messages(payload.get("new_messages", []), payload.get("latest_bulletin"))
        self._refresh_blocked_memory(hours)
        for message in payload.get("new_messages", []):
            if message.get("record_type") == "forecast":
                self._last_forecast_notices = message.get("notices", [])
        result = payload.get("last_result") or {}
        if result.get("action") == "observe":
            self.total_assigned += int(result.get("assigned_count", 0))
            self.total_hit += int(result.get("hit_count", 0))
        state.hit_rate = self.total_hit / self.total_assigned if self.total_assigned else 1.0
        self._current_action_index = payload.get("observe_action_index")
        active_requests_payload = payload.get("active_requests")
        self._active_requests_now = active_requests_payload or []
        self._request_views_now = self._request_views(self._active_requests_now, now)
        self._request_thresholds_now = self._request_thresholds(self._active_requests_now)
        self._request_bonus_now = self._request_bonuses(self._active_requests_now, now)
        if active_requests_payload is not None:
            state.refresh_recovery_queue(self._active_requests_now)
        resync_token = (state.resync_count, state.last_resync.get("event_id"))
        if state.resync_count and resync_token != self._last_resync_token:
            self._last_resync_token = resync_token
            self.trace.write({
                "event": "data_loss_recovery",
                "event_id": state.last_resync.get("event_id"),
                "invalidated_target_ids": state.last_resync.get("invalidated_target_ids", []),
                "recovery_target_ids": state.last_resync.get("recovery_target_ids", []),
                "request_ids": state.last_resync.get("request_ids", []),
            })
            self.log("planner: resync recovery queue "
                     f"{len(state.recovery_queue)} targets")
        # Requests need fresh exposures even for previously saturated targets.
        reactivated = set(self._request_thresholds_now) - set(state.active)
        state.active.extend(i for i in sorted(reactivated) if state.hmax[i] > 0)
        # A resync recovery target is an explicit recall obligation.  Restore
        # it to the active catalogue before the bounded search can prune
        # ordinary saturated science targets.
        recovery = state.recovery_target_indices()
        state.active.extend(i for i in recovery
                            if i not in state.active and state.hmax[i] > 0)
        self._pace(now)
        night = state.current_night(now)
        if night is None:
            nxt = state.next_night_start(now)
            return ({"action": "wait", "until_utc": format_utc(nxt), "reason": "next observing night"}
                    if nxt else {"action": "finish", "reason": "no observing night left"})
        night_index, night_start, night_end = night
        self.advisor.update(state, payload, night_index, self.clock.wall_remaining(), self._last_forecast_notices)
        if (night_end - now).total_seconds() < state.min_exposure:
            nxt = state.next_night_start(now)
            return ({"action": "wait", "until_utc": format_utc(nxt), "reason": "night ending"}
                    if nxt else {"action": "finish", "reason": "survey over"})
        if state.site_closed():
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start),
                    "reason": "whole-sky rain/storm notice"}
        report = self._maybe_report(hours, payload)
        if report:
            return report
        action = self.plan(now, night_end, night_index, hours)
        if action is None:
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start),
                    "reason": "nothing useful is up"}
        self.observe_count += 1
        action["reason"] = f"{len(action['assignments'])} fibres, program {action['program']}"
        return action

    def note_action(self, action):
        self.consecutive_reports = self.consecutive_reports + 1 if action.get("action") == "report" else 0
        if action.get("action") != "observe":
            self.state.pending.clear()
            self.state.pending_action_index = None

    def on_finish(self, payload):
        self.trace.write({"event": "finish", **payload,
                          "model_role_applications": dict(self.advisor.applied)})
        self.trace.close()
        self.log(f"planner: termination={payload.get('termination_reason')} "
                 f"observes={self.observe_count} reports={self.reports} llm_calls={self.llm.calls_made}")

    def _to_next_slot(self, now, night_start):
        slot = self.state.slot_seconds
        into = (now - night_start).total_seconds() % slot
        return int(max(60, min(3600, slot - into if into else slot)))

    def _pace(self, now):
        state = self.state
        night_seconds = sum(max(0, (end - max(start, now)).total_seconds())
                            for start, end in state.nights if end > now)
        decisions_left = max(1, night_seconds / max(state.min_exposure, self.exposure_ema))
        allowance = max(0, self.clock.compute_left() - 3) * 0.85 / decisions_left
        self._search_allowance = allowance
        remaining_compute = self.clock.compute_left()
        # Convert one decision's CPU use into the observing time it consumes
        # from the remaining season.  ``avg_cost / remaining_compute`` is the
        # fraction of the remaining CPU budget spent by one decision; applying
        # it to the remaining observing seconds gives a value with seconds as
        # its unit.  The old expression omitted that season-time scale and
        # treated a dimensionless fraction as exposure seconds.
        self._decision_overhead = (night_seconds * self.clock.avg_cost / remaining_compute
                                   if (math.isfinite(remaining_compute) and
                                       remaining_compute > 0 and night_seconds > 0)
                                   else 0.0)
        target = 0 if allowance > 0.12 else 1 if allowance > 0.04 else 2
        if self.clock.avg_cost > 1.2 * allowance:
            target = min(2, max(target, state.fast_level + 1))
        # Hysteresis permits recovery from transient expensive decisions.
        if target < state.fast_level and self.clock.avg_cost > 0.7 * allowance:
            target = state.fast_level
        if target != state.fast_level:
            self.log(f"planner: pace={target}, CPU allowance={allowance * 1000:.1f}ms")
            state.fast_level = target

    def _recovery_priority(self, i):
        """Return deterministic priority for a target awaiting resync recovery."""
        entry = self.state.recovery_queue.get(i)
        if not entry:
            return 0.0
        required_pending = (self.state.required[i] and
                            self.state.factor[i] < self.state.scoring.required_threshold)
        priority = RECOVERY_REQUIRED_BONUS if required_pending else 0.0
        priority += RECOVERY_REQUEST_BONUS * len(entry.get("request_ids", ()))
        # Repeated misses should lower, but never erase, the recovery priority;
        # this prevents an impossible target from monopolising every pointing.
        return priority * max(0.25, 0.75 ** min(6, self.state.misses[i]))

    def _value(self, i):
        return super()._value(i) + self._recovery_priority(i)

    def _target_gain(self, item, duration, program):
        gain = super()._target_gain(item, duration, program)
        i = item["i"]
        recovery = self._recovery_priority(i)
        if not recovery:
            return gain
        required_ready = (
            self.state.required[i]
            and self.state.factor[i] < self.state.scoring.required_threshold
            and item.get("low_k", 0.0) * duration >= self.state.scoring.required_threshold
        )
        request_threshold = self._request_thresholds_now.get(i)
        request_ready = (
            request_threshold is not None
            and item.get("low_k", 0.0) * duration >= request_threshold
        )
        # Give a full bonus only to a duration that can actually repair the
        # obligation; retain a smaller recall bonus for a legal partial pass.
        return gain + recovery * (1.0 if required_ready or request_ready else 0.15)

    def _maybe_report(self, hours, payload):
        state = self.state
        state.force_program = None
        if self.reports >= MAX_REPORTS or hours - self.last_report_hours < 24:
            return None
        evidence = state.fault_evidence()
        threshold = REPORT_DROP if self.reports == 0 else REPORT_DROP - 0.07
        if evidence is None or evidence.drop >= threshold:
            self.suspicion_hours = []
            return None
        if evidence.dark_checks < 6:
            state.force_program = "DARK"
        elif evidence.dark_matched < 0.5 * evidence.dark_checks:
            self.suspicion_hours = []
            return None
        if self.suspicion_hours and hours - self.suspicion_hours[-1] < REPORT_SPACING_HOURS:
            return None
        self.suspicion_hours.append(hours)
        if len(self.suspicion_hours) < REPORT_CONFIRMATIONS:
            return None
        self.suspicion_hours = []
        answer = None
        if not self.advisor.pending and self.clock.wall_remaining() > 320:
            answer = self.llm.ask_json(
                'Check instrument-fault evidence. Return JSON {"report":true|false}. '
                'False reports cost points; missing fiber hits are not fault evidence.',
                evidence._asdict(), self.clock.wall_remaining())
        verdict = answer.get("report") if isinstance(answer, dict) else None
        if verdict is False:
            self.last_report_hours = hours
            return None
        self.reports += 1
        self.last_report_hours = hours
        # Keep the evidence until the platform confirms this report.  A correct
        # report_result clears it through SurveyState; a false report (or a run
        # that never publishes feedback) must leave the history available for a
        # later diagnosis instead of silently erasing the only fault evidence.
        self.log(f"planner: fault report with multi-night evidence at {payload.get('now_utc')}")
        return {"action": "report", "reason": f"quality dropped to {evidence.drop:.0%}",
                "decision_source": "llm-confirmed" if verdict is True else "rule"}

    def _direction_factor(self, alt, az):
        state = self.state
        # Keep direct planner callers (which do not go through decide()) in
        # sync as well.  The state list remains the public compatibility API;
        # timestamps live only in this planner's short-lived memory.
        if (self._blocked_seen != len(state.blocked) or
                getattr(self, "_direction_hours", None) != self._planning_hours or
                self._notice_signature != tuple(sorted(state.notices))):
            self._refresh_blocked_memory(self._planning_hours)
        for direction in state.terrain:
            if direction in DIRECTION_AZ and alt < 50 and _az_distance(az, DIRECTION_AZ[direction]) <= 60:
                return 0.0
        factor = 1.0
        for key in state.notices:
            kind, _, direction = key.partition("|")
            if direction not in DIRECTION_AZ:
                continue
            near = _az_distance(az, DIRECTION_AZ[direction]) <= 67.5
            if kind in BLOCKING_KINDS and near and alt < 62:
                return 0.0
            if near and alt < 75:
                factor = min(factor, 0.35)
        for direction in state.extra_avoid:
            if direction in DIRECTION_AZ and _az_distance(az, DIRECTION_AZ[direction]) <= 67.5 and alt < 70:
                factor = min(factor, 0.35)
        for _blocked_hours, blocked_az, blocked_alt in self._blocked_history:
            if _az_distance(az, blocked_az) <= 12 and alt <= blocked_alt + 3:
                factor = min(factor, 0.2)
        return factor

    def _refresh_blocked_memory(self, hours):
        """Track recent zero-score sectors without making them permanent masks."""
        try:
            current_hours = float(hours)
        except (TypeError, ValueError):
            current_hours = self._planning_hours
        blocked = self.state.blocked
        # A resync or test fixture may replace the list.  Drop stale local
        # indexes when that happens, then consume the new entries once.
        if self._blocked_seen > len(blocked):
            self._blocked_seen = 0
            self._blocked_history.clear()
        for entry in blocked[self._blocked_seen:]:
            try:
                blocked_az, blocked_alt = entry[:2]
                self._blocked_history.append(
                    (current_hours, float(blocked_az), float(blocked_alt)))
            except (TypeError, ValueError, IndexError):
                continue
        self._blocked_seen = len(blocked)

        if len(self._blocked_history) > BLOCKED_MEMORY_MAX:
            self._blocked_history = self._blocked_history[-BLOCKED_MEMORY_MAX:]

        cutoff = current_hours - BLOCKED_MEMORY_HOURS
        self._blocked_history = [entry for entry in self._blocked_history
                                 if entry[0] >= cutoff]

        notice_signature = tuple(sorted(self.state.notices))
        if self._notice_signature is not None and notice_signature != self._notice_signature:
            # A bulletin transition can change a local obstruction's cause;
            # do not carry the old empirical mask across that transition.
            self._blocked_history.clear()
        self._notice_signature = notice_signature
        self._direction_hours = current_hours
