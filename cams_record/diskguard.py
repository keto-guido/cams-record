"""Disk guard: stop recording before the volume fills (pure logic, injectable disk usage).

Levels, with hysteresis so recorders do not flap around a threshold:

- ``LEVEL_OK``: everything records.
- ``LEVEL_MAIN_STOPPED``: free space fell below ``min_free_gb``; main stops, sub keeps running.
- ``LEVEL_ALL_STOPPED``: free space fell below half of ``min_free_gb``; sub stops too.

The level only drops back to ``LEVEL_OK``, once free space rises above ``resume_free_gb``. A service
that starts below the minimum goes straight to ``LEVEL_ALL_STOPPED``: no recorder may start there.
An already running sub, by contrast, may still respawn its ffmpeg at ``LEVEL_MAIN_STOPPED``.

Sizes are decimal GB (1e9 bytes), the same unit as ``disk`` in /api/status. If the usage cannot be
read the level is left unchanged: an unreadable path must not stop an otherwise healthy NVR.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30.0  # seconds
GB = 1e9

LEVEL_OK = 0
LEVEL_MAIN_STOPPED = 1
LEVEL_ALL_STOPPED = 2
LEVEL_NAMES = {LEVEL_OK: "ok", LEVEL_MAIN_STOPPED: "main_stopped", LEVEL_ALL_STOPPED: "all_stopped"}


class DiskGuard:
    def __init__(self, root: str, min_free_gb: float, resume_free_gb: float,
                 free_bytes: Callable[[str], int] | None = None):
        self.root = root
        self.min_free = float(min_free_gb) * GB
        self.resume_free = float(resume_free_gb) * GB
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._lock = threading.Lock()
        self.level = LEVEL_OK

    def _next_level(self, free: float, startup: bool) -> int:
        if free < self.min_free / 2 or (startup and free < self.min_free):
            return LEVEL_ALL_STOPPED
        if free < self.min_free:
            return max(self.level, LEVEL_MAIN_STOPPED)
        if free > self.resume_free:
            return LEVEL_OK
        return self.level

    def check(self, startup: bool = False) -> int:
        """Re-read the free space and return the (possibly new) level."""
        with self._lock:
            try:
                free = self._free_bytes(self.root)
            except OSError as exc:
                log.warning("disk guard: cannot read free space on %s: %s", self.root, exc)
                return self.level
            level = self._next_level(free, startup)
            if level > self.level:
                log.error("disk guard: %.1f GB free on %s (minimum %.0f GB): %s", free / GB, self.root,
                          self.min_free / GB, LEVEL_NAMES[level])
            elif level < self.level:
                log.warning("disk guard: %.1f GB free on %s (resume above %.0f GB): recording resumes",
                            free / GB, self.root, self.resume_free / GB)
            self.level = level
            return level

    def blocked(self, quality: str) -> bool:
        """True when this quality must not record at the current level."""
        return self.level >= (LEVEL_MAIN_STOPPED if quality == "main" else LEVEL_ALL_STOPPED)

    @property
    def state(self) -> str:
        return LEVEL_NAMES[self.level]
