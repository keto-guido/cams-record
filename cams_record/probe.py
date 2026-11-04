"""ffprobe helpers (segment metadata, source video and audio codecs)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


class ProbeError(Exception):
    pass


@dataclass
class SegmentInfo:
    duration: float
    size_bytes: int
    codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    audio_codec: str | None = None


def _rate(v: str | None) -> float | None:
    if not v or v in ("0/0", "0"):
        return None
    try:
        if "/" in v:
            n, d = v.split("/", 1)
            return float(n) / float(d) if float(d) else None
        return float(v)
    except ValueError:
        return None


def _nice(level: int):
    def fn():
        try:
            os.nice(level)
        except OSError:
            pass

    return fn if level else None


def parse_probe(data: dict, size_fallback: int = 0) -> SegmentInfo:
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError("no video stream")
    try:
        duration = float(fmt.get("duration") or video.get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise ProbeError("no duration")
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    if fps is not None:
        fps = round(min(fps, 999.99), 2)
    return SegmentInfo(
        duration=duration,
        size_bytes=int(fmt.get("size") or size_fallback),
        codec=video.get("codec_name"),
        width=int(video["width"]) if video.get("width") else None,
        height=int(video["height"]) if video.get("height") else None,
        fps=fps,
        audio_codec=audio.get("codec_name") if audio else None,
    )


def probe_segment(path: str, ffprobe: str = "ffprobe", nice: int = 10, timeout: float = 30) -> SegmentInfo:
    cmd = [ffprobe, "-v", "error", "-show_entries",
           "format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration",
           "-of", "json", path]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, preexec_fn=_nice(nice), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"ffprobe failed: {exc}") from exc
    if p.returncode != 0:
        raise ProbeError(p.stderr.decode(errors="replace").strip()[-300:] or f"ffprobe exit {p.returncode}")
    try:
        data = json.loads(p.stdout or b"{}")
    except ValueError as exc:
        raise ProbeError("ffprobe returned invalid JSON") from exc
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return parse_probe(data, size)


def probe_source(url: str, ffprobe: str = "ffprobe", rtsp_transport: str = "tcp",
                 timeout: float = 15) -> tuple[bool, str | None, str | None]:
    """(reachable, video codec name or None, audio codec name or None) for the recorder's input."""
    cmd = [ffprobe, "-v", "error"]
    if url.startswith("rtsp"):
        cmd += ["-rtsp_transport", rtsp_transport, "-timeout", str(int(timeout * 1_000_000))]
    cmd += ["-show_entries", "stream=codec_type,codec_name", "-of", "json", url]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, None, None
    if p.returncode != 0:
        return False, None, None
    try:
        streams = json.loads(p.stdout or b"{}").get("streams") or []
    except ValueError:
        return False, None, None
    video = next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None)
    return True, video, audio
