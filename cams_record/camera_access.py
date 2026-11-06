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
# HD companion go2rtc name for cameras.yaml `main_stream: <id>-main`. Not an arbitrary suffix.
MAIN_COMPANION_SUFFIX = "-main"


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


def main_companion_parent(name: str) -> str | None:
    """Parent camera id for an HD ``<id>-main`` companion stream name, else None.

    Scoped to the exact ``-main`` suffix used by cameras.yaml ``main_stream`` companions.
    Arbitrary suffixes (``-hd``, ``-hires``, …) do not inherit. The parent must itself be a
    valid registry camera id shape; this helper does not invent registry rows.
    """
    if not isinstance(name, str) or not name.endswith(MAIN_COMPANION_SUFFIX):
        return None
    parent = name[: -len(MAIN_COMPANION_SUFFIX)]
    if not parent or not ID_RE.fullmatch(parent):
        return None
    return parent


def is_main_companion_id(camera_id: str) -> bool:
    """True for internal HD helper ids that must not appear in App Hub list/publication."""
    return main_companion_parent(camera_id) is not None


def publishable_cameras(cameras: dict[str, Camera]) -> list[Camera]:
    """Registry cameras that belong in the shared App Hub list/publication.

    Live grid and ``/cams/api/list`` must expose only parent low-res cameras. Internal
    ``*-main`` HD companions used as go2rtc ``main_stream`` names stay out of the UX even
    if a stale helper row remains in ``cams.json`` during migration.
    """
    return [c for c in cameras.values() if not is_main_companion_id(c.id)]


def can_stream(cameras: dict[str, Camera], name: str, user: str, roles) -> bool:
    """A go2rtc stream is the same footage as every camera using it as id or snap: all must allow it.

    Exact id/snap ownership only. Recorder HD ``main_stream`` companions inherit via
    ``CameraAccess`` (configured parent mapping), not through this shared stream helper, so
    App Hub live ``canStream`` stays exact until its twin is refreshed separately.
    """
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
        # Copy configured raw sources once; registry ACLs remain live on every request.
        self.sources = {cid: tuple(src for src in (cam.main_stream, cam.sub_stream) if src)
                        for cid, cam in (cameras or {}).items()}
        self.source_owners = {}
        for cid, sources in self.sources.items():
            for source in sources:
                self.source_owners.setdefault(source, set()).add(cid)
        self.enforce = cfg["enforce"]
        self.registry = CameraRegistry(cfg["registry_file"])

    def _stream_owners(self, cameras: dict[str, Camera], name: str) -> list[Camera]:
        """Registry owners of a stream name, plus a configured ``<parent>-main`` companion parent.

        A ``*-main`` name inherits the parent camera's ACL only when that parent is a real
        registry camera and cameras.yaml maps the companion as one of the parent's sources.
        No parent mapping and no id/snap hit means unknown/denied — including for admins.
        Direct registry ``*-main`` rows still count as owners when present (migration).
        """
        owners = [c for c in cameras.values() if name in (c.id, c.snap)]
        parent_id = main_companion_parent(name)
        if parent_id is not None and parent_id in self.source_owners.get(name, ()):
            parent = cameras.get(parent_id)
            if parent is not None and all(o.id != parent.id for o in owners):
                owners.append(parent)
        return owners

    def _can_source(self, cameras: dict[str, Camera], name: str, who: Viewer) -> bool:
        owners = self._stream_owners(cameras, name)
        return bool(owners) and all(can_access(c, who.user, who.roles) for c in owners)

    def allowed(self, camera: str, who: Viewer) -> bool:
        if who.claimed_user is not None and who.claimed_user != who.user:
            return False
        cameras = self.registry.current()
        sources = self.sources.get(camera)
        # Camera id itself must be a real registry entry. Companion stream names used only as
        # main_stream sources inherit below; requesting cam=<id>-main without a registry row
        # does not gain the parent's capabilities.
        return (camera in cameras and bool(sources)
                and self._can_source(cameras, camera, who)
                and all(self._can_source(cameras, src, who)
                        and all(self._can_source(cameras, owner, who)
                                for owner in self.source_owners[src]) for src in sources))

    def require(self, camera: str, who: Viewer) -> None:
        if self.enforce and not self.allowed(camera, who):
            raise ClipError(404, "camera not found")
