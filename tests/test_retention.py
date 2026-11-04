"""Retention janitor. Every behaviour test runs on an in-memory fake and on MariaDB (when reachable)."""

import calendar
import logging
import os
import time

import pytest
from fastapi.testclient import TestClient

from cams_record.config import DEFAULTS, deep_merge, resolve_camera
from cams_record.db import DBUnavailable
from cams_record.diskguard import LEVEL_MAIN_STOPPED
from cams_record.retention import Janitor, UnsafePath, live_hours, remove_empty_date_dirs, resolve_segment_path
from cams_record.segments import rel_path_for, to_dt
from cams_record.service import Service
from cams_record.web import create_app

NOW = calendar.timegm((2026, 11, 4, 21, 32, 0, 0, 0, 0))
DAY = 86400
SEG = 60
OLD = 30 * DAY


class FakeDB:
    """In-memory stand-in with the semantics of the SQL in db.py."""

    def __init__(self):
        self.rows: list[dict] = []
        self.fail_mark = False

    def add_row(self, **r):
        r["id"] = len(self.rows) + 1
        self.rows.append(r)
        return r["id"]

    def _live(self):
        return [r for r in self.rows if r["deleted_at"] is None]

    def live_ids(self):
        return {r["id"] for r in self._live()}

    def oldest_segments(self, quality, end_before, limit, camera_id=None, start_before=None, present_only=False):
        rows = [r for r in self._live() if not r["protected"] and r["quality"] == quality and r["end_ts"] < end_before
                and (camera_id is None or r["camera_id"] == camera_id)
                and (start_before is None or r["start_ts"] < start_before)
                and not (present_only and r["missing"])]
        rows.sort(key=lambda r: (r["start_ts"], r["id"]))
        return [{**r, "missing": bool(r["missing"])} for r in rows[:limit]]

    def segment_totals(self):
        out = []
        for cam, q in sorted({(r["camera_id"], r["quality"]) for r in self._live()}):
            rows = [r for r in self._live() if (r["camera_id"], r["quality"]) == (cam, q)]
            there = [r for r in rows if not r["missing"]]
            out.append({"camera_id": cam, "quality": q, "segments": len(there),
                        "bytes": sum(r["size_bytes"] for r in there),
                        "protected_bytes": sum(r["size_bytes"] for r in there if r["protected"]),
                        "oldest_start_ts": min((r["start_ts"] for r in there), default=None)})
        return out

    def mark_deleted(self, ids):
        if self.fail_mark:
            raise DBUnavailable("gone")
        hits = [r for r in self._live() if r["id"] in ids]
        for r in hits:
            r["deleted_at"] = NOW
        return len(hits)


class MariaStore:
    """The real Database, plus what the tests need to set rows up."""

    def __init__(self, db):
        self.db = db
        self.fail_mark = False

    def __getattr__(self, name):
        return getattr(self.db, name)

    def mark_deleted(self, ids):
        if self.fail_mark:
            raise DBUnavailable("gone")
        return self.db.mark_deleted(ids)

    def add_row(self, **r):
        self.db.insert_segment({**r, "duration_ms": int((r["end_ts"] - r["start_ts"]) * 1000)})

        def q(cur):
            key = (r["camera_id"], r["quality"], to_dt(r["start_ts"]))
            cur.execute("UPDATE segments SET protected = %s, missing = %s "
                        "WHERE camera_id = %s AND quality = %s AND start_ts = %s",
                        (r["protected"], r["missing"], *key))
            cur.execute("SELECT id FROM segments WHERE camera_id = %s AND quality = %s AND start_ts = %s", key)
            return cur.fetchone()[0]

        return self.db.run(q)

    def live_ids(self):
        return self.db.run(lambda cur: (cur.execute("SELECT id FROM segments WHERE deleted_at IS NULL"),
                                        {r[0] for r in cur.fetchall()})[1])


class Env:
    def __init__(self, store, root):
        self.store = store
        self.root = root

    def add(self, age_s, cam="cam", q="main", size=100, protected=False, missing=False, path=None, file=True):
        """A segment that started ``age_s`` seconds before NOW, with a file unless ``file`` is false."""
        start = NOW - age_s
        rel = rel_path_for(cam, q, start) if path is None else path
        if file and path is None:
            f = self.root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
        return self.store.add_row(camera_id=cam, quality=q, start_ts=start, end_ts=start + SEG, path=rel,
                                  size_bytes=size, protected=int(protected), missing=int(missing), deleted_at=None)

    def janitor(self, **kw):
        cfg = deep_merge(DEFAULTS["retention"], {"dry_run": False, **kw})
        return Janitor(self.store, str(self.root), cfg, SEG, clock=lambda: NOW)

    def gone(self, *ids):
        return not set(ids) & self.store.live_ids()

    def exists(self, age_s, cam="cam", q="main"):
        return (self.root / rel_path_for(cam, q, NOW - age_s)).exists()


@pytest.fixture(params=["fake", "mariadb"])
def env(request, tmp_path):
    root = tmp_path / "nvr"
    root.mkdir()
    store = FakeDB() if request.param == "fake" else MariaStore(request.getfixturevalue("db"))
    return Env(store, root)


def days(n):
    return int(n * DAY)


def budget_gb(n_bytes):
    return n_bytes / 1e9


# -- age rule -----------------------------------------------------------------
def test_age_rule_per_quality(env):
    old_main = env.add(days(3) + 60, q="main")
    new_main = env.add(days(3) - 60, q="main")
    old_sub = env.add(days(14) + 60, q="sub")
    new_sub = env.add(days(3) + 60, q="sub")  # past main_days, but sub keeps 14 days
    res = env.janitor().run_once()
    assert res["result"] == "ok" and res["segments"] == 2 and res["bytes"] == 200
    assert (res["age_main"], res["age_sub"], res["budget_main"], res["budget_sub"]) == (1, 1, 0, 0)
    assert env.gone(old_main, old_sub) and env.store.live_ids() == {new_main, new_sub}
    assert not env.exists(days(3) + 60) and env.exists(days(3) - 60)
    assert not env.exists(days(14) + 60, q="sub") and env.exists(days(3) + 60, q="sub")


def test_age_rule_camera_override(env):
    shorter = env.add(days(2), cam="garage", q="main")
    longer = env.add(days(5), cam="yard", q="main")
    other_quality = env.add(days(5), cam="garage", q="sub")  # the override sets main only: sub stays at 14 days
    default = env.add(days(5), cam="other", q="main")
    janitor = env.janitor(cameras={"garage": {"main_days": 1}, "yard": {"main_days": 7}})
    assert janitor.run_once()["segments"] == 2
    assert env.gone(shorter, default) and env.store.live_ids() == {longer, other_quality}


# -- budget rule --------------------------------------------------------------
def test_budget_deletes_oldest_main_before_any_sub(env):
    sub_oldest = env.add(3000, q="sub")
    m1 = env.add(2000, q="main")
    m2 = env.add(1500, q="main")
    m3 = env.add(1000, q="main")
    res = env.janitor(max_total_gb=budget_gb(250)).run_once()  # 400 indexed
    assert (res["budget_main"], res["budget_sub"], res["age_main"]) == (2, 0, 0)
    assert env.gone(m1, m2) and env.store.live_ids() == {m3, sub_oldest}


def test_budget_moves_on_to_oldest_sub_once_main_is_used_up(env):
    m = env.add(1000, q="main")
    s1 = env.add(3000, q="sub")
    s2 = env.add(2000, q="sub")
    s3 = env.add(1500, q="sub")
    res = env.janitor(max_total_gb=budget_gb(150)).run_once()
    assert (res["budget_main"], res["budget_sub"]) == (1, 2)
    assert env.gone(m, s1, s2) and env.store.live_ids() == {s3}


def test_exactly_on_budget_deletes_nothing(env):
    env.add(1000)
    env.add(900)
    assert env.janitor(max_total_gb=budget_gb(200)).run_once()["segments"] == 0


def test_age_deletions_count_towards_the_budget(env):
    aged = env.add(days(4), q="main")
    keep = env.add(1000, q="main")
    res = env.janitor(max_total_gb=budget_gb(100)).run_once()
    assert (res["age_main"], res["budget_main"]) == (1, 0)
    assert env.gone(aged) and env.store.live_ids() == {keep}


def test_budget_ignores_rows_without_a_file(env):
    env.add(days(1), size=10**6, missing=True, file=False)
    keep = env.add(1000, size=100)
    assert env.janitor(max_total_gb=budget_gb(100)).run_once()["segments"] == 0
    assert keep in env.store.live_ids()


# -- never deleted ------------------------------------------------------------
def test_protected_segments_survive_age_and_budget(env, caplog):
    old = env.add(days(30), protected=True)
    newer = env.add(1000, protected=True, q="sub")
    with caplog.at_level(logging.WARNING):
        res = env.janitor(max_total_gb=budget_gb(1), batch_size=10).run_once()
    assert res["segments"] == 0
    assert env.store.live_ids() == {old, newer} and env.exists(days(30))
    assert "over budget" in caplog.text


def test_protected_bytes_count_but_are_stepped_over(env):
    prot = env.add(days(1), size=300, protected=True)
    deletable = env.add(1000, size=100)
    res = env.janitor(max_total_gb=budget_gb(350)).run_once()
    assert res["budget_main"] == 1 and env.gone(deletable) and env.store.live_ids() == {prot}


def test_recent_segments_survive_even_a_tiny_budget(env):
    ended_121s_ago = env.add(3 * SEG + 1)
    ended_120s_ago = env.add(3 * SEG, q="sub")
    ended_60s_ago = env.add(2 * SEG, cam="other")
    assert env.janitor(max_total_gb=budget_gb(1)).run_once()["segments"] == 1
    assert env.gone(ended_121s_ago) and env.store.live_ids() == {ended_120s_ago, ended_60s_ago}


def test_margin_applies_to_a_very_short_age_limit(env):
    keep = env.add(2 * SEG)
    assert env.janitor(main_days=1 / DAY).run_once()["segments"] == 0  # past the limit, but too recent
    assert keep in env.store.live_ids()


# -- path safety --------------------------------------------------------------
def link(target, name):
    try:
        os.symlink(target, name)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")


def assert_refused(env, rel, victim, check=os.path.exists):
    rid = env.add(OLD, path=rel)
    res = env.janitor().run_once()
    assert res["result"] == "ok"
    assert res["skipped"] == 1 and res["segments"] == 0
    assert check(victim), "the file must not be touched"
    assert rid in env.store.live_ids(), "the row must stay as it is"


def outside_file(tmp_path, name="outside.mp4"):
    victim = tmp_path / name
    victim.parent.mkdir(exist_ok=True)
    victim.write_bytes(b"x")
    return victim


def test_absolute_path_outside_root(env, tmp_path):
    victim = outside_file(tmp_path)
    assert_refused(env, str(victim), victim)


def test_absolute_path_inside_root_is_refused_too(env):
    victim = outside_file(env.root, "a.mp4")
    assert_refused(env, str(victim), victim)


@pytest.mark.parametrize("rel", ["../outside.mp4", "cam/main/../../../outside.mp4", "cam/../../outside.mp4"])
def test_traversal(env, tmp_path, rel):
    assert_refused(env, rel, outside_file(tmp_path))


def test_traversal_that_stays_inside_root(env):
    assert_refused(env, "cam/../a.mp4", outside_file(env.root, "a.mp4"))


def test_sibling_sharing_the_root_prefix(env, tmp_path):
    victim = outside_file(tmp_path / "nvr-other", "a.mp4")  # a string prefix check would accept this
    assert_refused(env, "../nvr-other/a.mp4", victim)


def test_symlink_to_a_file_outside(env, tmp_path):
    victim = outside_file(tmp_path)
    link(victim, env.root / "link.mp4")
    assert_refused(env, "link.mp4", victim)
    assert (env.root / "link.mp4").is_symlink()


def test_symlink_to_a_file_inside(env):
    victim = outside_file(env.root, "real.mp4")
    link(victim, env.root / "link.mp4")
    assert_refused(env, "link.mp4", victim)
    assert (env.root / "link.mp4").is_symlink()


def test_dangling_symlink(env):
    link(env.root / "nothing.mp4", env.root / "link.mp4")
    assert_refused(env, "link.mp4", env.root / "link.mp4", check=os.path.lexists)


def test_symlinked_directory(env, tmp_path):
    victim = outside_file(tmp_path / "outside", "a.mp4")
    link(tmp_path / "outside", env.root / "cam")
    assert_refused(env, "cam/a.mp4", victim)


def test_symlinked_directory_inside_root(env):
    victim = outside_file(env.root / "real", "a.mp4")
    link(env.root / "real", env.root / "cam")
    assert_refused(env, "cam/a.mp4", victim)


@pytest.mark.parametrize("name", ["a.txt", "a.mp4.bad", "a.MP4", "a", ".mp4", "a.mp4.tmp"])
def test_other_extensions(env, name):
    assert_refused(env, name, outside_file(env.root, name))


@pytest.mark.parametrize("rel", ["", ".", "./", "cam/.."])
def test_root_itself(env, rel):
    assert_refused(env, rel, env.root)


def test_directory_named_like_a_recording(env):
    victim = env.root / "dir.mp4"
    victim.mkdir()
    assert_refused(env, "dir.mp4", victim)


def test_refusal_is_logged(env, caplog):
    env.add(OLD, path="../outside.mp4")
    with caplog.at_level(logging.WARNING):
        env.janitor().run_once()
    assert "skipping segment" in caplog.text and "../outside.mp4" in caplog.text


def test_unsafe_rows_do_not_block_the_queue(env):
    bad = {env.add(OLD + i, path=f"../outside{i}.mp4") for i in range(3)}
    good = env.add(OLD - 10)
    janitor = env.janitor(batch_size=3)
    first = janitor.run_once()
    assert first["skipped"] == 3 and first["segments"] == 0
    assert janitor.run_once()["segments"] == 1
    assert env.gone(good) and env.store.live_ids() == bad
    assert janitor.status()["skipped_total"] == 3


def test_undeletable_file_keeps_its_row(env, monkeypatch):
    rid = env.add(OLD)

    def refuse(_path):
        raise PermissionError("no")

    monkeypatch.setattr("cams_record.retention.os.unlink", refuse)
    res = env.janitor().run_once()
    assert res["failed"] == 1 and res["segments"] == 0 and rid in env.store.live_ids()


def test_resolve_accepts_a_normal_segment(tmp_path):
    root = os.path.realpath(tmp_path)
    rel = "cam/main/2026/11/04/21/20261104T213200.mp4"
    assert resolve_segment_path(root, rel) == (os.path.join(root, *rel.split("/")), False)
    (tmp_path / "a.mp4").write_bytes(b"x")
    assert resolve_segment_path(root, "a.mp4")[1] is True
    with pytest.raises(UnsafePath):
        resolve_segment_path(root, "a\0.mp4")


def test_missing_root_stops_the_pass(tmp_path):
    env = Env(FakeDB(), tmp_path / "unmounted")
    rid = env.add(OLD, file=False)
    res = env.janitor().run_once()
    assert res["result"] == "error" and "unusable recording root" in res["error"]
    assert rid in env.store.live_ids()


# -- deleting -----------------------------------------------------------------
def test_missing_file_still_marks_the_row(env):
    rid = env.add(OLD, file=False, size=500)
    res = env.janitor().run_once()
    assert res["segments"] == 1 and res["bytes"] == 0 and res["already_missing"] == 1
    assert env.gone(rid)


def test_crash_between_unlink_and_mark_recovers(env):
    rid = env.add(OLD)
    janitor = env.janitor()
    env.store.fail_mark = True
    res = janitor.run_once()
    assert res["result"] == "error" and not env.exists(OLD) and rid in env.store.live_ids()
    env.store.fail_mark = False
    res = janitor.run_once()
    assert res["result"] == "ok" and res["already_missing"] == 1 and env.gone(rid)


def test_batch_limit_oldest_first(env):
    ages = [OLD + i * 60 for i in range(7)]
    ids = {age: env.add(age) for age in ages}
    janitor = env.janitor(batch_size=3)
    res = janitor.run_once()
    assert res["segments"] == 3 and res["batch_full"] is True and janitor.backlog is True
    assert env.store.live_ids() == {ids[a] for a in ages[:4]}  # the three oldest went
    assert janitor.run_once()["segments"] == 3
    res = janitor.run_once()
    assert res["segments"] == 1 and janitor.backlog is False
    assert env.store.live_ids() == set()


def test_batch_limit_spans_rules_and_cameras(env):
    env.add(days(30), cam="a", q="main")
    env.add(days(29), cam="b", q="main")
    env.add(days(28), cam="a", q="sub")
    env.add(1000, cam="a", q="main")
    res = env.janitor(batch_size=2, max_total_gb=budget_gb(1)).run_once()
    assert res["segments"] == 2 and (res["age_main"], res["budget_main"]) == (2, 0)


def test_rows_are_committed_in_small_chunks(env, monkeypatch):
    monkeypatch.setattr("cams_record.retention.COMMIT_EVERY", 2)
    ids = [env.add(OLD + i * 60) for i in range(5)]
    calls = []
    real = env.store.mark_deleted
    env.store.mark_deleted = lambda chunk: (calls.append(len(chunk)), real(chunk))[1]
    assert env.janitor().run_once()["segments"] == 5
    assert calls == [2, 2, 1] and env.gone(*ids)


def test_stop_between_chunks(env, monkeypatch):
    monkeypatch.setattr("cams_record.retention.COMMIT_EVERY", 2)
    for i in range(4):
        env.add(OLD + i * 60)
    janitor = env.janitor()
    real = env.store.mark_deleted

    def mark(chunk):
        janitor._stop.set()
        return real(chunk)

    env.store.mark_deleted = mark
    assert janitor.run_once()["segments"] == 2
    assert len(env.store.live_ids()) == 2


# -- empty directories --------------------------------------------------------
def test_empty_date_dirs_are_removed_bottom_up(env):
    env.add(OLD)
    keep = env.add(OLD - 3 * 3600)  # the same UTC day or the next, another hour directory
    res = env.janitor(main_days=29.99).run_once()
    assert res["segments"] == 1 and res["dirs_removed"] >= 1
    assert keep in env.store.live_ids()
    assert (env.root / rel_path_for("cam", "main", NOW - OLD + 3 * 3600)).parent.is_dir()
    res = env.janitor(main_days=1).run_once()
    assert res["segments"] == 1
    assert [p.name for p in (env.root / "cam" / "main" / "2026").iterdir()] == []  # the year holds the live hours


def test_cleanup_stops_at_the_quality_directory(tmp_path):
    (tmp_path / "cam/main/2025/10/04/21").mkdir(parents=True)
    assert remove_empty_date_dirs(str(tmp_path), ["cam/main/2025/10/04/21/x.mp4"], live_hours(NOW)) == 4
    assert (tmp_path / "cam" / "main").is_dir() and list((tmp_path / "cam" / "main").iterdir()) == []


def test_dir_with_other_files_stays(env):
    env.add(OLD)
    bad = (env.root / rel_path_for("cam", "main", NOW - OLD)).with_suffix(".mp4.bad")
    bad.write_bytes(b"x")
    res = env.janitor().run_once()
    assert res["segments"] == 1 and res["dirs_removed"] == 0 and bad.exists()


def test_shallow_paths_never_remove_anything(tmp_path):
    (tmp_path / "cam" / "main").mkdir(parents=True)
    assert remove_empty_date_dirs(str(tmp_path), ["x.mp4", "cam/x.mp4", "cam/main/x.mp4"], set()) == 0
    assert (tmp_path / "cam" / "main").is_dir()


def test_live_hours_stay(tmp_path):
    hours = live_hours(NOW)
    assert hours == {"2026/11/04/20", "2026/11/04/21", "2026/11/04/22"}
    (tmp_path / "cam/main/2026/11/04/21").mkdir(parents=True)
    assert remove_empty_date_dirs(str(tmp_path), ["cam/main/2026/11/04/21/x.mp4"], hours) == 0
    assert (tmp_path / "cam/main/2026/11/04/21").is_dir()


def test_recorder_dirs_survive_a_budget_delete(env):
    """With a tiny budget a segment from the current hour goes; recorders still need its directory."""
    env.add(10 * SEG)
    res = env.janitor(max_total_gb=budget_gb(1)).run_once()
    assert res["segments"] == 1 and res["dirs_removed"] == 0
    assert (env.root / rel_path_for("cam", "main", NOW - 10 * SEG)).parent.is_dir()


# -- dry run ------------------------------------------------------------------
def test_dry_run_deletes_nothing_and_reports(env, caplog):
    aged_main = env.add(days(4), size=2_000_000_000)
    aged_sub = env.add(days(15), q="sub", size=500_000_000)
    over = env.add(1000, size=100_000_000)
    prot = env.add(days(30), q="sub", protected=True, size=7)
    janitor = env.janitor(dry_run=True, max_total_gb=0.05)
    with caplog.at_level(logging.INFO):
        res = janitor.run_once()
    assert env.store.live_ids() == {aged_main, aged_sub, over, prot}
    assert env.exists(days(4)) and env.exists(days(15), q="sub") and env.exists(1000)
    assert res["result"] == "dry_run" and res["segments"] == 3 and res["bytes"] == 2_600_000_000
    assert ("retention dry-run: would delete 3 segments (2.60 GB) [age main=1 sub=1, budget main=1 sub=0]"
            in caplog.text)
    st = janitor.status()
    assert st["would_delete"]["segments"] == 3 and st["would_delete"]["bytes"] == 2_600_000_000
    assert st["would_delete"]["age_main"] == 1 and st["would_delete"]["budget_main"] == 1
    assert st["deleted"] == {"total_segments": 0, "total_bytes": 0, "last_segments": 0, "last_bytes": 0}
    assert janitor.backlog is False


def test_dry_run_refuses_unsafe_paths_too(env):
    env.add(OLD, path="../outside.mp4")
    good = env.add(OLD - 60)
    res = env.janitor(dry_run=True).run_once()
    assert res["skipped"] == 1 and res["segments"] == 1 and good in env.store.live_ids()


def test_dry_run_leaves_directories_alone(env):
    env.add(OLD)
    res = env.janitor(dry_run=True).run_once()
    assert "dirs_removed" not in res and env.exists(OLD)


# -- running ------------------------------------------------------------------
def wait_for(cond, timeout=10):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)
    assert cond()


def test_thread_runs_an_urgent_pass_on_trigger(env):
    env.add(OLD)
    janitor = env.janitor(interval_s=3600)
    janitor.start()
    try:
        janitor.trigger()
        wait_for(lambda: janitor.last_run is not None)
        assert janitor.last_run["urgent"] is True and janitor.last_run["segments"] == 1
    finally:
        janitor.stop()
    assert not janitor._thread.is_alive()


def test_thread_runs_on_its_interval(env):
    janitor = env.janitor(interval_s=0.02)
    janitor.start()
    try:
        wait_for(lambda: janitor.last_run is not None)
        assert janitor.last_run["urgent"] is False
    finally:
        janitor.stop()


def test_thread_lowers_its_own_priority(env, monkeypatch):
    seen = []
    monkeypatch.setattr("cams_record.retention.os.nice", seen.append, raising=False)
    janitor = env.janitor(nice=7, interval_s=3600)
    janitor.start()
    janitor.stop()
    assert seen == [7]


def test_db_failure_ends_the_pass_not_the_thread(tmp_path):
    class Down(FakeDB):
        def segment_totals(self):
            raise DBUnavailable("connection refused")

    janitor = Env(Down(), tmp_path).janitor()
    res = janitor.run_once()
    assert res["result"] == "error" and "connection refused" in res["error"]
    assert janitor.status()["errors_total"] == 1


# -- disk guard ---------------------------------------------------------------
@pytest.fixture
def service(tmp_path):
    root = tmp_path / "nvr"
    root.mkdir()
    cfg = deep_merge(DEFAULTS, {"auth": {"mode": "none"}, "retention": {"dry_run": False},
                                "recording": {"root": str(root), "state_dir": str(tmp_path / "state"),
                                              "ffmpeg": "false", "audio": "off"}})
    cams = {"cam": resolve_camera("cam", {"main_stream": "/x.mp4", "sub_stream": "/y.mp4"})}
    svc = Service(cfg, cams, db=None)
    svc.janitor = Janitor(FakeDB(), str(root), cfg["retention"], SEG, clock=lambda: NOW)
    return svc, Env(svc.janitor.db, root), cfg


def set_free(svc, gb):
    svc.guard._free_bytes = lambda _path: int(gb * 1e9)


def test_guard_trip_triggers_an_urgent_pass(service):
    svc, _, _ = service
    set_free(svc, 300)
    svc._guard_tick()
    assert not svc.janitor._wake.is_set()
    set_free(svc, 150)
    svc._guard_tick()
    assert svc.janitor._wake.is_set()
    svc.janitor._wake.clear()
    svc._guard_tick()  # still tripped, no backlog: the regular interval is enough
    assert not svc.janitor._wake.is_set()
    set_free(svc, 90)
    svc._guard_tick()  # worse
    assert svc.janitor._wake.is_set()


def test_guard_trip_seen_first_by_a_recorder_still_triggers(service):
    svc, _, _ = service
    set_free(svc, 150)
    assert svc.guard.check() == LEVEL_MAIN_STOPPED  # a recorder thread, before starting ffmpeg
    svc._guard_tick()
    assert svc.janitor._wake.is_set()


def test_tripped_guard_with_a_backlog_keeps_triggering(service):
    svc, env, _ = service
    set_free(svc, 150)
    svc.janitor.batch_size = 1
    env.add(OLD)
    env.add(OLD + 60)
    svc._guard_tick()
    svc.janitor._wake.clear()
    svc.janitor.run_once(urgent=True)
    svc._guard_tick()
    assert svc.janitor._wake.is_set()


def test_urgent_pass_keeps_every_rule(service):
    svc, env, _ = service
    prot = env.add(OLD, protected=True)
    bad = env.add(OLD + 60, path="../outside.mp4")
    recent = env.add(1000)
    res = svc.janitor.run_once(urgent=True)
    assert res["urgent"] is True and res["segments"] == 0 and res["skipped"] == 1
    assert env.store.live_ids() == {prot, bad, recent}
    svc.janitor.dry_run = True
    aged = env.add(OLD + 120)
    assert svc.janitor.run_once(urgent=True)["result"] == "dry_run"
    assert aged in env.store.live_ids() and env.exists(OLD + 120)


# -- status and metrics -------------------------------------------------------
def test_status_and_metrics(service):
    svc, env, cfg = service
    env.add(OLD, size=1000)
    env.add(1000, size=50)
    env.add(2000, q="sub", size=70)
    svc.janitor.run_once()
    client = TestClient(create_app(svc, cfg))
    r = client.get("/api/status").json()["retention"]
    assert r["settings"]["dry_run"] is False
    assert (r["settings"]["main_days"], r["settings"]["sub_days"], r["settings"]["max_total_gb"]) == (3, 14, 1500)
    assert r["total_bytes"] == 120  # after the pass
    assert r["streams"] == [
        {"camera_id": "cam", "quality": "main", "segments": 1, "bytes": 50, "protected_bytes": 0,
         "oldest_start_ts": NOW - 1000},
        {"camera_id": "cam", "quality": "sub", "segments": 1, "bytes": 70, "protected_bytes": 0,
         "oldest_start_ts": NOW - 2000}]
    assert r["last_run"]["ts"] == NOW and r["last_run"]["result"] == "ok" and r["last_run"]["duration_s"] >= 0
    assert r["deleted"] == {"total_segments": 1, "total_bytes": 1000, "last_segments": 1, "last_bytes": 1000}
    assert r["would_delete"] is None

    text = client.get("/api/metrics").text
    assert "cams_record_retention_dry_run 0" in text
    assert "cams_record_retention_total_bytes 120" in text
    assert "cams_record_retention_max_bytes 1500000000000" in text
    assert 'cams_record_retention_stream_bytes{camera="cam",quality="sub"} 70' in text
    assert f'cams_record_retention_oldest_segment_timestamp_seconds{{camera="cam",quality="main"}} {NOW - 1000}' in text
    assert "cams_record_retention_deleted_segments_total 1" in text
    assert "cams_record_retention_deleted_bytes_total 1000" in text
    assert f"cams_record_retention_last_run_timestamp_seconds {NOW}" in text

    svc.janitor.run_once()  # nothing left to do
    r = client.get("/api/status").json()["retention"]
    assert r["deleted"] == {"total_segments": 1, "total_bytes": 1000, "last_segments": 0, "last_bytes": 0}


def test_status_without_a_database(tmp_path):
    cfg = deep_merge(DEFAULTS, {"auth": {"mode": "none"}, "recording": {"root": str(tmp_path)}})
    svc = Service(cfg, {}, db=None)
    r = TestClient(create_app(svc, cfg)).get("/api/status").json()["retention"]
    assert r["settings"]["dry_run"] is True and r["total_bytes"] == 0 and r["last_run"] is None


def test_service_only_starts_the_janitor_with_a_database(tmp_path):
    cfg = deep_merge(DEFAULTS, {"recording": {"root": str(tmp_path), "state_dir": str(tmp_path / "s"),
                                              "min_free_gb": 0, "resume_free_gb": 0}})
    svc = Service(cfg, {}, db=None)
    svc.start()
    assert svc.janitor._thread is None
    svc.stop()
