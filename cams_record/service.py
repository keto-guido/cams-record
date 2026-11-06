"""Wires config, database, indexer, recorders, retention and the startup reconcile together."""

from __future__ import annotations

import logging
import shutil
import threading
import time

from . import __version__
from .camera_access import CameraAccess
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
    def __init__(self, cfg: dict, cameras: dict[str, CameraConfig], db: Database | None = None,
                 source=None, *, file_cameras: dict[str, CameraConfig] | None = None):
        """``cameras`` is what to record now. With camera_source shadow or db, ``source`` is the
        CameraSource and ``file_cameras`` what cameras.yaml says (shadow compares against it)."""
        self.cfg = cfg
        self.cameras = cameras
        self.mode = cfg.get("camera_source", "file")
        self.source = source if self.mode != "file" else None
        self.file_cameras = cameras if file_cameras is None else file_cameras
        self.file_retention = dict(cfg["retention"].get("cameras") or {})
        self.shadow_diffs: list[str] | None = None
        self.registry_status: dict = {"enabled": bool(cfg["access"].get("publish_registry")), "error": None,
                                      "published_revision": None}
        self._cam_lock = threading.RLock()
        self._started = False
        self.rec_cfg = cfg["recording"]
        self.root = str(self.rec_cfg["root"]).rstrip("/")
        self.db = db
        self.indexer = Indexer(db, self.rec_cfg.get("ffprobe", "ffprobe"), int(self.rec_cfg.get("ffprobe_nice", 10)))
        self.guard = DiskGuard(self.root, self.rec_cfg["min_free_gb"], self.rec_cfg["resume_free_gb"])
        self.recorders: dict[str, StreamRecorder] = {}
        for key, (cam_id, quality, url) in self._wanted_streams(cameras).items():
            self.recorders[key] = StreamRecorder(cam_id, quality, url, self.rec_cfg, self.indexer, self.guard)
        self.janitor = Janitor(db, self.root, cfg["retention"], int(self.rec_cfg["segment_time"]))
        self.protection = DetectionProtection(db, cfg["detection_protection"])
        # One registry reader shared by the HTTP routes and detection pushes.
        self.access = CameraAccess(cfg["access"], cameras)
        self.push = PushService(db, cfg.get("push") or {}, access=self.access)
        self.push_notifier = PushNotifier(self.push, db, cfg.get("push") or {})
        self._guard_seen = LEVEL_OK
        self.started_at = time.time()
        self.reconcile_result: dict | None = None
        self._stop = threading.Event()
        self._bg: threading.Thread | None = None
        self._guard_thread: threading.Thread | None = None
        self._source_thread: threading.Thread | None = None

    def _wanted_streams(self, cameras: dict[str, CameraConfig]) -> dict[str, tuple[str, str, str]]:
        """recorder key -> (camera id, quality, url) for every stream these cameras record."""
        out = {}
        for cam in cameras.values():
            for quality, stream in cam.streams():
                out[f"{cam.id}/{quality}"] = (cam.id, quality, stream_url(self.cfg["go2rtc"]["rtsp_url"], stream))
        return out

    # -- cams_config -----------------------------------------------------------
    def apply_cameras(self, cameras: dict[str, CameraConfig], retention_overrides: dict) -> dict:
        """Switch to a new camera list, touching only recorders whose stream changed.

        A changed stream is stopped (and its ffmpeg joined) before its replacement starts, so two
        processes never write the same segment files. Returns {"stopped": [...], "started": [...]}.
        """
        with self._cam_lock:
            wanted = self._wanted_streams(cameras)
            stopped, started = [], []
            for key, rec in list(self.recorders.items()):
                want = wanted.get(key)
                if want is None or want[2] != rec.url:
                    rec.stop()
                    rec.join(timeout=float(self.rec_cfg.get("stop_timeout", 10)) + 5)
                    del self.recorders[key]
                    stopped.append(key)
            for key, (cam_id, quality, url) in wanted.items():
                if key in self.recorders:
                    continue
                rec = StreamRecorder(cam_id, quality, url, self.rec_cfg, self.indexer, self.guard)
                self.recorders[key] = rec
                if self._started:
                    rec.start()
                started.append(key)
            self.cameras = cameras
            self.access.set_cameras(cameras)
            self.janitor.overrides = dict(retention_overrides)
            if stopped or started:
                log.info("cameras changed: stopped %s, started %s", stopped or "none", started or "none")
        if self.db is not None:
            try:
                self.db.upsert_cameras(cameras.values())
            except DBUnavailable as exc:
                log.warning("recorder_cameras not updated (database unavailable): %s", exc)
        return {"stopped": stopped, "started": started}

    def on_camera_objects(self) -> None:
        """React to the camera objects currently loaded in self.source (after load or a revision change)."""
        from .camconfig.compare import record_differences
        from .camconfig.publish import publish_registry
        from .camconfig.views import record_camera

        src = self.source
        if src is None or src.origin is None:
            return
        objects = dict(src.cameras)
        if self.mode == "shadow":
            diffs = record_differences(self.file_cameras, self.file_retention, objects)
            if diffs != self.shadow_diffs:
                if diffs:
                    log.warning("cams_config shadow (revision %s): %d differences from cameras.yaml",
                                src.revision, len(diffs))
                    for line in diffs:
                        log.warning("cams_config shadow: %s", line)
                else:
                    log.info("cams_config shadow (revision %s): no differences from cameras.yaml", src.revision)
            self.shadow_diffs = diffs
            return
        cams = {cid: record_camera(o) for cid, o in objects.items()}
        overrides = {cid: dict(o.recording) for cid, o in objects.items() if o.recording}
        self.apply_cameras(cams, overrides)
        if self.registry_status["enabled"]:
            try:
                publish_registry(self.cfg["access"]["registry_file"], objects)
                self.registry_status.update(error=None, published_revision=src.revision)
            except (OSError, ValueError) as exc:
                if self.registry_status["error"] != str(exc):
                    log.error("cams.json not published (the previous file stays in place): %s", exc)
                self.registry_status["error"] = str(exc)

    def _registry_needs_retry(self) -> bool:
        """True when a previous cams.json publish failed and should be attempted again."""
        return bool(self.registry_status["enabled"] and self.registry_status["error"])

    def _source_loop(self) -> None:
        self.on_camera_objects()  # initial state: shadow report, or publish the registry
        interval = float(self.cfg["cams_config"]["poll_s"])
        while not self._stop.wait(interval):
            try:
                # Retry a failed registry write even when camera objects/revision did not change,
                # so revocations cannot stay stale after a transient filesystem error.
                if self.source.poll() or self._registry_needs_retry():
                    self.on_camera_objects()
            except Exception:  # noqa: BLE001  (never let this thread die)
                log.exception("cams_config: applying camera objects failed")

    def start(self, run_reconcile: bool = True) -> None:
        self.started_at = time.time()
        self._started = True
        self.indexer.start()
        self.protection.start()
        self.push_notifier.start()
        self.guard.check(startup=True)  # before any recorder starts: refuse them below the minimum
        with self._cam_lock:
            for rec in self.recorders.values():
                rec.start()
        self._guard_thread = threading.Thread(target=self._guard_loop, name="disk-guard", daemon=True)
        self._guard_thread.start()
        if self.source is not None:
            log.info("camera_source: %s (cams_config revision %s from %s)", self.mode, self.source.revision,
                     self.source.origin or "nowhere yet")
            self._source_thread = threading.Thread(target=self._source_loop, name="cams-config", daemon=True)
            self._source_thread.start()
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
        if self._source_thread is not None:
            self._source_thread.join(timeout=30)
        self.protection.stop()
        self.push_notifier.stop()
        self.janitor.stop()
        with self._cam_lock:
            recorders = list(self.recorders.values())
        for rec in recorders:
            rec.stop()
        for rec in recorders:
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
        with self._cam_lock:
            recorders = list(self.recorders.values())
        streams = [r.status() for r in recorders]
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
            "camera_source": self.camera_source_status(),
        }

    def camera_source_status(self) -> dict:
        out: dict = {"mode": self.mode}
        if self.source is not None:
            out.update(self.source.status())
        if self.mode == "shadow":
            out["differences"] = self.shadow_diffs
        if self.mode == "db":
            out["registry"] = dict(self.registry_status)
        return out

    def metrics_text(self) -> str:
        lines = [
            "# TYPE cams_record_restarts_total counter",
            "# TYPE cams_record_segments_total counter",
            "# TYPE cams_record_last_segment_age_seconds gauge",
            "# TYPE cams_record_recording gauge",
            "# TYPE cams_record_disk_full gauge",
            "# TYPE cams_record_disk_guard_level gauge",
        ]
        with self._cam_lock:
            recorders = list(self.recorders.values())
        for r in recorders:
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
