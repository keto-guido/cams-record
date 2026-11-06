import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = {
    "host": os.environ.get("CAMS_TEST_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("CAMS_TEST_DB_PORT", "3306")),
    "user": os.environ.get("CAMS_TEST_DB_USER", "cams_record"),
    "password": os.environ.get("CAMS_TEST_DB_PASSWORD", "cams_record"),
    "database": os.environ.get("CAMS_TEST_DB_NAME", "cams_record_test"),
    "connect_timeout": 2,
}


def mariadb_unavailable() -> str | None:
    try:
        import pymysql
    except ImportError:
        return "PyMySQL not installed"
    try:
        pymysql.connect(**DB).close()
    except Exception as exc:  # noqa: BLE001
        return f"MariaDB not reachable at {DB['host']}:{DB['port']} ({exc.__class__.__name__})"
    return None


def reset_mariadb() -> None:
    import pymysql

    from cams_record.db import TABLES

    conn = pymysql.connect(**DB, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SET SESSION lock_wait_timeout = 10")
        for t in TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
    conn.close()


@pytest.fixture
def db():
    reason = mariadb_unavailable()
    if reason:
        pytest.skip(reason)
    reset_mariadb()
    from cams_record.db import Database

    d = Database(DB)
    d.ensure_schema()
    yield d
    d.close()


CFG_DB = {
    "host": os.environ.get("CAMS_TEST_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("CAMS_TEST_DB_PORT", "3306")),
    "user": os.environ.get("CAMS_TEST_DB_USER", "cams_record"),
    "password": os.environ.get("CAMS_TEST_DB_PASSWORD", "cams_record"),
    "database": os.environ.get("CAMS_TEST_CONFIG_DB_NAME", "cams_config_test"),
    "connect_timeout": 2,
}


def _config_db_unavailable() -> str | None:
    try:
        import pymysql

        pymysql.connect(**CFG_DB).close()
    except Exception as exc:  # noqa: BLE001
        return f"MariaDB {CFG_DB['database']} not reachable ({exc.__class__.__name__})"
    return None


@pytest.fixture
def store():
    reason = _config_db_unavailable()
    if reason:
        pytest.skip(reason)
    import pymysql

    from cams_record.camconfig.schema import TABLES
    from cams_record.camconfig.store import CamConfigStore, open_db

    conn = pymysql.connect(**CFG_DB, autocommit=True)
    with conn.cursor() as cur:
        for t in TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
    conn.close()
    db = open_db(CFG_DB)
    db.ensure_schema()
    yield CamConfigStore(db)
    db.close()


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not installed")


def make_clip(path: Path, seconds: float, start_offset: str | None = None, audio: bool = True) -> Path:
    """Tiny H.264 (+AAC) test clip with a keyframe every second."""
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "32k"]
    cmd += ["-movflags", "+faststart", str(path)]
    subprocess.run(cmd, check=True)
    return path
