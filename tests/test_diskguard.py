import logging
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from cams_record.config import DEFAULTS, deep_merge, resolve_camera
from cams_record.diskguard import LEVEL_ALL_STOPPED, LEVEL_MAIN_STOPPED, LEVEL_OK, DiskGuard
from cams_record.indexer import Indexer
from cams_record.recorder import StreamRecorder
from cams_record.service import Service
from cams_record.watchdog import Backoff
from cams_record.web import create_app


class Disk:
    """Fake free space, in GB."""

    def __init__(self, gb):
        self.gb = gb

    def __call__(self, _path):
        if self.gb is None:
            raise OSError("no such volume")
        return int(self.gb * 1e9)


def make_guard(gb):
    disk = Disk(gb)
    return DiskGuard("/nvr", 200, 250, disk), disk


def rec_cfg(tmp_path, **kw):
    cfg = deep_merge(DEFAULTS["recording"], {"root": str(tmp_path / "nvr"),
                                              "state_dir": str(tmp_path.parent / (tmp_path.name + "-state")),
                                             "audio": "off"})
    cfg.update(kw)
    return cfg


def wait_for(cond, timeout=15):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.05)
    assert cond()


@pytest.fixture
def fake_ffmpeg(tmp_path):
    """A wrapper that runs until signalled, and records that it was started."""
    runs = tmp_path / "runs"
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(f"import time\nopen({str(runs)!r}, 'a').write('x')\ntime.sleep(60)\n")
    launcher = tmp_path / "fake_ffmpeg.sh"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    launcher.chmod(0o755)
    return str(launcher), runs


def ffmpeg_starts(runs):
    return len(runs.read_text()) if runs.exists() else 0


@pytest.mark.parametrize("gb,level", [(300, LEVEL_OK), (200, LEVEL_OK), (199.9, LEVEL_MAIN_STOPPED),
                                      (100, LEVEL_MAIN_STOPPED), (99.9, LEVEL_ALL_STOPPED)])
def test_thresholds_are_strictly_below(gb, level):
    g, _ = make_guard(gb)
    assert g.check() == level


def test_hysteresis_holds_until_above_resume():
    g, disk = make_guard(300)
    assert g.check() == LEVEL_OK
    disk.gb = 150
    assert g.check() == LEVEL_MAIN_STOPPED
    assert g.blocked("main") and not g.blocked("sub")
    disk.gb = 220  # above the minimum, not above resume
    assert g.check() == LEVEL_MAIN_STOPPED
    disk.gb = 250  # resume is strictly above
    assert g.check() == LEVEL_MAIN_STOPPED
    disk.gb = 250.1
    assert g.check() == LEVEL_OK
    assert not g.blocked("main")


def test_sub_also_waits_for_the_resume_threshold():
    g, disk = make_guard(90)
    assert g.check() == LEVEL_ALL_STOPPED
    assert g.blocked("main") and g.blocked("sub")
    for gb in (150, 240):  # recovering past half the minimum and the minimum does not restart sub
        disk.gb = gb
        assert g.check() == LEVEL_ALL_STOPPED
    disk.gb = 260
    assert g.check() == LEVEL_OK


def test_startup_below_minimum_refuses_every_quality():
    g, _ = make_guard(150)
    assert g.check(startup=True) == LEVEL_ALL_STOPPED
    assert g.blocked("main") and g.blocked("sub")
    g, _ = make_guard(220)  # between the thresholds: nothing to hold back at startup
    assert g.check(startup=True) == LEVEL_OK


def test_unreadable_disk_keeps_the_level(caplog):
    g, disk = make_guard(None)
    with caplog.at_level(logging.WARNING):
        assert g.check() == LEVEL_OK
    assert "cannot read free space" in caplog.text
    disk.gb = 150
    assert g.check() == LEVEL_MAIN_STOPPED
    disk.gb = None
    assert g.check() == LEVEL_MAIN_STOPPED


def test_zero_minimum_disables_the_guard():
    g = DiskGuard("/nvr", 0, 0, Disk(0))
    assert g.check(startup=True) == LEVEL_OK


def test_level_change_is_logged_once_at_error(caplog):
    g, disk = make_guard(150)
    with caplog.at_level(logging.INFO):
        g.check()
        g.check()
    assert [r.levelname for r in caplog.records] == ["ERROR"]


def test_real_disk_usage(tmp_path):
    assert DiskGuard(str(tmp_path), 0, 0).check() == LEVEL_OK
    assert DiskGuard(str(tmp_path), 1e9, 1e9).check() == LEVEL_ALL_STOPPED  # an exabyte is never free


def test_recorder_refuses_to_start_below_minimum(tmp_path, caplog):
    """No ffmpeg, no restart counted, state disk_full logged at ERROR; resumes above the threshold."""
    g, disk = make_guard(150)
    idx = Indexer(None)
    r = StreamRecorder("cam", "main", "/x.mp4", rec_cfg(tmp_path, ffmpeg="false"), idx, g)
    r.backoff = Backoff(0.05, 0.2)
    with caplog.at_level(logging.INFO):
        r.start()
        wait_for(lambda: r.state == "disk_full")
    assert r.restarts.total == 0
    assert "low disk space" in r.last_error
    assert idx._status[("cam", "main")]["state"] == "disk_full"
    assert any(rec.levelname == "ERROR" and "cam/main: disk_full" in rec.message for rec in caplog.records)
    disk.gb = 300
    g.check()
    wait_for(lambda: r.restarts.total >= 1)  # back to the normal supervisor (ffmpeg=false exits)
    r.stop()
    r.join(10)
    assert r.state == "stopped"
    assert "low disk space" not in r.last_error


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX ffmpeg stand-in")
def test_running_main_stops_and_sub_keeps_recording(tmp_path, fake_ffmpeg):
    launcher, runs = fake_ffmpeg
    g, disk = make_guard(300)
    idx = Indexer(None)
    cfg = rec_cfg(tmp_path, ffmpeg=launcher, stop_timeout=1)
    main = StreamRecorder("cam", "main", "/x.mp4", cfg, idx, g)
    sub = StreamRecorder("cam", "sub", "/x.mp4", cfg, idx, g)
    main.start()
    sub.start()
    wait_for(lambda: main._proc is not None and sub._proc is not None and ffmpeg_starts(runs) == 2)

    disk.gb = 150
    g.check()
    wait_for(lambda: main.state == "disk_full")
    assert main._proc is None and main.restarts.total == 0
    assert sub._proc is not None and sub._proc.poll() is None

    disk.gb = 90
    g.check()
    wait_for(lambda: sub.state == "disk_full")
    assert sub._proc is None and sub.restarts.total == 0

    disk.gb = 300
    g.check()
    wait_for(lambda: main._proc is not None and sub._proc is not None and ffmpeg_starts(runs) == 4)
    assert main.state == sub.state == "starting"  # a new run, until its first segment closes
    assert main.restarts.total == sub.restarts.total == 0
    for r in (main, sub):
        r.stop()
    for r in (main, sub):
        r.join(10)
    assert main.state == sub.state == "stopped"


def test_running_sub_may_respawn_ffmpeg_below_minimum(tmp_path):
    """A go2rtc hiccup between the two limits must not turn "keep sub running" into a stop."""
    g, disk = make_guard(300)
    g.check()
    disk.gb = 150
    g.check()
    r = StreamRecorder("cam", "sub", "/x.mp4", rec_cfg(tmp_path, ffmpeg="false"), Indexer(None), g)
    r.backoff = Backoff(0.05, 0.2)
    r.start()
    wait_for(lambda: r.restarts.total >= 1)
    assert r.state != "disk_full"
    r.stop()
    r.join(10)


def test_backoff_ends_as_soon_as_the_guard_blocks(tmp_path):
    g, disk = make_guard(300)
    r = StreamRecorder("cam", "main", "/x.mp4", rec_cfg(tmp_path, ffmpeg="false"), Indexer(None), g)
    r.backoff = Backoff(60, 60)
    r.start()
    wait_for(lambda: r.state == "backoff")
    disk.gb = 150
    g.check()
    wait_for(lambda: r.state == "disk_full", timeout=5)
    r.stop()
    r.join(10)


@pytest.fixture
def service(tmp_path):
    cfg = deep_merge(DEFAULTS, {"auth": {"mode": "none"},
                                "recording": {"root": str(tmp_path),
                                              "state_dir": str(tmp_path.parent / (tmp_path.name + "-state")),
                                              "ffmpeg": "false", "audio": "off"}})
    cams = {"cam": resolve_camera("cam", {"main_stream": "/x.mp4", "sub_stream": "/y.mp4"})}
    return Service(cfg, cams, db=None), cfg


def test_service_start_below_minimum_refuses_all_recorders(service):
    svc, _ = service
    svc.guard._free_bytes = Disk(150)
    svc.start()
    try:
        recs = svc.recorders.values()
        wait_for(lambda: all(r.state == "disk_full" for r in recs))
        assert all(r.restarts.total == 0 for r in recs)
    finally:
        svc.stop()
    assert all(r.state == "stopped" for r in svc.recorders.values())


def test_status_and_metrics_show_disk_full(service):
    svc, cfg = service
    svc.guard._free_bytes = Disk(150)
    svc.start()
    try:
        wait_for(lambda: all(r.state == "disk_full" for r in svc.recorders.values()))
        client = TestClient(create_app(svc, cfg))
        st = client.get("/api/status").json()
        assert {s["state"] for s in st["streams"]} == {"disk_full"}
        assert st["disk"]["guard"] == "all_stopped"
        assert st["disk"]["min_free_gb"] == 200 and st["disk"]["resume_free_gb"] == 250
        text = client.get("/api/metrics").text
        assert 'cams_record_disk_full{camera="cam",quality="main"} 1' in text
        assert 'cams_record_recording{camera="cam",quality="main"} 0' in text
        assert "cams_record_disk_guard_level 2" in text
    finally:
        svc.stop()
