import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import fastapi.routing
import pytest
from fastapi.testclient import TestClient

from cams_record.camera_access import CameraAccess, CameraRegistry, Viewer, can_access, can_stream, parse_registry
from cams_record.config import DEFAULTS, apply_env, deep_merge, resolve_camera, validate
from cams_record.web import create_app
from tests.test_clips import seg
from tests.test_web import SECRET, claim_tok, tok

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / "apphub" / "camera_acl_vectors.json").read_text(encoding="utf-8"))
REGISTRY = [{"id": "open", "name": "Open"},
            {"id": "family", "name": "Family", "roles": ["family"]},
            {"id": "private", "name": "Private", "users": ["ann"]}]
CAMERA_ROUTES = ["/api/timeline?cam={c}", "/api/timeline?camera={c}", "/api/playback?cam={c}",
                 "/api/playback?camera={c}", "/api/hls/{c}/index.m3u8?start=0&end=60",
                 "/api/hls/{c}/seg/1.ts?o=0", "/api/export/info?cam={c}&start=0&end=60",
                 "/api/export?cam={c}&start=0&end=60", "/api/clip/info?camera={c}&start=0&end=60",
                 "/api/clip?camera={c}&start=0&end=60"]


def headers(user="bob", roles=("cams",)):
    return {"x-apphub-token": tok(list(roles), sub=user)}


def test_vectors_registry():
    assert list(parse_registry(json.dumps(VECTORS["registry"]))) == VECTORS["ids"]
    for text in VECTORS["broken_files"]:
        assert parse_registry(text) == {}


@pytest.mark.parametrize("case", VECTORS["access"], ids=lambda c: f"{c['user']}-{c['roles']}")
def test_vectors_access(case):
    cameras = parse_registry(json.dumps(VECTORS["registry"]))
    assert [i for i, c in cameras.items() if can_access(c, case["user"], case["roles"])] == case["allowed"]


@pytest.mark.parametrize("case", VECTORS["streams"], ids=lambda c: f"{c['name']}-{c['user']}")
def test_vectors_streams(case):
    cameras = parse_registry(json.dumps(VECTORS["registry"]))
    assert can_stream(cameras, case["name"], case["user"], case["roles"]) is case["allowed"]


def test_node_twin_passes_same_vectors():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for script in ("test_camera_access.js", "test_cams_page.js", "test_camera_streams.js", "test_camera_audio.js"):
        done = subprocess.run([node, str(ROOT / "apphub" / script)], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stdout + done.stderr


def test_config():
    assert DEFAULTS["access"]["enforce"] is True
    cfg = apply_env(deep_merge(DEFAULTS, None), {"ACCESS_REGISTRY_FILE": "/x/cams.json", "ACCESS_ENFORCE": "0"})
    assert validate(cfg)["access"] == {"enforce": False, "registry_file": "/x/cams.json", "publish_registry": False}
    with pytest.raises(ValueError, match="publish_registry needs camera_source: db"):
        validate(deep_merge(DEFAULTS, {"access": {"publish_registry": True}}))
    with pytest.raises(ValueError, match="publish_registry must be"):
        validate(deep_merge(DEFAULTS, {"access": {"publish_registry": "yes"}}))
    assert validate(deep_merge(DEFAULTS, {"camera_source": "db", "access": {"publish_registry": True}}))
    with pytest.raises(ValueError, match="camera_source must be"):
        validate(deep_merge(DEFAULTS, {"camera_source": "yaml"}))
    with pytest.raises(ValueError, match="registry_file"):
        validate(deep_merge(DEFAULTS, {"access": {"registry_file": ""}}))
    with pytest.raises(ValueError, match="enforce"):
        validate(deep_merge(DEFAULTS, {"access": {"enforce": "no"}}))


def test_registry_reloads_and_fails_closed(tmp_path):
    path = tmp_path / "cams.json"
    registry = CameraRegistry(str(path))
    assert registry.current() == {}  # missing file
    path.write_text(json.dumps(REGISTRY))
    assert set(registry.current()) == {"open", "family", "private"}
    replacement = tmp_path / "next"
    replacement.write_text(json.dumps(REGISTRY[:1]))
    os.replace(replacement, path)  # how the hub saves
    assert set(registry.current()) == {"open"}
    path.write_text("{broken")
    assert registry.current() == {}
    path.unlink()
    assert registry.current() == {}


@pytest.fixture
def setup(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text(SECRET)
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY))
    root = tmp_path / "nvr"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"segment")
    cfg = deep_merge(DEFAULTS, {"auth": {"secret_file": str(secret)}, "access": {"registry_file": str(registry)},
                                "recording": {"root": str(root), "state_dir": str(tmp_path / "state")}})
    db = Mock()
    db.segment_rows.return_value = [seg() | {"id": 1}]
    db.clip_segments.return_value = ([seg()], 60)
    db.segment_by_id.return_value = None
    cameras = {c["id"]: resolve_camera(c["id"], {}) for c in REGISTRY}
    return db, registry, TestClient(create_app(SimpleNamespace(db=db, cameras=cameras), cfg))


def touched(db):
    return db.segment_rows.called or db.clip_segments.called or db.segment_by_id.called


@pytest.mark.parametrize("route", CAMERA_ROUTES)
@pytest.mark.parametrize("cam", ["private", "family", "unregistered"])
def test_forbidden_matches_unknown_and_reads_nothing(setup, route, cam):
    db, _, client = setup
    response = client.get(route.format(c=cam), headers=headers())
    assert response.status_code == 404
    assert response.json() == {"detail": "camera not found"}
    assert not touched(db)


def mounted_paths(routes, prefix=""):
    """Every path served. A route without a path raises here rather than vanishing from the check."""
    paths = set()
    # Newer FastAPI keeps include_router as one lazy pathless route; older versions flatten it.
    for route in getattr(fastapi.routing, "iter_route_contexts", iter)(routes):
        path = prefix + route.path
        children = getattr(route, "routes", None)  # a Mount
        paths |= mounted_paths(children, path) if children else {path}
    return paths


def test_every_camera_route_is_covered(setup):
    """Fails when a new route takes a camera without being added to CAMERA_ROUTES."""
    _, _, client = setup
    found = mounted_paths(client.app.app.routes)
    camera_free = {"/cams-record/healthz", "/cams-record/api/status", "/cams-record/api/metrics",
                   "/cams-record/api/push/vapid-public-key", "/cams-record/api/push/status",
                   "/cams-record/api/push/subscribe", "/cams-record/api/push/test",
                   "/cams-record/api/push/event-context/{event_id}", "/cams-record/api/internal/detection-event"}
    covered = {"/cams-record" + u.split("?")[0].replace("{c}", "{cam}").replace("seg/1.ts", "seg/{segment_id}.ts")
               for u in CAMERA_ROUTES}
    assert found == covered | camera_free | set(ADMIN_ONLY)


# Camera management (Cameras app -> Admin tab) manages every camera, so it is admin-role only rather
# than per camera: each method must refuse a non-admin before reading anything.
ADMIN_ONLY = {
    "/cams-record/api/admin/cameras": ("GET", "POST"),
    "/cams-record/api/admin/cameras/{cam_id}": ("GET", "PUT", "DELETE"),
    "/cams-record/api/admin/streams": ("GET",),
}


@pytest.mark.parametrize("roles", [["cams"], ["cams", "family"]])
def test_admin_routes_refuse_non_admins(setup, roles):
    db, _, client = setup
    for path, methods in ADMIN_ONLY.items():
        url = path.replace("/cams-record", "").replace("{cam_id}", "open") + "?expect_revision=0"
        for method in methods:
            r = client.request(method, url, headers=headers("ann", roles), json={"camera": {}, "expect_revision": 0})
            assert r.status_code == 403, (method, path, r.status_code)
    assert not touched(db)


@pytest.mark.parametrize("path", ["clip", "clip/info"])
def test_cam_alias_cannot_smuggle_a_camera(setup, path):
    db, _, client = setup
    url = f"/api/{path}?cam=open&camera=private&start=0&end=60"
    assert client.get(url, headers=headers()).status_code == 404
    assert not touched(db)


@pytest.mark.parametrize("path", ["timeline", "playback"])
def test_alias_serves_the_authorized_camera(setup, path):
    db, _, client = setup
    assert client.get(f"/api/{path}?cam=open&camera=private&start=0&end=60", headers=headers()).status_code == 200
    assert {call.args[0] for call in db.segment_rows.call_args_list} == {"open"}


@pytest.mark.parametrize("cam,user,roles,status", [
    ("open", "bob", ["cams"], 200), ("private", "ann", ["cams"], 200), ("family", "kim", ["cams", "family"], 200),
    ("private", "root", ["admin"], 200), ("unregistered", "root", ["admin"], 404),
    ("open", "bob", ["viewer"], 403)])
def test_grants(setup, cam, user, roles, status):
    _, _, client = setup
    assert client.get(f"/api/timeline?cam={cam}&start=0&end=60", headers=headers(user, roles)).status_code == status


def test_foreign_segment_id_is_refused(setup, monkeypatch):
    db, _, client = setup
    db.segment_by_id.return_value = seg() | {"id": 1, "camera_id": "private", "quality": "sub"}
    assert client.get("/api/hls/open/seg/1.ts?o=0", headers=headers()).status_code == 404
    # Keep the actual DB ownership check and media path resolution; stub only ffmpeg.
    db.segment_by_id.return_value = seg() | {"id": 1, "camera_id": "open", "quality": "sub"}

    def remux(cmd, timeout):
        Path(cmd[-1]).write_bytes(b"transport stream")
        return True
    monkeypatch.setattr(client.app.app.state.media, "run", remux)
    response = client.get("/api/hls/open/seg/1.ts?o=0", headers=headers())
    assert response.status_code == 200
    assert response.content == b"transport stream"


def test_identity_comes_from_the_jwt_not_headers(setup):
    _, _, client = setup
    url = "/api/timeline?cam=private&start=0&end=60"
    assert client.get(url, headers=headers() | {"x-apphub-user": "ann"}).status_code == 404  # mismatch
    assert client.get(url, headers=headers("ann") | {"x-apphub-user": "bob"}).status_code == 404  # mismatch
    assert client.get(url, headers=headers("ann") | {"x-apphub-user": "ann"}).status_code == 200
    assert client.get(url, headers=headers() | {"x-apphub-roles": "admin,family"}).status_code == 404
    family = url.replace("private", "family")
    assert client.get(family, headers=headers() | {"x-apphub-roles": "family"}).status_code == 404


@pytest.mark.parametrize("claims", [
    {"roles": ["admin"]},  # missing sub
    {"sub": "", "roles": ["admin"]},
    {"sub": "   ", "roles": ["admin"]},
    {"sub": None, "roles": ["admin"]},
    {"sub": 0, "roles": ["admin"]},
    {"sub": "root", "roles": [""]},
    {"sub": "root", "roles": {"admin": True}},
])
@pytest.mark.parametrize("route", [
    "/api/timeline?cam=open&start=0&end=60",
    "/api/timeline?cam=private&start=0&end=60",
    "/api/clip/info?camera=open&start=0&end=60",
    "/api/export/info?cam=open&start=0&end=60",
    "/api/push/event-context/event-123",
    "/api/push/status",
    "/api/status",
])
def test_empty_or_invalid_jwt_principal_is_rejected(setup, claims, route):
    """Correctly signed tokens with blank/non-string sub or bad roles fail closed at auth."""
    db, _, client = setup
    db.detection_event_by_id.return_value = {
        "id": "event-123", "camera_id": "private", "label": "person", "score": 0.9,
        "start_ts": 10, "end_ts": 20,
    }
    response = client.get(route, headers={"x-apphub-token": claim_tok(claims)})
    assert response.status_code == 401
    assert not touched(db)
    assert db.detection_event_by_id.called is False


def test_empty_sub_admin_cannot_expose_registered_cameras(setup):
    """Regression: verify_token used to coerce missing/empty sub to '?', bypassing empty-user ACL."""
    db, _, client = setup
    # A real non-blank admin still reaches open and private cameras.
    assert client.get("/api/timeline?cam=open&start=0&end=60",
                      headers=headers("root", ["admin"])).status_code == 200
    assert client.get("/api/timeline?cam=private&start=0&end=60",
                      headers=headers("root", ["admin"])).status_code == 200
    db.reset_mock()
    for claims in ({"sub": "", "roles": ["admin"]}, {"sub": "  \t", "roles": ["admin"]},
                   {"sub": None, "roles": ["admin"]}, {"roles": ["admin"]},
                   {"sub": 123, "roles": ["admin"]}):
        token = claim_tok(claims)
        for cam in ("open", "private", "family"):
            response = client.get(f"/api/timeline?cam={cam}&start=0&end=60",
                                  headers={"x-apphub-token": token})
            assert response.status_code == 401, (claims, cam, response.status_code)
            # Same for clip/export metadata and push event deep-links.
            assert client.get(f"/api/clip/info?camera={cam}&start=0&end=60",
                              headers={"x-apphub-token": token}).status_code == 401
            assert client.get(f"/api/export/info?cam={cam}&start=0&end=60",
                              headers={"x-apphub-token": token}).status_code == 401
        assert client.get("/api/push/event-context/event-123",
                          headers={"x-apphub-token": token}).status_code == 401
        assert client.get("/api/push/status", headers={"x-apphub-token": token}).status_code == 401
        assert client.get("/api/status", headers={"x-apphub-token": token}).status_code == 401
        assert not touched(db)
        assert db.detection_event_by_id.called is False


def test_revocation_applies_to_the_next_request(setup):
    _, registry, client = setup
    url = "/api/timeline?cam=private&start=0&end=60"
    assert client.get(url, headers=headers("ann")).status_code == 200
    registry.write_text(json.dumps([{"id": "private", "name": "Now someone else's", "users": ["zed"]}]))
    assert client.get(url, headers=headers("ann")).status_code == 404


def test_cached_clip_ranges_reauthorize_every_request(setup, monkeypatch):
    db, registry, client = setup

    def run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"0123456789")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("cams_record.clips.subprocess.run", run)
    url = "/api/clip?camera=private&start=10&end=40"
    first = client.get(url, headers=headers("ann") | {"Range": "bytes=0-1"})
    assert first.status_code == 206
    continuation = {"Range": "bytes=2-", "If-Range": first.headers["etag"]}
    assert client.get(url, headers=headers("ann") | continuation).content == b"23456789"
    db.reset_mock()
    assert client.get(url, headers=continuation).status_code == 401
    assert client.get(url, headers=headers("ann", ["viewer"]) | continuation).status_code == 403
    assert client.get(url, headers=headers("ann") | continuation |
                      {"x-apphub-token": tok(["cams"], exp=-120, sub="ann")}).status_code == 401
    assert client.get(url, headers=headers("bob") | continuation).status_code == 404
    assert not touched(db)
    registry.write_text(json.dumps([{"id": "private", "name": "Revoked", "users": ["zed"]}]))
    assert client.get(url, headers=headers("ann") | continuation).status_code == 404
    assert not touched(db)


def test_push_event_context_checks_camera_and_revocation(setup):
    db, registry, client = setup
    db.detection_event_by_id.return_value = {
        "id": "event-123", "camera_id": "private", "label": "person", "score": 0.9,
        "start_ts": 10, "end_ts": 20,
    }
    url = "/api/push/event-context/event-123"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers("ann", ["viewer"])).status_code == 403
    response = client.get(url, headers=headers("ann"))
    assert response.status_code == 200
    assert response.json()["camera"] == "private"
    assert client.get(url, headers=headers("root", ["admin"])).status_code == 200
    denied = client.get(url, headers=headers("bob"))
    assert denied.status_code == 404
    assert denied.json() == {"detail": "event not found"}
    assert client.get(url, headers=headers("ann") | {"x-apphub-user": "bob"}).status_code == 404
    registry.write_text(json.dumps([{"id": "private", "name": "Revoked", "users": ["zed"]}]))
    assert client.get(url, headers=headers("ann")).status_code == 404
    db.detection_event_by_id.return_value = None
    missing = client.get(url, headers=headers("bob"))
    assert missing.status_code == denied.status_code and missing.json() == denied.json()


def test_missing_registry_denies_even_admin(setup):
    _, registry, client = setup
    registry.unlink()
    assert client.get("/api/timeline?cam=open&start=0&end=60", headers=headers("root", ["admin"])).status_code == 404


def test_bad_id_is_rejected_before_lookup(setup):
    _, _, client = setup
    assert client.get("/api/timeline?cam=../etc&start=0&end=60", headers=headers()).status_code == 400


@pytest.mark.parametrize("name", ["ACCESS_ENFORCE", "PUSH_ENABLED", "PUSH_POLL_ENABLED",
                                  "DETECTION_PROTECTION_ENABLED"])
@pytest.mark.parametrize("value", ["treu", "false-ish", "2", " "])
def test_invalid_boolean_env_fails(name, value):
    with pytest.raises(ValueError, match=name):
        apply_env(deep_merge(DEFAULTS, None), {name: value})


@pytest.mark.parametrize("quality", ["main_stream", "sub_stream"])
@pytest.mark.parametrize("source,user,allowed", [
    # Explicit registered id/snap sources keep exact can_stream(src) (not weakened).
    ("private", "bob", False), ("private", "ann", True),
    ("shared-raw", "bob", False), ("shared-raw", "ann", True),
])
def test_explicit_registered_sources_keep_can_stream(tmp_path, quality, source, user, allowed):
    """When a configured source is a registry id or snap, can_stream(src) still applies."""
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY + [
        {"id": "shared-private", "name": "Restricted alias", "snap": "shared-raw", "users": ["ann"]}]))
    secret = tmp_path / "secret"
    secret.write_text(SECRET)
    cfg = deep_merge(DEFAULTS, {"auth": {"secret_file": str(secret)},
                                "access": {"registry_file": str(registry)},
                                "recording": {"state_dir": str(tmp_path / "state")}})
    # Keep main_stream=open so open remains an explicit registered source alongside `source`
    # when quality is sub_stream; when quality is main_stream, `source` replaces main.
    cameras = {"open": resolve_camera("open", {"main_stream": "open", quality: source})}
    db = Mock()
    db.segment_rows.return_value = []
    client = TestClient(create_app(SimpleNamespace(db=db, cameras=cameras), cfg))
    roles = ("admin",) if user == "root" else ("cams",)
    response = client.get("/api/timeline?cam=open&start=0&end=60", headers=headers(user, roles))
    assert response.status_code == (200 if allowed else 404)
    assert touched(db) is allowed


def test_unregistered_name_without_config_owner_cannot_be_streamed(tmp_path):
    """can_stream itself still denies unknown names; allowed() never invents go2rtc aliases."""
    cameras = parse_registry(json.dumps(REGISTRY))
    assert can_stream(cameras, "unknown", "root", ("admin",)) is False
    assert can_stream(cameras, "private-main", "ann", ("cams",)) is False
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY))
    access = CameraAccess(
        {"enforce": True, "registry_file": str(registry)},
        {"open": resolve_camera("open", {"main_stream": "open", "sub_stream": "open"})},
    )
    assert not access.allowed("unknown", Viewer("root", ("admin",)))
    assert not access.allowed("private-main", Viewer("root", ("admin",)))


def test_missing_source_mapping_denies_even_admin(tmp_path):
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY))
    access = CameraAccess({"enforce": True, "registry_file": str(registry)}, {})
    assert not access.allowed("open", Viewer("root", ("admin",)))


@pytest.mark.parametrize("user,allowed", [("bob", False), ("ann", True)])
def test_configured_shared_source_requires_all_camera_owners(tmp_path, user, allowed):
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY))
    cameras = {cid: resolve_camera(cid, {"main_stream": "open"}) for cid in ("open", "private")}
    access = CameraAccess({"enforce": True, "registry_file": str(registry)}, cameras)
    assert access.allowed("open", Viewer(user, ("cams",))) is allowed


@pytest.mark.parametrize("user,roles,parent_ok", [
    ("ann", ("cams",), True),
    ("bob", ("cams",), False),
    ("root", ("admin",), True),
    ("", ("admin",), False),
])
def test_unregistered_config_alias_inherits_parent_source_owners(tmp_path, user, roles, parent_ok):
    """Config alias not registered as id/snap inherits; no -main stripping; main!=sub!=id."""
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps([
        {"id": "private", "name": "Private", "users": ["ann"]},
        {"id": "open", "name": "Open"},
        {"id": "main-street", "name": "Main Street", "users": ["ann"]},
        {"id": "dock-main", "name": "Dock low-res", "users": ["ann"]},
    ]))  # no helper rows
    cameras = {
        "private": resolve_camera("private", {"main_stream": "private-main", "sub_stream": "private"}),
        "open": resolve_camera("open", {"main_stream": "open-hires", "sub_stream": "open"}),
        # Live-shaped mismatch: neither stream equals the logical id or <id>-main.
        "main-street": resolve_camera("main-street", {
            "main_stream": "main-st-south-main", "sub_stream": "main-st-south",
        }),
        # Logical id itself ends in -main; HD alias is unrelated.
        "dock-main": resolve_camera("dock-main", {
            "main_stream": "dock-main-hires", "sub_stream": "dock-main",
        }),
    }
    access = CameraAccess({"enforce": True, "registry_file": str(registry)}, cameras)
    assert access.allowed("private", Viewer(user, roles)) is parent_ok
    assert access.allowed("open", Viewer(user, roles)) is bool(user)
    assert access.allowed("main-street", Viewer(user, roles)) is parent_ok
    assert access.allowed("dock-main", Viewer(user, roles)) is parent_ok
    # Defaults main==id would hide this: empty config is not the live shape.
    assert resolve_camera("main-street", {}).main_stream == "main-street"
    assert cameras["main-street"].main_stream != cameras["main-street"].id
    assert cameras["main-street"].sub_stream != cameras["main-street"].id
    assert cameras["main-street"].main_stream != cameras["main-street"].sub_stream


def test_explicit_registered_helper_still_requires_can_stream_src(tmp_path):
    """Leftover helper registry rows remain exact can_stream(src); source_owners still apply."""
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY + [
        {"id": "private-main", "name": "Stale helper", "users": ["zed"]}]))
    cameras = {"private": resolve_camera("private", {"main_stream": "private-main", "sub_stream": "private"})}
    access = CameraAccess({"enforce": True, "registry_file": str(registry)}, cameras)
    assert not access.allowed("private", Viewer("ann", ("cams",)))  # helper blocks
    assert not access.allowed("private", Viewer("zed", ("cams",)))  # parent blocks
    assert access.allowed("private", Viewer("root", ("admin",)))


def test_unregistered_helper_camera_id_does_not_inherit(tmp_path):
    """cam=<helper> without a registry row is denied; no App Hub stream alias is created."""
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY + [
        {"id": "main-street", "name": "Main Street", "users": ["ann"]}]))
    cameras = {
        "private": resolve_camera("private", {"main_stream": "private-main", "sub_stream": "private"}),
        "main-street": resolve_camera("main-street", {
            "main_stream": "main-st-south-main", "sub_stream": "main-st-south",
        }),
    }
    access = CameraAccess({"enforce": True, "registry_file": str(registry)}, cameras)
    assert access.allowed("private", Viewer("ann", ("cams",)))
    assert access.allowed("main-street", Viewer("ann", ("cams",)))
    for helper in ("private-main", "main-st-south", "main-st-south-main"):
        assert not access.allowed(helper, Viewer("ann", ("cams",)))
        assert not access.allowed(helper, Viewer("root", ("admin",)))


def test_hd_clip_export_parent_only_without_helper_registry(tmp_path, monkeypatch):
    """HD /api/clip|export on the parent inherits; unregistered helpers stay 404."""
    from cams_record.media import Media

    secret = tmp_path / "secret"
    secret.write_text(SECRET)
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps([
        {"id": "private", "name": "Private", "users": ["ann"]},
        {"id": "main-street", "name": "Main Street", "users": ["ann"]},
        {"id": "dock-main", "name": "Dock", "users": ["ann"]},
    ]))
    root = tmp_path / "nvr"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"segment")
    cfg = deep_merge(DEFAULTS, {"auth": {"secret_file": str(secret)}, "access": {"registry_file": str(registry)},
                                "recording": {"root": str(root), "state_dir": str(tmp_path / "state")}})
    cameras = {
        "private": resolve_camera("private", {"main_stream": "private-main", "sub_stream": "private"}),
        "main-street": resolve_camera("main-street", {
            "main_stream": "main-st-south-main", "sub_stream": "main-st-south",
        }),
        "dock-main": resolve_camera("dock-main", {
            "main_stream": "dock-main-hires", "sub_stream": "dock-main",
        }),
    }
    db = Mock()
    db.segment_rows.return_value = [seg() | {"id": 1, "camera_id": "private", "quality": "sub"}]
    db.clip_segments.return_value = ([seg() | {"camera_id": "private", "quality": "main"}], 60)
    db.segment_by_id.return_value = None
    client = TestClient(create_app(SimpleNamespace(db=db, cameras=cameras), cfg))

    def clip_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"0123456789")
        return SimpleNamespace(returncode=0)

    def media_run(self, command, timeout):
        Path(command[-1]).write_bytes(b"export-bytes")
        return True

    monkeypatch.setattr("cams_record.clips.subprocess.run", clip_run)
    monkeypatch.setattr(Media, "run", media_run)

    assert client.get("/api/timeline?cam=private&start=0&end=60", headers=headers("ann")).status_code == 200
    assert client.get("/api/timeline?cam=main-street&start=0&end=60", headers=headers("ann")).status_code == 200
    assert client.get("/api/timeline?cam=dock-main&start=0&end=60", headers=headers("ann")).status_code == 200
    export = client.get("/api/export?cam=private&start=10&end=40&quality=main", headers=headers("ann"))
    assert export.status_code == 200 and export.headers.get("x-export-quality") == "main"
    assert client.get("/api/clip?camera=private&start=10&end=40&quality=main",
                      headers=headers("ann")).status_code == 200
    db.clip_segments.return_value = ([seg() | {"camera_id": "main-street", "quality": "main"}], 60)
    assert client.get("/api/export?cam=main-street&start=10&end=40&quality=main",
                      headers=headers("ann")).status_code == 200
    assert client.get("/api/export?cam=private&start=10&end=40&quality=main", headers=headers("bob")).status_code == 404
    for helper in ("private-main", "main-st-south", "main-st-south-main", "dock-main-hires"):
        assert client.get(f"/api/export?cam={helper}&start=10&end=40&quality=main",
                          headers=headers("ann")).status_code == 404
        assert client.get(f"/api/export?cam={helper}&start=10&end=40&quality=main",
                          headers=headers("root", ["admin"])).status_code == 404
