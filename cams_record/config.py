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
import math
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
    "clips": {"cache_dir": "", "max_cache_gb": 2, "max_age_s": 86400,
              "export_max_s": 1800, "timeout_s": 120, "gap_tolerance_s": 2.0, "timezone": "America/Chicago"},
    "detection_protection": {"enabled": True, "interval_s": 60, "days": 14,
                             "lookback_s": 600, "pre_s": 5, "post_s": 5,
                             "fallback_s": 10, "max_window_s": 120},
    "retention": {
        "main_days": 3,
        "sub_days": 14,
        "cameras": {},  # per-camera overrides: <id>: {main_days: 7, sub_days: 30}
        "max_total_gb": 1500,  # budget for indexed bytes; over it the oldest main, then sub, is deleted
        "dry_run": True,  # log and report what would be deleted, delete nothing
        "interval_s": 300,
        "batch_size": 500,  # most segments handled per pass
        "nice": 19,  # janitor thread only
    },
    "auth": {
        "app_id": "cams-record",
        "mode": "jwt",  # jwt | none
        "secret_file": "/run/secrets/apphub_jwt",
        "header": "x-apphub-token",
        "roles": ["admin"],  # legacy setting; non-clip API routes always require admin
        "leeway_seconds": 30,
    },
    "push": {
        "enabled": True,  # still inactive until vapid_private_file is present
        "vapid_private_file": "/run/secrets/vapid_private_key",
        "vapid_public_key": "",  # optional override; otherwise derived from the private key
        "vapid_subject": "mailto:cams@localhost",  # contact URI required by VAPID
        "webhook_secret_file": "/run/secrets/push_webhook_secret",
        "poll_enabled": True,  # also send from the cams_detect.events poller
        "interval_s": 15,
        "lookback_s": 120,
        "labels": [],  # empty = all labels; e.g. [person, car]
        "ttl_s": 86400,
        "timeout_s": 10,
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
    "CLIP_EXPORT_MAX_S": (("clips", "export_max_s"), float),
    "CLIP_CACHE_DIR": (("clips", "cache_dir"), str),
    "CLIP_CACHE_GB": (("clips", "max_cache_gb"), float),
    "CLIP_GAP_TOLERANCE_S": (("clips", "gap_tolerance_s"), float),
    "CLIP_TIMEOUT_S": (("clips", "timeout_s"), float),
    "CLIP_TIMEZONE": (("clips", "timezone"), str),
    "DETECTION_PROTECTION_ENABLED": (("detection_protection", "enabled"), bool),
    "DETECTION_PROTECTION_INTERVAL_S": (("detection_protection", "interval_s"), float),
    "DETECTION_PROTECTION_DAYS": (("detection_protection", "days"), float),
    "AUTH_APP_ID": (("auth", "app_id"), str),
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
    "PUSH_ENABLED": (("push", "enabled"), bool),
    "PUSH_VAPID_PRIVATE_FILE": (("push", "vapid_private_file"), str),
    "PUSH_VAPID_PUBLIC_KEY": (("push", "vapid_public_key"), str),
    "PUSH_VAPID_SUBJECT": (("push", "vapid_subject"), str),
    "PUSH_WEBHOOK_SECRET_FILE": (("push", "webhook_secret_file"), str),
    "PUSH_POLL_ENABLED": (("push", "poll_enabled"), bool),
    "PUSH_INTERVAL_S": (("push", "interval_s"), float),
    "PUSH_LOOKBACK_S": (("push", "lookback_s"), float),
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


def _positive(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a number > 0")


def _validate_retention(ret: dict) -> None:
    if not isinstance(ret["dry_run"], bool):
        raise ValueError("retention.dry_run must be true or false")
    for key in ("main_days", "sub_days", "max_total_gb", "interval_s"):
        _positive(f"retention.{key}", ret[key])
    if type(ret["batch_size"]) is not int or ret["batch_size"] < 1:
        raise ValueError("retention.batch_size must be an integer >= 1")
    if type(ret["nice"]) is not int or not 0 <= ret["nice"] <= 19:
        raise ValueError("retention.nice must be an integer from 0 to 19")
    cameras = ret.get("cameras") or {}
    if not isinstance(cameras, dict):
        raise ValueError("retention.cameras must be a mapping of id -> overrides")
    for cam_id, override in cameras.items():
        if not isinstance(override, dict) or set(override) - {"main_days", "sub_days"}:
            raise ValueError(f"retention.cameras.{cam_id}: only main_days and sub_days can be set")
        for key, value in override.items():
            _positive(f"retention.cameras.{cam_id}.{key}", value)
    ret["cameras"] = {str(cam_id): override for cam_id, override in cameras.items()}


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
    from zoneinfo import ZoneInfo

    ZoneInfo(cfg["clips"]["timezone"])
    export_max = cfg["clips"]["export_max_s"]
    if not isinstance(export_max, int | float) or not math.isfinite(export_max) or not 1 <= export_max <= 1800:
        raise ValueError("clips.export_max_s must be between 1 and 1800")
    for section, keys in (("clips", ("max_cache_gb", "max_age_s", "timeout_s", "gap_tolerance_s", "export_max_s")),
                          ("detection_protection", ("interval_s", "days", "lookback_s", "fallback_s", "max_window_s"))):
        for key in keys:
            _positive(f"{section}.{key}", cfg[section][key])
    for key in ("pre_s", "post_s"):
        if cfg["detection_protection"][key] < 0:
            raise ValueError(f"detection_protection.{key} must be >= 0")
    if not isinstance(cfg["detection_protection"]["enabled"], bool):
        raise ValueError("detection_protection.enabled must be true or false")
    push = cfg.setdefault("push", {})
    for key in ("enabled", "poll_enabled"):
        if key in push and not isinstance(push[key], bool):
            raise ValueError(f"push.{key} must be true or false")
    for key in ("interval_s", "lookback_s", "ttl_s", "timeout_s"):
        if key in push:
            _positive(f"push.{key}", push[key])
    labels = push.get("labels") or []
    if not isinstance(labels, list) or any(not isinstance(x, str) or not x for x in labels):
        raise ValueError("push.labels must be a list of non-empty strings")
    push["labels"] = [str(x) for x in labels]
    subject = str(push.get("vapid_subject") or "").strip()
    if subject and not (subject.startswith("mailto:") or subject.startswith("https://")):
        raise ValueError("push.vapid_subject must be a mailto: or https: URI")
    _validate_retention(cfg["retention"])
    return cfg


def load_config(config_dir: str | Path, env: dict | None = None) -> tuple[dict, dict[str, CameraConfig]]:
    d = Path(config_dir)
    cfg = deep_merge(DEFAULTS, _read_yaml(d / "config.yaml"))
    apply_env(cfg, env)
    validate(cfg)
    cameras = parse_cameras(_read_yaml(d / "cameras.yaml"))
    return cfg, cameras
