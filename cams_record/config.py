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
# Ids App Hub's cams.json accepts (docs/camera-access.md); ids outside it still load but nobody can view them.
REGISTRY_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
QUALITIES = ("main", "sub")

DEFAULTS: dict[str, Any] = {
    "log_level": "INFO",
    "base_path": "/cams-record",
    # Where cameras come from (docs/camera-model.md): file = cameras.yaml (default); shadow = cameras.yaml,
    # while also reading cams_config and reporting every difference; db = cams_config.
    "camera_source": "file",
    "http": {"host": "127.0.0.1", "port": 8097},
    "go2rtc": {"rtsp_url": "rtsp://127.0.0.1:8554/{stream}",
               "api_url": "http://127.0.0.1:1984"},  # stream list for the camera admin
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
        "roles": ["cams", "admin"],
        "admin_roles": ["admin"],
        "leeway_seconds": 30,
    },
    "access": {
        "enforce": True,  # false turns off per-camera checks (docs/camera-access.md)
        "registry_file": "/apphub-data/cams.json",  # App Hub's registry; bind its directory, not the file
        # camera_source db only: write registry_file from cams_config on every change. The registry
        # directory must then be mounted read-write for cams-record (and stay read-only elsewhere).
        "publish_registry": False,
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
        # Roles saved with a subscription (refreshed whenever the app opens) stop counting after this;
        # the subscriber then only gets alerts for open cameras and cameras that name them.
        "roles_max_age_days": 30,
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
    # Camera objects (docs/camera-model.md). Only `python -m cams_record.camconfig` uses this so far.
    "cams_config": {
        "host": "127.0.0.1",
        "port": 3306,
        "database": "cams_config",
        "user": "cams_record",
        "password_file": "/run/secrets/db_password",
        "connect_timeout": 5,
        "poll_s": 5,  # how often the revision is checked
        "cache_file": "",  # last good camera objects; default recording.state_dir/cams_config.json
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
    "CAMERA_SOURCE": (("camera_source",), str),
    "ACCESS_PUBLISH_REGISTRY": (("access", "publish_registry"), bool),
    "AUTH_APP_ID": (("auth", "app_id"), str),
    "ACCESS_ENFORCE": (("access", "enforce"), bool),
    "ACCESS_REGISTRY_FILE": (("access", "registry_file"), str),
    "LOG_LEVEL": (("log_level",), str),
    "BASE_PATH": (("base_path",), str),
    "HTTP_HOST": (("http", "host"), str),
    "HTTP_PORT": (("http", "port"), int),
    "AUTH_MODE": (("auth", "mode"), str),
    "AUTH_SECRET_FILE": (("auth", "secret_file"), str),
    "GO2RTC_RTSP_URL": (("go2rtc", "rtsp_url"), str),
    "GO2RTC_API_URL": (("go2rtc", "api_url"), str),
    "RECORD_ROOT": (("recording", "root"), str),
    "SEGMENT_TIME": (("recording", "segment_time"), int),
    "STATE_DIR": (("recording", "state_dir"), str),
    "DB_HOST": (("db", "host"), str),
    "DB_PORT": (("db", "port"), int),
    "DB_NAME": (("db", "database"), str),
    "DB_USER": (("db", "user"), str),
    "DB_PASSWORD_FILE": (("db", "password_file"), str),
    "CAMS_CONFIG_DB_HOST": (("cams_config", "host"), str),
    "CAMS_CONFIG_DB_PORT": (("cams_config", "port"), int),
    "CAMS_CONFIG_DB_NAME": (("cams_config", "database"), str),
    "CAMS_CONFIG_DB_USER": (("cams_config", "user"), str),
    "CAMS_CONFIG_DB_PASSWORD_FILE": (("cams_config", "password_file"), str),
    "CAMS_CONFIG_CACHE_FILE": (("cams_config", "cache_file"), str),
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
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
        raise ValueError("must be a recognized boolean")
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
        except ValueError as exc:
            if typ is bool:
                raise ValueError(f"{name} must be a recognized boolean") from exc
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


def camera_warnings(cam: CameraConfig, raw: dict | None) -> list[str]:
    """Settings that load but are probably not what was meant. Behaviour is unchanged; these are logged."""
    out = []
    if not REGISTRY_ID_RE.match(cam.id):
        out.append(f"camera {cam.id}: id is not valid in the App Hub camera registry (a-z, 0-9 and '-', not "
                   "starting or ending with '-'); the registry drops it, so nobody can view its recordings")
    if not (raw or {}).get("main_stream"):
        out.append(f"camera {cam.id}: main_stream is not set, so stream '{cam.main_stream}' is recorded as main "
                   f"(cams-detect defaults the high-resolution stream to '{cam.id}-main'); set main_stream explicitly")
    if cam.sub_stream and cam.sub_stream == cam.main_stream and cam.record_main and cam.record_sub:
        out.append(f"camera {cam.id}: main_stream and sub_stream are both '{cam.main_stream}', "
                   "so the same stream is recorded twice")
    return out


def parse_cameras(data: dict) -> dict[str, CameraConfig]:
    raw = data.get("cameras") or {}
    if not isinstance(raw, dict):
        raise ValueError("cameras.yaml: 'cameras' must be a mapping of id -> settings")
    cams = {str(cid): resolve_camera(str(cid), r) for cid, r in raw.items()}
    for cid, cam in cams.items():
        for msg in camera_warnings(cam, raw.get(cid)):
            log.warning("%s", msg)
    return cams


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
    if not isinstance(cfg["access"]["enforce"], bool):
        raise ValueError("access.enforce must be true or false")
    if cfg["access"]["enforce"] and not cfg["access"]["registry_file"]:
        raise ValueError("access.registry_file is required while access.enforce is true")
    if cfg["camera_source"] not in ("file", "shadow", "db"):
        raise ValueError("camera_source must be file, shadow or db")
    if not isinstance(cfg["access"].get("publish_registry", False), bool):
        raise ValueError("access.publish_registry must be true or false")
    if cfg["access"].get("publish_registry"):
        if cfg["camera_source"] != "db":
            raise ValueError("access.publish_registry needs camera_source: db")
        if not cfg["access"]["registry_file"]:
            raise ValueError("access.publish_registry needs access.registry_file")
    cc = cfg["cams_config"]
    _positive("cams_config.poll_s", cc["poll_s"])
    if not cc.get("cache_file"):
        cc["cache_file"] = str(Path(cfg["recording"]["state_dir"]) / "cams_config.json")
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
    for key in ("interval_s", "lookback_s", "ttl_s", "timeout_s", "roles_max_age_days"):
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
