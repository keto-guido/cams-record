from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from cams_record.clips import Clips, ffmpeg_command, parse_time, select_segments
from cams_record.config import DEFAULTS, deep_merge
from cams_record.protection import DetectionProtection, event_window
from cams_record.web import create_app
from tests.test_web import SECRET, tok


def seg(start=0, end=60, **kw):
    return dict(start_ts=start, end_ts=end, path="a.mp4" if start == 0 else f"{start}.mp4", codec="hevc", **kw)


@pytest.mark.parametrize("rows,latest,start,end,state", [
    ([seg(), seg(60, 120)], 120, 10, 100, "available"),
    ([seg(), seg(50, 120)], 120, 10, 100, "available"),
    ([seg(), seg(63, 120)], 120, 10, 100, "gone"),
    ([seg()], 60, 10, 80, "not_ready"),
    ([], None, 10, 80, "not_ready"),
    ([seg(deleted_at=True)], 60, 10, 50, "gone"),
    ([seg(missing=True)], 60, 10, 50, "gone"),
    ([seg(deleted_at=True)], 60, 10, 80, "gone"),
])
def test_selection(rows, latest, start, end, state):
    assert select_segments(rows, latest, start, end)[0] == state


def test_command_and_window():
    cmd = ffmpeg_command("ffmpeg", "list", "out", [seg(100, 160)], 110, 140)
    assert cmd[cmd.index("-ss") + 1] == "10"
    assert cmd[cmd.index("-to") + 1] == "40"
    assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
    assert event_window(100, None, {}) == (95, 115)
    assert event_window(100, 110, {}) == (95, 115)
    assert event_window(100, 400, {}) == (95, 215)
    assert parse_time("1970-01-01T00:01:40Z") == 100


@pytest.fixture
def setup(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text(SECRET)
    root = tmp_path / "nvr"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"segment")
    cfg = deep_merge(DEFAULTS, {"auth": {"secret_file": str(secret)},
                              "recording": {"root": str(root), "state_dir": str(tmp_path / "state")}})
    db = Mock()
    db.clip_segments.return_value = ([seg()], 60)
    service = SimpleNamespace(db=db)
    return cfg, db, TestClient(create_app(service, cfg))


def test_clip_api_auth_range_cache(setup, monkeypatch):
    cfg, db, client = setup
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"0123456789")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("cams_record.clips.subprocess.run", run)
    url = "/cams-record/api/clip?camera=cam&start=10&end=40&quality=main"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"x-apphub-token": tok(["viewer"])}).status_code == 403
    headers = {"x-apphub-token": tok(["cams"]), "Range": "bytes=2-5"}
    response = client.get(url, headers=headers)
    assert response.status_code == 206
    assert response.content == b"2345"
    assert "inline" in response.headers["content-disposition"]
    response = client.get(url + "&download=1", headers={"x-apphub-token": tok()})
    assert response.status_code == 200
    assert 'attachment; filename="cam_19691231-180010_main.mp4"' == response.headers["content-disposition"]
    assert len(calls) == 1
    info = client.get(url.replace("/clip?", "/clip/info?"), headers=headers)
    assert info.json() == {"sub": "available", "main": "available"}


def test_errors(setup):
    cfg, db, client = setup
    headers = {"x-apphub-token": tok(["cams"])}
    url = "/api/clip?camera=cam&quality=main&start=10&end="
    for end in ("10", "9", "311", "nan", "bogus"):
        assert client.get(url + end, headers=headers).status_code == 400
    db.clip_segments.return_value = ([seg()], 60)
    response = client.get(url + "80", headers=headers)
    assert response.status_code == 425 and response.json()["retry_after"] == 10
    db.clip_segments.return_value = ([seg(deleted_at=True)], 60)
    response = client.get(url + "40", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "main quality footage no longer available"


def test_audio_fallback_and_path_safety(setup, monkeypatch):
    cfg, db, _ = setup
    clips = Clips(db, cfg)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0 if "-an" in cmd else 1)

    monkeypatch.setattr("cams_record.clips.subprocess.run", run)
    assert clips.build("cam", "sub", 10, 40).read_bytes() == b"mp4"
    assert len(calls) == 2 and "-an" in calls[-1]
    db.clip_segments.return_value = ([seg(path_unused=True) | {"path": "../outside.mp4"}], 60)
    assert clips.availability("cam", "sub", 10, 40)[0] == "gone"


def test_protection_watermark_and_fail_soft(monkeypatch):
    db = Mock()
    db.detection_events.return_value = [{"camera_id": "cam", "start_ts": 100, "end_ts": None}]
    monkeypatch.setattr("cams_record.protection.time.time", lambda: 1000)
    worker = DetectionProtection(db, DEFAULTS["detection_protection"])
    worker.run_once()
    db.protect_window.assert_called_once_with("cam", 95, 115, 1000 + 14 * 86400)
    assert worker.watermark == 1000
    db.detection_events.side_effect = RuntimeError("grant missing")
    worker.run_once()
    assert worker.watermark == 1000


def test_cache_cleanup(setup):
    cfg, db, _ = setup
    clips = Clips(db, cfg)
    clips.cache.mkdir(parents=True)
    old = clips.cache / "old.mp4"
    old.write_bytes(b"old")
    import os
    os.utime(old, (1, 1))
    clips.cleanup()
    assert not old.exists()


def test_clip_audience_and_status_boundary(setup):
    import time

    import jwt

    _, _, client = setup
    for aud in (None, "cams-detect"):
        claims = {"sub": "test", "roles": ["cams"], "exp": time.time() + 60}
        if aud:
            claims["aud"] = aud
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        assert client.get("/api/clip/info?camera=cam&start=10&end=40",
                          headers={"x-apphub-token": token}).status_code == 401
    headers = {"x-apphub-token": tok(["cams"])}
    assert client.get("/api/status", headers=headers).status_code == 403
    assert client.get("/api/metrics", headers=headers).status_code == 403


def test_response_lease_survives_eviction(setup, monkeypatch):
    cfg, db, _ = setup
    clips = Clips(db, cfg)

    def run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("cams_record.clips.subprocess.run", run)
    path = clips.build("cam", "sub", 10, 40, lease=True)
    clips.cfg["max_cache_gb"] = 0.000000001
    clips.cleanup()
    assert path.read_bytes() == b"mp4"
    path.unlink()


def test_real_ffmpeg_concat(setup, tmp_path):
    import json
    import shutil
    import subprocess

    from tests.conftest import make_clip

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not installed")
    cfg, db, _ = setup
    cfg["recording"]["ffmpeg"] = "ffmpeg"
    root = Path(cfg["recording"]["root"])
    make_clip(root / "a.mp4", 3)
    shutil.copy(root / "a.mp4", root / "b.mp4")
    db.clip_segments.return_value = ([seg(100, 103) | {"path": "a.mp4", "codec": "h264"},
                                      seg(103, 106) | {"path": "b.mp4", "codec": "h264"}], 106)
    path = Clips(db, cfg).build("cam", "sub", 101, 105)
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                            capture_output=True, check=True)
    info = json.loads(result.stdout)
    assert 3 <= float(info["format"]["duration"]) <= 5
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}


def test_timeout_leaves_no_cached_output(setup, monkeypatch):
    import subprocess

    from cams_record.clips import ClipError

    cfg, db, _ = setup
    clips = Clips(db, cfg)

    def run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"unfinished")
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr("cams_record.clips.subprocess.run", run)
    with pytest.raises(ClipError) as error:
        clips.build("cam", "sub", 10, 40)
    assert error.value.status == 504
    assert not list(clips.cache.iterdir())


@pytest.mark.parametrize("gap,tolerance,state", [
    (0.06, 2, "available"), (0.4, 2, "available"), (2, 2, "available"),
    (2.01, 2, "gone"), (0.4, 0.2, "gone"), (0.4, 0.5, "available"),
])
def test_gap_tolerance(gap, tolerance, state):
    assert select_segments([seg(), seg(60 + gap, 120)], 120, 10, 100, tolerance)[0] == state


def test_restart_duplicates_choose_furthest_chain():
    rows = [seg(1, 60.970), seg(1.999, 61.969),
            seg(61, 120.970), seg(61.999, 121.969)]
    state, selected = select_segments(rows, 121.969, 10, 120)
    assert state == "available"
    assert selected == [rows[1], rows[3]]
    cmd = ffmpeg_command("ffmpeg", "list", "out", selected, 10, 120)
    assert float(cmd[cmd.index("-ss") + 1]) == 10 - rows[1]["start_ts"]


def test_overlap_only_used_when_needed():
    first, duplicate, next_row = seg(0, 60), seg(1, 60.5), seg(60.4, 120)
    assert select_segments([first, duplicate, next_row], 120, 0, 100)[1] == [first, next_row]
    assert select_segments([first, seg(50, 120)], 120, 0, 100)[1] == [first, seg(50, 120)]


def test_availability_uses_configured_gap(setup):
    cfg, db, _ = setup
    db.clip_segments.return_value = ([seg(), seg(60.4, 120)], 120)
    (Path(cfg["recording"]["root"]) / "60.4.mp4").write_bytes(b"segment")
    cfg["clips"]["gap_tolerance_s"] = 0.2
    assert Clips(db, cfg).availability("cam", "sub", 10, 100)[0] == "gone"
    cfg["clips"]["gap_tolerance_s"] = 0.5
    assert Clips(db, cfg).availability("cam", "sub", 10, 100)[0] == "available"


def test_selection_never_replays_same_file():
    rows = [seg(0, 60), dict(seg(0.999, 60.999), path="a.mp4"), seg(60, 120)]
    state, selected = select_segments(rows, 120, 10, 100)
    assert state == "available"
    assert [r["path"] for r in selected] == ["a.mp4", "60.mp4"]
    assert select_segments(rows[:2], 60.999, 10, 60.5)[0] != "available"
