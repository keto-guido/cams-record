"""Web Push: auth/ownership, payload/deep-link, failure pruning, detection webhook."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient
from py_vapid import Vapid

from cams_record.config import DEFAULTS, deep_merge, resolve_camera
from cams_record.push import (
    DetectionEvent,
    PushError,
    deep_link_for,
    endpoint_hash,
    notification_payload,
    validate_subscription,
)
from cams_record.service import Service
from cams_record.web import create_app

SECRET = "s3cret-for-tests-0123456789abcdef-xyz"
WEBHOOK = "webhook-secret-for-tests-abcdefghijklmnopqrstuvwxyz"


def _vapid_pem(tmp_path: Path) -> tuple[str, str]:
    vapid = Vapid()
    vapid.generate_keys()
    path = tmp_path / "vapid_private.pem"
    vapid.save_key(str(path))
    pem = path.read_text(encoding="utf-8")
    from cams_record.push import load_vapid_public_b64

    return pem, load_vapid_public_b64(pem)


def tok(sub="eric", roles=("cams",), exp=60, secret=SECRET):
    return jwt.encode(
        {"aud": "cams-record", "sub": sub, "roles": list(roles), "exp": int(time.time()) + exp},
        secret, "HS256",
    )


def _sub_body(endpoint="https://fcm.googleapis.com/fcm/send/testdevice"):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpDtUbVlDsiZv3h7FcOxT_YvCqd0_5e1O0tkzZV8ikhYQqE",
                 "auth": "tBHItJI5svbpez7KI4CCXg"},
    }


@pytest.fixture
def push_env(tmp_path):
    pem, pub = _vapid_pem(tmp_path)
    jwt_file = tmp_path / "apphub_jwt"
    jwt_file.write_text(SECRET + "\n")
    vapid_file = tmp_path / "vapid_private_key"
    vapid_file.write_text(pem)
    hook_file = tmp_path / "push_webhook_secret"
    hook_file.write_text(WEBHOOK)
    cfg = deep_merge(DEFAULTS, {
        "auth": {"secret_file": str(jwt_file)},
        "recording": {"root": str(tmp_path / "nvr"), "state_dir": str(tmp_path / "state")},
        "push": {
            "enabled": True,
            "vapid_private_file": str(vapid_file),
            "vapid_public_key": pub,
            "vapid_subject": "mailto:test@example.com",
            "webhook_secret_file": str(hook_file),
            "poll_enabled": False,
        },
    })
    cams = {"front-door": resolve_camera("front-door", {"main_stream": "front-door", "sub_stream": "fd-sub"})}
    return cfg, cams, pub


@pytest.fixture
def client_nodb(push_env):
    cfg, cams, _ = push_env
    svc = Service(cfg, cams, db=None)
    return TestClient(create_app(svc, cfg)), cfg


def test_validate_subscription_rejects_bad_hosts():
    with pytest.raises(PushError):
        validate_subscription({"endpoint": "http://evil.example/push", "keys": {"p256dh": "x", "auth": "y"}})
    with pytest.raises(PushError):
        validate_subscription({"endpoint": "https://evil.example/push", "keys": {"p256dh": "x", "auth": "y"}})
    ok = validate_subscription(_sub_body())
    assert ok["endpoint_hash"] == endpoint_hash(ok["endpoint"])


def test_notification_payload_deep_link_and_no_image():
    event = DetectionEvent(id=42, camera_id="front-door", label="person", score=0.9,
                           start_ts=1_700_000_000.0, end_ts=None, camera_name="Front Door")
    payload = notification_payload(event)
    assert payload["url"] == "/cams/#event=42"
    assert deep_link_for(event) == payload["url"]
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
    # internal webhook is secret-based, not App Hub JWT
    assert client.post("/cams-record/api/internal/detection-event", json={}).status_code == 401


def test_vapid_public_key_for_cams_role(client_nodb):
    client, _ = client_nodb
    headers = {"x-apphub-token": tok(roles=["cams"])}
    r = client.get("/cams-record/api/push/vapid-public-key", headers=headers)
    assert r.status_code == 200
    assert r.json()["publicKey"]
    # never returns private material
    assert "PRIVATE" not in r.text
    assert "private" not in r.json()


def test_subscribe_ownership_and_unsubscribe(db, push_env):
    cfg, cams, pub = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    eric = {"x-apphub-token": tok(sub="eric", roles=["cams"])}
    other = {"x-apphub-token": tok(sub="other", roles=["cams"])}
    body = _sub_body("https://fcm.googleapis.com/fcm/send/eric-phone")

    assert client.post("/cams-record/api/push/subscribe", headers=eric, json=body).status_code == 200
    st = client.get("/cams-record/api/push/status", headers=eric).json()
    assert st["subscribed"] is True and st["subscription_count"] == 1
    # another user does not see eric's subscription count as theirs empty
    st2 = client.get("/cams-record/api/push/status", headers=other).json()
    assert st2["subscription_count"] == 0
    # other cannot delete eric's endpoint via their token (delete is scoped to user_sub)
    gone = client.request("DELETE", "/cams-record/api/push/subscribe", headers=other,
                          json={"endpoint": body["endpoint"]}).json()
    assert gone["removed"] == 0
    assert client.get("/cams-record/api/push/status", headers=eric).json()["subscription_count"] == 1
    assert client.request("DELETE", "/cams-record/api/push/subscribe", headers=eric,
                          json={"endpoint": body["endpoint"]}).json()["removed"] == 1
    assert client.get("/cams-record/api/push/status", headers=eric).json()["subscription_count"] == 0


def test_failure_pruning_dead_endpoint(db, push_env, monkeypatch):
    cfg, cams, _ = push_env
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

def test_detection_webhook_dedupes_and_payload(db, push_env, monkeypatch):
    cfg, cams, _ = push_env
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
        "id": 99,
        "camera_id": "front-door",
        "camera_name": "Front Door",
        "label": "person",
        "score": 0.88,
        "start_ts": 1_700_000_100,
    }
    headers = {"x-cams-push-secret": WEBHOOK}
    r1 = client.post("/cams-record/api/internal/detection-event", headers=headers, json=event)
    assert r1.status_code == 200
    assert r1.json()["sent"] == 1
    assert r1.json().get("deduped") is not True
    assert sent[0]["url"] == "/cams/#event=99"
    assert sent[0]["event_id"] == 99
    assert "image" not in sent[0]

    r2 = client.post("/cams-record/api/internal/detection-event", headers=headers, json=event)
    assert r2.status_code == 200
    assert r2.json().get("deduped") is True
    assert r2.json()["sent"] == 0
    assert len(sent) == 1  # no second fan-out


def test_push_test_only_own_subscriptions(db, push_env, monkeypatch):
    cfg, cams, _ = push_env
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


def test_event_context_auth(db, push_env, monkeypatch):
    cfg, cams, _ = push_env
    svc = Service(cfg, cams, db=db)
    client = TestClient(create_app(svc, cfg))
    monkeypatch.setattr(db, "detection_event_by_id", lambda event_id: {
        "id": event_id, "camera_id": "front-door", "label": "car", "score": 0.5,
        "start_ts": 10.0, "end_ts": 12.0,
    })
    assert client.get("/cams-record/api/push/event-context/7").status_code == 401
    r = client.get("/cams-record/api/push/event-context/7",
                   headers={"x-apphub-token": tok(roles=["cams"])})
    assert r.status_code == 200
    assert r.json()["url"] == "/cams/#event=7"
    assert "snapshot" not in r.json()


def test_webhook_rejects_jwt_as_public_send(client_nodb):
    client, _ = client_nodb
    # Even a valid admin JWT must not authorize the internal webhook.
    r = client.post(
        "/cams-record/api/internal/detection-event",
        headers={"x-apphub-token": tok(roles=["admin"])},
        json={"id": 1, "camera_id": "front-door", "start_ts": 1},
    )
    assert r.status_code == 401
