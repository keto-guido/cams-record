"""Indexed, stream-copy clips with bounded on-disk caching."""
import hashlib
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .retention import UnsafePath, resolve_segment_path


class ClipError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


def parse_time(value):
    try:
        result = float(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("timezone required")
            result = dt.timestamp()
        except (ValueError, OverflowError) as exc:
            raise ClipError(400, "invalid timestamp; use epoch or ISO8601 with timezone") from exc
    if not math.isfinite(result):
        raise ClipError(400, "invalid timestamp")
    try:
        datetime.fromtimestamp(result, UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise ClipError(400, "timestamp out of range") from exc
    return result


def validate_window(start, end):
    if end <= start or end - start > 300:
        raise ClipError(400, "clip window must be positive and at most 5 minutes")


def select_segments(rows, latest, start, end, gap_tolerance_s=2.0):
    cursor = start
    selected = []
    by_path = {}
    for row in rows:
        if not row.get("missing") and not row.get("deleted_at"):
            by_path.setdefault(row["path"], row)
    remaining = list(by_path.values())
    while cursor < end:
        candidates = [r for r in remaining
                      if r["start_ts"] <= cursor + gap_tolerance_s and r["end_ts"] > cursor]
        if not candidates:
            break
        # The first row may contain the requested start. Later, avoid replaying a
        # restart duplicate unless an overlapping row is the only way forward.
        fresh = [r for r in candidates if r["start_ts"] >= cursor - gap_tolerance_s]
        initial = [r for r in candidates if r["start_ts"] <= start]
        row = max((fresh or candidates) if selected else (initial or candidates),
                  key=lambda r: (r["end_ts"], r["start_ts"]))
        selected.append(row)
        cursor = row["end_ts"]
        remaining = [r for r in remaining if r["end_ts"] > cursor]
    if cursor >= end:
        return "available", selected
    if any(r["start_ts"] > cursor + gap_tolerance_s for r in remaining):
        return "gone", []
    if latest is None or end > latest:
        # A known hole before the indexed frontier cannot be fixed by closing the live segment.
        if cursor < min(end, latest or start) - gap_tolerance_s:
            return "gone", []
        return "not_ready", []
    return "gone", []


def ffmpeg_command(ffmpeg, listing, output, rows, start, end, audio=True):
    # Input seeking retains the preceding keyframe when stream copying. The
    # duration is relative to the requested seek, so that early frames are extra.
    cmd = [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
           "-ss", str(max(0, start - rows[0]["start_ts"])), "-i", str(listing),
           "-t", str(end - max(start, rows[0]["start_ts"])),
           "-map", "0:v:0"]
    cmd += ["-map", "0:a?"] if audio else ["-an"]
    cmd += ["-c", "copy"]
    if rows[0].get("codec", "") in ("hevc", "h265"):
        cmd += ["-tag:v", "hvc1"]
    return cmd + ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(output)]


class Clips:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg["clips"]
        self.root = os.path.realpath(cfg["recording"]["root"])
        self.cache = Path(self.cfg["cache_dir"] or Path(cfg["recording"]["state_dir"]) / "clips").resolve()
        if self.cache == Path(self.root) or Path(self.root) in self.cache.parents:
            raise ValueError("clip cache must be outside recordings root")
        self.ffmpeg = cfg["recording"]["ffmpeg"]
        self.semaphore = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()

    def availability(self, camera, quality, start, end):
        if self.db is None:
            raise ClipError(503, "segment index unavailable")
        rows, latest = self.db.clip_segments(camera, quality, start, end)
        state, rows = select_segments(rows, latest, start, end, self.cfg["gap_tolerance_s"])
        if state == "available":
            try:
                for row in rows:
                    if any(c in row["path"] for c in ("\r", "\n")):
                        return "gone", []
                    if not resolve_segment_path(self.root, row["path"])[1]:
                        return "gone", []
            except UnsafePath:
                return "gone", []
        return state, rows

    def _result(self, target, lease):
        if not lease:
            return target
        # A response owns a hard link, so LRU eviction cannot interrupt Range streaming.
        serving = self.cache / (uuid.uuid4().hex + ".lease")
        serving.hardlink_to(target)
        return serving

    def cleanup(self):
        for p in self.cache.glob("*.lease"):
            try:
                if time.time() - p.stat().st_mtime > float(self.cfg["max_age_s"]):
                    p.unlink(missing_ok=True)
            except FileNotFoundError:
                pass  # response completed while cleanup was scanning
        files = sorted(self.cache.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            size = p.stat().st_size
            if total > float(self.cfg["max_cache_gb"]) * 1e9 or time.time() - p.stat().st_mtime > self.cfg["max_age_s"]:
                p.unlink(missing_ok=True)
                total -= size

    def build(self, camera, quality, start, end, lease=False):
        with self.semaphore:
            state, rows = self.availability(camera, quality, start, end)
            if state != "available":
                raise ClipError(425 if state == "not_ready" else 404,
                                "footage is not indexed yet" if state == "not_ready" else
                                f"{quality} quality footage no longer available")
            key = hashlib.sha256(f"{camera}/{quality}/{start}/{end}".encode()).hexdigest()
            self.cache.mkdir(parents=True, exist_ok=True)
            target = self.cache / (key + ".mp4")
            with self.lock:
                self.cleanup()
                if target.exists():
                    target.touch()
                    return self._result(target, lease)
            with tempfile.TemporaryDirectory(dir=self.cache) as tmp:
                listing, output = Path(tmp) / "concat.txt", Path(tmp) / "clip.mp4"
                paths = [resolve_segment_path(self.root, r["path"])[0] for r in rows]
                # The concat demuxer needs every duration to support seeking.
                listing.write_text(
                    "".join("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n"
                            + f"duration {r['end_ts'] - r['start_ts']}\n"
                            for p, r in zip(paths, rows, strict=True)),
                    encoding="utf-8")
                prefix = []
                if shutil.which("nice"):
                    prefix += ["nice", "-n", "10"]
                if shutil.which("ionice"):
                    prefix += ["ionice", "-c3"]
                try:
                    for audio in (True, False):
                        command = ffmpeg_command(self.ffmpeg, listing, output, rows, start, end, audio)
                        result = subprocess.run(prefix + command,
                                                capture_output=True, timeout=float(self.cfg["timeout_s"]))
                        if result.returncode == 0:
                            break
                    else:
                        raise ClipError(500, "clip assembly failed")
                except OSError as exc:
                    raise ClipError(500, "clip assembler unavailable") from exc
                except subprocess.TimeoutExpired as exc:
                    raise ClipError(504, "clip assembly timed out") from exc
                with self.lock:
                    if output.stat().st_size > float(self.cfg["max_cache_gb"]) * 1e9:
                        raise ClipError(507, "clip exceeds cache size limit")
                    os.replace(output, target)
                    self.cleanup()
                    return self._result(target, lease)
