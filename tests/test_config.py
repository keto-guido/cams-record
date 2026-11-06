import pytest
import yaml

from cams_record.config import load_config, resolve_camera, stream_url


def write(d, cams, cfg=None):
    (d / "cameras.yaml").write_text(yaml.safe_dump({"cameras": cams}))
    if cfg is not None:
        (d / "config.yaml").write_text(yaml.safe_dump(cfg))


def test_defaults_and_cameras(tmp_path):
    write(tmp_path, {"front-door": {"main_stream": "front-door", "sub_stream": "front-door-sub"},
                     "garage": {"main_stream": "garage", "enabled": False}})
    cfg, cams = load_config(tmp_path, env={})
    assert cfg["recording"]["root"] == "/volume1/nvr"
    assert cfg["recording"]["segment_time"] == 60
    assert cfg["http"] == {"host": "127.0.0.1", "port": 8097}
    assert cfg["recording"]["min_free_gb"] == 200 and cfg["recording"]["resume_free_gb"] == 250
    assert cfg["db"]["database"] == "cams_record"
    assert cfg["db"]["password_file"] == "/run/secrets/db_password"
    assert cfg["base_path"] == "/cams-record"
    fd = cams["front-door"]
    assert fd.streams() == [("main", "front-door"), ("sub", "front-door-sub")]
    assert cams["garage"].streams() == []


def test_record_flags():
    c = resolve_camera("x", {"main_stream": "x", "sub_stream": "x-sub", "record_main": False})
    assert c.streams() == [("sub", "x-sub")]
    c = resolve_camera("x", {"main_stream": "x"})  # no sub stream -> record_sub defaults off
    assert c.record_sub is False and c.streams() == [("main", "x")]
    assert resolve_camera("x", None).main_stream == "x"


@pytest.mark.parametrize("cid,raw", [
    ("Front Door", {}),
    ("ok", {"enabled": "yes"}),
    ("ok", {"main_stream": "a", "bogus": 1}),
    ("ok", ["not", "a", "mapping"]),
])
def test_invalid_cameras(cid, raw):
    with pytest.raises(ValueError):
        resolve_camera(cid, raw)


def test_env_overrides_and_validation(tmp_path):
    write(tmp_path, {}, {"recording": {"segment_time": 30}})
    cfg, cams = load_config(tmp_path, env={"RECORD_ROOT": "/tmp/nvr", "DB_HOST": "192.168.1.5",
                                           "HTTP_PORT": "9000", "BASE_PATH": "x/"})
    assert cams == {}
    assert cfg["recording"]["root"] == "/tmp/nvr"
    assert cfg["recording"]["segment_time"] == 30
    assert cfg["recording"]["backoff_max"] == 60  # deep merge keeps other defaults
    assert cfg["db"]["host"] == "192.168.1.5"
    assert cfg["http"]["port"] == 9000
    assert cfg["base_path"] == "/x"


@pytest.mark.parametrize("cfg", [{"auth": {"mode": "basic"}}, {"recording": {"segment_time": 1}},
                                 {"recording": {"audio": "always"}}, {"recording": {"backoff_initial": 0}},
                                 {"recording": {"min_free_gb": -1}}, {"recording": {"min_free_gb": 300}}])
def test_invalid_global(tmp_path, cfg):
    write(tmp_path, {}, cfg)
    with pytest.raises(ValueError):
        load_config(tmp_path, env={})


def test_cameras_must_be_mapping(tmp_path):
    (tmp_path / "cameras.yaml").write_text("cameras: [a, b]\n")
    with pytest.raises(ValueError):
        load_config(tmp_path, env={})


def test_example_files_parse(tmp_path):
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "config"
    (tmp_path / "cameras.yaml").write_text((src / "cameras.example.yaml").read_text())
    (tmp_path / "config.yaml").write_text((src / "config.example.yaml").read_text())
    cfg, cams = load_config(tmp_path, env={})
    assert cams["front-door"].streams() == [("main", "front-door-main"), ("sub", "front-door")]
    assert cfg["http"]["port"] == 8097
    assert cfg["go2rtc"]["rtsp_url"] == "rtsp://127.0.0.1:8554/{stream}"


def test_stream_url():
    t = "rtsp://127.0.0.1:8554/{stream}"
    assert stream_url(t, "front-door") == "rtsp://127.0.0.1:8554/front-door"
    assert stream_url(t, "/clips/a.mp4") == "/clips/a.mp4"
    assert stream_url(t, "rtsp://cam/x") == "rtsp://cam/x"


def test_retention_defaults_are_safe(tmp_path):
    write(tmp_path, {})
    cfg, _ = load_config(tmp_path, env={})
    ret = cfg["retention"]
    assert ret["dry_run"] is True
    assert (ret["main_days"], ret["sub_days"], ret["max_total_gb"]) == (3, 14, 1500)
    assert (ret["interval_s"], ret["batch_size"], ret["cameras"]) == (300, 500, {})


def test_retention_camera_overrides(tmp_path):
    write(tmp_path, {}, {"retention": {"cameras": {"garage": {"main_days": 0.5}}}})
    cfg, _ = load_config(tmp_path, env={})
    assert cfg["retention"]["cameras"] == {"garage": {"main_days": 0.5}}
    assert cfg["retention"]["sub_days"] == 14  # deep merge keeps the other defaults


@pytest.mark.parametrize("ret", [{"dry_run": "false"}, {"main_days": 0}, {"sub_days": -1}, {"main_days": "3"},
                                 {"max_total_gb": 0}, {"interval_s": 0}, {"batch_size": 0}, {"batch_size": 1.5},
                                 {"nice": 20}, {"nice": -1}, {"cameras": ["garage"]},
                                 {"cameras": {"garage": {"main_day": 1}}}, {"cameras": {"garage": {"sub_days": 0}}},
                                 {"cameras": {"garage": 3}}])
def test_invalid_retention(tmp_path, ret):
    write(tmp_path, {}, {"retention": ret})
    with pytest.raises(ValueError):
        load_config(tmp_path, env={})


def test_camera_warnings_do_not_change_behaviour(caplog):
    from cams_record.config import parse_cameras

    data = {"cameras": {
        "front-door": {"main_stream": "front-door-main", "sub_stream": "front-door"},
        "garage": {"sub_stream": "garage"},  # main defaults to the id = the sub stream
        "back_yard": {"main_stream": "back_yard-main"},
    }}
    with caplog.at_level("WARNING"):
        cams = parse_cameras(data)
    assert cams["garage"].streams() == [("main", "garage"), ("sub", "garage")]  # unchanged
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("front-door" in m for m in msgs)
    assert any("garage: main_stream is not set" in m for m in msgs)
    assert any("garage: main_stream and sub_stream are both 'garage'" in m for m in msgs)
    assert any("back_yard: id is not valid in the App Hub camera registry" in m for m in msgs)
