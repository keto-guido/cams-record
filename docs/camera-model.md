# Camera model: one camera object, stored in MariaDB

This is the design for how cameras are defined across the camera system. The same file is in
`cams-record/docs/` and `cams-detect/docs/`; keep both copies identical, like `camera-access.md`.
cams-record owns the `cams_config` database and its code (`cams_record/camconfig/`).

## Goals

- **One camera app on the hub.** App Hub is a launcher. The whole camera system (live view,
  recordings, detections, alerts and its admin pages) is a single app tile on the hub page.
- **Admins manage cameras and who sees them.** Admin pages inside the camera app add cameras, pick
  streams, choose which stream does which job, set detection and notification rules, and decide
  which users and roles can see each camera. Only the `admin` role reaches those pages.
- **A camera is an object** with its own settings and permissions, stored in MariaDB in its own
  database, `cams_config`.
- **Adding a camera is easy and modular**: pick its go2rtc streams, choose live, detection and
  recording streams. Defaults: low-res for live and detection, high-res for recording and for
  verifying detections.
- **More connectors later.** Today every camera is RTSP behind go2rtc. A remote webcam connector over
  a VPN tunnel comes next, without changing detection, recording or permissions.
- **Per-camera detection rules and notification rules**, with MQTT (Home Assistant) as one of the
  notification layers.
- **The system is in use**: every step keeps today's behaviour unless the new data says otherwise,
  and every switch can be rolled back.

## How cameras are defined today

| Where | What it defines | Live reload |
|---|---|---|
| `go2rtc.yaml` on the NAS (no repo) | RTSP URLs and credentials, as named streams (`front-door`, `front-door-main`) | yes |
| App Hub `cams.json` | `id`, `name`, `snap`, `users`, `roles`: who can see what | yes (stat change) |
| `cams-detect/config/cameras.yaml` | detection stream, high-res stream, masks, zones, classes, motion | yes (also written by the mask editor and Cameras page) |
| `cams-record/config/cameras.yaml` | main/sub streams, record flags; retention overrides in `config.yaml` | no |

Which stream does which job follows from naming, not settings:

- Live view (`apphub/cams.js`) plays go2rtc stream `<camera id>`, the low-res stream.
- cams-detect detects on `stream` and verifies and grabs HD snapshots from `main_stream`
  (default `<stream>-main`).
- cams-record records `main_stream` and `sub_stream`.
- MQTT topics and Home Assistant entities are keyed by camera id, so **ids never change**.

### Problems found (November 2026)

| Problem | Status |
|---|---|
| Push alerts went to every subscriber, ignoring per-camera access | **Fixed**: alerts follow the registry rule; roles saved per subscription (cams-record migration v7, `docs/web-push.md`) |
| cams-record defaults `main_stream` to the camera id (the low-res stream); cams-detect defaults it to `<id>-main` | **Warned** at startup; the importer refuses such cameras until `main_stream` is set |
| Camera id rules differ: detect `[a-z0-9_-]{1,48}`, record `{1,64}`, registry no `_` and no trailing `-` | **Warned** in both; new ids from the Cameras page are always registry-valid; cams_config enforces the registry rule |
| Notification rules are global (`push.labels`) | Planned: `camera_notify_rules` |
| Adding a camera means editing four files | Planned: admin pages writing cams_config |

## Data model: database `cams_config`

Schema: `cams_record/camconfig/schema.py`. The camera object in code:
`cams_record/camconfig/model.py` (`CameraObject`).

| Table | Holds |
|---|---|
| `cameras` | `id` (registry rules, fixed for life), `name`, `enabled`, `connector_type` (`go2rtc`, later `tunnel_webcam`), `connector_json` (non-secret), `visibility` (`everyone` / `restricted`), `sort_order`, `updated_by` |
| `camera_streams` | one row per stream: `stream_key` (`main`, `sub`, `snap`, ...), `go2rtc_name`, probed width/height/codec/fps |
| `camera_stream_use` | which stream does each job: `live`, `live_hd`, `detect`, `verify`, `snapshot`, `record` (`record` may list several) |
| `camera_access` | grants for restricted cameras: `kind` (`user`/`role`) and `principal`, exact and case-sensitive |
| `camera_detection` | cams-detect settings as JSON: everything in today's `cameras.yaml` entry except `name`, `stream`, `main_stream` |
| `camera_recording` | per-camera retention overrides (`main_days`, `sub_days`) |
| `camera_notify_rules` | per camera and channel (`push`, `mqtt`): labels, zones, verified-only, cooldown, quiet hours, recipients |
| `config_revision` | one counter, bumped in the same transaction as every change; readers poll it |
| `config_audit` | who changed what: actor, camera, action, revision, before/after JSON |

**Secrets never go in the database.** RTSP credentials stay in `go2rtc.yaml`; tunnel keys for remote
webcams will live in their own secret files.

### Stream purposes

`camera_stream_use` is authoritative: what is stored is what is used, and a missing purpose is off
(no `record` rows = not recorded). Defaults only pre-fill a new camera:

| Purpose | Default | Used by |
|---|---|---|
| `live` | `sub` | hub live view and tiles |
| `live_hd` | `main` | "HD" toggle in live view (planned) |
| `detect` | `sub` | cams-detect motion and object detection |
| `verify` | `main` | cams-detect verification and HD event snapshots |
| `snapshot` | `sub` | hub tile snapshots (`snap` in cams.json) |
| `record` | `main` and `sub` | cams-record (segments are filed under quality `main`/`sub`, so only those keys can be recorded for now) |

Until the hub reads cams_config itself, its live view still plays the go2rtc stream named after the
camera id, so the `live` stream should keep that name; the importer warns when it does not.

### Access

- `visibility: everyone`: every user of the camera app.
- `visibility: restricted`: admins, plus the users and roles in `camera_access`. With no grants,
  admins only. **New cameras start restricted**, so adding a camera never exposes it by accident.
- The `admin` role always sees every registered camera and reaches the admin pages.
- The registry rules in `docs/camera-access.md` are unchanged. cams_config exports `cams.json`
  (`CameraObject.registry_entry`); a restricted camera with no grants is exported as
  `roles: ["admin"]`. Every case in `apphub/camera_acl_vectors.json` gives the same answer after an
  import and export (`tests/test_camconfig.py`).
- Detection alerts use the same decision per subscriber (`docs/web-push.md`). MQTT is not per-user:
  Home Assistant sees every camera, so MQTT rules are about what is published, not who sees it.

### Notifications and MQTT (planned)

`camera_notify_rules` holds one rule per camera and channel:

- **push**: which labels and zones alert, verified-only, cooldown, quiet hours, and optionally a
  recipient list. Recipients are always narrowed further by camera access.
- **mqtt**: what cams-detect publishes for the camera beyond the always-on motion, occupancy and
  availability entities: for example, events only for verified detections or only in some zones.

Global `push.labels` stays the default for cameras without a push rule.

### Connectors

- `go2rtc`: today's cameras. The camera object only names go2rtc streams.
- `tunnel_webcam` (planned): a remote webcam publishes into go2rtc over a VPN tunnel (WireGuard),
  for example as RTSP publish or WebRTC. Once its stream exists in go2rtc, detection, recording,
  live view and permissions work unchanged. `connector_json` holds non-secret settings such as the
  peer name; keys stay in secret files.

## Who reads and writes cams_config

| Component | Access |
|---|---|
| cams-record (camera app backend, admin pages) | owns the schema (runs migrations), reads and writes everything |
| cams-detect | reads everything; will write only `camera_detection` (mask editor) |
| App Hub | nothing directly: it keeps reading `cams.json`, generated from cams_config |
| Reporting (Grafana, Home Assistant SQL) | optional read-only user |

## Safety rules

1. **A database outage never stops recording or detection.** Services keep the last good camera
   objects in a local cache file and keep running on them while they retry.
2. **Permissions still fail closed.** If access data cannot be refreshed within a set limit, access is
   denied, as today when `cams.json` is unreadable.
3. **Reloads are cheap and exact.** Readers poll `config_revision` every few seconds and reload only
   when it changes; writes are single transactions, so a reader never sees half a change.
4. **Concurrent admin edits cannot overwrite each other**: writes carry the revision they were based on
   (`expect_revision`).
5. **Switching is per service and reversible**: `camera_source: file | shadow | db`. In `shadow` a
   service reads both, keeps using its files and logs any difference. Going back to `file` is the
   rollback.
6. **The import changes nothing silently.** It is a dry run by default, refuses to write while files
   disagree, proves each service's view is unchanged (`check_plan`), and never overwrites a camera
   already in cams_config.

## Setting up cams_config

As a MariaDB admin on the NAS:

```sql
CREATE DATABASE IF NOT EXISTS cams_config CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON cams_config.* TO 'cams_record'@'127.0.0.1';
GRANT SELECT ON cams_config.* TO 'cams_detect'@'127.0.0.1';
FLUSH PRIVILEGES;
```

Use the host part (`127.0.0.1`, `%`, LAN range) your existing `cams_record` and `cams_detect` users
have. cams-record connects with the `cams_config` section of its `config.yaml` (defaults: same host,
user `cams_record`, its existing password file) or `CAMS_CONFIG_DB_*` environment variables. Tables
are created on first use.

## Importing today's cameras

Run inside the cams-record container. Mount the cams-detect config and the hub registry read-only
for this one command if they are not already visible:

```bash
python -m cams_record.camconfig import --detect /detect-config/cameras.yaml --record /config/cameras.yaml --record-config /config/config.yaml --registry /apphub-data/cams.json
```

It prints `CONFLICT` (must be fixed in the files first), `DIFFERENCE` (would change behaviour) and
`WARNING` (read before applying) lines. Add `-v` to see the planned camera objects. When there are no
conflicts or differences, add `--apply`. Nothing in the running services reads cams_config yet, so
applying changes no behaviour.

To check, `python -m cams_record.camconfig show` prints the stored objects, and
`python -m cams_record.camconfig export-registry` prints the `cams.json` cams_config would produce.

## Phases

| # | Step | Status |
|---|---|---|
| 1 | Push alerts follow camera access; warnings for stream defaults and ids | done |
| 2 | cams_config schema, store, import with equivalence check, registry export | done |
| 3 | Shadow mode: cams-record and cams-detect read cams_config, compare with files, keep using files | next |
| 4 | Switch each service to `camera_source: db`; cams-record publishes `cams.json` from cams_config | |
| 5 | Admin pages in the camera app: cameras, streams and purposes, access, detection | |
| 6 | Per-camera notification rules (push, MQTT) | |
| 7 | `tunnel_webcam` connector | |

### Open questions

- Phase 4: cams-record will write `cams.json` for the hub, so it needs a writable mount of the
  dedicated registry directory (today read-only). Alternatively the hub reads a small endpoint from
  the camera app.
- Phase 5: the admin pages need the hub to send the `admin` role in the JWT to the camera app (it
  already does for cams-detect writes).
