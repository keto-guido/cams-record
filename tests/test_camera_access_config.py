"""camera_source=db ACL: can_view_camera / CameraAccess use cams_config, not cams.json."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

from cams_record.camconfig.importer import build_plan
from cams_record.camconfig.model import CameraObject, Stream
from cams_record.camconfig.views import record_camera, registry
from cams_record.camera_access import (
    CameraAccess,
    Viewer,
    can_access,
    can_stream,
    can_view_config,
    parse_registry,
)
from cams_record.config import resolve_camera

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "cams_config_topology_20261106"


def _obj(cid="front-door", *, enabled=True, visibility="everyone", users=(), roles=(),
         main=None, sub=None, **kw) -> CameraObject:
    main = cid if main is None else main
    sub = cid if sub is None else sub
    streams = {}
    use = {}
    if main:
        streams["main"] = Stream("main", main)
    if sub:
        streams["sub"] = Stream("sub", sub)
    if streams:
        use["record"] = tuple(streams)
    return CameraObject(id=cid, name=cid.replace("-", " ").title(), enabled=enabled,
                        visibility=visibility, users=tuple(users), roles=tuple(roles),
                        streams=streams, use=use, **kw)


def _access(objects: dict[str, CameraObject], *, max_age=60, loaded_at=None, last_ok_at=None,
            origin="db", registry_file="/nonexistent/cams.json") -> CameraAccess:
    cameras = {cid: record_camera(o) for cid, o in objects.items()}
    access = CameraAccess(
        {"enforce": True, "registry_file": registry_file, "config_acl_max_age_s": max_age},
        cameras, use_config=True,
    )
    now = time.time()
    access.set_config_objects(objects, loaded_at=loaded_at if loaded_at is not None else now,
                              last_ok_at=last_ok_at if last_ok_at is not None else now,
                              origin=origin)
    return access


def test_can_view_config_semantics():
    open_cam = _obj("open", visibility="everyone")
    family = _obj("family", visibility="restricted", roles=("family",))
    private = _obj("private", visibility="restricted", users=("ann",))
    empty = _obj("empty", visibility="restricted")  # admin-only
    disabled = _obj("disabled", enabled=False, visibility="everyone")
    assert can_view_config(open_cam, "bob", ("cams",))
    assert can_view_config(open_cam, "root", ("admin",))
    assert not can_view_config(open_cam, "", ("admin",))
    assert can_view_config(family, "bob", ("family",))
    assert not can_view_config(family, "bob", ("cams",))
    assert can_view_config(private, "ann", ("cams",))
    assert not can_view_config(private, "bob", ("cams",))
    assert can_view_config(empty, "root", ("admin",))
    assert not can_view_config(empty, "bob", ("cams",))
    assert not can_view_config(disabled, "bob", ("cams",))
    assert not can_view_config(disabled, "root", ("admin",))  # disabled hidden
    assert not can_view_config(None, "root", ("admin",))


def test_empty_grants_admin_only():
    access = _access({"vault": _obj("vault", visibility="restricted")})
    assert access.can_view_camera("vault", Viewer("root", ("admin",)))
    assert not access.can_view_camera("vault", Viewer("bob", ("cams",)))
    assert access.allowed("vault", Viewer("root", ("admin",)))
    assert not access.allowed("vault", Viewer("ann", ("cams",)))


def test_unknown_users_denied():
    access = _access({"private": _obj("private", visibility="restricted", users=("ann",))})
    assert access.can_view_camera("private", Viewer("ann", ("cams",)))
    assert not access.can_view_camera("private", Viewer("zed", ("cams",)))
    assert not access.can_view_camera("missing", Viewer("ann", ("cams",)))
    assert not access.can_view_camera("missing", Viewer("root", ("admin",)))


def test_parity_malformed_grant_ignored_valid_still_apply():
    """(a) A malformed grant entry is skipped; sibling valid grants still apply."""
    cam = _obj("private", visibility="restricted", users=("ann",))
    cam.users = [None, "ann", "", 3]  # only "ann" counts
    access = _access({"private": cam})
    assert access.can_view_camera("private", Viewer("ann", ("cams",)))
    assert not access.can_view_camera("private", Viewer("bob", ("cams",)))


def test_parity_everyone_before_bad_grants():
    """(b) visibility everyone is checked first: bad grant rows cannot close an open camera."""
    cam = _obj("open", visibility="everyone")
    cam.users = [None, ""]  # would be poison if grants were consulted first
    cam.roles = "not-a-list"
    access = _access({"open": cam})
    assert access.can_view_camera("open", Viewer("bob", ("cams",)))
    assert can_view_config(cam, "bob", ("cams",))


def test_malformed_visibility_admin_only():
    bad_vis = _obj("bad-vis", visibility="open")  # not everyone/restricted
    access = _access({"bad-vis": bad_vis})
    assert access.can_view_camera("bad-vis", Viewer("root", ("admin",)))
    assert not access.can_view_camera("bad-vis", Viewer("ann", ("cams",)))


def test_wholly_unusable_grant_field_on_restricted_is_admin_only():
    bad_roles = _obj("bad-roles", visibility="restricted")
    bad_roles.roles = "family"  # string instead of list → no grants
    access = _access({"bad-roles": bad_roles})
    assert access.can_view_camera("bad-roles", Viewer("root", ("admin",)))
    assert not access.can_view_camera("bad-roles", Viewer("ann", ("family",)))


def test_disabled_cameras_hidden():
    access = _access({
        "open": _obj("open", visibility="everyone"),
        "off": _obj("off", enabled=False, visibility="everyone"),
    })
    assert access.can_view_camera("open", Viewer("bob", ("cams",)))
    assert not access.can_view_camera("off", Viewer("bob", ("cams",)))
    assert not access.can_view_camera("off", Viewer("root", ("admin",)))
    assert not access.allowed("off", Viewer("root", ("admin",)))


def test_db_down_fresh_cache_keeps_acl(tmp_path):
    """Within max age, last-good objects still authorize non-admins (no cams.json fallback needed)."""
    registry = tmp_path / "cams.json"
    # Poisoned registry that would WIDEN access if consulted: everyone open.
    registry.write_text(json.dumps([{"id": "private", "name": "Private"}]))
    objects = {"private": _obj("private", visibility="restricted", users=("ann",))}
    access = _access(objects, max_age=60, last_ok_at=time.time() - 10, origin="cache",
                     registry_file=str(registry))
    assert access.can_view_camera("private", Viewer("ann", ("cams",)))
    assert not access.can_view_camera("private", Viewer("bob", ("cams",)))
    # Registry mode would open private to bob; db mode must not consult it.
    assert can_access(parse_registry(registry.read_text())["private"], "bob", ("cams",))


def test_db_down_stale_cache_admin_only_never_registry(tmp_path):
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps([{"id": "open", "name": "Open"},
                                    {"id": "private", "name": "Private", "users": ["ann"]}]))
    objects = {
        "open": _obj("open", visibility="everyone"),
        "private": _obj("private", visibility="restricted", users=("ann",)),
    }
    access = _access(objects, max_age=30, last_ok_at=time.time() - 120, loaded_at=time.time() - 120,
                     origin="cache", registry_file=str(registry))
    assert access.can_view_camera("open", Viewer("root", ("admin",)))
    assert access.can_view_camera("private", Viewer("root", ("admin",)))
    assert not access.can_view_camera("open", Viewer("bob", ("cams",)))
    assert not access.can_view_camera("private", Viewer("ann", ("cams",)))
    assert not access.allowed("open", Viewer("bob", ("cams",)))
    # Confirm registry itself would have allowed them (so we prove no fallback).
    cams = parse_registry(registry.read_text())
    assert can_access(cams["open"], "bob", ("cams",))
    assert can_access(cams["private"], "ann", ("cams",))


def test_file_mode_unchanged_uses_registry(tmp_path):
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps([{"id": "open", "name": "Open"},
                                    {"id": "private", "name": "Private", "users": ["ann"]}]))
    cameras = {
        "open": resolve_camera("open", {"main_stream": "open", "sub_stream": "open"}),
        "private": resolve_camera("private", {"main_stream": "private", "sub_stream": "private"}),
    }
    access = CameraAccess({"enforce": True, "registry_file": str(registry)}, cameras, use_config=False)
    # Even if config objects are present, file mode ignores them.
    access.set_config_objects({"open": _obj("open", visibility="restricted")}, last_ok_at=time.time())
    assert access.can_view_camera("open", Viewer("bob", ("cams",)))
    assert access.allowed("open", Viewer("bob", ("cams",)))
    assert not access.allowed("private", Viewer("bob", ("cams",)))
    assert access.allowed("private", Viewer("ann", ("cams",)))


def test_parity_with_registry_except_main_st_south():
    """Live Jefferson topology: cams_config ACL matches registry for every id except main-st-south.

    main-st-south is absent from cams.json (unknown → deny all) but present in cams_config as
    restricted with no grants (admin-only). That intentional difference is the exception.
    """
    detect = yaml.safe_load((FIX / "detect_cameras.yaml").read_text())
    record = yaml.safe_load((FIX / "record_cameras.yaml").read_text())
    text = (FIX / "cams.json").read_text()
    plan = build_plan(detect, record, text)
    assert plan.ok
    objects = plan.cameras
    reg = parse_registry(text)
    access = _access(objects)

    viewers = [
        Viewer("cams-user", ("cams",)),
        Viewer("admin-user", ("admin",)),
        Viewer("<redacted>", ("cams",)),
        Viewer("stranger", ("cams",)),
        Viewer("", ("admin",)),
    ]
    for who in viewers:
        for cid in sorted(set(reg) | set(objects)):
            if cid == "main-st-south":
                continue
            config_ok = access.can_view_camera(cid, who)
            if cid in reg:
                registry_ok = can_stream(reg, cid, who.user, who.roles)
            else:
                registry_ok = False
            assert config_ok is registry_ok, (
                f"{cid} for {who.user}/{who.roles}: config={config_ok} registry={registry_ok}")

    # Document the intentional exception.
    assert "main-st-south" not in reg
    assert "main-st-south" in objects
    assert objects["main-st-south"].visibility == "restricted"
    assert not objects["main-st-south"].users and not objects["main-st-south"].roles
    assert access.can_view_camera("main-st-south", Viewer("admin-user", ("admin",)))
    assert not access.can_view_camera("main-st-south", Viewer("cams-user", ("cams",)))
    assert not can_stream(reg, "main-st-south", "admin-user", ("admin",))


def test_shared_source_owners_in_config_mode():
    objects = {
        "open": _obj("open", visibility="everyone", main="shared", sub="open"),
        "private": _obj("private", visibility="restricted", users=("ann",), main="shared", sub="private"),
    }
    access = _access(objects)
    assert access.allowed("open", Viewer("ann", ("cams",)))
    assert not access.allowed("open", Viewer("bob", ("cams",)))  # private co-owner blocks
    assert access.allowed("open", Viewer("root", ("admin",)))


def test_exported_registry_matches_config_acl_for_enabled_cameras():
    """registry_entry() decisions stay aligned with can_view_config for enabled cameras."""
    objects = {
        "open": _obj("open", visibility="everyone"),
        "family": _obj("family", visibility="restricted", roles=("family",)),
        "private": _obj("private", visibility="restricted", users=("ann",)),
        "empty": _obj("empty", visibility="restricted"),
        "off": _obj("off", enabled=False, visibility="everyone"),
    }
    exported = parse_registry(json.dumps(registry({k: v for k, v in objects.items() if v.enabled})))
    for cid, cam in objects.items():
        if not cam.enabled:
            continue
        for user, roles in (("bob", ("cams",)), ("ann", ("cams",)), ("bob", ("family",)),
                            ("root", ("admin",)), ("", ("admin",))):
            assert can_view_config(cam, user, roles) is can_access(exported[cid], user, roles), (
                f"{cid} {user}/{roles}")


def test_service_wires_use_config_in_db_mode(tmp_path):
    from cams_record.config import DEFAULTS, deep_merge, validate
    from cams_record.service import Service

    secret = tmp_path / "secret"
    secret.write_text("x" * 32)
    registry = tmp_path / "cams.json"
    registry.write_text("[]")
    (tmp_path / "nvr").mkdir()
    (tmp_path / "state").mkdir()
    cfg = validate(deep_merge(DEFAULTS, {
        "camera_source": "db",
        "access": {"enforce": True, "registry_file": str(registry), "publish_registry": False},
        "recording": {"root": str(tmp_path / "nvr"), "state_dir": str(tmp_path / "state")},
        "auth": {"mode": "none", "secret_file": str(secret)},
        "push": {"enabled": False},
        "detection_protection": {"enabled": False},
        "cams_config": {"cache_file": str(tmp_path / "cache.json")},
    }))
    objects = {"open": _obj("open", visibility="everyone")}
    source = SimpleNamespace(cameras=objects, origin="db", loaded_at=time.time(),
                             last_ok_at=time.time(), revision=1)
    cams = {cid: record_camera(o) for cid, o in objects.items()}
    svc = Service(cfg, cams, db=None, source=source, file_cameras=cams)
    assert svc.access.use_config is True
    assert svc.access.can_view_camera("open", Viewer("bob", ("cams",)))
