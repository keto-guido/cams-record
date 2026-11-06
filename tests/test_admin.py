"""Camera admin API (Cameras app -> Admin tab): auth, CRUD on camera objects, optimistic locking,
read-only file mode, preserved fields, JSON-only writes, go2rtc stream list."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from conftest import CFG_DB
from fastapi.testclient import TestClient

from cams_record.admin import apply_form
from cams_record.camconfig.model import CameraObject, Stream
from cams_record.config import DEFAULTS, deep_merge, validate
from cams_record.service import Service
from cams_record.web import create_app

SECRET = "admin-api-test-secret-0123456789abcdef"


def tok(sub="eric", roles=("admin",)):
    return {"x-apphub-token": jwt.encode({"aud": "cams-record", "sub": sub, "roles": list(roles),
                                          "exp": int(time.time()) + 60}, SECRET, "HS256")}


ADMIN, VIEWER = tok(), tok("bob", ("cams",))


def front_door(**kw):
    base = dict(id="front-door", name="Front Door", visibility="everyone",
                streams={"main": Stream("main", "front-door-main", 2560, 1440, "h264"),
                         "sub": Stream("sub", "front-door")},
                use={"live": ("sub",), "live_hd": ("main",), "detect": ("sub",), "verify": ("main",),
                     "snapshot": ("sub",), "record": ("main", "sub")},
                detection={"enabled": True, "zones": {"porch": [[0, 0.5], [1, 0.5], [1, 1]]}},
                notify=[{"channel": "push", "enabled": True, "labels": ["person"], "zones": None,
                         "verified_only": False, "cooldown_s": 0, "quiet": None, "recipients": None}])
    base.update(kw)
    return CameraObject(**base)


@pytest.fixture
def client(store, tmp_path):
    def make(mode="shadow", go2rtc=None):
        secret = tmp_path / "jwt"
        secret.write_text(SECRET)
        cfg = validate(deep_merge(DEFAULTS, {
            "camera_source": mode, "auth": {"secret_file": str(secret)},
            "recording": {"root": str(tmp_path / "nvr"), "state_dir": str(tmp_path / "state")},
            "access": {"registry_file": str(tmp_path / "cams.json")},
            "go2rtc": {"api_url": go2rtc or "http://127.0.0.1:9"},
            "cams_config": {k: CFG_DB[k] for k in ("host", "port", "user", "password", "database")},
        }))
        return TestClient(create_app(Service(cfg, {}), cfg))
    return make


def form(**kw):
    base = {"id": "garage", "name": "Garage", "enabled": True, "visibility": "restricted",
            "users": [], "roles": ["family"],
            "streams": {"main": {"go2rtc_name": "garage-main"}, "sub": {"go2rtc_name": "garage"}},
            "use": {"live": ["sub"], "live_hd": ["main"], "detect": ["sub"], "verify": ["main"],
                    "snapshot": ["sub"], "record": ["main", "sub"]},
            "recording": {"main_days": 7, "sub_days": None}, "detection_enabled": True}
    base.update(kw)
    return base


A = "/cams-record/api/admin"


def test_admin_only(client, store):
    c = client()
    assert c.get(A + "/cameras").status_code == 401
    assert c.get(A + "/cameras", headers=VIEWER).status_code == 403
    assert c.post(A + "/cameras", headers=VIEWER, json={"camera": form(), "expect_revision": 0}).status_code == 403
    assert c.get(A + "/cameras", headers=ADMIN).status_code == 200


def test_create_edit_delete_with_history(client, store):
    c = client()
    r = c.post(A + "/cameras", headers=ADMIN, json={"camera": form(), "expect_revision": 0})
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == 1
    got = store.get("garage")
    assert got.roles == ("family",) and got.recording == {"main_days": 7} and got.detection == {"enabled": True}

    body = c.get(A + "/cameras", headers=ADMIN).json()
    assert body["revision"] == 1 and body["editable"] is True and body["mode"] == "shadow"
    assert [x["id"] for x in body["cameras"]] == ["garage"]

    r = c.put(A + "/cameras/garage", headers=ADMIN,
              json={"camera": {"name": "Garage Side", "visibility": "everyone"}, "expect_revision": 1})
    assert r.status_code == 200, r.text
    got = store.get("garage")
    assert got.name == "Garage Side" and got.visibility == "everyone" and got.roles == ()

    one = c.get(A + "/cameras/garage", headers=ADMIN).json()
    assert [(h["action"], h["actor"]) for h in one["history"]] == [("update", "eric"), ("create", "eric")]
    assert one["history"][0]["before"]["name"] == "Garage" and one["history"][0]["at"].endswith("Z")

    assert c.delete(A + "/cameras/garage?expect_revision=1", headers=ADMIN).status_code == 409  # stale form
    assert c.delete(A + "/cameras/garage?expect_revision=2", headers=ADMIN).json() == {"revision": 3}
    assert store.get("garage") is None
    assert c.get(A + "/cameras/garage", headers=ADMIN).status_code == 404


def test_stale_form_is_refused_not_overwritten(client, store):
    store.save(front_door(), "eric")
    c = client()
    store.save(front_door(name="Changed elsewhere"), "ann")  # revision 2
    r = c.put(A + "/cameras/front-door", headers=ADMIN, json={"camera": {"name": "Mine"}, "expect_revision": 1})
    assert r.status_code == 409 and "changed since revision 1" in r.json()["detail"]
    assert store.get("front-door").name == "Changed elsewhere"


def test_edit_keeps_fields_the_form_does_not_show(client, store):
    store.save(front_door(), "eric")
    c = client()
    r = c.put(A + "/cameras/front-door", headers=ADMIN, json={"camera": {
        "streams": {"main": {"go2rtc_name": "front-door-main"}, "sub": {"go2rtc_name": "front-door-low"}},
        "detection_enabled": False}, "expect_revision": 1})
    assert r.status_code == 200, r.text
    got = store.get("front-door")
    assert got.detection == {"enabled": False, "zones": {"porch": [[0, 0.5], [1, 0.5], [1, 1]]}}  # masks kept
    assert got.notify[0]["labels"] == ["person"]
    assert got.streams["main"].width == 2560  # same stream: probe data kept
    assert got.streams["sub"].go2rtc_name == "front-door-low" and got.streams["sub"].width is None


@pytest.mark.parametrize("bad,needle", [
    ({"id": "Bad_ID"}, "id must be"),
    ({"use": {"live": ["hd"]}}, "unknown stream"),
    ({"use": {"live": ["main", "sub"]}}, "only one stream"),
    ({"recording": {"main_days": -1}}, "must be a number > 0"),
    ({"users": "ann"}, "list of names"),
    ({"enabled": "yes"}, "true or false"),
    ({"color": "red"}, "unknown fields"),
    ({"streams": {"main": {}}}, "go2rtc_name is required"),
])
def test_validation(client, store, bad, needle):
    r = client().post(A + "/cameras", headers=ADMIN, json={"camera": form(**bad), "expect_revision": 0})
    assert r.status_code == 422 and needle in r.json()["detail"], r.text
    assert store.revision() == 0


def test_id_cannot_change_and_duplicates_refused(client, store):
    store.save(front_door(), "eric")
    c = client()
    r = c.put(A + "/cameras/front-door", headers=ADMIN, json={"camera": {"id": "porch"}, "expect_revision": 1})
    assert r.status_code == 422 and "cannot change" in r.json()["detail"]
    r = c.post(A + "/cameras", headers=ADMIN, json={"camera": form(id="front-door"), "expect_revision": 1})
    assert r.status_code == 409


def test_file_mode_is_read_only(client, store):
    store.save(front_door(), "eric")
    c = client("file")
    body = c.get(A + "/cameras", headers=ADMIN).json()
    assert body["editable"] is False and body["cameras"][0]["id"] == "front-door"
    r = c.put(A + "/cameras/front-door", headers=ADMIN, json={"camera": {"name": "X"}, "expect_revision": 1})
    assert r.status_code == 409 and "read-only" in r.json()["detail"]


def test_writes_must_be_json(client, store):
    c = client()
    r = c.post(A + "/cameras", headers={**ADMIN, "content-type": "text/plain"},
               content=json.dumps({"camera": form(), "expect_revision": 0}))
    assert r.status_code == 415
    r = c.post(A + "/cameras", headers=ADMIN, json={"camera": form()})
    assert r.status_code == 422 and "expect_revision" in r.json()["detail"]


def test_database_unavailable(store, tmp_path):
    secret = tmp_path / "jwt"
    secret.write_text(SECRET)
    cfg = validate(deep_merge(DEFAULTS, {
        "camera_source": "shadow", "auth": {"secret_file": str(secret)},
        "recording": {"root": str(tmp_path / "nvr"), "state_dir": str(tmp_path / "state")},
        "cams_config": {"host": "127.0.0.1", "port": 1, "password": "x", "connect_timeout": 1}}))
    c = TestClient(create_app(Service(cfg, {}), cfg))
    r = c.get(A + "/cameras", headers=ADMIN)
    assert r.status_code == 503 and r.json()["detail"] == "camera settings database unavailable"


def test_go2rtc_stream_list(client, store):
    store.save(front_door(), "eric")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"front-door": {}, "front-door-main": {}, "garage": {}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = client(go2rtc=f"http://127.0.0.1:{srv.server_port}").get(A + "/streams", headers=ADMIN).json()
    finally:
        srv.shutdown()
    assert body["reachable"] is True
    assert body["streams"] == [{"name": "front-door", "used_by": ["front-door"]},
                               {"name": "front-door-main", "used_by": ["front-door"]},
                               {"name": "garage", "used_by": []}]
    assert client().get(A + "/streams", headers=ADMIN).json() == {"reachable": False, "streams": []}


def test_apply_form_new_camera_without_detection():
    cam = apply_form(None, form(detection_enabled=False))
    assert cam.detection is None  # not set up for cams-detect
    assert cam.validate() == []
