"""Shadow mode: where cams_config would make cams-record behave differently from its files."""

from __future__ import annotations

from ..config import CameraConfig
from .model import CameraObject
from .views import record_camera


def _days(d: dict | None) -> dict:
    return {k: float(v) for k, v in (d or {}).items() if v is not None}


def record_differences(file_cams: dict[str, CameraConfig], file_retention: dict,
                       objects: dict[str, CameraObject]) -> list[str]:
    """One line per difference; empty when switching cams-record to cams_config changes nothing."""
    out = []
    for cid in sorted(set(file_cams) | set(objects)):
        f, o = file_cams.get(cid), objects.get(cid)
        new = record_camera(o) if o is not None else None
        if f is None:
            if new is not None and new.streams():
                out.append(f"{cid}: only in cams_config, would record {new.streams()}")
            continue
        if new is None:
            out.append(f"{cid}: only in cameras.yaml (records {f.streams()}); not in cams_config")
            continue
        if f.streams() != new.streams():
            out.append(f"{cid}: records {f.streams()} from files but {new.streams()} from cams_config")
        if (f.main_stream, f.sub_stream) != (new.main_stream, new.sub_stream):
            out.append(f"{cid}: streams main={f.main_stream!r} sub={f.sub_stream!r} from files but "
                       f"main={new.main_stream!r} sub={new.sub_stream!r} from cams_config")
    for cid in sorted(set(file_retention) | {c for c, o in objects.items() if o.recording}):
        a, b = _days(file_retention.get(cid)), _days(objects[cid].recording if cid in objects else None)
        if a != b:
            out.append(f"{cid}: retention overrides {a or 'none'} from files but {b or 'none'} from cams_config")
    return out
