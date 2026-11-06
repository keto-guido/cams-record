import time

import jwt
import pytest
from fastapi.testclient import TestClient

from cams_record.auth import Authenticator, AuthError
from cams_record.config import DEFAULTS, deep_merge
from cams_record.service import Service
from cams_record.web import create_app

SECRET = "s3cret-for-tests-0123456789abcdef-xyz"


@pytest.fixture
def client(tmp_path):
    sf = tmp_path / "apphub_jwt"
    sf.write_text(SECRET + "\n")
    cfg = deep_merge(DEFAULTS, {"auth": {"secret_file": str(sf)},
                                "recording": {"root": str(tmp_path),
                                              "state_dir": str(tmp_path.parent / (tmp_path.name + "-state"))}})
    from cams_record.config import resolve_camera

    cams = {"front-door": resolve_camera("front-door", {"main_stream": "front-door", "sub_stream": "fd-sub"})}
    svc = Service(cfg, cams, db=None)  # recorders not started
    return TestClient(create_app(svc, cfg))


def tok(roles=("admin",), exp=60, secret=SECRET, sub="eric"):
    return jwt.encode({"aud": "cams-record", "sub": sub, "roles": list(roles), "exp": int(time.time()) + exp},
                      secret, "HS256")


def claim_tok(claims, secret=SECRET):
    """Mint a real HS256 JWT with arbitrary claims for fail-closed auth regressions."""
    body = {"aud": "cams-record", "exp": int(time.time()) + 60, **claims}
    return jwt.encode(body, secret, "HS256")


@pytest.fixture
def authenticator(tmp_path):
    secret = tmp_path / "apphub_jwt"
    secret.write_text(SECRET)
    return Authenticator({"secret_file": str(secret), "app_id": "cams-record"})


@pytest.mark.parametrize("claims", [
    {},  # missing sub
    {"sub": ""},
    {"sub": "   "},
])
def test_verify_token_rejects_blank_sub(authenticator, claims):
    token = claim_tok({**claims, "roles": ["admin"]})
    with pytest.raises(AuthError) as err:
        authenticator.verify_token(token)
    assert err.value.status == 401
    assert err.value.detail == "invalid token: missing subject"


@pytest.mark.parametrize("sub", [None, 1, True, ["eric"]])
def test_verify_token_rejects_nonstring_sub(authenticator, sub):
    # PyJWT itself refuses non-string subjects on decode; still fail closed at the boundary.
    token = claim_tok({"sub": sub, "roles": ["admin"]})
    with pytest.raises(AuthError) as err:
        authenticator.verify_token(token)
    assert err.value.status == 401
    assert err.value.detail.startswith("invalid token:")


@pytest.mark.parametrize("roles", [
    1,
    True,
    {"admin": True},
    [""],
    ["  "],
    [None],
    [1],
    ["admin", ""],
    ["admin", None],
])
def test_verify_token_rejects_invalid_roles(authenticator, roles):
    token = claim_tok({"sub": "eric", "roles": roles})
    with pytest.raises(AuthError) as err:
        authenticator.verify_token(token)
    assert err.value.status == 401
    assert err.value.detail == "invalid token: invalid roles"


def test_verify_token_accepts_valid_subject_and_roles(authenticator):
    principal = authenticator.verify_token(tok(roles=["cams", "admin"], sub="eric"))
    assert principal.sub == "eric"
    assert principal.roles == ["cams", "admin"]
    # A single role string remains supported (App Hub historically sends a list).
    principal = authenticator.verify_token(claim_tok({"sub": "ann", "roles": "cams"}), ["cams"])
    assert principal.sub == "ann" and principal.roles == ["cams"]
    with pytest.raises(AuthError) as err:
        authenticator.verify_token(claim_tok({"sub": "eric", "roles": []}))
    assert err.value.status == 403


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
    # Signed admin with an empty subject must not authenticate.
    empty_admin = claim_tok({"sub": "", "roles": ["admin"]})
    assert client.get("/cams-record/api/status", headers={"x-apphub-token": empty_admin}).status_code == 401


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
