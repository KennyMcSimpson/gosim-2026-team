//! Time budget on the platform's fair clock.
//!
//! Each card has a budget of 900 *normalized CPU seconds*. Only the CPU time
//! the agent uses inside its own turns is charged, divided by the machine's
//! `speed_factor`; waiting for a model, the network or the engine is free. A
//! real-time cap (30 minutes) ends hung runs. Every `decision_request` carries
//! `payload.wallclock` with, among others:
//!
//! ```text
//! remaining_seconds            budget left, normalized seconds
//! remaining_real_cpu_seconds   the same budget in real CPU seconds of THIS machine
//! wall_remaining_seconds       real time left before the 30-minute cap
//! ```
//!
//! Pace compute on `remaining_real_cpu_seconds` and measure your own work with
//! process CPU time (`getrusage`), so both numbers are in the same unit. A wall
//! clock (`Instant`) would also count waiting and other processes, and
//! comparing it with the normalized `remaining_seconds` makes an agent too
//! timid on slow machines and too greedy on fast ones.

use crate::protocol::Wallclock;

/// Leave a fifth of the real time for the engine, model waits and safety: on
/// a slow machine (speed_factor 2) the CPU budget alone would fill the whole
/// 30-minute cap.
const WALL_SHARE: f64 = 0.8;

pub struct Clock {
    /// Real CPU seconds of this machine.
    pub cpu_left: f64,
    /// Real seconds before the hard cap.
    pub wall_left: f64,
    /// Smoothed CPU seconds per decision.
    pub avg_cost: f64,
    started: Option<f64>,
}

impl Clock {
    pub fn new(budget_seconds: f64) -> Clock {
        Clock { cpu_left: budget_seconds, wall_left: f64::INFINITY, avg_cost: 0.0, started: None }
    }

    /// Reads the clock fields of one decision_request. Older local runners
    /// only send `remaining_seconds` (then real time), so it is the fallback
    /// for both.
    pub fn update(&mut self, wallclock: &Wallclock) {
        let fallback = wallclock.remaining_seconds;
        self.cpu_left = wallclock.remaining_real_cpu_seconds.unwrap_or(fallback);
        self.wall_left = wallclock.wall_remaining_seconds.unwrap_or(fallback);
    }

    /// Real CPU seconds this agent may still spend thinking.
    pub fn compute_left(&self) -> f64 {
        self.cpu_left.min(WALL_SHARE * self.wall_left)
    }

    /// Own cost, in process CPU seconds (all threads), around each decision.
    pub fn start_decision(&mut self) {
        self.started = Some(process_cpu_seconds());
    }

    pub fn end_decision(&mut self) {
        if let Some(started) = self.started.take() {
            let cost = process_cpu_seconds() - started;
            self.avg_cost = if self.avg_cost == 0.0 { cost } else { 0.9 * self.avg_cost + 0.1 * cost };
        }
    }
}

/// User + system CPU time of this process, all threads, in seconds.
#[cfg(unix)]
pub fn process_cpu_seconds() -> f64 {
    // SAFETY: getrusage only writes into the zeroed struct we hand it.
    let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage) } != 0 {
        return 0.0;
    }
    let seconds = |t: libc::timeval| t.tv_sec as f64 + t.tv_usec as f64 / 1e6;
    seconds(usage.ru_utime) + seconds(usage.ru_stime)
}

/// The platform runs Linux; elsewhere we simply do not measure our own cost.
#[cfg(not(unix))]
pub fn process_cpu_seconds() -> f64 {
    0.0
}
