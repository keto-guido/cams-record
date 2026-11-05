"""Retention janitor: deletes old recordings, in bounded batches, from the ``segments`` table only.

Each pass selects, oldest first and at most ``batch_size`` segments:

1. **Age rule:** segments older than ``main_days``/``sub_days``, with per-camera overrides.
2. **Budget rule:** while the indexed bytes left exceed ``max_total_gb``, the oldest main segments,
   then the oldest sub segments.

Protected segments and segments that closed within the last 2 x segment_time are never selected.
The filesystem is never walked to find files: only indexed rows are deleted, and every path has to
pass ``resolve_segment_path`` first. A row that fails is logged and left alone, and is passed over
for an hour so that it cannot hold up the oldest-first queue.

The file is unlinked first, then its row is marked ``deleted_at``. After a crash between the two the
next pass finds the file gone and marks the row. Rows marked ``missing`` have no file, so they count
towards no budget, but the age rule still retires them.

The pass runs in its own low-priority thread. HTTP handlers read the numbers cached by the last
pass, so ``status()`` touches neither the DB nor the disk.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import nullcontext

from .config import QUALITIES
from .db import Database, DBUnavailable
from .diskguard import GB

log = logging.getLogger(__name__)

EXTENSION = ".mp4"
COMMIT_EVERY = 50  # rows per transaction
SKIP_TTL = 3600.0  # seconds a row that could not be deleted is passed over
FIRST_PASS_DELAY = 60.0  # seconds after start, or interval_s if that is shorter
STOP_TIMEOUT = 15.0
DAY = 86400


class UnsafePath(Exception):
    pass


def resolve_segment_path(real_root: str, rel: str) -> tuple[str, bool]:
    """Return (absolute path, file exists) for an indexed path, or raise UnsafePath.

    ``real_root`` must already be a realpath. Besides lying strictly inside the root, the path may
    not go through any symlink: what gets unlinked has to be the very file the row names.
    """
    if not rel or "\0" in rel or os.path.isabs(rel):
        raise UnsafePath("empty or absolute path")
    if ".." in rel.replace("\\", "/").split("/"):
        raise UnsafePath("parent directory reference")
    joined = os.path.normpath(os.path.join(real_root, rel))
    real = os.path.realpath(joined)
    try:
        inside = real != real_root and os.path.commonpath([real_root, real]) == real_root
    except ValueError:  # different drives (Windows)
        inside = False
    if not inside:
        raise UnsafePath("outside the recording root")
    if real != joined:
        raise UnsafePath("goes through a symlink")
    if os.path.splitext(real)[1] != EXTENSION:
        raise UnsafePath(f"not a {EXTENSION} file")
    try:
        mode = os.lstat(real).st_mode
    except FileNotFoundError:
        return real, False
    except OSError as exc:
        raise UnsafePath(f"cannot stat: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise UnsafePath("not a regular file")
    return real, True


def live_hours(now: float) -> set[str]:
    """``YYYY/MM/DD/HH`` of the hours recorders may be writing to.

    Recorders create these directories ahead of time and remember doing so, so they must stay.
    """
    return {time.strftime("%Y/%m/%d/%H", time.gmtime(now + d)) for d in (-3600, 0, 3600)}


def remove_empty_date_dirs(real_root: str, rel_paths: Iterable[str], keep_hours: set[str]) -> int:
    """rmdir, bottom-up, the empty ``YYYY/MM/DD/HH`` parents of these files.

    The first two levels below the root (``<camera>/<quality>``) always stay, and so does anything
    holding one of ``keep_hours``. ``rel_paths`` must have passed ``resolve_segment_path``.
    """
    removed = 0
    for rel_dir in sorted({os.path.dirname(rel.replace("\\", "/")) for rel in rel_paths}, reverse=True):
        parts = rel_dir.split("/")
        while len(parts) > 2:
            date = "/".join(parts[2:])
            if any(h == date or h.startswith(date + "/") for h in keep_hours):
                break
            try:
                os.rmdir(os.path.join(real_root, *parts))
            except FileNotFoundError:  # already gone with an earlier file of this pass
                pass
            except OSError:  # not empty
                break
            else:
                removed += 1
            parts.pop()
    return removed


def _gb(n: float) -> float:
    return n / GB


class Janitor:
    def __init__(self, db: Database | None, root: str, cfg: dict, segment_time: int,
                 clock: Callable[[], float] = time.time):
        self.db = db
        self.root = root
        self.cfg = cfg
        self.overrides = cfg.get("cameras") or {}
        self.dry_run = cfg["dry_run"]
        self.batch_size = int(cfg["batch_size"])
        self.budget = int(float(cfg["max_total_gb"]) * GB)
        self.open_margin = 2 * int(segment_time)
        self.clock = clock
        self._skip: set[int] = set()
        self._skip_since = 0.0
        self._lock = threading.Lock()  # guards the cached numbers below
        self._pass_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.totals: list[dict] = []
        self.totals_ts: float | None = None
        self.last_run: dict | None = None
        self.deleted_segments = 0
        self.deleted_bytes = 0
        self.skipped_total = 0
        self.errors_total = 0
        self.would_delete: dict | None = None
        self.backlog = False  # the last real pass filled its batch: more is waiting

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="retention", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(STOP_TIMEOUT)

    def trigger(self) -> None:
        """Request an urgent pass now (disk guard tripped), whatever the interval."""
        self._wake.set()

    def _run(self) -> None:
        self._lower_priority()
        interval = float(self.cfg["interval_s"])
        self._wake.wait(min(FIRST_PASS_DELAY, interval))
        while not self._stop.is_set():
            urgent = self._wake.is_set()
            self._wake.clear()
            self.run_once(urgent=urgent)
            self._wake.wait(interval)

    def _lower_priority(self) -> None:
        nice = int(self.cfg.get("nice", 0))
        if nice <= 0:
            return
        try:
            os.nice(nice)  # on Linux this applies to the calling thread only, not the recorders
        except (AttributeError, OSError) as exc:
            log.warning("retention: cannot lower priority: %s", exc)

    # -- settings ----------------------------------------------------------
    def days(self, camera_id: str, quality: str) -> float:
        key = f"{quality}_days"
        return float((self.overrides.get(camera_id) or {}).get(key, self.cfg[key]))

    # -- one pass ----------------------------------------------------------
    def run_once(self, urgent: bool = False) -> dict:
        """One pass. Failures end up in the result: they must never kill the thread."""
        with self._pass_lock:
            started = self.clock()
            t0 = time.monotonic()
            try:
                # Serialize against protection without holding the shared DB lock during I/O.
                with getattr(self.db, "retention_lock", nullcontext()):
                    result = self._pass(started)
            except DBUnavailable as exc:
                log.warning("retention: pass abandoned, database unavailable: %s", exc)
                result = self._failed(str(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("retention: pass failed")
                result = self._failed(str(exc))
            result.update(ts=started, duration_s=round(time.monotonic() - t0, 3), urgent=urgent,
                          dry_run=self.dry_run)
            with self._lock:
                self.last_run = result
            return result

    def _failed(self, error: str) -> dict:
        with self._lock:
            self.errors_total += 1
        return {"result": "error", "error": error[:200], "segments": 0, "bytes": 0}

    def _pass(self, now: float) -> dict:
        real_root = os.path.realpath(self.root)
        if not os.path.isdir(real_root) or os.path.dirname(real_root) == real_root:
            raise ValueError(f"unusable recording root {real_root!r}")
        if now - self._skip_since > SKIP_TTL:
            self._skip.clear()
            self._skip_since = now
        selected = self._select(now, self._refresh_totals(now))
        counts = {f"{rule}_{q}": 0 for rule in ("age", "budget") for q in QUALITIES}
        ready: list[tuple[dict, str, bool]] = []
        skipped = 0
        for seg in selected:
            try:
                path, exists = resolve_segment_path(real_root, seg["path"])
            except UnsafePath as exc:
                log.warning("retention: skipping segment %s (%r): %s", seg["id"], seg["path"], exc)
                self._skip.add(seg["id"])
                skipped += 1
                continue
            ready.append((seg, path, exists))
            counts[f"{seg['rule']}_{seg['quality']}"] += 1
        with self._lock:
            self.skipped_total += skipped
        result = {"segments": len(ready), "bytes": sum(s["size_bytes"] for s, _, e in ready if e),
                  "skipped": skipped, "batch_full": len(selected) >= self.batch_size, **counts}
        if self.dry_run:
            return self._report_dry_run(now, result)
        return self._delete(now, real_root, ready, result)

    def _refresh_totals(self, now: float) -> list[dict]:
        totals = self.db.segment_totals()
        with self._lock:
            self.totals, self.totals_ts = totals, now
        return totals

    def _select(self, now: float, totals: list[dict]) -> list[dict]:
        end_before = now - self.open_margin
        fetch = self.batch_size + len(self._skip)  # skipped rows are still the oldest in the table
        age: list[dict] = []
        for t in totals:
            cutoff = now - self.days(t["camera_id"], t["quality"]) * DAY
            rows = self.db.oldest_segments(t["quality"], end_before, fetch, camera_id=t["camera_id"],
                                           start_before=cutoff)
            age += [{**r, "rule": "age"} for r in rows if r["id"] not in self._skip]
        age.sort(key=lambda s: (s["start_ts"], s["id"]))
        selected = age[:self.batch_size]

        chosen = {s["id"] for s in selected}
        excess = sum(t["bytes"] for t in totals) - self.budget
        excess -= sum(s["size_bytes"] for s in selected if not s["missing"])
        for quality in QUALITIES:
            room = self.batch_size - len(selected)
            if excess <= 0 or room <= 0:
                break
            rows = self.db.oldest_segments(quality, end_before, room + len(self._skip) + len(chosen),
                                           present_only=True)
            for r in rows:
                if excess <= 0 or len(selected) >= self.batch_size:
                    break
                if r["id"] not in self._skip and r["id"] not in chosen:
                    selected.append({**r, "rule": "budget"})
                    excess -= r["size_bytes"]
        if excess > 0 and len(selected) < self.batch_size:
            log.warning("retention: %.2f GB over budget, but nothing else may be deleted (protected or too recent)",
                        _gb(excess))
        return selected

    def _report_dry_run(self, now: float, result: dict) -> dict:
        log.info("retention dry-run: would delete %d segments (%.2f GB) [age main=%d sub=%d, budget main=%d sub=%d]",
                 result["segments"], _gb(result["bytes"]), result["age_main"], result["age_sub"],
                 result["budget_main"], result["budget_sub"])
        result["result"] = "dry_run"
        with self._lock:
            self.would_delete = {**result, "ts": now}
        return result

    def _delete(self, now: float, real_root: str, ready: list[tuple[dict, str, bool]], result: dict) -> dict:
        marked: list[str] = []
        freed = missing = failed = 0
        for i in range(0, len(ready), COMMIT_EVERY):
            if self._stop.is_set():
                break
            ids: list[int] = []
            rels: list[str] = []
            chunk_bytes = 0
            for seg, path, exists in ready[i:i + COMMIT_EVERY]:
                try:
                    if exists:
                        os.unlink(path)
                        chunk_bytes += seg["size_bytes"]
                    else:
                        missing += 1
                except FileNotFoundError:
                    missing += 1
                except OSError as exc:
                    log.warning("retention: cannot delete %s: %s", seg["path"], exc)
                    self._skip.add(seg["id"])
                    failed += 1
                    continue
                ids.append(seg["id"])
                rels.append(seg["path"])
            self.db.mark_deleted(ids)  # if this fails, the next pass finds the files gone and marks the rows
            marked += rels
            freed += chunk_bytes
            with self._lock:
                self.deleted_segments += len(ids)
                self.deleted_bytes += chunk_bytes
        dirs = remove_empty_date_dirs(real_root, marked, live_hours(now))
        result.update(result="ok", segments=len(marked), bytes=freed, already_missing=missing, failed=failed,
                      dirs_removed=dirs)
        self.backlog = result["batch_full"]
        if marked or failed or result["skipped"]:
            log.info("retention: deleted %d segments (%.2f GB, %d already missing), %d dirs removed, %d skipped, "
                     "%d failed [age main=%d sub=%d, budget main=%d sub=%d]", len(marked), _gb(freed), missing, dirs,
                     result["skipped"], failed, result["age_main"], result["age_sub"], result["budget_main"],
                     result["budget_sub"])
        if marked:
            self._refresh_totals(now)
        return result

    # -- visibility --------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            last = self.last_run
            real = last is not None and last["result"] == "ok"
            return {
                "settings": {"dry_run": self.dry_run, "main_days": self.cfg["main_days"],
                             "sub_days": self.cfg["sub_days"], "max_total_gb": self.cfg["max_total_gb"],
                             "interval_s": self.cfg["interval_s"], "batch_size": self.batch_size,
                             "cameras": self.overrides},
                "total_bytes": sum(t["bytes"] for t in self.totals),
                "streams": [dict(t) for t in self.totals],
                "totals_ts": self.totals_ts,
                "last_run": dict(last) if last else None,
                "deleted": {"total_segments": self.deleted_segments, "total_bytes": self.deleted_bytes,
                            "last_segments": last["segments"] if real else 0,
                            "last_bytes": last["bytes"] if real else 0},
                "would_delete": dict(self.would_delete) if self.would_delete else None,
                "skipped_total": self.skipped_total,
                "errors_total": self.errors_total,
            }

    def metrics_lines(self) -> list[str]:
        st = self.status()
        p = "cams_record_retention"
        would = st["would_delete"] or {}
        lines = [f"# TYPE {p}_dry_run gauge", f"# TYPE {p}_total_bytes gauge", f"# TYPE {p}_max_bytes gauge",
                 f"# TYPE {p}_stream_bytes gauge", f"# TYPE {p}_oldest_segment_timestamp_seconds gauge",
                 f"# TYPE {p}_last_run_timestamp_seconds gauge", f"# TYPE {p}_last_run_duration_seconds gauge",
                 f"# TYPE {p}_deleted_segments_total counter", f"# TYPE {p}_deleted_bytes_total counter",
                 f"# TYPE {p}_last_deleted_segments gauge", f"# TYPE {p}_last_deleted_bytes gauge",
                 f"# TYPE {p}_would_delete_segments gauge", f"# TYPE {p}_would_delete_bytes gauge",
                 f"# TYPE {p}_skipped_total counter", f"# TYPE {p}_errors_total counter",
                 f"{p}_dry_run {1 if st['settings']['dry_run'] else 0}",
                 f"{p}_total_bytes {st['total_bytes']}",
                 f"{p}_max_bytes {self.budget}"]
        for t in st["streams"]:
            lbl = f'camera="{t["camera_id"]}",quality="{t["quality"]}"'
            lines.append(f"{p}_stream_bytes{{{lbl}}} {t['bytes']}")
            if t["oldest_start_ts"] is not None:
                lines.append(f"{p}_oldest_segment_timestamp_seconds{{{lbl}}} {t['oldest_start_ts']}")
        if st["last_run"]:
            lines.append(f"{p}_last_run_timestamp_seconds {st['last_run']['ts']}")
            lines.append(f"{p}_last_run_duration_seconds {st['last_run']['duration_s']}")
        lines += [f"{p}_deleted_segments_total {st['deleted']['total_segments']}",
                  f"{p}_deleted_bytes_total {st['deleted']['total_bytes']}",
                  f"{p}_last_deleted_segments {st['deleted']['last_segments']}",
                  f"{p}_last_deleted_bytes {st['deleted']['last_bytes']}",
                  f"{p}_would_delete_segments {would.get('segments', 0)}",
                  f"{p}_would_delete_bytes {would.get('bytes', 0)}",
                  f"{p}_skipped_total {st['skipped_total']}",
                  f"{p}_errors_total {st['errors_total']}"]
        return lines
