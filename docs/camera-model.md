# Camera model: one camera object, stored in MariaDB

This is the design for how cameras are defined across the camera system. The same file is in
`cams-record/docs/` and `cams-detect/docs/`; keep both copies identical, like `camera-access.md`.
cams-record owns the `cams_config` database and its code (`cams_record/camconfig/`); cams-detect
reads it with `cams_detect/camconfig.py`.

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
| cams-record defaults `main_stream` to the camera id (the low-res stream); cams-detect defaults it to `<id>-main` | **Warned** at startup; the importer refuses a camera when detect's high-res target disagrees with the recorded main while `hires_snapshot` is on |
| Detect/live go2rtc name is neither recorded main nor sub (e.g. live `back-yard`, record `back-yard-main` + `back-yard-raw`) | **Supported**: stored as an extra stream key; record and detect aliases are not retargeted |
| Detect-only camera omits `main_stream` and `{id}-main` is absent from go2rtc | **Supported**: no synthesized HD / verify stream (low-res only) |
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
| cams-detect | reads everything; writes only `camera_detection` (mask editor, detection switch), bumping `config_revision` and appending to `config_audit` |
| App Hub | nothing directly: it keeps reading `cams.json`, which cams-record publishes from cams_config (`access.publish_registry`) |
| Reporting (Grafana, Home Assistant SQL) | optional read-only user |

## Safety rules

1. **A database outage never stops recording or detection.** Services keep the last good camera
   objects in a local cache file and keep running on them while they retry.
2. **Permissions still fail closed.** Every service keeps enforcing `cams.json` exactly as today: a
   missing or unreadable file denies everyone. cams-record only replaces it with a complete, valid
   file, and a database outage leaves the last published file in place; no access can change while
   the database is down, so an outage cannot widen access.
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
FLUSH PRIVILEGES;
```

Then create the tables (cams-record does it on first use; `python -m cams_record.camconfig show`
is enough) and give cams-detect exactly what it needs. Table grants need the tables to exist:

```sql
GRANT SELECT ON cams_config.* TO 'cams_detect'@'127.0.0.1';
GRANT INSERT, UPDATE ON cams_config.camera_detection TO 'cams_detect'@'127.0.0.1';
GRANT UPDATE ON cams_config.config_revision TO 'cams_detect'@'127.0.0.1';
GRANT INSERT ON cams_config.config_audit TO 'cams_detect'@'127.0.0.1';
FLUSH PRIVILEGES;
```

cams-detect's CI runs its tests as a user with exactly these grants
(`scripts/grant_camconfig_test.py`), and checks it cannot write anything else.

Use the host part (`127.0.0.1`, `%`, LAN range) your existing `cams_record` and `cams_detect` users
have. Both services connect with the `cams_config` section of their `config.yaml` (defaults: same
host, their existing user and password file) or `CAMS_CONFIG_DB_*` environment variables.

## Importing today's cameras

Run inside the cams-record container. Mount the cams-detect config and the hub registry read-only
for this one command if they are not already visible:

```bash
python -m cams_record.camconfig import --detect /detect-config/cameras.yaml --record /config/cameras.yaml --record-config /config/config.yaml --registry /apphub-data/cams.json
```

It prints `CONFLICT` (must be fixed in the files first), `DIFFERENCE` (would change behaviour) and
`WARNING` (read before applying) lines. Add `-v` to see the planned camera objects. When there are no
conflicts or differences, add `--apply`. Applying changes no behaviour: the services only read
cams_config once `camera_source` is set to `shadow` or `db`.

To check, `python -m cams_record.camconfig show` prints the stored objects, and
`python -m cams_record.camconfig export-registry` prints the `cams.json` cams_config would produce.

## Shadow mode and switching (phases 3 and 4)

Both services have `camera_source` (`CAMERA_SOURCE`): `file` (default, today's behaviour),
`shadow` or `db`. Nothing changes until it is set.

### Shadow

Set `camera_source: shadow` in both services and restart them.

| | cams-record | cams-detect |
|---|---|---|
| Uses | `cameras.yaml` and `config.yaml` retention, as before | `cameras.yaml`, as before |
| Compares | streams recorded, main/sub stream names (they also authorize old footage), retention overrides | every resolved setting (streams, masks, zones, classes, motion, fps, ...), names, cameras missing on either side |
| Edits | none (cams-record's files are not edited at runtime) | mask editor and detection switch write `cameras.yaml` and are mirrored into `camera_detection`; a failed mirror never fails the edit |
| Report | log lines `cams_config shadow: ...`, and `/cams-record/api/status` -> `camera_source.differences` | log lines `cams_config shadow: ...`, and `/cams-detect/api/status` -> `service.camera_source.differences` (admins) |

`differences: []` means switching that service changes nothing. A camera added on the cams-detect
Cameras page during shadow shows as "only in cameras.yaml": re-run the import (it only adds new
cameras) to bring it in. When detect and hub display names differ, each surface keeps its own
(detect settings keep the cameras.yaml name; the hub registry name stays on the camera object).
Entity ids do not change.

Phase 3 is done when both reports stay empty for a period you trust, including a mask edit and a
database restart.

### Switching

One service at a time; each step is a setting and a restart.

1. **cams-record `camera_source: db`.** Recorders follow cams_config. On a change only the affected
   recorders restart (the old ffmpeg is joined before the new one starts); the access map and
   retention overrides switch with them.
2. **cams-detect `camera_source: db`.** Cameras, streams and detection settings come from cams_config
   and hot-reload; unchanged cameras keep running untouched. The mask editor and detection switch
   save to cams_config. Adding or renaming a camera on the Cameras page answers 409: that is done in
   the camera app's admin pages. A camera cams-detect cannot use (for example an id over 48
   characters) is skipped and listed under `camera_source.skipped`.
3. **cams-record publishes `cams.json`** (`access.publish_registry: true`, db mode only). Before
   enabling:
   - mount the dedicated registry directory read-write for cams-record (it is `:ro` today), and keep
     it read-only for cams-detect;
   - stop the hub from editing `cams.json` itself: from now on cameras and access are changed in
     cams_config, and any other write is overwritten on the next change.

   cams-record writes the file by atomic rename only when its content changes, and refuses (keeping
   the old file, error in `camera_source.registry.error`) if the registry would drop any camera.

### When the database is unavailable

| Situation | cams-record | cams-detect |
|---|---|---|
| Database down while running | keeps recording the loaded cameras; retries every `poll_s` | keeps detecting; saving masks or the detection switch answers 503 |
| Database down at startup, cache present | starts from the cache file | starts from the cache file |
| Neither database nor cache | records from `cameras.yaml`, switches when cams_config loads | runs on `cameras.yaml` read-only (edits answer 503), switches when cams_config loads |
| `cams.json` | the published file stays as it is | unaffected (it reads `cams.json` as today) |

Cache files: cams-record `CAMS_CONFIG_CACHE_FILE` (compose: `./data/cams_config.json`), cams-detect
`storage.data_dir/cams_config.json` (`./data`). Both are rewritten after every successful load.

### Rollback

Set `camera_source: file` and restart. To keep changes made in cams_config since the switch, first
write the files from it:

```bash
python -m cams_record.camconfig export-files --record-out /tmp/record-cameras.yaml --detect-out /tmp/detect-cameras.yaml
```

It prints the `retention.cameras` section for cams-record's `config.yaml`. Review the files, then
put them in place of each service's `config/cameras.yaml`. They behave like the originals
(`tests/test_camsource.py::test_export_files_round_trip`); comments are not kept.

### Keeping the two repositories in step

cams-record owns the schema and the views. `scripts/gen_camconfig_contracts.py` writes
`contracts/camconfig_schema.sql` and `contracts/camconfig_vectors.json`; copy both into cams-detect's
`contracts/` whenever they change. cams-record's tests fail while they are stale, and cams-detect's
tests check its reader against the vectors and (when both repositories are checked out side by
side) that the copies are identical.

## Camera management (Admin tab)

The Cameras app (`/cams`) has an **Admin** tab for users with an `auth.admin_roles` role (default
`admin`); everyone else never sees it, and cams-record refuses its API (`/cams-record/api/admin/*`)
with 403. This is the one place to manage a camera object.

- **List**: one row per cams_config camera (never a separate row for `*-main` / HD helpers). Each row
  shows the display name, id, low-res and HD stream names, on/off, who can see it, what is recorded,
  detection state and any recorder that is not recording; a banner with the mode and, in shadow mode,
  every difference reported by cams-record and cams-detect.
- **Per-camera page**: sections for **Name**, **Streams** (low-res sub + HD main + optional snapshot;
  advanced "which stream does what" is collapsed), **Detection**, **Masks** (link to cams-detect's
  editor), **Access**, **Recording** retention, and **Audio** (separate service; see below).
- **History**: who changed what and when, from `config_audit`.
- **Safety**: the form is read-only while `camera_source` is `file` (nothing would read the changes).
  Every save carries the revision the form was loaded at, so a change made elsewhere in the meantime
  is reported instead of overwritten. Settings the form does not show (masks, zones, other detection
  settings, notification rules, connector settings) are kept as they are.

### Audio (cams-audio, not cams_config)

Audio enablement and retention are owned by `keto-guido/cams-audio` and are **not** stored in
`cams_config`. The Admin per-camera page mounts a self-contained panel (`apphub/static/camera_audio.js`)
that calls the same-origin `/cams-audio` JSON API with the App Hub `apphub_token` cookie:

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/cams-audio/api/cameras` | `[{camera_id, name, has_audio, source}]` (`source` is null when unlinked) |
| GET | `/cams-audio/api/cameras/{id}/audio` | `{id, name, type:"camera", enabled, clip_retention_days, transcript_retention_days, camera_id, config:{stream}}` |
| PATCH | `/cams-audio/api/cameras/{id}/audio` | partial `{enabled, clip_retention_days, transcript_retention_days, stream}`; retention 1–3650 |

Defaults: `enabled` on (when a source is already linked), clip retention 90 days, transcript
retention 365 days. `has_audio` decides whether the Audio section appears. If `has_audio` is true
but `source` is null, the panel still shows with **Enable** off; turning Enable on sends
`PATCH …/audio` with `{"enabled": true}`, which creates the linked source — the UI then refreshes
from the returned `source` object. If the list GET returns 404/502 or the network fails, the panel
shows "Audio service not installed". PATCH 404 = unknown camera; PATCH 409 (enabling a camera with
no audio) is shown inline.

### cams-detect Cameras / Masks / Events pages

cams-detect's pages link to the Admin tab. In `db` mode its Cameras page no longer adds or renames
cameras; it keeps the detection switch and the mask editor. **Required in cams-detect:** when listing
cameras, derive rows from cams_config (or the detect camera registry), one object per camera with
`main_stream` / HD as a property — do **not** list go2rtc `*-main` or owned `*-raw` aliases as
separate "not a camera yet" entries. Prefer linking detection toggles and masks into this Admin
per-camera page so admins are not split across two tabs.

## Phases

| # | Step | Status |
|---|---|---|
| 1 | Push alerts follow camera access; warnings for stream defaults and ids | done |
| 2 | cams_config schema, store, import with equivalence check, registry export | done |
| 3 | Shadow mode in cams-record and cams-detect | built; off until `camera_source: shadow` |
| 4 | `camera_source: db` in both, `cams.json` published from cams_config, `export-files` rollback | built; off until switched |
| 5 | Camera management in the Cameras app (Admin tab): cameras, streams, stream jobs, access, detection on/off, retention, history | built; editable in shadow and db |
| 6 | Per-camera notification rules (push, MQTT) | |
| 7 | `tunnel_webcam` connector | |

### Open questions

- Phase 4: the hub's own camera editing must be switched off before `access.publish_registry` is
  enabled (the hub's code is not in these repositories).
- The hub still lists cams-detect's pages as their own app tile. They now present themselves as part
  of the Cameras app and link to its Admin tab; removing the separate tile is a hub setting.
- Masks and zones are still edited in cams-detect's mask editor (linked from the Admin tab).
