"""End to end: a looped testsrc file -> ffmpeg segmenter -> indexer -> MariaDB.

Records 3 x segment_time (CAMS_TEST_SEGMENT_TIME, default 60 s => ~3 minutes) and checks the
index: 3 full segments, start/end consistent with file names, no gaps > 1 s.
"""

import os
import time

import pytest

from cams_record.config import DEFAULTS, deep_merge, resolve_camera
from cams_record.segments import parse_rel_path
from cams_record.service import Service
from tests.conftest import make_clip, needs_ffmpeg

SEG = int(os.environ.get("CAMS_TEST_SEGMENT_TIME", "60"))


@pytest.mark.integration
@needs_ffmpeg
def test_three_segments_indexed_without_gaps(db, tmp_path):
    clip = make_clip(tmp_path / "src" / "testsrc.mp4", 3 * SEG)  # looped with -stream_loop -1
    root = tmp_path / "nvr"
    cfg = deep_merge(DEFAULTS, {"recording": {"root": str(root), "state_dir": str(tmp_path / "state"),
                                              "segment_time": SEG}})
    cams = {"front-door": resolve_camera("front-door", {"main_stream": str(clip), "sub_stream": str(clip)})}
    svc = Service(cfg, cams, db)

    # start just after a clock boundary so the first segment is (nearly) full length
    time.sleep(SEG - (time.time() % SEG) + 0.2)
    t0 = time.time()
    svc.start()
    time.sleep(3 * SEG + 1.5)
    svc.stop()  # SIGINT: ffmpeg closes the last (short) segment
    t1 = time.time()

    for q in ("main", "sub"):
        segs = [s for s in db.list_segments("front-door", q) if not s["missing"]]
        full = [s for s in segs if abs(s["duration_ms"] / 1000 - SEG) <= 1.5]
        assert len(full) >= 3, segs
        for s in segs:
            name_ts = parse_rel_path(s["path"]).start_ts
            assert name_ts - 1 <= s["start_ts"] < name_ts + 1  # consistent with the file name
            assert s["end_ts"] - s["start_ts"] == pytest.approx(s["duration_ms"] / 1000, abs=0.002)
            assert s["codec"] == "h264" and s["width"] == 160 and s["height"] == 120
            assert (root / s["path"]).is_file()
        for a, b in zip(segs, segs[1:], strict=False):
            assert b["start_ts"] - a["end_ts"] <= 1.0, (a, b)  # no gaps > 1 s
            assert a["end_ts"] - b["start_ts"] <= 1.0  # no big overlaps either
        assert segs[0]["start_ts"] <= t0 + 2 and segs[-1]["end_ts"] >= t1 - 15
        # every file on disk has a row
        on_disk = {str(p.relative_to(root)) for p in (root / "front-door" / q).rglob("*.mp4")}
        assert on_disk == {s["path"] for s in segs}

    st = db.run(lambda cur: (cur.execute("SELECT quality, state, last_segment_ts FROM recorder_status "
                                         "ORDER BY quality"), cur.fetchall())[1])
    assert [r[0] for r in st] == ["main", "sub"] and all(r[1] == "stopped" and r[2] for r in st)
