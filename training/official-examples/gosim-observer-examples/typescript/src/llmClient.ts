/**
 * LLM advice via an OpenAI-compatible chat-completions endpoint, using Node's built-in `fetch`
 * (no SDK dependency). Defaults to the Kimi Coding Plan endpoint and model; point OPENAI_BASE_URL
 * at any other OpenAI-compatible provider to use that instead.
 *
 *   OPENAI_BASE_URL   endpoint base URL; defaults to https://api.kimi.com/coding/v1
 *                      (use https://api.kimi.ai/coding/v1 for the overseas endpoint)
 *   OPENAI_API_KEY    credential for the endpoint (KIMI_API_KEY also accepted)
 *   OPENAI_MODEL      model id; defaults to "k3"
 *
 * Waiting for the model is not charged to the CPU budget (see clock.ts), but it does use real time,
 * and each card has a 30-minute real-time cap. So model use is bounded by real time: every attempt
 * has a timeout (default 20 s), one question gives up after 60 s in total, no call starts in the last
 * 5 minutes before the cap, and a run makes at most 100 requests. HTTP 429 (rate limit) and 5xx
 * answers, timeouts and network errors are retried with exponential backoff plus random jitter,
 * honouring `Retry-After`. In the hidden final a card's 3 repeats run at the same time on the same
 * key, so 429s are to be expected; the jitter keeps the repeats from retrying in lockstep. If a
 * question still fails, the caller gets `null` and uses its rule-based decision for that one step.
 */
import { log } from "./protocol";

export interface LLMConfig {
  enabled: boolean;
  baseUrl: string;
  apiKey: string;
  model: string;
}

const DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1";
const DEFAULT_MODEL = "k3";
const WALL_RESERVE_SECONDS = 300; // no new model call this close to the real-time cap
const QUESTION_DEADLINE_SECONDS = 60; // one question, all retries and backoff included
const MAX_BACKOFF_SECONDS = 20;

/** 429, 5xx, timeout or network trouble: worth another try after a pause. */
class RetryableError extends Error {
  constructor(message: string, readonly retryAfterSeconds: number | null = null) {
    super(message);
  }
}

const sleep = (seconds: number) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));

function readConfig(): LLMConfig {
  const baseUrl = (process.env.OPENAI_BASE_URL ?? DEFAULT_BASE_URL).trim().replace(/\/+$/, "");
  const apiKey = (process.env.OPENAI_API_KEY ?? process.env.KIMI_API_KEY ?? "").trim();
  const model = (process.env.OPENAI_MODEL ?? "").trim() || DEFAULT_MODEL;
  return { enabled: Boolean(baseUrl && apiKey), baseUrl, apiKey, model };
}

/** Short, user-facing config error, or null when a key is configured. */
export function configError(): string | null {
  return readConfig().apiKey ? null : "missing API key: set OPENAI_API_KEY";
}

export class LLMAdvisor {
  readonly enabled: boolean;
  private readonly config: LLMConfig;
  private readonly timeoutSeconds: number;
  private readonly maxCalls: number;
  private readonly maxAttempts: number;
  calls = 0;

  constructor(timeoutSeconds = 20.0, maxCalls = 100, maxAttempts = 4) {
    this.config = readConfig();
    this.enabled = this.config.enabled;
    this.timeoutSeconds = timeoutSeconds;
    this.maxCalls = maxCalls;
    this.maxAttempts = maxAttempts;
  }

  get status(): string {
    return this.enabled ? "on" : "off";
  }

  /** One HTTP attempt. Throws RetryableError for problems worth retrying, any other error otherwise. */
  private async attempt(system: string, user: string, timeoutSeconds: number): Promise<Record<string, unknown>> {
    let response: Response;
    try {
      response = await fetch(`${this.config.baseUrl}/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.config.apiKey}` },
        body: JSON.stringify({
          model: this.config.model,
          messages: [
            { role: "system", content: system },
            { role: "user", content: user },
          ],
          // No temperature: Kimi Coding Plan models (k3 / kimi-for-coding) reject any value
          // but 1 with HTTP 400, so leave it to the provider's default. Reasoning models
          // spend tokens thinking before the JSON answer, hence the roomy cap.
          max_tokens: 1024,
        }),
        signal: AbortSignal.timeout(timeoutSeconds * 1000),
      });
    } catch (exc) {
      throw new RetryableError(exc instanceof Error ? exc.name : "network error"); // timeout, reset, DNS ...
    }
    if (response.status === 429 || response.status >= 500) {
      const header = response.headers.get("retry-after"); // seconds; an HTTP date falls back to our own backoff
      const retryAfter = header === null || header.trim() === "" ? NaN : Number(header);
      throw new RetryableError(`http ${response.status}`, Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter : null);
    }
    if (!response.ok) throw new Error(`http ${response.status}`); // 400/401/403/404: retrying will not help
    const data = (await response.json()) as { choices?: { message?: { content?: string } }[] };
    const text = data.choices?.[0]?.message?.content ?? "";
    const match = /\{[\s\S]*\}/.exec(text);
    if (!match) throw new RetryableError("no JSON object in model reply");
    return JSON.parse(match[0]) as Record<string, unknown>;
  }

  /**
   * One planning question, answered as one JSON object, or null so the caller's rule-based answer
   * takes over. `wallLeftSeconds` is the real time left before the card's cap
   * (`wallclock.wall_remaining_seconds`).
   */
  private async chat(system: string, user: string, wallLeftSeconds: number): Promise<Record<string, unknown> | null> {
    if (!this.enabled) return null;
    const deadline = Date.now() + Math.min(QUESTION_DEADLINE_SECONDS, wallLeftSeconds - WALL_RESERVE_SECONDS) * 1000;
    for (let attempt = 1; attempt <= this.maxAttempts; attempt++) {
      const timeLeft = (deadline - Date.now()) / 1000;
      if (timeLeft < 2 || this.calls >= this.maxCalls) break;
      this.calls++;
      try {
        return await this.attempt(system, user, Math.min(this.timeoutSeconds, timeLeft));
      } catch (exc) {
        if (!(exc instanceof RetryableError)) {
          log(`llm: call failed (${String(exc)}); not retrying`); // never log the key
          break;
        }
        // Exponential backoff with full jitter (1, 2, 4 ... s, randomised), or the server's Retry-After.
        const pause = Math.min(exc.retryAfterSeconds ?? Math.random() * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS);
        log(`llm: attempt ${attempt}/${this.maxAttempts} failed (${exc.message}); retry in ${pause.toFixed(1)}s`);
        if (attempt === this.maxAttempts || Date.now() + pause * 1000 > deadline - 2000) break;
        await sleep(pause); // waiting: no CPU budget is charged
      }
    }
    log("llm: no answer for this question; using the rule-based decision for this step");
    return null;
  }

  /** Shared parsing for the {avoid_directions, duration_scale} answer shape. */
  private parseAdvice(answer: Record<string, unknown>): { avoidDirections: string[]; durationScale: number } {
    const directions = new Set(["N", "NE", "E", "SE", "S", "SW", "W", "NW"]);
    const avoid = Array.isArray(answer.avoid_directions)
      ? [...new Set(answer.avoid_directions.map((d) => String(d).toUpperCase()).filter((d) => directions.has(d)))].sort()
      : [];
    let scale = 1.0;
    const raw = Number(answer.duration_scale);
    if (Number.isFinite(raw)) scale = Math.min(1.4, Math.max(0.7, raw));
    return { avoidDirections: avoid, durationScale: scale };
  }

  /** -> {avoidDirections, durationScale} or null. One short call per night, from the forecast. */
  async nightPlan(
    nightDate: string,
    forecastNotices: unknown,
    bulletinNotices: unknown,
    wallLeftSeconds: number
  ): Promise<{ avoidDirections: string[]; durationScale: number } | null> {
    const system =
      "You help schedule a telescope survey. Reply with one JSON object only: " +
      '{"avoid_directions": [compass codes among N,NE,E,SE,S,SW,W,NW], "duration_scale": number 0.7-1.4}. ' +
      "Avoid directions with bad weather tonight; use a larger duration_scale when the sky is poor.";
    const user = JSON.stringify({ night: nightDate, forecast_notices_for_tonight: forecastNotices, current_bulletin_notices: bulletinNotices });
    const answer = await this.chat(system, user, wallLeftSeconds);
    return answer ? this.parseAdvice(answer) : null;
  }

  /**
   * -> {avoidDirections, durationScale} or null. A second, separate call each night that reacts to
   * the latest public bulletin and the run's own hit rate so far -- distinct from nightPlan, which
   * only looks at the forecast. Catches weather or performance that changed since nightPlan ran.
   */
  async bulletinCheckIn(
    nightDate: string,
    bulletinNotices: unknown,
    hitRateSoFar: number | null,
    wallLeftSeconds: number
  ): Promise<{ avoidDirections: string[]; durationScale: number } | null> {
    const system =
      "You monitor an in-progress telescope survey. Reply with one JSON object only: " +
      '{"avoid_directions": [compass codes among N,NE,E,SE,S,SW,W,NW], "duration_scale": number 0.7-1.4}. ' +
      "Avoid directions the current bulletin flags as bad right now; raise duration_scale if the recent hit " +
      "rate is low (fibres are missing their targets), lower it if the hit rate is high.";
    const user = JSON.stringify({ night: nightDate, current_bulletin_notices: bulletinNotices, hit_rate_so_far: hitRateSoFar });
    const answer = await this.chat(system, user, wallLeftSeconds);
    return answer ? this.parseAdvice(answer) : null;
  }

  /** -> true (report), false (do not report), or null (no opinion: keep the rule's decision). */
  async confirmReport(evidence: unknown, wallLeftSeconds: number): Promise<boolean | null> {
    const system =
      "You check telescope data quality. A false instrument-fault report costs points, a correct one " +
      'earns points. Reply with one JSON object only: {"report": true|false}.';
    const answer = await this.chat(system, JSON.stringify(evidence), wallLeftSeconds);
    return answer && typeof answer.report === "boolean" ? (answer.report as boolean) : null;
  }
}
