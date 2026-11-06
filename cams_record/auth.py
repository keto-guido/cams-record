"""App Hub JWT auth (HS256), same verification as cams-detect.

Only the ``x-apphub-token`` header is trusted: the App Hub reverse proxy injects its per-app JWT
there (and strips client-sent copies). Clip routes accept admin or cams; other /api routes require admin.
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


def _nonblank_str(value) -> str | None:
    """Return value when it is a non-blank string; otherwise None."""
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    return value


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

    def verify_token(self, token: str, allowed_roles=None) -> Principal:
        secret = self.secret()
        if not secret:
            log.error("auth: JWT secret file %s missing or empty", self.cfg.get("secret_file"))
            raise AuthError(503, "auth not configured")
        try:
            claims = jwt.decode(token, secret, algorithms=["HS256"], leeway=self.leeway,
                                audience=self.cfg.get("app_id", "cams-record"),
                                options={"require": ["exp", "aud"]})
        except jwt.ExpiredSignatureError as exc:
            raise AuthError(401, "token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(401, f"invalid token: {exc.__class__.__name__}") from exc
        # Fail closed at the auth boundary: never invent a subject like "?". Empty principals
        # would otherwise bypass camera ACL empty-user denial when the token carries admin.
        sub = _nonblank_str(claims.get("sub"))
        if sub is None:
            raise AuthError(401, "invalid token: missing subject")
        roles = claims.get("roles")
        if roles is None:
            roles = []
        if isinstance(roles, str):
            roles = [roles]
        if not isinstance(roles, list) or any(_nonblank_str(r) is None for r in roles):
            raise AuthError(401, "invalid token: invalid roles")
        roles = list(roles)
        if not set(allowed_roles or ["admin"]) & set(roles):
            raise AuthError(403, "required role missing")
        return Principal(sub=sub, roles=roles)

    def authenticate(self, headers, allowed_roles=None) -> Principal:
        if self.mode == "none":
            return Principal(sub="anonymous", roles=["admin"])
        token = (headers.get(self.header) or "").strip()
        if not token:
            raise AuthError(401, "authentication required")
        return self.verify_token(token, allowed_roles)
