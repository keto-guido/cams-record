"""Read and write camera objects in the cams_config database.

Every write runs in one transaction that also bumps ``config_revision`` and appends to
``config_audit``, so readers polling the revision never see half of a change.
"""

from __future__ import annotations

import json
import logging

from ..db import Database
from .model import CameraObject, Stream
from .schema import MIGRATIONS

log = logging.getLogger(__name__)


def open_db(cfg: dict) -> Database:
    """A Database on the cams_config schema (cfg: host, port, database, user, password[_file])."""
    return Database(cfg, migrations=MIGRATIONS)


def _j(value) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unj(value):
    return None if value is None else json.loads(value)


class CamConfigStore:
    def __init__(self, db: Database):
        self.db = db

    # -- reads --------------------------------------------------------------
    def revision(self) -> int:
        def q(cur):
            cur.execute("SELECT revision FROM config_revision WHERE id=1")
            row = cur.fetchone()
            return int(row[0]) if row else 0
        return self.db.run(q)

    def load_all(self) -> tuple[int, dict[str, CameraObject]]:
        """(revision, cameras) read in one consistent snapshot."""
        def q(cur):
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            try:
                return _read_all(cur)
            finally:
                cur.execute("COMMIT")
        return self.db.run(q)

    def get(self, cam_id: str) -> CameraObject | None:
        return self.load_all()[1].get(cam_id)

    # -- writes -------------------------------------------------------------
    def save(self, cam: CameraObject, actor: str, *, expect_revision: int | None = None,
             action: str | None = None) -> int:
        """Insert or fully replace one camera. Returns the new revision.

        ``expect_revision`` makes the write fail (ValueError) if anything changed since that revision,
        so two admins editing at once cannot silently overwrite each other.
        """
        return self.save_many([cam], actor, expect_revision=expect_revision, action=action)

    def save_many(self, cams: list[CameraObject], actor: str, *, expect_revision: int | None = None,
                  action: str | None = None) -> int:
        errs = [e for c in cams for e in c.validate()]
        if errs:
            raise ValueError("; ".join(errs))
        if not actor:
            raise ValueError("actor is required")

        def tx(cur):
            rev = _lock_revision(cur, expect_revision)
            _, before = _read_all(cur)
            rev += 1
            for cam in cams:
                old = before.get(cam.id)
                _write_camera(cur, cam, actor)
                _audit(cur, actor, cam.id, action or ("update" if old else "create"), rev,
                       old.to_dict() if old else None, cam.to_dict())
            cur.execute("UPDATE config_revision SET revision=%s WHERE id=1", (rev,))
            return rev
        return self._transaction(tx)

    def delete(self, cam_id: str, actor: str, *, expect_revision: int | None = None) -> int:
        def tx(cur):
            rev = _lock_revision(cur, expect_revision)
            old = _read_all(cur)[1].get(cam_id)
            if old is None:
                raise KeyError(cam_id)
            rev += 1
            cur.execute("DELETE FROM cameras WHERE id=%s", (cam_id,))  # children cascade
            _audit(cur, actor, cam_id, "delete", rev, old.to_dict(), None)
            cur.execute("UPDATE config_revision SET revision=%s WHERE id=1", (rev,))
            return rev
        return self._transaction(tx)

    def audit(self, cam_id: str | None = None, limit: int = 100) -> list[dict]:
        def q(cur):
            sql = "SELECT at, actor, camera_id, action, revision, before_json, after_json FROM config_audit"
            args: tuple = ()
            if cam_id:
                sql += " WHERE camera_id=%s"
                args = (cam_id,)
            cur.execute(sql + " ORDER BY id DESC LIMIT %s", (*args, int(limit)))
            keys = ("at", "actor", "camera_id", "action", "revision", "before", "after")
            return [dict(zip(keys, (*r[:5], _unj(r[5]), _unj(r[6])), strict=True)) for r in cur.fetchall()]
        return self.db.run(q)

    def _transaction(self, fn):
        def run(cur):
            cur.execute("START TRANSACTION")
            try:
                result = fn(cur)
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            cur.execute("COMMIT")
            return result
        return self.db.run(run)


def _lock_revision(cur, expect: int | None) -> int:
    cur.execute("SELECT revision FROM config_revision WHERE id=1 FOR UPDATE")
    row = cur.fetchone()
    rev = int(row[0]) if row else 0
    if row is None:
        cur.execute("INSERT INTO config_revision (id, revision) VALUES (1, 0)")
    if expect is not None and expect != rev:
        raise ValueError(f"camera settings changed since revision {expect} (now {rev}); reload and retry")
    return rev


def _audit(cur, actor, cam_id, action, rev, before, after) -> None:
    cur.execute("""INSERT INTO config_audit (actor, camera_id, action, revision, before_json, after_json)
                   VALUES (%s, %s, %s, %s, %s, %s)""", (actor[:128], cam_id, action, rev, _j(before), _j(after)))


def _write_camera(cur, cam: CameraObject, actor: str) -> None:
    cur.execute("""INSERT INTO cameras (id, name, enabled, connector_type, connector_json, visibility,
                       sort_order, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE name=VALUES(name), enabled=VALUES(enabled),
                     connector_type=VALUES(connector_type), connector_json=VALUES(connector_json),
                     visibility=VALUES(visibility), sort_order=VALUES(sort_order), updated_by=VALUES(updated_by)""",
                (cam.id, cam.name, int(cam.enabled), cam.connector_type, _j(cam.connector or None),
                 cam.visibility, int(cam.sort_order), actor[:128]))
    # Replace the child rows wholesale: simpler and exact; the revision bump covers the whole change.
    for table in ("camera_stream_use", "camera_access", "camera_notify_rules"):
        cur.execute(f"DELETE FROM {table} WHERE camera_id=%s", (cam.id,))  # noqa: S608 (fixed names)
    cur.execute("DELETE FROM camera_streams WHERE camera_id=%s", (cam.id,))
    for s in cam.streams.values():
        cur.execute("""INSERT INTO camera_streams (camera_id, stream_key, go2rtc_name, width, height, codec, fps)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (cam.id, s.key, s.go2rtc_name, s.width, s.height, s.codec, s.fps))
    for purpose, keys in cam.use.items():
        for k in keys:
            cur.execute("INSERT INTO camera_stream_use (camera_id, purpose, stream_key) VALUES (%s, %s, %s)",
                        (cam.id, purpose, k))
    for kind, names in (("user", cam.users), ("role", cam.roles)):
        for n in sorted(set(names)):
            cur.execute("INSERT INTO camera_access (camera_id, kind, principal) VALUES (%s, %s, %s)",
                        (cam.id, kind, n))
    if cam.detection is None:
        cur.execute("DELETE FROM camera_detection WHERE camera_id=%s", (cam.id,))
    else:
        cur.execute("""INSERT INTO camera_detection (camera_id, settings_json, updated_by) VALUES (%s, %s, %s)
                       ON DUPLICATE KEY UPDATE settings_json=VALUES(settings_json), updated_by=VALUES(updated_by)""",
                    (cam.id, _j(cam.detection), actor[:128]))
    if cam.recording is None:
        cur.execute("DELETE FROM camera_recording WHERE camera_id=%s", (cam.id,))
    else:
        cur.execute("""INSERT INTO camera_recording (camera_id, main_days, sub_days) VALUES (%s, %s, %s)
                       ON DUPLICATE KEY UPDATE main_days=VALUES(main_days), sub_days=VALUES(sub_days)""",
                    (cam.id, cam.recording.get("main_days"), cam.recording.get("sub_days")))
    for rule in cam.notify:
        cur.execute("""INSERT INTO camera_notify_rules (camera_id, channel, enabled, labels_json, zones_json,
                           verified_only, cooldown_s, quiet_json, recipients_json, updated_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (cam.id, rule.get("channel", "push"), int(rule.get("enabled", True)), _j(rule.get("labels")),
                     _j(rule.get("zones")), int(bool(rule.get("verified_only"))), int(rule.get("cooldown_s") or 0),
                     _j(rule.get("quiet")), _j(rule.get("recipients")), actor[:128]))


def _num(v):
    return None if v is None else float(v)


def _read_all(cur) -> tuple[int, dict[str, CameraObject]]:
    cur.execute("SELECT revision FROM config_revision WHERE id=1")
    row = cur.fetchone()
    rev = int(row[0]) if row else 0
    cur.execute("""SELECT id, name, enabled, connector_type, connector_json, visibility, sort_order
                   FROM cameras ORDER BY sort_order, id""")
    cams = {r[0]: CameraObject(id=r[0], name=r[1], enabled=bool(r[2]), connector_type=r[3],
                               connector=_unj(r[4]) or {}, visibility=r[5], sort_order=int(r[6]))
            for r in cur.fetchall()}
    cur.execute("SELECT camera_id, stream_key, go2rtc_name, width, height, codec, fps FROM camera_streams")
    for cid, key, name, w, h, codec, fps in cur.fetchall():
        cams[cid].streams[key] = Stream(key, name, w, h, codec, _num(fps))
    cur.execute("SELECT camera_id, purpose, stream_key FROM camera_stream_use ORDER BY camera_id, purpose, stream_key")
    for cid, purpose, key in cur.fetchall():
        cams[cid].use[purpose] = (*cams[cid].use.get(purpose, ()), key)
    users: dict[str, list[str]] = {}
    roles: dict[str, list[str]] = {}
    cur.execute("SELECT camera_id, kind, principal FROM camera_access ORDER BY principal")
    for cid, kind, principal in cur.fetchall():
        (users if kind == "user" else roles).setdefault(cid, []).append(principal)
    for cid, cam in cams.items():
        cam.users, cam.roles = tuple(users.get(cid, ())), tuple(roles.get(cid, ()))
    cur.execute("SELECT camera_id, settings_json FROM camera_detection")
    for cid, settings in cur.fetchall():
        cams[cid].detection = json.loads(settings)
    cur.execute("SELECT camera_id, main_days, sub_days FROM camera_recording")
    for cid, main_days, sub_days in cur.fetchall():
        cams[cid].recording = {k: _num(v) for k, v in (("main_days", main_days), ("sub_days", sub_days))
                               if v is not None}
    cur.execute("""SELECT camera_id, channel, enabled, labels_json, zones_json, verified_only, cooldown_s,
                          quiet_json, recipients_json FROM camera_notify_rules ORDER BY id""")
    for cid, channel, enabled, labels, zones, verified, cooldown, quiet, recipients in cur.fetchall():
        cams[cid].notify.append({"channel": channel, "enabled": bool(enabled), "labels": _unj(labels),
                                 "zones": _unj(zones), "verified_only": bool(verified), "cooldown_s": int(cooldown),
                                 "quiet": _unj(quiet), "recipients": _unj(recipients)})
    return rev, cams
