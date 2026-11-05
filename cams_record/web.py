"""HTTP API (FastAPI), bound to 127.0.0.1:8097 and served under BASE_PATH (default /cams-record)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

from .auth import Authenticator, AuthError, Principal
from .clips import ClipError, Clips, merge_spans, parse_time, validate_window
from .config import CAMERA_ID_RE
from .db import DBUnavailable
from .media import Media, playback_entries, playlist

log = logging.getLogger(__name__)


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
    app.state.media = media

    def clip_user(request: Request) -> Principal:
        try:
            return auth.authenticate(request.headers, ["admin", "cams"])
        except AuthError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    def window(camera, start, end):
        if not CAMERA_ID_RE.fullmatch(camera):
            raise ClipError(400, "invalid camera id")
        start, end = parse_time(start), parse_time(end)
        validate_window(start, end)
        return start, end

    r = APIRouter()

    def range_window(cam, camera, start, end, limit, default=False):
        camera = cam or camera or ""
        if not CAMERA_ID_RE.fullmatch(camera):
            raise ClipError(400, "invalid camera id")
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
                 end: str | None = None, _: Principal = Depends(clip_user)):
        camera, start, end = range_window(cam, camera, start, end, 8 * 86400, True)
        spans, latest = {}, {}
        for quality in ("main", "sub"):
            rows = indexed(camera, quality, start, end)
            spans[quality] = merge_spans(rows, start, end, clips.cfg["gap_tolerance_s"])
            latest[quality] = max((r["end_ts"] for r in rows if not r.get("missing")
                                   and not r.get("deleted_at")), default=None)
        return dict(cam=camera, start=start, end=end, gap_tolerance_s=clips.cfg["gap_tolerance_s"],
                    spans=spans, latest=latest)

    def playback_data(cam, camera, start, end):
        camera, start, end = range_window(cam, camera, start, end, 26 * 3600, True)
        entries = playback_entries(indexed(camera, "sub", start, end), start, end)
        return camera, start, end, entries

    @r.get("/api/playback")
    def playback(cam: str | None = None, camera: str | None = None, start: str | None = None,
                 end: str | None = None, _: Principal = Depends(clip_user)):
        camera, start, end, entries = playback_data(cam, camera, start, end)
        return dict(cam=camera, start=start, end=end, duration=sum(e["duration"] for e in entries),
                    playlist=f"hls/{camera}/index.m3u8?start={start}&end={end}",
                    segments=[{k: v for k, v in e.items() if k != "codec"} for e in entries])

    @r.get("/api/hls/{cam}/index.m3u8")
    def hls_playlist(cam: str, start: str | None = None, end: str | None = None,
                     _: Principal = Depends(clip_user)):
        _, _, _, entries = playback_data(cam, None, start, end)
        if not entries:
            raise ClipError(404, "no recorded segments")
        return PlainTextResponse(playlist(entries, clips.cfg["gap_tolerance_s"]),
                                 media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    @r.get("/api/hls/{cam}/seg/{segment_id}.ts")
    def hls_segment(cam: str, segment_id: int, _: Principal = Depends(clip_user)):
        if service.db is None:
            raise ClipError(503, "segment index unavailable")
        path = media.ts(cam, segment_id)
        return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "private, max-age=86400"},
                            background=BackgroundTask(path.unlink, missing_ok=True))

    def export_info_data(camera, start, end):
        main, sub = (media.estimate(camera, q, start, end) for q in ("main", "sub"))
        busy = not media.export_slot.acquire(blocking=False)
        if not busy:
            media.export_slot.release()
        return dict(max_s=clips.cfg["export_max_s"], seconds=end-start, busy=busy, main=main, sub=sub,
                    recommended="main" if main["status"] == "available" else
                    "sub" if sub["status"] == "available" else None)

    @r.get("/api/export/info")
    def export_info(cam: str, start: str, end: str, _: Principal = Depends(clip_user)):
        camera, start, end = range_window(cam, None, start, end, clips.cfg["export_max_s"])
        return export_info_data(camera, start, end)

    @r.get("/api/export")
    def export(cam: str, start: str, end: str, quality: Literal["main", "sub"] = "main",
               fallback: Literal["0", "1"] = "1", _: Principal = Depends(clip_user)):
        camera, start, end = range_window(cam, None, start, end, clips.cfg["export_max_s"])
        if not media.export_slot.acquire(blocking=False):
            return JSONResponse({"detail": "another export is in progress"}, status_code=429,
                                headers={"Retry-After": "15"})
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
        return FileResponse(path, media_type="video/mp4", filename=f"{camera}_{stamp}_{stop}_{quality}.mp4",
                            headers={"X-Export-Quality": quality}, background=BackgroundTask(finish))

    @r.get("/api/clip/info")
    def clip_info(camera: str, start: str, end: str, quality: Literal["sub", "main"] = "sub",
                  download: Literal["0", "1"] = "0", _: Principal = Depends(clip_user)):
        start, end = window(camera, start, end)
        return {q: clips.availability(camera, q, start, end)[0] for q in ("sub", "main")}

    @r.get("/api/clip")
    def clip(camera: str, start: str, end: str, quality: Literal["sub", "main"] = "sub",
             download: Literal["0", "1"] = "0", _: Principal = Depends(clip_user)):
        start, end = window(camera, start, end)
        path = clips.build(camera, quality, start, end, lease=True)
        stamp = datetime.fromtimestamp(start, ZoneInfo(cfg["clips"]["timezone"])).strftime("%Y%m%d-%H%M%S")
        return FileResponse(path, media_type="video/mp4", filename=f"{camera}_{stamp}_{quality}.mp4",
                            content_disposition_type="attachment" if download == "1" else "inline",
                            background=BackgroundTask(path.unlink, missing_ok=True))

    @app.exception_handler(ClipError)
    async def clip_exc(request: Request, exc: ClipError):
        payload = {"detail": exc.detail}
        if exc.status == 425:
            payload["retry_after"] = 10
        return JSONResponse(payload, status_code=exc.status,
                            headers={"Retry-After": "10"} if exc.status == 425 else None)

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

    app.include_router(r, prefix=base)

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return BasePathMiddleware(app, base) if base else app
