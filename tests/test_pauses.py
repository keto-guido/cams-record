"""Per-camera phone alert pause: durations and the morning boundary (CST/CDT and both DST changes),
cache invalidation and expiry, skip-and-claim in the poller and the webhook, test pushes, the
/api/push/pauses authorization rules, the MariaDB table, and the Home Assistant MQTT bridge.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from cams_record import mqtt as mq
from cams_record.config import DEFAULTS, deep_merge, resolve_camera, validate
from cams_record.db import DBUnavailable
from cams_record.pauses import MAX_PAUSE_S, PauseError, PushPauses, next_morning, resolve_until
from cams_record.push import DetectionEvent, PushNotifier
from cams_record.service import Service
from cams_record.web import create_app
from tests.test_push import WEBHOOK, push_env, tok  # noqa: F401  (push_env is a fixture)

CHI = ZoneInfo("America/Chicago")


def utc(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def local(*args) -> datetime:
    return datetime(*args, tzinfo=CHI)


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeDB:
    """The Database methods pauses and pushes use, in memory."""

    def __init__(self, clock):
        self.clock = clock
        self.rows: dict[str, dict] = {}
        self.claimed: dict[str, str] = {}
        self.subs: list[dict] = []
        self.events: list[dict] = []
        self.reads = 0
        self.writes = 0
        self.fail = False

    def active_push_pauses(self):
        self.reads += 1
        if self.fail:
            raise DBUnavailable("database down")
        now = self.clock()
        return [{"camera_id": k, **v} for k, v in sorted(self.rows.items()) if v["until_ts"] > now]

    def set_push_pause(self, camera_id, until_ts, set_by, set_at):
        if self.fail:
            raise DBUnavailable("database down")
        self.writes += 1
        self.rows[camera_id] = {"until_ts": until_ts, "set_by": set_by, "set_at": set_at}

    def clear_push_pause(self, camera_id):
        if self.fail:
            raise DBUnavailable("database down")
        self.writes += 1
        return 1 if self.rows.pop(camera_id, None) else 0

    def claim_push_event(self, event_id, camera_id):
        if event_id in self.claimed:
            return False
        self.claimed[event_id] = camera_id
        return True

    def list_push_subscriptions(self):
        return list(self.subs)

    def push_subscriptions_for_user(self, user_sub):
        return [s for s in self.subs if s["user_sub"] == user_sub]

    def detection_events_for_push(self, since, limit=200):
        return [e for e in self.events if e["id"] not in self.claimed and e["start_ts"] >= since][:limit]

    def touch_push_subscription(self, endpoint_hash):
        pass

    def refresh_push_roles(self, user_sub, roles):
        return 0


def sub_row(user, roles):
    return {"user_sub": user, "endpoint": f"https://fcm.googleapis.com/fcm/send/{user}",
            "endpoint_hash": f"hash-{user}", "p256dh": "k", "auth": "a",
            "roles_json": json.dumps(list(roles)), "roles_at": time.time()}


# ---------------------------------------------------------------------------- durations


def test_presets_and_seconds():
    now = utc(2026, 1, 15, 18, 0)
    assert resolve_until("1h", now, CHI) == now + 3600
    assert resolve_until("3h", now, CHI) == now + 3 * 3600
    assert resolve_until(" 1 H ", now, CHI) == now + 3600
    assert resolve_until(900, now, CHI) == now + 900
    assert resolve_until("900", now, CHI) == now + 900
    assert resolve_until(60.0, now, CHI) == now + 60
    assert resolve_until(MAX_PAUSE_S, now, CHI) == now + MAX_PAUSE_S
    assert resolve_until("Until morning", now, CHI) == resolve_until("morning", now, CHI)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, False, None, "", "abc", "1e3", "-60", "0", " ",
                                 MAX_PAUSE_S + 1, float("inf"), float("nan"), [3600], {"s": 1}, "2h",
                                 "99999999"])
def test_invalid_durations(bad):
    with pytest.raises(PauseError) as exc:
        resolve_until(bad, utc(2026, 1, 15, 18, 0), CHI)
    assert exc.value.status == 422


@pytest.mark.parametrize(("now", "want"), [
    # CST (UTC-6): 07:00 local is 13:00 UTC.
    (local(2026, 1, 15, 21, 40), local(2026, 1, 16, 7, 0)),
    (local(2026, 1, 16, 0, 5), local(2026, 1, 16, 7, 0)),     # after midnight: this morning
    (local(2026, 1, 16, 6, 59, 59), local(2026, 1, 16, 7, 0)),
    (local(2026, 1, 16, 7, 0), local(2026, 1, 17, 7, 0)),      # exactly 07:00: the next one
    (local(2026, 1, 16, 7, 0, 1), local(2026, 1, 17, 7, 0)),
    # CDT (UTC-5): 07:00 local is 12:00 UTC.
    (local(2026, 7, 4, 22, 0), local(2026, 7, 5, 7, 0)),
    (local(2026, 12, 31, 23, 30), local(2027, 1, 1, 7, 0)),    # across a year boundary
])
def test_next_morning_is_local_seven(now, want):
    got = next_morning(now.timestamp(), CHI)
    assert got == want.timestamp()
    assert datetime.fromtimestamp(got, CHI).hour == 7


def test_morning_cst_and_cdt_utc_offsets():
    assert next_morning(local(2026, 1, 15, 22, 0).timestamp(), CHI) == utc(2026, 1, 16, 13, 0)
    assert next_morning(local(2026, 7, 15, 22, 0).timestamp(), CHI) == utc(2026, 7, 16, 12, 0)


def test_morning_across_spring_forward():
    # 2026-03-08 02:00 CST jumps to 03:00 CDT: the night is an hour shorter, still ending 07:00 CDT.
    evening = local(2026, 3, 7, 22, 0).timestamp()          # 04:00 UTC
    got = next_morning(evening, CHI)
    assert got == utc(2026, 3, 8, 12, 0)                     # 07:00 CDT
    assert got - evening == 8 * 3600                         # 9 wall-clock hours, one skipped
    assert datetime.fromtimestamp(got, CHI).utcoffset().total_seconds() == -5 * 3600
    # From inside the new offset, just after the jump.
    assert next_morning(utc(2026, 3, 8, 8, 30), CHI) == utc(2026, 3, 8, 12, 0)


def test_morning_across_fall_back():
    # 2026-11-01 02:00 CDT falls back to 01:00 CST: the night is an hour longer, ending 07:00 CST.
    evening = local(2026, 10, 31, 22, 0).timestamp()        # 03:00 UTC (CDT)
    got = next_morning(evening, CHI)
    assert got == utc(2026, 11, 1, 13, 0)                    # 07:00 CST
    assert got - evening == 10 * 3600
    # Both 01:30s of the repeated hour reach the same 07:00 CST.
    first = utc(2026, 11, 1, 6, 30)   # 01:30 CDT
    second = utc(2026, 11, 1, 7, 30)  # 01:30 CST
    assert datetime.fromtimestamp(first, CHI).hour == datetime.fromtimestamp(second, CHI).hour == 1
    assert next_morning(first, CHI) == next_morning(second, CHI) == utc(2026, 11, 1, 13, 0)


# ---------------------------------------------------------------------------- cache


def make_pauses(t=None, refresh_s=30.0):
    clock = Clock(t if t is not None else local(2026, 1, 15, 21, 0).timestamp())
    db = FakeDB(clock)
    return PushPauses(db, "America/Chicago", refresh_s, clock=clock), db, clock


def test_pause_resume_apply_at_once_without_rereading():
    pauses, db, clock = make_pauses()
    assert pauses.is_paused("garage") is False
    reads = db.reads
    p = pauses.pause("garage", "1h", "eric")
    assert p.until_ts == clock.t + 3600 and p.set_by == "eric"
    assert pauses.is_paused("garage") and db.reads == reads  # the write replaced the cache
    assert pauses.resume("garage", "eric") is True
    assert pauses.is_paused("garage") is False and db.reads == reads
    assert pauses.resume("garage", "eric") is False


def test_expiry_drops_the_pause_without_a_query():
    pauses, db, clock = make_pauses()
    pauses.active()  # first lookup loads the table once
    pauses.pause("garage", 120, "eric")
    reads = db.reads
    clock.t += 119.9
    assert pauses.is_paused("garage")
    clock.t += 0.1  # until_ts reached: expired rows are ignored
    assert pauses.is_paused("garage") is False
    assert db.reads == reads
    assert pauses.next_expiry() is None


def test_lookups_are_cached_and_refresh_picks_up_other_writers(monkeypatch):
    pauses, db, clock = make_pauses(refresh_s=30)
    mono = Clock(1000.0)
    monkeypatch.setattr("cams_record.pauses.time.monotonic", mono)
    pauses.active()
    reads = db.reads
    for _ in range(100):
        pauses.is_paused("garage")
    assert db.reads == reads
    db.rows["garage"] = {"until_ts": clock.t + 600, "set_by": "sql", "set_at": clock.t}  # another process
    assert pauses.is_paused("garage") is False
    mono.t += 30
    assert pauses.is_paused("garage") is True and db.reads == reads + 1


def test_database_error_keeps_last_known_pauses(monkeypatch):
    pauses, db, clock = make_pauses(refresh_s=1)
    mono = Clock(0.0)
    monkeypatch.setattr("cams_record.pauses.time.monotonic", mono)
    pauses.pause("garage", "1h", "eric")
    db.fail = True
    mono.t += 5
    assert pauses.is_paused("garage")  # no crash, and the error is not cached as "nothing paused"
    with pytest.raises(DBUnavailable):
        pauses.pause("front-door", "1h", "eric")
    assert pauses.is_paused("front-door") is False


def test_mode_of():
    pauses, _, clock = make_pauses()
    assert pauses.mode_of(pauses.pause("a", "1h", "x")) == "1h"
    assert pauses.mode_of(pauses.pause("a", "3h", "x")) == "3h"
    assert pauses.mode_of(pauses.pause("a", "morning", "x")) == "morning"
    assert pauses.mode_of(pauses.pause("a", 900, "x")) is None
    assert pauses.mode_of(None) is None


# ---------------------------------------------------------------------------- skip and claim


@pytest.fixture
def fake_svc(push_env):  # noqa: F811
    cfg, cams, *_ = push_env
    clock = Clock(time.time())
    db = FakeDB(clock)
    svc = Service(cfg, cams, db=db)
    svc.pauses.clock = clock
    db.subs = [sub_row("root", ["admin"]), sub_row("eric", ["cams"])]
    sent = []

    def capture(subscription, payload):
        sent.append((subscription["user_sub"], payload))
        return True, 201

    svc.push.send_raw = capture
    return svc, db, clock, sent


def event(camera, eid):
    return DetectionEvent(id=eid, camera_id=camera, label="person", score=0.9, start_ts=time.time(),
                          end_ts=None, camera_name=camera)


def test_paused_camera_is_claimed_but_not_sent(fake_svc):
    svc, db, clock, sent = fake_svc
    svc.pauses.pause("garage", "1h", "eric")
    r = svc.push.notify_event(event("garage", "1793897909-aaaaaa"))
    assert r["paused"] is True and r["sent"] == 0
    assert db.claimed == {"1793897909-aaaaaa": "garage"}  # claimed: resume releases no backlog
    assert sent == []
    # Other cameras are unaffected.
    svc.push.notify_event(event("front-door", "1793897909-bbbbbb"))
    assert sorted(u for u, _ in sent) == ["eric", "root"]


def test_resume_has_no_backlog_and_new_events_send(fake_svc):
    svc, db, clock, sent = fake_svc
    svc.pauses.pause("garage", "1h", "eric")
    svc.push.notify_event(event("garage", "1793897909-aaaaaa"))
    svc.pauses.resume("garage", "eric")
    again = svc.push.notify_event(event("garage", "1793897909-aaaaaa"))  # cams-detect retries the hook
    assert again.get("deduped") is True and sent == []
    svc.push.notify_event(event("garage", "1793897909-cccccc"))
    assert sorted(u for u, _ in sent) == ["eric", "root"]


def test_pause_expiry_restores_alerts(fake_svc):
    svc, db, clock, sent = fake_svc
    svc.pauses.pause("garage", 60, "eric")
    svc.push.notify_event(event("garage", "1793897909-000001"))
    clock.t += 61
    svc.push.notify_event(event("garage", "1793897909-000002"))
    assert {p["event_id"] for _, p in sent} == {"1793897909-000002"}


def test_test_pushes_are_exempt(fake_svc):
    svc, db, clock, sent = fake_svc
    svc.pauses.pause("garage", "1h", "eric")
    svc.pauses.pause("test", "1h", "eric")
    test_event = DetectionEvent(id="", camera_id="test", label="test", score=None, start_ts=0.0, end_ts=None,
                                camera_name="Cameras")
    assert svc.push.notify_user("eric", test_event, test=True)["sent"] == 1
    r = svc.push.notify_event(event("garage", "1793897909-dddddd"), test=True)
    assert r.get("paused") is None and len(sent) == 3
    assert db.claimed == {}  # a test push never claims


def test_poller_run_once_skips_and_claims(fake_svc):
    svc, db, clock, sent = fake_svc
    notifier = PushNotifier(svc.push, db, {**svc.cfg["push"], "poll_enabled": True})
    now = time.time()
    db.events = [{"id": "1793897909-p00001", "camera_id": "garage", "label": "person", "score": 0.8,
                  "start_ts": now, "end_ts": None},
                 {"id": "1793897909-p00002", "camera_id": "front-door", "label": "car", "score": 0.7,
                  "start_ts": now, "end_ts": None}]
    svc.pauses.pause("garage", "3h", "eric")
    notifier.run_once()
    assert set(db.claimed) == {"1793897909-p00001", "1793897909-p00002"}
    assert {p["camera_id"] for _, p in sent} == {"front-door"}
    svc.pauses.resume("garage", "eric")
    sent.clear()
    notifier.run_once()  # the paused camera's event is already claimed: nothing is released on resume
    assert sent == []


def test_webhook_skips_paused_camera_and_claims(fake_svc):
    svc, db, clock, sent = fake_svc
    client = TestClient(create_app(svc, svc.cfg))
    svc.pauses.pause("front-door", "1h", "eric")
    body = {"id": "1793897909-w00001", "camera_id": "front-door", "label": "person", "max_score": 0.9,
            "start_ts": time.time()}
    r = client.post("/cams-record/api/internal/detection-event", headers={"x-cams-push-secret": WEBHOOK}, json=body)
    assert r.status_code == 200 and r.json()["paused"] is True and r.json()["sent"] == 0
    assert "1793897909-w00001" in db.claimed and sent == []
    client.delete("/cams-record/api/push/pauses/front-door", headers={"x-apphub-token": tok(sub="root",
                                                                                             roles=["admin"])})
    r = client.post("/cams-record/api/internal/detection-event", headers={"x-cams-push-secret": WEBHOOK}, json=body)
    assert r.json().get("deduped") is True and sent == []


# ---------------------------------------------------------------------------- API

REGISTRY = [{"id": "open", "name": "Open"},
            {"id": "family", "name": "Family", "roles": ["family"]},
            {"id": "private", "name": "Private", "users": ["ann"]},
            {"id": "shared", "name": "Shared", "users": ["ann", "bob"]}]
# Routes that take a camera and are authorized here, not through CAMERA_ROUTES (test_camera_access.py).
PAUSE_ROUTES = {"/cams-record/api/push/pauses": ("GET",),
                "/cams-record/api/push/pauses/{cam}": ("PUT", "DELETE")}


@pytest.fixture
def api(push_env):  # noqa: F811
    cfg, _, *_ = push_env
    with open(cfg["access"]["registry_file"], "w", encoding="utf-8") as fh:
        json.dump(REGISTRY, fh)
    cams = {c["id"]: resolve_camera(c["id"], {}) for c in REGISTRY}
    clock = Clock(local(2026, 1, 15, 21, 40).timestamp())
    db = FakeDB(clock)
    svc = Service(cfg, cams, db=db)
    svc.pauses.clock = clock
    return TestClient(create_app(svc, cfg)), svc, db, clock


def h(user="bob", roles=("cams",), **extra):
    return {"x-apphub-token": tok(sub=user, roles=list(roles)), **extra}


URL = "/cams-record/api/push/pauses"


def test_list_shows_only_visible_cameras(api):
    client, svc, db, clock = api
    svc.pauses.pause("private", "1h", "ann")
    bob = client.get(URL, headers=h("bob"))
    assert bob.status_code == 200 and bob.headers["cache-control"] == "private, no-store"
    assert [c["camera_id"] for c in bob.json()["cameras"]] == ["open", "shared"]  # never private/family
    assert "private" not in bob.text and "ann" not in bob.text
    ann = client.get(URL, headers=h("ann", ("cams", "family"))).json()
    by = {c["camera_id"]: c for c in ann["cameras"]}
    assert set(by) == {"open", "family", "private", "shared"}
    assert by["private"]["paused"] is True and by["private"]["can_edit"] is True
    assert by["private"]["until"] == "2026-01-15T22:40:00-06:00" and by["private"]["mode"] == "1h"
    assert by["private"]["set_by"] == "ann"
    assert by["family"]["can_edit"] is False and by["open"]["can_edit"] is False  # a role grant only views
    assert ann["timezone"] == "America/Chicago"
    assert ann["morning_ts"] == local(2026, 1, 16, 7, 0).timestamp()
    admin = client.get(URL, headers=h("root", ("admin",))).json()
    assert all(c["can_edit"] for c in admin["cameras"]) and len(admin["cameras"]) == 4


def test_writes_need_owner_or_admin(api):
    client, svc, db, clock = api
    assert client.put(URL + "/open", headers=h("bob"), json={"duration": "1h"}).status_code == 403
    carol = h("carol", ("cams", "family"))
    assert client.put(URL + "/family", headers=carol, json={"duration": "1h"}).status_code == 403
    assert client.delete(URL + "/open", headers=h("bob")).status_code == 403
    assert db.writes == 0
    r = client.put(URL + "/shared", headers=h("bob"), json={"duration": "3h"})
    assert r.status_code == 200 and r.json()["paused"] is True and r.json()["mode"] == "3h"
    r = client.put(URL + "/open", headers=h("root", ("admin",)), json={"duration": "morning"})
    assert r.status_code == 200 and r.json()["until"] == "2026-01-16T07:00:00-06:00"
    assert svc.pauses.is_paused("open") and svc.pauses.is_paused("shared")
    r = client.delete(URL + "/shared", headers=h("ann"))
    assert r.status_code == 200 and r.json()["paused"] is False
    assert not svc.pauses.is_paused("shared")


@pytest.mark.parametrize("cam", ["private", "unregistered"])
def test_hidden_camera_matches_unknown_and_writes_nothing(api, cam):
    client, svc, db, clock = api
    put = client.put(URL + f"/{cam}", headers=h("bob"), json={"duration": "1h"})
    delete = client.delete(URL + f"/{cam}", headers=h("bob"))
    assert put.status_code == delete.status_code == 404
    assert put.json() == delete.json() == {"detail": "camera not found"}
    assert db.writes == 0


def test_claimed_user_must_match_token(api):
    client, svc, db, clock = api
    r = client.put(URL + "/private", headers=h("ann", **{"x-apphub-user": "mallory"}), json={"duration": "1h"})
    assert r.status_code == 404 and db.writes == 0
    assert client.get(URL, headers=h("ann", **{"x-apphub-user": "mallory"})).json()["cameras"] == []


def test_requires_authentication(api):
    client, *_ = api
    assert client.get(URL).status_code == 401
    assert client.put(URL + "/open", json={"duration": "1h"}).status_code == 401
    assert client.delete(URL + "/open").status_code == 401
    assert client.get(URL, headers={"x-apphub-token": tok(roles=["viewer"])}).status_code == 403


@pytest.mark.parametrize("body", [{"duration": 0}, {"duration": -5}, {"duration": "2h"}, {"duration": None},
                                  {"duration": True}, {"duration": MAX_PAUSE_S + 1}, {}, {"duration": "1h", "x": 1},
                                  ["1h"], "1h"])
def test_invalid_bodies(api, body):
    client, svc, db, clock = api
    r = client.put(URL + "/private", headers=h("ann"), json=body)
    assert r.status_code == 422, r.text
    assert db.writes == 0


def test_csrf_conventions(api):
    client, svc, db, clock = api
    form = client.put(URL + "/private", headers=h("ann"), data={"duration": "1h"})
    assert form.status_code == 415
    text = client.put(URL + "/private", headers=h("ann", **{"content-type": "text/plain"}), content='{"duration":"1h"}')
    assert text.status_code == 415
    cross = client.put(URL + "/private", headers=h("ann", **{"sec-fetch-site": "cross-site"}), json={"duration": "1h"})
    assert cross.status_code == 403
    assert client.delete(URL + "/private", headers=h("ann", **{"sec-fetch-site": "same-site"})).status_code == 403
    bad_json = client.put(URL + "/private", headers=h("ann", **{"content-type": "application/json"}), content="{")
    assert bad_json.status_code == 400
    assert db.writes == 0
    ok = client.put(URL + "/private", headers=h("ann", **{"sec-fetch-site": "same-origin"}), json={"duration": 600})
    assert ok.status_code == 200 and ok.json()["until_ts"] == clock.t + 600


def test_invalid_camera_id(api):
    client, svc, db, clock = api
    assert client.put(URL + "/Bad%20Id", headers=h("root", ("admin",)), json={"duration": "1h"}).status_code == 400
    assert client.delete(URL + "/" + "a" * 70, headers=h("root", ("admin",))).status_code == 400


def test_database_down(api):
    client, svc, db, clock = api
    db.fail = True
    r = client.put(URL + "/private", headers=h("ann"), json={"duration": "1h"})
    assert r.status_code == 503
    assert client.delete(URL + "/private", headers=h("ann")).status_code == 503


def test_no_database_answers_503(push_env):  # noqa: F811
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=None)
    client = TestClient(create_app(svc, cfg))
    assert svc.pauses is None
    assert client.get(URL, headers=h("eric")).status_code == 503
    assert client.put(URL + "/garage", headers=h("eric"), json={"duration": "1h"}).status_code == 503


# ---------------------------------------------------------------------------- MariaDB


def test_push_pauses_table(db):
    from cams_record.db import MIGRATIONS

    assert db.schema_version == MIGRATIONS[-1][0] >= 8
    now = time.time()
    db.set_push_pause("garage", now + 3600, "eric", now)
    db.set_push_pause("shed", now - 1, "eric", now - 60)  # already expired
    rows = db.active_push_pauses()
    assert [r["camera_id"] for r in rows] == ["garage"]
    assert rows[0]["until_ts"] == pytest.approx(now + 3600, abs=0.002) and rows[0]["set_by"] == "eric"
    db.set_push_pause("garage", now + 60, "ann", now)  # replace, not duplicate
    assert [(r["camera_id"], r["set_by"]) for r in db.active_push_pauses()] == [("garage", "ann")]
    # A later write sweeps expired rows; expired rows were ignored either way.
    left = db.run(lambda cur: (cur.execute("SELECT camera_id FROM push_pauses ORDER BY camera_id"), cur.fetchall())[1])
    assert [r[0] for r in left] == ["garage"]
    assert db.clear_push_pause("garage") == 1 and db.clear_push_pause("garage") == 0
    assert db.active_push_pauses() == []


def test_push_pauses_migration_is_idempotent(db):
    db.set_push_pause("garage", time.time() + 600, "eric", time.time())
    db.run(lambda cur: cur.execute("DELETE FROM schema_version WHERE version >= 8"))
    db.ready = False
    db.ensure_schema()  # CREATE TABLE IF NOT EXISTS: a re-run keeps existing pauses
    assert [r["camera_id"] for r in db.active_push_pauses()] == ["garage"]


def test_pause_with_real_database(db, push_env):  # noqa: F811
    cfg, cams, *_ = push_env
    svc = Service(cfg, cams, db=db)
    sent = []
    svc.push.send_raw = lambda s, p: (sent.append(p), (True, 201))[1]
    from cams_record.push import validate_subscription
    from tests.test_push import _sub_body

    db.upsert_push_subscription("root", validate_subscription(_sub_body()), roles=["admin"])
    svc.pauses.pause("front-door", "1h", "root")
    r = svc.push.notify_event(event("front-door", "1793897909-db0001"))
    assert r["paused"] is True and sent == []
    assert db.claim_push_event("1793897909-db0001", "front-door") is False  # it was claimed
    svc.pauses.resume("front-door", "root")
    assert svc.push.notify_event(event("front-door", "1793897909-db0002"))["sent"] == 1


# ---------------------------------------------------------------------------- MQTT


class FakeInfo:
    def wait_for_publish(self, timeout=None):
        return True


class FakeClient:
    def __init__(self):
        self.published: list[tuple[str, str, int, bool]] = []
        self.subscribed: list[str] = []
        self.calls: list[str] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return FakeInfo()

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def connect_async(self, host, port, keepalive=60):
        self.calls.append(f"connect {host}:{port}")

    def loop_start(self):
        self.calls.append("loop_start")

    def loop_stop(self):
        self.calls.append("loop_stop")

    def disconnect(self):
        self.calls.append("disconnect")

    def last(self, topic):
        return next((p for t, p, _, _ in reversed(self.published) if t == topic), None)


class Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload.encode(), retain


class OK:
    is_failure = False


@pytest.fixture
def bridge():
    pauses, db, clock = make_pauses()
    cams = {"front-door": "Front Door", "garage": "Garage"}
    cfg = validate(deep_merge(DEFAULTS, {"mqtt": {"enabled": True}}))["mqtt"]
    client = FakeClient()
    b = mq.AlertPauseMqtt(cfg, pauses, lambda: dict(cams), client=client)
    b.client.on_connect = b._on_connect
    b._on_connect(client, None, None, OK())
    return b, client, pauses, clock, cams


def test_mqtt_disabled_without_config():
    cfg = validate(deep_merge(DEFAULTS, {}))
    assert cfg["mqtt"]["enabled"] is False
    pauses, _, _ = make_pauses()
    assert mq.build(cfg, pauses, dict) is None
    enabled = validate(deep_merge(DEFAULTS, {"mqtt": {"enabled": True}}))
    assert mq.build(enabled, None, dict) is None  # no database: nothing to pause


def test_mqtt_never_uses_cams_detect_namespace():
    for base in ("cams-detect", "CAMS-DETECT", "cams-detect2"):
        with pytest.raises(ValueError, match="cams-detect"):
            validate(deep_merge(DEFAULTS, {"mqtt": {"base_topic": base}}))
    for bad in ("a/b", "#", "+", "", "x" * 70):
        with pytest.raises(ValueError, match="mqtt.base_topic"):
            validate(deep_merge(DEFAULTS, {"mqtt": {"base_topic": bad}}))
    with pytest.raises(ValueError, match="switch_on"):
        validate(deep_merge(DEFAULTS, {"mqtt": {"switch_on": "forever"}}))
    with pytest.raises(ValueError, match="port"):
        validate(deep_merge(DEFAULTS, {"mqtt": {"port": 0}}))


def test_mqtt_env_overrides():
    from cams_record.config import apply_env

    cfg = validate(apply_env(deep_merge(DEFAULTS, {}), {"MQTT_ENABLED": "1", "MQTT_HOST": "10.0.0.5",
                                                         "MQTT_USERNAME": "cams-record"}))
    assert cfg["mqtt"]["enabled"] is True and cfg["mqtt"]["host"] == "10.0.0.5"
    assert cfg["mqtt"]["password_file"] == "/run/secrets/mqtt_password"


def test_mqtt_startup_discovery_and_retained_state(bridge):
    b, client, pauses, clock, cams = bridge
    assert client.subscribed == ["cams-record/+/pause/set", "homeassistant/status"]
    assert ("cams-record/status", "online", 1, True) in client.published
    assert all(retain for _, _, _, retain in client.published)
    assert not [t for t, *_ in client.published if t.startswith("cams-detect")]
    assert not [t for t in client.subscribed if t.startswith("cams-detect")]
    configs = {t: json.loads(p) for t, p, _, _ in client.published if t.endswith("/config")}
    assert set(configs) == {
        "homeassistant/switch/cams_record/front_door_alerts_paused/config",
        "homeassistant/sensor/cams_record/front_door_alerts_paused_until/config",
        "homeassistant/select/cams_record/front_door_alerts_pause/config",
        "homeassistant/switch/cams_record/garage_alerts_paused/config",
        "homeassistant/sensor/cams_record/garage_alerts_paused_until/config",
        "homeassistant/select/cams_record/garage_alerts_pause/config",
    }
    sw = configs["homeassistant/switch/cams_record/front_door_alerts_paused/config"]
    assert sw["default_entity_id"] == "switch.front_door_alerts_paused"
    assert sw["command_topic"] == "cams-record/front-door/pause/set"
    assert sw["state_topic"] == "cams-record/front-door/pause/state"
    assert sw["availability"] == [{"topic": "cams-record/status"}]
    assert sw["unique_id"] == "cams_record_front_door_alerts_paused"
    until = configs["homeassistant/sensor/cams_record/garage_alerts_paused_until/config"]
    assert until["default_entity_id"] == "sensor.garage_alerts_paused_until" and until["device_class"] == "timestamp"
    sel = configs["homeassistant/select/cams_record/garage_alerts_pause/config"]
    assert sel["default_entity_id"] == "select.garage_alerts_pause"
    assert sel["options"] == ["Off", "1 h", "3 h", "Until morning"]
    assert sel["command_topic"] == "cams-record/garage/pause/set"
    assert client.last("cams-record/garage/pause/state") == "OFF"
    assert client.last("cams-record/garage/pause/until") == "None"
    assert client.last("cams-record/garage/pause/option") == "Off"


@pytest.mark.parametrize(("payload", "option", "seconds"), [
    ("1 h", "1 h", 3600), ("3 h", "3 h", 3 * 3600), ("ON", "1 h", 3600), ("on", "1 h", 3600),
    ("1h", "1 h", 3600), ("900", "None", 900),
])
def test_mqtt_pause_commands(bridge, payload, option, seconds):
    b, client, pauses, clock, cams = bridge
    assert b.handle_command("cams-record/garage/pause/set", payload) == "paused"
    assert pauses.get("garage").until_ts == clock.t + seconds and pauses.get("garage").set_by == "mqtt"
    assert client.last("cams-record/garage/pause/state") == "ON"
    assert client.last("cams-record/garage/pause/option") == option
    assert client.last("cams-record/garage/pause/until") == pauses.iso(clock.t + seconds)


def test_mqtt_until_morning_and_resume(bridge):
    b, client, pauses, clock, cams = bridge
    assert b.handle_command("cams-record/front-door/pause/set", "Until morning") == "paused"
    assert client.last("cams-record/front-door/pause/until") == "2026-01-16T07:00:00-06:00"
    assert client.last("cams-record/front-door/pause/option") == "Until morning"
    for off in ("OFF", "Off"):
        pauses.pause("front-door", "1h", "x")
        assert b.handle_command("cams-record/front-door/pause/set", off) == "resumed"
        assert not pauses.is_paused("front-door")
        assert client.last("cams-record/front-door/pause/state") == "OFF"


@pytest.mark.parametrize("payload", ["", "bogus", "0", "-60", "2 h", "999999999", "x" * 100, "1.5"])
def test_mqtt_invalid_commands_change_nothing_and_restate(bridge, payload):
    b, client, pauses, clock, cams = bridge
    pauses.pause("garage", "3h", "eric")
    before = pauses.get("garage")
    client.published.clear()
    assert b.handle_command("cams-record/garage/pause/set", payload) == "invalid"
    assert pauses.get("garage") == before
    assert client.last("cams-record/garage/pause/state") == "ON"  # HA's control snaps back


def test_mqtt_ignores_unknown_cameras_topics_and_retained(bridge):
    b, client, pauses, clock, cams = bridge
    assert b.handle_command("cams-record/shed/pause/set", "1 h") == "ignored"
    assert b.handle_command("cams-detect/garage/detection/set", "OFF") == "ignored"
    assert b.handle_command("cams-record/garage/pause/set/extra", "1 h") == "ignored"
    assert b.handle_command("cams-record/garage/other/set", "1 h") == "ignored"
    b._on_message(client, None, Msg("cams-record/garage/pause/set", "1 h", retain=True))
    assert pauses.active() == {}
    b._on_message(client, None, Msg("cams-record/garage/pause/set", "1 h"))
    assert pauses.is_paused("garage")


def test_mqtt_follows_api_changes_and_expiry(bridge):
    b, client, pauses, clock, cams = bridge
    pauses.pause("garage", 120, "ann")  # e.g. through the HTTP API
    assert client.last("cams-record/garage/pause/state") == "ON"
    assert pauses.next_expiry() == clock.t + 120
    clock.t += 121
    b.publish_all()  # what the worker does when the expiry arrives
    assert client.last("cams-record/garage/pause/state") == "OFF"
    assert client.last("cams-record/garage/pause/until") == "None"


def test_mqtt_worker_publishes_expiry_promptly():
    pauses, db, _ = make_pauses(t=time.time())
    pauses.clock = time.time
    db.clock = time.time
    cfg = validate(deep_merge(DEFAULTS, {"mqtt": {"enabled": True, "refresh_s": 30}}))["mqtt"]
    client = FakeClient()
    b = mq.AlertPauseMqtt(cfg, pauses, lambda: {"garage": "Garage"}, client=client)
    b.start()
    b._on_connect(client, None, None, OK())
    pauses.pause("garage", 1, "ann")
    deadline = time.time() + 5
    while client.last("cams-record/garage/pause/state") != "OFF" and time.time() < deadline:
        time.sleep(0.05)
    try:
        assert client.last("cams-record/garage/pause/state") == "OFF"  # well before refresh_s
    finally:
        b.stop()
    assert client.published[-1][:2] == ("cams-record/status", "offline")
    assert "disconnect" in client.calls and "loop_stop" in client.calls


def test_mqtt_camera_removal_and_rename(bridge):
    b, client, pauses, clock, cams = bridge
    client.published.clear()
    del cams["garage"]
    cams["front-door"] = "Front Porch"
    b.publish_all()
    removed = {t for t, p, _, _ in client.published if p == "" and t.endswith("/config")}
    assert removed == {"homeassistant/switch/cams_record/garage_alerts_paused/config",
                       "homeassistant/sensor/cams_record/garage_alerts_paused_until/config",
                       "homeassistant/select/cams_record/garage_alerts_pause/config"}
    assert client.last("cams-record/garage/pause/state") == ""  # retained state cleared
    renamed = json.loads(client.last("homeassistant/switch/cams_record/front_door_alerts_paused/config"))
    assert renamed["device"]["name"] == "Front Porch"


def test_mqtt_reconnect_and_ha_restart_resend(bridge):
    b, client, pauses, clock, cams = bridge
    b._on_disconnect(client, None, None, "lost")
    assert b.connected is False
    pauses.pause("garage", "1h", "eric")  # while offline: nothing can be sent
    client.published.clear()
    b._on_connect(client, None, None, OK())
    assert client.last("cams-record/garage/pause/state") == "ON"
    assert client.last("homeassistant/select/cams_record/garage_alerts_pause/config")
    b._on_message(client, None, Msg("homeassistant/status", "online"))
    assert b._resend_at is not None
    fail = type("RC", (), {"is_failure": True})()
    b.connected = False
    b._on_connect(client, None, None, fail)
    assert b.connected is False


def test_mqtt_service_wiring(push_env, monkeypatch):  # noqa: F811
    cfg, cams, *_ = push_env
    cfg["mqtt"] = validate(deep_merge(DEFAULTS, {"mqtt": {"enabled": True, "username": "cams-record",
                                                          "password_file": "/nonexistent/mqtt_password"}}))["mqtt"]
    svc = Service(cfg, cams, db=FakeDB(Clock(time.time())))
    assert isinstance(svc.mqtt, mq.AlertPauseMqtt)
    assert svc.alert_cameras() == {"front-door": "Front Door", "garage": "Garage"}
    assert Service(cfg, cams, db=None).mqtt is None
