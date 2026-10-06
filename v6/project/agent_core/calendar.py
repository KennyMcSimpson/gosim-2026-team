"""Lazy public REQUIRED geometry/lunar calendar; future weather remains unknown."""
from __future__ import annotations

from .geometry import Moon, local_sidereal_deg, lunar_factor, radec_to_altaz, wrap180


class RequiredCalendar:
    def __init__(self, state):
        self.state = state
        self.suffix = {}
        # Keep the public, geometry-only window information separately from
        # the score used by the bounded search.  The latter may be lazy, but
        # the former is needed to tell an actually urgent REQUIRED target from
        # one that still has several nights in hand.
        self.window_nights = {}
        self.window_durations = {}
        self.builds_left = 0
        self.moons = []
        self.spans = []
        for start, end in state.nights:
            mid = start + (end - start) / 2
            self.moons.append(Moon(mid, local_sidereal_deg(mid, state.lon), state.lat))
            self.spans.append((local_sidereal_deg(start, state.lon), (end - start).total_seconds() * 360.98564736629 / 86400))

    def _build(self, i):
        state = self.state
        opportunities = []
        feasible_nights = []
        durations = {}
        for k, (lst, span) in enumerate(self.spans):
            ha = wrap180(lst - state.ra[i])
            opportunity = False
            best_duration = 0.0
            for shift in (-360, 0, 360):
                h0 = ha + shift
                low, high = max(h0, -state.hmax[i]), min(h0 + span, state.hmax[i])
                if high <= low:
                    continue
                duration = (high - low) * 86400 / 360.98564736629
                best_ha = max(low, min(high, 0))
                alt, _ = radec_to_altaz(state.ra[i], state.dec[i], state.ra[i] + best_ha, state.lat)
                lunar = lunar_factor(self.moons[k], state.ra[i], state.dec[i], state.scoring.lunar_model)
                quality = state.scoring.quality_model(alt, lunar)
                # The 0.65 factor is the existing conservative public-quality
                # prior.  It is not a hidden-weather forecast; it merely keeps
                # a target that needs more than the legal exposure cap out of
                # the "safe window" list.
                rate = state.flux[i] * quality * 0.65 / state.scoring.f0t0
                needed = (state.scoring.required_threshold / rate
                          if rate > 0 else float("inf"))
                legal = min(duration, state.max_exposure)
                feasible = (duration >= state.min_exposure and
                            legal >= max(state.min_exposure, needed))
                if feasible:
                    opportunity = True
                    best_duration = min(best_duration or legal,
                                        max(state.min_exposure, needed))
            if opportunity:
                feasible_nights.append(k)
                durations[k] = best_duration
            opportunities.append(int(opportunity))
        suffix = [0] * (len(opportunities) + 1)
        for k in range(len(opportunities) - 1, -1, -1):
            suffix[k] = suffix[k + 1] + opportunities[k]
        self.suffix[i] = suffix
        self.window_nights[i] = tuple(feasible_nights)
        self.window_durations[i] = durations

    def ensure(self, i, force=False):
        """Build one target's public window cache when the caller needs it."""
        if i in self.suffix:
            return True
        if force or self.builds_left > 0:
            if not force:
                self.builds_left -= 1
            self._build(i)
            return True
        return False

    def deadline_night(self, i):
        """Last geometrically and quality-feasible night, with a safe fallback."""
        if i in self.window_nights:
            nights = self.window_nights[i]
            return nights[-1] if nights else -1
        return self.state.last_night[i]

    def rank(self, i, night_index):
        """Return an urgency key: fewer remaining nights sort first."""
        deadline = self.deadline_night(i)
        remaining = deadline - night_index if deadline >= night_index else -1
        return (remaining if remaining >= 0 else -1, self.state.first_night[i], i)

    def urgency(self, i, night_index):
        """A finite hard-priority score for an incomplete REQUIRED target.

        This is deliberately a scheduling score, not a replacement for the
        platform's final penalty.  Targets on their final feasible night get
        a much larger score than ordinary science targets; an already missed
        geometric window remains visible so the caller can attempt recovery.
        """
        state = self.state
        if not state.required[i] or state.factor[i] >= state.scoring.required_threshold:
            return 0.0
        # Reuse the bounded cache populated by ``value``.  For a target whose
        # detailed quality window has not been admitted yet, deadline_night()
        # falls back to the cheap geometry-only last visible night instead of
        # forcing a full-season scan inside every coarse candidate pass.
        self.ensure(i)
        deadline = self.deadline_night(i)
        if deadline < 0:
            return 0.0
        penalty = max(1.0, state.scoring.required_penalty)
        if deadline <= night_index:
            return penalty * 16.0
        # Count future public geometry/quality opportunities, not merely
        # calendar nights.  Six, 0.25 and 11.75 are tuning knobs: keep the
        # far-future hint small while concentrating priority near the final
        # feasible opportunities.
        future = (self.suffix[i][min(len(state.nights), night_index + 1)]
                  if i in self.suffix else
                  max(0, state.last_night[i] - night_index))
        closeness = max(0.0, min(1.0, (6.0 - future) / 6.0))
        return penalty * (0.25 + 11.75 * closeness)

    def value(self, i, night_index):
        state = self.state
        if not state.required[i] or state.factor[i] >= state.scoring.required_threshold:
            return 0.0
        self.ensure(i)
        future = (self.suffix[i][min(len(state.nights), night_index + 1)] if i in self.suffix else
                  max(0, state.last_night[i] - night_index))
        # Approximate deferral loss under finite capacity and unknown weather,
        # never a calibrated probability or a hidden-event forecast.
        miss_defer = 0.35 + 0.65 / (1 + 0.08 * future)
        # Make the last one or two feasible nights a genuine scheduling
        # obligation.  The cap is intentionally finite so a malformed or
        # already-expired catalogue row cannot poison all other plans.
        urgency = 1.0 + 2.5 / (1.0 + max(0, future))
        value = max(0.0, state.scoring.required_penalty) * miss_defer * urgency
        return min(state.scoring.required_penalty, value * 1.1) if state.advice_priority == "required" else value
