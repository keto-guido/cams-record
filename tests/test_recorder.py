import calendar
import os
import time

from cams_record.config import DEFAULTS, deep_merge
from cams_record.indexer import Indexer
from cams_record.recorder import StreamRecorder, build_ffmpeg_cmd
from cams_record.segments import rel_path_for
from cams_record.watchdog import Backoff

T = calendar.timegm((2026, 11, 4, 21, 59, 30, 0, 0, 0))


def rec_cfg(tmp_path, **kw):
    cfg = deep_merge(DEFAULTS["recording"], {"root": str(tmp_path / "nvr"), "state_dir": str(tmp_path / "state"),
                                             "audio": "off"})
    cfg.update(kw)
    return cfg


def test_ffmpeg_cmd_rtsp_stream_copy():
    cmd = build_ffmpeg_cmd("rtsp://127.0.0.1:8554/front-door", "/r/%Y.mp4", "/s/l.csv", 60, audio=True)
    s = " ".join(cmd)
    assert "-rtsp_transport tcp" in s and "-i rtsp://127.0.0.1:8554/front-door" in s
    assert "-map 0:v -map 0:a? -c copy -f segment -segment_time 60 -segment_atclocktime 1" in s
    assert "-reset_timestamps 1 -strftime 1 -segment_format mp4 -segment_format_options movflags=+faststart" in s
    assert "-segment_list /s/l.csv -segment_list_type csv" in s
    assert cmd[-1] == "/r/%Y.mp4"
    assert "-re" not in cmd and "-vcodec" not in cmd and "libx264" not in s


def test_ffmpeg_cmd_no_audio_and_file_input():
    cmd = build_ffmpeg_cmd("/clips/a.mp4", "/r/x.mp4", "/s/l.csv", 10, audio=False)
    assert "0:a?" not in cmd
    assert cmd[cmd.index("-i") - 3:cmd.index("-i")] == ["-re", "-stream_loop", "-1"]


def test_ensure_dirs_creates_current_and_next_hour(tmp_path):
    r = StreamRecorder("cam", "main", "/x.mp4", rec_cfg(tmp_path), Indexer(None))
    r.ensure_dirs(now=T)  # 21:59:30 -> 21 and 22 (next 120 s cross the hour)
    base = tmp_path / "nvr/cam/main/2026/11/04"
    assert (base / "21").is_dir() and (base / "22").is_dir()


def test_poll_segment_list_queues_only_closed_segments(tmp_path):
    idx = Indexer(None)
    r = StreamRecorder("cam", "sub", "/x.mp4", rec_cfg(tmp_path), idx)
    for ts in (T, T + 30):
        p = tmp_path / "nvr" / rel_path_for("cam", "sub", ts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 1000)
    with open(r.list_path, "w") as fh:
        fh.write("20261104T215930.mp4,0.000000,30.000000\n20261104T220000.mp4,30.0")  # 2nd line incomplete
    assert r.poll_segment_list() == 1
    assert idx.pending() == 1 and r.segments_total == 1 and r.state == "recording"
    with open(r.list_path, "a") as fh:
        fh.write("00000,60.000000\n")
    assert r.poll_segment_list() == 1
    jobs = [idx._q.get_nowait()[2] for _ in range(2)]
    assert [j.rel_path for j in jobs] == [rel_path_for("cam", "sub", T), rel_path_for("cam", "sub", T + 30)]
    assert jobs[0].end_ts == jobs[1].start_ts  # contiguous
    assert jobs[0].start_ts == T and r.last_segment_end == T + 60
    assert r.mb_per_hour() == 0.0  # 2 kB rounds to 0.0 MB
    assert r.bytes_total == 2000


def test_supervisor_restarts_with_backoff(tmp_path):
    """ffmpeg that exits at once -> restart counted, logged, backoff grows, status row queued."""
    idx = Indexer(None)
    r = StreamRecorder("cam", "main", "/x.mp4", rec_cfg(tmp_path, ffmpeg="false"), idx)
    r.backoff = Backoff(0.05, 0.2)
    r.start()
    deadline = time.time() + 15
    while r.restarts.total < 2 and time.time() < deadline:
        time.sleep(0.1)
    r.stop()
    r.join(10)
    assert r.restarts.total >= 2 and r.restarts.last_24h() >= 2
    assert "exited with code 1" in r.last_error
    assert r.state == "stopped"
    assert idx._status[("cam", "main")]["state"] == "stopped"


def test_unlisted_segment_after_kill_is_indexed(tmp_path):
    idx = Indexer(None)
    r = StreamRecorder("cam", "main", "/x.mp4", rec_cfg(tmp_path), idx)
    now = time.time()
    p = tmp_path / "nvr" / rel_path_for("cam", "main", now)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"partial")
    old = tmp_path / "nvr" / rel_path_for("cam", "main", now - 1)
    old.write_bytes(b"old")
    os.utime(old, (now - 600, now - 600))
    r._sweep_unlisted(run_started=now - 5)
    jobs = []
    while idx.pending():
        jobs.append(idx._q.get_nowait()[2])
    assert [j.abs_path for j in jobs] == [str(p)]
