"""Turn today's files (cams-detect cameras.yaml, cams-record cameras.yaml + retention, App Hub
cams.json) into camera objects, and prove the result behaves the same before anything is written.

``build_plan`` never guesses about retargeting: wherever detect and record disagree about which
go2rtc name is the high-res verify/main stream it records a conflict, and ``--apply`` refuses to
write while any conflict or behaviour difference is left. Distinct live/detect/record aliases are
kept as separate stream keys. ``check_plan`` re-derives each service's view from the plan
(views.py) and compares it with what the files produce today, including every access decision the
registry makes.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from ..camera_access import can_access, can_stream, parse_registry
from ..config import resolve_camera
from .model import STREAM_KEY_RE, CameraObject, Stream
from .views import DETECT_STREAM_KEYS, detect_camera, record_camera, registry


@dataclass
class Plan:
    cameras: dict[str, CameraObject] = field(default_factory=dict)
    expected_ids: set[str] = field(default_factory=set)  # union of detect ∪ record ∪ registry
    conflicts: list[str] = field(default_factory=list)  # block --apply
    warnings: list[str] = field(default_factory=list)  # behaviour changes to read before applying

    @property
    def ok(self) -> bool:
        return not self.conflicts and set(self.cameras) == self.expected_ids

    @property
    def missing_ids(self) -> list[str]:
        return sorted(self.expected_ids - set(self.cameras))


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

    plan.expected_ids = set(map(str, detect)) | set(record) | set(reg)
    for order, cid in enumerate(sorted(plan.expected_ids)):
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


def _stream_key(streams: dict[str, Stream], go2rtc_name: str, prefer: str) -> str:
    """Reuse an existing key for this go2rtc name, or add ``prefer`` (or prefer2, …)."""
    for key, stream in streams.items():
        if stream.go2rtc_name == go2rtc_name:
            return key
    key = prefer if prefer not in streams else None
    if key is None:
        n = 2
        while True:
            candidate = f"{prefer}{n}"
            if candidate not in streams and STREAM_KEY_RE.fullmatch(candidate):
                key = candidate
                break
            n += 1
    streams[key] = Stream(key, go2rtc_name)
    return key


def _build_camera(plan: Plan, cid, d, rc, rg, retention) -> CameraObject | None:
    conflicts_before = len(plan.conflicts)
    # -- streams ----------------------------------------------------------------
    d_sub = d_main = None
    d_main_explicit = False
    if d is not None:
        d_sub = str(d.get("stream", cid))
        if not _is_go2rtc_name(d_sub):
            plan.conflicts.append(f"{cid}: cams-detect reads a URL or file ({d_sub!r}), not a go2rtc stream")
            return None
        d_main_explicit = "main_stream" in d
        d_main = str(d["main_stream"]) if d_main_explicit else str(f"{d_sub}-main")
    if rc is not None and not rc.streams() and not rc.record_main and not rc.record_sub \
            and rc.sub_stream in (None, rc.main_stream) and d is not None:
        # export-files used to emit a inert record stub for detect-only cameras; ignore it so we
        # do not invent an HD conflict against a synthesized {stream}-main.
        rc = None
    if rc is not None:
        main, sub = rc.main_stream, rc.sub_stream
    elif d is not None and d_main_explicit:
        # Detect-only with an explicit HD name: keep it. Do not invent {stream}-main when the key
        # was omitted (webcam low-res only; missing HD must not appear as live_hd/verify).
        main, sub = d_main, d_sub
    elif d is not None:
        main, sub = None, d_sub
    else:  # only in cams.json: the hub plays the stream named after the id
        main, sub = None, cid
    if rc is not None and not _is_go2rtc_name(main):
        plan.conflicts.append(f"{cid}: cams-record main_stream is a URL or file ({main!r})")
        return None
    if main is not None and main == sub:
        if rc is not None and not any(q == "main" for q, _ in rc.streams()):
            main = None  # a sub-only camera (as export-files writes one): nothing is recorded as main
        else:
            plan.conflicts.append(f"{cid}: main_stream and sub_stream are the same stream {main!r}; "
                                  "set cams-record main_stream to the high-resolution stream")
    if d is not None and rc is not None:
        # Detect may use a go2rtc alias that is neither recorded main nor sub (e.g. live ``back-yard``
        # while recording ``back-yard-main`` + ``back-yard-raw``). That is stored as an extra stream
        # key below; only a mismatched high-res verify target is a conflict.
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

    streams: dict[str, Stream] = {}
    if main is not None:
        streams["main"] = Stream("main", main)
    if sub is not None and sub != main:
        streams["sub"] = Stream("sub", sub)
    if d is not None:
        # May add a third alias when detect/live use a go2rtc name that is neither recorded main
        # nor sub (back-yard → detect on ``back-yard``, record ``back-yard-main`` + ``back-yard-raw``).
        _stream_key(streams, d_sub, "detect")
    by_name = {s.go2rtc_name: k for k, s in streams.items()}

    # -- purposes -----------------------------------------------------------------
    use: dict[str, tuple[str, ...]] = {}
    live_key = by_name.get(cid)
    if live_key is None:
        plan.warnings.append(f"{cid}: the hub's live view plays go2rtc stream {cid!r}, which is not one of "
                             f"this camera's streams ({', '.join(sorted(by_name))})")
        live_key = "sub" if "sub" in streams else ("detect" if "detect" in streams else "main")
    use["live"] = (live_key,)
    if "main" in streams:
        use["live_hd"] = ("main",)
    snap_name = rg.snap if rg is not None and rg.snap else cid
    snap_key = by_name.get(snap_name)
    if snap_key is None:
        streams["snap"] = Stream("snap", snap_name)
        snap_key = "snap"
        by_name[snap_name] = snap_key
    use["snapshot"] = (snap_key,)
    if d is not None:
        use["detect"] = (by_name[d_sub],)
        # Verify / HD snapshot only when we have a real main stream. Detect-only cameras without an
        # explicit main_stream stay low-res (no synthesized {stream}-main).
        if "main" in streams and (rc is not None or d_main_explicit):
            use["verify"] = ("main",)
    recorded = tuple(q for q, _ in rc.streams()) if rc is not None else ()
    if recorded:  # nothing recorded = no record purpose at all (stored use is authoritative)
        use["record"] = recorded

    # -- name and access -----------------------------------------------------------
    d_name = str(d.get("name")) if d is not None and d.get("name") else None
    if rg is not None:
        name = rg.name
        if d_name and d_name != name:
            plan.warnings.append(f"{cid}: cams-detect calls it {d_name!r}, the hub {name!r}; each surface "
                                 "keeps its own name (hub registry vs detect settings)")
    else:
        name = d_name or cid
        plan.warnings.append(f"{cid}: not in cams.json today, so nobody can view it; after import admins can "
                             "see it in Admin inventory only (live registry unchanged while "
                             "publish_registry is false)")
    if rg is None:
        visibility, users, roles = "restricted", (), ()
    elif not rg.restricted:
        visibility, users, roles = "everyone", (), ()
    else:
        visibility, users, roles = "restricted", tuple(sorted(rg.users)), tuple(sorted(rg.roles))

    detection = None
    if d is not None:
        # Keep detect's own name inside settings when present; stream fields come from purposes.
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
        o_main_explicit = "main_stream" in old_d
        o_main = str(old_d["main_stream"]) if o_main_explicit else f"{o_stream}-main"
        n_stream, n_main = new_d["stream"], new_d.get("main_stream")
        if o_stream != n_stream:
            diffs.append(f"{cid}: cams-detect streams would change from {(o_stream, o_main)} to "
                         f"{(n_stream, n_main)}")
        elif o_main_explicit:
            if n_main != o_main:
                diffs.append(f"{cid}: cams-detect streams would change from {(o_stream, o_main)} to "
                             f"{(n_stream, n_main)}")
        elif n_main is not None and n_main != o_main:
            # File omitted main_stream (defaulted at runtime). Plan may omit verify entirely so a
            # missing {stream}-main is not invented; that is not a difference. A different explicit
            # verify target is.
            diffs.append(f"{cid}: cams-detect streams would change from {(o_stream, o_main)} to "
                         f"{(n_stream, n_main)}")
        o_rest = {k: v for k, v in old_d.items() if k not in DETECT_STREAM_KEYS}
        n_rest = {k: v for k, v in new_d.items() if k not in DETECT_STREAM_KEYS}
        # File omitted name → detect_camera may synthesize CameraObject.name; not a behaviour change.
        # When the file set a name, it must round-trip on the detect surface (hub name stays separate).
        if "name" not in old_d:
            o_rest.pop("name", None)
            n_rest.pop("name", None)
        if json.dumps(o_rest, sort_keys=True) != json.dumps(n_rest, sort_keys=True):
            diffs.append(f"{cid}: cams-detect settings would change")
    for cid in sorted(set(detect) - set(plan.cameras)):
        diffs.append(f"{cid}: cams-detect camera missing from import plan")
    for cid in sorted(set(record_raw) - set(plan.cameras)):
        diffs.append(f"{cid}: cams-record camera missing from import plan")
    diffs.extend(check_registry(plan, registry_text))
    return diffs


def check_registry(plan: Plan, registry_text: str | None) -> list[str]:
    """Every access decision the old and the exported cams.json make, for every id, snap and principal.

    Cameras that nobody could see before and admins can see now are expected (reported as warnings by
    build_plan) and skipped; any other difference is returned. Admin inventory visibility is not the
    same as publishing a new live tile: export is only written when access.publish_registry is true.
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
