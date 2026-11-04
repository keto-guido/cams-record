import time

import jwt
import pytest
from fastapi.testclient import TestClient

from cams_record.config import DEFAULTS, deep_merge
from cams_record.service import Service
from cams_record.web import create_app

SECRET = "s3cret-for-tests-0123456789abcdef-xyz"


@pytest.fixture
def client(tmp_path):
    sf = tmp_path / "apphub_jwt"
    sf.write_text(SECRET + "\n")
    cfg = deep_merge(DEFAULTS, {"auth": {"secret_file": str(sf)},
                                "recording": {"root": str(tmp_path), "state_dir": str(tmp_path / "state")}})
    from cams_record.config import resolve_camera

    cams = {"front-door": resolve_camera("front-door", {"main_stream": "front-door", "sub_stream": "fd-sub"})}
    svc = Service(cfg, cams, db=None)  # recorders not started
    return TestClient(create_app(svc, cfg))


def tok(roles=("admin",), exp=60, secret=SECRET):
    return jwt.encode({"sub": "eric", "roles": list(roles), "exp": int(time.time()) + exp}, secret, "HS256")


def test_healthz_no_auth(client):
    assert client.get("/cams-record/healthz").json() == {"ok": True}
    assert client.get("/healthz").status_code == 200  # prefix optional


def test_status_requires_admin_token(client):
    assert client.get("/cams-record/api/status").status_code == 401
    assert client.get("/cams-record/api/status", headers={"x-apphub-token": tok(roles=["cams"])}).status_code == 403
    assert client.get("/cams-record/api/status", headers={"x-apphub-token": tok(exp=-120)}).status_code == 401
    bad = tok(secret="another-secret-0123456789abcdef-xyz")
    assert client.get("/cams-record/api/status", headers={"x-apphub-token": bad}).status_code == 401
    # only the App Hub header is trusted (no bearer/cookie fallback)
    assert client.get("/cams-record/api/status", headers={"authorization": "Bearer " + tok()}).status_code == 401


def test_status_payload(client):
    r = client.get("/cams-record/api/status", headers={"x-apphub-token": tok()})
    assert r.status_code == 200
    st = r.json()
    assert {(s["camera_id"], s["quality"]) for s in st["streams"]} == {("front-door", "main"), ("front-door", "sub")}
    s = st["streams"][0]
    for k in ("state", "last_segment_age_s", "mb_per_hour", "restarts_24h"):
        assert k in s
    assert st["disk"]["free_gb"] > 0
    m = client.get("/api/metrics", headers={"x-apphub-token": tok()})
    assert 'cams_record_restarts_total{camera="front-door",quality="main"} 0' in m.text
