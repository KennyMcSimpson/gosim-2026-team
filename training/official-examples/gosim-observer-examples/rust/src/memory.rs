//! What the agent remembers between decisions, and the stderr logger.
//!
//! Everything stored here is derived from the agent's *own* run: hit scores
//! the backend reported back (`last_result.hits`), and the public bulletins
//! and forecasts in `new_messages`/`latest_bulletin`. No scenario truth ever
//! passes through this module -- it only ever sees what the protocol already
//! sent the agent.

use std::collections::{HashSet, VecDeque};
use std::io::Write as _;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::scoring;
use crate::state::Config;

/// Writes one timestamped line to stderr. Per the guide, stdout is reserved
/// for protocol JSON only, so every diagnostic goes here. Never panics: a
/// broken stderr pipe must not take down the agent either.
pub fn log(text: &str) {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let _ = std::io::stderr().write_fmt(format_args!("[{:.3}] {}\n", now.as_secs_f64(), text));
}

/// How long a quality sample stays "recent enough" to trust over the older
/// `prior_scale` median -- mirrors the reference agent's own sky-memory window.
const SKY_MEMORY_HOURS: f64 = 2.0;
const SAMPLES_CAP: usize = 24;
const ALL_RATIOS_CAP: usize = 400;
/// Only the most recent assignments this long are checked against a repeat
/// miss at (nearly) the same pointing -- `direction_factor`'s "blocked" list.
const BLOCKED_RECENT: usize = 40;

/// What the planner expected for one assigned fibre, kept only long enough to
/// interpret the matching `last_result` on the next `decision_request`.
pub struct PendingPrediction {
    pub model: f64,
    pub band_model: f64,
    pub alt: f64,
    pub az: f64,
}

/// Everything the agent has learned so far: per-target completion progress,
/// the learned sky-quality scale, and public weather/terrain notices.
pub struct Memory {
    pub factor: Vec<f64>,
    pub misses: Vec<u32>,
    pub attempts: Vec<u32>,
    /// Targets still worth considering at all (pruned once truly done; see
    /// `planner::value`). Rebuilt wholesale on a Hard-mode `state_resync`.
    pub active: Vec<usize>,

    pub scale: f64,
    prior_scale: f64,
    samples: VecDeque<(f64, f64)>,
    all_ratios: VecDeque<f64>,

    /// Exposure-duration multiplier the night-advice LLM step may set
    /// (clamped to [0.7, 1.4]); reset to 1.0 at the start of every night.
    pub duration_scale: f64,
    /// `(az, alt)` of recent assigned-but-scoreless fibres, used to suspect a
    /// hidden pointing offset or obstruction near that direction.
    blocked: Vec<(f64, f64)>,
    /// `(event_kind, direction)` from the latest bulletin (terrain excluded).
    notices: HashSet<(String, String)>,
    terrain: HashSet<String>,
    /// Extra compass directions the night-advice LLM step suggested avoiding.
    pub extra_avoid: HashSet<String>,
    /// The latest `forecast` message's notices, for the next night-advice prompt.
    pub last_forecast_notices: Value,

    /// A `BTreeMap`, not a `HashMap`: `on_result` drains this in whatever
    /// order it iterates in, and that order feeds the `blocked` list and the
    /// sample-ring eviction below -- both of which should depend only on the
    /// run itself, not on this process's random hash seed.
    pub pending: std::collections::BTreeMap<String, PendingPrediction>,
    pub pending_program: String,
    pub pending_duration: f64,
    pub pending_night: i64,
    pub night_seen: Option<usize>,
    pub consecutive_full_miss: u32,
    pub consecutive_reports: u32,
    pub reports_issued: u32,
    /// Running totals of fibres assigned and of those that scored, across the
    /// whole run so far -- the "own hit rate so far" context for the nightly
    /// hit-rate-aware LLM step (`planner::night_llm_steps`).
    pub total_assigned: i64,
    pub total_hits: i64,

    /// Per-observe-action ledger: `(observe_action_index, target_id, factor)`,
    /// mirroring the backend's own BestLedger so a Hard-mode `state_resync`
    /// can be answered exactly (see `resync`) instead of only from the
    /// resync message's `best_scores`.
    pub ledger: Vec<(i64, String, f64)>,
    pub pending_action_index: Option<i64>,
    /// `decision_request.payload.observe_action_index` for the decision in
    /// progress; set by `planner::decide` before planning, copied into
    /// `pending_action_index` by `finish_plan` if this decision ends up
    /// being an `observe`.
    pub current_action_index: Option<i64>,
}

impl Memory {
    pub fn new(config: &Config) -> Memory {
        let n = config.targets.len();
        let active = config.targets.iter().enumerate().filter(|(_, t)| t.hmax_deg > 0.0).map(|(i, _)| i).collect();
        Memory {
            factor: vec![0.0; n],
            misses: vec![0; n],
            attempts: vec![0; n],
            active,
            scale: 1.0,
            prior_scale: 1.0,
            samples: VecDeque::new(),
            all_ratios: VecDeque::new(),
            duration_scale: 1.0,
            blocked: Vec::new(),
            notices: HashSet::new(),
            terrain: HashSet::new(),
            extra_avoid: HashSet::new(),
            last_forecast_notices: Value::Array(vec![]),
            pending: std::collections::BTreeMap::new(),
            pending_program: "BACKUP".to_string(),
            pending_duration: 0.0,
            pending_night: -1,
            night_seen: None,
            consecutive_full_miss: 0,
            consecutive_reports: 0,
            reports_issued: 0,
            total_assigned: 0,
            total_hits: 0,
            ledger: Vec::new(),
            pending_action_index: None,
            current_action_index: None,
        }
    }

    // --- public bulletins/forecasts -------------------------------------------------------------

    pub fn on_messages(&mut self, config: &Config, new_messages: &[Value], latest_bulletin: Option<&Value>) {
        for message in new_messages {
            let record_type = message.get("record_type").and_then(|v| v.as_str()).unwrap_or("");
            if record_type == "bulletin" && message.get("initial").and_then(|v| v.as_bool()).unwrap_or(false) {
                for notice in message.get("notices").and_then(|v| v.as_array()).into_iter().flatten() {
                    if notice.get("event_kind").and_then(|v| v.as_str()) == Some("terrain_obstruction") {
                        if let Some(dir) = notice.get("direction").and_then(|v| v.as_str()) {
                            self.terrain.insert(dir.to_string());
                        }
                    }
                }
            } else if record_type == "forecast" {
                self.last_forecast_notices = message.get("notices").cloned().unwrap_or(Value::Array(vec![]));
            } else if record_type == "state_resync" {
                self.resync(config, message);
            } else if record_type == "observation_request" {
                let request_id = message.get("request_id").and_then(|v| v.as_str()).unwrap_or("?");
                let n_targets = message.get("target_ids").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);
                let deadline = message.get("deadline_utc").and_then(|v| v.as_str()).unwrap_or("?");
                log(&format!("planner: observation request {request_id} issued, {n_targets} targets by {deadline}"));
            } else if record_type == "observation_request_result" {
                let request_id = message.get("request_id").and_then(|v| v.as_str()).unwrap_or("?");
                let status = message.get("status").and_then(|v| v.as_str()).unwrap_or("?");
                let reward = message.get("score_delta").and_then(|v| v.as_f64()).unwrap_or(0.0);
                log(&format!("planner: observation request {request_id} {status} (reward {reward})"));
            }
        }
        self.notices.clear();
        if let Some(bulletin) = latest_bulletin {
            for notice in bulletin.get("notices").and_then(|v| v.as_array()).into_iter().flatten() {
                let kind = notice.get("event_kind").and_then(|v| v.as_str()).unwrap_or("");
                if kind == "terrain_obstruction" {
                    continue;
                }
                let direction = notice.get("direction").and_then(|v| v.as_str()).unwrap_or("");
                self.notices.insert((kind.to_string(), direction.to_string()));
            }
        }
    }

    /// Hard-mode `state_resync` (participant guide, Appendix A / section 8): a
    /// prior window of `observe` actions was invalidated. The message itself
    /// only gives `best_scores` (score, not factor) for targets with any
    /// surviving valid hit -- the guide is explicit that it does not return
    /// each target's completion factor, and that an agent that needs it
    /// exactly should combine `invalidated_window` with its own saved
    /// valid-exposure history.
    ///
    /// We can do exactly that: every entry in `self.ledger` already holds the
    /// EXACT factor for one past observe action (`on_result` backs it out of
    /// the real score the backend returned, the public weight, and whichever
    /// of the two public program multipliers it matches -- not an estimate).
    /// Dropping the ledger entries inside the invalidated action-index window
    /// and taking, per target, the max factor among what is left reproduces
    /// the backend's own ledger exactly -- a reconstruction, not an
    /// approximation from best_score.
    ///
    /// The only remaining uncertainty: a target with no ledger entry at all
    /// (e.g. this process restarted mid-run and lost its in-memory history)
    /// falls back to the old best_score/top_multiplier estimate below, same
    /// as before this change.
    fn resync(&mut self, config: &Config, message: &Value) {
        let window = message.get("invalidated_window");
        let start = window.and_then(|w| w.get("action_index_start")).and_then(|v| v.as_i64());
        let end = window.and_then(|w| w.get("action_index_end_exclusive")).and_then(|v| v.as_i64());
        match (start, end) {
            (Some(start), Some(end)) => self.ledger.retain(|(index, _, _)| !(*index >= start && *index < end)),
            _ => self.ledger.clear(), // no window given: nothing in the ledger can be trusted
        }
        let mut exact: std::collections::HashMap<&str, f64> = std::collections::HashMap::new();
        for (_, target_id, factor) in &self.ledger {
            let entry = exact.entry(target_id.as_str()).or_insert(0.0);
            if *factor > *entry {
                *entry = *factor;
            }
        }

        let best_scores: Vec<(String, f64)> = message
            .get("best_scores")
            .and_then(|v| v.as_array())
            .map(|rows| {
                rows.iter()
                    .filter_map(|row| {
                        let id = row.get("target_id")?.as_str()?.to_string();
                        let score = row.get("best_score")?.as_f64()?;
                        Some((id, score))
                    })
                    .collect()
            })
            .unwrap_or_default();
        let top = config.program.richest_multiplier();
        let mut best: std::collections::HashMap<&str, f64> = std::collections::HashMap::new();
        for (id, score) in &best_scores {
            best.insert(id.as_str(), *score);
        }
        for (i, target) in config.targets.iter().enumerate() {
            if let Some(&factor) = exact.get(target.target_id.as_str()) {
                self.factor[i] = factor;
                continue;
            }
            let score = best.get(target.target_id.as_str()).copied().unwrap_or(0.0);
            self.factor[i] = if score > 0.0 { (score / (target.science_weight.max(1e-9) * top)).min(1.0) } else { 0.0 };
        }
        self.active = config.targets.iter().enumerate().filter(|(_, t)| t.hmax_deg > 0.0).map(|(i, _)| i).collect();
        self.pending.clear();
        self.pending_action_index = None;
        log(&format!(
            "memory: applied state_resync, {} ledger entr{} survived, {} best_score fallback(s)",
            exact.len(),
            if exact.len() == 1 { "y" } else { "ies" },
            best_scores.len()
        ));
    }

    pub fn site_closed(&self) -> bool {
        self.notices.iter().any(|(kind, dir)| matches!(kind.as_str(), "rain" | "storm") && dir == "ALL")
    }

    /// Public weather/terrain/LLM-advised directions to avoid, plus a short
    /// memory of recent assigned-but-scoreless pointings -- all PUBLIC, all
    /// derived from the agent's own run. 0.0 = don't even try; <1.0 = discount.
    pub fn direction_factor(&self, alt: f64, az: f64) -> f64 {
        for direction in &self.terrain {
            if let Some(dir_az) = scoring::direction_azimuth(direction) {
                if alt < 50.0 && scoring::az_distance(az, dir_az) <= 60.0 {
                    return 0.0;
                }
            }
        }
        let mut factor: f64 = 1.0;
        const BLOCKING_KINDS: [&str; 2] = ["terrain_obstruction", "rocket_launch"];
        for (kind, direction) in &self.notices {
            let Some(dir_az) = scoring::direction_azimuth(direction) else { continue };
            let near = scoring::az_distance(az, dir_az) <= 67.5;
            if BLOCKING_KINDS.contains(&kind.as_str()) && near && alt < 62.0 {
                return 0.0;
            }
            if near && alt < 75.0 {
                factor = factor.min(0.35);
            }
        }
        for direction in &self.extra_avoid {
            if let Some(dir_az) = scoring::direction_azimuth(direction) {
                if scoring::az_distance(az, dir_az) <= 67.5 && alt < 70.0 {
                    factor = factor.min(0.35);
                }
            }
        }
        let recent_start = self.blocked.len().saturating_sub(BLOCKED_RECENT);
        for &(blocked_az, blocked_alt) in &self.blocked[recent_start..] {
            if scoring::az_distance(az, blocked_az) <= 12.0 && alt <= blocked_alt + 3.0 {
                factor = factor.min(0.2);
            }
        }
        factor
    }

    // --- learning from our own last_result --------------------------------------------------------

    /// Ports the reference agent's `onResult`: backs out each hit's true
    /// completion factor from the declared-vs-mismatch multiplier, updates
    /// per-target misses/attempts, and feeds the learned sky-quality scale.
    pub fn on_result(&mut self, config: &Config, last_result: Option<&Value>, hours: f64) {
        let action_index = self.pending_action_index.take();
        let Some(result) = last_result else {
            self.pending.clear();
            return;
        };
        let action = result.get("action").and_then(|v| v.as_str()).unwrap_or("");
        if action == "report" {
            log(&format!("planner: previous report result = {result}"));
            self.pending.clear();
            return;
        }
        if action != "observe" || self.pending.is_empty() {
            self.pending.clear();
            return;
        }
        let assigned_count = result.get("assigned_count").and_then(|v| v.as_i64()).unwrap_or(0);
        let hits_array = result.get("hits").and_then(|v| v.as_array()).cloned().unwrap_or_default();
        let hits: std::collections::HashMap<String, f64> = hits_array
            .iter()
            .filter_map(|h| Some((h.get("target_id")?.as_str()?.to_string(), h.get("score")?.as_f64()?)))
            .collect();
        let any_positive = hits.values().any(|&s| s > 0.0);
        let declared = config.program.multiplier_for(&self.pending_program);
        let mismatch = config.program.mismatch_multiplier;
        let flux0t0 = (config.flux_zero_point * config.exposure_zero_point_seconds).max(1e-9);
        let pending_duration = self.pending_duration;
        let pending_program = self.pending_program.clone();

        let pending = std::mem::take(&mut self.pending);
        for (target_id, prediction) in pending {
            let Some(i) = config.index_of(&target_id) else { continue };
            match hits.get(&target_id) {
                None => self.misses[i] += 1,
                Some(&score) if score <= 0.0 => {
                    if any_positive {
                        self.blocked.push((prediction.az, prediction.alt));
                    }
                }
                Some(&score) => {
                    let weight = config.targets[i].science_weight.max(1e-9);
                    let factor_if_match = score / (weight * declared);
                    let factor_if_miss = score / (weight * mismatch.max(1e-9));
                    let ratio_match = (factor_if_match * flux0t0) / (config.targets[i].feature_flux.max(1e-9) * pending_duration * prediction.model.max(1e-9));
                    let band = scoring::program_band(ratio_match * prediction.band_model, &config.program);
                    let matched = band == pending_program.as_str();
                    let factor = (if matched { factor_if_match } else { factor_if_miss }).min(1.0);
                    self.factor[i] = self.factor[i].max(factor);
                    if let Some(action_index) = action_index {
                        self.ledger.push((action_index, target_id.clone(), factor));
                    }
                    if config.targets[i].required && self.factor[i] < 0.5 {
                        self.attempts[i] += 1;
                    }
                    if factor < 0.97 && prediction.model > 0.0 {
                        let ratio = (factor * flux0t0) / (config.targets[i].feature_flux.max(1e-9) * pending_duration * prediction.model);
                        push_capped(&mut self.samples, (hours, ratio), SAMPLES_CAP);
                        push_capped(&mut self.all_ratios, ratio, ALL_RATIOS_CAP);
                    }
                }
            }
        }
        self.total_assigned += assigned_count;
        self.total_hits += hits_array.len() as i64;
        if hits.is_empty() && assigned_count >= 3 {
            self.consecutive_full_miss += 1;
        } else {
            self.consecutive_full_miss = 0;
        }
        self.update_scale(hours);
    }

    fn update_scale(&mut self, hours: f64) {
        if self.all_ratios.len() >= 8 {
            let mut ordered: Vec<f64> = self.all_ratios.iter().copied().collect();
            ordered.sort_by(|a, b| a.total_cmp(b));
            self.prior_scale = ordered[ordered.len() / 2];
        }
        let mut recent: Vec<f64> = self.samples.iter().filter(|(when, _)| *when >= hours - SKY_MEMORY_HOURS).map(|(_, r)| *r).collect();
        recent.sort_by(|a, b| a.total_cmp(b));
        self.scale = if recent.len() >= 4 { recent[recent.len() / 2].max(0.05) } else { self.prior_scale };
    }

    pub fn has_recent_sample(&self, hours: f64) -> bool {
        self.samples.iter().any(|(when, _)| *when >= hours - SKY_MEMORY_HOURS)
    }
}

fn push_capped<T>(buffer: &mut VecDeque<T>, item: T, cap: usize) {
    buffer.push_back(item);
    if buffer.len() > cap {
        buffer.pop_front();
    }
}
