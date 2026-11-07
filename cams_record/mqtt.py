"""Home Assistant controls for the per-camera phone alert pause (MQTT Discovery).

Optional: nothing here runs unless ``mqtt.enabled`` is true, and paho-mqtt is only imported then.

Everything lives under its own namespace (``mqtt.base_topic``, default ``cams-record``). It never
publishes or subscribes under ``cams-detect/``: those topics (motion, objects, detection switch) drive
lights and automations and stay owned by cams-detect. config.py refuses a cams-detect base topic.

Topics (base = cams-record):
  <base>/status                  online|offline, retained (also the MQTT last will)
  <base>/<cam>/pause/state       ON|OFF, retained                         -> switch.<cam>_alerts_paused
  <base>/<cam>/pause/until       ISO 8601 end time or None, retained      -> sensor.<cam>_alerts_paused_until
  <base>/<cam>/pause/option      Off|1 h|3 h|Until morning|None, retained  -> select.<cam>_alerts_pause
  <base>/<cam>/pause/set         command (from HA): ON, OFF, Off, 1 h, 3 h, Until morning, or seconds

<cam> is the camera id; entity ids use it with "-" replaced by "_", as cams-detect does.
Retained command messages are ignored, so a stray retained "1 h" cannot re-pause on every reconnect.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from . import __version__
from .pauses import PauseError, PushPauses

log = logging.getLogger(__name__)

OPTIONS = {"Off": None, "1 h": "1h", "3 h": "3h", "Until morning": "morning"}
MODE_OPTION = {v: k for k, v in OPTIONS.items() if v}
PAYLOAD_MAX = 64


def slug(cam_id: str) -> str:
    return cam_id.replace("-", "_")


def parse_command(payload: str, switch_on: str = "1h"):
    """("pause", duration) or ("resume", None) for a command payload; None when it is not one."""
    text = payload.strip()
    if not text or len(text) > PAYLOAD_MAX:
        return None
    key = text.lower().replace(" ", "")
    if key in ("off", "resume"):
        return ("resume", None)
    if key == "on":
        return ("pause", switch_on)
    if key in ("1h", "3h", "morning", "untilmorning"):
        return ("pause", key)
    if key.isdigit():
        return ("pause", key)
    return None


def discovery_messages(cam_id: str, name: str, cfg: dict) -> list[tuple[str, dict]]:
    """(config topic, payload) for one camera's three entities."""
    prefix = cfg.get("discovery_prefix", "homeassistant")
    base = cfg.get("base_topic", "cams-record")
    s = slug(cam_id)
    device = {"identifiers": [f"cams_record_{s}"], "name": name, "manufacturer": "cams-record",
              "model": "Phone alert pause", "sw_version": __version__}
    origin = {"name": "cams-record", "sw_version": __version__}
    command = f"{base}/{cam_id}/pause/set"

    def common(component: str, key: str, label: str) -> dict:
        return {"name": label, "unique_id": f"cams_record_{s}_{key}", "default_entity_id": f"{component}.{s}_{key}",
                "device": device, "origin": origin, "availability": [{"topic": f"{base}/status"}]}

    return [
        (f"{prefix}/switch/cams_record/{s}_alerts_paused/config", {
            **common("switch", "alerts_paused", "Alerts paused"),
            "state_topic": f"{base}/{cam_id}/pause/state", "command_topic": command,
            "payload_on": "ON", "payload_off": "OFF", "state_on": "ON", "state_off": "OFF",
            "icon": "mdi:bell-off"}),
        (f"{prefix}/sensor/cams_record/{s}_alerts_paused_until/config", {
            **common("sensor", "alerts_paused_until", "Alerts paused until"),
            "state_topic": f"{base}/{cam_id}/pause/until", "device_class": "timestamp",
            "icon": "mdi:bell-sleep"}),
        (f"{prefix}/select/cams_record/{s}_alerts_pause/config", {
            **common("select", "alerts_pause", "Alerts pause"),
            "state_topic": f"{base}/{cam_id}/pause/option", "command_topic": command,
            "options": list(OPTIONS), "icon": "mdi:bell-cog"}),
    ]


def _paho_client(cfg: dict, password: str | None, base: str):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.get("client_id") or "cams-record",
                         clean_session=True)
    if cfg.get("username"):
        client.username_pw_set(cfg["username"], password or None)
    client.will_set(f"{base}/status", "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.max_queued_messages_set(1000)
    return client


class AlertPauseMqtt:
    """Publishes pause state and discovery, and applies commands from Home Assistant.

    ``cameras()`` returns {camera_id: display name}: the cameras that get entities. It is re-read every
    ``refresh_s`` seconds, adding entities for new cameras and deleting them for removed ones.
    """

    def __init__(self, cfg: dict, pauses: PushPauses, cameras: Callable[[], dict[str, str]],
                 password: str | None = None, client=None):
        self.cfg = cfg
        self.pauses = pauses
        self.cameras = cameras
        self.base = cfg.get("base_topic", "cams-record")
        self.prefix = cfg.get("discovery_prefix", "homeassistant")
        self.switch_on = cfg.get("switch_on", "1h")
        self.refresh_s = float(cfg.get("refresh_s", 30))
        self.connected = False
        self._lock = threading.Lock()  # _state
        self._sync = threading.RLock()  # _known: publish_all runs on paho's thread and on the worker
        self._known: dict[str, str] = {}  # camera id -> name with entities published
        self._state: dict[str, tuple[str, str, str]] = {}  # camera id -> last published (state, until, option)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._resend_at: float | None = None  # monotonic: HA restarted, re-send discovery then
        self._thread: threading.Thread | None = None
        self._password = password
        self.client = client
        pauses.subscribe(self._on_pause_changed)

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        if self.client is None:
            self.client = _paho_client(self.cfg, self._password, self.base)
        self._password = None  # only needed to build the client
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        host, port = self.cfg.get("host", "127.0.0.1"), int(self.cfg.get("port", 1883))
        log.info("mqtt: alert pause controls on %s:%d as %s under %s/", host, port,
                 self.cfg.get("username") or "<anonymous>", self.base)
        self.client.connect_async(host, port, keepalive=int(self.cfg.get("keepalive", 60)))
        self.client.loop_start()
        self._thread = threading.Thread(target=self._run, name="mqtt-pauses", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.client is None:
            return
        try:
            if self.connected:
                info = self.client.publish(f"{self.base}/status", "offline", qos=1, retain=True)
                info.wait_for_publish(timeout=2)
        except Exception:  # noqa: BLE001  (shutting down; the last will covers a lost connection)
            pass
        try:
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:  # noqa: BLE001
            log.debug("mqtt: disconnect failed", exc_info=True)
        self.connected = False

    # -- paho callbacks ------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            log.error("mqtt: connect failed: %s", reason_code)
            return
        self.connected = True
        log.info("mqtt: connected")
        client.subscribe(f"{self.base}/+/pause/set", qos=1)
        client.subscribe(f"{self.prefix}/status", qos=1)
        client.publish(f"{self.base}/status", "online", qos=1, retain=True)
        self.publish_all(force=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        if not self._stop.is_set():
            log.warning("mqtt: disconnected (%s); reconnecting", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", "replace") if len(msg.payload) <= PAYLOAD_MAX else ""
            if msg.topic == f"{self.prefix}/status":
                if payload.strip() == "online":
                    # Home Assistant restarted: give it a moment, then re-send discovery and state.
                    self._resend_at = time.monotonic() + 2.0
                    self._wake.set()
                return
            if getattr(msg, "retain", False):
                log.warning("mqtt: ignoring retained command on %s", msg.topic)
                return
            self.handle_command(msg.topic, payload)
        except Exception:  # noqa: BLE001  (never let a bad message kill paho's network thread)
            log.exception("mqtt: message handling failed")

    # -- commands ----------------------------------------------------------------
    def handle_command(self, topic: str, payload: str) -> str:
        """Apply one command. Returns what happened: paused, resumed, invalid or ignored."""
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != self.base or parts[2:] != ["pause", "set"]:
            return "ignored"
        cam = parts[1]
        if cam not in self._camera_names():
            log.warning("mqtt: pause command for unknown camera ignored")
            return "ignored"
        cmd = parse_command(payload, self.switch_on)
        try:
            if cmd is None:
                raise PauseError(422, "unrecognised command")
            if cmd[0] == "resume":
                self.pauses.resume(cam, "mqtt")
                return "resumed"
            self.pauses.pause(cam, cmd[1], "mqtt")
            return "paused"
        except PauseError as exc:
            log.warning("mqtt: invalid pause command for %s: %s", cam, exc.detail)
            self.publish_state(cam, force=True)  # put HA's control back to the real state
            return "invalid"
        except Exception as exc:  # noqa: BLE001  (database down: HA keeps showing the real state)
            log.warning("mqtt: pause command for %s failed: %s", cam, exc)
            self.publish_state(cam, force=True)
            return "invalid"

    # -- publishing ----------------------------------------------------------------
    def _send(self, topic: str, payload: str) -> None:
        if self.client is not None and self.connected:
            self.client.publish(topic, payload, qos=1, retain=True)

    def _camera_names(self) -> dict[str, str]:
        try:
            cams = self.cameras() or {}
        except Exception:  # noqa: BLE001
            log.warning("mqtt: camera list unavailable", exc_info=True)
            return dict(self._known)
        return {str(k): str(v or k) for k, v in cams.items()}

    def state_of(self, cam: str) -> tuple[str, str, str]:
        p = self.pauses.get(cam)
        if p is None:
            return ("OFF", "None", "Off")
        return ("ON", self.pauses.iso(p.until_ts), MODE_OPTION.get(self.pauses.mode_of(p), "None"))

    def publish_state(self, cam: str, force: bool = False) -> None:
        state = self.state_of(cam)
        with self._lock:
            if not force and self._state.get(cam) == state:
                return
            self._state[cam] = state
        for leaf, value in zip(("state", "until", "option"), state, strict=True):
            self._send(f"{self.base}/{cam}/pause/{leaf}", value)

    def _publish_discovery(self, cam: str, name: str) -> None:
        for topic, payload in discovery_messages(cam, name, self.cfg):
            self._send(topic, json.dumps(payload, separators=(",", ":")))

    def _remove(self, cam: str) -> None:
        name = self._known.pop(cam, cam)
        for topic, _ in discovery_messages(cam, name, self.cfg):
            self._send(topic, "")
        for leaf in ("state", "until", "option"):
            self._send(f"{self.base}/{cam}/pause/{leaf}", "")
        with self._lock:
            self._state.pop(cam, None)

    def publish_all(self, force: bool = False) -> None:
        """Bring HA up to date: discovery for new/renamed cameras, removal for gone ones, all state."""
        if not self.connected:
            return
        cams = self._camera_names()
        with self._sync:
            for cam in [c for c in self._known if c not in cams]:
                self._remove(cam)
            for cam, name in sorted(cams.items()):
                if force or self._known.get(cam) != name:
                    self._publish_discovery(cam, name)
                    self._known[cam] = name
                self.publish_state(cam, force=force)

    def _on_pause_changed(self, cam: str) -> None:
        if cam in self._known:
            self.publish_state(cam)
        self._wake.set()  # the next expiry may have moved

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._resend_at is not None and time.monotonic() >= self._resend_at:
                    self._resend_at = None
                    self.publish_all(force=True)
                else:
                    self.publish_all()  # also publishes OFF for any pause that just expired
            except Exception:  # noqa: BLE001  (keep the loop alive)
                log.exception("mqtt: refresh failed")
            wait = self.refresh_s
            nxt = self.pauses.next_expiry()
            if nxt is not None:
                wait = min(wait, max(0.05, nxt - time.time() + 0.05))
            if self._resend_at is not None:
                wait = min(wait, max(0.05, self._resend_at - time.monotonic()))
            self._wake.wait(wait)
            self._wake.clear()


def build(cfg: dict, pauses: PushPauses | None, cameras: Callable[[], dict[str, str]]) -> AlertPauseMqtt | None:
    """The MQTT bridge when mqtt.enabled and everything it needs is present; otherwise None (logged)."""
    mq = cfg.get("mqtt") or {}
    if not mq.get("enabled"):
        return None
    if pauses is None:
        log.warning("mqtt: enabled but there is no database, so alert pauses (and MQTT) are off")
        return None
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        log.error("mqtt: enabled but paho-mqtt is not installed; alert pause controls are off")
        return None
    from .config import read_secret

    password = read_secret(mq.get("password_file")) if mq.get("username") else None
    if mq.get("username") and not password:
        log.warning("mqtt: no password in mqtt.password_file; connecting as %s without one", mq["username"])
    return AlertPauseMqtt(mq, pauses, cameras, password=password)
