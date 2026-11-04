"""Configuration loading: config/config.yaml (global, optional) + config/cameras.yaml (per camera).

Global config is a plain nested dict deep-merged over DEFAULTS, with a handful of environment
variable overrides (same scheme as cams-detect). Cameras are resolved into CameraConfig objects.

cameras.yaml::

    cameras:
      front-door:                 # id = cams_detect.cameras.camera_id
        main_stream: front-door-main   # go2rtc stream names
        sub_stream: front-door
        enabled: true
        record_main: true
        record_sub: true
"""

from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
QUALITIES = ("main", "sub")

DEFAULTS: dict[str, Any] = {
    "log_level": "INFO",
    "base_path": "/cams-record",
    "http": {"host": "127.0.0.1", "port": 8097},
    "go2rtc": {"rtsp_url": "rtsp://127.0.0.1:8554/{stream}"},
    "recording": {
        "root": "/volume1/nvr",
        "segment_time": 60,  # seconds; cuts land on the first keyframe after each clock boundary
        "state_dir": "/tmp/cams-record",  # ffmpeg segment lists (CSV), not recordings
        "stall_factor": 3,  # restart when no segment closed for stall_factor * segment_time
        "backoff_initial": 1,
        "backoff_max": 60,
        "rtsp_transport": "tcp",
        "audio": "auto",  # auto (copy only if the source audio is AAC) | off
        "probe_timeout": 15,
        "stop_timeout": 10,
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "ffprobe_nice": 10,
        "min_free_gb": 200,  # below: stop main; below half of it: stop sub as well; 0 disables
        "resume_free_gb": 250,  # resume once free space is back above this
    },
    "auth": {
        "mode": "jwt",  # jwt | none
        "secret_file": "/run/secrets/apphub_jwt",
        "header": "x-apphub-token",
        "roles": ["admin"],  # every /api/* route needs one of these roles
        "leeway_seconds": 30,
    },
    "db": {
        "host": "127.0.0.1",
        "port": 3306,
        "database": "cams_record",
        "user": "cams_record",
        "password_file": "/run/secrets/db_password",
        "connect_timeout": 5,
    },
}

# env var -> (path in config, type)
ENV_OVERRIDES: dict[str, tuple[tuple[str, ...], type]] = {
    "LOG_LEVEL": (("log_level",), str),
    "BASE_PATH": (("base_path",), str),
    "HTTP_HOST": (("http", "host"), str),
    "HTTP_PORT": (("http", "port"), int),
    "AUTH_MODE": (("auth", "mode"), str),
    "AUTH_SECRET_FILE": (("auth", "secret_file"), str),
    "GO2RTC_RTSP_URL": (("go2rtc", "rtsp_url"), str),
    "RECORD_ROOT": (("recording", "root"), str),
    "SEGMENT_TIME": (("recording", "segment_time"), int),
    "STATE_DIR": (("recording", "state_dir"), str),
    "DB_HOST": (("db", "host"), str),
    "DB_PORT": (("db", "port"), int),
    "DB_NAME": (("db", "database"), str),
    "DB_USER": (("db", "user"), str),
    "DB_PASSWORD_FILE": (("db", "password_file"), str),
}


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _parse_env(value: str, typ: type):
    if typ is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if typ is list:
        return [v.strip() for v in value.split(",") if v.strip()]
    return typ(value)


def apply_env(cfg: dict, env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    for name, (path, typ) in ENV_OVERRIDES.items():
        raw = env.get(name)
        if raw is None or raw == "":
            continue
        try:
            val = _parse_env(raw, typ)
        except ValueError:
            log.warning("ignoring invalid env %s=%r", name, raw)
            continue
        node = cfg
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = val
    return cfg


def normalize_base_path(bp: str | None) -> str:
    bp = (bp or "").strip()
    if not bp or bp == "/":
        return ""
    if not bp.startswith("/"):
        bp = "/" + bp
    return bp.rstrip("/")


def read_secret(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def stream_url(template: str, stream: str) -> str:
    """go2rtc URL for a stream name; full URLs and local files (tests, replay) pass through."""
    if "://" in stream or stream.startswith(("/", "./", "../")):
        return stream
    return template.replace("{stream}", stream)


@dataclass(frozen=True)
class CameraConfig:
    id: str
    main_stream: str
    sub_stream: str | None
    enabled: bool = True
    record_main: bool = True
    record_sub: bool = True

    def streams(self) -> list[tuple[str, str]]:
        """(quality, go2rtc stream name) pairs this camera should record."""
        if not self.enabled:
            return []
        out = []
        if self.record_main and self.main_stream:
            out.append(("main", self.main_stream))
        if self.record_sub and self.sub_stream:
            out.append(("sub", self.sub_stream))
        return out


def _bool(raw: dict, key: str, default: bool, cam_id: str) -> bool:
    v = raw.get(key, default)
    if not isinstance(v, bool):
        raise ValueError(f"camera {cam_id}: {key} must be true or false")
    return v


def resolve_camera(cam_id: str, raw: dict | None) -> CameraConfig:
    if not isinstance(cam_id, str) or not CAMERA_ID_RE.match(cam_id):
        raise ValueError(f"invalid camera id {cam_id!r} (use a-z, 0-9, _ and -, max 64)")
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError(f"camera {cam_id}: settings must be a mapping")
    unknown = set(raw) - {"main_stream", "sub_stream", "enabled", "record_main", "record_sub", "name"}
    if unknown:
        raise ValueError(f"camera {cam_id}: unknown keys {sorted(unknown)}")
    main = str(raw.get("main_stream") or cam_id)
    sub = raw.get("sub_stream")
    sub = str(sub) if sub else None
    return CameraConfig(
        id=cam_id,
        main_stream=main,
        sub_stream=sub,
        enabled=_bool(raw, "enabled", True, cam_id),
        record_main=_bool(raw, "record_main", True, cam_id),
        record_sub=_bool(raw, "record_sub", bool(sub), cam_id),
    )


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def parse_cameras(data: dict) -> dict[str, CameraConfig]:
    raw = data.get("cameras") or {}
    if not isinstance(raw, dict):
        raise ValueError("cameras.yaml: 'cameras' must be a mapping of id -> settings")
    return {str(cid): resolve_camera(str(cid), r) for cid, r in raw.items()}


def validate(cfg: dict) -> dict:
    cfg["base_path"] = normalize_base_path(cfg.get("base_path"))
    if cfg["auth"]["mode"] not in ("jwt", "none"):
        raise ValueError("auth.mode must be 'jwt' or 'none'")
    rec = cfg["recording"]
    if int(rec["segment_time"]) < 2:
        raise ValueError("recording.segment_time must be >= 2 seconds")
    if rec["audio"] not in ("auto", "off"):
        raise ValueError("recording.audio must be 'auto' or 'off'")
    if float(rec["backoff_initial"]) <= 0 or float(rec["backoff_max"]) < float(rec["backoff_initial"]):
        raise ValueError("recording.backoff_initial must be > 0 and <= backoff_max")
    if float(rec["min_free_gb"]) < 0 or float(rec["resume_free_gb"]) < float(rec["min_free_gb"]):
        raise ValueError("recording.min_free_gb must be >= 0 and <= resume_free_gb")
    return cfg


def load_config(config_dir: str | Path, env: dict | None = None) -> tuple[dict, dict[str, CameraConfig]]:
    d = Path(config_dir)
    cfg = deep_merge(DEFAULTS, _read_yaml(d / "config.yaml"))
    apply_env(cfg, env)
    validate(cfg)
    cameras = parse_cameras(_read_yaml(d / "cameras.yaml"))
    return cfg, cameras
