"""Bounded joint science/program/exposure search with deadline-aware requests.

Only public geometry, public messages and the agent's own feedback are used.
Completion predictions never update real progress; only a real result can do so.
"""
from __future__ import annotations

import heapq
import math
import time
from datetime import timedelta

from .geometry import (Moon, SIDEREAL_DEG_PER_SECOND, altaz_to_radec,
                       local_sidereal_deg, lunar_factor, max_hour_angle_deg,
                       parse_utc, radec_to_altaz, shift_altaz, tangent_offsets, wrap180)
from .state import EARLIER_SAMPLES, RECENT_SAMPLES, PendingPrediction


class JointSearch:
    def _select_candidate(self, plans, best, now):
        selector = getattr(self, "candidate_selector", None)
        if selector is None:
            return best
        if self.clock.wall_remaining() <= 300 or self._out_of_search_time():
            return best
        from .decision_bridge import CandidateBridge

        if self._candidate_bridge is None:
            self._candidate_bridge = CandidateBridge()
        # Keep the local winner available even if it is a partial/sequence plan
        # whose utility is not represented by the ordinary science shortlist.
        alternatives = heapq.nlargest(7, (p for p in plans if p is not best and p["utility"] > 0),
                                      key=lambda p: p["rate"])
        token = f"{now.isoformat()}:{self.state.progress_version}:{self._current_action_index}"
        offer = self._candidate_bridge.offer([best] + alternatives, token, 0.25)
        try:
            # This local callback maps already-computed advice to this offer,
            # never waits on a model. IDs from earlier offers are invalid.
            # A future model transport needs its own deadline/cancellation.
            response = selector(offer)
            chosen = self._candidate_bridge.take(response, token)
        except Exception as exc:
            self.log(f"planner: candidate selection failed ({type(exc).__name__})")
            chosen = None
        if chosen is None:
            return best
        self.trace.write({"event": "candidate_selected", "offer_version": offer["offer_version"]})
        return chosen

    def _plan_rate(self, utility, duration, steps=1):
        # Charge a decision its share of the remaining observing season. This
        # amortizes actual measured CPU without imposing a fixed exposure floor.
        return utility / (duration + steps * getattr(self, "_decision_overhead", 0.0))

    def _out_of_search_time(self):
        return time.process_time() >= getattr(self, "_search_deadline", float("inf"))

    def _prepare_geometry(self):
        if hasattr(self, "_tile_ids"):
            return
        state = self.state
        state._sin_dec = [math.sin(math.radians(d)) for d in state.dec]
        state._cos_dec = [math.cos(math.radians(d)) for d in state.dec]
        cell_size = max(0.5, self.grid.fov * 0.7)
        self._tile_ids = []
        for ra, dec in zip(state.ra, state.dec):
            keys = []
            for shift in (0.0, 0.5):
                band = int((dec + 90) / cell_size + shift)
                center_dec = (band + 0.5 - shift) * cell_size - 90
                count = max(1, int(360 * max(0.1, math.cos(math.radians(center_dec))) / cell_size))
                keys.append((shift, band, int(ra * count / 360 + shift) % count))
            self._tile_ids.append(tuple(keys))

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
        instrument_scale = getattr(self, "_pass_instrument_scale", None)
        if instrument_scale is None:
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
                    partial = any(self._request_hits(selected, duration, now, v)
                                  for v in self._request_views_now)
                    if utility <= 0 and not partial:
                        continue
                    plans.append({"utility": utility, "science": science, "duration": duration,
                                  "program": program, "items": selected, "pointing": (c_alt, c_az),
                                  "rate": self._plan_rate(utility, duration), "reward": reward,
                                  "valid": valid, "uniformity": uniformity,
                                  "start": now, "completed": completed})
        if not plans:
            return []
        preferred = self._best_plan(plans)
        # Retain the long equal-rate alternative before truncating the beam.
        kept = [preferred] + heapq.nlargest(3, (p for p in plans if p is not preferred),
                                           key=lambda x: x["rate"])
        for view in self._request_views_now:
            partials = [p for p in plans if self._request_hits(p["items"], p["duration"], now, view)]
            if partials:
                partial = max(partials, key=lambda p: (
                    len(self._request_hits(p["items"], p["duration"], now, view)) / p["duration"],
                    p["utility"]))
                if all(partial is not p for p in kept):
                    kept.append(partial)
        if getattr(self, "_review_collect", False):
            pool = self._review_pool + plans
            # Keep numerical representatives without more sky search or an
            # unbounded candidate history. All entries were evaluated above.
            keys = (lambda p: p["rate"], lambda p: p["reward"] * p["valid"],
                    lambda p: p["uniformity"], lambda p: p["valid"],
                    lambda p: p["utility"] - p["science"] - p["reward"] * p["valid"] - p["uniformity"],
                    lambda p: p["science"] / p["duration"])
            frontier, seen = [], set()
            for key in keys:
                for p in heapq.nlargest(6, pool, key=key):
                    if id(p) not in seen:
                        frontier.append(p)
                        seen.add(id(p))
            self._review_pool = frontier
        return kept

    def _two_step(self, plans, best, now, night_end, night_index, hours):
        state = self.state
        if not self._request_views_now:
            return best
        baseline_rate = max((self._plan_rate(p["utility"] - p["reward"] * p["valid"], p["duration"])
                             for p in plans), default=0.0)
        original_views = self._request_views_now
        original_thresholds, original_bonuses = self._request_thresholds_now, self._request_bonus_now
        for view in original_views:
            partial = [p for p in plans if 0 < len(self._request_hits(
                p["items"], p["duration"], now, view)) < view["remaining"]]
            contenders = heapq.nlargest(2, partial, key=lambda p: (
                len(self._request_hits(p["items"], p["duration"], now, view)) / p["duration"],
                p["utility"]))
            for first in contenders:
                if self._out_of_search_time():
                    break
                sequence, later = [first], now + timedelta(seconds=first["duration"])
                hits = self._request_hits(first["items"], first["duration"], now, view)
                horizon = min(night_end, view["deadline"])
                try:
                    # Only the small remaining request set is searched. All
                    # virtual progress is local; each real result replans it.
                    while (len(hits) < view["remaining"] and len(sequence) < 8 and
                           (horizon - later).total_seconds() >= state.min_exposure and
                           not self._out_of_search_time()):
                        remaining_view = dict(view, needed=view["needed"] - hits,
                                              remaining=view["remaining"] - len(hits))
                        self._request_views_now = [remaining_view]
                        self._request_thresholds_now = self._request_thresholds([])
                        self._request_bonus_now = self._request_bonuses([], later)
                        future = self._search(later, horizon, night_index,
                                              hours + (later - now).total_seconds() / 3600,
                                              lookahead=True)
                        eligible = [(p, self._request_hits(p["items"], p["duration"], later, view) - hits)
                                    for p in future]
                        eligible = [(p, new) for p, new in eligible if new]
                        if not eligible:
                            break
                        following, new = max(eligible, key=lambda x: (
                            len(x[1]) / x[0]["duration"], x[0]["utility"]))
                        sequence.append(following)
                        hits |= new
                        later += timedelta(seconds=following["duration"])
                finally:
                    self._request_views_now = original_views
                    self._request_thresholds_now, self._request_bonus_now = original_thresholds, original_bonuses
                if len(hits) < view["remaining"]:
                    continue
                earned = 0.0
                for other in original_views:
                    covered = set()
                    for p in sequence:
                        covered |= self._request_hits(p["items"], p["duration"], p["start"], other)
                    if len(covered) >= other["remaining"]:
                        earned += other["reward"]
                seen, gain = set(), 0.0
                for p in sequence:
                    gain += sum(self._target_gain(x, p["duration"], p["program"])
                                for x in p["items"].values() if x["i"] not in seen)
                    seen.update(x["i"] for x in p["items"].values())
                # Union bound across exposures, without an independence claim.
                valid = max(0.0, 1 - sum(1 - p["valid"] for p in sequence))
                combined = gain + first["uniformity"] + earned * valid
                duration = sum(p["duration"] for p in sequence)
                rate = self._plan_rate(combined, duration, len(sequence))
                if rate > baseline_rate and (best is None or rate > best["rate"]):
                    best = dict(first, rate=rate, two_step=True, request_steps=len(sequence))
        if best is None:
            # The search deadline can interrupt proof of a full request
            # sequence. When ordinary science is exhausted, retain a valid
            # partial exposure rather than waiting through the whole window.
            # This is a recall heuristic, not earned reward or real progress.
            def partial_rate(plan):
                progress = sum(
                    view["reward"] * len(self._request_hits(
                        plan["items"], plan["duration"], now, view)) / view["remaining"]
                    for view in original_views
                )
                return self._plan_rate(progress * plan["valid"], plan["duration"])

            partial = max(plans, key=partial_rate, default=None)
            if partial is not None and partial_rate(partial) > 0:
                best = dict(partial, request_partial=True)
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
        self._prepare_geometry()
        sin_lat, cos_lat = math.sin(math.radians(state.lat)), math.cos(math.radians(state.lat))
        coarse_values = {}
        # Future request probes have a small explicit target set. They never
        # rescan the science catalogue or prune the real active list.
        scan = sorted(request_needed) if lookahead else state.active
        for i in scan:
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
            # For max(0, W * min(k*t, 1) - best)/t the maximum is at
            # saturation clamped to the available exposure interval.
            cap = min(up, state.max_exposure)
            d = min(cap, max(state.min_exposure, 1 / k)) if k > 0 else cap
            science_rate = (max(0.0, state.weight[i] * min(1.0, k * d) * self._top_multiplier -
                                state.best_score[i]) / d if d >= state.min_exposure else 0.0)
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
            coarse_values[i] = priority
            # Two shifted celestial tilings give spatially diverse science
            # anchors. These are recall estimates; exact fibers decide below.
            if science_rate > 0 and not lookahead:
                for key in self._tile_ids[i]:
                    cells.setdefault(key, []).append((science_rate * reliability, i))
        if not lookahead:
            state.active = still_active
        if not candidates:
            return []
        visible = {i for _, i in candidates}
        pool = heapq.nlargest(160 if state.fast_level >= 2 else 320, candidates)
        moon = Moon(now + timedelta(seconds=min(450, seconds_left / 2)), lst, state.lat)
        altaz_cache, item_cache = {}, {}
        def altaz(i):
            if i not in altaz_cache:
                altaz_cache[i] = radec_to_altaz(state.ra[i], state.dec[i], lst, state.lat)
            return altaz_cache[i]
        def item(i):
            if i not in item_cache:
                item_cache[i] = self._item(i, altaz, lst, moon, seconds_left, night_index)
            return item_cache[i]
        def achievable(i):
            return coarse_values.get(i, 0.0)
        individual = pool[:8]
        base_anchor_count = 2 if lookahead else (3 if state.fast_level >= 2 else 6)

        # Request targets can have little standalone science value and can be
        # absent from both the individual top-k and the spatial field winners.
        # Select one reachable target per active request before filling the
        # normal science anchors.  This is entirely driven by the public
        # request views, so it does not depend on card or target IDs.
        request_anchors = []
        request_anchor_set = set()
        programs = (state.force_program,) if state.force_program else ("DARK", "BRIGHT", "BACKUP")
        for view in sorted(self._request_views_now, key=lambda v: (v["deadline"], -v["reward"])):
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

        n_anchors = base_anchor_count + len(request_anchors)
        anchors = []
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
            if len(anchors) >= max(1, base_anchor_count - 1):
                break
        for _, i in individual:
            if len(anchors) >= base_anchor_count:
                break
            if i not in anchors:
                anchors.append(i)
        # A field-level science baseline must exist before spending the rest
        # of the beam on a request whose reward might not cover its cost.
        anchors.extend(i for i in request_anchors if i not in anchors)
        protected = set(anchors[:1])
        if request_anchors:
            # Reserve at most one request layout after the science baseline.
            # Otherwise an already-expired slice never evaluates this request,
            # even though its anchor survived all recall/truncation stages.
            urgent = request_anchors[0]
            protected.add(urgent)
            anchors = list(dict.fromkeys(anchors[:1] + [urgent] + anchors))
        if state.fast_level == 0 and self.grid.n <= 25:
            fibers = tuple(range(self.grid.n))
        else:
            middle = self.grid.representative_fibers()
            corners = (0, self.grid.side - 1, self.grid.n - self.grid.side, self.grid.n - 1)
            fibers = self._fast_fibers() if state.fast_level >= 2 else tuple(dict.fromkeys(middle + corners))
        # Radius covers anchor-to-any-fiber distance across the complete field.
        radius = min(25.0, math.degrees(math.atan(math.radians(self.grid.fov * math.sqrt(2)))))
        pointings, plans, evaluated = [], [], set()
        for anchor in anchors[:n_anchors]:
            if plans and anchor not in protected and self._out_of_search_time():
                break
            a_alt, a_az = altaz(anchor)
            near = [j for j in state.neighbours(state.ra[anchor], state.dec[anchor], radius) if j in visible]
            near_values = {j: achievable(j) for j in near}
            # Target unit vectors are constant across the anchor's fibre
            # placements. Cache them once; the gnomonic projection is exact.
            vectors = {}
            for j in near:
                alt, az = map(math.radians, altaz(j))
                ca = math.cos(alt)
                vectors[j] = (ca * math.cos(az), ca * math.sin(az), math.sin(alt))
            anchor_pointings = []
            for fiber in fibers:
                dn, de = self.grid.fiber_center(fiber)
                c_alt, c_az = shift_altaz(a_alt, a_az, -dn, -de)
                if not state.min_alt + 1 <= c_alt <= 89.0:
                    continue
                c_alt, c_az = round(c_alt, 4), round(c_az, 4) % 360
                groups = {}
                alt_r, az_r = math.radians(c_alt), math.radians(c_az)
                sa, ca, sz, cz = math.sin(alt_r), math.cos(alt_r), math.sin(az_r), math.cos(az_r)
                center = (ca * cz, ca * sz, sa)
                north, east = (-sa * cz, -sa * sz, ca), (-sz, cz, 0.0)
                for j, v in near_values.items():
                    if v <= 0 and j not in request_needed:
                        continue
                    x, y, z = vectors[j]
                    depth = x * center[0] + y * center[1] + z * center[2]
                    if depth <= 0:
                        continue
                    offsets = (math.degrees((x * north[0] + y * north[1] + z * north[2]) / depth),
                               math.degrees((x * east[0] + y * east[1]) / depth))
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
                pointing = (heuristic, c_alt, c_az, selected)
                pointings.append(pointing)
                anchor_pointings.append(pointing)
            # Keep a complete, validated-layout candidate before trying another
            # anchor. A cutoff preserves this best-so-far observation.
            if anchor_pointings:
                seed = max(anchor_pointings, key=lambda p: p[0])
                plans.extend(self._joint_pointing(seed, now, lst, seconds_left, night_index))
                evaluated.add(id(seed))
        pointing_limit = 2 if state.fast_level >= 2 else 5
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
            if id(pointing) in evaluated:
                continue
            # Finish one legal useful layout before interrupting refinement.
            if any(p["utility"] > 0 for p in plans) and self._out_of_search_time():
                break
            plans.extend(self._joint_pointing(pointing, now, lst, seconds_left, night_index))
        return plans

    def plan(self, now, night_end, night_index, hours):
        state = self.state
        state.update_scale(hours)
        self._prepare_uniformity()
        self._quality_cache, self._risk_cache = {}, {}
        self._pass_instrument_scale = self._instrument_scale()
        self._planning_hours = hours
        self._refresh_blocked_memory(hours)
        allowance = self._search_allowance
        if allowance is None or not math.isfinite(allowance):
            allowance = (0.30, 0.09, 0.045)[min(2, state.fast_level)]
        started = self.clock._started if self.clock._started is not None else time.process_time()
        self._search_deadline = started + max(0.0, allowance * 0.9)
        self.required_calendar.builds_left = 2 if state.fast_level >= 2 else 8
        reviewer = getattr(self, "plan_reviewer", None)
        self._review_pool = []
        self._review_collect = reviewer is not None and reviewer.due(self, night_index)
        try:
            plans = self._search(now, night_end, night_index, hours)
        finally:
            self._review_collect = False
        if not plans:
            return None
        positive = [p for p in plans if p["utility"] > 0]
        best = self._best_plan(positive) if positive else None
        best = self._two_step(plans, best, now, night_end, night_index, hours)
        if best is None:
            return None
        best = self._select_candidate(plans, best, now)
        if reviewer is not None:
            try:
                best = reviewer.select(self, plans + self._review_pool, best, now, night_end, night_index)
            except Exception as exc:
                reviewer.last_outcome = "review_error"
                self.log(f"pi: review error ({type(exc).__name__}); using recovery plan")
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
        elif best.get("request_partial"):
            self.trace.write({"event": "request_partial", "night": night_index, "executed_steps": 1})
        action = self._plan_action(best)
        if reviewer is not None and reviewer.last_outcome == "applied":
            action["decision_source"] = "v4-pi"
        return action

    def _plan_action(self, plan):
        return {"action": "observe", "pointing": {"alt_deg": plan["pointing"][0], "az_deg": plan["pointing"][1]},
                "assignments": {str(f): self.state.ids[x["i"]] for f, x in plan["items"].items()},
                "duration_seconds": plan["duration"], "program": plan["program"],
                "decision_source": "joint-planner"}
