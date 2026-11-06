"""cams_config command line.

  python -m cams_record.camconfig import --detect D --record R --registry J [--record-config C] [--apply]
  python -m cams_record.camconfig export-registry [--out FILE]
  python -m cams_record.camconfig show [ID]

Database settings come from --config-dir/config.yaml (section cams_config) and CAMS_CONFIG_DB_* env.
import is a dry run unless --apply; --apply refuses while there are conflicts or behaviour differences,
and never changes a camera that is already in cams_config.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml

from ..config import DEFAULTS, _read_yaml, apply_env, deep_merge
from .importer import build_plan, check_plan
from .store import CamConfigStore, open_db
from .views import registry


def _db_cfg(config_dir: str | None) -> dict:
    cfg = deep_merge(DEFAULTS, _read_yaml(Path(config_dir) / "config.yaml") if config_dir else {})
    apply_env(cfg)
    return cfg["cams_config"]


def _yaml(path: str | None) -> dict | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: top level must be a mapping")
    return data


def cmd_import(args) -> int:
    detect, record = _yaml(args.detect), _yaml(args.record)
    record_cfg = _yaml(args.record_config)
    registry_text = Path(args.registry).read_text(encoding="utf-8") if args.registry else None
    plan = build_plan(detect, record, registry_text, record_cfg)
    diffs = check_plan(plan, detect, record, registry_text) if plan.ok else []
    print(f"cameras: {len(plan.cameras)}  conflicts: {len(plan.conflicts)}  "
          f"warnings: {len(plan.warnings)}  behaviour differences: {len(diffs)}")
    for title, items in (("CONFLICT", plan.conflicts), ("DIFFERENCE", diffs), ("WARNING", plan.warnings)):
        for line in items:
            print(f"{title}: {line}")
    if args.verbose:
        print(json.dumps({cid: c.to_dict() for cid, c in plan.cameras.items()}, indent=2, sort_keys=True))
    if not args.apply:
        print("dry run: nothing written (add --apply to write)")
        return 1 if (plan.conflicts or diffs) else 0
    if plan.conflicts or diffs:
        print("not applied: fix the conflicts and differences first", file=sys.stderr)
        return 2
    store = CamConfigStore(open_db(_db_cfg(args.config_dir)))
    _, existing = store.load_all()
    new = [c for cid, c in plan.cameras.items() if cid not in existing]
    for cid in sorted(set(plan.cameras) & set(existing)):
        print(f"SKIPPED: {cid} is already in cams_config and was left unchanged")
    if not new:
        print("nothing to write")
        return 0
    rev = store.save_many(new, args.actor, action="import")
    print(f"imported {len(new)} cameras (revision {rev})")
    return 0


def cmd_export(args) -> int:
    store = CamConfigStore(open_db(_db_cfg(args.config_dir)))
    rev, cams = store.load_all()
    text = json.dumps(registry(cams), indent=2) + "\n"
    if not args.out:
        sys.stdout.write(text)
        return 0
    out = Path(args.out)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=".cams.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)  # atomic rename: readers watch the inode (docs/camera-access.md)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print(f"wrote {len(cams)} cameras from revision {rev} to {out}", file=sys.stderr)
    return 0


def cmd_show(args) -> int:
    store = CamConfigStore(open_db(_db_cfg(args.config_dir)))
    rev, cams = store.load_all()
    if args.id:
        if args.id not in cams:
            print(f"unknown camera {args.id}", file=sys.stderr)
            return 1
        cams = {args.id: cams[args.id]}
    print(json.dumps({"revision": rev, "cameras": {k: c.to_dict() for k, c in cams.items()}},
                     indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m cams_record.camconfig")
    p.add_argument("--config-dir", default=os.environ.get("CONFIG_DIR", "/config"))
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import", help="load today's files into cams_config (dry run by default)")
    i.add_argument("--detect", help="cams-detect config/cameras.yaml")
    i.add_argument("--record", help="cams-record config/cameras.yaml")
    i.add_argument("--record-config", help="cams-record config/config.yaml (per-camera retention)")
    i.add_argument("--registry", help="App Hub cams.json")
    i.add_argument("--apply", action="store_true")
    i.add_argument("--actor", default="import")
    i.add_argument("-v", "--verbose", action="store_true", help="print the planned camera objects")
    e = sub.add_parser("export-registry", help="write cams.json from cams_config")
    e.add_argument("--out")
    s = sub.add_parser("show", help="print camera objects")
    s.add_argument("id", nargs="?")
    args = p.parse_args(argv)
    return {"import": cmd_import, "export-registry": cmd_export, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
