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
from .state import EARLIER_SAMPLES, RECENT_SAMPLES, PendingPrediction


class JointSearch:
    @staticmethod
    def _best_plan(plans):
        # Linear unsaturated science has the same rate for 60 and 900 seconds.
        # Prefer the more complete *single* exposure within a 3% rate tolerance;
        # best-score scoring never accumulates the small fragments.
        top = max(p["rate"] for p in plans)
        return max((p for p in plans if p["rate"] >= top * 0.97),
                   key=lambda p: (p["utility"], p["rate"]))

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
        return value * max(0.1, 0.65 ** min(5, state.misses[i]))

    def _instrument_scale(self):
        """Estimate the efficiency factor that must be removed before banding.

        ``quality_scales`` is learned from science scores, so it includes the
        instrument efficiency.  Program bands use the efficiency-free quality
        from the public scoring contract.  The state keeps an exposure-level
        matched/mismatch signal for clean hits; use those public thresholds to
        gently adapt the efficiency estimate.  The estimate is deliberately
        conservative and cached for one planning pass: a single ambiguous
        hit must not flip every program choice.
        """
        state = self.state
        checks = list(getattr(state, "_band_checks", ()))[-32:]
        evidence = getattr(state, "band_evidence", {}) or {}
        evidence_total = sum(max(0, int(value)) for value in evidence.values())
        signature = (
            evidence_total,
            tuple(sorted((str(key), int(value)) for key, value in evidence.items())),
            tuple((str(program), bool(matched), round(float(model), 5))
                  for program, matched, model in checks[-8:]),
            round(float(getattr(state, "scale", 1.0)), 4),
        )
        if signature == getattr(self, "_instrument_scale_signature", None):
            return self._instrument_scale_value

        # No clean band feedback means that a neutral efficiency prior is less
        # biased than treating the score scale itself as a program-band scale.
        current = float(getattr(self, "_instrument_scale_value", 1.0))
        current = min(1.25, max(0.35, current))
        if not checks or evidence_total < 2:
            # ``forget_quality_history`` follows a confirmed repair/resync;
            # restart the efficiency prior with the fresh public evidence.
            self._instrument_scale_value = 1.0
            self._instrument_scale_signature = signature
            return self._instrument_scale_value

        bands = state.scoring.program_bands
        dark = max(1e-6, float(bands.get("DARK", 0.65)))
        bright = max(1e-6, float(bands.get("BRIGHT", 0.40)))
        total_scale = max(0.05, float(getattr(state, "scale", 1.0)))
        lower_bounds, upper_bounds = [], []
        for program, matched, model in checks:
            model = max(1e-6, float(model))
            # score/model ~= eta * B/model.  For a known matched/mismatched
            # program, derive the interval of eta compatible with the public
            # band thresholds.  BRIGHT mismatches have two disjoint branches;
            # retaining the current estimate avoids inventing a branch.
            lo, hi = 0.50, 1.25
            if program == "DARK":
                boundary = model * total_scale / dark
                if matched:
                    hi = min(hi, boundary)
                else:
                    lo = max(lo, boundary)
            elif program == "BRIGHT":
                low_boundary = model * total_scale / dark
                high_boundary = model * total_scale / bright
                if matched:
                    lo, hi = max(lo, low_boundary), min(hi, high_boundary)
                else:
                    # A mismatch can mean BACKUP or DARK.  Do not use it as a
                    # hard bound; its evidence still contributes confidence.
                    continue
            elif program == "BACKUP":
                boundary = model * total_scale / bright
                if matched:
                    lo = max(lo, boundary)
                else:
                    hi = min(hi, boundary)
            if lo <= hi:
                lower_bounds.append(lo)
                upper_bounds.append(hi)

        if lower_bounds or upper_bounds:
            # Weather varies between exposures, so use interior quantiles of
            # the one-sided constraints rather than intersecting every sample.
            # The geometric midpoint is appropriate for a multiplicative
            # efficiency factor and does not force a fixed instrument prior.
            def quantile(values, q):
                ordered = sorted(values)
                return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]

            lo = max(0.35, quantile(lower_bounds, 0.75) if lower_bounds else 0.35)
            hi = min(1.25, quantile(upper_bounds, 0.25) if upper_bounds else 1.25)
            if lo <= hi:
                target = math.sqrt(max(1e-9, lo * hi))
            else:
                target = current
            clean_evidence = max(0, int(evidence.get("matched", 0))) + max(0, int(evidence.get("mismatch", 0)))
            ambiguity = max(0, int(evidence.get("ambiguous", 0)))
            confidence = min(0.65, clean_evidence / (clean_evidence + 8.0))
            confidence *= max(0.25, 1.0 - ambiguity / max(1.0, evidence_total))
            current = (1.0 - confidence) * current + confidence * target
        self._instrument_scale_value = min(1.25, max(0.35, current))
        self._instrument_scale_signature = signature
        return self._instrument_scale_value

    def _fast_fibers(self):
        """Return a small spatially diverse, legal fibre subset for fast mode."""
        grid = self.grid
        side = grid.side
        middle = set(grid.representative_fibers())
        corners = {0, side - 1, grid.n - side, grid.n - 1}
        # Integer locations nearest one-quarter and three-quarters of each
        # axis.  They are distinct for useful grids and remain legal for 1/2/3.
        low = max(0, min(side - 1, int(math.floor(side * 0.25))))
        high = max(0, min(side - 1, int(math.ceil(side * 0.75)) - 1))
        quadrants = {row * side + col for row in (low, high) for col in (low, high)}
        return tuple(sorted(middle | corners | quadrants))

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
        instrument_scale = self._instrument_scale()
        band_scales = tuple(max(0.05, min(3.0, scale / instrument_scale)) for scale in scales)
        return {"i": i, "alt": alt, "az": az, "model": model,
                "up": min(up, seconds_left), "ks": tuple(k * s for s in scales),
                # Program bands exclude instrument efficiency; score scenarios
                # retain the full learned scale above.
                "band_qs": tuple(model * s for s in band_scales),
                "band_model": model / instrument_scale,
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
                thresholds.extend(view["threshold"] for view in self._request_views_now
                                  if i in view["needed"])
                for threshold in set(thresholds):
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
                               (self._request_bonus_now.get(x["i"], 0) * 0.05 if x["low_k"] * duration >= self._request_thresholds_now.get(x["i"], 2) else 0))
                    chosen[fiber] = item
                if not chosen:
                    continue
                variants = [chosen]
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
                                  "start": now, "completed": completed})
        if not plans:
            return []
        preferred = self._best_plan(plans)
        # Retain the long equal-rate alternative before truncating the beam.
        return [preferred] + heapq.nlargest(5, (p for p in plans if p is not preferred),
                                            key=lambda x: x["rate"])

    def _two_step(self, plans, best, now, night_end, night_index, hours):
        state = self.state
        if not self._request_views_now:
            return best
        # Keep a bounded request lookahead even in fast mode.  At level 2 it
        # only considers two first steps and two second-step plans, so the
        # request deadline signal survives CPU pacing without reopening the
        # full joint search.
        minimum_budget = 2.0 if state.fast_level >= 2 else 5.0
        if self.clock.compute_left() < minimum_budget:
            return best
        baseline_rate = max((x["science"] / x["duration"] for x in plans), default=0)
        contenders = []
        for view in self._request_views_now:
            partial = [p for p in plans if 0 < len(self._request_hits(p["items"], p["duration"], now, view)) < view["remaining"]]
            contenders.extend(heapq.nlargest(
                2 if state.fast_level >= 2 else 4,
                partial,
                key=lambda p: len(self._request_hits(p["items"], p["duration"], now, view)) / p["duration"],
            ))
        # At most two first steps in fast mode, each with two fresh second-step
        # request field plans.  The normal mode retains the wider beam.
        first_limit = 2 if state.fast_level >= 2 else 4
        second_limit = 2 if state.fast_level >= 2 else 4
        for first in sorted(contenders, key=lambda p: -p["rate"])[:first_limit]:
            later = now + timedelta(seconds=first["duration"])
            original_views = self._request_views_now
            second_views = []
            for view in original_views:
                hits = self._request_hits(first["items"], first["duration"], now, view)
                remaining = view["remaining"] - len(hits)
                if remaining > 0 and later < view["deadline"]:
                    second_views.append(dict(view, needed=view["needed"] - hits, remaining=remaining))
            if not second_views:
                continue
            horizon = min(night_end, max(v["deadline"] for v in second_views))
            if (horizon - later).total_seconds() < state.min_exposure:
                continue
            original_thresholds, original_bonuses = self._request_thresholds_now, self._request_bonus_now
            try:
                # Virtual request progress affects only second-step recall;
                # real progress and the exposure ledger stay feedback-only.
                self._request_views_now = second_views
                self._request_thresholds_now = self._request_thresholds([])
                self._request_bonus_now = self._request_bonuses([], later)
                second_plans = self._search(later, horizon, night_index,
                                            hours + first["duration"] / 3600, lookahead=True)
            finally:
                self._request_views_now = original_views
                self._request_thresholds_now, self._request_bonus_now = original_thresholds, original_bonuses
            for second in heapq.nlargest(second_limit, second_plans, key=lambda p: p["rate"]):
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
        candidates, still_active, cells = [], [], {}
        # Keep request targets in the recall set even when their ordinary
        # science value is saturated.  The request-specific anchor pass below
        # then decides whether at least one of them is actually reachable.
        request_needed = set()
        for view in self._request_views_now:
            request_needed.update(view["needed"])
        if not hasattr(state, "_sin_dec"):
            state._sin_dec = [math.sin(math.radians(d)) for d in state.dec]
            state._cos_dec = [math.cos(math.radians(d)) for d in state.dec]
        sin_lat, cos_lat = math.sin(math.radians(state.lat)), math.cos(math.radians(state.lat))
        cell_size = max(0.5, self.grid.fov * 0.7)
        reference_durations = tuple(dict.fromkeys((min(600, state.max_exposure),
                                                   min(1200, state.max_exposure),
                                                   min(2400, state.max_exposure), state.max_exposure)))
        for i in state.active:
            # cheap upper-bound value; celestial/quality work only for shortlist
            value = self._value(i)
            if value <= 1e-8 and i not in request_needed:
                continue
            still_active.append(i)
            ha, h = wrap180(lst - state.ra[i]), state.hmax[i]
            if h <= 0 or not -h <= ha <= h - min_visible:
                continue
            if lookahead and i not in self._request_thresholds_now:
                continue
            # Recall the fields that can earn science per unit time, rather
            # than only large-weight targets whose flux may be extremely low.
            sin_alt = max(0.05, sin_lat * state._sin_dec[i] +
                          cos_lat * state._cos_dec[i] * math.cos(math.radians(ha)))
            k = state.flux[i] * state.scale * sin_alt ** state.scoring.airmass_exponent / (
                state.scoring.q0 * state.scoring.f0t0)
            up = min(seconds_left, (h - ha) / SIDEREAL_DEG_PER_SECOND if h < 180 else seconds_left)
            rates = []
            for base in reference_durations:
                d = min(base, up)
                if d >= state.min_exposure:
                    rates.append(max(0.0, state.weight[i] * min(1.0, k * d) * self._top_multiplier -
                                     state.best_score[i]) / d)
            science_rate = max(rates, default=0.0)
            obligation = 0.0
            if state.required[i] and state.factor[i] < state.scoring.required_threshold and k > 0:
                need = max(state.min_exposure, state.scoring.required_threshold / (k * 0.7))
                if need <= min(up, state.max_exposure):
                    obligation += state.scoring.required_penalty / need
            if i in self._request_thresholds_now and k > 0:
                need = max(state.min_exposure, self._request_thresholds_now[i] / (k * 0.7))
                if need <= min(up, state.max_exposure):
                    obligation += self._request_bonus_now.get(i, 0) / need
            reliability = max(0.1, 0.65 ** min(5, state.misses[i]))
            priority = (science_rate + obligation) * reliability
            candidates.append((priority, i))
            # Two shifted celestial tilings give spatially diverse science
            # anchors. These are recall estimates; exact fibers decide below.
            if science_rate > 0 and not lookahead:
                for shift in (0.0, 0.5):
                    band = int((state.dec[i] + 90) / cell_size + shift)
                    center_dec = (band + 0.5 - shift) * cell_size - 90
                    count = max(1, int(360 * max(0.1, math.cos(math.radians(center_dec))) / cell_size))
                    key = (shift, band, int(state.ra[i] * count / 360 + shift) % count)
                    cells.setdefault(key, []).append((science_rate * reliability, i))
        if not lookahead:
            state.active = still_active
        if not candidates:
            return []
        visible = {i for _, i in candidates}
        pool = heapq.nlargest(160 if state.fast_level >= 2 else 320, candidates)
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
                value = 0.0
                for base in reference_durations + (state.max_exposure,):
                    d = min(base, x["up"])
                    if d < state.min_exposure:
                        continue
                    gain = max(self._target_gain(x, d, p) for p in ("DARK", "BRIGHT", "BACKUP"))
                    if x["low_k"] * d >= self._request_thresholds_now.get(i, 2):
                        gain += self._request_bonus_now.get(i, 0)
                    value = max(value, gain / d)
                reach_cache[i] = value * min(1, x["direction"]) * max(0.1, 0.65 ** min(5, state.misses[i]))
            return reach_cache[i]
        individual = heapq.nlargest(8, ((achievable(i), i) for _, i in pool))
        base_anchor_count = 2 if lookahead else (4 if state.fast_level >= 2 else 6)

        # Request targets can have little standalone science value and can be
        # absent from both the individual top-k and the spatial field winners.
        # Select one reachable target per active request before filling the
        # normal science anchors.  This is entirely driven by the public
        # request views, so it does not depend on card or target IDs.
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

        n_anchors = max(base_anchor_count, len(request_anchors))
        anchors = list(request_anchors)
        anchors.extend(i for v, i in individual[:2] if v > 0 and i not in anchors)
        fields = []
        for values in cells.values():
            strongest = heapq.nlargest(self.grid.n, values)
            fields.append((sum(v for v, _ in strongest), strongest[0][1]))
        # Do not spend the whole search budget on neighboring REQUIRED anchors.
        for _, i in heapq.nlargest(24, fields):
            if i in anchors:
                continue
            if achievable(i) <= 0:
                continue
            if any(abs(wrap180(state.ra[i] - state.ra[j])) * max(0.1, state._cos_dec[i]) < self.grid.fov * 0.45
                   and abs(state.dec[i] - state.dec[j]) < self.grid.fov * 0.45 for j in anchors):
                continue
            anchors.append(i)
            if len(anchors) >= n_anchors:
                break
        for _, i in individual:
            if len(anchors) >= n_anchors:
                break
            if i not in anchors:
                anchors.append(i)
        if state.fast_level == 0 and self.grid.n <= 25:
            fibers = tuple(range(self.grid.n))
        else:
            middle = self.grid.representative_fibers()
            corners = (0, self.grid.side - 1, self.grid.n - self.grid.side, self.grid.n - 1)
            fibers = self._fast_fibers() if state.fast_level >= 2 else tuple(dict.fromkeys(middle + corners))
        # Radius covers anchor-to-any-fiber distance across the complete field.
        radius = min(25.0, math.degrees(math.atan(math.radians(self.grid.fov * math.sqrt(2)))))
        pointings = []
        for anchor in anchors[:n_anchors]:
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
                    groups.setdefault(fib, []).append((score, j))
                if not groups:
                    continue
                selected = {}
                for fib, values in groups.items():
                    kept = heapq.nlargest(3, values)
                    kept_ids = {j for _, j in kept}
                    # Preserve the request anchor on this pointing even when
                    # its ordinary heuristic rank falls below three science
                    # alternatives on the same fibre.
                    kept.extend((score, j) for score, j in values
                                if j in request_anchor_set and j not in kept_ids)
                    selected[fib] = [item(j) for _, j in kept]
                heuristic = sum(max(v for v, _ in values) for values in groups.values())
                pointings.append((heuristic, c_alt, c_az, selected))
        plans = []
        pointing_limit = 3 if state.fast_level >= 2 else 5
        selected_pointings = heapq.nlargest(pointing_limit, pointings, key=lambda x: x[0])
        if request_anchor_set and len(selected_pointings) < len(pointings):
            # Keep the best pointing containing each request anchor in the
            # bounded beam.  A low-science request target must not disappear
            # solely because another field has a larger aggregate heuristic.
            for anchor in request_anchor_set:
                covered = [p for p in pointings
                           if any(anchor == selected_item["i"]
                                  for values in p[3].values() for selected_item in values)]
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
                model=item["model"], band_model=item.get("band_model", item["model"]),
                alt=item["alt"], az=item["az"], clean=not state.all_sky_notice() and item["direction"] >= 1)
        state.pending_program, state.pending_duration, state.pending_night = best["program"], duration, night_index
        self.exposure_ema = 0.9 * self.exposure_ema + 0.1 * duration
        if best.get("two_step"):
            self.trace.write({"event": "request_two_step", "night": night_index, "executed_steps": 1})
        return {"action": "observe", "pointing": {"alt_deg": c_alt, "az_deg": c_az},
                "assignments": {str(f): state.ids[x["i"]] for f, x in best["items"].items()},
                "duration_seconds": duration, "program": best["program"],
                "decision_source": "joint-planner"}
