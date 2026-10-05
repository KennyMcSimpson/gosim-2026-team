/**
 * Agent state: the target catalogue, learned sky-quality estimate, and per-target progress.
 * Built once from `initialize`, then updated from each decision_request's messages and last_result.
 * Holds no hidden data -- only what the public protocol hands us plus what we infer from our own hits.
 */
import type {
  BulletinMessage,
  InitializePayload,
  LastResult,
  Notice,
  PlatformMessage,
  StateResyncMessage,
} from "./protocol";
import { SIDEREAL_DEG_PER_SECOND, localSiderealDeg, maxHourAngleDeg, parseUtc, wrap180 } from "./skymath";

const ALT_MARGIN_DEG = 0.6;
export const SKY_MEMORY_HOURS = 2.0;
const RECENT_SAMPLES = 60;
const EARLIER_SAMPLES = 60;

export interface PendingPrediction {
  model: number; // lunar/airmass quality model used at planning time
  bandModel: number; // model / 0.95, used for program-band back-estimation
  alt: number;
  az: number;
  clean: boolean; // true when no all-sky notice and no directional block applied at plan time
}

export interface FaultEvidence {
  recent_median: number;
  earlier_median: number;
  drop: number;
  recent_samples: number;
  recent_nights: number;
  earlier_samples: number;
  dark_checks: number;
  dark_matched: number;
}

/** Fixed-size ring buffer of (hours, ratio) or (hours, night, ratio) samples. */
class Ring<T> {
  private buf: T[] = [];
  constructor(private readonly cap: number) {}
  push(item: T): void {
    this.buf.push(item);
    if (this.buf.length > this.cap) this.buf.shift();
  }
  get length(): number {
    return this.buf.length;
  }
  toArray(): T[] {
    return this.buf;
  }
  clear(): void {
    this.buf = [];
  }
}

export class AgentState {
  readonly lat: number;
  readonly lon: number;
  readonly minAlt: number;
  readonly nights: { start: Date; end: Date }[];
  readonly surveyEnd: Date;
  readonly slotSeconds: number;
  readonly minExposure: number;
  readonly maxExposure: number;
  readonly payload: InitializePayload;

  readonly ids: string[] = [];
  readonly ra: number[] = [];
  readonly dec: number[] = [];
  readonly flux: number[] = [];
  readonly weight: number[] = [];
  readonly required: boolean[] = [];
  readonly hmax: number[] = [];
  readonly indexOf = new Map<string, number>();
  readonly firstNight: number[] = [];
  readonly lastNight: number[] = [];

  factor: number[] = [];
  misses: number[] = [];
  attempts: number[] = [];
  active: number[] = [];

  // dec-band spatial index: integer floor(dec) -> sorted [ra, index][]
  private cells = new Map<number, [number, number][]>();

  scale = 1.0;
  priorScale = 1.0;
  private samples = new Ring<[number, number]>(24); // (hours, ratio)
  private allRatios = new Ring<number>(400);
  cleanHistory: [number, number, number][] = []; // (hours, night, ratio)
  pendingNight = -1;
  private bandChecks = new Ring<[string, boolean, number]>(60);
  forceProgram: "DARK" | "BRIGHT" | "BACKUP" | null = null;
  pending = new Map<string, PendingPrediction>();
  pendingProgram: "DARK" | "BRIGHT" | "BACKUP" = "BACKUP";
  pendingDuration = 0;
  blocked: [number, number][] = []; // (az, alt) where a hit scored zero
  notices: Set<string> = new Set(); // "kind|direction"
  terrain: Set<string> = new Set();
  extraAvoid: Set<string> = new Set();
  durationScale = 1.0;
  fastLevel = 0;

  /** Per-observe-action ledger: [observeActionIndex, targetId, factor], mirroring the
   * backend's own BestLedger so a Hard-mode state_resync can be answered exactly (see
   * `resync`) instead of only from the resync message's `best_scores`. */
  ledger: [number, string, number][] = [];
  pendingActionIndex: number | null = null;
  /** `decision_request.payload.observe_action_index` for the decision in progress; set by
   * index.ts at the top of `respond()`, copied into `pendingActionIndex` by `finishPlan`
   * if this decision ends up being an `observe`. */
  currentActionIndex: number | null = null;

  constructor(payload: InitializePayload) {
    this.payload = payload;
    const site = payload.site;
    this.lat = site.latitude_deg;
    this.lon = site.longitude_deg;
    this.minAlt = site.minimum_altitude_deg;
    this.nights = payload.survey.nights.map((n) => ({
      start: parseUtc(n.observing_start_utc),
      end: parseUtc(n.observing_end_utc),
    }));
    this.surveyEnd = parseUtc(payload.survey.end_utc);
    this.slotSeconds = payload.survey.slot_seconds;
    this.minExposure = payload.instrument.exposure.min_duration_seconds;
    this.maxExposure = payload.instrument.exposure.max_duration_seconds;

    const columns = payload.targets.columns;
    const col = (name: string) => columns.indexOf(name);
    const cId = col("target_id");
    const cRa = col("ra_deg");
    const cDec = col("dec_deg");
    const cFlux = col("feature_flux");
    const cWeight = col("science_weight");
    const cRequired = col("required");
    for (const row of payload.targets.rows) {
      const id = String(row[cId]);
      this.ids.push(id);
      this.indexOf.set(id, this.ids.length - 1);
      this.ra.push(Number(row[cRa]));
      this.dec.push(Number(row[cDec]));
      this.flux.push(Number(row[cFlux]));
      this.weight.push(Number(row[cWeight]));
      this.required.push(Boolean(row[cRequired]));
    }
    for (let i = 0; i < this.ids.length; i++) {
      this.hmax.push(maxHourAngleDeg(this.dec[i]!, this.lat, this.minAlt + ALT_MARGIN_DEG));
      this.factor.push(0.0);
      this.misses.push(0);
      this.attempts.push(0);
    }
    this.active = [];
    for (let i = 0; i < this.ids.length; i++) {
      if (this.hmax[i]! > 0.0) this.active.push(i);
    }
    this.buildIndex();
    [this.firstNight, this.lastNight] = this.buildWindows();
  }

  private buildIndex(): void {
    for (const i of this.active) {
      const key = Math.floor(this.dec[i]!);
      let band = this.cells.get(key);
      if (!band) {
        band = [];
        this.cells.set(key, band);
      }
      band.push([this.ra[i]!, i]);
    }
    for (const band of this.cells.values()) band.sort((a, b) => a[0] - b[0]);
  }

  /** Targets within `radius` degrees of (ra, dec), using the 1-degree declination-band index. */
  neighbours(ra: number, dec: number, radius: number): number[] {
    const found: number[] = [];
    const cosDec = Math.max(0.05, Math.cos((Math.min(89.0, Math.abs(dec) + radius) * Math.PI) / 180));
    const width = radius / cosDec;
    const lo = Math.floor(dec - radius);
    const hi = Math.floor(dec + radius);
    for (let key = lo; key <= hi; key++) {
      const band = this.cells.get(key);
      if (!band) continue;
      let spans: [number, number][] = [[ra - width, ra + width]];
      if (spans[0]![0] < 0) {
        spans = [
          [0.0, spans[0]![1]],
          [spans[0]![0] + 360.0, 360.0],
        ];
      } else if (spans[0]![1] >= 360) {
        spans = [
          [spans[0]![0], 360.0],
          [0.0, spans[0]![1] - 360.0],
        ];
      }
      for (const [low, high] of spans) {
        const start = lowerBound(band, low);
        const end = upperBound(band, high);
        for (let k = start; k < end; k++) found.push(band[k]![1]);
      }
    }
    return found;
  }

  /** First/last night index on which each target has at least 20 minutes above the limit. */
  private buildWindows(): [number[], number[]] {
    const need = 20 * 60 * SIDEREAL_DEG_PER_SECOND;
    const spans = this.nights.map((n) => {
      const l0 = localSiderealDeg(n.start, this.lon);
      const span = (n.end.getTime() - n.start.getTime()) / 1000 * SIDEREAL_DEG_PER_SECOND;
      return [l0, span] as [number, number];
    });
    const firstNight = new Array(this.ra.length).fill(this.nights.length);
    const lastNight = new Array(this.ra.length).fill(-1);
    for (const i of this.active) {
      const h = this.hmax[i]!;
      for (let k = 0; k < spans.length; k++) {
        const [l0, span] = spans[k]!;
        let overlap: number;
        if (h >= 180.0) {
          overlap = span;
        } else {
          const a = mod(this.ra[i]! - h - l0, 360.0);
          overlap = Math.max(0.0, Math.min(span, a + 2 * h) - a) + Math.max(0.0, Math.min(span, a - 360.0 + 2 * h));
        }
        if (overlap >= need) {
          if (firstNight[i] > k) firstNight[i] = k;
          lastNight[i] = k;
        }
      }
    }
    return [firstNight, lastNight];
  }

  // --- messages and results -------------------------------------------------------------------

  onMessages(messages: PlatformMessage[], latestBulletin: BulletinMessage | null): void {
    for (const message of messages) {
      if (message.record_type === "bulletin" && message.initial) {
        for (const notice of message.notices) {
          if (notice.event_kind === "terrain_obstruction") this.terrain.add(notice.direction);
        }
      } else if (message.record_type === "state_resync") {
        this.resync(message);
      }
    }
    const notices: Notice[] = latestBulletin?.notices ?? [];
    this.notices = new Set(
      notices.filter((n) => n.event_kind !== "terrain_obstruction").map((n) => `${n.event_kind}|${n.direction}`)
    );
  }

  /**
   * Hard-mode state_resync (participant guide, Appendix A / section 8): a prior window
   * of `observe` actions was invalidated. The message itself only gives `best_scores`
   * (score, not factor) for targets with any surviving valid hit -- the guide is
   * explicit that it does not return each target's completion factor, and that an
   * agent that needs it exactly should combine `invalidated_window` with its own saved
   * valid-exposure history.
   *
   * We can do exactly that: every entry in `this.ledger` already holds the EXACT factor
   * for one past observe action (`onResult` backs it out of the real score the backend
   * returned, the public weight, and whichever of the two public program multipliers it
   * matches -- not an estimate). Dropping the ledger entries inside the invalidated
   * action-index window and taking, per target, the max factor among what is left
   * reproduces the backend's own ledger exactly -- this is a reconstruction, not an
   * approximation from best_score.
   *
   * The only remaining uncertainty: a target with no ledger entry at all (e.g. this
   * process restarted mid-run and lost its in-memory history) falls back to the old
   * best_score/top_multiplier estimate below, same as before this change.
   */
  private resync(message: StateResyncMessage): void {
    const { action_index_start: start, action_index_end_exclusive: end } = message.invalidated_window ?? {};
    if (start !== undefined && end !== undefined) {
      this.ledger = this.ledger.filter(([index]) => !(index >= start && index < end));
    } else {
      this.ledger = []; // no window given: nothing in the ledger can be trusted
    }
    const exact = new Map<string, number>();
    for (const [, targetId, factor] of this.ledger) {
      if (factor > (exact.get(targetId) ?? 0.0)) exact.set(targetId, factor);
    }

    const best = new Map((message.best_scores ?? []).map((row) => [row.target_id, row.best_score]));
    const top = Math.max(...Object.values(this.payload.scoring.program.multipliers));
    for (let i = 0; i < this.ids.length; i++) {
      const targetId = this.ids[i]!;
      const exactFactor = exact.get(targetId);
      if (exactFactor !== undefined) {
        this.factor[i] = exactFactor;
        continue;
      }
      const score = best.get(targetId) ?? 0.0;
      this.factor[i] = score > 0 ? Math.min(1.0, score / (this.weight[i]! * top)) : 0.0;
    }
    this.active = [];
    for (let i = 0; i < this.ids.length; i++) {
      if (this.hmax[i]! > 0.0) this.active.push(i);
    }
    this.pending.clear();
    this.pendingActionIndex = null;
  }

  siteClosed(): boolean {
    for (const key of this.notices) {
      const [kind, direction] = key.split("|");
      if ((kind === "rain" || kind === "storm") && direction === "ALL") return true;
    }
    return false;
  }

  allSkyNotice(): boolean {
    for (const key of this.notices) {
      if (key.split("|")[1] === "ALL") return true;
    }
    return false;
  }

  onResult(result: LastResult, hours: number): void {
    const actionIndex = this.pendingActionIndex;
    this.pendingActionIndex = null;
    if (!result || result.action !== "observe" || this.pending.size === 0) {
      this.pending.clear();
      return;
    }
    const hits = new Map(result.hits.map((h) => [h.target_id, h.score]));
    const anyPositive = [...hits.values()].some((s) => s > 0);
    const scoring = this.payload.scoring;
    const multipliers = scoring.program.multipliers;
    const mismatch = scoring.program.mismatch_multiplier;
    const declared = multipliers[this.pendingProgram];
    const flux0t0 = scoring.flux_zero_point * scoring.exposure_zero_point_seconds;
    for (const [targetId, prediction] of this.pending) {
      const i = this.indexOf.get(targetId);
      if (i === undefined) continue;
      const score = hits.get(targetId);
      if (score === undefined) {
        this.misses[i]!++;
        continue;
      }
      if (score <= 0.0) {
        if (anyPositive) this.blocked.push([prediction.az, prediction.alt]);
        continue;
      }
      const multiplierSeen = score / this.weight[i]!;
      if (prediction.clean) {
        if (Math.abs(multiplierSeen - declared) < 2e-4) {
          this.bandChecks.push([this.pendingProgram, true, prediction.model]);
        } else if (Math.abs(multiplierSeen - mismatch) < 2e-4) {
          this.bandChecks.push([this.pendingProgram, false, prediction.model]);
        }
      }
      const factorIfMatch = score / (this.weight[i]! * declared);
      const factorIfMiss = score / (this.weight[i]! * mismatch);
      const ratioMatch = (factorIfMatch * flux0t0) / (this.flux[i]! * this.pendingDuration * prediction.model);
      const band = this.band(ratioMatch * prediction.bandModel, scoring.program.bands);
      const matched = band === this.pendingProgram;
      const factor = Math.min(1.0, matched ? factorIfMatch : factorIfMiss);
      this.factor[i] = Math.max(this.factor[i]!, factor);
      if (actionIndex !== null) this.ledger.push([actionIndex, targetId, factor]);
      if (this.required[i] && this.factor[i]! < 0.5) this.attempts[i]!++;
      if (factor < 0.97) {
        const ratio = (factor * flux0t0) / (this.flux[i]! * this.pendingDuration * prediction.model);
        this.samples.push([hours, ratio]);
        this.allRatios.push(ratio);
        if (prediction.clean) this.cleanHistory.push([hours, this.pendingNight, ratio]);
      }
    }
    this.pending.clear();
    this.updateScale(hours);
  }

  /** True when at least one quality sample was recorded within the sky-memory window of `hours`. */
  hasRecentSample(hours: number): boolean {
    return this.samples.toArray().some(([when]) => when >= hours - SKY_MEMORY_HOURS);
  }

  updateScale(hours: number): void {
    if (this.allRatios.length >= 8) {
      const ordered = [...this.allRatios.toArray()].sort((a, b) => a - b);
      this.priorScale = ordered[Math.floor(ordered.length / 2)]!;
    }
    const recent = this.samples
      .toArray()
      .filter(([when]) => when >= hours - SKY_MEMORY_HOURS)
      .map(([, ratio]) => ratio)
      .sort((a, b) => a - b);
    this.scale = recent.length >= 4 ? Math.max(0.05, recent[Math.floor(recent.length / 2)]!) : this.priorScale;
  }

  private band(qBand: number, bands: { DARK: number; BRIGHT: number }): "DARK" | "BRIGHT" | "BACKUP" {
    if (qBand >= bands.DARK) return "DARK";
    if (qBand >= bands.BRIGHT) return "BRIGHT";
    return "BACKUP";
  }

  // --- fault diagnostics ------------------------------------------------------------------------

  faultEvidence(): FaultEvidence | null {
    const history = this.cleanHistory;
    if (history.length < RECENT_SAMPLES + EARLIER_SAMPLES) return null;
    const recent = history.slice(history.length - RECENT_SAMPLES);
    const earlier = history.slice(0, history.length - RECENT_SAMPLES);
    const span = recent[recent.length - 1]![0] - recent[0]![0];
    const nights = new Set(recent.map(([, night]) => night)).size;
    if (span < 4.0 || nights < 2) return null;
    const recentMedian = [...recent.map(([, , r]) => r)].sort((a, b) => a - b)[Math.floor(recent.length / 2)]!;
    const earlierMedian = [...earlier.map(([, , r]) => r)].sort((a, b) => a - b)[Math.floor(earlier.length / 2)]!;
    const darkLine = this.payload.scoring.program.bands.DARK * 1.3;
    const dark = this.bandChecks
      .toArray()
      .filter(([program, , model]) => program === "DARK" && (model * earlierMedian) / 0.95 >= darkLine)
      .slice(-16);
    return {
      recent_median: round3(recentMedian),
      earlier_median: round3(earlierMedian),
      drop: round3(recentMedian / Math.max(1e-9, earlierMedian)),
      recent_samples: recent.length,
      recent_nights: nights,
      earlier_samples: earlier.length,
      dark_checks: dark.length,
      dark_matched: dark.filter(([, matched]) => matched).length,
    };
  }

  forgetQualityHistory(): void {
    this.cleanHistory = [];
    this.bandChecks.clear();
    this.samples.clear();
    this.allRatios.clear();
    this.priorScale = 1.0;
  }

  // --- night lookup -----------------------------------------------------------------------------

  currentNight(now: Date): { index: number; start: Date; end: Date } | null {
    for (let k = 0; k < this.nights.length; k++) {
      const { start, end } = this.nights[k]!;
      if (start <= now && now < end) return { index: k, start, end };
    }
    return null;
  }

  nextNightStart(now: Date): Date | null {
    for (const { start } of this.nights) {
      if (start > now) return start;
    }
    return null;
  }
}

function mod(a: number, n: number): number {
  const m = a % n;
  return m < 0 ? m + n : m;
}

function round3(x: number): number {
  return Math.round(x * 1000) / 1000;
}

function lowerBound(band: [number, number][], value: number): number {
  let lo = 0;
  let hi = band.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (band[mid]![0] < value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function upperBound(band: [number, number][], value: number): number {
  let lo = 0;
  let hi = band.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (band[mid]![0] <= value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export { wrap180 };
