"""HTTP API (FastAPI), bound to 127.0.0.1:8097 and served under BASE_PATH (default /cams-record)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

from .auth import Authenticator, AuthError, Principal
from .clips import ClipError, Clips, parse_time, validate_window
from .config import CAMERA_ID_RE
from .db import DBUnavailable

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
