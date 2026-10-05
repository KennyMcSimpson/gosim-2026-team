"""Read OS CPU counters without a dependency on psutil or worker self-reports."""
from __future__ import annotations

import os
from pathlib import Path


def cgroup_reader():
    if os.name != "posix":
        return None
    try:
        groups = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return None
    paths = []
    for row in groups:
        hierarchy, controllers, group = row.split(":", 2)
        if hierarchy == "0" and not controllers:
            paths += [(Path("/sys/fs/cgroup") / group.lstrip("/") / "cpu.stat", True),
                      (Path("/sys/fs/cgroup/cpu.stat"), True)]
        elif "cpuacct" in controllers.split(","):
            paths += [(Path("/sys/fs/cgroup") / controllers / group.lstrip("/") / "cpuacct.usage", False)]
    for path, v2 in paths:
        def read(path=path, v2=v2):
            text = path.read_text()
            if v2:
                counters = dict(row.split() for row in text.splitlines())
                return int(counters["usage_usec"]) / 1e6
            return int(text.strip()) / 1e9
        try:
            read()
            return read
        except (OSError, ValueError, KeyError):
            continue
    return None


def process_cpu(process):
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            get_times = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
            get_times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            get_times.restype = wintypes.BOOL
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not get_times(int(process._handle), ctypes.byref(created), ctypes.byref(exited),
                             ctypes.byref(kernel), ctypes.byref(user)):
                return None
            ticks = sum((v.dwHighDateTime << 32) + v.dwLowDateTime for v in (kernel, user))
            return ticks / 1e7
        fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, AttributeError, IndexError):
        return None


class WorkerMeter:
    def __init__(self):
        self.process = None
        self.retired = self.current = 0.0
        self.available = True

    def attach(self, process):
        self.process = process
        self.current = 0.0
        self.total()

    def total(self):
        if self.process is not None:
            value = process_cpu(self.process)
            if value is not None:
                self.current = max(self.current, value)
            elif self.process.poll() is None or self.current == 0:
                self.available = False
        return self.retired + self.current

    def retire(self, process):
        if self.process is process:
            self.total()
            self.retired += self.current
            self.current, self.process = 0.0, None
