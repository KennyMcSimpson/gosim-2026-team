"""Survey state: the target catalogue, learned sky-quality scale, and per-target
progress. Built once from `initialize`, then updated from every `decision_request`'s
messages and `last_result`. Holds no hidden data -- only what the public protocol
hands us, plus what we infer from our own hits (never from a file).

Mirrors the structure-of-parallel-arrays design (ids/ra/dec/flux/weight/required/...,
all indexed by the same integer i) used by this project's companion TypeScript example,
so both examples solve the same problem the same way and can be compared directly.
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections import deque
from typing import NamedTuple, Optional

from .geometry import FiberGrid, max_hour_angle_deg, parse_utc, wrap180
from .scoring import ScoringModel

ALT_MARGIN_DEG = 0.6
SKY_MEMORY_HOURS = 2.0
# Quality and fault learning is exposure based.  A 100-fibre exposure is one
# observation of the sky, rather than one hundred independent weather votes.
RECENT_ACTIONS = 6
MIN_FAULT_ACTIONS = 4
# Fault evidence is deliberately exposure-level and needs enough observations
# on both sides of a possible step change before it can trigger a report.
RECENT_SAMPLES = 60
EARLIER_SAMPLES = 60
SIDEREAL_DEG_PER_SECOND = 360.98564736629 / 86400.0


class PendingPrediction(NamedTuple):
    model: float          # lunar/airmass quality model used at planning time
    band_model: float      # model adjusted by learned instrument scale
    alt: float
    az: float
    clean: bool            # true when no all-sky notice / directional block applied at plan time


class FaultEvidence(NamedTuple):
    recent_median: float
    earlier_median: float
    drop: float
    recent_samples: int
    recent_nights: int
    earlier_samples: int
    dark_checks: int
    dark_matched: int
    recent_directions: int = 0


class ExposureRecord(NamedTuple):
    action_index: int
    target_id: str
    factor: float             # conservative lower bound when multiplier is unobservable
    score: float              # actual feedback science score
    start: object
    end: object
    direction: int


def _mod(a: float, n: float) -> float:
    m = a % n
    return m + n if m < 0 else m


class SurveyState:
    def __init__(self, init_payload: dict):
        site = init_payload["site"]
        survey = init_payload["survey"]
        instrument = init_payload["instrument"]
        limits = init_payload.get("limits", {})

        self.lat = float(site["latitude_deg"])
        self.lon = float(site["longitude_deg"])
        self.min_alt = float(site.get("minimum_altitude_deg", 30.0))
        self.sun_altitude_limit_deg = float(site.get("sun_altitude_limit_deg", -18.0))

        self.survey_start = parse_utc(survey["start_utc"])
        self.survey_end = parse_utc(survey["end_utc"])
        self.slot_seconds = int(survey.get("slot_seconds", 900))
        self.nights = [(parse_utc(n["observing_start_utc"]), parse_utc(n["observing_end_utc"]))
                       for n in survey.get("nights", [])]

        self.fiber_grid = FiberGrid(instrument)
        exposure = instrument.get("exposure", {})
        self.min_exposure = int(exposure.get("min_duration_seconds", 60))
        self.max_exposure = int(exposure.get("max_duration_seconds", 3600))

        self.scoring = ScoringModel(init_payload.get("scoring", {}), site)

        reporting = init_payload.get("scoring", {}).get("reporting", {})
        self.max_consecutive_reports = int(reporting.get("max_consecutive_reports", limits.get("max_consecutive_reports", 32)))
        self.false_report_free_allowance = int(reporting.get("false_report_free_allowance", 0))
        self.response_max_bytes = int(limits.get("response_max_bytes", 524288))

        # Parallel arrays, one slot per target, in catalogue order.
        self.ids: list[str] = []
        self.ra: list[float] = []
        self.dec: list[float] = []
        self.flux: list[float] = []
        self.weight: list[float] = []
        self.required: list[bool] = []
        self.index_of: dict[str, int] = {}

        columns = init_payload.get("targets", {}).get("columns", [])
        col = {name: idx for idx, name in enumerate(columns)}
        for row in init_payload.get("targets", {}).get("rows", []):
            target_id = str(row[col["target_id"]])
            self.index_of[target_id] = len(self.ids)
            self.ids.append(target_id)
            self.ra.append(float(row[col["ra_deg"]]))
            self.dec.append(float(row[col["dec_deg"]]))
            self.flux.append(float(row[col["feature_flux"]]))
            self.weight.append(float(row[col["science_weight"]]))
            self.required.append(bool(row[col["required"]]))

        n = len(self.ids)
        self.hmax = [max_hour_angle_deg(self.dec[i], self.lat, self.min_alt + ALT_MARGIN_DEG) for i in range(n)]
        self.factor = [0.0] * n
        self.best_score = [0.0] * n
        self.progress_version = 0
        width = self.scoring.uniformity_band_width_deg
        self.ra_band = [int(ra // width) if width > 0 else 0 for ra in self.ra]
        self.band_totals: dict[int, int] = {}
        self.band_observed: dict[int, int] = {}
        for band in self.ra_band:
            self.band_totals[band] = self.band_totals.get(band, 0) + 1
        self.misses = [0] * n
        self.attempts = [0] * n
        self.active = [i for i in range(n) if self.hmax[i] > 0.0]

        self._cells: dict[int, list[tuple[float, int]]] = {}
        self._cell_keys: dict[int, list[float]] = {}
        self._build_index()
        self.first_night, self.last_night = self._build_windows()

        self.scale = 1.0
        self.prior_scale = 1.0
        self._samples: deque = deque(maxlen=24)           # (hours, ratio)
        self._all_ratios: deque = deque(maxlen=400)        # ratio
        self.direction_ratios = {b: deque(maxlen=64) for b in range(8)}
        self.validity_actions: dict[int, int] = {}       # action index -> compass bucket
        self.invalidated_actions: set[int] = set()
        self.direction_actions = [0] * 8
        self.direction_invalidated = [0] * 8
        self.clean_history: list[tuple[float, int, float]] = []  # (hours, night, ratio)
        # Direction evidence is kept separately so legacy three-tuples in
        # synthetic callers remain valid.  Each entry corresponds to one
        # exposure-level quality vote and contains the clean compass buckets
        # represented by that exposure.
        self.clean_direction_history: list[tuple[float, frozenset[int]]] = []
        self.pending_night = -1
        # Score-only band evidence.  Entries are saturated matched/mismatch
        # classifications; instrument efficiency is deliberately absent.
        self._band_checks: deque = deque(maxlen=64)        # (program, status, model), one per exposure
        self.band_evidence = {"matched": 0, "mismatch": 0, "ambiguous": 0}
        self.force_program: Optional[str] = None
        self.pending: dict[str, PendingPrediction] = {}
        self.pending_program = "BACKUP"
        self.pending_duration = 0
        self.blocked: list[tuple[float, float]] = []       # (az, alt) where a hit scored zero
        self.notices: set[str] = set()                      # "kind|direction"
        self.terrain: set[str] = set()
        self.extra_avoid: set[str] = set()
        self.duration_scale = 1.0
        self.advice_priority = "balanced"
        self.advice_risk = "balanced"
        self.hit_rate = 1.0
        self.fast_level = 0

        # Separate science scores and conservative completion-factor lower bounds.
        # Only surviving actual exposures remain after a Hard-mode rollback.
        self.ledger: list[ExposureRecord] = []
        self.pending_action_index: Optional[int] = None
        self.pending_start = None
        self.pending_end = None

        # Report feedback is public state.  Both report_result messages and
        # the next last_result can carry the same record, so signatures are
        # deduplicated here before changing free allowances or quality memory.
        self.false_reports_since_correct = 0
        self.report_free_remaining = self.false_report_free_allowance
        self.report_feedback_seen: set[tuple] = set()
        self.last_report_feedback_signature = None
        self.last_report_feedback_hours = float("-inf")
        self.report_cooldown_until_hours = float("-inf")
        # ``last_result`` has no report id in the public v4 payload, while the
        # corresponding report_result message normally carries issued_at_utc.
        # Keep a one-turn alias so the two views of one report can be joined
        # without collapsing two later reports that happen to have equal deltas.
        self._pending_report_aliases: dict[tuple, tuple] = {}

    # -- spatial index -------------------------------------------------------

    def _build_index(self) -> None:
        for i in self.active:
            key = math.floor(self.dec[i])
            self._cells.setdefault(key, []).append((self.ra[i], i))
        for band in self._cells.values():
            band.sort(key=lambda pair: pair[0])
        self._cell_keys = {key: [ra for ra, _ in band] for key, band in self._cells.items()}

    def neighbours(self, ra: float, dec: float, radius: float):
        """Indices within `radius` degrees of (ra, dec), using the 1-degree declination-band index."""
        found: list[int] = []
        cos_dec = max(0.05, math.cos(math.radians(min(89.0, abs(dec) + radius))))
        width = radius / cos_dec
        lo_key, hi_key = math.floor(dec - radius), math.floor(dec + radius)
        for key in range(lo_key, hi_key + 1):
            band = self._cells.get(key)
            if not band:
                continue
            spans: list[tuple[float, float]]
            lo, hi = ra - width, ra + width
            if lo < 0:
                spans = [(0.0, hi), (lo + 360.0, 360.0)]
            elif hi >= 360:
                spans = [(lo, 360.0), (0.0, hi - 360.0)]
            else:
                spans = [(lo, hi)]
            keys = self._cell_keys[key]
            for low, high in spans:
                start = bisect.bisect_left(keys, low)
                end = bisect.bisect_right(keys, high)
                for k in range(start, end):
                    found.append(band[k][1])
        return found

    def _build_windows(self):
        """First/last night index with enough visibility for one legal exposure."""
        need = self.min_exposure * SIDEREAL_DEG_PER_SECOND
        spans = []
        for start, end in self.nights:
            from .geometry import local_sidereal_deg
            l0 = local_sidereal_deg(start, self.lon)
            span = (end - start).total_seconds() * SIDEREAL_DEG_PER_SECOND
            spans.append((l0, span))
        n = len(self.ra)
        first_night = [len(self.nights)] * n
        last_night = [-1] * n
        for i in self.active:
            h = self.hmax[i]
            for k, (l0, span) in enumerate(spans):
                if h >= 180.0:
                    overlap = span
                else:
                    a = _mod(self.ra[i] - h - l0, 360.0)
                    overlap = max(0.0, min(span, a + 2 * h) - a) + max(0.0, min(span, a - 360.0 + 2 * h))
                if overlap >= need:
                    if first_night[i] > k:
                        first_night[i] = k
                    last_night[i] = k
        return first_night, last_night

    # -- messages and results -------------------------------------------------

    @staticmethod
    def _report_feedback_base(record: dict) -> tuple:
        """Return hashable outcome fields shared by both report payload forms."""
        correct = bool(record.get("correct", record.get("repaired", False)))
        repaired = bool(record.get("repaired", correct))
        raw_delta = record.get("score_delta")
        try:
            delta = round(float(raw_delta), 9) if raw_delta is not None else None
        except (TypeError, ValueError):
            delta = str(raw_delta)
        return correct, repaired, delta

    def _report_feedback_token(self, record: dict, base: tuple) -> Optional[tuple]:
        """Use the strongest public event identifier available for a report."""
        for field in ("report_id", "decision_id", "issued_at_utc"):
            value = record.get(field)
            if value is not None:
                return (field, str(value), *base)
        return None

    def _report_message_hours(self, record: dict) -> float:
        issued = record.get("issued_at_utc")
        if issued is not None:
            try:
                return (parse_utc(str(issued)) - self.survey_start).total_seconds() / 3600.0
            except (TypeError, ValueError, OverflowError):
                pass
        return self.last_report_feedback_hours

    def _apply_report_feedback(self, record: dict, hours: float, signature: tuple) -> bool:
        """Apply one previously unseen report outcome and return whether applied."""
        if signature in self.report_feedback_seen:
            return False
        self.report_feedback_seen.add(signature)
        base = self._report_feedback_base(record)
        correct = base[0]
        self.last_report_feedback_signature = signature
        self.last_report_feedback_hours = hours
        self.report_cooldown_until_hours = hours
        if correct:
            # A confirmed repair starts a fresh false-report allowance and a
            # fresh quality baseline for the next fault episode.
            self.false_reports_since_correct = 0
            self.report_free_remaining = self.false_report_free_allowance
            self.forget_quality_history()
        else:
            self.false_reports_since_correct += 1
            self.report_free_remaining = max(
                0, self.false_report_free_allowance - self.false_reports_since_correct
            )
        return True

    def _on_report_feedback(self, record: dict, hours: float, source: str) -> bool:
        """Ingest a report result from either a result or message payload.

        The runner exposes one report twice.  An untagged ``last_result`` is
        applied immediately and retained as a one-turn alias; a following
        ``report_result`` with the same outcome consumes that alias instead of
        applying the allowance/quality transition a second time.
        """
        base = self._report_feedback_base(record)
        token = self._report_feedback_token(record, base)
        if token is not None:
            alias = self._pending_report_aliases.pop(base, None)
            if alias is not None:
                # Mark the explicit form as seen so a replayed message is also
                # harmless, while retaining the state transition done by the
                # untagged last_result.
                self.report_feedback_seen.add(token)
                self.last_report_feedback_signature = token
                return False
            return self._apply_report_feedback(record, hours, token)

        if source == "last_result":
            # Some protocol adapters deliver the explicit message before the
            # matching last_result.  In that order the most recent explicit
            # token is the same report when it is close in simulated time.
            previous = self.last_report_feedback_signature
            if isinstance(previous, tuple) and len(previous) >= 3 and tuple(previous[-3:]) == base:
                if previous[0] in ("report_id", "decision_id", "issued_at_utc"):
                    if abs(hours - self.last_report_feedback_hours) <= 2.0:
                        return False
                elif previous[0] == "last_result" and len(previous) >= 5 and previous[1] == round(float(hours), 9):
                    return False
            # Include the decision time because a later report can legitimately
            # have identical correct/repaired/delta fields.
            signature = ("last_result", round(float(hours), 9), *base)
            applied = self._apply_report_feedback(record, hours, signature)
            if applied:
                self._pending_report_aliases[base] = signature
            return applied

        signature = ("message", round(float(hours), 9), *base)
        return self._apply_report_feedback(record, hours, signature)

    def on_messages(self, messages: list[dict], latest_bulletin: Optional[dict]) -> None:
        # Aliases are only valid for the current decision: in the official
        # runner this method follows on_result in the same turn.
        for message in messages:
            if message.get("record_type") == "bulletin" and message.get("initial"):
                for notice in message.get("notices", []):
                    if notice.get("event_kind") == "terrain_obstruction":
                        self.terrain.add(notice.get("direction"))
            elif message.get("record_type") == "state_resync":
                self._resync(message)
            elif message.get("record_type") == "report_result":
                self._on_report_feedback(message, self._report_message_hours(message), "message")
        notices = (latest_bulletin or {}).get("notices", [])
        self.notices = {f"{n.get('event_kind')}|{n.get('direction')}" for n in notices
                        if n.get("event_kind") != "terrain_obstruction"}
        self._pending_report_aliases.clear()

    def _resync(self, message: dict) -> None:
        """Discard retracted exposures and rebuild separate science/factor maxima.

        Science uses the authoritative best_scores. Completion uses surviving
        conservative lower bounds: score-only feedback does not uniquely expose
        factor when the program multiplier is ambiguous, and is rounded to 6 dp.
        Missing history falls back to score divided by the largest multiplier.
        """
        window = message.get("invalidated_window") or {}
        start, end = window.get("action_index_start"), window.get("action_index_end_exclusive")
        if start is not None and end is not None:
            self.ledger = [entry for entry in self.ledger if not (start <= entry[0] < end)]
        else:
            self.ledger = []  # no window given: nothing in the ledger can be trusted
        exact: dict[str, float] = {}
        scores: dict[str, float] = {}
        for entry in self.ledger:
            target_id, factor = entry.target_id, entry.factor
            if factor > exact.get(target_id, 0.0):
                exact[target_id] = factor
            scores[target_id] = max(scores.get(target_id, 0.0), entry.score)

        best_scores = message.get("best_scores") or []
        best: dict[str, float] = {}
        if best_scores and isinstance(best_scores[0], dict):
            for row in best_scores:
                best[row.get("target_id")] = float(row.get("best_score", 0.0))
        else:
            for target_id, score in zip(message.get("observed_target_ids", []), best_scores):
                best[target_id] = float(score)
        top_multiplier = max(self.scoring.mismatch_multiplier, *self.scoring.program_multipliers.values())

        for i in range(len(self.ids)):
            target_id = self.ids[i]
            self.best_score[i] = best.get(target_id, scores.get(target_id, 0.0))
            if target_id in exact:
                self.factor[i] = exact[target_id]
                continue
            score = best.get(target_id, 0.0)
            self.factor[i] = min(1.0, max(0, score - 0.0000005) / (self.weight[i] * top_multiplier)) if score > 0 and self.weight[i] > 0 else 0.0
        if start is not None and end is not None:
            self.invalidated_actions.update(k for k in self.validity_actions if start <= k < end)
        self.direction_invalidated = [0] * 8
        for k in self.invalidated_actions:
            self.direction_invalidated[self.validity_actions[k]] += 1
        self.band_observed = {}
        for i, f in enumerate(self.factor):
            if f >= self.scoring.uniformity_threshold:
                b = self.ra_band[i]
                self.band_observed[b] = self.band_observed.get(b, 0) + 1
        self.progress_version += 1
        self.active = [i for i in range(len(self.ids)) if self.hmax[i] > 0.0]
        self.misses = [0] * len(self.ids)
        self.attempts = [0] * len(self.ids)
        # Retracted observations must not remain in quality/fault learning either.
        self.forget_quality_history()
        for samples in self.direction_ratios.values():
            samples.clear()
        self.pending.clear()
        self.pending_action_index = None

    def site_closed(self) -> bool:
        for key in self.notices:
            kind, _, direction = key.partition("|")
            if kind in ("rain", "storm") and direction == "ALL":
                return True
        return False

    def all_sky_notice(self) -> bool:
        return any(key.partition("|")[2] == "ALL" for key in self.notices)

    def on_result(self, last_result: Optional[dict], hours: float) -> None:
        action_index = self.pending_action_index
        self.pending_action_index = None
        if last_result and last_result.get("action") == "report":
            self._on_report_feedback(last_result, hours, "last_result")
        if not last_result or last_result.get("action") != "observe" or not self.pending:
            self.pending.clear()
            return
        hits = {h.get("target_id"): float(h.get("score", 0.0)) for h in last_result.get("hits", [])}
        any_positive = any(score > 0 for score in hits.values())
        scoring = self.scoring
        multipliers = scoring.program_multipliers
        mismatch = scoring.mismatch_multiplier
        declared_multiplier = multipliers.get(self.pending_program, 1.0)
        f0t0 = scoring.f0t0
        # A single exposure is one sky-quality observation.  Collect target
        # level ratios while updating progress, then add one robust aggregate
        # to each quality/fault history below.
        quality_samples: list[tuple[float, int, bool]] = []
        clean_quality_samples: list[tuple[float, int]] = []
        band_outcomes: list[tuple[bool, float]] = []
        if action_index is not None and self.pending:
            prediction = next(iter(self.pending.values()))
            bucket = int((prediction.az + 22.5) % 360 / 45)
            if action_index not in self.validity_actions:
                self.direction_actions[bucket] += 1
            self.validity_actions[action_index] = bucket

        for target_id, prediction in self.pending.items():
            i = self.index_of.get(target_id)
            if i is None:
                continue
            if target_id not in hits:
                self.misses[i] += 1
                continue
            score = hits[target_id]
            if score <= 0.0:
                if any_positive:
                    self.blocked.append((prediction.az, prediction.alt))
                continue
            self.misses[i] = 0
            weight = self.weight[i] if self.weight[i] > 0 else 1e-9
            multiplier_seen = score / weight
            if prediction.clean:
                if abs(multiplier_seen - declared_multiplier) < 2e-4:
                    band_outcomes.append((True, prediction.model))
                elif abs(multiplier_seen - mismatch) < 2e-4:
                    band_outcomes.append((False, prediction.model))
            factor_if_match = score / (weight * declared_multiplier) if declared_multiplier > 0 else 0.0
            factor_if_miss = score / (weight * mismatch) if mismatch > 0 else 0.0
            ratio_match = (factor_if_match * f0t0) / (self.flux[i] * self.pending_duration * prediction.model) \
                if self.flux[i] > 0 and self.pending_duration > 0 and prediction.model > 0 else 0.0
            band = scoring.program_band(ratio_match * prediction.band_model)
            matched = band == self.pending_program
            factor = factor_if_match if matched else factor_if_miss
            factor = min(1.0, factor)
            # Feedback exposes score only.  Usually the hidden completion
            # factor and the program multiplier cannot be separated.  A hit
            # whose score/weight is within the public six-decimal rounding
            # tolerance of a known multiplier is the useful exception: it is
            # saturated (factor ~= 1), so that multiplier identifies the
            # denominator even when the declared program was mismatched.
            # Otherwise retain the conservative lower bound from the largest
            # multiplier that could have produced the score.
            lower_bound_multiplier = max(declared_multiplier, mismatch, 1e-9)
            if weight > 0.0:
                public_multipliers = [mismatch]
                public_multipliers.extend(multipliers.values())
                public_multipliers = [float(value) for value in public_multipliers if float(value) > 0.0]
                if public_multipliers:
                    nearest_multiplier = min(
                        public_multipliers,
                        key=lambda value: abs(multiplier_seen - value),
                    )
                    if abs(multiplier_seen - nearest_multiplier) < 2e-4:
                        lower_bound_multiplier = nearest_multiplier
            factor_lower = min(1.0, max(0.0, score - 0.0000005) /
                               (weight * lower_bound_multiplier))
            old = self.factor[i]
            self.factor[i] = max(old, factor_lower)
            self.best_score[i] = max(self.best_score[i], score)
            if old < scoring.uniformity_threshold <= self.factor[i]:
                b = self.ra_band[i]
                self.band_observed[b] = self.band_observed.get(b, 0) + 1
            self.progress_version += 1
            if action_index is not None:
                self.ledger.append(ExposureRecord(action_index, target_id, factor_lower, score,
                                                 self.pending_start, self.pending_end,
                                                 int((prediction.az + 22.5) % 360 / 45)))
            if self.required[i] and self.factor[i] < scoring.required_threshold:
                self.attempts[i] += 1
            if factor < 0.97 and self.flux[i] > 0 and self.pending_duration > 0 and prediction.model > 0:
                ratio = (factor * f0t0) / (self.flux[i] * self.pending_duration * prediction.model)
                bucket = int((prediction.az + 22.5) % 360 / 45)
                quality_samples.append((ratio, bucket, prediction.clean))
                if prediction.clean:
                    clean_quality_samples.append((ratio, bucket))
        if quality_samples:
            aggregate_ratio = float(statistics.median(ratio for ratio, _, _ in quality_samples))
            self._samples.append((hours, aggregate_ratio))
            self._all_ratios.append(aggregate_ratio)
            for bucket in {bucket for _, bucket, _ in quality_samples}:
                self.direction_ratios[bucket].append(aggregate_ratio)
        if clean_quality_samples:
            clean_ratio = float(statistics.median(ratio for ratio, _ in clean_quality_samples))
            clean_buckets = frozenset(bucket for _, bucket in clean_quality_samples)
            self.clean_history.append((hours, self.pending_night, clean_ratio))
            self.clean_direction_history.append((hours, clean_buckets))
        if band_outcomes:
            matched = sum(1 for is_match, _ in band_outcomes if is_match)
            status = matched * 2 > len(band_outcomes)
            model = float(statistics.median(model for _, model in band_outcomes))
            self._band_checks.append((self.pending_program, status, model))
            key = ("matched" if matched * 2 > len(band_outcomes)
                   else "mismatch" if matched * 2 < len(band_outcomes)
                   else "ambiguous")
            self.band_evidence[key] += 1
        self.pending.clear()
        self.update_scale(hours)

    def quality_scales(self, az: float) -> tuple[float, float, float]:
        """Shrunk 20/50/80 percentiles from valid, non-saturated positive hits.

        Misses and retractions are separate evidence, never zero-quality samples.
        These are empirical planning estimates, not measured weather forecasts.
        """
        bucket = int((az + 22.5) % 360 / 45)
        local = sorted(self.direction_ratios[bucket])
        global_samples = sorted(self._all_ratios)
        median = max(0.05, self.scale)
        if len(global_samples) < 8:
            return 0.7 * median, median, 1.1 * median
        def quantile(values, q):
            return values[min(len(values) - 1, int(q * (len(values) - 1)))]
        shrink = min(0.7, len(local) / (len(local) + 16.0)) if len(local) >= 8 else 0.0
        scales = []
        for q in (0.2, 0.5, 0.8):
            base = quantile(global_samples, q)
            directional = quantile(local, q) if shrink else base
            # Recent global level follows weather changes; direction retains
            # relative evidence but cannot override the whole-sky recent level.
            adjusted = ((1 - shrink) * base + shrink * directional) * median / max(0.05, self.prior_scale)
            scales.append(max(0.05, min(3.0, adjusted)))
        return tuple(scales)

    def invalidation_probability(self, az: float) -> float:
        """Only explicit state_resync windows update validity risk."""
        total = len(self.validity_actions)
        global_risk = len(self.invalidated_actions) / (200.0 + total)
        bucket = int((az + 22.5) % 360 / 45)
        local = self.direction_actions[bucket]
        if not local:
            return global_risk
        bad = self.direction_invalidated[bucket]
        return min(0.6, (bad + 40 * global_risk) / (local + 40))

    def has_recent_sample(self, hours: float) -> bool:
        return any(when >= hours - SKY_MEMORY_HOURS for when, _ in self._samples)

    def update_scale(self, hours: float) -> None:
        if len(self._all_ratios) >= 8:
            ordered = sorted(self._all_ratios)
            self.prior_scale = ordered[len(ordered) // 2]
        recent = sorted(ratio for when, ratio in self._samples if when >= hours - SKY_MEMORY_HOURS)
        self.scale = max(0.05, recent[len(recent) // 2]) if len(recent) >= 4 else self.prior_scale

    # -- fault diagnostics ------------------------------------------------------

    def fault_evidence(self) -> Optional[FaultEvidence]:
        history = self.clean_history
        if len(history) < RECENT_SAMPLES + EARLIER_SAMPLES:
            return None
        recent = history[-RECENT_SAMPLES:]
        earlier = history[:-RECENT_SAMPLES]
        span = recent[-1][0] - recent[0][0]
        nights = len({night for _, night, _ in recent})
        if span < 4.0 or nights < 2:
            return None
        recent_sorted = sorted(r for _, _, r in recent)
        earlier_sorted = sorted(r for _, _, r in earlier)
        recent_median = recent_sorted[len(recent_sorted) // 2]
        earlier_median = earlier_sorted[len(earlier_sorted) // 2]
        dark_line = self.scoring.program_bands["DARK"] * 1.3
        dark = [c for c in list(self._band_checks)[-16:]
                if c[0] == "DARK" and (c[2] * earlier_median) / 0.95 >= dark_line]
        return FaultEvidence(
            recent_median=round(recent_median, 3),
            earlier_median=round(earlier_median, 3),
            drop=round(recent_median / max(1e-9, earlier_median), 3),
            recent_samples=len(recent),
            recent_nights=nights,
            earlier_samples=len(earlier),
            dark_checks=len(dark),
            dark_matched=sum(1 for c in dark if c[1]),
        )

    def forget_quality_history(self) -> None:
        self.clean_history = []
        self.clean_direction_history = []
        self._band_checks.clear()
        for key in self.band_evidence:
            self.band_evidence[key] = 0
        self._samples.clear()
        self._all_ratios.clear()
        self.prior_scale = 1.0
        self.scale = 1.0
        for samples in self.direction_ratios.values():
            samples.clear()

    # -- night lookup -------------------------------------------------------------

    def current_night(self, now):
        for index, (start, end) in enumerate(self.nights):
            if start <= now < end:
                return index, start, end
        return None

    def next_night_start(self, now):
        for start, _end in self.nights:
            if start > now:
                return start
        return None
