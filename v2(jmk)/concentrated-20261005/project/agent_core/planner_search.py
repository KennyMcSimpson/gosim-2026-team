"""Bounded joint science/program/exposure search with deadline-aware requests.

Only public geometry, public messages and the agent's own feedback are used.
Completion predictions never update real progress; only a real result can do so.
"""
from __future__ import annotations

import heapq
import math
from datetime import timedelta

from .geometry import (Moon, SIDEREAL_DEG_PER_SECOND, altaz_to_radec,
                       local_sidereal_deg, lunar_factor, max_hour_angle_deg,
                       parse_utc, radec_to_altaz, shift_altaz, tangent_offsets, wrap180)
from .state import PendingPrediction


# The ordinary science shortlist is deliberately small for the 50k-target
# cards.  These limits reserve part of it for hard-task recovery so a REQUIRED
# or active request target cannot disappear solely because its current science
# score is below a high-value ordinary target.
PROTECTED_REQUIRED_LIMIT = 128
PROTECTED_REQUEST_LIMIT = 128
PROTECTED_PER_FIBER = 2

# A small, bounded recall bonus for ordinary targets in RA bands that are
# materially behind the current catalogue-wide completion rate.  This is a
# scheduling hint rather than a replacement for the authoritative uniformity
# penalty: keeping the cap below one typical target gain prevents a sparse band
# from displacing a clearly better science target.  REQUIRED/request targets
# bypass this bonus and keep their separate hard-task paths.
RA_BAND_PRIORITY_MAX = 0.12
RA_BAND_PRIORITY_MIN_GAP = 0.025
RA_BAND_PLAN_BONUS_MAX = 0.36


class JointSearch:
    @staticmethod
    def _best_plan(plans):
        """Prefer completion utility when science rates are effectively tied.

        Pure rate maximization favors many short fragments.  The scorer keeps
        the best exposure per target and request completion needs a whole
        exposure above its threshold, so a slightly slower but more complete
        plan is the safer choice.
        """
        top_rate = max(plan["rate"] for plan in plans)
        near_best = [plan for plan in plans if plan["rate"] >= top_rate * 0.97]
        return max(near_best, key=lambda plan: (plan["utility"], plan["rate"]))

    def _request_views(self, requests, now):
        state = self.state
        views = []
        for request in requests:
            try:
                issued = parse_utc(request["issued_at_utc"])
                deadline = parse_utc(request["deadline_utc"])
                minimum = int(request["minimum_completed"])
                threshold = float(request["completion_factor_threshold"])
                reward = max(0.0, float(request["completion_reward"]))
            except (KeyError, ValueError, TypeError):
                continue
            completed = set(map(str, request.get("completed_target_ids") or []))
            try:
                remaining_count = int(request.get("remaining_count", minimum - len(completed)))
            except (ValueError, TypeError):
                continue
            if now < issued or now >= deadline or len(completed) >= minimum or remaining_count <= 0:
                continue
            needed = {state.index_of[str(t)] for t in request.get("target_ids", [])
                      if str(t) in state.index_of and str(t) not in completed}
            remaining = max(0, minimum - len(completed))
            if not 0 < threshold <= 1 or remaining > len(needed) or not remaining:
                continue
            views.append({"id": str(request.get("request_id", len(views))), "issued": issued,
                          "deadline": deadline, "needed": needed, "remaining": remaining,
                          "threshold": threshold, "reward": reward})
        return views

    def _request_thresholds(self, active_requests):
        thresholds = {}
        for view in self._request_views_now:
            for i in view["needed"]:
                thresholds[i] = min(thresholds.get(i, 1.0), view["threshold"])
        return thresholds

    def _request_bonuses(self, active_requests, now):
        bonuses = {}
        for view in self._request_views_now:
            seconds_left = max(1, (view["deadline"] - now).total_seconds())
            value = view["reward"] / view["remaining"] * (1 + min(2.0, 21600 / seconds_left))
            for i in view["needed"]:
                bonuses[i] = max(bonuses.get(i, 0), value)
        return bonuses  # candidate recall only; never added to final action utility

    def _uniformity_penalty(self, observed):
        totals = self._uniformity_totals
        if not totals or self.state.scoring.uniformity_weight <= 0 or self.state.scoring.uniformity_band_width_deg <= 0:
            return 0.0
        ratios = [observed.get(b, 0) / n for b, n in totals.items()]
        squares = sum(r * r for r in ratios)
        jain = sum(ratios)**2 / (len(ratios) * squares) if squares else 0.0
        return self.state.scoring.uniformity_weight * (1 - jain)

    def _prepare_uniformity(self):
        state = self.state
        if self._uniformity_version == state.progress_version:
            return
        self._uniformity_version = state.progress_version
        self._uniformity_totals = state.band_totals
        self._uniformity_observed = state.band_observed
        self._uniformity_penalty_now = self._uniformity_penalty(state.band_observed)
        self._uniformity_gain_cache.clear()
        # The deficit is derived from the same authoritative progress snapshot
        # as the Jain penalty.  Never carry it across a result or resync.
        getattr(self, "_band_priority_cache", {}).clear()

    def _band_priority(self, i):
        """Return a bounded ordinary-science priority for an undercovered RA band.

        The scorer's uniformity term is based on completed targets, while the
        search operates on individual candidates.  A single-target Jain gain is
        too small to affect a 30k-target shortlist, so use the band's deficit
        from the global completion rate as a *recall* hint.  It is deliberately
        zero for unfinished REQUIRED or active request targets; those are
        protected by their dedicated candidate/variant logic.
        """
        state = self.state
        if (state.required[i] and state.factor[i] < state.scoring.required_threshold) or \
                i in self._request_thresholds_now:
            return 0.0
        cache = getattr(self, "_band_priority_cache", None)
        if cache is None:
            cache = self._band_priority_cache = {}
        if i in cache:
            return cache[i]
        if state.factor[i] >= state.scoring.uniformity_threshold:
            cache[i] = 0.0
            return 0.0
        band = state.ra_band[i]
        total = self._uniformity_totals.get(band, 0)
        if total <= 0:
            cache[i] = 0.0
            return 0.0
        total_targets = sum(self._uniformity_totals.values())
        total_observed = sum(self._uniformity_observed.values())
        if total_targets <= 0 or total_observed <= 0:
            cache[i] = 0.0
            return 0.0
        global_ratio = total_observed / total_targets
        band_ratio = self._uniformity_observed.get(band, 0) / total
        gap = global_ratio - band_ratio
        if global_ratio <= 0 or gap <= RA_BAND_PRIORITY_MIN_GAP:
            cache[i] = 0.0
            return 0.0
        # Normalize by a stable 0.25 floor so tiny early-run fluctuations do
        # not make the bonus dominate; cap at one before applying the gain cap.
        deficit = min(1.0, gap / max(0.25, global_ratio))
        bonus = RA_BAND_PRIORITY_MAX * deficit
        cache[i] = bonus
        return bonus

    def _uniformity_gain(self, i):
        state = self.state
        if state.factor[i] >= state.scoring.uniformity_threshold:
            return 0.0
        band = state.ra_band[i]
        if band not in self._uniformity_gain_cache:
            observed = dict(self._uniformity_observed)
            observed[band] = min(self._uniformity_totals.get(band, 0), observed.get(band, 0) + 1)
            self._uniformity_gain_cache[band] = max(0.0, self._uniformity_penalty_now - self._uniformity_penalty(observed))
        return self._uniformity_gain_cache[band]

    def _uniformity_group_gain(self, items, duration):
        state = self.state
        observed = dict(self._uniformity_observed)
        for item in items.values():
            i = item["i"]
            if state.factor[i] < state.scoring.uniformity_threshold <= item["low_k"] * duration:
                b = state.ra_band[i]
                observed[b] = min(state.band_totals[b], observed.get(b, 0) + 1)
        return max(0.0, self._uniformity_penalty_now - self._uniformity_penalty(observed))

    def _request_hits(self, items, duration, now, view):
        if now < view["issued"] or now + timedelta(seconds=duration) > view["deadline"]:
            return set()
        return {x["i"] for x in items.values() if x["i"] in view["needed"] and
                x["up"] >= duration and min(1, x["low_k"] * duration) >= view["threshold"]}

    def _request_reward_for_plan(self, info, duration, now):
        reward = 0.0
        for view in self._request_views_now:
            if len(self._request_hits(info, duration, now, view)) >= view["remaining"]:
                reward += view["reward"]
        return reward, reward > 0

    def _value(self, i):
        state = self.state
        value = max(0, state.weight[i] * self._top_multiplier - state.best_score[i])
        if state.required[i] and state.factor[i] < state.scoring.required_threshold:
            value += max(0, state.scoring.required_penalty)
        request_scale = {"request": 1.1, "science": 0.85}.get(state.advice_priority, 1.0)
        value += self._request_bonus_now.get(i, 0) * request_scale
        value += self._uniformity_gain(i)
        value += self._band_priority(i)
        return value * max(0.1, 0.65 ** min(5, state.misses[i]))

    def _item(self, i, altaz, lst, moon, seconds_left, night_index):
        state = self.state
        alt, az = altaz(i)
        model = state.scoring.quality_model(alt, lunar_factor(moon, state.ra[i], state.dec[i], state.scoring.lunar_model))
        bucket = int((az + 22.5) % 360 / 45)
        scales = self._quality_cache.get(bucket)
        if scales is None:
            scales = state.quality_scales(az)
            if state.advice_risk == "conservative":
                scales = (scales[0] * 0.9, scales[1], scales[2])
            self._quality_cache[bucket] = scales
        risk = self._risk_cache.get(bucket)
        if risk is None:
            risk = state.invalidation_probability(az)
            self._risk_cache[bucket] = risk
        ha = wrap180(lst - state.ra[i])
        up = (state.hmax[i] - ha) / SIDEREAL_DEG_PER_SECOND if state.hmax[i] < 180 else 1e9
        direction = self._direction_factor(alt, az)
        k = state.flux[i] * model / state.scoring.f0t0
        return {"i": i, "alt": alt, "az": az, "model": model,
                "up": min(up, seconds_left), "ks": tuple(k * s for s in scales),
                "band_qs": tuple(model * s / 0.95 for s in scales),
                "low_k": k * scales[0] * min(1.0, direction) * 0.98,
                "valid": 1 - risk, "direction": direction,
                "required_value": self.required_calendar.value(i, night_index)}

    def _science_gain(self, item, duration, program):
        state = self.state
        key = (duration, program)
        cache = item.setdefault("science_cache", {})
        if key in cache:
            return cache[key]
        i = item["i"]
        # Expectation of the positive improvement, not positive improvement at
        # the mean quality; this matters near saturation / best-score boundaries.
        gain = 0.0
        for probability, k, q in zip((0.2, 0.6, 0.2), item["ks"], item["band_qs"]):
            score = state.weight[i] * min(1, k * duration * item["direction"]) * state.scoring.program_multiplier(program, state.scoring.program_band(q))
            gain += probability * max(0, score - state.best_score[i])
        cache[key] = gain * item["valid"]
        return cache[key]

    def _target_gain(self, item, duration, program):
        gain = self._science_gain(item, duration, program)
        if item["low_k"] * duration >= self.state.scoring.required_threshold:
            gain += item["required_value"] * item["valid"]
        return gain

    def _duration_candidates(self, groups, cap, now):
        state = self.state
        cap = int(min(state.max_exposure, cap))
        if cap < state.min_exposure:
            return []
        durations = {state.min_exposure, cap}
        bases = (300, 600, 900, 1500, 2400, 3600)
        durations.update(d for d in bases if state.min_exposure <= d <= cap)
        boundaries = []
        for items in groups.values():
            for item in items:
                i, k = item["i"], item["low_k"]
                if k <= 0:
                    continue
                thresholds = []
                if item["required_value"] > 0:
                    thresholds.append(self.state.scoring.required_threshold)
                if i in self._request_thresholds_now:
                    thresholds.append(self._request_thresholds_now[i])
                for threshold in thresholds:
                    d = math.ceil(threshold / k) + 1
                    if state.min_exposure <= d <= min(cap, item["up"]):
                        boundaries.append((item["required_value"] + self._request_bonus_now.get(i, 0), d))
                # Science saturation at the expected sky level.
                d = math.ceil(1 / max(1e-9, item["ks"][1] * item["direction"]))
                if state.min_exposure <= d <= min(cap, item["up"]):
                    boundaries.append((state.weight[i], d))
        durations.update(d for _, d in heapq.nlargest(8, boundaries))
        for view in self._request_views_now:
            d = int((view["deadline"] - now).total_seconds())
            if state.min_exposure <= d <= cap:
                durations.add(d)
        return sorted(durations)

    def _joint_pointing(self, pointing, now, lst, seconds_left, night_index):
        state = self.state
        _, c_alt, c_az, groups = pointing
        c_ra, c_dec = altaz_to_radec(c_alt, c_az, lst, state.lat)
        h = max_hour_angle_deg(c_dec, state.lat, state.min_alt + 0.3)
        ha = wrap180(lst - c_ra)
        up = (h - ha) / SIDEREAL_DEG_PER_SECOND if h < 180 else 1e9
        plans = []
        programs = (state.force_program,) if state.force_program else ("DARK", "BRIGHT", "BACKUP")
        for duration in self._duration_candidates(groups, min(seconds_left, up), now):
            for program in programs:
                chosen = {}
                for fiber, options in groups.items():
                    eligible = [item for item in options if item["up"] >= duration and item["direction"] > 0]
                    if not eligible:
                        continue
                    # Request partial utility is only a tie/recall aid here.
                    # A completed-request alternative is evaluated below exactly.
                    item = max(eligible, key=lambda x: self._target_gain(x, duration, program) +
                               self._band_priority(x["i"]) +
                               (self._request_bonus_now.get(x["i"], 0) * 0.05 if x["low_k"] * duration >= self._request_thresholds_now.get(x["i"], 2) else 0))
                    chosen[fiber] = item
                if not chosen:
                    continue
                variants = [chosen]
                # Keep a REQUIRED-first alternative beside the ordinary science
                # layout.  Candidate recall alone is insufficient: without this
                # variant a low-weight REQUIRED target can be visible and still
                # lose every per-fibre comparison to a high-value science target.
                required_choice = dict(chosen)
                required_replacements = []
                for fiber, options in groups.items():
                    eligible = [
                        x for x in options
                        if state.required[x["i"]]
                        and state.factor[x["i"]] < state.scoring.required_threshold
                        and x["up"] >= duration
                        and x["direction"] > 0
                        and x["low_k"] * duration >= state.scoring.required_threshold
                    ]
                    if not eligible:
                        continue
                    item = max(
                        eligible,
                        key=lambda x: (x["required_value"], self._target_gain(x, duration, program)),
                    )
                    old_gain = self._target_gain(chosen[fiber], duration, program) if fiber in chosen else 0
                    loss = old_gain - self._target_gain(item, duration, program)
                    required_replacements.append((loss, fiber, item))
                if required_replacements:
                    for _, fiber, item in sorted(required_replacements, key=lambda row: row[0]):
                        required_choice[fiber] = item
                    variants.append(required_choice)
                # For each request, prefer its completable targets in occupied
                # fibers and compare the actual lost science with one reward.
                for view in self._request_views_now:
                    if now + timedelta(seconds=duration) > view["deadline"]:
                        continue
                    request_choice = dict(chosen)
                    replacements = []
                    for fiber, options in groups.items():
                        eligible = [x for x in options if x["i"] in view["needed"] and x["up"] >= duration and
                                    x["direction"] > 0 and x["low_k"] * duration >= view["threshold"]]
                        if eligible:
                            item = max(eligible, key=lambda x: self._target_gain(x, duration, program))
                            old_gain = self._target_gain(chosen[fiber], duration, program) if fiber in chosen else 0
                            loss = old_gain - self._target_gain(item, duration, program)
                            replacements.append((loss, fiber, item))
                    replacements.sort(key=lambda x: x[0])
                    # Keep this partial layout for bounded two-step planning even
                    # when one field cannot supply the entire remaining count.
                    for _, fiber, item in replacements[:view["remaining"]]:
                        request_choice[fiber] = item
                    if replacements:
                        variants.append(request_choice)
                for selected in variants:
                    science = sum(self._science_gain(item, duration, program) for item in selected.values())
                    utility = sum(self._target_gain(item, duration, program) for item in selected.values())
                    # Keep the deficit signal bounded and separate from the
                    # reported science sum.  This is a small uniformity-aware
                    # tie-break, never a hard-task substitute.
                    band_priority = min(
                        RA_BAND_PLAN_BONUS_MAX,
                        sum(self._band_priority(item["i"]) for item in selected.values()),
                    )
                    utility += band_priority
                    reward, completed = self._request_reward_for_plan(selected, duration, now)
                    # Joint validity is bounded conservatively by the weakest
                    # direction; no multiplied independent per-fiber fiction.
                    valid = min(x["valid"] for x in selected.values())
                    uniformity = self._uniformity_group_gain(selected, duration) * valid
                    utility += reward * valid + uniformity
                    if utility <= 0:
                        continue
                    plans.append({"utility": utility, "science": science, "duration": duration,
                                  "program": program, "items": selected, "pointing": (c_alt, c_az),
                                  "rate": utility / duration, "reward": reward,
                                  "valid": valid, "uniformity": uniformity,
                                  "band_priority": band_priority,
                                  "start": now, "completed": completed})
        return heapq.nlargest(6, plans, key=lambda x: x["rate"])

    def _two_step(self, plans, best, now, night_end, night_index, hours):
        if not self._request_views_now or self.state.fast_level >= 2 or self.clock.compute_left() < 5:
            return best
        state = self.state
        baseline_rate = max((x["science"] / x["duration"] for x in plans), default=0)
        contenders = []
        for view in self._request_views_now:
            partial = [p for p in plans if 0 < len(self._request_hits(p["items"], p["duration"], now, view)) < view["remaining"]]
            contenders.extend(heapq.nlargest(4, partial, key=lambda p: len(self._request_hits(p["items"], p["duration"], now, view)) / p["duration"]))
        # At most four first steps, each with four fresh second-step field plans.
        for first in sorted(contenders, key=lambda p: -p["rate"])[:4]:
            later = now + timedelta(seconds=first["duration"])
            horizon = min(night_end, min(v["deadline"] for v in self._request_views_now))
            if (horizon - later).total_seconds() < state.min_exposure:
                continue
            second_plans = self._search(later, horizon, night_index, hours + first["duration"] / 3600, lookahead=True)
            for second in heapq.nlargest(4, second_plans, key=lambda p: p["rate"]):
                reward = 0
                for view in self._request_views_now:
                    one = self._request_hits(first["items"], first["duration"], now, view)
                    two = self._request_hits(second["items"], second["duration"], later, view)
                    if len(one | two) >= view["remaining"]:
                        reward += view["reward"]
                if not reward:
                    continue
                # Conservatively exclude overlapping second-step gain. This is
                # a bounded lookahead estimate, not an exact virtual ledger.
                overlap = {x["i"] for x in first["items"].values()} & {x["i"] for x in second["items"].values()}
                duplicate = sum(self._target_gain(x, second["duration"], second["program"])
                                for x in second["items"].values() if x["i"] in overlap)
                combined = (first["utility"] + second["utility"] - first["reward"] * first["valid"] -
                            second["reward"] * second["valid"] - duplicate - second["uniformity"])
                combined += reward * max(0, first["valid"] + second["valid"] - 1)
                duration = first["duration"] + second["duration"]
                # R2: explicit opportunity cost, R3: replan after first real result.
                net = combined - baseline_rate * duration
                if net > 0 and combined / duration > best["rate"]:
                    best = dict(first, rate=combined / duration, two_step=True)
        return best

    def _search(self, now, night_end, night_index, hours, lookahead=False):
        state = self.state
        lst = local_sidereal_deg(now, state.lon)
        seconds_left = (min(night_end, state.survey_end) - now).total_seconds()
        if seconds_left < state.min_exposure:
            return []
        min_visible = state.min_exposure * SIDEREAL_DEG_PER_SECOND
        candidates, still_active = [], []
        required_targets = {
            i for i in state.active
            if state.required[i] and state.factor[i] < state.scoring.required_threshold
        }
        request_targets = set(self._request_thresholds_now)
        protected_targets = required_targets | request_targets
        for i in state.active:
            # cheap upper-bound value; celestial/quality work only for shortlist
            value = self._value(i)
            if value <= 1e-8 and i not in protected_targets:
                continue
            still_active.append(i)
            ha, h = wrap180(lst - state.ra[i]), state.hmax[i]
            if h <= 0 or not -h <= ha <= h - min_visible:
                continue
            if lookahead and i not in self._request_thresholds_now:
                continue
            nights_left = max(1, state.last_night[i] - night_index + 1)
            setting = 1 + 0.5 * max(0, ha / h) if h < 180 else 1
            # Keep a positive shortlist key even when a saturated request target
            # has no remaining science gain.  The request itself is still worth
            # protecting until the platform reports it completed.
            shortlist_value = max(value, 1e-6) if i in protected_targets else value
            candidates.append((shortlist_value * (1 + 2 / nights_left) * setting, i))
        if not lookahead:
            state.active = still_active
        if not candidates:
            return []
        visible = {i for _, i in candidates}
        pool_limit = 160 if state.fast_level >= 2 else 240
        pool = heapq.nlargest(pool_limit, candidates)
        pool_ids = {i for _, i in pool}

        # Add request targets explicitly, then reserve a bounded slice for the
        # most urgent REQUIRED targets.  Without this union, the global science
        # ranking can discard all low-weight hard targets before geometry is
        # evaluated.  Visibility and fibre feasibility are still checked below.
        request_limit = 64 if state.fast_level >= 2 else PROTECTED_REQUEST_LIMIT
        required_limit = 64 if state.fast_level >= 2 else PROTECTED_REQUIRED_LIMIT
        request_pool = heapq.nlargest(
            request_limit,
            ((score, i) for score, i in candidates if i in request_targets),
            key=lambda pair: pair[0],
        )
        required_pool = heapq.nlargest(
            required_limit,
            ((score, i) for score, i in candidates if i in required_targets),
            key=lambda pair: (
                1 / max(1, state.last_night[pair[1]] - night_index + 1),
                pair[0],
            ),
        )
        for entry in request_pool + required_pool:
            if entry[1] not in pool_ids:
                pool.append(entry)
                pool_ids.add(entry[1])
        moon = Moon(now + timedelta(seconds=min(450, seconds_left / 2)), lst, state.lat)
        altaz_cache, item_cache, reach_cache = {}, {}, {}
        def altaz(i):
            if i not in altaz_cache:
                altaz_cache[i] = radec_to_altaz(state.ra[i], state.dec[i], lst, state.lat)
            return altaz_cache[i]
        def item(i):
            if i not in item_cache:
                item_cache[i] = self._item(i, altaz, lst, moon, seconds_left, night_index)
            return item_cache[i]
        def achievable(i):
            if i not in reach_cache:
                x = item(i)
                d = min(state.max_exposure, x["up"])
                value = max(self._target_gain(x, d, p) for p in ("DARK", "BRIGHT", "BACKUP"))
                if x["low_k"] * d >= self._request_thresholds_now.get(i, 2):
                    value += self._request_bonus_now.get(i, 0)
                value += self._uniformity_gain(i) if x["low_k"] * d >= state.scoring.uniformity_threshold else 0
                value += self._band_priority(i)
                reach_cache[i] = value * min(1, x["direction"]) * max(0.1, 0.65 ** min(5, state.misses[i]))
            return reach_cache[i]
        # Select one reachable anchor per active request before the ordinary
        # science anchors.  A request target can be scientifically saturated
        # yet still be necessary for the all-or-nothing request reward.
        request_anchors = []
        request_anchor_set = set()
        programs = (state.force_program,) if state.force_program else ("DARK", "BRIGHT", "BACKUP")
        for view in self._request_views_now:
            remaining_seconds = min(
                seconds_left,
                max(0.0, (view["deadline"] - now).total_seconds()),
                state.max_exposure,
            )
            viable = []
            if remaining_seconds >= state.min_exposure:
                for i in view["needed"]:
                    if i not in visible:
                        continue
                    candidate = item(i)
                    duration = min(remaining_seconds, candidate["up"])
                    if (duration < state.min_exposure or candidate["direction"] <= 0 or
                            candidate["low_k"] * duration < view["threshold"]):
                        continue
                    gain = max(self._target_gain(candidate, duration, program)
                               for program in programs)
                    score = (gain + self._request_bonus_now.get(i, 0)) / max(duration, 1.0)
                    viable.append((score, candidate["low_k"] * duration, i))
            if viable:
                _, _, anchor = max(viable)
                if anchor not in request_anchor_set:
                    request_anchor_set.add(anchor)
                    request_anchors.append(anchor)

        ranked_anchors = [(achievable(i) * priority / max(1e-9, self._value(i)), i)
                          for priority, i in pool]
        ranked_by_id = {entry[1]: entry for entry in ranked_anchors}
        request_entries = [ranked_by_id.get(i, (achievable(i), i)) for i in request_anchors]
        anchors = request_entries + heapq.nlargest(8, ranked_anchors)
        # Put a few hard-task anchors in front of the ordinary shortlist.  The
        # later pointing cap is still bounded, but a request/REQUIRED target is
        # now guaranteed at least one geometry expansion opportunity whenever it
        # is visible and has positive achievable value.
        hard_anchors = heapq.nlargest(4, (entry for entry in ranked_anchors if entry[1] in protected_targets))
        anchor_ids = {i for _, i in hard_anchors}
        request_ids = {i for _, i in request_entries}
        anchors = request_entries + [
            entry for entry in hard_anchors if entry[1] not in request_ids
        ] + [
            entry for entry in anchors
            if entry[1] not in anchor_ids and entry[1] not in request_ids
        ]
        n_anchors = 2 if lookahead else (2 if state.fast_level >= 1 else 4)
        n_anchors = min(8, max(n_anchors, len(hard_anchors), len(request_entries)))
        if state.fast_level == 0 and self.grid.n <= 25:
            fibers = tuple(range(self.grid.n))
        else:
            middle = self.grid.representative_fibers()
            corners = (0, self.grid.side - 1, self.grid.n - self.grid.side, self.grid.n - 1)
            fibers = middle if state.fast_level >= 2 else tuple(dict.fromkeys(middle + corners))
        # Radius covers anchor-to-any-fiber distance across the complete field.
        radius = min(25.0, math.degrees(math.atan(math.radians(self.grid.fov * math.sqrt(2)))))
        pointings = []
        for _, anchor in anchors[:n_anchors]:
            a_alt, a_az = altaz(anchor)
            near = [j for j in state.neighbours(state.ra[anchor], state.dec[anchor], radius) if j in visible]
            near_values = {j: achievable(j) for j in near}
            for fiber in fibers:
                dn, de = self.grid.fiber_center(fiber)
                c_alt, c_az = shift_altaz(a_alt, a_az, -dn, -de)
                if not state.min_alt + 1 <= c_alt <= 89.0:
                    continue
                c_alt, c_az = round(c_alt, 4), round(c_az, 4) % 360
                groups = {}
                for j, v in near_values.items():
                    if v <= 0 and j not in request_anchor_set:
                        continue
                    alt, az = altaz(j)
                    offsets = tangent_offsets(alt, az, c_alt, c_az)
                    if offsets is None:
                        continue
                    fib, margin = self.grid.classify(*offsets)
                    if fib is None:
                        continue
                    edge = min(0.08, self.grid.glass * 0.12) * (1 + min(2, state.misses[j]))
                    score = v * (1 if margin >= edge else 0.4)
                    if j not in protected_targets:
                        score += self._band_priority(j)
                    groups.setdefault(fib, []).append((score, j))
                if not groups:
                    continue
                selected = {}
                for fib, values in groups.items():
                    chosen_values = heapq.nlargest(3, values, key=lambda pair: pair[0])
                    chosen_ids = {j for _, j in chosen_values}
                    # A protected target that shares a fibre with ordinary
                    # candidates must survive the per-fibre top-3 cut too.
                    hard_values = [pair for pair in values if pair[1] in protected_targets]
                    hard_values.sort(
                        key=lambda pair: (
                            pair[1] in request_targets,
                            pair[1] in required_targets,
                            1 / max(1, state.last_night[pair[1]] - night_index + 1),
                            pair[0],
                        ),
                        reverse=True,
                    )
                    for pair in hard_values[:PROTECTED_PER_FIBER]:
                        if pair[1] not in chosen_ids:
                            chosen_values.append(pair)
                            chosen_ids.add(pair[1])
                    selected[fib] = [item(j) for _, j in chosen_values]
                heuristic = sum(max(v for v, _ in values) for values in groups.values())
                pointings.append((heuristic, c_alt, c_az, selected))
        plans = []
        pointing_limit = 2 if state.fast_level >= 2 else 3
        selected_pointings = heapq.nlargest(pointing_limit, pointings, key=lambda x: x[0])
        if request_anchor_set and len(selected_pointings) < len(pointings):
            # Keep the best field containing each request anchor in the bounded
            # beam; otherwise a high-science field can erase the request variant
            # before _joint_pointing gets to evaluate its exact reward.
            for anchor in request_anchor_set:
                covered = [
                    p for p in pointings
                    if any(anchor == selected_item["i"]
                           for values in p[3].values() for selected_item in values)
                ]
                if not covered:
                    continue
                best_request_pointing = max(covered, key=lambda x: x[0])
                if best_request_pointing not in selected_pointings:
                    selected_pointings.append(best_request_pointing)
        for pointing in selected_pointings:
            plans.extend(self._joint_pointing(pointing, now, lst, seconds_left, night_index))
        return plans

    def plan(self, now, night_end, night_index, hours):
        state = self.state
        state.update_scale(hours)
        self._prepare_uniformity()
        self._quality_cache, self._risk_cache = {}, {}
        self.required_calendar.builds_left = 2 if state.fast_level >= 2 else 8
        plans = self._search(now, night_end, night_index, hours)
        if not plans:
            return None
        best = self._best_plan(plans)
        best = self._two_step(plans, best, now, night_end, night_index, hours)
        c_alt, c_az = best["pointing"]
        duration = best["duration"]
        state.pending.clear()
        state.pending_action_index = self._current_action_index
        state.pending_start, state.pending_end = now, now + timedelta(seconds=duration)
        for item in best["items"].values():
            state.pending[state.ids[item["i"]]] = PendingPrediction(
                model=item["model"], band_model=item["model"] / 0.95,
                alt=item["alt"], az=item["az"], clean=not state.all_sky_notice() and item["direction"] >= 1)
        state.pending_program, state.pending_duration, state.pending_night = best["program"], duration, night_index
        self.exposure_ema = 0.9 * self.exposure_ema + 0.1 * duration
        if best.get("two_step"):
            self.trace.write({"event": "request_two_step", "night": night_index, "executed_steps": 1})
        return {"action": "observe", "pointing": {"alt_deg": c_alt, "az_deg": c_az},
                "assignments": {str(f): state.ids[x["i"]] for f, x in best["items"].items()},
                "duration_seconds": duration, "program": best["program"],
                "decision_source": "joint-planner"}
