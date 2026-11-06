"""Entry point: python -m cams_record [--config-dir DIR] [--no-http] [--no-db] [--no-reconcile]."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cams-record", description=__doc__)
    ap.add_argument("--config-dir", default=os.environ.get("CONFIG_DIR", "/config"))
    ap.add_argument("--no-http", action="store_true", help="do not start the HTTP server")
    ap.add_argument("--no-db", action="store_true", help="record without indexing (debugging)")
    ap.add_argument("--no-reconcile", action="store_true", help="skip the startup disk/DB reconcile")
    ap.add_argument("--log-level", help="override log_level")
    args = ap.parse_args(argv)

    from .config import load_config

    cfg, cameras = load_config(args.config_dir)
    level = (args.log_level or cfg.get("log_level") or "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    log = logging.getLogger("cams_record")

    from .db import Database
    from .service import Service

    db = None if args.no_db else Database(cfg["db"])
    source, file_cameras = None, cameras
    if cfg["camera_source"] != "file":
        source, cameras = camera_source(cfg, cameras, log)
    service = Service(cfg, cameras, db, source, file_cameras=file_cameras)
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("signal %d: stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    service.start(run_reconcile=not args.no_reconcile)

    server = None
    if not args.no_http:
        import uvicorn

        from .web import create_app

        server = uvicorn.Server(uvicorn.Config(create_app(service, cfg), host=cfg["http"]["host"],
                                               port=int(cfg["http"]["port"]), log_level="warning",
                                               access_log=False))
        server.install_signal_handlers = lambda: None  # we own SIGTERM/SIGINT
        threading.Thread(target=server.run, name="http", daemon=True).start()
        log.info("http: listening on %s:%s%s", cfg["http"]["host"], cfg["http"]["port"], cfg["base_path"] or "/")

    stop.wait()
    if server is not None:
        server.should_exit = True
    service.stop()
    log.info("stopped")
    return 0


def camera_source(cfg: dict, file_cameras: dict, log):
    """(CameraSource, cameras to record at startup) for camera_source shadow or db.

    shadow always records from cameras.yaml. db records from cams_config, or from its cache while the
    database is down; with neither it starts from cameras.yaml and switches once cams_config loads.
    """
    from .camconfig.source import CameraSource
    from .camconfig.views import record_camera

    source = CameraSource(cfg["cams_config"], cfg["cams_config"]["cache_file"])
    loaded = source.load_initial()
    if cfg["camera_source"] == "shadow":
        return source, file_cameras
    if not loaded:
        log.error("camera_source db: cams_config and its cache are both unavailable; recording from "
                  "cameras.yaml until cams_config can be read")
        return source, file_cameras
    cameras = {cid: record_camera(o) for cid, o in source.cameras.items()}
    cfg["retention"]["cameras"] = {cid: dict(o.recording) for cid, o in source.cameras.items() if o.recording}
    return source, cameras


if __name__ == "__main__":
    sys.exit(main())
