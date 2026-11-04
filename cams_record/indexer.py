"""Indexer: one thread, one ffprobe at a time (nice 10), then INSERT into ``segments``.

Live segments (closed by ffmpeg) have priority over reconcile work. Recorder status rows are
coalesced in memory and written by the same thread, so recorder threads never block on the DB.
If the database is down the current job is retried with backoff (nothing is dropped; anything
lost to a crash is picked up by the startup reconcile).
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

from .db import Database, DBUnavailable
from .probe import ProbeError, probe_segment

log = logging.getLogger(__name__)

PRIO_LIVE, PRIO_RECONCILE = 0, 1
BAD_SUFFIX = ".bad"


@dataclass
class SegmentJob:
    camera_id: str
    quality: str
    abs_path: str
    rel_path: str
    name_ts: float  # from the file name
    start_ts: float | None = None  # from the segment list (live), else None
    end_ts: float | None = None


def build_row(job: SegmentJob, duration: float, size: int, codec, width, height, fps) -> dict:
    if job.start_ts is not None and job.end_ts is not None and job.end_ts > job.start_ts:
        start, end = job.start_ts, job.end_ts
        if abs((end - start) - duration) > 2.0:  # segment list disagrees with the file: trust the file
            end = start + duration
    else:
        start, end = job.name_ts, job.name_ts + duration
    return {"camera_id": job.camera_id, "quality": job.quality, "start_ts": round(start, 3),
            "end_ts": round(end, 3), "duration_ms": int(round((end - start) * 1000)), "path": job.rel_path,
            "size_bytes": int(size), "codec": codec, "width": width, "height": height, "fps": fps}


class Indexer:
    def __init__(self, db: Database | None, ffprobe: str = "ffprobe", nice: int = 10):
        self.db = db
        self.ffprobe = ffprobe
        self.nice = nice
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._status: dict[tuple[str, str], dict] = {}
        self._status_lock = threading.Lock()
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._thread: threading.Thread | None = None
        self.indexed = 0
        self.probe_errors = 0
        self.db_errors = 0

    # -- producer API ------------------------------------------------------
    def submit(self, job: SegmentJob, priority: int = PRIO_LIVE) -> None:
        self._q.put((priority, next(self._seq), job))

    def set_status(self, row: dict) -> None:
        with self._status_lock:
            self._status[(row["camera_id"], row["quality"])] = dict(row)

    def pending(self) -> int:
        return self._q.qsize()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="indexer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Finish queued live jobs (bounded by timeout), flush statuses, stop."""
        deadline = time.monotonic() + timeout
        while (self._q.qsize() or self._busy.is_set()) and time.monotonic() < deadline:
            time.sleep(0.1)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1.0, deadline - time.monotonic()))
        self._flush_status()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._q.qsize() and not self._busy.is_set():
                return True
            time.sleep(0.05)
        return False

    # -- worker ------------------------------------------------------------
    def _run(self) -> None:
        last_flush = 0.0
        while not self._stop.is_set():
            if time.monotonic() - last_flush > 5:
                self._flush_status()
                last_flush = time.monotonic()
            try:
                _, _, job = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            self._busy.set()
            try:
                self.process(job)
            except Exception:  # noqa: BLE001 - never let one file kill the indexer
                log.exception("indexer: unexpected error on %s", job.rel_path)
            finally:
                self._busy.clear()

    def process(self, job: SegmentJob) -> dict | None:
        try:
            info = probe_segment(job.abs_path, self.ffprobe, self.nice)
        except ProbeError as exc:
            self.probe_errors += 1
            if os.path.exists(job.abs_path):
                bad = job.abs_path + BAD_SUFFIX
                try:
                    os.replace(job.abs_path, bad)
                    log.warning("indexer: %s unreadable (%s); renamed to %s", job.rel_path, exc, bad)
                except OSError as e2:
                    log.warning("indexer: %s unreadable (%s); rename failed: %s", job.rel_path, exc, e2)
            return None
        row = build_row(job, info.duration, info.size_bytes, info.codec, info.width, info.height, info.fps)
        if self.db is None:
            self.indexed += 1
            return row
        backoff = 1.0
        while True:
            try:
                self.db.insert_segment(row)
                self.indexed += 1
                return row
            except DBUnavailable as exc:
                self.db_errors += 1
                if self.db_errors == 1 or self.db_errors % 30 == 0:
                    log.warning("indexer: database unavailable (%s); %d segments waiting", exc, self.pending() + 1)
                if self._stop.wait(backoff):
                    return None
                backoff = min(backoff * 2, 30.0)
            except Exception as exc:  # noqa: BLE001 - bad row: log and drop, reconcile can retry later
                log.error("indexer: insert failed for %s: %s", job.rel_path, exc)
                return None

    def _flush_status(self) -> None:
        if self.db is None:
            return
        with self._status_lock:
            rows = list(self._status.values())
            self._status.clear()
        if not rows:
            return
        try:
            self.db.upsert_status(rows)
        except Exception as exc:  # noqa: BLE001
            with self._status_lock:  # keep the newest version of each row for the next try
                for r in rows:
                    self._status.setdefault((r["camera_id"], r["quality"]), r)
            log.debug("indexer: status flush failed: %s", exc)
