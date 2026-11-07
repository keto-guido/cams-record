"""Per-camera pause of phone (Web Push) alerts.

A pause only stops Web Push for detections on that camera. Recording, detection, the event log and
cams-detect's MQTT topics (and every Home Assistant automation on them) carry on unchanged.

Rows live in MariaDB ``push_pauses`` (db.py migration 8). Pushes are claimed while a camera is paused,
so resuming never releases a backlog of old alerts.

Lookups run on every detection, so the active set is cached in memory:

* a pause or resume made through this process (HTTP API, MQTT) replaces the cache at once;
* an entry is dropped the moment its ``until_ts`` passes, without a query;
* otherwise the table is re-read at most every ``refresh_s`` seconds, which picks up rows written by
  another process (a second instance, a manual SQL fix).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MORNING_HOUR = 7
MAX_PAUSE_S = 7 * 86400
SET_BY_MAX = 128
# Named durations accepted by the API and MQTT. "morning" is the next 07:00 local time.
PRESETS = {"1h": 3600, "3h": 3 * 3600}
_SECONDS_RE = re.compile(r"^[0-9]{1,7}$")


class PauseError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Pause:
    camera_id: str
    until_ts: float
    set_by: str
    set_at: float

    def active(self, now: float | None = None) -> bool:
        return self.until_ts > (time.time() if now is None else now)


def next_morning(now: float, tz: ZoneInfo, hour: int = MORNING_HOUR) -> float:
    """Epoch of the next ``hour``:00 wall-clock time in ``tz`` strictly after ``now``.

    Built from the local calendar date rather than ``now + 24 h``, so the night a DST change happens
    still ends at 07:00 local (a 23 h or 25 h night). 07:00 is never inside a US DST gap or overlap.
    """
    local = datetime.fromtimestamp(now, tz)
    target = datetime(local.year, local.month, local.day, hour, tzinfo=tz)
    if target.timestamp() <= now:
        day = local.date() + timedelta(days=1)
        target = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
    return target.timestamp()


def resolve_until(duration, now: float, tz: ZoneInfo) -> float:
    """Validate a requested duration and return the epoch the pause ends.

    Accepts ``"1h"``, ``"3h"``, ``"morning"`` or a positive whole number of seconds (int, or a string of
    digits) up to 7 days. Anything else raises PauseError(422).
    """
    if isinstance(duration, str):
        key = duration.strip().lower().replace(" ", "")
        if key in PRESETS:
            return now + PRESETS[key]
        if key in ("morning", "untilmorning"):
            return next_morning(now, tz)
        if _SECONDS_RE.fullmatch(key):
            duration = int(key)
        else:
            raise PauseError(422, 'duration must be "1h", "3h", "morning" or a number of seconds')
    if isinstance(duration, bool) or not isinstance(duration, int | float):
        raise PauseError(422, 'duration must be "1h", "3h", "morning" or a number of seconds')
    if not math.isfinite(duration) or duration != int(duration) or not 1 <= duration <= MAX_PAUSE_S:
        raise PauseError(422, f"duration seconds must be a whole number from 1 to {MAX_PAUSE_S}")
    return now + int(duration)


class PushPauses:
    """Cached view of ``push_pauses`` plus the pause/resume writes. Thread-safe."""

    def __init__(self, db, timezone: str = "America/Chicago", refresh_s: float = 30.0,
                 clock: Callable[[], float] = time.time):
        self.db = db
        self.tz = ZoneInfo(timezone)
        self.refresh_s = float(refresh_s)
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, Pause] = {}
        self._loaded_at: float | None = None  # monotonic
        self._listeners: list[Callable[[str], None]] = []

    # -- change notification (MQTT state) ------------------------------------
    def subscribe(self, fn: Callable[[str], None]) -> None:
        """fn(camera_id) after a pause or resume made through this process."""
        self._listeners.append(fn)

    def _changed(self, camera_id: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(camera_id)
            except Exception:  # noqa: BLE001  (a listener must never break an API write)
                log.exception("pauses: change listener failed")

    # -- reads -----------------------------------------------------------------
    def _load(self) -> None:
        rows = self.db.active_push_pauses()
        self._cache = {r["camera_id"]: Pause(r["camera_id"], float(r["until_ts"]), str(r["set_by"] or ""),
                                             float(r["set_at"] or 0.0)) for r in rows}
        self._loaded_at = time.monotonic()

    def active(self, *, fresh: bool = False) -> dict[str, Pause]:
        """camera_id -> Pause for every unexpired pause.

        A database error keeps the last known set (alerts fail open, never silently stay paused
        forever), and the next call tries again.
        """
        with self._lock:
            stale = (self._loaded_at is None or fresh
                     or time.monotonic() - self._loaded_at >= self.refresh_s)
            if stale:
                try:
                    self._load()
                except Exception as exc:  # noqa: BLE001  (DBUnavailable, missing table, grants)
                    log.warning("pauses: could not read push_pauses (%s); using the last known pauses", exc)
                    self._loaded_at = time.monotonic()
            now = self.clock()
            expired = [cid for cid, p in self._cache.items() if not p.active(now)]
            for cid in expired:
                del self._cache[cid]
            return dict(self._cache)

    def get(self, camera_id: str) -> Pause | None:
        return self.active().get(camera_id)

    def is_paused(self, camera_id: str) -> bool:
        return self.get(camera_id) is not None

    def next_expiry(self) -> float | None:
        pauses = self.active()
        return min((p.until_ts for p in pauses.values()), default=None)

    # -- writes ----------------------------------------------------------------
    def pause(self, camera_id: str, duration, set_by: str) -> Pause:
        now = self.clock()
        until = resolve_until(duration, now, self.tz)
        who = (str(set_by or "") or "unknown")[:SET_BY_MAX]
        self.db.set_push_pause(camera_id, until, who, now)
        p = Pause(camera_id, until, who, now)
        with self._lock:
            self._cache[camera_id] = p
        log.info("pauses: phone alerts for %s paused until %s by %s", camera_id,
                 datetime.fromtimestamp(until, self.tz).isoformat(timespec="minutes"), who)
        self._changed(camera_id)
        return p

    def resume(self, camera_id: str, by: str) -> bool:
        n = self.db.clear_push_pause(camera_id)
        with self._lock:
            had = self._cache.pop(camera_id, None) is not None
        if n or had:
            log.info("pauses: phone alerts for %s resumed by %s", camera_id, (str(by or "") or "unknown")[:SET_BY_MAX])
        self._changed(camera_id)
        return bool(n or had)

    # -- presentation ----------------------------------------------------------
    def iso(self, ts: float | None) -> str | None:
        return None if ts is None else datetime.fromtimestamp(ts, self.tz).isoformat(timespec="seconds")

    def mode_of(self, p: Pause | None) -> str | None:
        """Which preset a pause came from ("1h", "3h", "morning"), or None for a custom length."""
        if p is None:
            return None
        length = p.until_ts - p.set_at
        for key, seconds in PRESETS.items():
            if abs(length - seconds) < 1:
                return key
        if p.set_at and abs(p.until_ts - next_morning(p.set_at, self.tz)) < 1:
            return "morning"
        return None
