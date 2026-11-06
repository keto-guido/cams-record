"""Camera objects for a running service: cams_config, with a local last-good cache.

* ``load_initial()`` reads the database, or the cache file when the database is unreachable.
* ``poll()`` checks the revision (one tiny query) and reloads only when it changed.
* Every successful load rewrites the cache atomically, so a restart during a database outage
  still starts from the newest camera objects this service has seen.
* A failed poll never drops what is loaded: recording and detection keep running on it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from .model import CameraObject
from .store import CamConfigStore, open_db

log = logging.getLogger(__name__)

MODES = ("file", "shadow", "db")


class CameraSource:
    def __init__(self, db_cfg: dict, cache_file: str | Path | None, store: CamConfigStore | None = None):
        self.store = store or CamConfigStore(open_db(db_cfg))
        self.cache_file = Path(cache_file) if cache_file else None
        self.lock = threading.Lock()
        self.revision: int | None = None
        self.cameras: dict[str, CameraObject] = {}
        self.origin: str | None = None  # "db" | "cache" | None (nothing loaded)
        self.loaded_at: float | None = None
        self.last_ok_at: float | None = None  # last successful contact with the database
        self.last_error = ""
        self._error_logged = False

    # -- loading --------------------------------------------------------------
    def load_initial(self) -> bool:
        """Load from the database, else the cache. True if camera objects are available."""
        try:
            self._load_db()
            return True
        except Exception as exc:  # noqa: BLE001  (DB down, grants missing, schema not created yet)
            self._failed(exc)
        cached = self._read_cache()
        if cached is None:
            return False
        rev, cams = cached
        with self.lock:
            self.revision, self.cameras, self.origin, self.loaded_at = rev, cams, "cache", time.time()
        log.warning("cams_config: database unreachable, using cached camera objects (revision %s, %d cameras)",
                    rev, len(cams))
        return True

    def poll(self) -> bool:
        """Reload if the revision changed (or we are running on the cache). True when cameras changed."""
        try:
            rev = self.store.revision()
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)
            return False
        self.last_ok_at = time.time()
        if self._error_logged:
            log.info("cams_config: database reachable again")
            self._error_logged = False
        self.last_error = ""
        if rev == self.revision and self.origin == "db":
            return False
        # First successful load from an uninitialized source must apply even when the DB is empty,
        # so db mode can drop the cameras.yaml fallback.
        first_load = self.origin is None
        before = {k: c.to_dict() for k, c in self.cameras.items()}
        try:
            self._load_db()
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)
            return False
        if first_load:
            return True
        # A revision bump that changed nothing (e.g. an identical save) needs no re-apply.
        return before != {k: c.to_dict() for k, c in self.cameras.items()}

    def _load_db(self) -> None:
        rev, cams = self.store.load_all()
        now = time.time()
        with self.lock:
            self.revision, self.cameras, self.origin, self.loaded_at = rev, cams, "db", now
            self.last_ok_at, self.last_error = now, ""
        self._write_cache(rev, cams)

    def _failed(self, exc: Exception) -> None:
        self.last_error = f"{exc.__class__.__name__}: {exc}"[:300]
        if not self._error_logged:
            log.error("cams_config: database unavailable, keeping the camera objects already loaded: %s",
                      self.last_error)
            self._error_logged = True

    # -- cache ------------------------------------------------------------------
    def _write_cache(self, rev: int, cams: dict[str, CameraObject]) -> None:
        if self.cache_file is None:
            return
        data = {"revision": rev, "saved_at": time.time(), "cameras": {k: c.to_dict() for k, c in cams.items()}}
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.cache_file.parent), prefix=".camconfig.", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, sort_keys=True)
                os.replace(tmp, self.cache_file)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.warning("cams_config: could not write cache %s: %s", self.cache_file, exc)

    def _read_cache(self) -> tuple[int, dict[str, CameraObject]] | None:
        if self.cache_file is None or not self.cache_file.exists():
            return None
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            cams = {k: CameraObject.from_dict(v) for k, v in data["cameras"].items()}
            bad = [e for c in cams.values() for e in c.validate()]
            if bad:
                raise ValueError("; ".join(bad))
            return int(data["revision"]), cams
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.error("cams_config: cache %s unusable: %s", self.cache_file, exc)
            return None

    # -- status -----------------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        return {
            "revision": self.revision,
            "origin": self.origin,
            "cameras": len(self.cameras),
            "loaded_age_s": round(now - self.loaded_at) if self.loaded_at else None,
            "db_ok_age_s": round(now - self.last_ok_at) if self.last_ok_at else None,
            "error": self.last_error or None,
        }
