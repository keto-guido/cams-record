"""MariaDB (10.6+) segment index via PyMySQL.

* One connection guarded by a lock, reconnected on demand (the indexer thread is the only
  heavy writer; HTTP reads are rare).
* Schema created/migrated on first connect, tracked in ``schema_version`` (like cams-detect).
* All DATETIME(3) columns are UTC (session ``time_zone = '+00:00'``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable

from .config import CameraConfig, read_secret
from .segments import from_dt, to_dt

log = logging.getLogger(__name__)


class DBUnavailable(Exception):
    pass


MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        """CREATE TABLE IF NOT EXISTS recorder_cameras (
            id VARCHAR(64) NOT NULL PRIMARY KEY,
            main_stream VARCHAR(255) NULL,
            sub_stream VARCHAR(255) NULL,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            record_main TINYINT(1) NOT NULL DEFAULT 1,
            record_sub TINYINT(1) NOT NULL DEFAULT 1,
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS segments (
            id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            camera_id VARCHAR(64) NOT NULL,
            quality ENUM('main','sub') NOT NULL,
            start_ts DATETIME(3) NOT NULL,
            end_ts DATETIME(3) NOT NULL,
            duration_ms INT NOT NULL,
            path VARCHAR(512) NOT NULL,
            size_bytes BIGINT NOT NULL,
            codec VARCHAR(32) NULL,
            width INT NULL,
            height INT NULL,
            fps DECIMAL(5,2) NULL,
            has_motion TINYINT NOT NULL DEFAULT 0,
            keep_until DATETIME NULL,
            missing TINYINT NOT NULL DEFAULT 0,
            created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            UNIQUE KEY uq_segments_cam_q_start (camera_id, quality, start_ts),
            KEY idx_segments_cam_q_range (camera_id, quality, start_ts, end_ts),
            KEY idx_segments_end (end_ts),
            KEY idx_segments_path (path)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
        """CREATE TABLE IF NOT EXISTS recorder_status (
            camera_id VARCHAR(64) NOT NULL,
            quality ENUM('main','sub') NOT NULL,
            state VARCHAR(16) NOT NULL,
            last_segment_ts DATETIME(3) NULL,
            restarts_24h INT NOT NULL DEFAULT 0,
            last_error VARCHAR(512) NULL,
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            PRIMARY KEY (camera_id, quality)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    ]),
    (2, [
        # IF NOT EXISTS: a retry after a read timeout must not fail on an ALTER that did finish.
        """ALTER TABLE segments
            ADD COLUMN IF NOT EXISTS protected BOOLEAN NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS deleted_at DATETIME NULL,
            ADD KEY IF NOT EXISTS idx_segments_retention (deleted_at, quality, start_ts)""",
    ]),
    (3, ["ALTER TABLE segments ADD COLUMN IF NOT EXISTS protected_until DATETIME NULL"]),
    (4, ["ALTER TABLE segments ADD KEY IF NOT EXISTS idx_segments_path_id (path, id)"]),
    (5, [
        """CREATE TABLE IF NOT EXISTS push_subscriptions (
            id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_sub VARCHAR(128) NOT NULL,
            endpoint VARCHAR(2048) NOT NULL,
            endpoint_hash CHAR(64) NOT NULL,
            p256dh VARCHAR(255) NOT NULL,
            auth VARCHAR(255) NOT NULL,
            user_agent VARCHAR(512) NULL,
            created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            last_success_at DATETIME(3) NULL,
            UNIQUE KEY uq_push_endpoint_hash (endpoint_hash),
            KEY idx_push_user (user_sub)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS push_sent_events (
            event_id BIGINT NOT NULL PRIMARY KEY,
            camera_id VARCHAR(64) NOT NULL,
            sent_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            KEY idx_push_sent_at (sent_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    ]),
    # v5 created event_id as BIGINT; production cams_detect.events.id values are strings
    # (e.g. "1793897909-7de0b7"). Tables may already exist empty on rolled-back installs.
    (6, [
        """ALTER TABLE push_sent_events
            MODIFY COLUMN event_id VARCHAR(64) NOT NULL""",
    ]),
    # Roles from the subscriber's last verified JWT, so detection pushes honour per-camera access.
    # NULL (rows from before v7) means "no roles known": only open cameras and explicit user grants.
    (7, [
        """ALTER TABLE push_subscriptions
            ADD COLUMN IF NOT EXISTS roles_json VARCHAR(2048) NULL,
            ADD COLUMN IF NOT EXISTS roles_at DATETIME(3) NULL""",
    ]),
]

_PUSH_COLUMNS = ("user_sub", "endpoint", "endpoint_hash", "p256dh", "auth", "user_agent", "roles_json", "roles_at")
_PUSH_SELECT = ", ".join(_PUSH_COLUMNS)


def _push_row(r) -> dict:
    row = dict(zip(_PUSH_COLUMNS, r, strict=True))
    row["roles_at"] = from_dt(row["roles_at"])
    return row


TABLES = ("segments", "recorder_status", "recorder_cameras", "push_subscriptions",
          "push_sent_events", "schema_version")


class Database:
    def __init__(self, cfg: dict, migrations: list[tuple[int, list[str]]] | None = None):
        import pymysql

        self.pymysql = pymysql
        self.cfg = cfg
        self.migrations = MIGRATIONS if migrations is None else migrations
        self.password = cfg.get("password")
        if self.password is None:
            self.password = read_secret(cfg.get("password_file")) or ""
        self._conn = None
        self._lock = threading.RLock()
        self.retention_lock = threading.RLock()
        self.ready = False
        self.connected = False
        self.last_error = ""
        self.schema_version = 0

    # -- connection ------------------------------------------------------
    def _connect(self):
        c = self.cfg
        return self.pymysql.connect(
            host=c.get("host", "127.0.0.1"), port=int(c.get("port", 3306)), user=c.get("user", "cams_record"),
            password=self.password, database=c.get("database", "cams_record"), charset="utf8mb4",
            autocommit=True, connect_timeout=float(c.get("connect_timeout", 5)),
            read_timeout=float(c.get("read_timeout", 30)), write_timeout=float(c.get("write_timeout", 30)),
            init_command="SET time_zone = '+00:00'")

    def run(self, fn):
        """Run fn(cursor) with the schema in place. Raises DBUnavailable on connection errors."""
        err = self.pymysql.err
        with self._lock:
            try:
                if self._conn is None:
                    self._conn = self._connect()
                else:
                    self._conn.ping(reconnect=True)
                if not self.ready:
                    self._migrate()
                with self._conn.cursor() as cur:
                    result = fn(cur)
                self.connected = True
                return result
            except (err.OperationalError, err.InterfaceError, OSError) as exc:
                self._drop(exc)
                raise DBUnavailable(str(exc)) from exc

    def _drop(self, exc) -> None:
        self.connected = False
        self.ready = False
        self.last_error = str(exc)[:200]
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    def _migrate(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS schema_version (version INT NOT NULL PRIMARY KEY, "
                        "applied_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)) ENGINE=InnoDB")
            cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
            current = int(cur.fetchone()[0])
            for version, stmts in self.migrations:
                if version <= current:
                    continue
                for stmt in stmts:
                    cur.execute(stmt)
                cur.execute("INSERT IGNORE INTO schema_version (version) VALUES (%s)", (version,))
                log.info("mariadb: applied schema migration v%d", version)
                current = version
        self.schema_version = current
        self.ready = True
        log.info("mariadb: connected to %s:%s/%s (schema v%d)", self.cfg.get("host", "127.0.0.1"),
                 self.cfg.get("port", 3306), self.cfg.get("database", "cams_record"), current)

    def ensure_schema(self) -> int:
        self.run(lambda cur: None)
        return self.schema_version

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self._conn = None
            self.ready = False

    # -- cameras -----------------------------------------------------------
    def upsert_cameras(self, cameras: Iterable[CameraConfig]) -> None:
        rows = [(c.id, c.main_stream, c.sub_stream, int(c.enabled), int(c.record_main), int(c.record_sub))
                for c in cameras]
        if not rows:
            return
        self.run(lambda cur: cur.executemany(
            """INSERT INTO recorder_cameras (id, main_stream, sub_stream, enabled, record_main, record_sub)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE main_stream=VALUES(main_stream), sub_stream=VALUES(sub_stream),
                 enabled=VALUES(enabled), record_main=VALUES(record_main), record_sub=VALUES(record_sub)""",
            rows))

    # -- segments ----------------------------------------------------------
    def insert_segment(self, row: dict) -> None:
        """Insert (or refresh) one segment. ``row`` times are UTC epoch seconds."""
        params = (row["camera_id"], row["quality"], to_dt(row["start_ts"]), to_dt(row["end_ts"]),
                  int(row["duration_ms"]), row["path"], int(row["size_bytes"]), row.get("codec"),
                  row.get("width"), row.get("height"), row.get("fps"))

        def q(cur):
            # CSV clock mapping and reconcile/sweep filename times can differ.
            # run() holds the single writer's lock across lookup and write.
            cur.execute("SELECT id FROM segments WHERE path=%s ORDER BY id LIMIT 1", (row["path"],))
            existing = cur.fetchone()
            if existing:
                # Preserve the first mapping and protection; revive only the canonical row.
                cur.execute("""UPDATE segments SET size_bytes=%s, codec=%s, width=%s, height=%s,
                               fps=%s, missing=0, deleted_at=NULL WHERE id=%s""",
                            (int(row["size_bytes"]), row.get("codec"), row.get("width"),
                             row.get("height"), row.get("fps"), existing[0]))
                return
            cur.execute(
                """INSERT INTO segments (camera_id, quality, start_ts, end_ts, duration_ms, path, size_bytes,
                                         codec, width, height, fps)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE end_ts=VALUES(end_ts), duration_ms=VALUES(duration_ms),
                     path=VALUES(path), size_bytes=VALUES(size_bytes), codec=VALUES(codec), width=VALUES(width),
                     height=VALUES(height), fps=VALUES(fps), missing=0, deleted_at=NULL""", params)

        with self.retention_lock:
            self.run(q)

    def paths_with_prefix(self, prefix: str) -> set[str]:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        def q(cur):
            cur.execute("SELECT path FROM segments WHERE path LIKE %s AND deleted_at IS NULL", (like,))
            return {r[0] for r in cur.fetchall()}

        return self.run(q)

    def present_rows(self, after_id: int, limit: int = 5000) -> list[tuple[int, str]]:
        def q(cur):
            cur.execute("""SELECT id, path FROM segments WHERE id > %s AND missing = 0 AND deleted_at IS NULL
                           ORDER BY id LIMIT %s""", (int(after_id), int(limit)))
            return list(cur.fetchall())

        return self.run(q)

    def mark_missing(self, ids: list[int]) -> int:
        n = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join(["%s"] * len(chunk))
            n += self.run(lambda cur, c=chunk, m=marks: cur.execute(
                f"UPDATE segments SET missing = 1 WHERE id IN ({m})", c))
        return n

    def list_segments(self, camera_id: str, quality: str, limit: int = 1000) -> list[dict]:
        def q(cur):
            cur.execute("""SELECT id, start_ts, end_ts, duration_ms, path, size_bytes, codec, width, height, fps,
                                  missing FROM segments
                           WHERE camera_id=%s AND quality=%s AND deleted_at IS NULL
                           ORDER BY start_ts LIMIT %s""", (camera_id, quality, int(limit)))
            return cur.fetchall()

        return [{"id": r[0], "start_ts": from_dt(r[1]), "end_ts": from_dt(r[2]), "duration_ms": r[3],
                 "path": r[4], "size_bytes": r[5], "codec": r[6], "width": r[7], "height": r[8],
                 "fps": float(r[9]) if r[9] is not None else None, "missing": bool(r[10])}
                for r in self.run(q)]

    def segment_rows(self, camera, quality, start, end):
        def q(cur):
            cur.execute("""SELECT id, camera_id, quality, start_ts, end_ts, path, codec,
                           size_bytes, missing, deleted_at FROM segments
                           WHERE camera_id=%s AND quality=%s AND start_ts<%s AND end_ts>%s
                           ORDER BY start_ts, id""", (camera, quality, to_dt(end), to_dt(start)))
            return [self._segment_row(r) for r in cur.fetchall()]
        return self.run(q)

    @staticmethod
    def _segment_row(r):
        return dict(zip(("id", "camera_id", "quality", "start_ts", "end_ts", "path", "codec",
                         "size", "missing", "deleted_at"),
                        (*r[:3], from_dt(r[3]), from_dt(r[4]), *r[5:]), strict=True))

    def segment_by_id(self, segment_id):
        def q(cur):
            cur.execute("""SELECT id, camera_id, quality, start_ts, end_ts, path, codec,
                           size_bytes, missing, deleted_at FROM segments WHERE id=%s""", (segment_id,))
            row = cur.fetchone()
            return self._segment_row(row) if row else None
        return self.run(q)

    def clip_segments(self, camera: str, quality: str, start: float, end: float) -> tuple[list[dict], float | None]:
        def q(cur):
            cur.execute("SELECT MAX(end_ts) FROM segments WHERE camera_id=%s AND quality=%s",
                        (camera, quality))
            latest = from_dt(cur.fetchone()[0])
            cur.execute("""SELECT start_ts, end_ts, path, codec, missing, deleted_at FROM segments
                           WHERE camera_id=%s AND quality=%s AND start_ts < %s AND end_ts > %s
                           ORDER BY start_ts""", (camera, quality, to_dt(end), to_dt(start)))
            rows = [{"start_ts": from_dt(r[0]), "end_ts": from_dt(r[1]), "path": r[2],
                     "codec": r[3], "missing": bool(r[4]), "deleted_at": r[5]} for r in cur.fetchall()]
            return rows, latest
        return self.run(q)

    def detection_events(self, since: float) -> list[dict]:
        def q(cur):
            cur.execute("""SELECT camera_id, start_ts, end_ts FROM cams_detect.events
                           WHERE updated_at >= %s OR start_ts >= %s""", (to_dt(since), to_dt(since)))
            return [{"camera_id": r[0], "start_ts": from_dt(r[1]), "end_ts": from_dt(r[2])}
                    for r in cur.fetchall()]
        return self.run(q)

    def detection_events_for_push(self, since: float, limit: int = 200,
                                  snapshot_wait_s: float = 0) -> list[dict]:
        """Recent cams_detect events that have not yet been claimed for Web Push.

        Production ``cams_detect.events`` has a BIGINT auto-increment ``id`` PK and the
        string event id (MQTT / API / push, e.g. ``1793897909-7de0b7``) in
        ``event_uid``; the score column is ``max_score`` (not ``score``). Push claims
        in ``push_sent_events.event_id`` are keyed by that uid.

        Releases before the uid switch claimed events by the numeric PK, so an event whose
        ``CAST(id AS CHAR)`` is already claimed is skipped too (no duplicate push on deploy).
        cams_detect.events is ``utf8mb4_general_ci`` and push_sent_events
        ``utf8mb4_unicode_ci``; both join keys are converted to the push table's collation
        explicitly, otherwise MariaDB raises "Illegal mix of collations" (1267).

        With ``snapshot_wait_s > 0`` an event whose ``snapshot_path`` is still NULL is held
        until it started more than that many seconds ago, so the notification never
        arrives before its snapshot can be served; snapshot-less events still notify.
        """
        snap_clause = ""
        params: list = [to_dt(since)]
        if snapshot_wait_s and float(snapshot_wait_s) > 0:
            snap_clause = "AND (e.snapshot_path IS NOT NULL OR e.start_ts <= %s)"
            params.append(to_dt(time.time() - float(snapshot_wait_s)))
        params.append(int(limit))

        def q(cur):
            cur.execute(f"""SELECT e.event_uid, e.camera_id, e.label, e.max_score, e.start_ts, e.end_ts
                           FROM cams_detect.events e
                           LEFT JOIN push_sent_events s
                             ON s.event_id = CONVERT(e.event_uid USING utf8mb4) COLLATE utf8mb4_unicode_ci
                           LEFT JOIN push_sent_events legacy
                             ON legacy.event_id = CONVERT(CAST(e.id AS CHAR) USING utf8mb4)
                                                  COLLATE utf8mb4_unicode_ci
                           WHERE s.event_id IS NULL AND legacy.event_id IS NULL AND e.start_ts >= %s
                             {snap_clause}
                           ORDER BY e.start_ts ASC, e.id ASC LIMIT %s""",
                        params)
            return [{"id": str(r[0]), "camera_id": r[1], "label": r[2],
                     "score": float(r[3]) if r[3] is not None else None,
                     "start_ts": from_dt(r[4]),
                     "end_ts": from_dt(r[5]) if r[5] is not None else None}
                    for r in cur.fetchall()]
        return self.run(q)

    def detection_event_by_id(self, event_id: str) -> dict | None:
        """Look up one cams_detect event by its string uid (``events.event_uid``).

        Legacy deep links: releases before the uid switch sent pushes linking to
        ``#event=<numeric PK>``, and those notifications are still on phones. When no
        uid matches and ``event_id`` is all digits, the row with that numeric ``id`` is
        returned instead, so the client opens the right event.

        The returned ``id`` is always the uid, never the numeric PK. ``has_snapshot`` /
        ``has_snapshot_hires`` are true only when the path columns are non-NULL.
        """
        cols = """SELECT event_uid, camera_id, label, max_score, start_ts, end_ts,
                         snapshot_path, snapshot_hires FROM cams_detect.events"""
        eid = str(event_id)

        def q(cur):
            cur.execute(f"{cols} WHERE event_uid=%s", (eid,))
            r = cur.fetchone()
            if not r and eid.isascii() and eid.isdigit():
                cur.execute(f"{cols} WHERE id=%s", (int(eid),))
                r = cur.fetchone()
            if not r:
                return None
            return {"id": str(r[0]), "camera_id": r[1], "label": r[2],
                    "score": float(r[3]) if r[3] is not None else None,
                    "start_ts": from_dt(r[4]),
                    "end_ts": from_dt(r[5]) if r[5] is not None else None,
                    "has_snapshot": r[6] is not None,
                    "has_snapshot_hires": r[7] is not None}
        return self.run(q)

    def upsert_push_subscription(self, user_sub: str, sub: dict, roles=None) -> None:
        roles_json = json.dumps(sorted(set(roles))) if roles is not None else None
        self.run(lambda cur: cur.execute(
            """INSERT INTO push_subscriptions
               (user_sub, endpoint, endpoint_hash, p256dh, auth, user_agent, roles_json, roles_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, IF(%s IS NULL, NULL, UTC_TIMESTAMP(3)))
               ON DUPLICATE KEY UPDATE user_sub=VALUES(user_sub), endpoint=VALUES(endpoint),
                 p256dh=VALUES(p256dh), auth=VALUES(auth),
                 user_agent=COALESCE(VALUES(user_agent), user_agent),
                 roles_json=VALUES(roles_json), roles_at=VALUES(roles_at)""",
            (user_sub, sub["endpoint"], sub["endpoint_hash"], sub["p256dh"], sub["auth"],
             sub.get("user_agent"), roles_json, roles_json)))

    def refresh_push_roles(self, user_sub: str, roles) -> int:
        """Store the roles from a freshly verified JWT on every subscription of this user."""
        return self.run(lambda cur: cur.execute(
            "UPDATE push_subscriptions SET roles_json=%s, roles_at=UTC_TIMESTAMP(3) WHERE user_sub=%s",
            (json.dumps(sorted(set(roles))), user_sub)))

    def delete_push_subscription(self, user_sub: str, endpoint_hash: str) -> int:
        return self.run(lambda cur: cur.execute(
            "DELETE FROM push_subscriptions WHERE user_sub=%s AND endpoint_hash=%s",
            (user_sub, endpoint_hash)))

    def delete_push_endpoint(self, endpoint_hash: str) -> int:
        return self.run(lambda cur: cur.execute(
            "DELETE FROM push_subscriptions WHERE endpoint_hash=%s", (endpoint_hash,)))

    def touch_push_subscription(self, endpoint_hash: str) -> None:
        self.run(lambda cur: cur.execute(
            "UPDATE push_subscriptions SET last_success_at=UTC_TIMESTAMP(3) WHERE endpoint_hash=%s",
            (endpoint_hash,)))

    def push_subscriptions_for_user(self, user_sub: str) -> list[dict]:
        def q(cur):
            cur.execute(f"""SELECT {_PUSH_SELECT} FROM push_subscriptions WHERE user_sub=%s""", (user_sub,))
            return [_push_row(r) for r in cur.fetchall()]
        return self.run(q)

    def list_push_subscriptions(self) -> list[dict]:
        def q(cur):
            cur.execute(f"""SELECT {_PUSH_SELECT} FROM push_subscriptions""")
            return [_push_row(r) for r in cur.fetchall()]
        return self.run(q)

    def claim_push_event(self, event_id: str, camera_id: str) -> bool:
        """Insert a send claim. Returns True when this caller won (should send)."""
        def q(cur):
            n = cur.execute(
                "INSERT IGNORE INTO push_sent_events (event_id, camera_id) VALUES (%s, %s)",
                (str(event_id), camera_id))
            return n == 1
        return self.run(q)

    def protect_window(self, camera: str, start: float, end: float, until: float) -> int:
        with self.retention_lock:
            return self.run(lambda cur: cur.execute("""UPDATE segments AS target
                JOIN segments AS overlap ON overlap.path=target.path
                SET target.protected_until=GREATEST(COALESCE(target.protected_until, %s), %s)
                WHERE overlap.camera_id=%s AND overlap.start_ts < %s AND overlap.end_ts > %s
                """,
                (to_dt(until), to_dt(until), camera, to_dt(end), to_dt(start))))

    # -- retention ---------------------------------------------------------
    def oldest_segments(self, quality: str, end_before: float, limit: int, camera_id: str | None = None,
                        start_before: float | None = None, present_only: bool = False) -> list[dict]:
        """Deletable segments, oldest first: never protected, never deleted, closed before ``end_before``."""
        where = ["s.deleted_at IS NULL",
                 "NOT EXISTS (SELECT 1 FROM segments p WHERE p.path=s.path "
                 "AND (p.protected=1 OR p.protected_until > UTC_TIMESTAMP()))",
                 "NOT EXISTS (SELECT 1 FROM segments earlier WHERE earlier.path=s.path "
                 "AND earlier.deleted_at IS NULL AND earlier.id < s.id)",
                 "quality = %s", "end_ts < %s"]
        params: list = [quality, to_dt(end_before)]
        if camera_id is not None:
            where.append("camera_id = %s")
            params.append(camera_id)
        if start_before is not None:
            where.append("start_ts < %s")
            params.append(to_dt(start_before))
        if present_only:
            where.append("missing = 0")

        def q(cur):
            cur.execute(f"""SELECT id, camera_id, quality, start_ts, path, size_bytes, missing FROM segments s
                            WHERE {" AND ".join(where)} ORDER BY start_ts, id LIMIT %s""", (*params, int(limit)))
            return cur.fetchall()

        return [{"id": r[0], "camera_id": r[1], "quality": r[2], "start_ts": from_dt(r[3]), "path": r[4],
                 "size_bytes": r[5], "missing": bool(r[6])} for r in self.run(q)]

    def segment_totals(self) -> list[dict]:
        """Per (camera, quality): non-deleted rows; segment and byte counts cover files on disk only."""
        def q(cur):
            cur.execute("""SELECT camera_id, quality, SUM(missing = 0), SUM(IF(missing = 0, size_bytes, 0)),
                                  SUM(IF(missing = 0 AND (protected = 1 OR protected_until > UTC_TIMESTAMP()),
                                         size_bytes, 0)),
                                  MIN(IF(missing = 0, start_ts, NULL))
                           FROM (
                               SELECT path, camera_id, quality, MIN(missing) AS missing,
                                      MAX(size_bytes) AS size_bytes, MIN(start_ts) AS start_ts,
                                      MAX(protected) AS protected, MAX(protected_until) AS protected_until
                               FROM segments WHERE deleted_at IS NULL
                               GROUP BY path, camera_id, quality
                           ) files
                           GROUP BY camera_id, quality ORDER BY camera_id, quality""")
            return cur.fetchall()

        return [{"camera_id": r[0], "quality": r[1], "segments": int(r[2]), "bytes": int(r[3]),
                 "protected_bytes": int(r[4]), "oldest_start_ts": from_dt(r[5])} for r in self.run(q)]

    def mark_deleted(self, ids: list[int]) -> int:
        """Mark every row sharing a file with these ids deleted."""
        ids = sorted(ids)
        if not ids:
            return 0
        marks = ",".join(["%s"] * len(ids))

        def q(cur):
            self._conn.begin()
            try:
                n = cur.execute(f"UPDATE segments target JOIN (SELECT path FROM segments WHERE id IN ({marks})) chosen "
                                "ON chosen.path=target.path SET target.deleted_at=NOW() "
                                "WHERE target.deleted_at IS NULL", ids)
                self._conn.commit()
                return n
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001 - the connection itself may be gone
                    pass
                raise

        return self.run(q)

    # -- status ------------------------------------------------------------
    def upsert_status(self, rows: list[dict]) -> None:
        params = [(r["camera_id"], r["quality"], r["state"], to_dt(r.get("last_segment_ts")),
                   int(r.get("restarts_24h") or 0), (r.get("last_error") or None) and r["last_error"][:512])
                  for r in rows]
        if not params:
            return
        self.run(lambda cur: cur.executemany(
            """INSERT INTO recorder_status (camera_id, quality, state, last_segment_ts, restarts_24h, last_error)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE state=VALUES(state),
                 last_segment_ts=COALESCE(VALUES(last_segment_ts), last_segment_ts),
                 restarts_24h=VALUES(restarts_24h), last_error=VALUES(last_error)""", params))

    def status(self) -> dict:
        return {"connected": self.connected, "ready": self.ready, "schema_version": self.schema_version,
                "last_error": "" if self.connected else self.last_error}
