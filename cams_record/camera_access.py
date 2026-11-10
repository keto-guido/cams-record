"""Per-camera authorization. Contract: docs/camera-access.md.

Python twin of apphub/camera_acl.js; apphub/camera_acl_vectors.json holds the cases both must pass.
Unregistered and forbidden cameras are indistinguishable to the caller: both raise the same 404.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass

from .clips import ClipError

log = logging.getLogger(__name__)

ADMIN_ROLE = "admin"
ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
SNAP_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


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
    def __init__(self, cfg: dict, cameras=None):
        # Registry ACLs stay live on every request. The configured streams are swapped as one
        # snapshot by set_cameras() when the camera list changes (cams_config), never piecemeal.
        self.set_cameras(cameras or {})
        self.enforce = cfg["enforce"]
        self.registry = CameraRegistry(cfg["registry_file"])

    def set_cameras(self, cameras) -> None:
        """Each logical camera id maps to its (main_stream, sub_stream) tuple."""
        sources = {cid: tuple(src for src in (cam.main_stream, cam.sub_stream) if src)
                   for cid, cam in cameras.items()}
        owners: dict[str, set[str]] = {}
        for cid, srcs in sources.items():
            for source in srcs:
                owners.setdefault(source, set()).add(cid)
        self._map = (sources, owners)

    @property
    def sources(self) -> dict:
        return self._map[0]

    @property
    def source_owners(self) -> dict:
        return self._map[1]

    def allowed(self, camera: str, who: Viewer) -> bool:
        """Authorize a logical camera for recorded footage (timeline/export/clip/…).

        - Parent camera id must pass registry ``can_stream`` (exact id/snap).
        - Every cameras.yaml ``main_stream`` / ``sub_stream`` keeps all ``source_owners`` grants
          via ``can_stream(owner)``.
        - If a source is an explicit registered id or snap, also require ``can_stream(src)``
          exactly (do not weaken shared/restricted stream auth).
        - If a source is only a config alias (not registered as id/snap), inherit those parent
          ``source_owners`` grants — no helper registry row and no ``-main`` suffix stripping.
        - Missing cameras.yaml mapping or unknown camera denies, including for admins.
        - Requesting ``cam=<helper>`` without a registry row does not inherit parent capability.
        """
        if who.claimed_user is not None and who.claimed_user != who.user:
            return False
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

    def registered(self, camera: str) -> bool:
        return camera in self.registry.current()

    def can_view_camera(self, camera: str, who: Viewer) -> bool:
        """Registry decision for one camera id, without recordings: used for detection alerts.

        Same rule as a stream name: every registry entry whose id or snap equals ``camera`` must allow
        the viewer. Unregistered cameras and an unusable registry deny everyone, admins included.
        """
        if who.claimed_user is not None and who.claimed_user != who.user:
            return False
        return can_stream(self.registry.current(), camera, who.user, who.roles)

    def can_manage_alerts(self, camera: str, who: Viewer, admin_roles=(ADMIN_ROLE,)) -> bool:
        """Who may pause a camera's phone alerts (for everyone): an admin, or an owner, meaning a user
        named in the camera's registry ``users``. A role grant or an open camera only lets you view.

        Always on top of ``can_view_camera`` (or, with enforce off, a registered camera), so nobody
        manages a camera they cannot see.
        """
        if who.claimed_user is not None and who.claimed_user != who.user:
            return False
        if not who.user:
            return False
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
