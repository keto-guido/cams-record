"""MariaDB tests: schema, segment insert/upsert, reconcile. Set CAMS_TEST_DB_* (CI uses a mariadb:10.11 service)."""

import calendar
import os
import shutil

import pytest

from cams_record.indexer import Indexer, SegmentJob, build_row
from cams_record.reconcile import reconcile
from cams_record.segments import rel_path_for, to_dt
from tests.conftest import make_clip, needs_ffmpeg

T = calendar.timegm((2026, 11, 4, 21, 32, 0, 0, 0, 0))


def row(start=T, dur=60.0, cam="front-door", q="main", **kw):
    r = {"camera_id": cam, "quality": q, "start_ts": start, "end_ts": start + dur, "duration_ms": int(dur * 1000),
         "path": rel_path_for(cam, q, start), "size_bytes": 1234, "codec": "h264", "width": 1920, "height": 1080,
         "fps": 15.0}
    r.update(kw)
    return r


def test_schema_version(db):
    from cams_record.db import MIGRATIONS

    assert db.schema_version == MIGRATIONS[-1][0]
    v = db.run(lambda cur: (cur.execute("SELECT MAX(version) FROM schema_version"), cur.fetchone()[0])[1])
    assert v == MIGRATIONS[-1][0]
    db.ready = False  # re-running migrations is a no-op
    assert db.ensure_schema() == v


def test_insert_and_read_utc_ms(db):
    db.insert_segment(row(start=T + 0.25, dur=59.5))
    (s,) = db.list_segments("front-door", "main")
    assert s["start_ts"] == pytest.approx(T + 0.25)
    assert s["end_ts"] == pytest.approx(T + 59.75)
    assert s["duration_ms"] == 59500 and s["fps"] == 15.0 and not s["missing"]
    raw = db.run(lambda cur: (cur.execute("SELECT start_ts, has_motion, keep_until FROM segments"), cur.fetchone())[1])
    assert raw[0].strftime("%Y-%m-%d %H:%M:%S.%f") == "2026-11-04 21:32:00.250000"  # stored as UTC
    assert raw[1] == 0 and raw[2] is None


def test_unique_camera_quality_start_upserts(db):
    db.insert_segment(row())
    db.insert_segment(row(size_bytes=999, dur=61))
    db.insert_segment(row(q="sub"))
    main = db.list_segments("front-door", "main")
    assert len(main) == 1 and main[0]["size_bytes"] == 999 and main[0]["duration_ms"] == 61000
    assert len(db.list_segments("front-door", "sub")) == 1


def test_upsert_cameras_and_status(db):
    from cams_record.config import resolve_camera

    cams = [resolve_camera("front-door", {"main_stream": "front-door", "sub_stream": "front-door-sub"}),
            resolve_camera("garage", {"enabled": False})]
    db.upsert_cameras(cams)
    db.upsert_cameras(cams[:1])
    rows = db.run(lambda cur: (cur.execute("SELECT id, sub_stream, enabled, record_sub FROM recorder_cameras "
                                           "ORDER BY id"), cur.fetchall())[1])
    assert rows == (("front-door", "front-door-sub", 1, 1), ("garage", None, 0, 0))
    db.upsert_status([{"camera_id": "front-door", "quality": "main", "state": "recording", "last_segment_ts": T,
                       "restarts_24h": 2, "last_error": "x"}])
    db.upsert_status([{"camera_id": "front-door", "quality": "main", "state": "backoff", "last_segment_ts": None,
                       "restarts_24h": 3, "last_error": "boom"}])
    st = db.run(lambda cur: (cur.execute("SELECT state, last_segment_ts, restarts_24h, last_error "
                                         "FROM recorder_status"), cur.fetchall())[1])
    assert len(st) == 1 and st[0][0] == "backoff" and st[0][2] == 3 and st[0][3] == "boom"
    assert st[0][1] is not None  # NULL keeps the previous last_segment_ts


def test_paths_with_prefix_escapes_like(db):
    db.insert_segment(row(cam="a_b"))
    db.insert_segment(row(cam="axb"))
    assert db.paths_with_prefix("a_b/main/") == {rel_path_for("a_b", "main", T)}


def test_migration_2_upgrades_a_v1_table(db):
    from cams_record.db import MIGRATIONS

    def to_v1(cur):
        cur.execute("ALTER TABLE segments DROP INDEX idx_segments_retention, DROP COLUMN protected, "
                    "DROP COLUMN deleted_at")
        cur.execute("DELETE FROM schema_version WHERE version > 1")
        cur.execute("""INSERT INTO segments (camera_id, quality, start_ts, end_ts, duration_ms, path, size_bytes)
                       VALUES ('c', 'main', '2026-11-04 21:32:00', '2026-11-04 21:33:00', 60000, 'x.mp4', 5)""")

    db.run(to_v1)
    db.ready = False
    assert db.ensure_schema() == MIGRATIONS[-1][0]
    rows = db.run(lambda cur: (cur.execute("SELECT protected, deleted_at FROM segments"), cur.fetchall())[1])
    assert rows == ((0, None),)
    keys = db.run(lambda cur: (cur.execute("SHOW INDEX FROM segments WHERE Key_name = 'idx_segments_retention'"),
                               [r[4] for r in cur.fetchall()])[1])
    assert keys == ["deleted_at", "quality", "start_ts"]
    for stmt in MIGRATIONS[-1][1]:  # safe to retry after a timeout
        db.run(lambda cur, s=stmt: cur.execute(s))


def test_deleted_rows_are_hidden_and_reindexing_revives_them(db):
    db.insert_segment(row())
    db.insert_segment(row(start=T + 60))
    gone, kept = sorted(s["id"] for s in db.list_segments("front-door", "main"))
    assert db.mark_deleted([gone]) == 1
    assert db.mark_deleted([gone]) == 0
    marked, now = db.run(lambda cur: (cur.execute("SELECT deleted_at, UTC_TIMESTAMP() FROM segments WHERE id = %s",
                                                  (gone,)), cur.fetchone())[1])
    assert abs((now - marked).total_seconds()) < 5  # NOW() is UTC in this session
    assert [s["id"] for s in db.list_segments("front-door", "main")] == [kept]
    assert db.paths_with_prefix("front-door/main/") == {rel_path_for("front-door", "main", T + 60)}
    assert [r[0] for r in db.present_rows(0)] == [kept]
    assert [t["segments"] for t in db.segment_totals()] == [1]
    assert [s["id"] for s in db.oldest_segments("main", T + 3600, 10)] == [kept]
    db.insert_segment(row())  # reconcile found the file on disk after all
    assert len(db.list_segments("front-door", "main")) == 2


def test_segment_totals_cover_files_on_disk(db):
    db.insert_segment(row(size_bytes=100))
    db.insert_segment(row(start=T + 60, size_bytes=10))
    db.insert_segment(row(start=T + 120, size_bytes=1))
    db.insert_segment(row(start=T - 60, cam="gone", size_bytes=7))
    db.run(lambda cur: cur.execute("UPDATE segments SET missing = 1 WHERE start_ts < %s", (to_dt(T + 1),)))
    db.run(lambda cur: cur.execute("UPDATE segments SET protected = 1 WHERE size_bytes = 1"))
    assert db.segment_totals() == [
        {"camera_id": "front-door", "quality": "main", "segments": 2, "bytes": 11, "protected_bytes": 1,
         "oldest_start_ts": T + 60},
        {"camera_id": "gone", "quality": "main", "segments": 0, "bytes": 0, "protected_bytes": 0,
         "oldest_start_ts": None}]  # listed all the same, so the age rule can retire the row


def test_build_row_prefers_segment_list_times():
    job = SegmentJob("c", "main", "/x", "c/main/x.mp4", name_ts=T, start_ts=T + 0.4, end_ts=T + 60.4)
    r = build_row(job, 60.0, 10, "h264", 640, 360, 15.0)
    assert (r["start_ts"], r["end_ts"], r["duration_ms"]) == (T + 0.4, T + 60.4, 60000)
    job = SegmentJob("c", "main", "/x", "c/main/x.mp4", name_ts=T)  # reconcile: name + probed duration
    r = build_row(job, 59.9, 10, "h264", 640, 360, 15.0)
    assert (r["start_ts"], r["end_ts"], r["duration_ms"]) == (T, T + 59.9, 59900)
    job = SegmentJob("c", "main", "/x", "c/main/x.mp4", name_ts=T, start_ts=T, end_ts=T + 60)
    assert build_row(job, 12.0, 10, None, None, None, None)["end_ts"] == T + 12  # list wrong -> file wins


@needs_ffmpeg
def test_reconcile_inserts_unknown_files_and_marks_missing(db, tmp_path):
    root = tmp_path / "nvr"
    clip = make_clip(tmp_path / "src.mp4", 3)
    on_disk = []
    for i in range(3):
        rel = rel_path_for("front-door", "main", T + 60 * i)
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(clip, root / rel)
        on_disk.append(rel)
    # a known row whose file exists, and one whose file was deleted
    db.insert_segment(row(start=T, dur=3.0))
    db.insert_segment(row(start=T - 3600, dur=60.0))
    # junk the reconcile must ignore
    (root / "front-door/main/2026/11/04/21/notes.txt").write_text("x")
    (root / ".trash").mkdir()

    idx = Indexer(db)
    idx.start()
    res = reconcile(str(root), db, idx)
    assert idx.wait_idle(30)
    idx.stop()
    assert res["queued"] == 2 and res["marked_missing"] == 1
    segs = {s["path"]: s for s in db.list_segments("front-door", "main")}
    assert set(on_disk) <= set(segs)
    for rel in on_disk[1:]:
        s = segs[rel]
        assert s["codec"] == "h264" and s["width"] == 160 and s["height"] == 120 and s["fps"] == 10.0
        assert 2.9 <= s["duration_ms"] / 1000 <= 3.1 and s["size_bytes"] == os.path.getsize(root / rel)
    assert segs[rel_path_for("front-door", "main", T - 3600)]["missing"] is True

    # every file on disk has a row; second run is a no-op; a restored file is un-marked
    res2 = reconcile(str(root), db, Indexer(db))
    assert res2["queued"] == 0 and res2["marked_missing"] == 0
    gone = root / rel_path_for("front-door", "main", T - 3600)
    gone.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(clip, gone)
    assert reconcile(str(root), db, Indexer(db))["unmarked"] == 1


@needs_ffmpeg
def test_reconcile_skips_files_newer_than_cutoff_and_renames_corrupt(db, tmp_path):
    root = tmp_path / "nvr"
    live = root / rel_path_for("cam", "sub", T)
    live.parent.mkdir(parents=True)
    live.write_bytes(b"\0" * 100)  # being written by a live recorder
    broken = root / rel_path_for("cam", "sub", T + 60)
    broken.write_bytes(b"\0" * 100)
    os.utime(broken, (T, T))
    idx = Indexer(db)
    res = reconcile(str(root), db, idx, cutoff=T + 1)
    assert res["queued"] == 1  # only the old (broken) file
    idx.start()
    assert idx.wait_idle(30)
    idx.stop()
    assert idx.probe_errors == 1
    assert not broken.exists() and broken.with_name(broken.name + ".bad").exists()
    assert live.exists()
    assert db.list_segments("cam", "sub") == []


def test_detection_protection_expiry(db):
    import time

    now = time.time()
    db.insert_segment(row(start=now - 1000, dur=60))
    db.protect_window("front-door", now - 1001, now - 900, now + 86400)
    assert db.oldest_segments("main", now, 10) == []
    db.protect_window("front-door", now - 1001, now - 900, now + 100)
    until = db.run(lambda c: (c.execute("SELECT protected_until FROM segments"), c.fetchone()[0])[1])
    assert until > to_dt(now + 80000)
    db.run(lambda c: c.execute("UPDATE segments SET protected_until=UTC_TIMESTAMP() - INTERVAL 1 SECOND"))
    assert len(db.oldest_segments("main", now, 10)) == 1
    db.run(lambda c: c.execute("UPDATE segments SET protected=1"))
    assert db.oldest_segments("main", now, 10) == []


def test_clip_query_overlaps_and_deleted_rows(db):
    db.insert_segment(row(start=T, dur=60))
    db.insert_segment(row(start=T + 60, dur=60))
    db.insert_segment(row(start=T + 120, dur=60))
    db.insert_segment(row(start=T, q="sub", dur=300))
    rows, latest = db.clip_segments("front-door", "main", T + 60, T + 120)
    assert latest == T + 180
    assert len(rows) == 1 and rows[0]["start_ts"] == T + 60
    ids = db.list_segments("front-door", "main")
    db.mark_deleted([ids[1]["id"]])
    rows, _ = db.clip_segments("front-door", "main", T + 60, T + 120)
    assert rows[0]["deleted_at"] is not None


def test_protection_updates_both_qualities_only_overlaps(db):
    for quality in ("main", "sub"):
        db.insert_segment(row(start=T, q=quality))
        db.insert_segment(row(start=T + 60, q=quality))
    assert db.protect_window("front-door", T + 1, T + 60, T + 86400) == 2
    rows = db.run(lambda c: (c.execute("SELECT start_ts, protected_until FROM segments"), c.fetchall())[1])
    assert all((until is not None) == (start == to_dt(T)) for start, until in rows)


def test_migration_3_upgrades_v2(db):
    db.insert_segment(row())
    db.run(lambda c: c.execute("UPDATE segments SET protected=1"))
    db.run(lambda c: c.execute("ALTER TABLE segments DROP COLUMN protected_until"))
    db.run(lambda c: c.execute("DELETE FROM schema_version WHERE version=3"))
    db.ready = False
    assert db.ensure_schema() == 3
    values = db.run(lambda c: (c.execute("SELECT protected, protected_until FROM segments"), c.fetchone())[1])
    assert values == (1, None)
