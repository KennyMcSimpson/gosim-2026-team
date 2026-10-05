/**
 * Time budget on the platform's fair clock.
 *
 * Each card has a budget of 900 *normalized CPU seconds*. Only the CPU time the agent uses inside its
 * own turns is charged, divided by the machine's `speed_factor`; waiting for a model, the network or
 * the engine is free. A real-time cap (30 minutes) ends hung runs. Every `decision_request` carries
 * `payload.wallclock` with, among others:
 *
 *   remaining_seconds            budget left, normalized seconds
 *   remaining_real_cpu_seconds   the same budget in real CPU seconds of THIS machine
 *   wall_remaining_seconds       real time left before the 30-minute cap
 *
 * Pace compute on `remaining_real_cpu_seconds` and measure your own work with process CPU time
 * (`process.cpuUsage()`), so both numbers are in the same unit. A wall clock (`Date.now()`) would also
 * count waiting and other processes, and comparing it with the normalized `remaining_seconds` makes an
 * agent too timid on slow machines and too greedy on fast ones.
 */
import { Wallclock } from "./protocol";

/** Leave a fifth of the real time for the engine, model waits and safety: on a slow machine
 *  (speed_factor 2) the CPU budget alone would fill the whole 30-minute cap. */
const WALL_SHARE = 0.8;

export class Clock {
  /** Real CPU seconds of this machine. */
  cpuLeft = Infinity;
  /** Real seconds before the hard cap. */
  wallLeft = Infinity;
  /** Smoothed CPU seconds per decision. */
  avgCost = 0;
  private started: NodeJS.CpuUsage | null = null;

  /** Read the clock fields of one decision_request. Older local runners only send
   *  `remaining_seconds` (then real time), so it is the fallback for both. */
  update(wallclock: Wallclock | undefined): void {
    const fallback = wallclock?.remaining_seconds;
    const cpu = wallclock?.remaining_real_cpu_seconds ?? fallback;
    const wall = wallclock?.wall_remaining_seconds ?? fallback;
    if (cpu !== undefined) this.cpuLeft = cpu;
    if (wall !== undefined) this.wallLeft = wall;
  }

  /** Real CPU seconds this agent may still spend thinking. */
  computeLeft(): number {
    return Math.min(this.cpuLeft, WALL_SHARE * this.wallLeft);
  }

  /** Own cost, in process CPU seconds (user + system, all threads), around each decision. */
  startDecision(): void {
    this.started = process.cpuUsage();
  }

  endDecision(): void {
    if (this.started === null) return;
    const used = process.cpuUsage(this.started);
    const cost = (used.user + used.system) / 1e6;
    this.avgCost = this.avgCost === 0 ? cost : 0.9 * this.avgCost + 0.1 * cost;
    this.started = null;
  }
}
