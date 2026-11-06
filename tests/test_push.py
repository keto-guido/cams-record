"""Web Push: auth/ownership, payload/deep-link, failure pruning, detection webhook,
real cams_detect.events schema, and VAPID PEM → pywebpush key loading.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from py_vapid import Vapid

from cams_record.config import DEFAULTS, deep_merge, resolve_camera
from cams_record.push import (
    DetectionEvent,
    PushError,
    deep_link_for,
    endpoint_hash,
    load_vapid_from_pem,
    normalize_event_id,
    notification_payload,
    validate_subscription,
)
from cams_record.service import Service
from cams_record.web import create_app

SECRET = "s3cret-for-tests-0123456789abcdef-xyz"
WEBHOOK = "webhook-secret-for-tests-abcdefghijklmnopqrstuvwxyz"
# Production-shaped event id (string, not int).
PROD_EVENT_ID = "1793897909-7de0b7"


def _vapid_pem(tmp_path: Path) -> tuple[str, str, Path]:
    vapid = Vapid()
    vapid.generate_keys()
    path = tmp_path / "vapid_private_key"
    path.write_bytes(vapid.private_pem())
    pem = path.read_text(encoding="utf-8")
    from cams_record.push import load_vapid_public_b64

    return pem, load_vapid_public_b64(pem), path


def tok(sub="eric", roles=("cams",), exp=60, secret=SECRET):
    return jwt.encode(
        {"aud": "cams-record", "sub": sub, "roles": list(roles), "exp": int(time.time()) + exp},
        secret, "HS256",
    )


def _live_subscription_keys():
    """Generate a valid Web Push receiver keypair (needed for real http_ece encrypt)."""
    recv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    p256dh = base64.urlsafe_b64encode(
        recv.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
    ).decode("ascii").rstrip("=")
    auth = base64.urlsafe_b64encode(os.urandom(16)).decode("ascii").rstrip("=")
    return p256dh, auth


def _sub_body(endpoint="https://fcm.googleapis.com/fcm/send/testdevice", *, live_keys=False):
    if live_keys:
        p256dh, auth = _live_subscription_keys()
    else:
        p256dh = ("BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpDtUbVlDsiZv3h7FcOxT_YvCqd0_5e1O0tkzZV8ikhYQqE")
        auth = "tBHItJI5svbpez7KI4CCXg"
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


# Production cams_detect.events shape (string id, max_score — not score).
PROD_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS cams_detect.events (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    camera_id VARCHAR(64) NOT NULL,
    label VARCHAR(64) NOT NULL,
    max_score DOUBLE NULL,
    start_ts DATETIME(3) NOT NULL,
    end_ts DATETIME(3) NULL,
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


@pytest.fixture
def push_env(tmp_path):
    pem, pub, path = _vapid_pem(tmp_path)
    jwt_file = tmp_path / "apphub_jwt"
    jwt_file.write_text(SECRET + "\n")
    hook_file = tmp_path / "push_webhook_secret"
    hook_file.write_text(WEBHOOK)
    cfg = deep_merge(DEFAULTS, {
        "auth": {"secret_file": str(jwt_file)},
        "recording": {"root": str(tmp_path / "nvr"), "state_dir": str(tmp_path / "state")},
        "push": {
            "enabled": True,
            "vapid_private_file": str(path),
            "vapid_public_key": pub,
            "vapid_subject": "mailto:test@example.com",
            "webhook_secret_file": str(hook_file),
            "poll_enabled": False,
        },
    })
    cams = {"front-door": resolve_camera("front-door", {"main_stream": "front-door", "sub_stream": "fd-sub"})}
    return cfg, cams, pub, path, pem


@pytest.fixture
def client_nodb(push_env):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=None)
    return TestClient(create_app(svc, cfg)), cfg


@pytest.fixture
def cams_detect_events(db):
    """Create the real production-shaped cams_detect.events table (not a mock)."""
    def setup(cur):
        cur.execute("CREATE DATABASE IF NOT EXISTS cams_detect "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cur.execute("DROP TABLE IF EXISTS cams_detect.events")
        cur.execute(PROD_EVENTS_DDL)

    db.run(setup)
    yield db

    def teardown(cur):
        cur.execute("DROP TABLE IF EXISTS cams_detect.events")

    try:
        db.run(teardown)
    except Exception:  # noqa: BLE001
        pass


def test_validate_subscription_rejects_bad_hosts():
    with pytest.raises(PushError):
        validate_subscription({"endpoint": "http://evil.example/push", "keys": {"p256dh": "x", "auth": "y"}})
    with pytest.raises(PushError):
        validate_subscription({"endpoint": "https://evil.example/push", "keys": {"p256dh": "x", "auth": "y"}})
    ok = validate_subscription(_sub_body())
    assert ok["endpoint_hash"] == endpoint_hash(ok["endpoint"])


def test_normalize_event_id_preserves_production_strings():
    assert normalize_event_id(PROD_EVENT_ID) == PROD_EVENT_ID
    assert normalize_event_id("42") == "42"
    with pytest.raises(PushError):
        normalize_event_id("")
    with pytest.raises(PushError):
        normalize_event_id("../evil")
    with pytest.raises(PushError):
        normalize_event_id(None)


def test_notification_payload_deep_link_string_id_and_no_image():
    event = DetectionEvent(id=PROD_EVENT_ID, camera_id="front-door", label="person", score=0.9,
                           start_ts=1_700_000_000.0, end_ts=None, camera_name="Front Door")
    payload = notification_payload(event)
    assert payload["url"] == f"/cams/#event={PROD_EVENT_ID}"
    assert deep_link_for(event) == payload["url"]
    assert payload["event_id"] == PROD_EVENT_ID
    assert payload["tag"] == f"cams-event-{PROD_EVENT_ID}"
    assert "person" in payload["title"].lower() or "Person" in payload["title"]
    assert "image" not in payload
    assert "snapshot" not in payload["body"].lower()
    blob = str(payload)
    assert ".jpg" not in blob and "base64" not in blob


def test_push_routes_require_auth(client_nodb):
    client, _ = client_nodb
    assert client.get("/cams-record/api/push/status").status_code == 401
    assert client.get("/cams-record/api/push/vapid-public-key").status_code == 401
    assert client.post("/cams-record/api/push/subscribe", json=_sub_body()).status_code == 401
    assert client.post("/cams-record/api/push/test").status_code == 401
    assert client.post("/cams-record/api/internal/detection-event", json={}).status_code == 401


def test_vapid_public_key_for_cams_role(client_nodb):
    client, _ = client_nodb
    headers = {"x-apphub-token": tok(roles=["cams"])}
    r = client.get("/cams-record/api/push/vapid-public-key", headers=headers)
    assert r.status_code == 200
    assert r.json()["publicKey"]
    assert "PRIVATE" not in r.text
    assert "private" not in r.json()


def test_vapid_startup_loads_pem_via_from_pem(push_env):
    """Startup path must load the PEM secret with from_pem (not DER from_string)."""
    cfg, cams, pub, path, pem = push_env
    assert "BEGIN PRIVATE KEY" in pem
    with pytest.raises(ValueError, match="deserialize"):
        Vapid.from_string(pem)  # the bug pywebpush hits when given PEM text
    loaded = load_vapid_from_pem(pem)
    assert loaded is not None
    svc = Service(cfg, cams, db=None)
    assert svc.push.enabled is True
    assert svc.push.vapid() is not None
    assert svc.push.public_key() == pub


def test_vapid_send_deserializes_pem_and_signs(push_env, monkeypatch):
    """Send-test path: real PEM → Vapid → webpush encrypt/sign; only HTTP is mocked."""
    cfg, cams, _pub, path, pem = push_env
    svc = Service(cfg, cams, db=None)
    body = validate_subscription(_sub_body(live_keys=True))
    posts = []

    def fake_post(url, data=None, headers=None, timeout=None):
        posts.append({"url": url, "headers": dict(headers or {}), "data": data})
        resp = MagicMock()
        resp.status_code = 201
        resp.content = b""
        resp.text = ""
        resp.headers = {}
        return resp

    # Patch the Session/post boundary used by pywebpush, not key loading.
    with patch("pywebpush.requests.post", side_effect=fake_post):
        ok, status = svc.push.send_raw(body, {"title": "t", "body": "b", "url": "/cams/"})
    assert ok is True and status == 201
    assert len(posts) == 1
    assert posts[0]["url"].startswith("https://fcm.googleapis.com/")
    auth = posts[0]["headers"].get("Authorization") or posts[0]["headers"].get("authorization")
    assert auth and "vapid" in auth.lower()
    # Prove we did not pass raw PEM text into from_string: signing required a live Vapid object.
    assert svc.push.vapid() is not None
    assert Path(path).read_text(encoding="utf-8").strip().startswith("-----BEGIN")


def test_vapid_send_via_test_endpoint(db, push_env):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    sub = validate_subscription(_sub_body("https://fcm.googleapis.com/fcm/send/eric", live_keys=True))
    db.upsert_push_subscription("eric", sub)
    posts = []

    def fake_post(url, data=None, headers=None, timeout=None):
        posts.append(url)
        resp = MagicMock(status_code=201, content=b"", text="", headers={})
        return resp

    with patch("pywebpush.requests.post", side_effect=fake_post):
        r = client.post("/cams-record/api/push/test", headers={"x-apphub-token": tok(sub="eric")})
    assert r.status_code == 200, r.text
    assert r.json()["sent"] == 1
    assert posts and posts[0].startswith("https://fcm.googleapis.com/")


def test_subscribe_ownership_and_unsubscribe(db, push_env):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    eric = {"x-apphub-token": tok(sub="eric", roles=["cams"])}
    other = {"x-apphub-token": tok(sub="other", roles=["cams"])}
    body = _sub_body("https://fcm.googleapis.com/fcm/send/eric-phone")

    assert client.post("/cams-record/api/push/subscribe", headers=eric, json=body).status_code == 200
    st = client.get("/cams-record/api/push/status", headers=eric).json()
    assert st["subscribed"] is True and st["subscription_count"] == 1
    st2 = client.get("/cams-record/api/push/status", headers=other).json()
    assert st2["subscription_count"] == 0
    gone = client.request("DELETE", "/cams-record/api/push/subscribe", headers=other,
                          json={"endpoint": body["endpoint"]}).json()
    assert gone["removed"] == 0
    assert client.get("/cams-record/api/push/status", headers=eric).json()["subscription_count"] == 1
    assert client.request("DELETE", "/cams-record/api/push/subscribe", headers=eric,
                          json={"endpoint": body["endpoint"]}).json()["removed"] == 1
    assert client.get("/cams-record/api/push/status", headers=eric).json()["subscription_count"] == 0


def test_failure_pruning_dead_endpoint(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    push = svc.push
    body = validate_subscription(_sub_body("https://updates.push.services.mozilla.com/wpush/v2/dead"))
    db.upsert_push_subscription("eric", body)
    assert len(db.list_push_subscriptions()) == 1

    from pywebpush import WebPushException

    def boom(**_kwargs):
        exc = WebPushException("gone")
        exc.response = MagicMock(status_code=410)
        raise exc

    monkeypatch.setattr("pywebpush.webpush", boom)
    ok, status = push.send_raw(body, {"title": "t", "body": "b", "url": "/cams/"})
    assert ok is False and status == 410
    assert db.list_push_subscriptions() == []


def test_detection_webhook_dedupes_string_event_ids(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    sent = []

    def capture(subscription, payload):
        sent.append(payload)
        return True, 201

    monkeypatch.setattr(svc.push, "send_raw", capture)
    sub = validate_subscription(_sub_body())
    db.upsert_push_subscription("eric", sub)

    event = {
        "id": PROD_EVENT_ID,
        "camera_id": "front-door",
        "camera_name": "Front Door",
        "label": "person",
        "max_score": 0.88,
        "start_ts": 1_700_000_100,
    }
    headers = {"x-cams-push-secret": WEBHOOK}
    r1 = client.post("/cams-record/api/internal/detection-event", headers=headers, json=event)
    assert r1.status_code == 200
    assert r1.json()["sent"] == 1
    assert r1.json().get("deduped") is not True
    assert sent[0]["url"] == f"/cams/#event={PROD_EVENT_ID}"
    assert sent[0]["event_id"] == PROD_EVENT_ID
    assert "image" not in sent[0]

    r2 = client.post("/cams-record/api/internal/detection-event", headers=headers, json=event)
    assert r2.status_code == 200
    assert r2.json().get("deduped") is True
    assert r2.json()["sent"] == 0
    assert len(sent) == 1


def test_push_test_only_own_subscriptions(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    sent_users = []

    def capture(subscription, payload):
        sent_users.append(subscription["user_sub"])
        return True, 201

    monkeypatch.setattr(svc.push, "send_raw", capture)
    db.upsert_push_subscription("eric", validate_subscription(_sub_body("https://fcm.googleapis.com/fcm/send/a")))
    db.upsert_push_subscription("other", validate_subscription(_sub_body("https://fcm.googleapis.com/fcm/send/b")))
    r = client.post("/cams-record/api/push/test", headers={"x-apphub-token": tok(sub="eric")})
    assert r.status_code == 200
    assert sent_users == ["eric"]
    assert r.json()["payload_url"] == "/cams/#detections"


def test_event_context_string_id(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    monkeypatch.setattr(db, "detection_event_by_id", lambda event_id: {
        "id": event_id, "camera_id": "front-door", "label": "car", "score": 0.5,
        "start_ts": 10.0, "end_ts": 12.0,
    })
    assert client.get(f"/cams-record/api/push/event-context/{PROD_EVENT_ID}").status_code == 401
    r = client.get(f"/cams-record/api/push/event-context/{PROD_EVENT_ID}",
                   headers={"x-apphub-token": tok(roles=["cams"])})
    assert r.status_code == 200
    assert r.json()["id"] == PROD_EVENT_ID
    assert r.json()["url"] == f"/cams/#event={PROD_EVENT_ID}"
    assert "snapshot" not in r.json()


def test_webhook_rejects_jwt_as_public_send(client_nodb):
    client, _ = client_nodb
    r = client.post(
        "/cams-record/api/internal/detection-event",
        headers={"x-apphub-token": tok(roles=["admin"])},
        json={"id": PROD_EVENT_ID, "camera_id": "front-door", "start_ts": 1},
    )
    assert r.status_code == 401


def test_production_events_schema_max_score_and_string_ids(cams_detect_events, push_env, monkeypatch):
    """Query the real production events shape (string id + max_score), not a mocked SELECT."""
    db = cams_detect_events
    cfg, cams, *_ = push_env
    # Confirm schema v6 widened event_id (forward migration from BIGINT).
    col = db.run(lambda cur: (cur.execute(
        "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='push_sent_events' AND COLUMN_NAME='event_id'"),
        cur.fetchone())[1])
    assert col[0].lower() in ("varchar", "char")
    assert int(col[1] or 0) >= 32

    def insert(cur):
        cur.execute(
            """INSERT INTO cams_detect.events (id, camera_id, label, max_score, start_ts, end_ts, updated_at)
               VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(3), NULL, UTC_TIMESTAMP(3))""",
            (PROD_EVENT_ID, "front-door", "person", 0.91),
        )
        # A second event with a numeric-looking id must also stay a string.
        cur.execute(
            """INSERT INTO cams_detect.events (id, camera_id, label, max_score, start_ts, end_ts, updated_at)
               VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(3) - INTERVAL 30 SECOND, NULL, UTC_TIMESTAMP(3))""",
            ("42", "front-door", "car", 0.5),
        )

    db.run(insert)

    # The old wrong column name must fail against this real table.
    from cams_record.db import DBUnavailable

    with pytest.raises(DBUnavailable, match="Unknown column.*score"):
        db.run(lambda cur: cur.execute("SELECT e.score FROM cams_detect.events e LIMIT 1"))

    rows = db.detection_events_for_push(time.time() - 600)
    ids = {r["id"] for r in rows}
    assert PROD_EVENT_ID in ids
    assert "42" in ids
    assert all(isinstance(r["id"], str) for r in rows)
    person = next(r for r in rows if r["id"] == PROD_EVENT_ID)
    assert person["score"] == pytest.approx(0.91)
    assert person["label"] == "person"

    by_id = db.detection_event_by_id(PROD_EVENT_ID)
    assert by_id["id"] == PROD_EVENT_ID
    assert by_id["score"] == pytest.approx(0.91)

    assert db.claim_push_event(PROD_EVENT_ID, "front-door") is True
    assert db.claim_push_event(PROD_EVENT_ID, "front-door") is False
    # After claim, poller query must not return it again.
    remaining = {r["id"] for r in db.detection_events_for_push(time.time() - 600)}
    assert PROD_EVENT_ID not in remaining

    # Event-context HTTP path against the real table.
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    r = client.get(f"/cams-record/api/push/event-context/{PROD_EVENT_ID}",
                   headers={"x-apphub-token": tok(roles=["cams"])})
    assert r.status_code == 200
    assert r.json()["id"] == PROD_EVENT_ID
    assert r.json()["score"] == pytest.approx(0.91)


def test_migration_v6_widens_existing_push_sent_events(db):
    """Simulate a rolled-back install that already has empty v5 BIGINT push_sent_events."""
    from cams_record.db import MIGRATIONS

    # Force table back to the v5 BIGINT definition, then re-apply migrations.
    def shrink(cur):
        cur.execute("DROP TABLE IF EXISTS push_sent_events")
        cur.execute("""CREATE TABLE push_sent_events (
            event_id BIGINT NOT NULL PRIMARY KEY,
            camera_id VARCHAR(64) NOT NULL,
            sent_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            KEY idx_push_sent_at (sent_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        cur.execute("DELETE FROM schema_version WHERE version >= 6")

    db.run(shrink)
    db.ready = False
    assert db.ensure_schema() == MIGRATIONS[-1][0]
    assert db.claim_push_event(PROD_EVENT_ID, "front-door") is True
    stored = db.run(lambda cur: (cur.execute("SELECT event_id FROM push_sent_events"),
                                 cur.fetchone()[0])[1])
    assert stored == PROD_EVENT_ID
