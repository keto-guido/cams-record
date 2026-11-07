# Per-camera phone alert pause

Pause the **phone notifications** (Web Push) for one camera for 1 h, 3 h, until the next 07:00, or
a number of seconds. Everything else keeps running:

| Keeps running while paused | Why it matters |
|---|---|
| Recording, retention, detection protection | footage is still kept |
| cams-detect detection and its event log (`cams_detect.events`) | the Detections list still fills |
| cams-detect MQTT (`cams-detect/<cam>/...`) | Home Assistant lights and automations still fire |
| Test notifications (`POST /api/push/test`) | "Send test" still proves a device works |

A pause applies to the camera, so it silences that camera for **every** subscriber. That is why only
the camera's owners and admins can set it (see [Who can pause](#who-can-pause)).

## Behaviour

- **Skip and claim.** Both ways a detection reaches push, the `cams_detect.events` poller
  (`PushNotifier.run_once`) and the cams-detect webhook (`POST /api/internal/detection-event`), go
  through `PushService.notify_event`. It claims the event in `push_sent_events` first and only then
  checks the pause, so a paused camera's events are marked handled. Resuming does not release a
  backlog of old alerts, and a webhook retry of a paused event answers `deduped`.
- **Expiry.** A row whose `until_ts` has passed is ignored everywhere (SQL and in memory). There is
  no cleanup job: the next pause write deletes expired rows.
- **Until morning** is the next 07:00 in `clips.timezone` (default `America/Chicago`) strictly after
  now, computed on the server from the local calendar date. The night a DST change happens is 8 h
  or 10 h long instead of 9 h, and still ends at 07:00 local (CST `13:00Z`, CDT `12:00Z`). At exactly
  07:00 the next morning is tomorrow.
- **Seconds**: a whole number from 1 to 604800 (7 days).
- **Cache.** The active pauses are held in memory; a lookup costs a dict read. A pause or resume made
  through this process (HTTP or MQTT) replaces the cache at once, an entry is dropped the instant it
  expires, and otherwise the table is re-read at most every `push.pause_refresh_s` (30 s) to pick up
  changes made elsewhere. If the database cannot be read the last known set is kept (alerts are
  never stuck paused forever, and an unreachable database already stops pushes at the claim step).

## Who can pause

| Viewer | Sees the camera's pause state | Can pause / resume |
|---|---|---|
| Admin role (`auth.admin_roles`) | every registered camera | yes |
| User named in the camera's registry `users` ("owner") | yes | yes |
| Granted through `roles`, or the camera is open to everyone | yes | no (403) |
| Cannot view the camera, or unknown camera | no: never listed | 404, same body as an unknown camera |

Viewing uses the same registry decision as detection alerts (`CameraAccess.can_view_camera`,
`docs/camera-access.md`). A token whose `x-apphub-user` disagrees with its `sub` sees nothing.

## HTTP API

Under `/cams-record` (prefix optional), App Hub JWT with `cams` or `admin`. Responses are
`Cache-Control: private, no-store`.

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/push/pauses` | | `{timezone, now, morning_ts, options, max_seconds, cameras: [...]}` for the cameras the caller can view |
| PUT | `/api/push/pauses/{cam}` | `{"duration": "1h" \| "3h" \| "morning" \| <seconds>}` | the camera entry |
| DELETE | `/api/push/pauses/{cam}` | | the camera entry (`paused: false`) |

Camera entry: `{camera_id, name, paused, until_ts, until (ISO 8601 with offset), mode ("1h", "3h",
"morning" or null for a custom length), set_by (only for those who can edit), can_edit}`.

Errors: 400 malformed camera id or JSON, 401/403 auth, 403 not an owner/admin or a cross-site
request (`Sec-Fetch-Site` other than `same-origin`/`none`), 404 unknown or hidden camera, 415 PUT body
not `application/json` (a cross-site form cannot send JSON without a CORS preflight, the same rule
as `/api/admin`), 422 bad duration or extra keys, 503 no database.

Example:

```bash
curl -s -X PUT -H "x-apphub-token: $TOKEN" -H 'content-type: application/json' \
  -d '{"duration":"morning"}' http://127.0.0.1:8097/cams-record/api/push/pauses/front-door
```

## Database

`push_pauses` is created by schema migration **v8** (`CREATE TABLE IF NOT EXISTS`, applied on the
first connection after start, idempotent if re-run):

| column | |
|---|---|
| `camera_id VARCHAR(64)` | primary key |
| `until_ts DATETIME(3)` | UTC; the pause ends here |
| `set_by VARCHAR(128)` | App Hub user, or `mqtt` |
| `set_at DATETIME(3)` | UTC |

The `cams_record` MariaDB user already owns this database, so no new grant is needed.

## Home Assistant (MQTT)

`cams_record/mqtt.py`, **off by default**. With `mqtt.enabled: false` (or no database, or paho-mqtt
missing, each logged once) nothing connects and nothing else changes.

It uses its own namespace, `cams-record/`, and never publishes or subscribes under `cams-detect/`.
`mqtt.base_topic` refuses anything starting with `cams-detect`. Discovery goes to
`homeassistant/<component>/cams_record/...`, a different node id from cams-detect's, under a separate
HA device per camera (`cams_record_<cam>`), so cams-detect's devices and entities are untouched.

Per camera in the registry (`<cam>` = camera id; entity ids use `_` for `-`):

| Entity | Topic(s) | Values |
|---|---|---|
| `switch.<cam>_alerts_paused` | state `cams-record/<cam>/pause/state` | `ON` / `OFF` (ON pauses for `mqtt.switch_on`, default 1 h) |
| `sensor.<cam>_alerts_paused_until` (timestamp) | `cams-record/<cam>/pause/until` | ISO 8601, or `None` (unknown) |
| `select.<cam>_alerts_pause` | `cams-record/<cam>/pause/option` | `Off`, `1 h`, `3 h`, `Until morning` (`None` for a custom length) |
| commands (switch and select) | `cams-record/<cam>/pause/set` | `ON`, `OFF`, `Off`, `1 h`, `3 h`, `Until morning`, or seconds |

- All state and discovery is retained (QoS 1). `cams-record/status` is `online`/`offline` with a last
  will, and every entity uses it for availability.
- Discovery and state are sent at connect (so after every reconnect) and again 2 s after Home
  Assistant announces `homeassistant/status: online`. The camera list is re-read every
  `mqtt.refresh_s` (30 s): new cameras get entities, removed ones are deleted (empty retained config)
  and their retained state cleared.
- Changes made through the HTTP API are published at once; an expiry is published when it happens
  (the worker sleeps until the next `until_ts`).
- Invalid commands, unknown cameras and **retained** command messages are ignored (logged); after an
  invalid command the real state is re-published so HA's control snaps back.
- On shutdown it publishes `offline`, disconnects and stops paho's loop.

Example automation (pause the driveway until morning when the house goes to bed):

```yaml
action: select.select_option
target: {entity_id: select.driveway_alerts_pause}
data: {option: Until morning}
```

## Configuration

```yaml
push:
  pause_refresh_s: 30          # re-read push_pauses at most this often (own changes apply at once)
clips:
  timezone: America/Chicago    # "Until morning" = next 07:00 here
mqtt:
  enabled: false               # true to publish Home Assistant controls
  host: 127.0.0.1              # the broker (Mosquitto on the HA host); same as cams-detect's
  port: 1883
  username: cams-record        # empty = anonymous
  password_file: /run/secrets/mqtt_password   # read only when username is set; never logged
  client_id: cams-record       # must differ from cams-detect's client id
  discovery_prefix: homeassistant
  base_topic: cams-record      # never cams-detect
  keepalive: 60
  refresh_s: 30
  switch_on: 1h                # what the HA switch's ON pauses for: 1h, 3h, morning or seconds
```

Environment overrides: `MQTT_ENABLED`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`,
`MQTT_PASSWORD_FILE`, `MQTT_CLIENT_ID`, `MQTT_DISCOVERY_PREFIX`, `MQTT_BASE_TOPIC` (the same names
cams-detect uses).

**Secret.** Same convention as cams-detect: a Docker secret file `secrets/mqtt_password` mounted at
`/run/secrets/mqtt_password` (`docker-compose.yml` has the lines commented out; uncomment them only
once the file exists, since compose refuses to start with a missing secret file). Either give
cams-record its own broker user (recommended, so it can be limited to `cams-record/#` and
`homeassistant/#`) or reuse cams-detect's credentials.

**Dependency.** `paho-mqtt>=2.0,<3` (the same range as cams-detect) is in `requirements.txt`; the
image must be rebuilt to include it.

## Deployment checklist (not performed by this change)

1. Rebuild the image (new dependency): `DOCKER_BUILDKIT=0 docker compose build`.
2. Migration v8 runs by itself on start (`mariadb: applied schema migration v8`). Nothing to run by hand.
3. Optional, for Home Assistant: create the broker user, write `secrets/mqtt_password`, uncomment the
   `mqtt_password` secret in `docker-compose.yml`, set `mqtt.enabled: true` (+ `username`, `host`) in
   `config/config.yaml` or `MQTT_*` env.
4. Restart cams-record (`docker compose up -d`). Recording restarts with the container, as on any update.
5. Deploy the App Hub package (Cameras UI + Alerts menu item) and restart App Hub.
6. Check: `GET /api/push/pauses` lists cameras; pause a camera, trigger a detection, see
   `"paused": true` from the webhook (or no push from the poller) while the Detections list and
   cams-detect MQTT still update; resume and confirm the next detection notifies.

Rollback: the previous image ignores `push_pauses` (an unknown table is harmless). Pauses stop
applying at once; no data migration is needed. Drop the table by hand only if wanted.
