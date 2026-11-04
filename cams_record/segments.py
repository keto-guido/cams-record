"""Segment paths and timestamps.

Layout (relative to the recording root, all times UTC)::

    <camera>/<quality>/YYYY/MM/DD/HH/YYYYMMDDTHHMMSS.mp4

ffmpeg runs with TZ=UTC so its ``-strftime`` names are UTC whatever the container TZ is.
The name carries the wall-clock second the segment was opened (truncated). ``ClockMapper``
turns ffmpeg's segment-list stream times into sub-second UTC timestamps so consecutive
segments of one ffmpeg run line up exactly (end of one == start of the next).
"""

from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from .config import CAMERA_ID_RE, QUALITIES

FILE_PATTERN = "%Y/%m/%d/%H/%Y%m%dT%H%M%S.mp4"
NAME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})\.mp4$")


@dataclass(frozen=True)
class SegmentName:
    camera_id: str
    quality: str
    start_ts: float  # UTC epoch seconds (whole second from the file name)
    rel_path: str


def ffmpeg_pattern(root: str, camera_id: str, quality: str) -> str:
    return f"{root.rstrip('/')}/{camera_id}/{quality}/{FILE_PATTERN}"


def rel_path_for(camera_id: str, quality: str, start_ts: float) -> str:
    return f"{camera_id}/{quality}/" + time.strftime(FILE_PATTERN, time.gmtime(int(start_ts)))


def hour_dir(root: str, camera_id: str, quality: str, ts: float) -> str:
    return f"{root.rstrip('/')}/{camera_id}/{quality}/" + time.strftime("%Y/%m/%d/%H", time.gmtime(int(ts)))


def parse_name(name: str) -> float:
    """``20261104T213251.mp4`` -> UTC epoch seconds. Raises ValueError."""
    m = NAME_RE.match(name)
    if not m:
        raise ValueError(f"not a segment file name: {name!r}")
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    datetime(y, mo, d, h, mi, s)  # validates ranges
    return float(calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)))


def parse_rel_path(rel: str) -> SegmentName:
    """Validate a root-relative path and return its camera, quality and start time.

    The directory parts must agree with the file name (the file is where ffmpeg put it).
    """
    parts = PurePosixPath(rel).parts
    if len(parts) != 7:
        raise ValueError(f"unexpected segment path depth: {rel!r}")
    cam, quality, y, mo, d, h, name = parts
    if not CAMERA_ID_RE.match(cam):
        raise ValueError(f"bad camera id in path: {rel!r}")
    if quality not in QUALITIES:
        raise ValueError(f"bad quality in path: {rel!r}")
    ts = parse_name(name)
    if time.strftime("%Y/%m/%d/%H", time.gmtime(ts)) != f"{y}/{mo}/{d}/{h}":
        raise ValueError(f"directory does not match file name: {rel!r}")
    return SegmentName(cam, quality, ts, rel)


def to_dt(ts: float | None) -> datetime | None:
    """UTC epoch -> naive UTC datetime (ms precision) for DATETIME(3)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(round(float(ts), 3), tz=UTC).replace(tzinfo=None)


def from_dt(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return round(calendar.timegm(dt.timetuple()) + dt.microsecond / 1e6, 3)


class ClockMapper:
    """Map ffmpeg stream time (segment list start/end) to UTC for one ffmpeg run.

    For each closed segment we know ``name_ts`` (wall second the file was opened, truncated)
    and ``stream_start``. The true offset ``wall - stream`` therefore lies in
    ``[name_ts - stream_start, name_ts - stream_start + 1)``. We keep the tightest lower
    bound seen so far; if the stream clock runs fast relative to the wall clock (bound becomes
    inconsistent) we move to the largest value the newest segment still allows. Offsets thus
    change rarely and by sub-second amounts, and within a run segments stay contiguous.
    """

    def __init__(self) -> None:
        self.offset: float | None = None

    def reset(self) -> None:
        self.offset = None

    def map(self, name_ts: float, stream_start: float, stream_end: float) -> tuple[float, float]:
        lo = name_ts - stream_start
        if self.offset is None or lo > self.offset:
            self.offset = lo
        elif self.offset >= lo + 1.0:
            self.offset = lo + 0.999
        return round(self.offset + stream_start, 3), round(self.offset + stream_end, 3)


def parse_segment_list_line(line: str) -> tuple[str, float, float] | None:
    """ffmpeg ``-segment_list_type csv`` line ``name,start,end`` -> tuple (None if malformed)."""
    line = line.strip()
    if not line:
        return None
    try:
        name, start, end = line.rsplit(",", 2)
        name = name.strip().strip('"')
        return name, float(start), float(end)
    except ValueError:
        return None
