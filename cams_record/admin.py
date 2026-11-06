"""Camera management API for the Cameras app's Admin tab (admins only): camera objects in cams_config.

Routes (under base_path):
  GET    /api/admin/cameras             every camera object, the revision, mode and shadow report
  GET    /api/admin/cameras/{id}        one camera object and its change history
  POST   /api/admin/cameras             create  {camera, expect_revision}
  PUT    /api/admin/cameras/{id}        replace {camera, expect_revision}
  DELETE /api/admin/cameras/{id}?expect_revision=N
  GET    /api/admin/streams             go2rtc stream names, and which camera uses each

Writes need camera_source shadow or db (in file mode nothing reads cams_config, so editing it would
look like it did something it did not). Every write carries the revision the form was loaded at; a
newer change answers 409 instead of being overwritten. Writes must be JSON, which a cross-site form
cannot send without a CORS preflight.

The form edits identity, streams, stream jobs, access, retention and the detection on/off switch.
Everything else on the camera object (masks, zones and other detection settings, notification rules,
connector settings, stream probe data) is carried over unchanged.
"""

from __future__ import annotations

import copy
import json
import logging
import urllib.request
from datetime import datetime
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, HTTPException, Request

from .camconfig.model import CameraObject, Stream
from .camconfig.store import CamConfigStore, open_db

log = logging.getLogger(__name__)

EDITABLE = {"id", "name", "enabled", "visibility", "users", "roles", "streams", "use", "recording",
            "detection_enabled", "sort_order"}


def _str_list(value, what) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{what} must be a list of names")
    return tuple(sorted({v.strip() for v in value if v.strip()}))


def apply_form(existing: CameraObject | None, form: dict) -> CameraObject:
    """A camera object from the admin form, keeping every field the form does not edit."""
    if not isinstance(form, dict):
        raise ValueError("camera must be an object")
    unknown = set(form) - EDITABLE
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    cam = copy.deepcopy(existing) if existing is not None else CameraObject(id=form.get("id"), name="")
    if existing is not None and form.get("id", existing.id) != existing.id:
        raise ValueError("a camera id cannot change (Home Assistant entities and recordings are keyed by it)")
    if "name" in form:
        if not isinstance(form["name"], str):
            raise ValueError("name must be text")
        cam.name = form["name"].strip()
    if "enabled" in form:
        if not isinstance(form["enabled"], bool):
            raise ValueError("enabled must be true or false")
        cam.enabled = form["enabled"]
    if "visibility" in form:
        cam.visibility = form["visibility"]
    if "users" in form or "roles" in form:
        cam.users = _str_list(form.get("users", list(cam.users)), "users")
        cam.roles = _str_list(form.get("roles", list(cam.roles)), "roles")
    if cam.visibility == "everyone":
        cam.users, cam.roles = (), ()
    if "sort_order" in form:
        if isinstance(form["sort_order"], bool) or not isinstance(form["sort_order"], int):
            raise ValueError("sort_order must be a whole number")
        cam.sort_order = form["sort_order"]
    if "streams" in form:
        streams = form["streams"]
        if not isinstance(streams, dict):
            raise ValueError("streams must be an object of key -> {go2rtc_name}")
        new = {}
        for key, s in streams.items():
            name = s.get("go2rtc_name") if isinstance(s, dict) else None
            if not isinstance(name, str):
                raise ValueError(f"stream {key}: go2rtc_name is required")
            old = cam.streams.get(key)
            if old is not None and old.go2rtc_name == name.strip():
                new[key] = old  # keep probe data (resolution, codec)
            else:
                new[key] = Stream(str(key), name.strip())
        cam.streams = new
    if "use" in form:
        use = form["use"]
        if not isinstance(use, dict) or not all(isinstance(k, list) for k in use.values()):
            raise ValueError("use must be an object of purpose -> [stream keys]")
        cam.use = {p: tuple(keys) for p, keys in use.items() if keys}
    if "recording" in form:
        rec = form["recording"]
        if rec is None:
            cam.recording = None
        elif isinstance(rec, dict):
            cam.recording = {k: v for k, v in rec.items() if v is not None} or None
        else:
            raise ValueError("recording must be an object or null")
    if "detection_enabled" in form and form["detection_enabled"] is not None:
        on = form["detection_enabled"]
        if not isinstance(on, bool):
            raise ValueError("detection_enabled must be true, false or null")
        if cam.detection is None:
            if on:
                cam.detection = {"enabled": True}  # new camera for cams-detect: defaults, empty masks
        else:
            cam.detection = {**cam.detection, "enabled": on}  # masks and zones are never dropped here
    return cam


def _jsonable(row: dict) -> dict:
    return {k: (v.isoformat() + "Z" if isinstance(v, datetime) else v) for k, v in row.items()}


def admin_router(service, cfg: dict, admin_dep) -> APIRouter:
    r = APIRouter()
    mode = cfg.get("camera_source", "file")
    source = getattr(service, "source", None)
    holder: dict = {"store": source.store if source is not None else None}

    def store() -> CamConfigStore:
        if holder["store"] is None:
            holder["store"] = CamConfigStore(open_db(cfg["cams_config"]))
        return holder["store"]

    def run(fn):
        try:
            return fn(store())
        except KeyError as exc:
            raise HTTPException(404, "camera not found") from exc
        except ValueError as exc:
            msg = str(exc)
            raise HTTPException(409 if "changed since revision" in msg else 422, msg) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001  (database down, grants, schema missing)
            log.warning("camera admin: cams_config unavailable: %s", exc)
            raise HTTPException(503, "camera settings database unavailable") from exc

    def editable() -> None:
        if mode == "file":
            raise HTTPException(409, "camera management is read-only while camera_source is file: "
                                     "set shadow (compare) or db (apply) first")

    async def form_body(request: Request) -> dict:
        if (request.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "send JSON")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("camera"), dict):
            raise HTTPException(422, 'expected {"camera": {...}, "expect_revision": N}')
        rev = body.get("expect_revision")
        if isinstance(rev, bool) or not isinstance(rev, int):
            raise HTTPException(422, "expect_revision is required")
        return body

    def recorder_states(cam_id: str) -> dict:
        lock = getattr(service, "_cam_lock", None)
        recs = getattr(service, "recorders", {})
        if lock is not None:
            with lock:
                recs = dict(recs)
        out = {}
        for key, rec in recs.items():
            cid, _, quality = key.partition("/")
            if cid == cam_id:
                out[quality] = getattr(rec, "state", None)
        return out

    @r.get("/cameras")
    def list_cameras(_=Depends(admin_dep)):
        rev, cams = run(lambda s: s.load_all())
        status = service.camera_source_status() if hasattr(service, "camera_source_status") else {"mode": mode}
        return {
            "revision": rev, "mode": mode, "editable": mode != "file",
            "camera_source": status,
            "cameras": [{**c.to_dict(), "recording_state": recorder_states(c.id)}
                        for c in sorted(cams.values(), key=lambda c: (c.sort_order, c.id))],
        }

    @r.get("/cameras/{cam_id}")
    def get_camera(cam_id: str, _=Depends(admin_dep)):
        rev, cams = run(lambda s: s.load_all())
        if cam_id not in cams:
            raise HTTPException(404, "camera not found")
        history = run(lambda s: s.audit(cam_id, limit=25))
        return {"revision": rev, "mode": mode, "editable": mode != "file", "camera": cams[cam_id].to_dict(),
                "default_use": {p: list(k) for p, k in cams[cam_id].default_use().items()},
                "history": [_jsonable(h) for h in history]}

    @r.post("/cameras", status_code=201)
    async def create_camera(request: Request, who=Depends(admin_dep)):
        editable()
        body = await form_body(request)

        def tx(s):
            _, cams = s.load_all()
            cid = body["camera"].get("id")
            if cid in cams:
                raise HTTPException(409, f"camera {cid} already exists")
            cam = apply_form(None, body["camera"])
            rev = s.save(cam, who.sub, expect_revision=body["expect_revision"], action="create")
            return {"revision": rev, "camera": cam.to_dict()}
        return run(tx)

    @r.put("/cameras/{cam_id}")
    async def update_camera(cam_id: str, request: Request, who=Depends(admin_dep)):
        editable()
        body = await form_body(request)

        def tx(s):
            _, cams = s.load_all()
            if cam_id not in cams:
                raise KeyError(cam_id)
            form = {**body["camera"], "id": body["camera"].get("id", cam_id)}
            cam = apply_form(cams[cam_id], form)
            rev = s.save(cam, who.sub, expect_revision=body["expect_revision"], action="update")
            return {"revision": rev, "camera": cam.to_dict()}
        return run(tx)

    @r.delete("/cameras/{cam_id}")
    def delete_camera(cam_id: str, expect_revision: int, who=Depends(admin_dep)):
        editable()
        rev = run(lambda s: s.delete(cam_id, who.sub, expect_revision=expect_revision))
        return {"revision": rev}

    @r.get("/streams")
    def streams(_=Depends(admin_dep)):
        api = cfg["go2rtc"].get("api_url") or "http://127.0.0.1:1984"
        names, reachable = [], True
        try:
            with urllib.request.urlopen(urljoin(api.rstrip("/") + "/", "api/streams"), timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
            names = sorted(data) if isinstance(data, dict) else []
        except Exception as exc:  # noqa: BLE001
            log.warning("camera admin: go2rtc stream list unavailable: %s", exc)
            reachable = False
        try:
            _, cams = store().load_all()
        except Exception:  # noqa: BLE001
            cams = {}
        used: dict[str, list[str]] = {}
        for c in cams.values():
            for s in c.streams.values():
                used.setdefault(s.go2rtc_name, []).append(c.id)
        return {"reachable": reachable, "streams": [{"name": n, "used_by": sorted(used.get(n, []))} for n in names]}

    return r
