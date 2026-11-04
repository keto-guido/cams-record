"""Startup reconcile between the recording root and the ``segments`` table.

1. Files on disk without a row -> probed and inserted (via the indexer, low priority).
2. Rows whose file is gone -> ``missing = 1``; rows marked missing whose file is back -> ``missing = 0``.

Runs alongside the recorders: files modified after ``cutoff`` (service start) belong to the live
recorders and are skipped. Memory stays bounded: the DB is read one day directory (or one
5000-row page) at a time.
"""

from __future__ import annotations

import logging
import os
import re
import time

from .config import CAMERA_ID_RE, QUALITIES
from .db import Database
from .indexer import PRIO_RECONCILE, Indexer, SegmentJob
from .segments import parse_rel_path

log = logging.getLogger(__name__)

Y_RE, M_RE, D_RE, H_RE = re.compile(r"^\d{4}$"), re.compile(r"^\d{2}$"), re.compile(r"^\d{2}$"), re.compile(r"^\d{2}$")


def _subdirs(path: str, pattern: re.Pattern) -> list[str]:
    try:
        return sorted(e.name for e in os.scandir(path) if e.is_dir(follow_symlinks=False) and pattern.match(e.name))
    except OSError:
        return []


def iter_day_dirs(root: str):
    """Yield (camera, quality, 'cam/q/YYYY/MM/DD/', abs day dir)."""
    for cam in _subdirs(root, CAMERA_ID_RE):
        for q in QUALITIES:
            qdir = os.path.join(root, cam, q)
            for y in _subdirs(qdir, Y_RE):
                for m in _subdirs(os.path.join(qdir, y), M_RE):
                    for d in _subdirs(os.path.join(qdir, y, m), D_RE):
                        yield cam, q, f"{cam}/{q}/{y}/{m}/{d}/", os.path.join(qdir, y, m, d)


def reconcile(root: str, db: Database, indexer: Indexer, cutoff: float | None = None) -> dict:
    t0 = time.monotonic()
    cutoff = time.time() if cutoff is None else cutoff
    root = root.rstrip("/")
    queued = unmarked = 0
    for _cam, _q, prefix, day_dir in iter_day_dirs(root):
        known = db.paths_with_prefix(prefix)
        for h in _subdirs(day_dir, H_RE):
            try:
                entries = list(os.scandir(os.path.join(day_dir, h)))
            except OSError:
                continue
            for e in entries:
                if not e.name.endswith(".mp4"):
                    continue
                rel = prefix + h + "/" + e.name
                if rel in known:
                    continue
                try:
                    if not e.is_file() or e.stat().st_mtime >= cutoff:
                        continue
                    seg = parse_rel_path(rel)
                except (OSError, ValueError) as exc:
                    log.warning("reconcile: skipping %s: %s", rel, exc)
                    continue
                indexer.submit(SegmentJob(seg.camera_id, seg.quality, e.path, rel, seg.start_ts), PRIO_RECONCILE)
                queued += 1

    # Rows -> files (keyset pagination over present rows).
    missing_ids: list[int] = []
    after, checked = 0, 0
    while True:
        rows = db.present_rows(after)
        if not rows:
            break
        for rid, path in rows:
            if not os.path.exists(os.path.join(root, path)):
                missing_ids.append(rid)
        checked += len(rows)
        after = rows[-1][0]
    marked = db.mark_missing(missing_ids) if missing_ids else 0

    def back(cur):
        cur.execute("SELECT id, path FROM segments WHERE missing = 1")
        return [rid for rid, p in cur.fetchall() if os.path.exists(os.path.join(root, p))]

    found = db.run(back)
    for i in range(0, len(found), 500):
        chunk = found[i:i + 500]
        marks = ",".join(["%s"] * len(chunk))
        unmarked += db.run(lambda cur, c=chunk, m=marks: cur.execute(
            f"UPDATE segments SET missing = 0 WHERE id IN ({m})", c))
    res = {"queued": queued, "rows_checked": checked, "marked_missing": marked, "unmarked": unmarked,
           "seconds": round(time.monotonic() - t0, 1)}
    log.info("reconcile: %d files to index, %d rows checked, %d marked missing, %d found again (%.1fs)",
             queued, checked, marked, unmarked, res["seconds"])
    return res
