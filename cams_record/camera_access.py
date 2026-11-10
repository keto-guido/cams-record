"""Per-camera authorization. Contract: docs/camera-access.md.

Python twin of apphub/camera_acl.js; apphub/camera_acl_vectors.json holds the cases both must pass.
Unregistered and forbidden cameras are indistinguishable to the caller: both raise the same 404.

With ``camera_source: db``, decisions come from cams_config camera objects (not cams.json). File and
shadow modes keep the registry path unchanged. A cams_config outage uses the last-good objects only
while they are younger than ``access.config_acl_max_age_s``; after that only admins pass, and
non-admins never fall back to cams.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass

from .clips import ClipError

log = logging.getLogger(__name__)

ADMIN_ROLE = "admin"
ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
SNAP_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
DEFAULT_CONFIG_ACL_MAX_AGE_S = 60


@dataclass(frozen=True)
class Camera:
    id: str
    name: str
    snap: str | None
    users: frozenset[str]
    roles: frozenset[str]
    restricted: bool


@dataclass(frozen=True)
class Viewer:
    user: str
    roles: tuple[str, ...]
    claimed_user: str | None = None  # x-apphub-user, which must agree with the signed subject


def _acl(entry: dict, key: str) -> frozenset[str] | None:
    """The list under key; None if malformed. Missing is empty, but an explicit null is malformed."""
    if key not in entry:
        return frozenset()
    value = entry[key]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        return None
    return frozenset(value)


def _camera(entry) -> Camera | None:
    if not isinstance(entry, dict):
        return None
    cam_id, name, snap = entry.get("id"), entry.get("name"), entry.get("snap")
    if not isinstance(cam_id, str) or not ID_RE.fullmatch(cam_id) or not isinstance(name, str) or not name:
        return None
    if "snap" in entry and (not isinstance(snap, str) or not SNAP_RE.fullmatch(snap) or ".." in snap):
        return None
    users, roles = _acl(entry, "users"), _acl(entry, "roles")
    malformed = users is None or roles is None  # admin-only, never open
    users, roles = (frozenset() if malformed else users), (frozenset() if malformed else roles)
    return Camera(cam_id, name, snap, users, roles, malformed or bool(users or roles))


def parse_registry(text: str) -> dict[str, Camera]:
    """cams.json text to {id: Camera}. Bad JSON or a non-array means no cameras; bad entries and
    every entry sharing a duplicated id are dropped, so they are unknown to everyone including admin."""
    try:
        data = json.loads(text)
    except ValueError:
        log.error("camera registry is not valid JSON, denying all cameras")
        return {}
    if not isinstance(data, list):
        log.error("camera registry is not an array, denying all cameras")
        return {}
    cameras = [c for c in map(_camera, data) if c]
    # Count raw IDs before validation: a malformed duplicate must not leave a valid twin open.
    counts = Counter(e["id"] for e in data if isinstance(e, dict) and isinstance(e.get("id"), str))
    return {c.id: c for c in cameras if counts[c.id] == 1}


def can_access(camera: Camera | None, user: str, roles) -> bool:
    if camera is None or not user:
        return False
    if ADMIN_ROLE in roles:
        return True
    return not camera.restricted or user in camera.users or bool(camera.roles.intersection(roles))


def can_stream(cameras: dict[str, Camera], name: str, user: str, roles) -> bool:
    """A go2rtc stream is the same footage as every camera using it as id or snap: all must allow it."""
    owners = [c for c in cameras.values() if name in (c.id, c.snap)]
    return bool(owners) and all(can_access(c, user, roles) for c in owners)


def _principals(value) -> frozenset[str]:
    """Valid non-empty string principals. Each malformed entry is skipped on its own (parity with apphub)."""
    if value is None:
        return frozenset()
    if isinstance(value, str) or not isinstance(value, (tuple, list, set, frozenset)):
        return frozenset()  # wholly unusable field → no grants
    return frozenset(item for item in value if isinstance(item, str) and item)


def can_view_config(cam, user: str, roles) -> bool:
    """cams_config camera-object ACL (camera_source: db). Disabled cameras are hidden from everyone.

    - empty user: deny
    - disabled / missing: deny (including admin)
    - role ``admin``: allow every enabled camera
    - ``visibility: everyone``: any cams user (checked before grants; bad grant rows cannot close it)
    - ``visibility: restricted``: granted users/roles; empty grants are admin-only
    - a malformed grant entry is ignored; sibling valid grants still apply
    - malformed visibility: admin-only (never open)
    """
    if cam is None or not user:
        return False
    if not bool(getattr(cam, "enabled", False)):
        return False
    if ADMIN_ROLE in roles:
        return True
    visibility = getattr(cam, "visibility", None)
    # everyone first: a bad grant row must not close an open camera.
    if visibility == "everyone":
        return True
    if visibility == "restricted":
        users, role_grants = _principals(getattr(cam, "users", ())), _principals(getattr(cam, "roles", ()))
        return user in users or bool(role_grants.intersection(roles))
    return False  # unknown / malformed visibility: admin-only


class CameraRegistry:
    """cams.json, re-read when its stat changes so a revocation applies to the next request."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.stamp: tuple | str | None = None
        self.cameras: dict[str, Camera] = {}

    def current(self) -> dict[str, Camera]:
        with self.lock:
            try:
                st = os.stat(self.path)
                stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
                if stamp != self.stamp:
                    with open(self.path, encoding="utf-8") as fh:
                        self.cameras = parse_registry(fh.read())
                    self.stamp = stamp
            except (OSError, UnicodeDecodeError) as exc:
                if self.stamp != "unreadable":
                    log.error("camera registry %s unreadable, denying all cameras: %s", self.path, exc)
                self.stamp, self.cameras = "unreadable", {}
            return self.cameras


class CameraAccess:
    def __init__(self, cfg: dict, cameras=None, *, use_config: bool = False):
        # Registry ACLs stay live on every request. The configured streams are swapped as one
        # snapshot by set_cameras() when the camera list changes (cams_config), never piecemeal.
        # use_config (camera_source: db) takes ACL from cams_config objects instead of cams.json.
        self.set_cameras(cameras or {})
        self.enforce = cfg["enforce"]
        self.registry = CameraRegistry(cfg["registry_file"])
        self.use_config = bool(use_config)
        self.config_max_age_s = float(cfg.get("config_acl_max_age_s", DEFAULT_CONFIG_ACL_MAX_AGE_S))
        self._config_lock = threading.Lock()
        self._objects: dict = {}
        self._loaded_at: float | None = None  # age of the camera-object snapshot (cache saved_at or load)
        self._last_ok_at: float | None = None  # last successful cams_config DB contact
        self._origin: str | None = None

    def set_cameras(self, cameras) -> None:
        """Each logical camera id maps to its (main_stream, sub_stream) tuple."""
        sources = {cid: tuple(src for src in (cam.main_stream, cam.sub_stream) if src)
                   for cid, cam in cameras.items()}
        owners: dict[str, set[str]] = {}
        for cid, srcs in sources.items():
            for source in srcs:
                owners.setdefault(source, set()).add(cid)
        self._map = (sources, owners)

    def sync_config(self, source) -> None:
        """Refresh the cams_config ACL snapshot from a CameraSource (db mode only)."""
        if not self.use_config or source is None:
            return
        with self._config_lock:
            self._objects = dict(source.cameras)
            self._loaded_at = source.loaded_at
            self._last_ok_at = source.last_ok_at
            self._origin = source.origin

    def set_config_objects(self, objects: dict, *, loaded_at: float | None = None,
                           last_ok_at: float | None = None, origin: str | None = None) -> None:
        """Test / direct hook: replace the cams_config ACL snapshot."""
        with self._config_lock:
            self._objects = dict(objects or {})
            self._loaded_at = loaded_at
            self._last_ok_at = last_ok_at
            self._origin = origin

    @property
    def sources(self) -> dict:
        return self._map[0]

    @property
    def source_owners(self) -> dict:
        return self._map[1]

    def _config_snapshot(self) -> tuple[dict, bool]:
        """(objects, non_admin_ok). non_admin_ok is False when the snapshot is missing or too old."""
        with self._config_lock:
            objects = dict(self._objects)
            loaded_at, last_ok_at = self._loaded_at, self._last_ok_at
        if not objects:
            return {}, False
        # Prefer last successful DB contact; fall back to snapshot age (cache saved_at on cold start).
        anchor = last_ok_at if last_ok_at is not None else loaded_at
        if anchor is None:
            return objects, False
        return objects, (time.time() - float(anchor)) <= self.config_max_age_s

    def _viewer_ok(self, who: Viewer) -> bool:
        return who.claimed_user is None or who.claimed_user == who.user

    def _can_view_object(self, cam, who: Viewer, *, non_admin_ok: bool) -> bool:
        if not who.user:
            return False
        if ADMIN_ROLE in who.roles:
            return can_view_config(cam, who.user, who.roles)
        if not non_admin_ok:
            return False  # stale / DB-down: never widen via cams.json
        return can_view_config(cam, who.user, who.roles)

    def allowed(self, camera: str, who: Viewer) -> bool:
        """Authorize a logical camera for recorded footage (timeline/export/clip/…).

        Registry mode (file/shadow):
        - Parent camera id must pass registry ``can_stream`` (exact id/snap).
        - Every cameras.yaml ``main_stream`` / ``sub_stream`` keeps all ``source_owners`` grants
          via ``can_stream(owner)``.
        - If a source is an explicit registered id or snap, also require ``can_stream(src)``
          exactly (do not weaken shared/restricted stream auth).
        - If a source is only a config alias (not registered as id/snap), inherit those parent
          ``source_owners`` grants — no helper registry row and no ``-main`` suffix stripping.
        - Missing cameras.yaml mapping or unknown camera denies, including for admins.
        - Requesting ``cam=<helper>`` without a registry row does not inherit parent capability.

        cams_config mode (camera_source: db):
        - Parent and every ``source_owners`` peer must pass ``can_view_config``.
        - Disabled cameras are hidden; stale last-good cache is admin-only.
        """
        if not self._viewer_ok(who):
            return False
        if self.use_config:
            return self._allowed_config(camera, who)
        return self._allowed_registry(camera, who)

    def _allowed_registry(self, camera: str, who: Viewer) -> bool:
        cameras = self.registry.current()
        source_map, source_owners = self._map  # one consistent snapshot for this decision
        sources = source_map.get(camera)
        if not (camera in cameras and bool(sources)):
            return False
        user, roles = who.user, who.roles
        if not can_stream(cameras, camera, user, roles):
            return False
        for src in sources:
            owners = source_owners[src]
            if not all(can_stream(cameras, owner, user, roles) for owner in owners):
                return False
            if any(src in (c.id, c.snap) for c in cameras.values()):
                if not can_stream(cameras, src, user, roles):
                    return False
        return True

    def _allowed_config(self, camera: str, who: Viewer) -> bool:
        objects, non_admin_ok = self._config_snapshot()
        source_map, source_owners = self._map
        sources = source_map.get(camera)
        cam = objects.get(camera)
        if not (cam is not None and bool(sources)):
            return False
        if not self._can_view_object(cam, who, non_admin_ok=non_admin_ok):
            return False
        for src in sources:
            for owner_id in source_owners.get(src) or ():
                owner = objects.get(owner_id)
                if not self._can_view_object(owner, who, non_admin_ok=non_admin_ok):
                    return False
        return True

    def registered(self, camera: str) -> bool:
        if self.use_config:
            objects, _ = self._config_snapshot()
            return camera in objects
        return camera in self.registry.current()

    def can_view_camera(self, camera: str, who: Viewer) -> bool:
        """Authorize one camera id without recordings: used for detection alerts.

        Registry mode: every registry entry whose id or snap equals ``camera`` must allow the viewer.
        cams_config mode: the camera object id must pass ``can_view_config`` (enabled + visibility).
        Unusable / empty sources deny everyone, admins included. Stale DB-down cache is admin-only
        and never falls back to cams.json for non-admins.
        """
        if not self._viewer_ok(who):
            return False
        if self.use_config:
            objects, non_admin_ok = self._config_snapshot()
            return self._can_view_object(objects.get(camera), who, non_admin_ok=non_admin_ok)
        return can_stream(self.registry.current(), camera, who.user, who.roles)

    def can_manage_alerts(self, camera: str, who: Viewer, admin_roles=(ADMIN_ROLE,)) -> bool:
        """Who may pause a camera's phone alerts (for everyone): an admin, or an owner, meaning a user
        named in the camera's ``users`` grant. A role grant or an open camera only lets you view.

        Registry mode reads cams.json ``users``; cams_config mode reads the camera object's ``users``.
        Always on top of ``can_view_camera`` (or, with enforce off, a known camera), so nobody
        manages a camera they cannot see.
        """
        if not self._viewer_ok(who) or not who.user:
            return False
        if self.use_config:
            if self.enforce and not self.can_view_camera(camera, who):
                return False
            if set(admin_roles or ()) & set(who.roles):
                return True
            objects, _ = self._config_snapshot()
            cam = objects.get(camera)
            if cam is None or not bool(getattr(cam, "enabled", False)):
                return False
            return who.user in _principals(getattr(cam, "users", ()))
        cameras = self.registry.current()
        if self.enforce and not can_stream(cameras, camera, who.user, who.roles):
            return False
        if set(admin_roles or ()) & set(who.roles):
            return True
        entry = cameras.get(camera)
        return entry is not None and who.user in entry.users

    def require(self, camera: str, who: Viewer) -> None:
        if self.enforce and not self.allowed(camera, who):
            raise ClipError(404, "camera not found")
