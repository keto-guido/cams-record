"""Turn today's files (cams-detect cameras.yaml, cams-record cameras.yaml + retention, App Hub
cams.json) into camera objects, and prove the result behaves the same before anything is written.

``build_plan`` never guesses: wherever the files disagree about a stream it records a conflict, and
``--apply`` refuses to write while any conflict is left. ``check_plan`` re-derives each service's view
from the plan (views.py) and compares it with what the files produce today, including every access
decision the registry makes.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from ..camera_access import can_access, can_stream, parse_registry
from ..config import resolve_camera
from .model import CameraObject, Stream
from .views import DETECT_STREAM_KEYS, detect_camera, record_camera, registry


@dataclass
class Plan:
    cameras: dict[str, CameraObject] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)  # block --apply
    warnings: list[str] = field(default_factory=list)  # behaviour changes to read before applying

    @property
    def ok(self) -> bool:
        return not self.conflicts


def _is_go2rtc_name(stream: str) -> bool:
    return not ("://" in stream or stream.startswith(("/", "./", "../")))


def build_plan(detect_yaml: dict | None, record_yaml: dict | None, registry_text: str | None,
               record_cfg: dict | None = None) -> Plan:
    plan = Plan()
    detect = (detect_yaml or {}).get("cameras") or {}
    record_raw = (record_yaml or {}).get("cameras") or {}
    retention = ((record_cfg or {}).get("retention") or {}).get("cameras") or {}
    reg = parse_registry(registry_text) if registry_text is not None else {}
    raw_reg_ids = _raw_registry_ids(registry_text)
    for cid in sorted(raw_reg_ids - set(reg)):
        plan.warnings.append(f"{cid}: dropped by cams.json today (invalid or duplicate entry); nobody can see it")

    record = {}
    for cid, raw in record_raw.items():
        try:
            record[str(cid)] = resolve_camera(str(cid), raw)
        except ValueError as exc:
            plan.conflicts.append(f"{cid}: cams-record cameras.yaml: {exc}")

    ids = sorted(set(map(str, detect)) | set(record) | set(reg))
    for order, cid in enumerate(ids):
        d = detect.get(cid)
        if d is not None and not isinstance(d, dict):
            d = {}
        cam = _build_camera(plan, cid, d, record.get(cid), reg.get(cid), retention.get(cid))
        if cam is None:
            continue
        cam.sort_order = order
        errs = cam.validate()
        if errs:
            plan.conflicts.extend(errs)
            continue
        plan.cameras[cid] = cam
    return plan


def _raw_registry_ids(text: str | None) -> set[str]:
    try:
        data = json.loads(text) if text is not None else []
    except ValueError:
        return set()
    if not isinstance(data, list):
        return set()
    return {e["id"] for e in data if isinstance(e, dict) and isinstance(e.get("id"), str)}


def _build_camera(plan: Plan, cid, d, rc, rg, retention) -> CameraObject | None:
    conflicts_before = len(plan.conflicts)
    # -- streams ----------------------------------------------------------------
    d_sub = d_main = None
    if d is not None:
        d_sub = str(d.get("stream", cid))
        if not _is_go2rtc_name(d_sub):
            plan.conflicts.append(f"{cid}: cams-detect reads a URL or file ({d_sub!r}), not a go2rtc stream")
            return None
        d_main = str(d.get("main_stream") or f"{d_sub}-main")
    if rc is not None:
        main, sub = rc.main_stream, rc.sub_stream
    elif d is not None:
        main, sub = d_main, d_sub
    else:  # only in cams.json: the hub plays the stream named after the id
        main, sub = None, cid
    if rc is not None and not _is_go2rtc_name(main):
        plan.conflicts.append(f"{cid}: cams-record main_stream is a URL or file ({main!r})")
        return None
    if main is not None and main == sub:
        plan.conflicts.append(f"{cid}: main_stream and sub_stream are the same stream {main!r}; "
                              "set cams-record main_stream to the high-resolution stream")
    if d is not None and rc is not None:
        if d_sub not in (sub, main):
            plan.conflicts.append(f"{cid}: cams-detect detects on {d_sub!r}, which cams-record does not know "
                                  f"(main {main!r}, sub {sub!r})")
        uses_main = bool(d.get("hires_snapshot", True))
        if d_main != main:
            msg = (f"{cid}: cams-detect's high-resolution stream is {d_main!r} but cams-record records "
                   f"{main!r} as main")
            if uses_main:
                plan.conflicts.append(msg + "; set main_stream to the same stream in both files")
            else:
                plan.warnings.append(msg + " (unused today: hires_snapshot is off); cams-record's wins")
    if len(plan.conflicts) > conflicts_before:
        return None

    streams = {}
    if main is not None:
        streams["main"] = Stream("main", main)
    if sub is not None and sub != main:
        streams["sub"] = Stream("sub", sub)
    by_name = {s.go2rtc_name: k for k, s in streams.items()}

    # -- purposes -----------------------------------------------------------------
    use: dict[str, tuple[str, ...]] = {}
    live_key = by_name.get(cid)
    if live_key is None:
        plan.warnings.append(f"{cid}: the hub's live view plays go2rtc stream {cid!r}, which is not one of "
                             f"this camera's streams ({', '.join(sorted(by_name))})")
        live_key = "sub" if "sub" in streams else "main"
    use["live"] = (live_key,)
    if "main" in streams:
        use["live_hd"] = ("main",)
    snap_name = rg.snap if rg is not None and rg.snap else cid
    snap_key = by_name.get(snap_name)
    if snap_key is None:
        streams["snap"] = Stream("snap", snap_name)
        snap_key = "snap"
    use["snapshot"] = (snap_key,)
    if d is not None:
        use["detect"] = (by_name[d_sub],)
        if "main" in streams:
            use["verify"] = ("main",)
    recorded = tuple(q for q, _ in rc.streams()) if rc is not None else ()
    if recorded:  # nothing recorded = no record purpose at all (stored use is authoritative)
        use["record"] = recorded

    # -- name and access -----------------------------------------------------------
    d_name = str(d.get("name")) if d is not None and d.get("name") else None
    if rg is not None:
        name = rg.name
        if d_name and d_name != name:
            plan.warnings.append(f"{cid}: cams-detect calls it {d_name!r}, the hub {name!r}; the hub's name is "
                                 "kept (Home Assistant entity names follow once cams-detect reads cams_config)")
    else:
        name = d_name or cid
        plan.warnings.append(f"{cid}: not in cams.json today, so nobody can view it; after import admins can")
    if rg is None:
        visibility, users, roles = "restricted", (), ()
    elif not rg.restricted:
        visibility, users, roles = "everyone", (), ()
    else:
        visibility, users, roles = "restricted", tuple(sorted(rg.users)), tuple(sorted(rg.roles))

    detection = None
    if d is not None:
        detection = {k: copy.deepcopy(v) for k, v in d.items() if k not in DETECT_STREAM_KEYS}
    recording = dict(retention) if retention else None
    return CameraObject(id=cid, name=name, visibility=visibility, users=users, roles=roles,
                        streams=streams, use=use, detection=detection, recording=recording)


# -- equivalence check -------------------------------------------------------------

def check_plan(plan: Plan, detect_yaml: dict | None, record_yaml: dict | None,
               registry_text: str | None) -> list[str]:
    """Differences between what the plan would do and what the files do today. Empty = equivalent."""
    diffs: list[str] = []
    detect = (detect_yaml or {}).get("cameras") or {}
    record_raw = (record_yaml or {}).get("cameras") or {}
    for cid, cam in plan.cameras.items():
        # cams-record: same streams recorded, same stream names authorizing old footage.
        if cid in record_raw:
            old = resolve_camera(cid, record_raw[cid])
            new = record_camera(cam)
            if (old.streams(), old.main_stream, old.sub_stream) != (new.streams(), new.main_stream, new.sub_stream):
                diffs.append(f"{cid}: cams-record would change from {old} to {new}")
        elif record_camera(cam).streams():
            diffs.append(f"{cid}: cams-record would start recording it")
        # cams-detect: same streams and identical settings.
        old_d = detect.get(cid)
        new_d = detect_camera(cam)
        if old_d is None:
            if new_d is not None:
                diffs.append(f"{cid}: cams-detect would start detecting on it")
            continue
        if new_d is None:
            diffs.append(f"{cid}: cams-detect would stop detecting on it")
            continue
        old_d = old_d or {}
        o_stream = str(old_d.get("stream", cid))
        o_main = str(old_d.get("main_stream") or f"{o_stream}-main")
        if (o_stream, o_main) != (new_d["stream"], new_d.get("main_stream")):
            diffs.append(f"{cid}: cams-detect streams would change from {(o_stream, o_main)} to "
                         f"{(new_d['stream'], new_d.get('main_stream'))}")
        o_rest = {k: v for k, v in old_d.items() if k not in DETECT_STREAM_KEYS}
        n_rest = {k: v for k, v in new_d.items() if k not in DETECT_STREAM_KEYS}
        if json.dumps(o_rest, sort_keys=True) != json.dumps(n_rest, sort_keys=True):
            diffs.append(f"{cid}: cams-detect settings would change")
    diffs.extend(check_registry(plan, registry_text))
    return diffs


def check_registry(plan: Plan, registry_text: str | None) -> list[str]:
    """Every access decision the old and the exported cams.json make, for every id, snap and principal.

    Cameras that nobody could see before and admins can see now are expected (reported as warnings by
    build_plan) and skipped; any other difference is returned.
    """
    old = parse_registry(registry_text) if registry_text is not None else {}
    new = parse_registry(json.dumps(registry(plan.cameras)))
    newly_admin_only = {cid for cid in new if cid not in old}
    users = {""} | {u for c in (*old.values(), *new.values()) for u in c.users} | {"someone-else"}
    roles = {r for c in (*old.values(), *new.values()) for r in c.roles} | {"cams", "admin"}
    principals = [(u, ()) for u in users] + [(u, (r,)) for u in users for r in roles]
    names = {n for c in (*old.values(), *new.values()) for n in (c.id, c.snap) if n}
    diffs = []
    for user, rs in principals:
        for cid in sorted(set(old) | set(new)):
            a, b = can_access(old.get(cid), user, rs), can_access(new.get(cid), user, rs)
            if a != b and not (cid in newly_admin_only and not a and "admin" in rs):
                diffs.append(f"{cid}: access for user {user!r} roles {list(rs)} would change from {a} to {b}")
        for name in sorted(names):
            a, b = can_stream(old, name, user, rs), can_stream(new, name, user, rs)
            owners_new = {c.id for c in new.values() if name in (c.id, c.snap)}
            if a != b and not (owners_new and owners_new <= newly_admin_only and not a and "admin" in rs):
                diffs.append(f"stream {name}: access for user {user!r} roles {list(rs)} would change from {a} to {b}")
    return sorted(set(diffs))
