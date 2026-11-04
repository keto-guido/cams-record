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
    assert cfg["http"] == {"host": "127.0.0.1", "port": 8099}
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
                                 {"recording": {"audio": "always"}}, {"recording": {"backoff_initial": 0}}])
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
    assert "front-door" in cams
    assert cfg["go2rtc"]["rtsp_url"] == "rtsp://127.0.0.1:8554/{stream}"


def test_stream_url():
    t = "rtsp://127.0.0.1:8554/{stream}"
    assert stream_url(t, "front-door") == "rtsp://127.0.0.1:8554/front-door"
    assert stream_url(t, "/clips/a.mp4") == "/clips/a.mp4"
    assert stream_url(t, "rtsp://cam/x") == "rtsp://cam/x"
