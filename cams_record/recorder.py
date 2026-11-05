"""One supervised ffmpeg (stream copy -> 60 s MP4 segments) per (camera, quality).

ffmpeg writes ``<root>/<camera>/<quality>/%Y/%m/%d/%H/%Y%m%dT%H%M%S.mp4`` (UTC names) and appends
each segment to a CSV segment list *when it closes*. We tail that list, so a segment is only
indexed once ffmpeg has finished it (moov written). The segment muxer does not create
directories, so the supervisor creates the current and next hour directories ahead of time.

Restart policy: exponential backoff (1 s .. 60 s, reset after a healthy segment) and a stall
watchdog (no segment closed in ``stall_factor * segment_time`` -> restart). Each restart is
logged and counted (``restarts_total``, ``restarts_24h``).

Disk guard: while the guard blocks this quality the recorder finalizes its open segment and waits
in state ``disk_full``. That is not a failure, so it counts no restart and adds no backoff.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .diskguard import DiskGuard
from .indexer import PRIO_LIVE, Indexer, SegmentJob
from .probe import probe_source
from .segments import (
    ClockMapper,
    ffmpeg_pattern,
    hour_dir,
    parse_name,
    parse_rel_path,
    parse_segment_list_line,
    rel_path_for,
)
from .watchdog import Backoff, RestartCounter, Watchdog

log = logging.getLogger(__name__)

TICK = 1.0


def build_ffmpeg_cmd(url: str, pattern: str, list_path: str, segment_time: int, audio: bool,
                     rtsp_transport: str = "tcp", ffmpeg: str = "ffmpeg",
                     video_codec: str | None = None) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error"]
    if url.startswith("rtsp"):
        cmd += ["-rtsp_transport", rtsp_transport, "-timeout", "10000000"]
    elif "://" not in url:  # local file (tests / replay): real-time, looped
        cmd += ["-re", "-stream_loop", "-1"]
    cmd += ["-i", url, "-map", "0:v"]
    if audio:
        cmd += ["-map", "0:a?"]
    cmd += ["-c", "copy"]
    if audio:
        cmd += ["-copyinkf:a"]
    if video_codec == "hevc":
        cmd += ["-tag:v", "hvc1"]  # Safari only plays HEVC in MP4 with the hvc1 tag (ffmpeg writes hev1)
    cmd += ["-f", "segment", "-segment_time", str(int(segment_time)),
            "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
            "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
            "-segment_list", list_path, "-segment_list_type", "csv", pattern]
    return cmd


class StreamRecorder:
    def __init__(self, camera_id: str, quality: str, url: str, rec_cfg: dict, indexer: Indexer,
                 guard: DiskGuard | None = None):
        self.camera_id = camera_id
        self.quality = quality
        self.url = url
        self.cfg = rec_cfg
        self.root = str(rec_cfg["root"]).rstrip("/")
        self.segment_time = int(rec_cfg["segment_time"])
        self.indexer = indexer
        self.guard = guard
        state_dir = Path(rec_cfg["state_dir"])
        state_dir.mkdir(parents=True, exist_ok=True)
        self.list_path = str(state_dir / f"{camera_id}-{quality}.csv")
        self.pattern = ffmpeg_pattern(self.root, camera_id, quality)
        self.backoff = Backoff(float(rec_cfg["backoff_initial"]), float(rec_cfg["backoff_max"]))
        self.watchdog = Watchdog(self.segment_time, float(rec_cfg.get("stall_factor", 3)))
        self.restarts = RestartCounter()
        self.mapper = ClockMapper()
        self.state = "starting"
        self.last_error = ""
        self.audio: bool | None = None  # last probe result (None = not probed yet)
        self.video_codec: str | None = None  # last probed source video codec
        self.last_segment_end: float | None = None  # UTC epoch of the newest closed segment's end
        self.last_segment_wall: float | None = None  # wall time it closed
        self.segments_total = 0
        self.bytes_total = 0
        self._recent: deque[tuple[float, int]] = deque()  # (wall close time, bytes) for MB/hour
        self._seen: deque[str] = deque(maxlen=16)  # rel paths indexed in this run
        self._stderr: deque[str] = deque(maxlen=20)
        self._proc: subprocess.Popen | None = None
        self._list_pos = 0
        self._list_buf = ""
        self._dirs_made: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def key(self) -> str:
        return f"{self.camera_id}/{self.quality}"

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"rec-{self.key}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # -- helpers -----------------------------------------------------------
    def _set_state(self, state: str, error: str | None = None) -> None:
        self.state = state
        if error is not None:
            self.last_error = error[:512]
        self.indexer.set_status(self.status_row())

    def status_row(self) -> dict:
        return {"camera_id": self.camera_id, "quality": self.quality, "state": self.state,
                "last_segment_ts": self.last_segment_end, "restarts_24h": self.restarts.last_24h(),
                "last_error": self.last_error}

    def ensure_dirs(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for ts in (now, now + 2 * self.segment_time):
            d = hour_dir(self.root, self.camera_id, self.quality, ts)
            if d in self._dirs_made:
                continue
            os.makedirs(d, exist_ok=True)
            self._dirs_made.add(d)
            if len(self._dirs_made) > 8:
                self._dirs_made = {d}

    def mb_per_hour(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        while self._recent and self._recent[0][0] < now - 3600:
            self._recent.popleft()
        return round(sum(b for _, b in list(self._recent)) / 1e6, 1)

    def _probe_source(self) -> bool:
        """Refresh the source's video codec and decide on audio (a failed probe keeps the last result)."""
        ok, video, audio_codec = probe_source(self.url, self.cfg.get("ffprobe", "ffprobe"),
                                              self.cfg.get("rtsp_transport", "tcp"),
                                              float(self.cfg.get("probe_timeout", 15)))
        if ok:
            if video != self.video_codec:
                log.info("%s: source video %s", self.key, video or "none")
            self.video_codec = video
        if self.cfg.get("audio", "auto") == "off":
            return False
        if ok:
            audio = audio_codec == "aac"
            if audio != self.audio:
                log.info("%s: source audio %s -> %s", self.key, audio_codec or "none",
                         "copy" if audio else "dropped (only AAC is recorded)")
            self.audio = audio
        return bool(self.audio)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if line:
                self._stderr.append(line)
                log.debug("%s ffmpeg: %s", self.key, line)

    # -- segment list ------------------------------------------------------
    def poll_segment_list(self) -> int:
        """Consume newly closed segments from ffmpeg's CSV list. Returns how many were queued."""
        try:
            with open(self.list_path, encoding="utf-8", errors="replace") as fh:
                fh.seek(self._list_pos)
                data = fh.read()
                self._list_pos = fh.tell()
        except FileNotFoundError:
            return 0
        if not data:
            return 0
        self._list_buf += data
        *lines, self._list_buf = self._list_buf.split("\n")
        n = 0
        for line in lines:
            parsed = parse_segment_list_line(line)
            if parsed is None:
                continue
            name, st, et = parsed
            try:
                name_ts = parse_name(os.path.basename(name))
            except ValueError:
                log.warning("%s: unexpected segment name %r", self.key, name)
                continue
            start, end = self.mapper.map(name_ts, st, et)
            self._closed(name_ts, start, end)
            n += 1
        return n

    def _closed(self, name_ts: float, start: float | None, end: float | None) -> None:
        rel = rel_path_for(self.camera_id, self.quality, name_ts)
        abs_path = f"{self.root}/{rel}"
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            log.warning("%s: closed segment %s not found on disk", self.key, rel)
            return
        now = time.time()
        self._seen.append(rel)
        self.segments_total += 1
        self.bytes_total += size
        self._recent.append((now, size))
        self.last_segment_wall = now
        self.last_segment_end = end if end is not None else name_ts
        self.watchdog.segment()
        self.backoff.reset()
        self.indexer.submit(SegmentJob(self.camera_id, self.quality, abs_path, rel, name_ts, start, end), PRIO_LIVE)
        self._set_state("recording")

    def _sweep_unlisted(self, run_started: float) -> None:
        """After ffmpeg exits, index files it opened but never listed (e.g. killed mid-segment).

        The indexer probes them; an unreadable one is renamed ``*.mp4.bad`` so no stray file
        without a row is left behind.
        """
        dirs = {hour_dir(self.root, self.camera_id, self.quality, t)
                for t in (run_started, time.time(), time.time() - self.segment_time)}
        for d in dirs:
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if not e.name.endswith(".mp4") or not e.is_file():
                    continue
                rel = os.path.relpath(e.path, self.root).replace(os.sep, "/")
                if rel in self._seen:
                    continue
                try:
                    if e.stat().st_mtime < run_started - 1:
                        continue
                    seg = parse_rel_path(rel)
                except (OSError, ValueError):
                    continue
                self._seen.append(rel)
                log.info("%s: indexing unlisted segment %s left by the previous ffmpeg", self.key, rel)
                self.indexer.submit(SegmentJob(self.camera_id, self.quality, e.path, rel, seg.start_ts), PRIO_LIVE)

    # -- supervisor loop ---------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            if self.guard is not None:
                self.guard.check()  # fresh reading: never spawn ffmpeg on a level up to 30 s old
            if self._disk_blocked():
                self._hold_disk_full()
                continue
            reason = self._run_once()
            if self._stop.is_set():
                break
            if self._disk_blocked():
                continue
            self.restarts.add()
            delay = self.backoff.next()
            log.warning("%s: ffmpeg %s; restart #%d (%d in 24h) in %.0fs", self.key, reason,
                        self.restarts.total, self.restarts.last_24h(), delay)
            self._set_state("backoff", reason)
            self._wait_backoff(delay)
        self._set_state("stopped")

    def _disk_blocked(self) -> bool:
        return self.guard is not None and self.guard.blocked(self.quality)

    def _wait_backoff(self, delay: float) -> None:
        """Sleep through a backoff, ending early on stop or when the guard blocks this quality."""
        end = time.monotonic() + delay
        while not self._disk_blocked():
            left = end - time.monotonic()
            if left <= 0 or self._stop.wait(min(TICK, left)):
                return

    def _hold_disk_full(self) -> None:
        """Stay in state disk_full until the guard lets this quality record again (or stop)."""
        msg = f"low disk space, recording stopped ({self.guard.state})"
        log.error("%s: disk_full: %s", self.key, msg)
        self._set_state("disk_full", msg)
        while self._disk_blocked():
            if self._stop.wait(TICK):
                return
        log.warning("%s: disk space recovered, recording resumes", self.key)
        self.backoff.reset()
        self._set_state("starting", "")

    def _run_once(self) -> str:
        self._set_state("starting")
        try:
            self.ensure_dirs()
        except OSError as exc:
            return f"cannot create directories: {exc}"
        audio = self._probe_source()
        if self._stop.is_set() or self._disk_blocked():
            return "stopped"
        cmd = build_ffmpeg_cmd(self.url, self.pattern, self.list_path, self.segment_time, audio,
                               self.cfg.get("rtsp_transport", "tcp"), self.cfg.get("ffmpeg", "ffmpeg"),
                               self.video_codec)
        try:
            os.unlink(self.list_path)
        except FileNotFoundError:
            pass
        self._list_pos, self._list_buf = 0, ""
        self.mapper.reset()
        self._stderr.clear()
        env = dict(os.environ, TZ="UTC")  # strftime file names in UTC
        run_started = time.time()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, env=env)
        except OSError as exc:
            return f"failed to start: {exc}"
        self._proc = proc
        threading.Thread(target=self._read_stderr, args=(proc,), name=f"ffmpeg-err-{self.key}",
                         daemon=True).start()
        self.watchdog.started()
        log.info("%s: recording %s (audio %s)", self.key, self.url, "on" if audio else "off")
        reason = "stopped"
        try:
            while True:
                if self._stop.wait(TICK):
                    break
                if self._disk_blocked():
                    reason = "disk_full"
                    break
                try:
                    self.ensure_dirs()
                except OSError as exc:
                    reason = f"cannot create directories: {exc}"
                    break
                self.poll_segment_list()
                rc = proc.poll()
                if rc is not None:
                    tail = self._stderr[-1] if self._stderr else ""
                    reason = f"exited with code {rc}" + (f": {tail}" if tail else "")
                    break
                if self.watchdog.stalled():
                    reason = f"stalled (no segment for {self.watchdog.idle_for():.0f}s)"
                    break
        finally:
            self._terminate(proc)
            self.poll_segment_list()
            self._sweep_unlisted(run_started)
            self._proc = None
        return reason

    def _terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)  # ffmpeg finalizes the open segment and its list entry
            proc.wait(timeout=float(self.cfg.get("stop_timeout", 10)))
        except subprocess.TimeoutExpired:
            log.warning("%s: ffmpeg did not exit on SIGINT, killing", self.key)
            proc.kill()
            proc.wait(timeout=5)
        except OSError:
            pass

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        return {
            "camera_id": self.camera_id, "quality": self.quality, "state": self.state, "url": self.url,
            "video_codec": self.video_codec, "audio": self.audio, "last_segment_ts": self.last_segment_end,
            "last_segment_age_s": round(now - self.last_segment_wall, 1) if self.last_segment_wall else None,
            "segments_total": self.segments_total, "mb_per_hour": self.mb_per_hour(now),
            "restarts_total": self.restarts.total, "restarts_24h": self.restarts.last_24h(),
            "last_error": self.last_error, "pid": self._proc.pid if self._proc else None,
        }
