"""Counts Mars Vista API requests so users can see how much of the free quota is left, and slows down before the
limit is reached. Mars Vista allows 10,000 requests per hour and 100,000 per day per API key. Its responses
don't report usage, so these counts are the program's own estimate. Photo downloads come from NASA's servers and
don't count.

Counts are kept per minute for the last 24 hours and saved to disk, so stopping and restarting the program
doesn't reset them while the server is still counting.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass

HOURLY_LIMIT = 10_000
DAILY_LIMIT = 100_000
SAFETY_MARGIN = 25      # stop this many requests short of a limit


@dataclass(frozen=True)
class Usage:
    hour: int
    day: int
    hourly_limit: int
    daily_limit: int
    waiting_until: float | None     # Unix time the current wait ends, or None when not waiting
    wait_reason: str                # "hourly limit", "daily limit", "server asked us to wait", or ""

    @property
    def waiting(self) -> bool:
        return self.waiting_until is not None and self.waiting_until > time.time()


class RequestTracker:
    def __init__(self, path: str | None = None, hourly_limit: int = HOURLY_LIMIT, daily_limit: int = DAILY_LIMIT,
                 margin: int = SAFETY_MARGIN, clock=time.time):
        self.path = path
        self.hourly_limit, self.daily_limit, self.margin = hourly_limit, daily_limit, margin
        self.clock = clock
        self._lock = threading.Lock()
        self._minutes: dict[int, int] = {}       # minute number (Unix time // 60) -> requests in that minute
        self._waiting_until: float | None = None
        self._wait_reason = ""
        self._unsaved = 0
        self._load()

    # --- counting --------------------------------------------------------------------------------------------

    def record(self) -> None:
        """Call once per request sent to Mars Vista."""
        with self._lock:
            m = int(self.clock() // 60)
            self._minutes[m] = self._minutes.get(m, 0) + 1
            self._unsaved += 1
            if self._unsaved >= 20:
                self._save_locked()

    def usage(self) -> Usage:
        with self._lock:
            hour, day = self._counts_locked()
            waiting_until = self._waiting_until if (self._waiting_until or 0) > self.clock() else None
            return Usage(hour, day, self.hourly_limit, self.daily_limit, waiting_until,
                         self._wait_reason if waiting_until else "")

    def seconds_until_allowed(self) -> tuple[float, str]:
        """(0, "") if a request can be sent now; otherwise how long to wait and which limit is the reason."""
        with self._lock:
            now = self.clock()
            now_m = int(now // 60)
            self._prune_locked(now_m)
            hour, day = self._counts_locked()
            if day >= self.daily_limit - self.margin:
                return self._until_count_drops_locked(now, 1440, day - (self.daily_limit - self.margin)), \
                    "daily limit"
            if hour >= self.hourly_limit - self.margin:
                return self._until_count_drops_locked(now, 60, hour - (self.hourly_limit - self.margin)), \
                    "hourly limit"
            return 0.0, ""

    # --- waits (shown in the GUI and CLI) --------------------------------------------------------------------

    def begin_wait(self, seconds: float, reason: str) -> None:
        with self._lock:
            self._waiting_until = self.clock() + seconds
            self._wait_reason = reason

    def end_wait(self) -> None:
        with self._lock:
            self._waiting_until = None
            self._wait_reason = ""

    # --- persistence -----------------------------------------------------------------------------------------

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _load(self) -> None:
        if not (self.path and os.path.exists(self.path)):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self._minutes = {int(k): int(v) for k, v in data.get("minutes", {}).items()}
        except (OSError, ValueError):
            self._minutes = {}
        self._prune_locked(int(self.clock() // 60))

    def _save_locked(self) -> None:
        self._unsaved = 0
        if not self.path:
            return
        self._prune_locked(int(self.clock() // 60))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"minutes": {str(k): v for k, v in self._minutes.items()}}, f)
        os.replace(tmp, self.path)

    # --- helpers ---------------------------------------------------------------------------------------------

    def _prune_locked(self, now_m: int) -> None:
        for m in [m for m in self._minutes if m <= now_m - 1440]:
            del self._minutes[m]

    def _counts_locked(self) -> tuple[int, int]:
        now_m = int(self.clock() // 60)
        hour = sum(c for m, c in self._minutes.items() if m > now_m - 60)
        day = sum(c for m, c in self._minutes.items() if m > now_m - 1440)
        return hour, day

    def _until_count_drops_locked(self, now: float, window_minutes: int, excess: int) -> float:
        """Seconds until enough old minutes leave the window for the count to drop by `excess` + 1."""
        now_m = int(now // 60)
        dropped = 0
        for m in sorted(m for m in self._minutes if m > now_m - window_minutes):
            dropped += self._minutes[m]
            if dropped > excess:
                return max(1.0, (m + window_minutes) * 60 - now)
        return 60.0


# ---- user's own download limit ---------------------------------------------------------------------------

PERIODS = {"minute": 60, "hour": 3600, "day": 86400}


class FileLimiter:
    """At most max_files photo downloads in any rolling period (e.g. 500 per hour), chosen by the user.

    acquire() is called before each photo download, from the download threads. When the limit is reached it
    waits until the oldest download in the window is a full period old. cancel interrupts the wait (raising
    the given exception), and on_wait(seconds) is called when a wait starts so the window can show it.
    Only actual photo downloads count; size checks and folder listings don't.
    """

    def __init__(self, max_files: int, period_seconds: float, cancel: threading.Event | None = None,
                 on_wait=None, clock=time.time, cancelled_error: type[Exception] = RuntimeError):
        if max_files < 1 or period_seconds <= 0:
            raise ValueError("max_files must be at least 1 and the period positive")
        self.max_files, self.period = max_files, period_seconds
        self.cancel = cancel or threading.Event()
        self.on_wait = on_wait or (lambda seconds: None)
        self.clock, self.cancelled_error = clock, cancelled_error
        self._lock = threading.Lock()
        self._times: list[float] = []          # start times of downloads in the current window, oldest first
        self.waiting_until: float | None = None
        self._announced: float | None = None      # end of the wait already reported, so it's said only once

    def acquire(self) -> None:
        while True:
            announce = False
            with self._lock:
                now = self.clock()
                cutoff = now - self.period
                while self._times and self._times[0] <= cutoff:
                    self._times.pop(0)
                if len(self._times) < self.max_files:
                    self._times.append(now)
                    self.waiting_until = None
                    return
                until = self._times[0] + self.period
                self.waiting_until = until
                if self._announced is None or until > self._announced + 1:
                    self._announced, announce = until, True
            if announce:
                self.on_wait(until - now)
            if self.cancel.wait(min(1.0, max(0.05, until - now))):
                self.waiting_until = None
                raise self.cancelled_error("stopped while waiting for the download limit")

    def in_window(self) -> int:
        with self._lock:
            cutoff = self.clock() - self.period
            return sum(1 for t in self._times if t > cutoff)
