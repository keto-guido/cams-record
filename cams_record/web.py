"""HTTP API (FastAPI), bound to 127.0.0.1:8097 and served under BASE_PATH (default /cams-record)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

from .auth import Authenticator, AuthError, Principal
from .camera_access import CameraAccess, Viewer
from .clips import ClipError, Clips, merge_spans, parse_time, validate_window
from .config import CAMERA_ID_RE
from .db import DBUnavailable
from .media import MAX_SEGMENT_OFFSET_MS, Media, playback_entries, playlist
from .push import DetectionEvent, PushError, PushService, normalize_event_id

log = logging.getLogger(__name__)

# Access-controlled export media and metadata must not be stored in shared caches.
PRIVATE_NO_STORE = {"Cache-Control": "private, no-store"}


class CleanupFileResponse(FileResponse):
    """Release disposable files even when sending fails or is cancelled."""

    def __init__(self, *args, cleanup, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup = cleanup
        self.cleaned = False

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self.cleaned:
                self.cleaned = True
                self.cleanup()


class BasePathMiddleware:
    """Accept requests both with and without the BASE_PATH prefix (App Hub may strip it)."""

    def __init__(self, app, base_path: str):
        self.app = app
        self.base = base_path

    async def __call__(self, scope, receive, send):
        if self.base and scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path != self.base and not path.startswith(self.base + "/"):
                scope = dict(scope)
                scope["path"] = self.base + (path if path.startswith("/") else "/" + path)
                scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


def create_app(service, cfg: dict):
    base = cfg.get("base_path", "")
    auth = Authenticator(cfg["auth"])
    app = FastAPI(title="cams-record", docs_url=None, redoc_url=None, openapi_url=None)

    def admin(request: Request) -> Principal:
        try:
            return auth.authenticate(request.headers)
        except AuthError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    clips = Clips(service.db, cfg)
    media = Media(clips, cfg)
    push = getattr(service, "push", None) or PushService(service.db, cfg.get("push") or {})
    app.state.media = media
    app.state.push = push

    def clip_user(request: Request) -> Principal:
        try:
            return auth.authenticate(request.headers, ["admin", "cams"])
        except AuthError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    def require_webhook(request: Request) -> None:
        secret = push.webhook_secret()
        if not secret:
            raise HTTPException(503, "push webhook not configured")
        got = (request.headers.get("x-cams-push-secret") or "").strip()
        if not got or got != secret:
            raise HTTPException(401, "invalid webhook secret")

    access = CameraAccess(cfg["access"], getattr(service, "cameras", {}))

    def viewer(request: Request) -> Viewer:
        # Identity is the signed JWT; the hub's x-apphub-user header must only agree with it.
        principal = clip_user(request)
        return Viewer(principal.sub, tuple(principal.roles), request.headers.get("x-apphub-user"))

    # Every camera route gets its id through window(), range_window() or a direct require(), so the
    # id that was authorized is the id that gets served, whichever of cam/camera the client sent.
    def window(who, camera, start, end):
        if not CAMERA_ID_RE.fullmatch(camera):
            raise ClipError(400, "invalid camera id")
        access.require(camera, who)
        start, end = parse_time(start), parse_time(end)
        validate_window(start, end)
        return start, end

    r = APIRouter()

    def range_window(who, cam, camera, start, end, limit, default=False):
        camera = cam or camera or ""
        if not CAMERA_ID_RE.fullmatch(camera):
            raise ClipError(400, "invalid camera id")
        access.require(camera, who)
        if start is None and end is None and default:
            day = datetime.now(ZoneInfo(cfg["clips"]["timezone"])).replace(hour=0, minute=0, second=0, microsecond=0)
            start, end = day.timestamp(), (day + timedelta(days=1)).timestamp()
        elif start is None or end is None:
            raise ClipError(400, "start and end required")
        else:
            start, end = parse_time(start), parse_time(end)
        validate_window(start, end, limit)
        return camera, start, end

    def indexed(camera, quality, start, end):
        if service.db is None:
            raise ClipError(503, "segment index unavailable")
        return service.db.segment_rows(camera, quality, start, end)

    @r.get("/api/timeline")
    def timeline(cam: str | None = None, camera: str | None = None, start: str | None = None,
                 end: str | None = None, who: Viewer = Depends(viewer)):
        camera, start, end = range_window(who, cam, camera, start, end, 8 * 86400, True)
        spans, latest = {}, {}
        for quality in ("main", "sub"):
            rows = indexed(camera, quality, start, end)
            spans[quality] = merge_spans(rows, start, end, clips.cfg["gap_tolerance_s"])
            latest[quality] = max((r["end_ts"] for r in rows if not r.get("missing")
                                   and not r.get("deleted_at")), default=None)
        return dict(cam=camera, start=start, end=end, gap_tolerance_s=clips.cfg["gap_tolerance_s"],
                    spans=spans, latest=latest)

    def playback_data(who, cam, camera, start, end):
        camera, start, end = range_window(who, cam, camera, start, end, 26 * 3600, True)
        entries = playback_entries(indexed(camera, "sub", start, end), start, end,
                                   clips.cfg["gap_tolerance_s"])
        return camera, start, end, entries

    @r.get("/api/playback")
    def playback(cam: str | None = None, camera: str | None = None, start: str | None = None,
                 end: str | None = None, who: Viewer = Depends(viewer)):
        camera, start, end, entries = playback_data(who, cam, camera, start, end)
        return dict(cam=camera, start=start, end=end, duration=sum(e["duration"] for e in entries),
                    playlist=f"hls/{camera}/index.m3u8?start={start}&end={end}",
                    segments=[{k: v for k, v in e.items() if k != "codec"} for e in entries])

    @r.get("/api/hls/{cam}/index.m3u8")
    def hls_playlist(cam: str, start: str | None = None, end: str | None = None,
                     who: Viewer = Depends(viewer)):
        _, _, _, entries = playback_data(who, cam, None, start, end)
        if not entries:
            raise ClipError(404, "no recorded segments")
        return PlainTextResponse(playlist(entries),
                                 media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    @r.get("/api/hls/{cam}/seg/{segment_id}.ts")
    def hls_segment(cam: str, segment_id: int, o: str | None = None, who: Viewer = Depends(viewer)):
        access.require(cam, who)  # media.ts() then refuses a segment_id belonging to any other camera
        if o is None or not re.fullmatch(r"[0-9]{1,9}", o) or int(o) > MAX_SEGMENT_OFFSET_MS:
            raise ClipError(400, "invalid segment offset")
        if service.db is None:
            raise ClipError(503, "segment index unavailable")
        path = media.ts(cam, segment_id, int(o))
        return CleanupFileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "private, max-age=86400"},
                                   cleanup=lambda: path.unlink(missing_ok=True))

    def export_info_data(camera, start, end):
        main, sub = (media.estimate(camera, q, start, end) for q in ("main", "sub"))
        busy = not media.export_slot.acquire(blocking=False)
        if not busy:
            media.export_slot.release()
        return dict(max_s=clips.cfg["export_max_s"], seconds=end-start, busy=busy, main=main, sub=sub,
                    recommended="main" if main["status"] == "available" else
                    "sub" if sub["status"] == "available" else None)

    @r.get("/api/export/info")
    def export_info(cam: str, start: str, end: str, who: Viewer = Depends(viewer)):
        camera, start, end = range_window(who, cam, None, start, end, clips.cfg["export_max_s"])
        return JSONResponse(export_info_data(camera, start, end), headers=PRIVATE_NO_STORE)

    @r.get("/api/export")
    def export(cam: str, start: str, end: str, quality: Literal["main", "sub"] = "main",
               fallback: Literal["0", "1"] = "1", who: Viewer = Depends(viewer)):
        camera, start, end = range_window(who, cam, None, start, end, clips.cfg["export_max_s"])
        if not media.export_slot.acquire(blocking=False):
            return JSONResponse({"detail": "another export is in progress"}, status_code=429,
                                headers={**PRIVATE_NO_STORE, "Retry-After": "15"})
        try:
            info = export_info_data(camera, start, end)
            if (quality == "main" and info["main"]["status"] == "gone" and fallback == "1"
                    and info["sub"]["status"] == "available"):
                quality = "sub"
            state = info[quality]["status"]
            if state != "available":
                raise ClipError(425 if state == "not_ready" else 404, "footage unavailable")
            path = media.export(camera, quality, start, end, info[quality]["bytes"])
        except BaseException:
            media.export_slot.release()
            raise
        def finish():
            try:
                path.unlink(missing_ok=True)
            finally:
                media.export_slot.release()
        zone = ZoneInfo(cfg["clips"]["timezone"])
        stamp = datetime.fromtimestamp(start, zone).strftime("%Y%m%d-%H%M%S")
        stop = datetime.fromtimestamp(end, zone).strftime("%H%M%S")
        return CleanupFileResponse(path, media_type="video/mp4", filename=f"{camera}_{stamp}_{stop}_{quality}.mp4",
                                   headers={**PRIVATE_NO_STORE, "X-Export-Quality": quality},
                                   cleanup=finish)

    @r.get("/api/clip/info")
    def clip_info(camera: str, start: str, end: str, quality: Literal["sub", "main"] = "sub",
                  download: Literal["0", "1"] = "0", who: Viewer = Depends(viewer)):
        start, end = window(who, camera, start, end)
        return {q: clips.availability(camera, q, start, end)[0] for q in ("sub", "main")}

    @r.get("/api/clip")
    def clip(camera: str, start: str, end: str, quality: Literal["sub", "main"] = "sub",
             download: Literal["0", "1"] = "0", who: Viewer = Depends(viewer)):
        start, end = window(who, camera, start, end)
        path = clips.build(camera, quality, start, end, lease=True)
        stamp = datetime.fromtimestamp(start, ZoneInfo(cfg["clips"]["timezone"])).strftime("%Y%m%d-%H%M%S")
        return FileResponse(path, media_type="video/mp4", filename=f"{camera}_{stamp}_{quality}.mp4",
                            headers=dict(PRIVATE_NO_STORE),
                            content_disposition_type="attachment" if download == "1" else "inline",
                            background=BackgroundTask(path.unlink, missing_ok=True))

    @app.exception_handler(ClipError)
    async def clip_exc(request: Request, exc: ClipError):
        payload = {"detail": exc.detail}
        if exc.status == 425:
            payload["retry_after"] = 10
        headers = dict(PRIVATE_NO_STORE) if "/api/export" in request.url.path else {}
        if exc.status == 425:
            headers["Retry-After"] = "10"
        return JSONResponse(payload, status_code=exc.status, headers=headers or None)

    @app.exception_handler(DBUnavailable)
    async def db_exc(request: Request, exc: DBUnavailable):
        return JSONResponse({"detail": "segment index unavailable"}, status_code=503)

    @r.get("/healthz")
    def healthz():
        return {"ok": True}

    @r.get("/api/status")
    def status(_: Principal = Depends(admin)):
        return service.status()

    @r.get("/api/metrics", response_class=PlainTextResponse)
    def metrics(_: Principal = Depends(admin)):
        return PlainTextResponse(service.metrics_text(), media_type="text/plain; version=0.0.4")

    # ---- Web Push (authenticated users manage their own subscriptions only) ----
    @r.get("/api/push/vapid-public-key")
    def push_vapid_public_key(_: Principal = Depends(clip_user)):
        key = push.public_key()
        if not key:
            raise HTTPException(503, "push not configured")
        return {"publicKey": key}

    @r.get("/api/push/status")
    def push_status(endpoint: str | None = None, user: Principal = Depends(clip_user)):
        return push.status_for(user.sub, endpoint)

    @r.post("/api/push/subscribe")
    async def push_subscribe(request: Request, user: Principal = Depends(clip_user)):
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "invalid JSON") from exc
        if isinstance(body, dict) and "user_agent" not in body:
            body = dict(body)
            body["user_agent"] = (request.headers.get("user-agent") or "")[:512]
        try:
            return push.upsert(user.sub, body if isinstance(body, dict) else {})
        except PushError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @r.delete("/api/push/subscribe")
    async def push_unsubscribe(request: Request, user: Principal = Depends(clip_user)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        endpoint = body.get("endpoint") if isinstance(body, dict) else None
        if not endpoint:
            endpoint = request.query_params.get("endpoint")
        try:
            return push.remove(user.sub, endpoint or "")
        except PushError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @r.post("/api/push/test")
    def push_test(user: Principal = Depends(clip_user)):
        event = DetectionEvent(id="", camera_id="test", label="test", score=None,
                               start_ts=0.0, end_ts=None, camera_name="Cameras")
        try:
            return push.notify_user(user.sub, event, test=True)
        except PushError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @r.get("/api/push/event-context/{event_id}")
    def push_event_context(event_id: str, who: Viewer = Depends(viewer)):
        """Authorized deep-link helper: metadata only (no snapshots/images)."""
        try:
            eid = normalize_event_id(event_id)
        except PushError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        if service.db is None:
            raise HTTPException(503, "database unavailable")
        try:
            row = service.db.detection_event_by_id(eid)
        except DBUnavailable as exc:
            raise HTTPException(503, "database unavailable") from exc
        if not row or (access.enforce and not access.allowed(row["camera_id"], who)):
            raise HTTPException(404, "event not found")
        from urllib.parse import quote

        return {
            "id": row["id"],
            "camera": row["camera_id"],
            "camera_id": row["camera_id"],
            "label": row["label"] or "detection",
            "score": row["score"],
            "start_ts": row["start_ts"],
            "end_ts": row["end_ts"],
            "url": f"/cams/#event={quote(str(row['id']), safe='')}",
        }

    @r.post("/api/internal/detection-event")
    async def push_detection_webhook(request: Request):
        """Authenticated cams-detect → cams-record detection hook. Not a public send API."""
        require_webhook(request)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "invalid JSON object")
        try:
            event_id = normalize_event_id(body.get("id") or body.get("event_id"))
        except PushError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        camera_id = str(body.get("camera_id") or body.get("camera") or "")
        try:
            start_ts = float(body.get("start_ts"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "id, camera_id and start_ts are required") from exc
        if not CAMERA_ID_RE.fullmatch(camera_id):
            raise HTTPException(400, "invalid id or camera_id")
        end_raw = body.get("end_ts")
        try:
            end_ts = float(end_raw) if end_raw is not None else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "invalid end_ts") from exc
        # Production cams_detect uses max_score; accept score as an alias for the webhook.
        score_raw = body.get("max_score", body.get("score"))
        try:
            score = float(score_raw) if score_raw is not None else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "invalid score") from exc
        event = DetectionEvent(
            id=event_id,
            camera_id=camera_id,
            label=str(body.get("label") or "detection"),
            score=score,
            start_ts=start_ts,
            end_ts=end_ts,
            camera_name=(str(body["camera_name"]) if body.get("camera_name") else None),
        )
        return push.notify_event(event)

    @app.exception_handler(PushError)
    async def push_exc(request: Request, exc: PushError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    app.include_router(r, prefix=base)

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return BasePathMiddleware(app, base) if base else app
