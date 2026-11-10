"""Web Push: auth/ownership, payload/deep-link, failure pruning, detection webhook,
real cams_detect.events schema, and VAPID PEM → pywebpush key loading.
"""

from __future__ import annotations

import base64
import json
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


# Production cams_detect.events shape (keto-guido/cams-detect storage_mariadb.py):
# ``id`` is a BIGINT auto-increment PK; the string event id used by MQTT / API /
# push payloads lives in ``event_uid``. Score column is ``max_score`` (not score).
PROD_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS cams_detect.events (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_uid VARCHAR(64) NOT NULL,
    camera_id VARCHAR(64) NOT NULL,
    label VARCHAR(64) NOT NULL,
    max_score DOUBLE NULL,
    start_ts DATETIME(3) NOT NULL,
    end_ts DATETIME(3) NULL,
    snapshot_path VARCHAR(512) NULL,
    snapshot_hires VARCHAR(512) NULL,
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY uq_event_uid (event_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
"""
# Production collations differ: cams_detect.events is utf8mb4_general_ci, cams_record's
# push tables utf8mb4_unicode_ci. Keep the fixture that way so joins are tested honestly.


def _insert_event(db, uid, *, camera_id="front-door", label="person", score=0.91,
                  age_s=0, snapshot_path=None, snapshot_hires=None) -> int:
    """Insert one production-shaped event and return its numeric auto-increment PK."""
    def q(cur):
        cur.execute(
            """INSERT INTO cams_detect.events
                   (event_uid, camera_id, label, max_score, start_ts, end_ts,
                    snapshot_path, snapshot_hires, updated_at)
               VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(3) - INTERVAL %s SECOND, NULL,
                       %s, %s, UTC_TIMESTAMP(3))""",
            (uid, camera_id, label, score, int(age_s), snapshot_path, snapshot_hires))
        return cur.lastrowid
    return db.run(q)


# front-door is open to every cams user; garage only to eric and the family role.
REGISTRY = [
    {"id": "front-door", "name": "Front Door"},
    {"id": "garage", "name": "Garage", "users": ["eric"], "roles": ["family"]},
]


@pytest.fixture
def push_env(tmp_path):
    pem, pub, path = _vapid_pem(tmp_path)
    jwt_file = tmp_path / "apphub_jwt"
    jwt_file.write_text(SECRET + "\n")
    hook_file = tmp_path / "push_webhook_secret"
    hook_file.write_text(WEBHOOK)
    registry_dir = tmp_path / "apphub-registry"
    registry_dir.mkdir()
    (registry_dir / "cams.json").write_text(json.dumps(REGISTRY))
    cfg = deep_merge(DEFAULTS, {
        "auth": {"secret_file": str(jwt_file)},
        "access": {"registry_file": str(registry_dir / "cams.json")},
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
    cams = {"front-door": resolve_camera("front-door", {"main_stream": "front-door", "sub_stream": "fd-sub"}),
            "garage": resolve_camera("garage", {"main_stream": "garage-main", "sub_stream": "garage"})}
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
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
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
    # Rows without the flags (older callers / mocks) report no snapshots.
    assert r.json()["has_snapshot"] is False and r.json()["has_snapshot_hires"] is False


def test_webhook_rejects_jwt_as_public_send(client_nodb):
    client, _ = client_nodb
    r = client.post(
        "/cams-record/api/internal/detection-event",
        headers={"x-apphub-token": tok(roles=["admin"])},
        json={"id": PROD_EVENT_ID, "camera_id": "front-door", "start_ts": 1},
    )
    assert r.status_code == 401


def test_production_events_schema_max_score_and_event_uid(cams_detect_events, push_env, monkeypatch):
    """Query the real production events shape (BIGINT id + event_uid + max_score)."""
    db = cams_detect_events
    cfg, cams, *_ = push_env
    # Confirm schema v6 widened event_id (forward migration from BIGINT).
    col = db.run(lambda cur: (cur.execute(
        "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='push_sent_events' AND COLUMN_NAME='event_id'"),
        cur.fetchone())[1])
    assert col[0].lower() in ("varchar", "char")
    assert int(col[1] or 0) >= 32

    # Occupy a few PKs first so the numeric id can never coincide with anything uid-like.
    for i in range(3):
        _insert_event(db, f"filler-{i}", age_s=3600)
    person_pk = _insert_event(db, PROD_EVENT_ID, snapshot_path="/snap/a.jpg",
                              snapshot_hires="/snap/a_hd.jpg")
    car_uid = "1793897879-0a1b2c"
    car_pk = _insert_event(db, car_uid, label="car", score=0.5, age_s=30)
    assert isinstance(person_pk, int) and person_pk > 0

    # The old wrong column name must fail against this real table.
    from cams_record.db import DBUnavailable

    with pytest.raises(DBUnavailable, match="Unknown column.*score"):
        db.run(lambda cur: cur.execute("SELECT e.score FROM cams_detect.events e LIMIT 1"))

    # Poller emits uids (never numeric PKs).
    rows = db.detection_events_for_push(time.time() - 600)
    ids = {r["id"] for r in rows}
    assert ids == {PROD_EVENT_ID, car_uid}
    assert str(person_pk) not in ids and str(car_pk) not in ids
    person = next(r for r in rows if r["id"] == PROD_EVENT_ID)
    assert person["score"] == pytest.approx(0.91)
    assert person["label"] == "person"

    # Lookup is by uid; a numeric PK (legacy deep link) resolves to the same row, reported by uid.
    by_id = db.detection_event_by_id(PROD_EVENT_ID)
    assert by_id["id"] == PROD_EVENT_ID
    assert by_id["score"] == pytest.approx(0.91)
    assert by_id["has_snapshot"] is True and by_id["has_snapshot_hires"] is True
    assert db.detection_event_by_id(str(person_pk)) == by_id

    # Claims are keyed by uid, so a claimed event drops out of the poller query.
    assert db.claim_push_event(PROD_EVENT_ID, "front-door") is True
    assert db.claim_push_event(PROD_EVENT_ID, "front-door") is False
    remaining = {r["id"] for r in db.detection_events_for_push(time.time() - 600)}
    assert remaining == {car_uid}

    # Event-context HTTP path against the real table.
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    r = client.get(f"/cams-record/api/push/event-context/{PROD_EVENT_ID}",
                   headers={"x-apphub-token": tok(roles=["cams"])})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == PROD_EVENT_ID
    assert body["score"] == pytest.approx(0.91)
    assert body["has_snapshot"] is True and body["has_snapshot_hires"] is True
    assert "snapshot_path" not in body and "snapshot_hires" not in body


@pytest.mark.parametrize(("snap", "hires", "want_snap", "want_hires"), [
    (None, None, False, False),
    ("/snap/low.jpg", None, True, False),
    ("/snap/low.jpg", "/snap/hd.jpg", True, True),
])
def test_event_context_snapshot_flags_by_uid(cams_detect_events, push_env, snap, hires, want_snap, want_hires):
    db = cams_detect_events
    cfg, cams, *_ = push_env
    uid = "1794318446-1314b3"
    pk = _insert_event(db, uid, snapshot_path=snap, snapshot_hires=hires)

    row = db.detection_event_by_id(uid)
    assert row is not None and row["id"] == uid
    assert row["has_snapshot"] is want_snap
    assert row["has_snapshot_hires"] is want_hires

    client = TestClient(create_app(Service(cfg, cams, db=db), cfg))
    headers = {"x-apphub-token": tok(roles=["cams"])}
    r = client.get(f"/cams-record/api/push/event-context/{uid}", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == uid
    assert body["url"] == f"/cams/#event={uid}"
    assert body["has_snapshot"] is want_snap
    assert body["has_snapshot_hires"] is want_hires
    # Legacy deep link by numeric PK answers with the uid and the same flags.
    legacy = client.get(f"/cams-record/api/push/event-context/{pk}", headers=headers)
    assert legacy.status_code == 200
    assert legacy.json() == body


def test_fixture_collations_match_production(cams_detect_events):
    db = cams_detect_events

    def collation(schema, table, column):
        return db.run(lambda cur: (cur.execute(
            "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=COALESCE(%s, DATABASE()) AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (schema, table, column)), cur.fetchone()[0])[1])

    assert collation("cams_detect", "events", "event_uid") == "utf8mb4_general_ci"
    assert collation(None, "push_sent_events", "event_id") == "utf8mb4_unicode_ci"
    # A plain comparison is what production rejects; the poller must not rely on it.
    from cams_record.db import DBUnavailable

    with pytest.raises(DBUnavailable, match="[Ii]llegal mix of collations"):
        db.run(lambda cur: cur.execute(
            "SELECT 1 FROM cams_detect.events e JOIN push_sent_events s "
            "ON s.event_id = CAST(e.id AS CHAR) LIMIT 1"))


def test_event_context_legacy_numeric_pk(cams_detect_events, push_env):
    db = cams_detect_events
    cfg, cams, *_ = push_env
    uid = "1794318446-1314b3"
    pk = _insert_event(db, uid, snapshot_path="/snap/low.jpg", snapshot_hires="/snap/hd.jpg")
    garage_pk = _insert_event(db, "1794318447-9f9f9f", camera_id="garage", snapshot_path="/snap/g.jpg")
    client = TestClient(create_app(Service(cfg, cams, db=db), cfg))
    bob = {"x-apphub-token": tok(sub="bob", roles=["cams"])}

    r = client.get(f"/cams-record/api/push/event-context/{pk}", headers=bob)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == uid and body["url"] == f"/cams/#event={uid}"
    assert body["has_snapshot"] is True and body["has_snapshot_hires"] is True

    # Unknown numeric id and a camera the viewer may not see are the same 404.
    assert client.get(f"/cams-record/api/push/event-context/{garage_pk + 1000}",
                      headers=bob).status_code == 404
    forbidden = client.get(f"/cams-record/api/push/event-context/{garage_pk}", headers=bob)
    assert forbidden.status_code == 404 and forbidden.json() == {"detail": "event not found"}
    eric = client.get(f"/cams-record/api/push/event-context/{garage_pk}",
                      headers={"x-apphub-token": tok(sub="eric", roles=["cams"])})
    assert eric.status_code == 200 and eric.json()["id"] == "1794318447-9f9f9f"


def _poller(db, push_env, monkeypatch, **push_cfg):
    cfg, cams, *_ = push_env
    cfg["push"].update({"poll_enabled": True, **push_cfg})
    svc = Service(cfg, cams, db=db)
    sent = []

    def capture(subscription, payload):
        sent.append(payload["event_id"])
        return True, 201

    monkeypatch.setattr(svc.push, "send_raw", capture)
    _subscribe(db, "eric", ["cams"], "eric")
    return svc.push_notifier, sent


def _claims(db):
    return db.run(lambda cur: (cur.execute("SELECT event_id FROM push_sent_events"),
                               {r[0] for r in cur.fetchall()})[1])


def test_poller_skips_events_claimed_by_legacy_pk(cams_detect_events, push_env, monkeypatch):
    db = cams_detect_events
    old_uid, new_uid = "1794318400-000001", "1794318401-000002"
    old_pk = _insert_event(db, old_uid, snapshot_path="/snap/old.jpg", age_s=20)
    _insert_event(db, new_uid, snapshot_path="/snap/new.jpg", age_s=10)
    # Claim written by the pre-uid release: keyed by the numeric PK.
    assert db.claim_push_event(str(old_pk), "front-door") is True

    assert [r["id"] for r in db.detection_events_for_push(time.time() - 600)] == [new_uid]
    notifier, sent = _poller(db, push_env, monkeypatch)
    assert notifier.run_once() == 1
    assert sent == [new_uid]
    assert _claims(db) == {str(old_pk), new_uid}
    assert notifier.run_once() == 0 and sent == [new_uid]


def test_poller_waits_for_snapshot(cams_detect_events, push_env, monkeypatch):
    db = cams_detect_events
    uid = "1794318446-1314b3"
    _insert_event(db, uid, age_s=0)
    notifier, sent = _poller(db, push_env, monkeypatch, snapshot_wait_s=5)

    assert notifier.run_once() == 0 and sent == []  # snapshot not written yet
    assert _claims(db) == set()
    db.run(lambda cur: cur.execute(
        "UPDATE cams_detect.events SET snapshot_path='/snap/low.jpg' WHERE event_uid=%s", (uid,)))
    assert notifier.run_once() == 1 and sent == [uid]


def test_poller_sends_snapshotless_event_after_wait(cams_detect_events, push_env, monkeypatch):
    db = cams_detect_events
    uid = "1794318446-aaaaaa"
    _insert_event(db, uid, age_s=0)
    notifier, sent = _poller(db, push_env, monkeypatch, snapshot_wait_s=5)

    assert notifier.run_once() == 0
    db.run(lambda cur: cur.execute(
        "UPDATE cams_detect.events SET start_ts = UTC_TIMESTAMP(3) - INTERVAL 6 SECOND "
        "WHERE event_uid=%s", (uid,)))
    assert notifier.run_once() == 1 and sent == [uid]


def test_poller_snapshot_wait_zero_sends_immediately(cams_detect_events, push_env, monkeypatch):
    db = cams_detect_events
    uid = "1794318446-bbbbbb"
    _insert_event(db, uid, age_s=0)
    notifier, sent = _poller(db, push_env, monkeypatch, snapshot_wait_s=0)
    assert notifier.run_once() == 1 and sent == [uid]


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


# ---- detection pushes follow per-camera access (docs/camera-access.md) ----

def _subscribe(db, user, roles, name):
    sub = validate_subscription(_sub_body(f"https://fcm.googleapis.com/fcm/send/{name}"))
    db.upsert_push_subscription(user, sub, roles=roles)
    return sub


def _capture(monkeypatch, push):
    sent = []

    def capture(subscription, payload):
        sent.append(subscription["user_sub"])
        return True, 201

    monkeypatch.setattr(push, "send_raw", capture)
    return sent


def _event(camera_id, eid="1793897909-aaaaaa"):
    return DetectionEvent(id=eid, camera_id=camera_id, label="person", score=0.9,
                          start_ts=1_700_000_000.0, end_ts=None, camera_name=camera_id)


def test_push_only_reaches_viewers_of_restricted_camera(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    sent = _capture(monkeypatch, svc.push)
    _subscribe(db, "eric", ["cams"], "eric")          # named in users
    _subscribe(db, "bob", ["cams"], "bob")            # not granted
    _subscribe(db, "carol", ["cams", "family"], "carol")  # granted by role
    _subscribe(db, "root", ["admin"], "root")         # admin sees every registered camera
    _subscribe(db, "dave", None, "dave")              # pre-v7 row: roles unknown

    r = svc.push.notify_event(_event("garage"))
    assert sorted(sent) == ["carol", "eric", "root"]
    assert r["sent"] == 3 and r["filtered"] == 2 and r["subscribers"] == 5

    sent.clear()
    svc.push.notify_event(_event("front-door", eid="1793897909-bbbbbb"))
    assert sorted(sent) == ["bob", "carol", "dave", "eric", "root"]


def test_push_unregistered_camera_notifies_nobody(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    sent = _capture(monkeypatch, svc.push)
    _subscribe(db, "root", ["admin"], "root")
    r = svc.push.notify_event(_event("shed"))
    assert sent == [] and r["filtered"] == 1


def test_push_unreadable_registry_notifies_nobody(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    Path(cfg["access"]["registry_file"]).write_text("not json")
    svc = Service(cfg, cams, db=db)
    sent = _capture(monkeypatch, svc.push)
    _subscribe(db, "root", ["admin"], "root")
    svc.push.notify_event(_event("front-door"))
    assert sent == []


def test_push_stale_roles_fall_back_to_no_roles(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    cfg["push"]["roles_max_age_days"] = 1
    svc = Service(cfg, cams, db=db)
    sent = _capture(monkeypatch, svc.push)
    _subscribe(db, "carol", ["family"], "carol")
    db.run(lambda cur: cur.execute(
        "UPDATE push_subscriptions SET roles_at = UTC_TIMESTAMP(3) - INTERVAL 2 DAY"))
    svc.push.notify_event(_event("garage"))
    assert sent == []


def test_push_status_refreshes_roles_used_for_alerts(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    sent = _capture(monkeypatch, svc.push)
    _subscribe(db, "carol", None, "carol")
    svc.push.notify_event(_event("garage"))
    assert sent == []

    assert client.get("/cams-record/api/push/status",
                      headers={"x-apphub-token": tok(sub="carol", roles=["cams", "family"])}).status_code == 200
    svc.push.notify_event(_event("garage", eid="1793897909-cccccc"))
    assert sent == ["carol"]

    # Losing the role takes effect on the next app open.
    client.get("/cams-record/api/push/status", headers={"x-apphub-token": tok(sub="carol", roles=["cams"])})
    sent.clear()
    svc.push.notify_event(_event("garage", eid="1793897909-dddddd"))
    assert sent == []


def test_subscribe_stores_roles(db, push_env):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    client.post("/cams-record/api/push/subscribe", json=_sub_body(),
                headers={"x-apphub-token": tok(sub="carol", roles=["family", "cams"])})
    (row,) = db.list_push_subscriptions()
    assert json.loads(row["roles_json"]) == ["cams", "family"]
    assert row["roles_at"] is not None


def test_push_access_not_enforced_sends_to_everyone(db, push_env, monkeypatch):
    cfg, cams, *_ = push_env
    cfg["access"]["enforce"] = False
    svc = Service(cfg, cams, db=db)
    sent = _capture(monkeypatch, svc.push)
    _subscribe(db, "bob", ["cams"], "bob")
    svc.push.notify_event(_event("shed"))
    assert sent == ["bob"]
