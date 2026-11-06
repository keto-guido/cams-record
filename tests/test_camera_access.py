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

from cams_record.camera_access import CameraRegistry, can_access, can_stream, parse_registry
from cams_record.config import DEFAULTS, apply_env, deep_merge, resolve_camera, validate
from cams_record.web import create_app
from tests.test_clips import seg
from tests.test_web import SECRET, tok

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
    for script in ("test_camera_access.js", "test_cams_page.js"):
        done = subprocess.run([node, str(ROOT / "apphub" / script)], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stdout + done.stderr


def test_config():
    assert DEFAULTS["access"]["enforce"] is True
    cfg = apply_env(deep_merge(DEFAULTS, None), {"ACCESS_REGISTRY_FILE": "/x/cams.json", "ACCESS_ENFORCE": "0"})
    assert validate(cfg)["access"] == {"enforce": False, "registry_file": "/x/cams.json"}
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
    assert found == covered | camera_free


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


def test_revocation_applies_to_the_next_request(setup):
    _, registry, client = setup
    url = "/api/timeline?cam=private&start=0&end=60"
    assert client.get(url, headers=headers("ann")).status_code == 200
    registry.write_text(json.dumps([{"id": "private", "name": "Now someone else's", "users": ["zed"]}]))
    assert client.get(url, headers=headers("ann")).status_code == 404


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
@pytest.mark.parametrize("source,user,allowed", [("private", "bob", False), ("private", "ann", True),
                                               ("unknown", "root", False), ("shared-raw", "bob", False),
                                               ("shared-raw", "ann", True)])
def test_configured_sources_are_checked_before_database(tmp_path, quality, source, user, allowed):
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY + [
        {"id": "shared-private", "name": "Restricted alias", "snap": "shared-raw", "users": ["ann"]}]))
    secret = tmp_path / "secret"
    secret.write_text(SECRET)
    cfg = deep_merge(DEFAULTS, {"auth": {"secret_file": str(secret)},
                                "access": {"registry_file": str(registry)},
                                "recording": {"state_dir": str(tmp_path / "state")}})
    cameras = {"open": resolve_camera("open", {"main_stream": "open", quality: source})}
    db = Mock()
    db.segment_rows.return_value = []
    client = TestClient(create_app(SimpleNamespace(db=db, cameras=cameras), cfg))
    roles = ("admin",) if user == "root" else ("cams",)
    response = client.get("/api/timeline?cam=open&start=0&end=60", headers=headers(user, roles))
    assert response.status_code == (200 if allowed else 404)
    assert touched(db) is allowed


def test_missing_source_mapping_denies_even_admin(tmp_path):
    from cams_record.camera_access import CameraAccess, Viewer
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY))
    access = CameraAccess({"enforce": True, "registry_file": str(registry)}, {})
    assert not access.allowed("open", Viewer("root", ("admin",)))


@pytest.mark.parametrize("user,allowed", [("bob", False), ("ann", True)])
def test_configured_shared_source_requires_all_camera_owners(tmp_path, user, allowed):
    from cams_record.camera_access import CameraAccess, Viewer
    registry = tmp_path / "cams.json"
    registry.write_text(json.dumps(REGISTRY))
    cameras = {cid: resolve_camera(cid, {"main_stream": "open"}) for cid in ("open", "private")}
    access = CameraAccess({"enforce": True, "registry_file": str(registry)}, cameras)
    assert access.allowed("open", Viewer(user, ("cams",))) is allowed
