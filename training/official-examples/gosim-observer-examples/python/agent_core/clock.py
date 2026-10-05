"""Time budget on the platform's fair clock.

Each card has a budget of 900 *normalized CPU seconds*. Only the CPU time the agent uses
inside its own turns is charged, divided by the machine's `speed_factor`; waiting for a
model, the network or the engine is free. A real-time cap (30 minutes) ends hung runs.
Every `decision_request` carries `payload.wallclock` with, among others:

    remaining_seconds            budget left, normalized seconds
    remaining_real_cpu_seconds   the same budget in real CPU seconds of THIS machine
    wall_remaining_seconds       real time left before the 30-minute cap

Pace compute on `remaining_real_cpu_seconds` and measure your own work with process CPU
time (`time.process_time()`), so both numbers are in the same unit. A wall clock
(`time.monotonic()`) would also count waiting and other processes, and comparing it with
the normalized `remaining_seconds` makes an agent too timid on slow machines and too
greedy on fast ones.
"""
from __future__ import annotations

import time

# Leave a fifth of the real time for the engine, model waits and safety: on a slow
# machine (speed_factor 2) the CPU budget alone would fill the whole 30-minute cap.
WALL_SHARE = 0.8


class Clock:
    def __init__(self) -> None:
        self.cpu_left = float("inf")   # real CPU seconds of this machine
        self.wall_left = float("inf")  # real seconds before the hard cap
        self._started = None
        self.last_cost = 0.0           # CPU seconds of the last decision
        self.avg_cost = 0.0            # smoothed CPU seconds per decision

    def update(self, wallclock: dict) -> None:
        """Read the clock fields of one decision_request. Older local runners only send
        `remaining_seconds` (then real time), so it is the fallback for both."""
        wallclock = wallclock or {}
        fallback = wallclock.get("remaining_seconds")
        cpu = wallclock.get("remaining_real_cpu_seconds", fallback)
        wall = wallclock.get("wall_remaining_seconds", fallback)
        if cpu is not None:
            self.cpu_left = float(cpu)
        if wall is not None:
            self.wall_left = float(wall)

    def compute_left(self) -> float:
        """Real CPU seconds this agent may still spend thinking."""
        return min(self.cpu_left, WALL_SHARE * self.wall_left)

    # Own cost, in process CPU seconds (all threads), around each decision.
    def start_decision(self) -> None:
        self._started = time.process_time()

    def end_decision(self) -> None:
        if self._started is None:
            return
        self.last_cost = time.process_time() - self._started
        self.avg_cost = self.last_cost if self.avg_cost == 0.0 else 0.9 * self.avg_cost + 0.1 * self.last_cost
        self._started = None
