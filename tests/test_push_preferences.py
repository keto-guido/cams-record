"""Per-user phone alert types: storage (migration v9), GET/PUT/DELETE /api/push/preferences validation,
and fan-out filtering (camera access first, then the user's chosen types; no saved choice = every type).
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from cams_record.db import MIGRATIONS
from cams_record.push import ALERT_TYPE_IDS, DetectionEvent, PushNotifier, alert_type, validate_subscription
from cams_record.service import Service
from cams_record.web import create_app
from tests import test_push
from tests.test_push import WEBHOOK, _sub_body, tok

push_env = test_push.push_env  # shared fixture

PREFS = "/cams-record/api/push/preferences"
JSON = {"content-type": "application/json"}


def auth(sub="eric", roles=("cams",)):
    return {"x-apphub-token": tok(sub=sub, roles=roles)}


# ------------------------------------------------------------------------------------- type model


def test_alert_types_match_cams_detect_labels():
    # cams-detect default classes (cams_detect/config.py) plus a bucket for any other configured class.
    assert ALERT_TYPE_IDS == ("person", "car", "dog", "cat", "other")
    assert not any("audio" in t or "sound" in t for t in ALERT_TYPE_IDS)


@pytest.mark.parametrize(("label", "want"), [
    ("person", "person"), ("Person", "person"), (" car ", "car"), ("dog", "dog"), ("cat", "cat"),
    ("truck", "other"), ("bird", "other"), ("", "other"), (None, "other"), ("other", "other"),
])
def test_alert_type_of_label(label, want):
    assert alert_type(label) == want


# ------------------------------------------------------------------------------------- storage


def test_migration_v9_is_additive_and_idempotent(db):
    version, stmts = MIGRATIONS[-1]
    assert version == 9 and db.schema_version == 9
    assert all(s.lstrip().upper().startswith("CREATE TABLE IF NOT EXISTS PUSH_PREFERENCES") for s in stmts)
    for stmt in stmts:  # safe to retry after a timeout
        db.run(lambda cur, s=stmt: cur.execute(s))
    cols = db.run(lambda cur: (cur.execute("SHOW COLUMNS FROM push_preferences"), cur.fetchall())[1])
    assert [c[0] for c in cols] == ["user_sub", "types_json", "updated_at"]


def test_storage_roundtrip_and_reset(db):
    assert db.push_preferences(["eric"]) == {}
    assert db.push_preferences([]) == {}
    db.set_push_preferences("eric", ["person", "car", "person"])
    db.set_push_preferences("ann", [])
    assert db.push_preferences(["eric", "ann", "nobody"]) == {"eric": ["car", "person"], "ann": []}
    db.set_push_preferences("eric", ["dog"])  # upsert
    assert db.push_preferences() == {"eric": ["dog"], "ann": []}
    assert db.clear_push_preferences("eric") == 1
    assert db.push_preferences(["eric"]) == {}


def test_unreadable_row_means_default(db):
    db.run(lambda cur: cur.execute("INSERT INTO push_preferences (user_sub, types_json) VALUES ('eric', 'nope')"))
    assert db.push_preferences(["eric"]) == {"eric": None}


# ------------------------------------------------------------------------------------- API


@pytest.fixture
def api(db, push_env):
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    return TestClient(create_app(svc, cfg)), svc


def test_get_defaults_to_every_type(api):
    client, _ = api
    r = client.get(PREFS, headers=auth())
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    body = r.json()
    assert body["default"] is True
    assert [t["id"] for t in body["types"]] == list(ALERT_TYPE_IDS)
    assert all(t["enabled"] for t in body["types"]) and all(t["label"] for t in body["types"])


def test_put_saves_per_user_and_get_reflects_it(api, db):
    client, _ = api
    r = client.put(PREFS, headers={**auth(), **JSON}, json={"types": ["person", "car"]})
    assert r.status_code == 200, r.text
    on = {t["id"]: t["enabled"] for t in r.json()["types"]}
    assert r.json()["default"] is False
    assert on == {"person": True, "car": True, "dog": False, "cat": False, "other": False}
    assert client.get(PREFS, headers=auth()).json() == r.json()
    # Other users keep their own (default) choice.
    assert client.get(PREFS, headers=auth(sub="ann")).json()["default"] is True
    assert db.push_preferences(["eric"]) == {"eric": ["car", "person"]}


def test_put_empty_list_turns_every_type_off(api):
    client, _ = api
    r = client.put(PREFS, headers={**auth(), **JSON}, json={"types": []})
    assert r.status_code == 200 and not any(t["enabled"] for t in r.json()["types"])


def test_delete_restores_default(api):
    client, _ = api
    client.put(PREFS, headers={**auth(), **JSON}, json={"types": ["dog"]})
    r = client.delete(PREFS, headers=auth())
    assert r.status_code == 200 and r.json()["default"] is True and all(t["enabled"] for t in r.json()["types"])


@pytest.mark.parametrize("body", [
    {"types": ["person", "unicorn"]}, {"types": ["audio"]}, {"types": ["sound"]}, {"types": ["Person"]},
    {"types": [1]}, {"types": [None]}, {"types": "person"}, {"types": None}, {"types": {"person": True}},
    {"types": ["person"], "extra": 1}, {}, ["person"], "person", {"types": ["person"] * 6},
])
def test_put_rejects_unknown_or_malformed(api, db, body):
    client, _ = api
    r = client.put(PREFS, headers={**auth(), **JSON}, json=body)
    assert r.status_code == 422, (body, r.text)
    assert db.push_preferences(["eric"]) == {}


def test_put_needs_json_and_same_origin(api, db):
    client, _ = api
    r = client.put(PREFS, headers={**auth(), "content-type": "text/plain"}, content='{"types": []}')
    assert r.status_code == 415
    r = client.put(PREFS, headers={**auth(), **JSON}, content="{nope")
    assert r.status_code == 400
    r = client.put(PREFS, headers={**auth(), **JSON, "sec-fetch-site": "cross-site"}, json={"types": []})
    assert r.status_code == 403
    r = client.delete(PREFS, headers={**auth(), "sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert db.push_preferences(["eric"]) == {}


@pytest.mark.parametrize(("method", "kw"), [("get", {}), ("put", {"json": {"types": []}}), ("delete", {})])
def test_requires_auth(api, db, method, kw):
    client, _ = api
    assert getattr(client, method)(PREFS, **kw).status_code == 401
    r = getattr(client, method)(PREFS, headers={"x-apphub-token": tok(roles=("viewer",))}, **kw)
    assert r.status_code == 403
    assert db.push_preferences() == {}


def test_without_database(push_env):
    cfg, cams, *_ = push_env
    client = TestClient(create_app(Service(cfg, cams, db=None), cfg))
    assert client.get(PREFS, headers=auth()).status_code == 503
    assert client.put(PREFS, headers={**auth(), **JSON}, json={"types": []}).status_code == 503


# ------------------------------------------------------------------------------------- fan-out


@pytest.fixture
def fanout(db, push_env):
    """eric (cams) and ann (family) subscribed; front-door is open, garage only eric + family (REGISTRY)."""
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    create_app(svc, cfg)  # wires CameraAccess into the push service, as in production
    for user, roles in (("eric", ["cams"]), ("ann", ["cams", "family"]), ("bob", ["cams"])):
        db.upsert_push_subscription(user, validate_subscription(
            _sub_body(f"https://fcm.googleapis.com/fcm/send/{user}")), roles=roles)
    sent = []

    def capture(subscription, payload):
        sent.append((subscription["user_sub"], payload["label"]))
        return True, 201

    svc.push.send_raw = capture
    n = iter(range(1, 10_000))

    def fire(camera, label):
        sent.clear()
        ev = DetectionEvent(id=f"1793897909-{next(n):06d}", camera_id=camera, label=label, score=0.9,
                            start_ts=time.time(), end_ts=None, camera_name=camera)
        result = svc.push.notify_event(ev)
        return sorted(u for u, _ in sent), result

    return svc, db, fire


def test_default_is_every_type(fanout):
    _, _, fire = fanout
    for label in ("person", "car", "dog", "cat", "truck"):
        assert fire("front-door", label)[0] == ["ann", "bob", "eric"]


def test_type_filter(fanout):
    _, db, fire = fanout
    db.set_push_preferences("eric", ["person"])
    db.set_push_preferences("bob", [])
    users, result = fire("front-door", "person")
    assert users == ["ann", "eric"] and result["type_filtered"] == 1
    users, result = fire("front-door", "car")
    assert users == ["ann"] and result["type_filtered"] == 2
    db.set_push_preferences("eric", ["other"])
    assert fire("front-door", "truck")[0] == ["ann", "eric"]  # unlisted labels map to "other"
    assert fire("front-door", "person")[0] == ["ann"]


def test_type_choice_never_widens_camera_access(fanout):
    _, db, fire = fanout
    db.set_push_preferences("bob", list(ALERT_TYPE_IDS))
    db.set_push_preferences("ann", ["car"])
    # garage: eric by name, ann by role; bob may not view it whatever types he picked.
    users, result = fire("garage", "person")
    assert users == ["eric"] and result["filtered"] == 1 and result["type_filtered"] == 1
    assert fire("garage", "car")[0] == ["ann", "eric"]
    assert fire("unregistered-cam", "person")[0] == []


def test_preferences_apply_to_all_devices_of_a_user(fanout):
    _, db, fire = fanout
    db.upsert_push_subscription("eric", validate_subscription(
        _sub_body("https://fcm.googleapis.com/fcm/send/eric-ipad")), roles=["cams"])
    assert fire("front-door", "dog")[0] == ["ann", "bob", "eric", "eric"]
    db.set_push_preferences("eric", ["person"])
    assert fire("front-door", "dog")[0] == ["ann", "bob"]
    assert fire("front-door", "person")[0] == ["ann", "bob", "eric", "eric"]


def test_filtered_events_are_still_claimed(fanout):
    svc, db, fire = fanout
    for user in ("eric", "ann", "bob"):
        db.set_push_preferences(user, ["person"])
    users, result = fire("front-door", "cat")
    assert users == [] and result["sent"] == 0
    # A later change of preferences never releases the old event.
    again = svc.push.notify_event(DetectionEvent(id=result["event_id"], camera_id="front-door", label="cat",
                                                 score=0.9, start_ts=time.time(), end_ts=None))
    assert again.get("deduped") is True


def test_test_push_ignores_type_choice(fanout):
    svc, db, _ = fanout
    db.set_push_preferences("eric", [])
    ev = DetectionEvent(id="", camera_id="test", label="test", score=None, start_ts=0.0, end_ts=None)
    assert svc.push.notify_user("eric", ev, test=True)["sent"] == 1


def test_webhook_and_poller_honour_choice(fanout):
    svc, db, _ = fanout
    db.set_push_preferences("eric", ["car"])
    db.set_push_preferences("ann", ["car"])
    db.set_push_preferences("bob", ["car"])
    sent = []
    svc.push.send_raw = lambda s, p: (sent.append(s["user_sub"]), (True, 201))[1]
    client = TestClient(create_app(svc, svc.cfg))
    body = {"id": "1793897909-wh0001", "camera_id": "front-door", "label": "person", "start_ts": time.time()}
    r = client.post("/cams-record/api/internal/detection-event", headers={"x-cams-push-secret": WEBHOOK}, json=body)
    assert r.status_code == 200 and r.json()["sent"] == 0 and r.json()["type_filtered"] == 3
    body = {**body, "id": "1793897909-wh0002", "label": "car"}
    r = client.post("/cams-record/api/internal/detection-event", headers={"x-cams-push-secret": WEBHOOK}, json=body)
    assert r.json()["sent"] == 3 and sorted(sent) == ["ann", "bob", "eric"]
    sent.clear()

    class Events:
        def detection_events_for_push(self, since, limit=200, snapshot_wait_s=0):
            now = time.time()
            return [{"id": "1793897909-po0001", "camera_id": "front-door", "label": "dog", "score": 0.8,
                     "start_ts": now, "end_ts": None},
                    {"id": "1793897909-po0002", "camera_id": "front-door", "label": "car", "score": 0.8,
                     "start_ts": now, "end_ts": None}]

        def __getattr__(self, name):
            return getattr(db, name)

    PushNotifier(svc.push, Events(), {**svc.cfg["push"], "poll_enabled": True}).run_once()
    assert sorted(sent) == ["ann", "bob", "eric"]
