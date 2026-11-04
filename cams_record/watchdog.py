"""Restart policy for ffmpeg: exponential backoff and the stall watchdog (pure logic, injectable clock)."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

Clock = Callable[[], float]


class Backoff:
    """1 s, 2 s, 4 s ... capped at ``maximum``; ``reset()`` after a healthy run."""

    def __init__(self, initial: float = 1.0, maximum: float = 60.0):
        self.initial = float(initial)
        self.maximum = float(maximum)
        self._next = self.initial

    def next(self) -> float:
        delay = self._next
        self._next = min(self._next * 2, self.maximum)
        return delay

    def reset(self) -> None:
        self._next = self.initial

    @property
    def upcoming(self) -> float:
        return self._next


class Watchdog:
    """Stalled when no segment closed within ``factor * segment_time`` of the last one (or of start).

    Measured on a monotonic clock so wall-clock jumps (NTP) never trigger restarts.
    """

    def __init__(self, segment_time: float, factor: float = 3.0, clock: Clock = time.monotonic):
        self.limit = float(segment_time) * float(factor)
        self.clock = clock
        self._last = clock()

    def started(self) -> None:
        self._last = self.clock()

    def segment(self) -> None:
        self._last = self.clock()

    def idle_for(self) -> float:
        return self.clock() - self._last

    def stalled(self) -> bool:
        return self.idle_for() > self.limit


class RestartCounter:
    """Restarts in the trailing 24 h (wall clock, for status/metrics) plus a lifetime total."""

    WINDOW = 24 * 3600

    def __init__(self, clock: Clock = time.time):
        self.clock = clock
        self.total = 0
        self._times: deque[float] = deque()

    def add(self) -> None:
        self.total += 1
        self._times.append(self.clock())
        self._trim()

    def _trim(self) -> None:
        cutoff = self.clock() - self.WINDOW
        while self._times and self._times[0] < cutoff:
            self._times.popleft()

    def last_24h(self) -> int:
        self._trim()
        return len(self._times)
