"""MariaDB (10.6+) segment index via PyMySQL.

* One connection guarded by a lock, reconnected on demand (the indexer thread is the only
  heavy writer; HTTP reads are rare).
* Schema created/migrated on first connect, tracked in ``schema_version`` (like cams-detect).
* All DATETIME(3) columns are UTC (session ``time_zone = '+00:00'``).
"""

from __future__ import annotations

import logging
import threading
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
]

TABLES = ("segments", "recorder_status", "recorder_cameras", "schema_version")


class Database:
    def __init__(self, cfg: dict):
        import pymysql

        self.pymysql = pymysql
        self.cfg = cfg
        self.password = cfg.get("password")
        if self.password is None:
            self.password = read_secret(cfg.get("password_file")) or ""
        self._conn = None
        self._lock = threading.RLock()
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
            for version, stmts in MIGRATIONS:
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
        self.run(lambda cur: cur.execute(
            """INSERT INTO segments (camera_id, quality, start_ts, end_ts, duration_ms, path, size_bytes,
                                     codec, width, height, fps)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE end_ts=VALUES(end_ts), duration_ms=VALUES(duration_ms),
                 path=VALUES(path), size_bytes=VALUES(size_bytes), codec=VALUES(codec), width=VALUES(width),
                 height=VALUES(height), fps=VALUES(fps), missing=0""", params))

    def paths_with_prefix(self, prefix: str) -> set[str]:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        def q(cur):
            cur.execute("SELECT path FROM segments WHERE path LIKE %s", (like,))
            return {r[0] for r in cur.fetchall()}

        return self.run(q)

    def present_rows(self, after_id: int, limit: int = 5000) -> list[tuple[int, str]]:
        def q(cur):
            cur.execute("SELECT id, path FROM segments WHERE id > %s AND missing = 0 ORDER BY id LIMIT %s",
                        (int(after_id), int(limit)))
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
                                  missing FROM segments WHERE camera_id=%s AND quality=%s
                           ORDER BY start_ts LIMIT %s""", (camera_id, quality, int(limit)))
            return cur.fetchall()

        return [{"id": r[0], "start_ts": from_dt(r[1]), "end_ts": from_dt(r[2]), "duration_ms": r[3],
                 "path": r[4], "size_bytes": r[5], "codec": r[6], "width": r[7], "height": r[8],
                 "fps": float(r[9]) if r[9] is not None else None, "missing": bool(r[10])}
                for r in self.run(q)]

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
