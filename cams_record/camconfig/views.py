"""What each service sees of a camera object, in the shape its file config has today.

These are the functions the services will use when they switch from files to cams_config, and the
import check compares them with today's files, so "imported" provably means "same behaviour".
"""

from __future__ import annotations

from ..config import CameraConfig
from .model import CameraObject

# Stream fields cams_config stores as stream purposes, not inside camera_detection.settings_json.
# ``name`` may stay in detection so the detect surface keeps its own display name when it differs
# from the hub registry name on CameraObject.name.
DETECT_STREAM_KEYS = ("stream", "main_stream")


def record_camera(cam: CameraObject) -> CameraConfig:
    """cams-record's CameraConfig: main/sub names (also used to authorize old footage) and what to record."""
    names = {k: s.go2rtc_name for k, s in cam.streams.items()}
    # Keep the sub mapping even when there is no main stream (sub-only cameras still record as sub).
    # main_stream falls back to the sub name so old footage keyed by that stream stays authorizable.
    main = names.get("main") or names.get("sub") or cam.id
    sub = names.get("sub")
    record = cam.effective_use().get("record") or ()
    return CameraConfig(id=cam.id, main_stream=main, sub_stream=sub, enabled=bool(record) and cam.enabled,
                        record_main="main" in record, record_sub="sub" in record and sub is not None)


def detect_camera(cam: CameraObject) -> dict | None:
    """cams-detect's cameras.yaml entry, or None when the camera is not set up for detection."""
    if cam.detection is None:
        return None
    stream = cam.stream_for("detect")
    if stream is None:
        return None
    raw = {k: v for k, v in cam.detection.items() if k not in DETECT_STREAM_KEYS}
    raw.setdefault("name", cam.name)
    raw["stream"] = stream
    verify = cam.stream_for("verify")
    if verify:
        raw["main_stream"] = verify
    if not cam.enabled:
        raw["enabled"] = False
    return raw


def registry(cams: dict[str, CameraObject]) -> list[dict]:
    """App Hub cams.json for these cameras, sorted like the admin list."""
    return [c.registry_entry() for c in sorted(cams.values(), key=lambda c: (c.sort_order, c.id))]
