"""cams_config: camera object model, file import + equivalence check, store, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import CFG_DB

from cams_record.camconfig.importer import build_plan, check_plan, check_registry
from cams_record.camconfig.model import CameraObject, Stream
from cams_record.camconfig.views import detect_camera, record_camera, registry
from cams_record.camera_access import can_access, can_stream, parse_registry

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / "apphub" / "camera_acl_vectors.json").read_text())

DETECT = {"cameras": {
    "front-door": {"name": "Front Door", "stream": "front-door", "main_stream": "front-door-main",
                   "enabled": True, "decode_fps": 3, "zones": {"porch": [[0, 0.5], [1, 0.5], [1, 1], [0, 1]]},
                   "required_zones": ["porch"], "classes": {"person": {"min_score": 0.6}, "car": False}},
    "back-yard": {"name": "Back Yard", "stream": "back-yard", "enabled": False},
}}
RECORD = {"cameras": {
    "front-door": {"main_stream": "front-door-main", "sub_stream": "front-door"},
    "back-yard": {"main_stream": "back-yard-main", "sub_stream": "back-yard", "record_sub": False},
    "driveway": {"main_stream": "driveway-main", "sub_stream": "driveway", "enabled": False},
}}
REGISTRY = [
    {"id": "front-door", "name": "Front Door"},
    {"id": "back-yard", "name": "Back Yard", "users": ["eric"], "roles": ["family"]},
    {"id": "driveway", "name": "Driveway", "snap": "driveway-snap", "roles": ["family"]},
    {"id": "porch-cam", "name": "Porch"},
]


def _plan(detect=DETECT, record=RECORD, reg=REGISTRY, record_cfg=None):
    text = json.dumps(reg) if reg is not None else None
    return build_plan(detect, record, text, record_cfg), text


# ---- model -----------------------------------------------------------------------

def _cam(**kw):
    base = dict(id="cam-1", name="Cam 1", streams={"main": Stream("main", "cam-1-main"), "sub": Stream("sub", "cam-1")})
    base.update(kw)
    return CameraObject(**base)


def test_default_use_low_res_live_and_detect_high_res_verify_and_record():
    use = _cam().default_use()
    assert use == {"live": ("sub",), "detect": ("sub",), "snapshot": ("sub",), "live_hd": ("main",),
                   "verify": ("main",), "record": ("main", "sub")}


def test_stored_use_is_authoritative():
    cam = _cam(use={"live": ("sub",)})
    assert cam.stream_for("record") is None and cam.streams_for("record") == []
    assert cam.stream_for("live") == "cam-1"


@pytest.mark.parametrize("kw,needle", [
    ({"id": "Bad_ID"}, "id must be"),
    ({"id": "trailing-"}, "id must be"),
    ({"name": " "}, "name must be"),
    ({"connector_type": "ftp"}, "connector_type"),
    ({"visibility": "everyone", "users": ("ann",)}, "only apply to restricted"),
    ({"streams": {}}, "at least one stream"),
    ({"streams": {"main": Stream("main", "rtsp://user:pw@x/y")}}, "invalid go2rtc stream name"),
    ({"use": {"live": ("main", "sub")}}, "only one stream"),
    ({"use": {"live": ("hd",)}}, "unknown stream"),
    ({"use": {"zoom": ("main",)}}, "unknown stream purpose"),
    ({"streams": {"main": Stream("main", "a"), "wide": Stream("wide", "b")}, "use": {"record": ("wide",)}},
     "only streams keyed main or sub"),
    ({"recording": {"main_days": 0}}, "must be a number > 0"),
    ({"recording": {"keep": 1}}, "may only hold"),
])
def test_validate(kw, needle):
    assert any(needle in e for e in _cam(**kw).validate())


def test_registry_entry_access_shapes():
    assert _cam(visibility="everyone").registry_entry() == {"id": "cam-1", "name": "Cam 1"}
    assert _cam().registry_entry() == {"id": "cam-1", "name": "Cam 1", "roles": ["admin"]}  # admins only
    assert _cam(users=("ann",)).registry_entry() == {"id": "cam-1", "name": "Cam 1", "users": ["ann"], "roles": []}
    snap = _cam(use={"snapshot": ("main",)}, visibility="everyone").registry_entry()
    assert snap == {"id": "cam-1", "name": "Cam 1", "snap": "cam-1-main"}
    with pytest.raises(ValueError):
        _cam(streams={"sub": Stream("sub", "has space")}, use={"snapshot": ("sub",)}).registry_entry()


def test_admin_only_export_decisions():
    reg = parse_registry(json.dumps([_cam().registry_entry()]))
    assert can_access(reg["cam-1"], "root", ["admin"])
    assert not can_access(reg["cam-1"], "ann", ["cams"])
    assert not can_access(reg["cam-1"], "ann", ["cams", "family"])


def test_record_camera_preserves_sub_only_recording():
    """A camera with only a sub stream and use.record=('sub',) must still record that stream."""
    cam = CameraObject(
        id="porch", name="Porch",
        streams={"sub": Stream("sub", "porch")},
        use={"live": ("sub",), "record": ("sub",)},
    )
    assert cam.validate() == []
    rc = record_camera(cam)
    assert rc.main_stream == "porch" and rc.sub_stream == "porch"
    assert rc.record_main is False and rc.record_sub is True and rc.enabled is True
    assert rc.streams() == [("sub", "porch")]


# ---- import ------------------------------------------------------------------------

def test_import_consistent_files_is_equivalent():
    plan, text = _plan()
    assert plan.conflicts == []
    assert check_plan(plan, DETECT, RECORD, text) == []
    fd = plan.cameras["front-door"]
    assert fd.use == {"live": ("sub",), "live_hd": ("main",), "snapshot": ("sub",), "detect": ("sub",),
                      "verify": ("main",), "record": ("main", "sub")}
    assert fd.visibility == "everyone"
    assert fd.detection["zones"]["porch"] and "stream" not in fd.detection
    by = plan.cameras["back-yard"]
    assert by.use["record"] == ("main",) and by.users == ("eric",) and by.roles == ("family",)
    assert detect_camera(by)["enabled"] is False  # detection switched off stays off
    dw = plan.cameras["driveway"]
    assert "record" not in dw.use and record_camera(dw).streams() == []  # enabled: false stays off
    assert dw.streams["snap"].go2rtc_name == "driveway-snap" and dw.use["snapshot"] == ("snap",)
    assert dw.detection is None
    porch = plan.cameras["porch-cam"]  # only in cams.json: live view stream only, nothing recorded
    assert set(porch.streams) == {"sub"} and "record" not in porch.use and porch.detection is None


def test_import_keeps_retention_overrides():
    plan, _ = _plan(record_cfg={"retention": {"cameras": {"front-door": {"main_days": 7}}}})
    assert plan.cameras["front-door"].recording == {"main_days": 7}


def test_import_conflict_record_main_defaults_to_sub():
    record = {"cameras": {"front-door": {"sub_stream": "front-door"}}}
    plan, _ = _plan(record=record)
    assert any("same stream 'front-door'" in c for c in plan.conflicts)
    assert "front-door" not in plan.cameras


def test_import_sub_only_camera_is_not_a_conflict():
    """main_stream == sub_stream is fine when only the sub stream is recorded (export-files writes this)."""
    record = {"cameras": {"porch-cam": {"main_stream": "porch-cam", "sub_stream": "porch-cam",
                                         "record_main": False, "record_sub": True}}}
    plan, text = _plan(detect={}, record=record, reg=[{"id": "porch-cam", "name": "Porch"}])
    assert plan.conflicts == []
    cam = plan.cameras["porch-cam"]
    assert set(cam.streams) == {"sub"} and cam.use["record"] == ("sub",)
    assert check_plan(plan, {}, record, text) == []
    # Recording view survives the file shape: still records as sub, never as a duplicated main.
    rc = record_camera(cam)
    assert rc.streams() == [("sub", "porch-cam")]
    assert rc.main_stream == rc.sub_stream == "porch-cam"
    assert rc.record_main is False and rc.record_sub is True


def test_import_conflict_hires_stream_mismatch():
    record = {"cameras": {"front-door": {"main_stream": "front-door-4k", "sub_stream": "front-door"}}}
    plan, _ = _plan(record=record)
    assert any("high-resolution stream is 'front-door-main'" in c for c in plan.conflicts)
    detect = json.loads(json.dumps(DETECT))
    detect["cameras"]["front-door"]["hires_snapshot"] = False
    plan, _ = _plan(detect=detect, record=record)
    assert plan.conflicts == [] and any("unused today" in w for w in plan.warnings)


def test_import_conflict_detects_on_unknown_stream():
    detect = {"cameras": {"front-door": {"stream": "front-door-wide", "main_stream": "front-door-main"}}}
    plan, _ = _plan(detect=detect)
    assert any("detects on 'front-door-wide'" in c for c in plan.conflicts)


def test_import_rejects_urls_and_bad_ids():
    plan, _ = _plan(detect={"cameras": {"x": {"stream": "rtsp://u:p@h/s"}, "back_yard": {}}}, record={}, reg=[])
    assert any("URL or file" in c for c in plan.conflicts)
    assert any("back_yard: id must be" in c for c in plan.conflicts)


def test_import_warns_about_unregistered_and_renamed_cameras():
    detect = {"cameras": {"front-door": {"name": "Front", "stream": "front-door"},
                          "shed": {"stream": "shed"}}}
    plan, text = _plan(detect=detect, record={})
    assert any("front-door: cams-detect calls it 'Front'" in w for w in plan.warnings)
    assert any("shed: not in cams.json today" in w for w in plan.warnings)
    assert plan.cameras["shed"].visibility == "restricted" and not plan.cameras["shed"].users
    assert check_plan(plan, detect, {}, text) == []  # shed: nobody -> admins only is the expected change


def test_check_plan_catches_a_changed_camera():
    plan, text = _plan()
    plan.cameras["front-door"].use["record"] = ("main",)
    plan.cameras["back-yard"].visibility = "everyone"
    plan.cameras["back-yard"].users = plan.cameras["back-yard"].roles = ()
    plan.cameras["front-door"].detection["decode_fps"] = 5
    diffs = check_plan(plan, DETECT, RECORD, text)
    assert any("front-door: cams-record would change" in d for d in diffs)
    assert any("front-door: cams-detect settings would change" in d for d in diffs)
    assert any("back-yard: access for user 'someone-else'" in d for d in diffs)


def test_shared_vectors_survive_import_and_export():
    """Every case in the shared contract gives the same answer from the exported cams.json."""
    text = json.dumps(VECTORS["registry"])
    plan = build_plan(None, None, text)
    assert plan.conflicts == []
    assert check_registry(plan, text) == []
    exported = parse_registry(json.dumps(registry(plan.cameras)))
    assert sorted(exported) == sorted(VECTORS["ids"])
    for case in VECTORS["access"]:
        got = sorted(cid for cid, cam in exported.items() if can_access(cam, case["user"], case["roles"]))
        assert got == sorted(case["allowed"]), case
    for case in VECTORS["streams"]:
        assert can_stream(exported, case["name"], case["user"], case["roles"]) is case["allowed"], case


# ---- store (MariaDB) --------------------------------------------------------------------

def test_store_round_trip(store):
    plan, _ = _plan(record_cfg={"retention": {"cameras": {"front-door": {"main_days": 7, "sub_days": 30}}}})
    cams = list(plan.cameras.values())
    cams[0].notify = [{"channel": "push", "enabled": True, "labels": ["person"], "zones": ["porch"],
                       "verified_only": True, "cooldown_s": 60, "quiet": None, "recipients": None}]
    rev = store.save_many(cams, "eric")
    assert rev == 1
    got_rev, got = store.load_all()
    assert got_rev == 1
    assert {k: c.to_dict() for k, c in got.items()} == {c.id: c.to_dict() for c in cams}
    assert registry(got) == registry(plan.cameras)


def test_store_revision_audit_and_optimistic_lock(store):
    cam = _cam()
    assert store.save(cam, "eric") == 1
    cam.name = "Renamed"
    assert store.save(cam, "ann", expect_revision=1) == 2
    with pytest.raises(ValueError, match="changed since revision 1"):
        store.save(cam, "bob", expect_revision=1)
    assert store.revision() == 2
    log = store.audit("cam-1")
    assert [(a["action"], a["actor"], a["revision"]) for a in log] == [("update", "ann", 2), ("create", "eric", 1)]
    assert log[0]["before"]["name"] == "Cam 1" and log[0]["after"]["name"] == "Renamed"


def test_store_rejects_invalid_and_rolls_back(store):
    with pytest.raises(ValueError, match="id must be"):
        store.save(_cam(id="Bad_ID"), "eric")
    store.save(_cam(), "eric")
    broken = _cam(name="Half written")
    broken.notify = [{"channel": "sms"}]  # rejected by the ENUM inside the transaction
    with pytest.raises(Exception):  # noqa: B017
        store.save(broken, "eric")
    assert store.revision() == 1
    assert store.get("cam-1").name == "Cam 1"


def test_store_delete_cascades(store):
    store.save(_cam(users=("ann",), detection={"decode_fps": 3}, recording={"main_days": 2},
                    use={"live": ("sub",), "record": ("main",)}), "eric")
    assert store.delete("cam-1", "eric") == 2
    assert store.load_all() == (2, {})
    with pytest.raises(KeyError):
        store.delete("cam-1", "eric")

    def leftovers(cur):
        counts = {}
        for t in ("camera_streams", "camera_stream_use", "camera_access", "camera_detection", "camera_recording"):
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            counts[t] = cur.fetchone()[0]
        return counts
    assert set(store.db.run(leftovers).values()) == {0}


def test_cli_import_apply_and_export(store, tmp_path, monkeypatch, capsys):
    from cams_record.camconfig import __main__ as cli

    for name, data in (("detect.yaml", DETECT), ("record.yaml", RECORD)):
        (tmp_path / name).write_text(yaml.safe_dump(data))
    (tmp_path / "cams.json").write_text(json.dumps(REGISTRY))
    pw = tmp_path / "pw"
    pw.write_text(CFG_DB["password"])
    for key, env in (("host", "CAMS_CONFIG_DB_HOST"), ("port", "CAMS_CONFIG_DB_PORT"),
                     ("database", "CAMS_CONFIG_DB_NAME"), ("user", "CAMS_CONFIG_DB_USER")):
        monkeypatch.setenv(env, str(CFG_DB[key]))
    monkeypatch.setenv("CAMS_CONFIG_DB_PASSWORD_FILE", str(pw))
    args = ["--config-dir", str(tmp_path), "import", "--detect", str(tmp_path / "detect.yaml"),
            "--record", str(tmp_path / "record.yaml"), "--registry", str(tmp_path / "cams.json")]

    assert cli.main(args) == 0
    assert "dry run" in capsys.readouterr().out and store.revision() == 0
    assert cli.main([*args, "--apply"]) == 0
    assert "imported 4 cameras (revision 1)" in capsys.readouterr().out
    assert cli.main([*args, "--apply"]) == 0  # re-run never overwrites
    assert "SKIPPED: front-door" in capsys.readouterr().out and store.revision() == 1

    out = tmp_path / "exported.json"
    assert cli.main(["--config-dir", str(tmp_path), "export-registry", "--out", str(out)]) == 0
    assert check_registry(build_plan(None, None, out.read_text()), json.dumps(REGISTRY)) == []

    (tmp_path / "record.yaml").write_text(yaml.safe_dump({"cameras": {"front-door": {"sub_stream": "front-door"}}}))
    assert cli.main([*args, "--apply"]) == 2  # conflicts block writing
    assert "CONFLICT: front-door" in capsys.readouterr().out
