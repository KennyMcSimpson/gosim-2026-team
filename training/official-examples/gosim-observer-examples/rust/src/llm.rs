//! LLM client: a plain OpenAI-compatible `chat/completions` call, defaulting
//! to the Kimi Coding Plan endpoint (https://www.kimi.com/code/docs/en/) --
//! `OPENAI_BASE_URL` / `OPENAI_MODEL` override the default base URL/model,
//! and the key comes from `OPENAI_API_KEY` (or `KIMI_API_KEY`). Any other
//! OpenAI-compatible endpoint works the same way by setting those three.
//!
//! Waiting for the model is not charged to the CPU budget (see `clock.rs`),
//! but it does use real time, and each card has a 30-minute real-time cap.
//! So model use is bounded by real time: every attempt has a timeout
//! (default 20 s), one question gives up after 60 s in total, no call starts
//! in the last 5 minutes before the cap, and a run makes at most
//! `LLM_MAX_CALLS` requests. HTTP 429 (rate limit) and 5xx answers, timeouts
//! and network errors are retried with exponential backoff plus random
//! jitter, honouring `Retry-After`. In the hidden final a card's 3 repeats
//! run at the same time on the same key, so 429s are to be expected; the
//! jitter keeps the repeats from retrying in lockstep. If a question still
//! fails, the night's plan uses its own rule-based numbers for that step.

use serde_json::{json, Value};
use std::env;
use std::time::{Duration, Instant};

use crate::state::RunState;

const DEFAULT_BASE_URL: &str = "https://api.kimi.com/coding/v1";
const DEFAULT_MODEL: &str = "k3";
const MAX_ATTEMPTS: u32 = 4;
/// No new model call this close to the real-time cap.
const WALL_RESERVE_SECONDS: f64 = 300.0;
/// One question, all retries and backoff included.
const QUESTION_DEADLINE_SECONDS: f64 = 60.0;
const MAX_BACKOFF_SECONDS: f64 = 20.0;

/// Why an attempt failed, and whether trying again can help.
enum Failure {
    /// 429, 5xx, timeout or network trouble; carries `Retry-After` if sent.
    Retryable(String, Option<f64>),
    /// 400/401/403/404 and the like: retrying will not help.
    Fatal(String),
}

/// A number in [0, 1) for backoff jitter, without a `rand` dependency.
fn jitter() -> f64 {
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
    (nanos % 1_000_000) as f64 / 1_000_000.0
}

pub struct LlmClient {
    base_url: String,
    api_key: String,
    model: String,
    timeout_seconds: f64,
    max_calls: u32,
}

impl LlmClient {
    /// `Err` with a plain, user-facing message when no key is configured --
    /// callers are expected to log it and exit rather than start a run that
    /// cannot plan.
    pub fn from_env() -> Result<LlmClient, String> {
        let api_key = env::var("OPENAI_API_KEY")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .or_else(|| env::var("KIMI_API_KEY").ok().filter(|s| !s.trim().is_empty()))
            .map(|s| s.trim().to_string())
            .ok_or_else(|| "missing API key: set OPENAI_API_KEY".to_string())?;
        let base_url = env::var("OPENAI_BASE_URL")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| DEFAULT_BASE_URL.to_string());
        let model = env::var("OPENAI_MODEL")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| DEFAULT_MODEL.to_string());
        let timeout_seconds: u64 = env::var("LLM_TIMEOUT_SECONDS")
            .ok()
            .and_then(|s| s.trim().parse().ok())
            .filter(|&s| s > 0)
            .unwrap_or(20);
        let max_calls: u32 = env::var("LLM_MAX_CALLS")
            .ok()
            .and_then(|s| s.trim().parse().ok())
            .filter(|&n| n > 0)
            .unwrap_or(100);
        Ok(LlmClient {
            base_url,
            api_key,
            model,
            timeout_seconds: timeout_seconds as f64,
            max_calls,
        })
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    fn attempt_chat(&self, system: &str, user: &str, timeout: Duration) -> Result<String, Failure> {
        let url = format!("{}/chat/completions", self.base_url.trim_end_matches('/'));
        let body = json!({
            "model": self.model,
            // No "temperature": Kimi Coding Plan models (k3 / kimi-for-coding) reject any
            // value but 1 with HTTP 400, so leave it to the provider's default. Reasoning
            // models spend tokens thinking before the JSON answer, hence the roomy cap.
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        });
        let response = match ureq::post(&url)
            .timeout(timeout)
            .set("Authorization", &format!("Bearer {}", self.api_key))
            .set("Content-Type", "application/json")
            .send_json(body)
        {
            Ok(response) => response,
            Err(ureq::Error::Status(code, response)) if code == 429 || code >= 500 => {
                let retry_after = response.header("Retry-After").and_then(|v| v.trim().parse::<f64>().ok());
                return Err(Failure::Retryable(format!("HTTP {code}"), retry_after));
            }
            Err(ureq::Error::Status(code, _)) => return Err(Failure::Fatal(format!("HTTP {code}"))),
            Err(e) => return Err(Failure::Retryable(format!("request failed ({e})"), None)), // timeout, reset, DNS ...
        };
        let parsed: Value = response.into_json().map_err(|e| Failure::Retryable(format!("response was not JSON ({e})"), None))?;
        parsed
            .get("choices")
            .and_then(|c| c.get(0))
            .and_then(|c| c.get("message"))
            .and_then(|m| m.get("content"))
            .and_then(|c| c.as_str())
            .map(|s| s.to_string())
            .ok_or_else(|| Failure::Retryable("response had no choices[0].message.content".to_string(), None))
    }

    /// One question: up to `MAX_ATTEMPTS` attempts with backoff, all within
    /// `QUESTION_DEADLINE_SECONDS` and never in the last
    /// `WALL_RESERVE_SECONDS` before the real-time cap
    /// (`run.clock.wall_left`). `None` means: use the rule-based answer.
    fn chat(&self, system: &str, user: &str, run: &mut RunState) -> Option<String> {
        let started = Instant::now();
        let deadline = QUESTION_DEADLINE_SECONDS.min(run.clock.wall_left - WALL_RESERVE_SECONDS);
        for attempt in 1..=MAX_ATTEMPTS {
            let time_left = deadline - started.elapsed().as_secs_f64();
            if time_left < 2.0 || run.llm_calls_made >= self.max_calls {
                return None;
            }
            run.llm_calls_made += 1;
            let timeout = Duration::from_secs_f64(self.timeout_seconds.min(time_left));
            match self.attempt_chat(system, user, timeout) {
                Ok(content) => return Some(content),
                Err(Failure::Fatal(reason)) => {
                    crate::memory::log(&format!("llm: call failed ({reason}); not retrying"));
                    return None;
                }
                Err(Failure::Retryable(reason, retry_after)) => {
                    // Exponential backoff with full jitter (1, 2, 4 ... s,
                    // randomised), or the server's Retry-After.
                    let pause = retry_after.unwrap_or_else(|| jitter() * 2f64.powi(attempt as i32 - 1)).min(MAX_BACKOFF_SECONDS);
                    crate::memory::log(&format!("llm: attempt {attempt}/{MAX_ATTEMPTS} failed ({reason}); retry in {pause:.1}s"));
                    if attempt == MAX_ATTEMPTS || started.elapsed().as_secs_f64() + pause > deadline - 2.0 {
                        return None;
                    }
                    std::thread::sleep(Duration::from_secs_f64(pause)); // waiting: no CPU budget is charged
                }
            }
        }
        None
    }

    /// Asks the model to answer in strict JSON, tolerating a little prose
    /// around it (some OpenAI-compatible providers do not honour
    /// `response_format`) by taking the outermost `{...}` substring.
    fn ask_json(&self, system: &str, user: &str, run: &mut RunState) -> Option<Value> {
        let content = self.chat(system, user, run)?;
        let start = content.find('{')?;
        let end = content.rfind('}')?;
        if end < start {
            return None;
        }
        match serde_json::from_str(&content[start..=end]) {
            Ok(v) => Some(v),
            Err(e) => {
                crate::memory::log(&format!("llm: could not parse JSON reply ({e})"));
                None
            }
        }
    }
}

/// What either nightly step returns: compass directions to discount and an
/// exposure-duration scale. Only ever nudges duration and which directions to
/// avoid -- never the pointing, fibre assignments or declared program.
#[derive(Clone, Debug, Default)]
pub struct NightAdvice {
    pub avoid_directions: Vec<String>,
    pub duration_scale: f64,
}

const COMPASS: [&str; 8] = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

fn parse_advice(value: &Value) -> NightAdvice {
    let avoid_directions: Vec<String> = value
        .get("avoid_directions")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str())
                .map(|s| s.to_uppercase())
                .filter(|s| COMPASS.contains(&s.as_str()))
                .collect()
        })
        .unwrap_or_default();
    let duration_scale = value.get("duration_scale").and_then(|v| v.as_f64()).unwrap_or(1.0).clamp(0.7, 1.4);
    NightAdvice { avoid_directions, duration_scale }
}

/// Step A: once per observing night, reads the public forecast notices
/// recorded for tonight plus the current bulletin (see `planner::night_llm_steps`).
pub fn ask_night_advice(client: &LlmClient, run: &mut RunState, context: &str) -> Option<NightAdvice> {
    let system = "You help schedule a telescope survey. The user message describes PUBLIC \
        forecast/bulletin notices for tonight only -- no hidden data. Reply with ONLY a compact \
        JSON object, no prose, no markdown fences: \
        {\"avoid_directions\":[compass codes among N,NE,E,SE,S,SW,W,NW],\"duration_scale\":0.7-1.4}. \
        Avoid directions with bad weather tonight; use a larger duration_scale when the sky looks poor.";
    Some(parse_advice(&client.ask_json(system, context, run)?))
}

/// Step B: once per observing night, reads tonight's live bulletin plus the
/// agent's own hit rate so far (all PUBLIC, from its own prior actions).
pub fn ask_hitrate_advice(client: &LlmClient, run: &mut RunState, context: &str) -> Option<NightAdvice> {
    let system = "You help schedule a telescope survey. The user message describes tonight's PUBLIC \
        bulletin and the agent's own hit rate so far this run -- no hidden data. Reply with ONLY a \
        compact JSON object, no prose, no markdown fences: \
        {\"avoid_directions\":[compass codes among N,NE,E,SE,S,SW,W,NW],\"duration_scale\":0.7-1.4}. \
        A low hit rate suggests longer exposures (duration_scale closer to 1.4); a high one, shorter.";
    Some(parse_advice(&client.ask_json(system, context, run)?))
}

/// Union of `avoid_directions`, average of `duration_scale`; `None` only
/// when both calls returned nothing usable.
pub fn merge_advice(a: Option<NightAdvice>, b: Option<NightAdvice>) -> Option<NightAdvice> {
    match (a, b) {
        (None, None) => None,
        (Some(only), None) | (None, Some(only)) => Some(only),
        (Some(a), Some(b)) => {
            let mut avoid_directions = a.avoid_directions;
            for direction in b.avoid_directions {
                if !avoid_directions.contains(&direction) {
                    avoid_directions.push(direction);
                }
            }
            Some(NightAdvice { avoid_directions, duration_scale: (a.duration_scale + b.duration_scale) / 2.0 })
        }
    }
}
