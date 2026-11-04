import calendar

import pytest

from cams_record.segments import (
    ClockMapper,
    ffmpeg_pattern,
    from_dt,
    parse_name,
    parse_rel_path,
    parse_segment_list_line,
    rel_path_for,
    to_dt,
)

T = calendar.timegm((2026, 11, 4, 21, 32, 51, 0, 0, 0))


def test_parse_name_utc():
    assert parse_name("20261104T213251.mp4") == T


@pytest.mark.parametrize("bad", ["20261104T213251.mkv", "2026114T213251.mp4", "20261332T000000.mp4",
                                 "20261104T256000.mp4", "x.mp4", ""])
def test_parse_name_rejects(bad):
    with pytest.raises(ValueError):
        parse_name(bad)


def test_rel_path_roundtrip():
    rel = rel_path_for("front-door", "main", T)
    assert rel == "front-door/main/2026/11/04/21/20261104T213251.mp4"
    seg = parse_rel_path(rel)
    assert (seg.camera_id, seg.quality, seg.start_ts, seg.rel_path) == ("front-door", "main", T, rel)


def test_rel_path_fractional_start_truncates():
    assert rel_path_for("c", "sub", T + 0.9).endswith("20261104T213251.mp4")


@pytest.mark.parametrize("bad", [
    "front-door/main/2026/11/04/22/20261104T213251.mp4",  # hour dir disagrees
    "front-door/hd/2026/11/04/21/20261104T213251.mp4",  # quality
    "Front/main/2026/11/04/21/20261104T213251.mp4",  # camera id
    "front-door/main/2026/11/04/20261104T213251.mp4",  # depth
])
def test_parse_rel_path_rejects(bad):
    with pytest.raises(ValueError):
        parse_rel_path(bad)


def test_ffmpeg_pattern():
    assert ffmpeg_pattern("/volume1/nvr/", "front-door", "sub") == \
        "/volume1/nvr/front-door/sub/%Y/%m/%d/%H/%Y%m%dT%H%M%S.mp4"


def test_datetime_roundtrip_ms():
    dt = to_dt(T + 0.1234)
    assert dt.tzinfo is None and dt.microsecond == 123000
    assert from_dt(dt) == pytest.approx(T + 0.123)
    assert to_dt(None) is None and from_dt(None) is None


def test_segment_list_line():
    assert parse_segment_list_line("20261104T213251.mp4,2.133333,12.133333\n") == \
        ("20261104T213251.mp4", 2.133333, 12.133333)
    assert parse_segment_list_line('"a,b.mp4",1,2') == ("a,b.mp4", 1.0, 2.0)
    assert parse_segment_list_line("") is None
    assert parse_segment_list_line("garbage") is None


def test_clock_mapper_contiguous():
    """Real ffmpeg list: names truncated to the second, stream times continuous."""
    m = ClockMapper()
    rows = [(T - 2, 0.0, 2.133333), (T, 2.133333, 12.133333), (T + 10, 12.133333, 22.133333)]
    out = [m.map(*r) for r in rows]
    for (_s0, e0), (s1, _e1) in zip(out, out[1:], strict=False):
        assert s1 == e0  # no gaps, no overlap
    for (name_ts, _, _), (s, _) in zip(rows, out, strict=True):
        assert name_ts <= s < name_ts + 1  # consistent with the file name


def test_clock_mapper_fast_stream_clock_reanchors():
    m = ClockMapper()
    m.map(T, 0.0, 60.0)
    # stream clock ran 3 s fast vs the wall clock: the next file opened at T+60 but stream says 63
    s, _ = m.map(T + 60, 63.0, 123.0)
    assert T + 60 <= s < T + 61
