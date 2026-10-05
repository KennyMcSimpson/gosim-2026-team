"""Decision logic: pick a pointing, fill the 16 fibres, choose exposure length and
program -- or wait / report / finish.

1. Rank visible, not-yet-done targets. Required targets that are not done yet get a
   bonus (missing one costs real points at the end). Targets that set soon, or that
   have few nights left, rank higher.
2. For the best few "anchor" candidates, try each fibre as the pointing centre; fill
   every fibre with the best-value neighbour that lands on its glass; keep the best
   pointing.
3. Pick the exposure length with the best expected score per second, and the program
   (DARK / BRIGHT / BACKUP) most assigned targets will match.

Everything here uses only the public catalogue, the public scoring config, and the
agent's own past hits (SurveyState) -- never hidden weather truth. Once per night, two
LLM calls run and their answers are merged (see `_night_advice` below): one reads the
forecast/bulletin notices for tonight, the other reads tonight's live bulletin text and
the agent's own hit rate so far. A call that keeps failing falls back to its rule-based
answer for that night; the next night's calls still run normally.

This mirrors the anchor-search algorithm of this project's companion TypeScript
example target-for-target, so both examples solve the problem the same way.
"""
from __future__ import annotations

from datetime import timedelta

from .geometry import (
    Moon,
    SIDEREAL_DEG_PER_SECOND,
    altaz_to_radec,
    format_utc,
    local_sidereal_deg,
    lunar_factor,
    max_hour_angle_deg,
    parse_utc,
    radec_to_altaz,
    shift_altaz,
    tangent_offsets,
    wrap180,
)
from .clock import Clock
from .llm_client import LLMClient
from .memory import TraceLog
from .state import PendingPrediction

PLAN_FACTOR_SAFETY = 0.9
EDGE_MARGIN_DEG = 0.08
DURATIONS = (300, 450, 600, 900, 1200, 1500, 1800, 2400, 3000, 3600)
NEIGHBOUR_RADIUS_DEG = 2.1
ANCHORS = 6
ANCHOR_POOL = 300
CLOSED_KINDS = {"rain", "storm"}
BLOCKING_KINDS = {"terrain_obstruction", "rocket_launch"}
DIRECTION_AZ = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0,
                "SW": 225.0, "W": 270.0, "NW": 315.0}

REPORT_DROP = 0.62
REPORT_CONFIRMATIONS = 3
REPORT_SPACING_HOURS = 6.0
MAX_REPORTS = 2

# Time-limited observation requests (participant guide section 8 / appendix B). Requests
# now influence both anchor ranking and duration selection, while the exact reward is only
# counted when the candidate exposure satisfies the request's minimum and deadline.
REQUEST_RATE_TOLERANCE = 0.9
REQUEST_BONUS_SCALE = 0.25


def _az_distance(a: float, b: float) -> float:
    return abs(wrap180(a - b))


def _bulletin_text(notices: list) -> str:
    """A human-readable rendering of a bulletin's notices, for the LLM call that reads
    "live bulletin text" rather than structured JSON."""
    if not notices:
        return "clear (no active notices)"
    return "; ".join(f"{n.get('event_kind')} {n.get('direction')}" for n in notices)


class Planner:
    def __init__(self, state, log=lambda text: None):
        self.state = state
        self.log = log
        self.grid = state.fiber_grid
        self.clock = Clock()
        self.min_level = 0
        self.llm = LLMClient(log=log)
        self.trace = TraceLog(log=log)

        self.observe_count = 0
        self.reports = 0
        self.last_report_hours = float("-inf")
        self.suspicion_hours: list[float] = []
        self.night_index_seen: int | None = None
        self.consecutive_reports = 0
        self._last_forecast_notices: list = []
        self.total_assigned = 0
        self.total_hit = 0
        self._current_action_index = None
        self._request_thresholds_now: dict = {}
        self._request_bonus_now: dict = {}
        self._active_requests_now: list[dict] = []
        self._uniformity_totals: dict[int, int] = {}
        self._uniformity_observed: dict[int, int] = {}
        self._uniformity_penalty_now = 0.0
        self._uniformity_gain_cache: dict[int, float] = {}

        log(f"planner: {len(state.ids)} targets ({sum(state.required)} required), "
            f"{len(state.nights)} nights, llm model={self.llm.model} base_url={self.llm.base_url}")

    # -- top-level decision ----------------------------------------------------

    def decide(self, payload: dict) -> dict:
        state = self.state
        now = parse_utc(payload["now_utc"])
        hours = (now - state.survey_start).total_seconds() / 3600.0

        for message in payload.get("new_messages", []):
            if message.get("record_type") == "forecast":
                self._last_forecast_notices = message.get("notices", [])
            elif message.get("record_type") == "observation_request":
                self.log(f"planner: observation request {message.get('request_id')} issued, "
                         f"{len(message.get('target_ids', []))} targets by {message.get('deadline_utc')}")
            elif message.get("record_type") == "observation_request_result":
                self.log(f"planner: observation request {message.get('request_id')} "
                         f"{message.get('status')} (reward {message.get('score_delta', 0.0)})")
        state.on_messages(payload.get("new_messages", []), payload.get("latest_bulletin"))
        state.on_result(payload.get("last_result"), hours)
        last_result = payload.get("last_result")
        if last_result and last_result.get("action") == "observe":
            self.total_assigned += int(last_result.get("assigned_count", 0))
            self.total_hit += int(last_result.get("hit_count", 0))
        self.clock.update(payload.get("wallclock"))
        self._pace(now)
        self._current_action_index = payload.get("observe_action_index")
        self._active_requests_now = payload.get("active_requests") or []
        self._request_thresholds_now = self._request_thresholds(self._active_requests_now)
        self._request_bonus_now = self._request_bonuses(self._active_requests_now, now)

        night = state.current_night(now)
        if night is None:
            nxt = state.next_night_start(now)
            if nxt is None:
                return {"action": "finish", "reason": "no observing night left"}
            return {"action": "wait", "until_utc": format_utc(nxt), "reason": "daytime: sleep until the next night"}
        night_index, night_start, night_end = night

        if self.night_index_seen != night_index:
            self.night_index_seen = night_index
            self._night_advice(night_start, payload)

        if (night_end - now).total_seconds() < state.min_exposure:
            nxt = state.next_night_start(now)
            if nxt is None:
                return {"action": "finish", "reason": "survey over"}
            return {"action": "wait", "until_utc": format_utc(nxt), "reason": "night ending"}

        if state.site_closed():
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start),
                    "reason": "bulletin: rain/storm over the whole sky"}

        report = self._maybe_report(hours, payload)
        if report is not None:
            return report

        action = self.plan(now, night_end, night_index, hours)
        if action is None:
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start),
                    "reason": "nothing useful is up"}
        self.observe_count += 1
        action["reason"] = f"{len(action['assignments'])} fibres, program {action['program']}"
        return action

    def on_finish(self, payload: dict) -> None:
        self.trace.write({"event": "finish", **payload})
        self.trace.close()
        self.log(f"planner: finished termination_reason={payload.get('termination_reason')} "
                 f"observes={self.observe_count} reports={self.reports} llm_calls={self.llm.calls_made}")

    def note_action(self, action: dict) -> None:
        """Called by agent.py right after an action is validated, so the consecutive-report
        counter (enforced by validation.py) stays correct even when a fallback replaced it."""
        self.consecutive_reports = self.consecutive_reports + 1 if action.get("action") == "report" else 0

    def _to_next_slot(self, now, night_start) -> int:
        slot = self.state.slot_seconds
        into = (now - night_start).total_seconds() % slot
        return int(max(60, min(3600, slot - into if into else slot)))

    def _pace(self, now) -> None:
        """Do less work per decision when the compute budget is short for the nights
        still to come. Budget and own cost are both real CPU seconds of this machine
        (see clock.py), so the pace does not depend on how fast the machine is."""
        state = self.state
        night_seconds = sum(max(0.0, (end - max(start, now)).total_seconds()) for start, end in state.nights if end > now)
        decisions_left = max(1.0, night_seconds / 700.0)
        per_decision = self.clock.compute_left() / decisions_left
        level = 0 if per_decision > 0.12 else 1 if per_decision > 0.04 else 2
        # Safety net from our own measurement: if recent decisions cost more CPU than we
        # can afford per decision, never go back to a slower level for the rest of the run.
        if self.clock.avg_cost > per_decision:
            self.min_level = min(2, max(self.min_level, level + 1))
        level = max(level, self.min_level)
        if level != state.fast_level:
            self.log(f"planner: pace level {level} ({per_decision * 1000:.0f} ms CPU per decision left, "
                     f"recent cost {self.clock.avg_cost * 1000:.0f} ms)")
            state.fast_level = level

    # -- LLM: two calls once per night, merged -----------------------------------

    def _night_advice(self, night_start, payload: dict) -> None:
        """Two independent planning questions, asked once at the start of each night,
        each answered as {avoid_directions, duration_scale}. Their answers are merged
        (directions to avoid are unioned; the duration scale is averaged) before being
        applied to state.extra_avoid / state.duration_scale for the rest of the night."""
        state = self.state
        night_date = (night_start - timedelta(hours=12)).date().isoformat()
        left = self.clock.wall_left  # model waits cost real time, not CPU budget

        forecast_tonight = [n for n in self._last_forecast_notices if night_date in (n.get("nights") or [])]
        bulletin_notices = (payload.get("latest_bulletin") or {}).get("notices", [])
        answer_forecast = self.llm.ask_json(
            "You help schedule a telescope survey. Reply with one JSON object only: "
            '{"avoid_directions": [compass codes among N,NE,E,SE,S,SW,W,NW], "duration_scale": '
            "number 0.7-1.4}. Avoid directions with bad weather tonight, going by the forecast "
            "and the current bulletin; use a larger duration_scale when the sky looks poor.",
            {"night": night_date, "forecast_notices_for_tonight": forecast_tonight,
             "current_bulletin_notices": bulletin_notices},
            left,
        )

        hit_rate = (self.total_hit / self.total_assigned) if self.total_assigned > 0 else 1.0
        answer_bulletin = self.llm.ask_json(
            "You help schedule a telescope survey using tonight's live weather bulletin and the "
            'agent\'s own recent hit rate. Reply with one JSON object only: {"avoid_directions": '
            '[compass codes among N,NE,E,SE,S,SW,W,NW], "duration_scale": number 0.7-1.4}. Avoid '
            "directions the bulletin text describes as closed or obstructed right now. Raise "
            "duration_scale when the hit rate has been low (the sky has been performing poorly); "
            "lower it when the hit rate has been high.",
            {"night": night_date, "bulletin_text": _bulletin_text(bulletin_notices),
             "hit_rate_so_far": round(hit_rate, 3)},
            left,
        )

        avoid: set[str] = set()
        scales: list[float] = []
        for answer in (answer_forecast, answer_bulletin):
            if not answer:
                continue
            avoid |= {str(d).upper() for d in (answer.get("avoid_directions") or []) if str(d).upper() in DIRECTION_AZ}
            try:
                scales.append(min(1.4, max(0.7, float(answer.get("duration_scale", 1.0)))))
            except (TypeError, ValueError):
                pass
        state.extra_avoid = avoid
        state.duration_scale = sum(scales) / len(scales) if scales else 1.0
        self.log(f"planner: night {night_date} llm advice (forecast call: "
                 f"{'ok' if answer_forecast else 'fell back'}, bulletin call: "
                 f"{'ok' if answer_bulletin else 'fell back'}) merged avoid={sorted(avoid)} "
                 f"duration x{state.duration_scale:.2f}")
        self.trace.write({"event": "night_advice", "night_date": night_date, "avoid": sorted(avoid),
                          "scale": state.duration_scale, "forecast_call_ok": bool(answer_forecast),
                          "bulletin_call_ok": bool(answer_bulletin)})

    # -- instrument fault reporting (deterministic rules + LLM confirmation) -----

    def _maybe_report(self, hours: float, payload: dict):
        state = self.state
        state.force_program = None
        if self.reports >= MAX_REPORTS or hours - self.last_report_hours < 24.0:
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
        verdict_answer = self.llm.ask_json(
            "You check telescope data quality. A false instrument-fault report costs points, "
            'a correct one earns points. Reply with one JSON object only: {"report": true|false}.',
            evidence._asdict(), self.clock.wall_left,
        )
        verdict = verdict_answer.get("report") if isinstance(verdict_answer, dict) and \
            isinstance(verdict_answer.get("report"), bool) else None
        if verdict is False:
            self.log(f"planner: report vetoed by the model at {payload.get('now_utc')} ({evidence})")
            self.last_report_hours = hours
            return None
        self.reports += 1
        self.last_report_hours = hours
        state.forget_quality_history()
        self.log(f"planner: reporting instrument fault at {payload.get('now_utc')} evidence={evidence}")
        return {"action": "report", "reason": f"quality dropped to {evidence.drop:.0%} of the earlier level",
                "decision_source": "llm-confirmed" if verdict else "rule"}

    # -- planning value / achievability -----------------------------------------

    def _direction_factor(self, alt: float, az: float) -> float:
        state = self.state
        for direction in state.terrain:
            if direction in DIRECTION_AZ and alt < 50.0 and _az_distance(az, DIRECTION_AZ[direction]) <= 60.0:
                return 0.0
        factor = 1.0
        for key in state.notices:
            kind, _, direction = key.partition("|")
            if direction not in DIRECTION_AZ:
                continue
            near = _az_distance(az, DIRECTION_AZ[direction]) <= 67.5
            if kind in BLOCKING_KINDS and near and alt < 62.0:
                return 0.0
            if near and alt < 75.0:
                factor = min(factor, 0.35)
        for direction in state.extra_avoid:
            if direction in DIRECTION_AZ and _az_distance(az, DIRECTION_AZ[direction]) <= 67.5 and alt < 70.0:
                factor = min(factor, 0.35)
        for blocked_az, blocked_alt in state.blocked[-40:]:
            if _az_distance(az, blocked_az) <= 12.0 and alt <= blocked_alt + 3.0:
                factor = min(factor, 0.2)
        return factor

    def _request_thresholds(self, active_requests: list) -> dict:
        """Return the lowest still-needed factor threshold for each request target."""
        state = self.state
        thresholds: dict[int, float] = {}
        for request in active_requests:
            completed = set(request.get("completed_target_ids") or [])
            minimum = int(request.get("minimum_completed", 0))
            completed_count = int(request.get("completed_count", len(completed)))
            remaining = int(request.get("remaining_count", max(0, minimum - completed_count)))
            if remaining <= 0:
                continue
            threshold = float(request.get("completion_factor_threshold", 1.0))
            for target_id in request.get("target_ids", []):
                if target_id in completed:
                    continue
                i = state.index_of.get(target_id)
                if i is None or state.factor[i] >= threshold:
                    continue
                if i not in thresholds or threshold < thresholds[i]:
                    thresholds[i] = threshold
        return thresholds

    def _request_bonuses(self, active_requests: list, now) -> dict:
        """Estimate the per-target value of completing an active request.

        The exact reward is granted only when a request reaches its minimum count;
        this bounded estimate is used only to make request targets competitive during
        anchor search.  The full reward is evaluated in `_request_reward_for_plan`.
        """
        state = self.state
        bonuses: dict[int, float] = {}
        for request in active_requests:
            completed = set(request.get("completed_target_ids") or [])
            minimum = int(request.get("minimum_completed", 0))
            completed_count = int(request.get("completed_count", len(completed)))
            remaining = int(request.get("remaining_count", max(0, minimum - completed_count)))
            if remaining <= 0:
                continue
            target_ids = [str(value) for value in request.get("target_ids", [])]
            reward = max(0.0, float(request.get("completion_reward", 0.0)))
            miss_penalty = max(0.0, float(request.get("miss_penalty", 0.0)))
            seconds_left = float("inf")
            try:
                seconds_left = max(1.0, (parse_utc(request["deadline_utc"]) - now).total_seconds())
            except (KeyError, TypeError, ValueError):
                pass
            urgency = 1.0 if seconds_left == float("inf") else 1.0 + min(2.0, 21600.0 / seconds_left)
            per_target = (reward + miss_penalty) / max(1, remaining)
            bonus = min(4.0, per_target * REQUEST_BONUS_SCALE * urgency)
            for target_id in target_ids:
                if target_id in completed:
                    continue
                i = state.index_of.get(target_id)
                if i is not None and state.factor[i] < float(request.get("completion_factor_threshold", 1.0)):
                    bonuses[i] = max(bonuses.get(i, 0.0), bonus)
        return bonuses

    def _uniformity_penalty(self, observed: dict[int, int]) -> float:
        state = self.state
        weight = state.scoring.uniformity_weight
        if weight <= 0.0 or not self._uniformity_totals:
            return 0.0
        ratios = [observed.get(band, 0) / total for band, total in self._uniformity_totals.items()]
        squares = sum(value * value for value in ratios)
        if squares <= 0.0:
            return weight
        jain = (sum(ratios) ** 2) / (len(ratios) * squares)
        return weight * (1.0 - jain)

    def _prepare_uniformity(self) -> None:
        state = self.state
        width = state.scoring.uniformity_band_width_deg
        threshold = state.scoring.uniformity_threshold
        self._uniformity_totals = {}
        self._uniformity_observed = {}
        self._uniformity_gain_cache = {}
        if width <= 0.0 or state.scoring.uniformity_weight <= 0.0:
            self._uniformity_penalty_now = 0.0
            return
        for i, ra in enumerate(state.ra):
            band = int(ra // width)
            self._uniformity_totals[band] = self._uniformity_totals.get(band, 0) + 1
            if state.factor[i] >= threshold:
                self._uniformity_observed[band] = self._uniformity_observed.get(band, 0) + 1
        self._uniformity_penalty_now = self._uniformity_penalty(self._uniformity_observed)

    def _uniformity_gain(self, i: int) -> float:
        cached = self._uniformity_gain_cache.get(i)
        if cached is not None:
            return cached
        state = self.state
        if state.scoring.uniformity_weight <= 0.0 or state.scoring.uniformity_band_width_deg <= 0.0:
            self._uniformity_gain_cache[i] = 0.0
            return 0.0
        if state.factor[i] >= state.scoring.uniformity_threshold:
            self._uniformity_gain_cache[i] = 0.0
            return 0.0
        band = int(state.ra[i] // state.scoring.uniformity_band_width_deg)
        if band not in self._uniformity_totals:
            self._uniformity_gain_cache[i] = 0.0
            return 0.0
        observed = dict(self._uniformity_observed)
        observed[band] = min(self._uniformity_totals[band], observed.get(band, 0) + 1)
        gain = max(0.0, self._uniformity_penalty_now - self._uniformity_penalty(observed))
        self._uniformity_gain_cache[i] = gain
        return gain

    def _request_reward_for_plan(self, info: dict, duration: int, now) -> tuple[float, bool]:
        """Return rewards unlocked by this complete exposure, respecting deadlines."""
        state = self.state
        end = now + timedelta(seconds=duration)
        total = 0.0
        completed_any = False
        for request in self._active_requests_now:
            try:
                if end > parse_utc(request["deadline_utc"]):
                    continue
            except (KeyError, TypeError, ValueError):
                pass
            completed = set(request.get("completed_target_ids") or [])
            threshold = float(request.get("completion_factor_threshold", 1.0))
            for item in info.values():
                target_id = state.ids[item["i"]]
                if target_id not in request.get("target_ids", []) or item["up"] < duration:
                    continue
                if min(1.0, item["k"] * duration) >= threshold:
                    completed.add(target_id)
            minimum = int(request.get("minimum_completed", request.get("remaining_count", 1)))
            if len(completed) >= minimum:
                total += max(0.0, float(request.get("completion_reward", 0.0)))
                completed_any = True
        return total, completed_any

    def _value(self, i: int) -> float:
        """Planning value of fully completing target i from here (ignores how much
        exposure is achievable tonight)."""
        state = self.state
        f = state.factor[i]
        damp = 0.6 ** state.misses[i]
        threshold = state.scoring.required_threshold
        value = state.weight[i] * max(0.0, 1.0 - f) * damp
        if state.required[i] and f < threshold:
            value += max(0.0, state.scoring.required_penalty) * damp
        value += self._request_bonus_now.get(i, 0.0)
        value += self._uniformity_gain(i)
        return value

    # -- main planning pass -------------------------------------------------------

    def plan(self, now, night_end, night_index: int, hours: float):
        state = self.state
        state.update_scale(hours)
        self._prepare_uniformity()
        lst = local_sidereal_deg(now, state.lon)
        horizon = min(night_end, state.survey_end)
        seconds_left = (horizon - now).total_seconds()
        if seconds_left < state.min_exposure:
            return None
        min_visible = min(state.min_exposure, seconds_left) * SIDEREAL_DEG_PER_SECOND

        still_active = []
        candidates: list[tuple[float, int]] = []
        for i in state.active:
            v = self._value(i)
            if v <= 0.0:
                continue
            still_active.append(i)
            ha = wrap180(lst - state.ra[i])
            h = state.hmax[i]
            if -h <= ha <= h - min_visible:
                nights_left = max(1, state.last_night[i] - night_index + 1)
                setting = (1.0 + 0.5 * max(0.0, ha / h)) if h < 180 else 1.0
                candidates.append((v * (1.0 + 2.0 / nights_left) * setting, i))
        state.active = still_active
        if not candidates:
            return None
        candidates.sort(key=lambda t: -t[0])

        moon = Moon(now + timedelta(seconds=450), lst, state.lat)
        altaz_cache: dict[int, tuple[float, float]] = {}

        def altaz(i: int) -> tuple[float, float]:
            cached = altaz_cache.get(i)
            if cached is None:
                cached = radec_to_altaz(state.ra[i], state.dec[i], lst, state.lat)
                altaz_cache[i] = cached
            return cached

        visible = {i for _, i in candidates}
        achievable_cache: dict[int, float] = {}
        scoring = state.scoring

        def achievable(i: int) -> float:
            cached = achievable_cache.get(i)
            if cached is not None:
                return cached
            alt, az = altaz(i)
            lunar = lunar_factor(moon, state.ra[i], state.dec[i], scoring.lunar_model)
            model = scoring.quality_model(alt, lunar) or 0.0
            k = (state.flux[i] * model * state.scale * PLAN_FACTOR_SAFETY) / scoring.f0t0
            ha = wrap180(lst - state.ra[i])
            up = (state.hmax[i] - ha) / SIDEREAL_DEG_PER_SECOND if state.hmax[i] < 180 else 1e9
            reach = min(1.0, k * min(state.max_exposure, up, seconds_left))
            f = state.factor[i]
            gain = state.weight[i] * max(0.0, reach - f)
            if state.required[i] and f < scoring.required_threshold and reach >= scoring.required_threshold:
                gain += max(0.0, scoring.required_penalty)
            request_threshold = self._request_thresholds_now.get(i)
            if request_threshold is not None and reach >= request_threshold:
                gain += self._request_bonus_now.get(i, 0.0)
            if reach >= scoring.uniformity_threshold:
                gain += self._uniformity_gain(i)
            damp = (0.6 ** state.misses[i]) * (0.7 ** state.attempts[i])
            result = gain * damp * self._direction_factor(alt, az)
            achievable_cache[i] = result
            return result

        anchors: list[tuple[float, int]] = []
        for checked, (priority, i) in enumerate(candidates):
            if checked >= ANCHOR_POOL and len(anchors) >= 3 * ANCHORS:
                break
            weighted = achievable(i) * priority / max(1e-9, self._value(i))
            if weighted > 0:
                anchors.append((weighted, i))
        if not anchors:
            return None
        anchors.sort(key=lambda t: -t[0])

        n_anchors = 1 if state.fast_level >= 1 else ANCHORS
        fibers = range(self.grid.n) if state.fast_level < 2 else (5, 6, 9, 10)
        best = None  # (total, c_alt, c_az, chosen)
        tried = 0
        for _, anchor in anchors:
            if tried >= n_anchors and best is not None:
                break
            if tried >= n_anchors + 8:
                break
            tried += 1
            a_alt, a_az = altaz(anchor)
            near = [j for j in state.neighbours(state.ra[anchor], state.dec[anchor], NEIGHBOUR_RADIUS_DEG) if j in visible]
            near_values = {j: achievable(j) for j in near}
            for fiber in fibers:
                d_north, d_east = self.grid.fiber_center(fiber)
                c_alt, c_az = shift_altaz(a_alt, a_az, -d_north, -d_east)
                if not (state.min_alt + 1.5 <= c_alt <= 89.0):
                    continue
                c_alt = round(c_alt, 4)
                c_az = round(c_az, 4) % 360.0
                chosen: dict[int, tuple[float, int, float]] = {}  # fiber -> (score, j, margin)
                for j, v in near_values.items():
                    if v <= 0.0:
                        continue
                    alt, az = altaz(j)
                    offsets = tangent_offsets(alt, az, c_alt, c_az)
                    if offsets is None:
                        continue
                    fib, margin = self.grid.classify(*offsets)
                    if fib is None:
                        continue
                    score = v * (1.0 if margin >= EDGE_MARGIN_DEG * (1 + 1.5 * state.misses[j]) else 0.4)
                    existing = chosen.get(fib)
                    if existing is None or score > existing[0]:
                        chosen[fib] = (score, j, margin)
                if not chosen:
                    continue
                total = sum(score for score, _, _ in chosen.values())
                if best is None or total > best[0]:
                    best = (total, c_alt, c_az, chosen)
        if best is None:
            return None
        _, c_alt, c_az, chosen = best
        return self._finish_plan(now, lst, c_alt, c_az, chosen, seconds_left, moon, altaz, hours, night_index)

    def _finish_plan(self, now, lst, c_alt, c_az, chosen, seconds_left, moon, altaz, hours, night_index):
        state = self.state
        scoring = state.scoring
        c_ra, c_dec = altaz_to_radec(c_alt, c_az, lst, state.lat)
        c_hmax = max_hour_angle_deg(c_dec, state.lat, state.min_alt + 0.3)
        c_ha = wrap180(lst - c_ra)

        info: dict[int, dict] = {}
        for fiber, (_, j, _margin) in chosen.items():
            alt, az = altaz(j)
            lunar = lunar_factor(moon, state.ra[j], state.dec[j], scoring.lunar_model)
            model = scoring.quality_model(alt, lunar) or 0.0
            ha = wrap180(lst - state.ra[j])
            up = (state.hmax[j] - ha) / SIDEREAL_DEG_PER_SECOND if state.hmax[j] < 180 else 1e9
            k = (state.flux[j] * model * state.scale * PLAN_FACTOR_SAFETY) / scoring.f0t0
            info[fiber] = {"i": j, "alt": alt, "az": az, "model": model, "up": up, "k": k}
        center_up = (c_hmax - c_ha) / SIDEREAL_DEG_PER_SECOND if c_hmax < 180 else 1e9

        best = None  # (rate, duration)
        request_best = None  # (rate, duration): best candidate that also clears a needed
                              # request target's completion_factor_threshold
        for base in DURATIONS:
            duration = round((base * state.duration_scale) / 30.0) * 30
            duration = int(max(state.min_exposure, min(state.max_exposure, duration)))
            if duration > seconds_left or duration > center_up:
                continue
            gain = 0.0
            completes_request = False
            covered_bands: set[int] = set()
            for item in info.values():
                if item["up"] < duration:
                    continue
                reached = min(1.0, item["k"] * duration)
                f = state.factor[item["i"]]
                gain += state.weight[item["i"]] * max(0.0, reached - f)
                if state.required[item["i"]] and f < scoring.required_threshold and reached >= scoring.required_threshold:
                    gain += max(0.0, scoring.required_penalty)
                threshold = self._request_thresholds_now.get(item["i"])
                if threshold is not None and reached >= threshold:
                    gain += self._request_bonus_now.get(item["i"], 0.0)
                band = int(state.ra[item["i"]] // scoring.uniformity_band_width_deg) \
                    if scoring.uniformity_band_width_deg > 0.0 else None
                if reached >= scoring.uniformity_threshold and band not in covered_bands:
                    gain += self._uniformity_gain(item["i"])
                    if band is not None:
                        covered_bands.add(band)
            request_reward, request_completed = self._request_reward_for_plan(info, duration, now)
            gain += request_reward
            completes_request = request_completed
            rate = gain / duration
            if best is None or rate > best[0]:
                best = (rate, duration)
            if completes_request and (request_best is None or rate > request_best[0]):
                request_best = (rate, duration)
        if best is None:
            return None
        # Tie-break, not a bonus: duration_seconds is one value shared by all 16 fibres,
        # so this only ever swaps to a request-completing duration that is already within
        # REQUEST_RATE_TOLERANCE of the best achievable rate -- it can trade a small,
        # bounded amount of score for a shot at a reward, never meaningfully distort the
        # exposure length chosen for everything else in this pointing.
        if request_best is not None and request_best[0] >= best[0] * REQUEST_RATE_TOLERANCE:
            best = request_best
        duration = best[1]
        if best[0] <= 0.0:
            if state.has_recent_sample(hours):
                return None
            fallback = next((d for d in (900, 600, 300) if d <= seconds_left and d <= center_up), None)
            if fallback is None:
                return None
            duration = fallback

        assignments: dict[str, str] = {}
        for fiber, item in info.items():
            if item["up"] >= duration:
                assignments[str(fiber)] = state.ids[item["i"]]
        if not assignments:
            return None

        band_scale = state.scale / 0.95
        votes = {"DARK": 0.0, "BRIGHT": 0.0, "BACKUP": 0.0}
        for fiber, item in info.items():
            if str(fiber) not in assignments:
                continue
            band = scoring.program_band(item["model"] * band_scale)
            votes[band] += state.weight[item["i"]] * min(1.0, item["k"] * duration) + \
                (max(0.0, scoring.required_penalty) * 0.02 if state.required[item["i"]] else 0.0)
        program, best_score = "BACKUP", float("-inf")
        for name in ("DARK", "BRIGHT", "BACKUP"):
            matched = votes[name] * scoring.program_multipliers.get(name, 1.0)
            mismatched = (votes["DARK"] + votes["BRIGHT"] + votes["BACKUP"] - votes[name]) * scoring.mismatch_multiplier
            score = matched + mismatched
            if score > best_score:
                best_score, program = score, name
        if state.force_program:
            program = state.force_program

        clean = not state.all_sky_notice()
        state.pending.clear()
        state.pending_action_index = self._current_action_index
        for fiber, item in info.items():
            if str(fiber) in assignments:
                state.pending[state.ids[item["i"]]] = PendingPrediction(
                    model=item["model"], band_model=item["model"] / 0.95, alt=item["alt"], az=item["az"],
                    clean=clean and self._direction_factor(item["alt"], item["az"]) >= 1.0,
                )
        state.pending_program = program
        state.pending_duration = duration
        state.pending_night = night_index

        return {
            "action": "observe",
            "pointing": {"alt_deg": c_alt, "az_deg": c_az},
            "assignments": assignments,
            "duration_seconds": duration,
            "program": program,
        }
