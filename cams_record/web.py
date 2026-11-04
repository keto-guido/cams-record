"""HTTP API (FastAPI), bound to 127.0.0.1:8099 and served under BASE_PATH (default /cams-record)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .auth import Authenticator, AuthError, Principal

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

    r = APIRouter()

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
