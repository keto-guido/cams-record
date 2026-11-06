"""The camera object: one camera's identity, streams, stream purposes, access and per-service settings.

Pure data and validation, no database. ``CameraObject.validate()`` returns every problem as text so
an import or an admin form can show them all at once.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from ..camera_access import ID_RE as REGISTRY_ID_RE
from ..camera_access import SNAP_RE

PURPOSES = ("live", "live_hd", "detect", "verify", "snapshot", "record")
MULTI_PURPOSES = frozenset({"record"})
# cams-record files segments under quality main/sub, so only those keys can be recorded for now.
RECORDABLE_KEYS = frozenset({"main", "sub"})
CONNECTORS = ("go2rtc", "tunnel_webcam")
VISIBILITY = ("everyone", "restricted")
STREAM_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
GO2RTC_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}")
PRINCIPAL_MAX = 128


@dataclass
class Stream:
    key: str
    go2rtc_name: str
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    fps: float | None = None


@dataclass
class CameraObject:
    id: str
    name: str
    enabled: bool = True
    connector_type: str = "go2rtc"
    connector: dict = field(default_factory=dict)  # non-secret connector settings only
    visibility: str = "restricted"  # new cameras are admin-only until someone is granted access
    users: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    sort_order: int = 0
    streams: dict[str, Stream] = field(default_factory=dict)
    # purpose -> stream keys. Exactly what is stored: a missing purpose is off (e.g. no record = not
    # recorded). default_use() only pre-fills new cameras.
    use: dict[str, tuple[str, ...]] = field(default_factory=dict)
    detection: dict | None = None  # cams-detect settings; None = not set up for detection
    recording: dict | None = None  # {"main_days": x, "sub_days": y} retention overrides
    notify: list[dict] = field(default_factory=list)

    # -- stream purposes ---------------------------------------------------
    def default_use(self) -> dict[str, tuple[str, ...]]:
        """Low-res for live and detection, high-res for verification, HD live and recording."""
        keys = self.streams
        low = "sub" if "sub" in keys else ("main" if "main" in keys else None)
        high = "main" if "main" in keys else low
        out: dict[str, tuple[str, ...]] = {}
        if low:
            out["live"] = (low,)
            out["detect"] = (low,)
            out["snapshot"] = (low,)
        if high:
            out["live_hd"] = (high,)
            out["verify"] = (high,)
        out["record"] = tuple(k for k in ("main", "sub") if k in keys)
        return out

    def effective_use(self) -> dict[str, tuple[str, ...]]:
        return {p: tuple(k) for p, k in self.use.items() if k}

    def stream_for(self, purpose: str) -> str | None:
        """go2rtc stream name serving a single-stream purpose, or None."""
        keys = self.effective_use().get(purpose) or ()
        return self.streams[keys[0]].go2rtc_name if keys else None

    def streams_for(self, purpose: str) -> list[tuple[str, str]]:
        """(stream key, go2rtc name) pairs for a purpose, in key order."""
        return [(k, self.streams[k].go2rtc_name) for k in self.effective_use().get(purpose) or ()]

    # -- validation -----------------------------------------------------------
    def validate(self) -> list[str]:
        errs: list[str] = []
        cid = self.id if isinstance(self.id, str) else repr(self.id)
        if not isinstance(self.id, str) or not REGISTRY_ID_RE.fullmatch(self.id):
            errs.append(f"{cid}: id must be a-z, 0-9 and '-', 1-64 characters, not starting or ending with '-'")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 128:
            errs.append(f"{cid}: name must be 1-128 characters")
        if self.connector_type not in CONNECTORS:
            errs.append(f"{cid}: connector_type must be one of {', '.join(CONNECTORS)}")
        if not isinstance(self.connector, dict):
            errs.append(f"{cid}: connector settings must be an object")
        if self.visibility not in VISIBILITY:
            errs.append(f"{cid}: visibility must be everyone or restricted")
        for kind, names in (("user", self.users), ("role", self.roles)):
            for n in names:
                if not isinstance(n, str) or not n or len(n) > PRINCIPAL_MAX:
                    errs.append(f"{cid}: invalid {kind} {n!r}")
        if self.visibility == "everyone" and (self.users or self.roles):
            errs.append(f"{cid}: users/roles only apply to restricted cameras")
        if not self.streams:
            errs.append(f"{cid}: at least one stream is required")
        for key, s in self.streams.items():
            if key != s.key or not STREAM_KEY_RE.fullmatch(key):
                errs.append(f"{cid}: invalid stream key {key!r}")
            if not isinstance(s.go2rtc_name, str) or not GO2RTC_NAME_RE.fullmatch(s.go2rtc_name) \
                    or ".." in s.go2rtc_name:
                errs.append(f"{cid}: stream {key}: invalid go2rtc stream name {s.go2rtc_name!r}")
        for purpose, keys in self.use.items():
            if purpose not in PURPOSES:
                errs.append(f"{cid}: unknown stream purpose {purpose!r}")
                continue
            if len(keys) > 1 and purpose not in MULTI_PURPOSES:
                errs.append(f"{cid}: {purpose} can use only one stream")
            for k in keys:
                if k not in self.streams:
                    errs.append(f"{cid}: {purpose} uses unknown stream {k!r}")
        record = self.effective_use().get("record") or ()
        bad = [k for k in record if k not in RECORDABLE_KEYS]
        if bad:
            errs.append(f"{cid}: only streams keyed main or sub can be recorded (got {', '.join(bad)})")
        if self.detection is not None and not isinstance(self.detection, dict):
            errs.append(f"{cid}: detection settings must be an object")
        if self.recording is not None:
            if not isinstance(self.recording, dict) or set(self.recording) - {"main_days", "sub_days"}:
                errs.append(f"{cid}: recording settings may only hold main_days and sub_days")
            else:
                for k, v in self.recording.items():
                    if v is not None and (isinstance(v, bool) or not isinstance(v, int | float) or v <= 0):
                        errs.append(f"{cid}: recording.{k} must be a number > 0")
        return errs

    # -- App Hub registry -----------------------------------------------------
    def registry_entry(self) -> dict:
        """This camera as a cams.json entry (docs/camera-access.md) with the same access decisions.

        A restricted camera with no grants becomes ``roles: ["admin"]``: admins always pass, nobody
        else holds a role literally named admin without being one. The hub's live view plays the
        stream named after the id; a different snapshot stream is exported as ``snap``.
        """
        entry: dict = {"id": self.id, "name": self.name}
        snap = self.stream_for("snapshot")
        if snap and snap != self.id:
            if not SNAP_RE.fullmatch(snap) or ".." in snap:
                raise ValueError(f"{self.id}: snapshot stream {snap!r} cannot be published as a registry snap")
            entry["snap"] = snap
        if self.visibility == "restricted":
            users, roles = sorted(set(self.users)), sorted(set(self.roles))
            if users:
                entry["users"] = users
            entry["roles"] = roles if (roles or users) else ["admin"]
        return entry

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "connector_type": self.connector_type, "connector": copy.deepcopy(self.connector),
            "visibility": self.visibility, "users": sorted(self.users), "roles": sorted(self.roles),
            "sort_order": self.sort_order,
            "streams": {k: {"go2rtc_name": s.go2rtc_name, "width": s.width, "height": s.height,
                            "codec": s.codec, "fps": s.fps} for k, s in sorted(self.streams.items())},
            "use": {p: list(k) for p, k in sorted(self.use.items())},
            "detection": copy.deepcopy(self.detection), "recording": copy.deepcopy(self.recording),
            "notify": copy.deepcopy(self.notify),
        }

    @classmethod
    def from_dict(cls, d: dict) -> CameraObject:
        """Inverse of to_dict (used for the local last-good cache)."""
        return cls(
            id=d["id"], name=d["name"], enabled=bool(d.get("enabled", True)),
            connector_type=d.get("connector_type", "go2rtc"), connector=copy.deepcopy(d.get("connector") or {}),
            visibility=d.get("visibility", "restricted"), users=tuple(d.get("users") or ()),
            roles=tuple(d.get("roles") or ()), sort_order=int(d.get("sort_order", 0)),
            streams={k: Stream(k, s["go2rtc_name"], s.get("width"), s.get("height"), s.get("codec"), s.get("fps"))
                     for k, s in (d.get("streams") or {}).items()},
            use={p: tuple(k) for p, k in (d.get("use") or {}).items()},
            detection=copy.deepcopy(d.get("detection")), recording=copy.deepcopy(d.get("recording")),
            notify=copy.deepcopy(d.get("notify") or []),
        )
