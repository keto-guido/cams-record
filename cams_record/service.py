"""Wires config, database, indexer, recorders, retention and the startup reconcile together."""

from __future__ import annotations

import logging
import shutil
import threading
import time

from . import __version__
from .config import CameraConfig, stream_url
from .db import Database, DBUnavailable
from .diskguard import CHECK_INTERVAL, LEVEL_OK, DiskGuard
from .indexer import Indexer
from .protection import DetectionProtection
from .push import PushNotifier, PushService
from .reconcile import reconcile
from .recorder import StreamRecorder
from .retention import Janitor

log = logging.getLogger(__name__)


class Service:
    def __init__(self, cfg: dict, cameras: dict[str, CameraConfig], db: Database | None = None):
        self.cfg = cfg
        self.cameras = cameras
        self.rec_cfg = cfg["recording"]
        self.root = str(self.rec_cfg["root"]).rstrip("/")
        self.db = db
        self.indexer = Indexer(db, self.rec_cfg.get("ffprobe", "ffprobe"), int(self.rec_cfg.get("ffprobe_nice", 10)))
        self.guard = DiskGuard(self.root, self.rec_cfg["min_free_gb"], self.rec_cfg["resume_free_gb"])
        self.recorders: dict[str, StreamRecorder] = {}
        for cam in cameras.values():
            for quality, stream in cam.streams():
                url = stream_url(cfg["go2rtc"]["rtsp_url"], stream)
                rec = StreamRecorder(cam.id, quality, url, self.rec_cfg, self.indexer, self.guard)
                self.recorders[rec.key] = rec
        self.janitor = Janitor(db, self.root, cfg["retention"], int(self.rec_cfg["segment_time"]))
        self.protection = DetectionProtection(db, cfg["detection_protection"])
        self.push = PushService(db, cfg.get("push") or {})
        self.push_notifier = PushNotifier(self.push, db, cfg.get("push") or {})
        self._guard_seen = LEVEL_OK
        self.started_at = time.time()
        self.reconcile_result: dict | None = None
        self._stop = threading.Event()
        self._bg: threading.Thread | None = None
        self._guard_thread: threading.Thread | None = None

    def start(self, run_reconcile: bool = True) -> None:
        self.started_at = time.time()
        self.indexer.start()
        self.protection.start()
        self.push_notifier.start()
        self.guard.check(startup=True)  # before any recorder starts: refuse them below the minimum
        for rec in self.recorders.values():
            rec.start()
        self._guard_thread = threading.Thread(target=self._guard_loop, name="disk-guard", daemon=True)
        self._guard_thread.start()
        log.info("cams-record %s: %d streams on %d cameras -> %s", __version__, len(self.recorders),
                 sum(1 for c in self.cameras.values() if c.streams()), self.root)
        if self.push.enabled:
            log.info("web push: enabled (poll=%s)",
                     bool((self.cfg.get("push") or {}).get("poll_enabled", True)))
        if self.db is not None:
            self.janitor.start()
            self._bg = threading.Thread(target=self._startup_db, args=(run_reconcile,), name="startup-db",
                                        daemon=True)
            self._bg.start()

    def _guard_loop(self) -> None:
        while not self._stop.wait(CHECK_INTERVAL):
            self._guard_tick()

    def _guard_tick(self) -> None:
        """Check the disk; run the janitor at once when the guard trips, and while it stays tripped with a backlog.

        Recorders call ``guard.check()`` too, so the level is compared with what this loop saw last.
        """
        level = self.guard.check()
        if level > self._guard_seen or (level > LEVEL_OK and self.janitor.backlog):
            self.janitor.trigger()
        self._guard_seen = level

    def _startup_db(self, run_reconcile: bool) -> None:
        """Sync recorder_cameras and run the reconcile once the DB is reachable (retries with backoff)."""
        delay = 2.0
        while not self._stop.is_set():
            try:
                self.db.upsert_cameras(self.cameras.values())
                if run_reconcile:
                    self.reconcile_result = reconcile(self.root, self.db, self.indexer, cutoff=self.started_at)
                return
            except DBUnavailable as exc:
                log.warning("startup: database unavailable (%s); retrying in %.0fs", exc, delay)
            except Exception:  # noqa: BLE001
                log.exception("startup: reconcile failed; retrying in %.0fs", delay)
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, 60.0)

    def stop(self) -> None:
        self._stop.set()
        if self._guard_thread is not None:
            self._guard_thread.join(timeout=5)
        self.protection.stop()
        self.push_notifier.stop()
        self.janitor.stop()
        for rec in self.recorders.values():
            rec.stop()
        for rec in self.recorders.values():
            rec.join(timeout=float(self.rec_cfg.get("stop_timeout", 10)) + 5)
        self.indexer.stop(timeout=30)
        if self.db is not None:
            self.db.close()

    # -- status --------------------------------------------------------------
    def disk(self) -> dict:
        guard = {"guard": self.guard.state, "min_free_gb": self.rec_cfg["min_free_gb"],
                 "resume_free_gb": self.rec_cfg["resume_free_gb"]}
        try:
            u = shutil.disk_usage(self.root)
            return {"path": self.root, "total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1),
                    "free_pct": round(100 * u.free / u.total, 1) if u.total else None, **guard}
        except OSError as exc:
            return {"path": self.root, "error": str(exc), **guard}

    def status(self) -> dict:
        streams = [r.status() for r in self.recorders.values()]
        return {
            "version": __version__,
            "uptime_s": round(time.time() - self.started_at),
            "segment_time": int(self.rec_cfg["segment_time"]),
            "disk": self.disk(),
            "streams": streams,
            "mb_per_hour_total": round(sum(s["mb_per_hour"] for s in streams), 1),
            "indexer": {"pending": self.indexer.pending(), "indexed": self.indexer.indexed,
                        "probe_errors": self.indexer.probe_errors, "db_errors": self.indexer.db_errors},
            "db": self.db.status() if self.db is not None else None,
            "reconcile": self.reconcile_result,
            "retention": self.janitor.status(),
        }

    def metrics_text(self) -> str:
        lines = [
            "# TYPE cams_record_restarts_total counter",
            "# TYPE cams_record_segments_total counter",
            "# TYPE cams_record_last_segment_age_seconds gauge",
            "# TYPE cams_record_recording gauge",
            "# TYPE cams_record_disk_full gauge",
            "# TYPE cams_record_disk_guard_level gauge",
        ]
        for r in self.recorders.values():
            s = r.status()
            lbl = f'camera="{r.camera_id}",quality="{r.quality}"'
            lines.append(f"cams_record_restarts_total{{{lbl}}} {s['restarts_total']}")
            lines.append(f"cams_record_segments_total{{{lbl}}} {s['segments_total']}")
            if s["last_segment_age_s"] is not None:
                lines.append(f"cams_record_last_segment_age_seconds{{{lbl}}} {s['last_segment_age_s']}")
            lines.append(f"cams_record_recording{{{lbl}}} {1 if s['state'] == 'recording' else 0}")
            lines.append(f"cams_record_disk_full{{{lbl}}} {1 if s['state'] == 'disk_full' else 0}")
        lines.append(f"cams_record_disk_guard_level {self.guard.level}")
        d = self.disk()
        if "free_gb" in d:
            lines.append(f"cams_record_disk_free_bytes {int(d['free_gb'] * 1e9)}")
        lines.append(f"cams_record_index_pending {self.indexer.pending()}")
        lines.extend(self.janitor.metrics_lines())
        return "\n".join(lines) + "\n"
