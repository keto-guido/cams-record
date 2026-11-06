"""Web Push (VAPID + Push API): subscription lifecycle and delivery.

Private VAPID material is read from a secret file and never returned to clients.
Dead endpoints (HTTP 404/410 from the push service) are pruned after send failures.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .config import read_secret

log = logging.getLogger(__name__)

ENDPOINT_MAX = 2048
KEY_MAX = 255
UA_MAX = 512
# RFC 8030 push services we accept; reject arbitrary URLs to limit SSRF via stored endpoints.
_ENDPOINT_HOST_OK = re.compile(
    r"^(?:([a-z0-9-]+\.)*web\.push\.apple\.com|"
    r"([a-z0-9-]+\.)*notify\.windows\.com|"
    r"([a-z0-9-]+\.)*push\.services\.mozilla\.com|"
    r"fcm\.googleapis\.com|"
    r"updates\.push\.services\.mozilla\.com|"
    r"([a-z0-9-]+\.)*cdn\.mozilla\.net)$",
    re.I,
)


class PushError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class DetectionEvent:
    id: int
    camera_id: str
    label: str
    score: float | None
    start_ts: float
    end_ts: float | None
    camera_name: str | None = None


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def validate_subscription(body: dict) -> dict:
    if not isinstance(body, dict):
        raise PushError(400, "subscription must be an object")
    endpoint = body.get("endpoint")
    keys = body.get("keys") if isinstance(body.get("keys"), dict) else {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(endpoint, str) or not (8 <= len(endpoint) <= ENDPOINT_MAX):
        raise PushError(400, "invalid endpoint")
    try:
        parsed = urlparse(endpoint)
    except Exception as exc:  # noqa: BLE001
        raise PushError(400, "invalid endpoint") from exc
    if parsed.scheme != "https" or not parsed.netloc or not _ENDPOINT_HOST_OK.match(parsed.hostname or ""):
        raise PushError(400, "endpoint host not allowed")
    if not isinstance(p256dh, str) or not (1 <= len(p256dh) <= KEY_MAX):
        raise PushError(400, "invalid p256dh key")
    if not isinstance(auth, str) or not (1 <= len(auth) <= KEY_MAX):
        raise PushError(400, "invalid auth key")
    ua = body.get("user_agent")
    if ua is not None and (not isinstance(ua, str) or len(ua) > UA_MAX):
        raise PushError(400, "invalid user_agent")
    return {
        "endpoint": endpoint,
        "endpoint_hash": endpoint_hash(endpoint),
        "p256dh": p256dh,
        "auth": auth,
        "user_agent": (ua or "")[:UA_MAX] or None,
    }


def application_server_key(public_raw: bytes) -> str:
    """URL-safe base64 (no padding) of the uncompressed P-256 public key."""
    import base64

    return base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("=")


def load_vapid_public_b64(private_pem: str) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid

    vapid = Vapid.from_pem(private_pem.encode("utf-8") if isinstance(private_pem, str) else private_pem)
    raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return application_server_key(raw)


def deep_link_for(event: DetectionEvent) -> str:
    return f"/cams/#event={int(event.id)}"


def notification_payload(event: DetectionEvent, *, test: bool = False) -> dict[str, Any]:
    label = (event.label or "detection").strip() or "detection"
    name = (event.camera_name or event.camera_id or "camera").strip() or "camera"
    title = "Test notification" if test else f"{label.capitalize()} · {name}"
    body = "Cameras push is working on this device." if test else f"Detection at {name}"
    url = deep_link_for(event) if not test else "/cams/#detections"
    if test and event.id <= 0:
        url = "/cams/#detections"
    return {
        "title": title,
        "body": body,
        "url": url,
        "tag": f"cams-event-{event.id}" if event.id > 0 else "cams-push-test",
        "event_id": int(event.id) if event.id > 0 else None,
        "camera_id": event.camera_id,
        "camera_name": event.camera_name or event.camera_id,
        "label": label,
        "start_ts": event.start_ts,
        "test": test,
    }


class PushService:
    """Owns VAPID config, subscription CRUD helpers, and fan-out sends."""

    def __init__(self, db, cfg: dict):
        self.db = db
        self.cfg = cfg
        self._private: str | None = None
        self._public_b64: str | None = None
        self._private_read = 0.0
        self._webhook: str | None = None
        self._webhook_read = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True)) and self.private_key() is not None

    def private_key(self) -> str | None:
        if self._private is None or time.monotonic() - self._private_read > 60:
            raw = read_secret(self.cfg.get("vapid_private_file"))
            self._private = raw or None
            self._public_b64 = None
            self._private_read = time.monotonic()
            if self._private:
                try:
                    self._public_b64 = load_vapid_public_b64(self._private)
                except Exception:  # noqa: BLE001
                    log.exception("push: failed to derive VAPID public key")
                    self._private = None
                    self._public_b64 = None
            configured = (self.cfg.get("vapid_public_key") or "").strip()
            if configured and self._private:
                self._public_b64 = configured
        return self._private

    def public_key(self) -> str | None:
        if self.private_key() is None:
            return None
        return self._public_b64

    def webhook_secret(self) -> str | None:
        if self._webhook is None or time.monotonic() - self._webhook_read > 60:
            self._webhook = read_secret(self.cfg.get("webhook_secret_file")) or None
            self._webhook_read = time.monotonic()
        return self._webhook

    def status_for(self, user_sub: str, endpoint: str | None = None) -> dict:
        configured = self.enabled
        rows = self.db.push_subscriptions_for_user(user_sub) if self.db is not None and configured else []
        match = None
        if endpoint:
            h = endpoint_hash(endpoint)
            match = next((r for r in rows if r["endpoint_hash"] == h), None)
        return {
            "configured": configured,
            "vapid_public_key": self.public_key() if configured else None,
            "subscribed": bool(match) if endpoint else bool(rows),
            "subscription_count": len(rows),
            "endpoint_subscribed": bool(match) if endpoint else None,
        }

    def upsert(self, user_sub: str, body: dict) -> dict:
        if not self.enabled:
            raise PushError(503, "push not configured")
        if self.db is None:
            raise PushError(503, "database unavailable")
        sub = validate_subscription(body)
        self.db.upsert_push_subscription(user_sub, sub)
        return {"ok": True, "subscribed": True}

    def remove(self, user_sub: str, endpoint: str) -> dict:
        if self.db is None:
            raise PushError(503, "database unavailable")
        if not isinstance(endpoint, str) or not endpoint:
            raise PushError(400, "endpoint required")
        n = self.db.delete_push_subscription(user_sub, endpoint_hash(endpoint))
        return {"ok": True, "removed": n}

    def send_raw(self, subscription: dict, payload: dict) -> tuple[bool, int | None]:
        """Send one notification. Returns (ok, status_code). Prunes 404/410."""
        private = self.private_key()
        if not private:
            return False, None
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:
            log.error("push: pywebpush is not installed")
            return False, None
        data = json.dumps(payload, separators=(",", ":"))
        claims = {"sub": self.cfg.get("vapid_subject") or "mailto:cams-record@localhost"}
        info = {
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        }
        try:
            webpush(
                subscription_info=info,
                data=data,
                vapid_private_key=private,
                vapid_claims=claims,
                ttl=int(self.cfg.get("ttl_s", 86400)),
                timeout=float(self.cfg.get("timeout_s", 10)),
            )
            if self.db is not None:
                self.db.touch_push_subscription(subscription["endpoint_hash"])
            return True, 201
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning("push: send failed endpoint=%s status=%s err=%s",
                        subscription["endpoint_hash"][:12], status, exc)
            if status in (404, 410) and self.db is not None:
                self.db.delete_push_endpoint(subscription["endpoint_hash"])
                log.info("push: pruned dead endpoint %s", subscription["endpoint_hash"][:12])
            return False, status
        except Exception:  # noqa: BLE001
            log.exception("push: send error endpoint=%s", subscription["endpoint_hash"][:12])
            return False, None

    def notify_event(self, event: DetectionEvent, *, test: bool = False) -> dict:
        """Claim the event (unless test) and fan out to every subscription."""
        if not self.enabled:
            return {"ok": False, "detail": "push not configured", "sent": 0}
        if self.db is None:
            return {"ok": False, "detail": "database unavailable", "sent": 0}
        if not test:
            if not self.db.claim_push_event(event.id, event.camera_id):
                return {"ok": True, "deduped": True, "sent": 0}
        payload = notification_payload(event, test=test)
        rows = self.db.list_push_subscriptions()
        sent = failed = 0
        for row in rows:
            ok, _ = self.send_raw(row, payload)
            if ok:
                sent += 1
            else:
                failed += 1
        return {"ok": True, "sent": sent, "failed": failed, "subscribers": len(rows),
                "event_id": event.id, "payload_url": payload["url"]}

    def notify_user(self, user_sub: str, event: DetectionEvent, *, test: bool = True) -> dict:
        if not self.enabled:
            raise PushError(503, "push not configured")
        if self.db is None:
            raise PushError(503, "database unavailable")
        rows = self.db.push_subscriptions_for_user(user_sub)
        if not rows:
            raise PushError(404, "no subscription for this user")
        payload = notification_payload(event, test=test)
        sent = failed = 0
        for row in rows:
            ok, _ = self.send_raw(row, payload)
            if ok:
                sent += 1
            else:
                failed += 1
        return {"ok": True, "sent": sent, "failed": failed, "payload_url": payload["url"]}


class PushNotifier:
    """Poll cams_detect.events and send Web Push for newly claimed detections."""

    def __init__(self, push: PushService, db, cfg: dict):
        self.push = push
        self.db = db
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.watermark = time.time() - float(cfg.get("lookback_s", 120))

    def run_once(self) -> int:
        if not self.cfg.get("poll_enabled", True) or not self.push.enabled or self.db is None:
            return 0
        labels = self.cfg.get("labels")
        label_set = {str(x).lower() for x in labels} if labels else None
        try:
            events = self.db.detection_events_for_push(self.watermark - float(self.cfg.get("lookback_s", 120)))
        except Exception:
            log.warning("push notifier: poll failed (check SELECT grant on cams_detect.events)",
                        exc_info=True)
            return 0
        n = 0
        newest = self.watermark
        for raw in events:
            newest = max(newest, float(raw["start_ts"]))
            label = str(raw.get("label") or "detection")
            if label_set is not None and label.lower() not in label_set:
                # Still claim so a later label-filter change does not flood old events.
                self.db.claim_push_event(int(raw["id"]), raw["camera_id"])
                continue
            event = DetectionEvent(
                id=int(raw["id"]),
                camera_id=str(raw["camera_id"]),
                label=label,
                score=float(raw["score"]) if raw.get("score") is not None else None,
                start_ts=float(raw["start_ts"]),
                end_ts=float(raw["end_ts"]) if raw.get("end_ts") is not None else None,
                camera_name=raw.get("camera_name"),
            )
            result = self.push.notify_event(event)
            if result.get("sent"):
                n += result["sent"]
        self.watermark = max(self.watermark, newest, time.time())
        return n

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("push notifier: unexpected error")
            self.stop_event.wait(float(self.cfg.get("interval_s", 15)))

    def start(self) -> None:
        if self.db is not None and self.cfg.get("enabled", True) and self.cfg.get("poll_enabled", True):
            self.thread = threading.Thread(target=self._run, name="push-notifier", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=35)
