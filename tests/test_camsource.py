"""camera_source shadow/db: CameraSource, shadow differences, hot recorder apply, registry publishing,
startup choice and the export-files rollback path."""

from __future__ import annotations

import json
import os

import pytest
import yaml
from conftest import CFG_DB
from fastapi.testclient import TestClient

from cams_record import service as service_mod
from cams_record.camconfig.compare import record_differences
from cams_record.camconfig.importer import build_plan, check_plan
from cams_record.camconfig.model import CameraObject, Stream
from cams_record.camconfig.publish import publish_registry, registry_text
from cams_record.camconfig.source import CameraSource
from cams_record.camconfig.views import record_camera
from cams_record.config import DEFAULTS, deep_merge, resolve_camera, validate
from cams_record.db import DBUnavailable
from cams_record.service import Service
from cams_record.web import create_app


def cam(cid="front-door", *, record=("main", "sub"), recording=None, visibility="everyone", **kw):
    use = {"live": ("sub",), "detect": ("sub",), "verify": ("main",)}
    if record:
        use["record"] = tuple(record)
    return CameraObject(id=cid, name=cid.title(), visibility=visibility, recording=recording, use=use,
                        streams={"main": Stream("main", f"{cid}-main"), "sub": Stream("sub", cid)}, **kw)


def file_cam(cid="front-door", **raw):
    return resolve_camera(cid, {"main_stream": f"{cid}-main", "sub_stream": cid, **raw})


class FlakyStore:
    """Stands in for CamConfigStore; flips between working and DB down."""

    def __init__(self, cams=None, rev=1):
        self.cams, self.rev, self.down = dict(cams or {}), rev, False

    def revision(self):
        if self.down:
            raise DBUnavailable("connection refused")
        return self.rev

    def load_all(self):
        if self.down:
            raise DBUnavailable("connection refused")
        return self.rev, {k: CameraObject.from_dict(v.to_dict()) for k, v in self.cams.items()}


# ---- CameraSource ---------------------------------------------------------------------

def test_source_loads_polls_and_caches(tmp_path):
    store = FlakyStore({"front-door": cam()})
    src = CameraSource({}, tmp_path / "cache.json", store=store)
    assert src.load_initial() and src.origin == "db" and src.revision == 1
    assert json.loads((tmp_path / "cache.json").read_text())["revision"] == 1
    assert src.poll() is False  # same revision: nothing reloaded

    store.cams["garage"], store.rev = cam("garage"), 2
    assert src.poll() is True and set(src.cameras) == {"front-door", "garage"}

    store.rev = 3  # revision bumped but nothing that matters changed
    assert src.poll() is False and src.revision == 3


def test_source_keeps_cameras_while_db_down_and_recovers(tmp_path, caplog):
    store = FlakyStore({"front-door": cam()})
    src = CameraSource({}, tmp_path / "cache.json", store=store)
    src.load_initial()
    store.down = True
    with caplog.at_level("ERROR"):
        assert src.poll() is False and src.poll() is False
    assert set(src.cameras) == {"front-door"} and "connection refused" in src.status()["error"]
    assert sum("database unavailable" in r.getMessage() for r in caplog.records) == 1  # logged once
    store.down, store.rev = False, 2
    store.cams["garage"] = cam("garage")
    assert src.poll() is True and src.status()["error"] is None


def test_source_starts_from_cache_when_db_down(tmp_path):
    store = FlakyStore({"front-door": cam(recording={"main_days": 7})})
    CameraSource({}, tmp_path / "cache.json", store=store).load_initial()
    store.down = True
    src = CameraSource({}, tmp_path / "cache.json", store=store)
    assert src.load_initial() and src.origin == "cache"
    assert src.cameras["front-door"].to_dict() == cam(recording={"main_days": 7}).to_dict()
    store.down = False
    assert src.poll() is False  # back on the database; same cameras, so nothing to apply
    assert src.origin == "db"


def test_source_without_db_or_cache(tmp_path):
    store = FlakyStore()
    store.down = True
    src = CameraSource({}, tmp_path / "cache.json", store=store)
    assert src.load_initial() is False and src.origin is None
    (tmp_path / "cache.json").write_text("{broken")
    assert src.load_initial() is False


def test_poll_first_empty_db_load_is_changed(tmp_path, fake_recorders):
    """Starting with no DB/cache, the first successful load of an empty authoritative DB must apply."""
    store = FlakyStore()  # empty cams_config, revision 1
    store.down = True
    src = CameraSource({}, None, store=store)
    assert not src.load_initial() and src.origin is None and src.cameras == {}

    cfg = make_cfg(tmp_path, camera_source="db")
    svc = Service(cfg, {"front-door": file_cam()}, source=src)
    svc.on_camera_objects()  # nothing loaded: keep recording from cameras.yaml
    assert set(svc.recorders) == {"front-door/main", "front-door/sub"}

    store.down = False
    assert src.poll() is True  # empty→empty still counts as the first authoritative load
    assert src.origin == "db" and src.cameras == {}
    svc.on_camera_objects()
    assert svc.recorders == {}  # file fallback applied away


# ---- shadow differences ---------------------------------------------------------------

def test_record_differences():
    objects = {"front-door": cam(), "garage": cam("garage", record=("main",))}
    files = {"front-door": file_cam(), "garage": file_cam("garage", record_sub=False)}
    assert record_differences(files, {}, objects) == []

    files["garage"] = file_cam("garage")
    assert any("garage: records" in d for d in record_differences(files, {}, objects))
    files["garage"] = file_cam("garage", record_sub=False)
    files["shed"] = file_cam("shed")
    assert any("shed: only in cameras.yaml" in d for d in record_differences(files, {}, objects))
    del files["shed"]
    objects["porch"] = cam("porch")
    assert any("porch: only in cams_config, would record" in d for d in record_differences(files, {}, objects))
    objects["porch"] = cam("porch", record=())
    assert record_differences(files, {}, objects) == []  # known but not recorded: no change
    objects["front-door"] = cam(recording={"main_days": 7})
    assert any("retention" in d for d in record_differences(files, {}, objects))
    assert record_differences(files, {"front-door": {"main_days": 7}}, objects) == []
    objects["front-door"].streams["main"] = Stream("main", "front-door-4k")
    assert any("streams main='front-door-main'" in d for d in
               record_differences(files, {"front-door": {"main_days": 7}}, objects))


# ---- Service ---------------------------------------------------------------------------

@pytest.fixture
def fake_recorders(monkeypatch):
    events = []

    class FakeRecorder:
        def __init__(self, camera_id, quality, url, *_a, **_k):
            self.camera_id, self.quality, self.url = camera_id, quality, url
            self.key = f"{camera_id}/{quality}"

        def start(self):
            events.append(("start", self.key, self.url))

        def stop(self):
            events.append(("stop", self.key, self.url))

        def join(self, timeout=None):
            events.append(("join", self.key, self.url))

        def status(self):
            return {"mb_per_hour": 0.0}

    monkeypatch.setattr(service_mod, "StreamRecorder", FakeRecorder)
    return events


def make_cfg(tmp_path, **over):
    reg = tmp_path / "registry"
    reg.mkdir(exist_ok=True)
    base = {"recording": {"root": str(tmp_path / "nvr"), "state_dir": str(tmp_path / "state")},
            "access": {"registry_file": str(reg / "cams.json")}}
    return validate(deep_merge(deep_merge(DEFAULTS, base), over))


def test_apply_cameras_touches_only_changed_streams(tmp_path, fake_recorders):
    cfg = make_cfg(tmp_path)
    svc = Service(cfg, {"front-door": file_cam(), "garage": file_cam("garage")})
    svc._started = True  # as after start(), without starting the other threads
    garage_sub = svc.recorders["garage/sub"]

    new = {"front-door": file_cam(main_stream="front-door-4k"),  # main changed
           "garage": file_cam("garage"),  # unchanged
           "porch": file_cam("porch", record_sub=False)}  # added
    result = svc.apply_cameras(new, {"porch": {"main_days": 9}})
    assert result == {"stopped": ["front-door/main"], "started": ["front-door/main", "porch/main"]}
    kinds = [(e[0], e[1]) for e in fake_recorders]
    assert kinds.index(("join", "front-door/main")) < kinds.index(("start", "front-door/main"))  # old stopped first
    assert svc.recorders["front-door/main"].url.endswith("front-door-4k")
    assert svc.recorders["garage/sub"] is garage_sub
    assert svc.access.sources["porch"] == ("porch-main", "porch")
    assert svc.janitor.overrides == {"porch": {"main_days": 9}}

    fake_recorders.clear()
    assert svc.apply_cameras(new, {"porch": {"main_days": 9}}) == {"stopped": [], "started": []}
    assert fake_recorders == []

    svc.apply_cameras({"garage": file_cam("garage")}, {})
    assert set(svc.recorders) == {"garage/main", "garage/sub"}
    assert "porch" not in svc.access.sources


def test_apply_before_start_does_not_start_recorders(tmp_path, fake_recorders):
    svc = Service(make_cfg(tmp_path), {})
    svc.apply_cameras({"garage": file_cam("garage")}, {})
    assert set(svc.recorders) == {"garage/main", "garage/sub"}
    assert not [e for e in fake_recorders if e[0] == "start"]


def test_shadow_reports_and_never_changes_recording(tmp_path, fake_recorders, caplog):
    cfg = make_cfg(tmp_path, camera_source="shadow")
    store = FlakyStore({"front-door": cam(), "garage": cam("garage")})
    src = CameraSource({}, None, store=store)
    src.load_initial()
    files = {"front-door": file_cam()}
    svc = Service(cfg, files, source=src, file_cameras=files)
    with caplog.at_level("WARNING"):
        svc.on_camera_objects()
    assert svc.shadow_diffs == ["garage: only in cams_config, would record [('main', 'garage-main'), "
                                "('sub', 'garage')]"]
    assert any("1 differences" in r.getMessage() for r in caplog.records)
    assert set(svc.recorders) == {"front-door/main", "front-door/sub"}  # untouched
    status = svc.camera_source_status()
    assert status["mode"] == "shadow" and status["origin"] == "db" and len(status["differences"]) == 1

    del store.cams["garage"]
    store.rev = 2
    assert src.poll()
    svc.on_camera_objects()
    assert svc.shadow_diffs == []


def test_db_mode_applies_and_publishes_registry(tmp_path, fake_recorders):
    cfg = make_cfg(tmp_path, camera_source="db", access={"publish_registry": True})
    store = FlakyStore({"front-door": cam(), "garage": cam("garage", visibility="restricted", roles=("family",))})
    src = CameraSource({}, None, store=store)
    src.load_initial()
    svc = Service(cfg, {}, source=src)
    svc.on_camera_objects()
    assert set(svc.recorders) == {"front-door/main", "front-door/sub", "garage/main", "garage/sub"}
    reg = json.loads(open(cfg["access"]["registry_file"]).read())
    assert reg == [{"id": "front-door", "name": "Front-Door"},
                   {"id": "garage", "name": "Garage", "roles": ["family"]}]
    assert svc.camera_source_status()["registry"] == {"enabled": True, "error": None, "published_revision": 1}

    inode = os.stat(cfg["access"]["registry_file"]).st_ino
    store.rev = 2
    src.poll()
    svc.on_camera_objects()
    assert os.stat(cfg["access"]["registry_file"]).st_ino == inode  # same content: not rewritten


def test_publish_refuses_to_drop_a_camera_and_keeps_old_file(tmp_path):
    path = tmp_path / "cams.json"
    path.write_text("[]")
    bad = cam("garage")
    bad.streams["snap"] = Stream("snap", "has space")
    bad.use["snapshot"] = ("snap",)
    with pytest.raises(ValueError):
        publish_registry(path, {"garage": bad})
    assert path.read_text() == "[]"
    assert publish_registry(path, {"garage": cam("garage")}) is True
    assert publish_registry(path, {"garage": cam("garage")}) is False
    assert json.loads(registry_text({"garage": cam("garage")})) == json.loads(path.read_text())


def test_registry_retries_after_transient_failure_without_revision_change(tmp_path, fake_recorders, monkeypatch):
    """A failed cams.json write must retry on the next poll even when the revision is unchanged."""
    from cams_record.camconfig import publish as publish_mod

    cfg = make_cfg(tmp_path, camera_source="db", access={"publish_registry": True})
    # Revocation: garage was public, now restricted to family only.
    store = FlakyStore({"garage": cam("garage", visibility="restricted", roles=("family",))})
    src = CameraSource({}, None, store=store)
    src.load_initial()
    svc = Service(cfg, {}, source=src)
    reg_path = cfg["access"]["registry_file"]
    with open(reg_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps([{"id": "garage", "name": "Garage"}]) + "\n")  # stale open grant

    calls = {"n": 0}
    real = publish_mod.publish_registry

    def flaky(path, objects):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real(path, objects)

    # on_camera_objects imports publish_registry from this module at call time.
    monkeypatch.setattr(publish_mod, "publish_registry", flaky)

    svc.on_camera_objects()
    assert calls["n"] == 1
    assert svc.registry_status["error"] == "disk full"
    assert svc.registry_status["published_revision"] is None
    assert json.loads(open(reg_path).read()) == [{"id": "garage", "name": "Garage"}]  # stale file kept
    assert set(svc.recorders) == {"garage/main", "garage/sub"}  # cameras still applied

    assert src.poll() is False  # no config revision change
    assert svc._registry_needs_retry() is True
    svc.on_camera_objects()  # what _source_loop does when retry is pending
    assert calls["n"] == 2
    assert svc.registry_status == {"enabled": True, "error": None, "published_revision": 1}
    assert json.loads(open(reg_path).read()) == [{"id": "garage", "name": "Garage", "roles": ["family"]}]
    assert svc._registry_needs_retry() is False


def test_status_endpoint_reports_camera_source(tmp_path, fake_recorders):
    cfg = make_cfg(tmp_path, camera_source="shadow", auth={"mode": "none"})
    src = CameraSource({}, None, store=FlakyStore({"front-door": cam()}))
    src.load_initial()
    svc = Service(cfg, {"front-door": file_cam()}, source=src)
    svc.on_camera_objects()
    body = TestClient(create_app(svc, cfg)).get("/cams-record/api/status").json()
    assert body["camera_source"]["mode"] == "shadow" and body["camera_source"]["differences"] == []


# ---- startup choice (__main__.camera_source) ---------------------------------------------

def test_startup_camera_choice(tmp_path, monkeypatch):
    import logging

    from cams_record import __main__ as main_mod
    from cams_record.camconfig import source as source_mod

    store = FlakyStore({"garage": cam("garage", recording={"sub_days": 30})})
    monkeypatch.setattr(source_mod, "CamConfigStore", lambda db: store)
    monkeypatch.setattr(source_mod, "open_db", lambda cfg: None)
    files = {"front-door": file_cam()}
    log = logging.getLogger("test")

    cfg = make_cfg(tmp_path, camera_source="shadow")
    src, cams = main_mod.camera_source(cfg, files, log)
    assert cams is files and src.origin == "db"

    cfg = make_cfg(tmp_path, camera_source="db")
    src, cams = main_mod.camera_source(cfg, files, log)
    assert set(cams) == {"garage"} and cams["garage"] == record_camera(store.cams["garage"])
    assert cfg["retention"]["cameras"] == {"garage": {"sub_days": 30}}

    store.down = True  # cache written above by the db-mode load
    cfg = make_cfg(tmp_path, camera_source="db")
    src, cams = main_mod.camera_source(cfg, files, log)
    assert set(cams) == {"garage"} and src.origin == "cache"

    os.unlink(cfg["cams_config"]["cache_file"])
    cfg = make_cfg(tmp_path, camera_source="db")
    src, cams = main_mod.camera_source(cfg, files, log)
    assert cams is files and src.origin is None  # neither: record from cameras.yaml


def test_db_mode_switches_from_files_once_cams_config_loads(tmp_path, fake_recorders):
    cfg = make_cfg(tmp_path, camera_source="db")
    store = FlakyStore({"garage": cam("garage")})
    store.down = True
    src = CameraSource({}, None, store=store)
    assert not src.load_initial()
    svc = Service(cfg, {"front-door": file_cam()}, source=src)
    svc.on_camera_objects()  # nothing loaded yet: keep recording from files
    assert set(svc.recorders) == {"front-door/main", "front-door/sub"}
    store.down = False
    assert src.poll()
    svc.on_camera_objects()
    assert set(svc.recorders) == {"garage/main", "garage/sub"}


# ---- export-files: the rollback path ------------------------------------------------------

def test_export_files_round_trip(store, tmp_path, monkeypatch, capsys):
    from test_camconfig import DETECT, RECORD, REGISTRY

    from cams_record.camconfig import __main__ as cli

    for key, env in (("host", "CAMS_CONFIG_DB_HOST"), ("port", "CAMS_CONFIG_DB_PORT"),
                     ("database", "CAMS_CONFIG_DB_NAME"), ("user", "CAMS_CONFIG_DB_USER")):
        monkeypatch.setenv(env, str(CFG_DB[key]))
    pw = tmp_path / "pw"
    pw.write_text(CFG_DB["password"])
    monkeypatch.setenv("CAMS_CONFIG_DB_PASSWORD_FILE", str(pw))
    text = json.dumps(REGISTRY)
    plan = build_plan(DETECT, RECORD, text, {"retention": {"cameras": {"front-door": {"main_days": 7}}}})
    store.save_many(list(plan.cameras.values()), "test")

    rec, det = tmp_path / "record.yaml", tmp_path / "detect.yaml"
    args = ["--config-dir", str(tmp_path), "export-files", "--record-out", str(rec), "--detect-out", str(det)]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert yaml.safe_load(out) == {"retention": {"cameras": {"front-door": {"main_days": 7.0}}}}
    record, detect = yaml.safe_load(rec.read_text()), yaml.safe_load(det.read_text())
    # The exported files behave exactly like the originals.
    replan = build_plan(detect, record, text)
    assert replan.conflicts == []
    assert check_plan(replan, DETECT, RECORD, text) == []
    for cid, raw in RECORD["cameras"].items():
        assert resolve_camera(cid, record["cameras"][cid]).streams() == resolve_camera(cid, raw).streams()
    assert cli.main(args) == 2  # never overwrites without --force
    assert cli.main([*args, "--force"]) == 0
