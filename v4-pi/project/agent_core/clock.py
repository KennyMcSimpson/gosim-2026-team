"""Time budget on the platform's fair clock.

Each card has a budget of 900 *normalized CPU seconds*. Only the CPU time the agent uses
inside its own turns is charged, divided by the machine's `speed_factor`; waiting for a
model, the network or the engine is free. A real-time cap (30 minutes) ends hung runs.
Every `decision_request` carries `payload.wallclock` with, among others:

    remaining_seconds            budget left, normalized seconds
    remaining_real_cpu_seconds   the same budget in real CPU seconds of THIS machine
    wall_remaining_seconds       real time left before the 30-minute cap

Pace compute on `remaining_real_cpu_seconds` and measure the container cgroup
or Python plus worker CPU, so both numbers are in the same unit. A wall clock
(`time.monotonic()`) would also count waiting and other processes, and comparing it with
the normalized `remaining_seconds` makes an agent too timid on slow machines and too
greedy on fast ones.
"""
from __future__ import annotations

import time
from .cpu_meter import cgroup_reader

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
        self._updated_wall = time.monotonic()
        self._updated_cpu = time.process_time()
        self._cgroup_reader = cgroup_reader()
        self._worker_meter = lambda: 0.0
        self.meter_name = "container-cgroup" if self._cgroup_reader else "python-plus-worker"
        self._last_own = time.process_time()
        self._last_group = self._read_group()
        self._total_cpu = 0.0
        self._updated_total = self._cpu_time()
        self._started_total = None

    def _read_group(self):
        try:
            return self._cgroup_reader() if self._cgroup_reader else None
        except (OSError, ValueError, KeyError):
            return None

    def set_worker_meter(self, meter):
        self._worker_meter = meter
        self._last_own = time.process_time() + meter()

    def _cpu_time(self):
        own = time.process_time() + self._worker_meter()
        group = self._read_group()
        own_delta = max(0.0, own - self._last_own)
        delta = (max(own_delta, group - self._last_group)
                 if group is not None and self._last_group is not None else own_delta)
        self._total_cpu += max(0.0, delta)
        self._last_own, self._last_group = own, group
        return self._total_cpu

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
        self._updated_wall = time.monotonic()
        self._updated_cpu = time.process_time()
        self._updated_total = self._cpu_time()

    def wall_remaining(self) -> float:
        return max(0.0, self.wall_left - (time.monotonic() - self._updated_wall))

    def compute_left(self) -> float:
        """Real CPU seconds this agent may still spend thinking."""
        cpu = max(0.0, self.cpu_left - (self._cpu_time() - self._updated_total))
        return min(cpu, WALL_SHARE * self.wall_remaining())

    # Own cost, in process CPU seconds (all threads), around each decision.
    def start_decision(self) -> None:
        self._started = time.process_time()
        self._started_total = self._cpu_time()

    def end_decision(self) -> None:
        if self._started is None:
            return
        self.last_cost = max(0.0, self._cpu_time() - self._started_total)
        self.avg_cost = self.last_cost if self.avg_cost == 0.0 else 0.9 * self.avg_cost + 0.1 * self.last_cost
        self._started = None
        self._started_total = None
