"""App Hub JWT auth (HS256), same verification as cams-detect.

Only the ``x-apphub-token`` header is trusted: the App Hub reverse proxy injects its per-app JWT
there (and strips client-sent copies). Every /api route requires one of ``auth.roles`` (admin).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import jwt

from .config import read_secret

log = logging.getLogger(__name__)


@dataclass
class Principal:
    sub: str
    roles: list[str] = field(default_factory=list)


class AuthError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Authenticator:
    def __init__(self, auth_cfg: dict):
        self.cfg = auth_cfg
        self.mode = auth_cfg.get("mode", "jwt")
        self.header = str(auth_cfg.get("header", "x-apphub-token")).lower()
        self.roles = set(auth_cfg.get("roles") or ["admin"])
        self.leeway = float(auth_cfg.get("leeway_seconds", 30))
        self._secret: str | None = None
        self._secret_read = 0.0

    def secret(self) -> str | None:
        # Re-read periodically so a rotated secret file is picked up without restart.
        if self._secret is None or time.monotonic() - self._secret_read > 60:
            self._secret = read_secret(self.cfg.get("secret_file"))
            self._secret_read = time.monotonic()
        return self._secret

    def verify_token(self, token: str) -> Principal:
        secret = self.secret()
        if not secret:
            log.error("auth: JWT secret file %s missing or empty", self.cfg.get("secret_file"))
            raise AuthError(503, "auth not configured")
        try:
            claims = jwt.decode(token, secret, algorithms=["HS256"], leeway=self.leeway,
                                options={"require": ["exp"], "verify_aud": False})
        except jwt.ExpiredSignatureError as exc:
            raise AuthError(401, "token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(401, f"invalid token: {exc.__class__.__name__}") from exc
        roles = claims.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        if not isinstance(roles, list):
            roles = []
        roles = [str(r) for r in roles]
        if not self.roles & set(roles):
            raise AuthError(403, "admin role required")
        return Principal(sub=str(claims.get("sub") or claims.get("username") or "?"), roles=roles)

    def authenticate(self, headers) -> Principal:
        if self.mode == "none":
            return Principal(sub="anonymous", roles=["admin"])
        token = (headers.get(self.header) or "").strip()
        if not token:
            raise AuthError(401, "authentication required")
        return self.verify_token(token)
