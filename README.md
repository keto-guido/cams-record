# cams-record

The self-hosted NVR on the Synology DVA3221 ("Jefferson"): a **headless continuous recorder** that
writes 60 s MP4 segments from go2rtc, indexes them in the NAS's **MariaDB** and enforces
**retention**, timeline playback and range exports.

- One supervised `ffmpeg` per (camera, quality), reading go2rtc RTSP
  (`rtsp://127.0.0.1:8554/<stream>`, host network). It **stream-copies only** and never
  transcodes. Audio is copied only when the source track is AAC, using `-copyinkf:a`
  to preserve go2rtc RTSP AAC packets without keyframe flags; video still starts at a keyframe.
- 60 s segments cut on the first keyframe after each clock minute (`-segment_atclocktime`).
- A segment is indexed only after ffmpeg closes it (CSV `-segment_list`). The indexer then runs
  `ffprobe` (nice 10, one at a time) and inserts a row: duration, codec, size, resolution, fps.
- Restart policy: exponential backoff (1 s up to 60 s), plus a stall watchdog that restarts
  ffmpeg when no segment closes within 3 × segment_time. Every restart is logged and counted
  (`restarts_24h` in `recorder_status` and `/api/status`, `cams_record_restarts_total` in
  `/api/metrics`).
- Disk guard: main stops below `recording.min_free_gb` and sub below half of it, until free space
  is back above `recording.resume_free_gb` (see [Disk guard](#disk-guard)).
- Retention: a low-priority janitor deletes segments by age and by a total size budget. It starts
  in dry-run mode and deletes nothing until you turn that off (see [Retention](#retention)).
- HEVC sources get `-tag:v hvc1`, so Safari can play the MP4s.
- Startup reconcile: files on disk without a row are inserted, and rows whose file is gone get
  `missing=1`.
- HTTP on `127.0.0.1:8097` only: `GET /healthz` (no auth) and `GET /api/status` (App Hub JWT
  in `x-apphub-token`, admin role required).

## Contents

- [Layout and timestamps](#layout-and-timestamps)
- [Configuration](#configuration)
- [Disk guard](#disk-guard)
- [Retention](#retention)
- [Database](#database)
- [Deploy (Jefferson)](#deploy-jefferson)
- [Rollout and acceptance](#rollout-and-acceptance)
- [Gap check SQL](#gap-check-sql)
- [Rollback](#rollback)
- [HTTP API](#http-api)
- [Web Push detection alerts](#web-push-detection-alerts)
- [Development](#development)

## Layout and timestamps

```
/volume1/nvr/<camera>/<quality>/YYYY/MM/DD/HH/YYYYMMDDTHHMMSS.mp4      quality = main | sub
```

- **File names, directories and DB times are all UTC.** ffmpeg runs with `TZ=UTC`, and the
  DB session uses `time_zone='+00:00'`. The container's `TZ=America/Chicago` only affects log
  lines. A later UI converts to America/Chicago for display.
- `segments.path` is relative to the recording root, e.g.
  `front-door/main/2026/11/04/21/20261104T213200.mp4`.
- The name holds the wall-clock second at which ffmpeg opened the segment. `start_ts` and
  `end_ts` (ms precision) come from ffmpeg's segment list (stream time) mapped onto the wall
  clock. Within one ffmpeg run, each segment's `end_ts` therefore equals the next segment's
  `start_ts` exactly, and the gap check only sees real outages.
- ffmpeg's segment muxer does not create directories, so the recorder creates the current and
  the next hour directory ahead of time.
- If ffmpeg is killed while a segment is open, that file has no `moov` atom. The supervisor
  still probes it. When it cannot be read it is renamed `*.mp4.bad`, so it is never silently
  left without a row. Retention never touches these files, because they have no row.

## Configuration

`config/cameras.yaml` (required; copy `config/cameras.example.yaml`):

```yaml
cameras:
  front-door:                 # same id as cams_detect.cameras.camera_id
    main_stream: front-door-main   # go2rtc stream names: <id>-main and <id>
    sub_stream: front-door
    enabled: true
    record_main: true
    record_sub: true
```

`config/config.yaml` is optional; every key has a default (see `config/config.example.yaml`):

| key | default | |
|---|---|---|
| `recording.root` | `/volume1/nvr` | env `RECORD_ROOT` |
| `recording.segment_time` | `60` | seconds |
| `recording.stall_factor` | `3` | watchdog limit = factor × segment_time |
| `recording.backoff_initial` / `backoff_max` | `1` / `60` | seconds |
| `recording.audio` | `auto` | `auto` = copy AAC audio, drop any other codec; `off` |
| `recording.min_free_gb` | `200` | decimal GB; see [Disk guard](#disk-guard); `0` disables the guard |
| `recording.resume_free_gb` | `250` | must be >= `min_free_gb` |
| `retention.dry_run` | `true` | log and report only; see [Retention](#retention) |
| `retention.main_days` / `sub_days` | `3` / `14` | > 0, fractions allowed |
| `retention.cameras.<id>.main_days` / `sub_days` | | optional per-camera overrides |
| `retention.max_total_gb` | `1500` | decimal GB of indexed segments |
| `retention.interval_s` | `300` | seconds between passes |
| `retention.batch_size` | `500` | most segments handled per pass |
| `retention.nice` | `19` | 0 to 19, janitor thread only |
| `go2rtc.rtsp_url` | `rtsp://127.0.0.1:8554/{stream}` | |
| `db.host` / `port` / `database` / `user` | `127.0.0.1` / `3306` / `cams_record` / `cams_record` | env `DB_HOST`, `DB_PORT`, … |
| `db.password_file` | `/run/secrets/db_password` | |
| `auth.secret_file` | `/run/secrets/apphub_jwt` | the App Hub's shared HS256 secret |
| `push.*` | see [Web Push](#web-push-detection-alerts) | VAPID + subscription storage; private key from secrets |
| `http.host` / `port` | `127.0.0.1` / `8097` | |
| `base_path` | `/cams-record` | requests with or without the prefix both work |

The config is read once at startup. After editing it, run `docker compose restart cams-record`.

## Disk guard

The free space on `recording.root` is checked every 30 s, and again before each ffmpeg start.

| free space | main | sub |
|---|---|---|
| below `min_free_gb` (200) | stopped (`disk_full`) | keeps recording |
| below half of `min_free_gb` (100) | stopped | stopped (`disk_full`) |
| between the thresholds on the way back up | stays as it was | stays as it was |
| above `resume_free_gb` (250) | resumes | resumes |

- A stopped recorder finalizes its open segment first and logs at ERROR. It shows
  `state: disk_full` in `/api/status`, with `disk.guard` set to `ok`, `main_stopped` or
  `all_stopped`. `/api/metrics` has `cams_record_disk_full` per stream and
  `cams_record_disk_guard_level` (0, 1 or 2). Disk stops do not count as restarts.
- **No recorder starts below the minimum.** If the service starts below `min_free_gb`, main and
  sub both stay in `disk_full` until free space is above `resume_free_gb`. A sub that is already
  recording can still restart its ffmpeg (after a go2rtc hiccup, say) until the 100 GB limit.
- When the guard trips, the [retention janitor](#retention) runs a pass at once. That pass follows
  the normal rules, so it frees nothing in dry-run mode or when no segment is past its limits.
  Recording then only resumes after you free space yourself.
- If the free space cannot be read, the guard logs a warning and keeps its current state.

## Retention

**A new install deletes nothing.** `retention.dry_run` defaults to `true`, so the janitor only logs
and reports what it would delete. To go live, watch a few passes, set `dry_run: false` in
`config/config.yaml` and restart.

```
retention dry-run: would delete 500 segments (21.34 GB) [age main=500 sub=0, budget main=0 sub=0]
```

Every `interval_s`, the janitor selects up to `batch_size` segments, oldest first:

1. **Age rule:** segments that started more than `main_days` / `sub_days` ago. Per-camera overrides
   apply, and cameras no longer in `cameras.yaml` use the global values.
2. **Budget rule:** while the indexed total is above `max_total_gb`, the oldest **main** segments,
   and then the oldest **sub** segments once no main segment can go.

A backlog larger than `batch_size` drains over several passes. A dry run reports one batch per pass
too; `batch_full` in its result means there is more.

Footage that is never deleted:

- **Protected segments**, under any rule. Protect footage with SQL for now:

  ```sql
  UPDATE segments SET protected = 1
  WHERE camera_id = 'front-door' AND start_ts BETWEEN '2026-11-04 21:00:00' AND '2026-11-04 21:10:00';
  ```

  Protected bytes still count towards `max_total_gb`. If they alone exceed it, the janitor deletes
  all it may and logs a warning on each pass.
- **Recent segments:** anything that ended within the last 2 × `segment_time`.
- **Anything without a row.** The janitor never walks the disk, so `*.mp4.bad` and stray files stay.
- **Rows with an unsafe path.** The file must be strictly inside the real recording root, reached
  through no symlink at all, a regular file, and end in `.mp4`. Any other row is logged, counted in
  `skipped_total` and retried an hour later.

Deleting a segment unlinks the file, then sets `deleted_at` on its row (50 rows per transaction).
Rows are kept, hidden from every query and total. If the file is already gone, the row is still
marked. Rows with `missing=1` do not count towards the budget because they use no disk, but the age
rule retires them. Date directories left empty are removed bottom-up. `<camera>/<quality>` and the
hours around the current one stay, because recorders are using them.

When the [disk guard](#disk-guard) trips, a pass runs at once. While the guard stays tripped and
passes keep filling their batch, one more pass runs every 30 s. These urgent passes apply exactly
the same rules, including `dry_run`: they run sooner, they do not delete more.

`/api/status` has a `retention` object, and `/api/metrics` has `cams_record_retention_*`. See
[HTTP API](#http-api). Byte totals come from the last pass, so they are up to `interval_s` old.

## Database

The service connects to database `cams_record` on the NAS's MariaDB 10 (port 3306) and creates
its tables itself. Migrations are tracked in `schema_version`, the same way as in cams-detect.

| table | purpose |
|---|---|
| `recorder_cameras` | camera config mirror: `id` (= `cams_detect.cameras.camera_id`), streams, flags |
| `segments` | one row per file: `camera_id, quality, start_ts, end_ts` (DATETIME(3) UTC), `duration_ms, path, size_bytes, codec, width, height, fps, has_motion, keep_until, missing, protected, protected_until, deleted_at` |
| `recorder_status` | per (camera, quality): `state, last_segment_ts, restarts_24h, last_error` |
| `push_subscriptions` | Web Push endpoints bound to App Hub JWT `sub` (schema v5) |
| `push_sent_events` | detection `event_id` claim table to avoid duplicate alerts (schema v5; `event_id` widened to `VARCHAR(64)` in v6 for string cams_detect ids) |

`segments` has `UNIQUE(camera_id, quality, start_ts)`, `INDEX(camera_id, quality, start_ts,
end_ts)`, `INDEX(end_ts)`, `INDEX(path)` and `INDEX(deleted_at, quality, start_ts)`. The path index
makes reconcile lookups cheap, and the last one serves the janitor. Queries of your own should add
`deleted_at IS NULL`.

## Deploy (Jefferson)

1. **Create the database and user.** Run this as a MariaDB admin, using phpMyAdmin or
   `mysql -u root -p -h 127.0.0.1 -P 3306`:

   ```sql
   CREATE DATABASE IF NOT EXISTS cams_record CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   CREATE USER IF NOT EXISTS 'cams_record'@'%' IDENTIFIED BY 'CHANGE-ME-long-random-password';
   GRANT ALL PRIVILEGES ON cams_record.* TO 'cams_record'@'%';
   -- optional read-only user for App Hub / Grafana / a later UI
   CREATE USER IF NOT EXISTS 'cams_reader'@'%' IDENTIFIED BY 'ANOTHER-long-random-password';
   GRANT SELECT ON cams_record.* TO 'cams_reader'@'%';
   FLUSH PRIVILEGES;
   ```

   (`'%'` can be narrowed to `'127.0.0.1'`, since the container uses host networking.)

2. **Get the code and the secrets** in a project folder, e.g. `/volume1/docker/cams-record`:

   ```bash
   git clone https://github.com/keto-guido/cams-record.git /volume1/docker/cams-record
   ```

   ```bash
   cd /volume1/docker/cams-record && mkdir -p secrets config
   ```

   ```bash
   printf '%s' 'CHANGE-ME-long-random-password' > secrets/db_password
   ```

   ```bash
   cp ../cams-detect/secrets/apphub_jwt secrets/apphub_jwt
   ```

   Generate Web Push VAPID material and a webhook secret on the NAS only (never commit):

   ```bash
   # See docs/web-push.md for the full generator snippet (py_vapid).
   # Writes secrets/vapid_private_key (PEM) and prints the public applicationServerKey.
   printf '%s' "$(openssl rand -hex 32)" > secrets/push_webhook_secret
   ```

   ```bash
   chmod 600 secrets/*
   ```

   Compose expects `secrets/vapid_private_key` and `secrets/push_webhook_secret` to exist
   before `docker compose up`. Until the VAPID private key is present and readable, push
   APIs return 503 and the poller stays idle.

3. **Create the recording share/folder** `/volume1/nvr`. It is bind-mounted into the
   container at the same path:

   ```bash
   sudo mkdir -p /volume1/nvr
   ```

4. **Configure cameras.** Start with front-door only (see the rollout section below):

   ```bash
   cp config/cameras.example.yaml config/cameras.yaml
   ```

   Check that the stream names exist in go2rtc (`http://<nas>:1984/`) and that
   `rtsp://127.0.0.1:8554/front-door-main` plays. Streams are named `<id>-main` (main) and
   `<id>` (sub).

5. **Build and start** (Docker 24 / compose 2.20 on DSM, legacy builder):

   ```bash
   sudo DOCKER_BUILDKIT=0 docker compose build
   ```

   ```bash
   sudo docker compose up -d
   ```

   ```bash
   sudo docker compose logs -f cams-record
   ```

   You should see `mariadb: applied schema migration v1` (and `v2`, `v3`),
   `front-door/main: recording rtsp://127.0.0.1:8554/front-door-main …` and, after about a minute,
   rows in `segments`.

6. **App Hub** (optional in Phase 1): proxy `/cams-record` to `http://127.0.0.1:8097`, with the
   App Hub injecting its JWT in `x-apphub-token`. Then `GET /cams-record/api/status` works for
   admins.

Compose limits: `cpu_shares: 256` (DSM has no CFS quota, so `cpus:` is not used),
`mem_limit: 512m`, `restart: unless-stopped`, and `stop_grace_period: 30s` so that every
ffmpeg can finalize its open segment on `docker compose down`.

### Update

```bash
cd /volume1/docker/cams-record && git pull && sudo DOCKER_BUILDKIT=0 docker compose build && sudo docker compose up -d
```

## Rollout and acceptance

1. **72 h on one camera:** front-door with `record_main` and `record_sub` set, and no other
   camera in `cameras.yaml`.
2. Check the following, then add all the cameras and restart:
   - **CPU** < 5 % of one core per stream: `sudo docker stats cams-record` (the CPU % is the
     sum over all ffmpeg processes). Per process, use `top -H -p $(pgrep -d, ffmpeg)` on the
     host.
   - **RSS** < 150 MB total: the `MEM USAGE` column of `docker stats`.
   - **Gaps** < 0.1 % missing time per camera per day: run the [gap check](#gap-check-sql).
   - **Every file on disk has a row.** Restart the container
     (`sudo docker compose restart cams-record`) and check that the reconcile log line reports
     `0 files to index, … 0 marked missing`. You can also compare counts directly:

     ```bash
     find /volume1/nvr/front-door -name '*.mp4' | wc -l
     ```

     ```sql
     SELECT COUNT(*) FROM segments WHERE camera_id='front-door' AND missing=0 AND deleted_at IS NULL;
     ```

   - Restarts: `SELECT * FROM recorder_status;` (`restarts_24h` should be about 0 on a healthy
     camera).

## Gap check SQL

This query is also saved as [`docs/gap-check.sql`](docs/gap-check.sql). It shows the missing
recording time per camera, quality and UTC day for the last 7 days. A gap is any break longer
than 1 s between one segment's `end_ts` and the next segment's `start_ts`.

```sql
WITH s AS (
  SELECT camera_id, quality, start_ts, end_ts,
         LAG(end_ts) OVER (PARTITION BY camera_id, quality ORDER BY start_ts) AS prev_end
  FROM segments
  WHERE missing = 0 AND deleted_at IS NULL
    AND start_ts >= UTC_DATE() - INTERVAL 7 DAY
)
SELECT camera_id,
       quality,
       DATE(start_ts)                                                              AS day_utc,
       COUNT(*)                                                                    AS segments,
       ROUND(SUM(TIMESTAMPDIFF(MICROSECOND, start_ts, end_ts)) / 1e6 / 3600, 2)   AS recorded_h,
       SUM(TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts) > 1000000)               AS gaps,
       ROUND(SUM(IF(TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts) > 1000000,
                    TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts), 0)) / 1e6, 1)  AS gap_s,
       ROUND(100 * SUM(IF(TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts) > 1000000,
                          TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts), 0)) / 1e6 / 86400, 3) AS missing_pct
FROM s
GROUP BY camera_id, quality, DATE(start_ts)
ORDER BY camera_id, quality, day_utc;
```

Acceptance is `missing_pct < 0.1` (86.4 s per day) on every complete day. The first and last
day of a run are partial, so judge those by `gap_s`. `docs/gap-check.sql` also has a query
that lists the individual gaps, which you can match against restart lines in the logs.

## Rollback

Recording is independent of Surveillance Station and cams-detect, so a rollback removes only
this service:

```bash
cd /volume1/docker/cams-record && sudo docker compose down
```

```bash
sudo rm -rf /volume1/nvr
```

To drop the index too, run this as a MariaDB admin:

```sql
DROP DATABASE cams_record;
```

Optionally remove the users: `DROP USER 'cams_record'@'%'; DROP USER 'cams_reader'@'%';`.

## HTTP API

These routes are served on `127.0.0.1:8097`, under `/cams-record` (the prefix is optional).

| route | auth | |
|---|---|---|
| `GET /healthz` | none | `{"ok": true}`; used by the Docker healthcheck |
| `GET /api/status` | admin JWT | per camera/quality: `state` (`starting`/`recording`/`backoff`/`disk_full`/`stopped`), `video_codec`, `last_segment_ts`, `last_segment_age_s`, `mb_per_hour`, `restarts_24h`, `last_error`; also disk free on the root and the disk guard state, indexer queue, DB state, last reconcile, `retention` (below) |
| `GET /api/metrics` | admin JWT | Prometheus text: restarts, segments, last segment age, disk free, disk guard, index backlog, `cams_record_retention_*` |
| `GET/POST/DELETE /api/push/*` | cams or admin JWT | Web Push subscribe/status/test/event-context; see [Web Push](#web-push-detection-alerts) |
| `POST /api/internal/detection-event` | `x-cams-push-secret` | cams-detect webhook only; not a public send API |

`retention` in `/api/status`:

| key | |
|---|---|
| `settings` | `dry_run`, `main_days`, `sub_days`, `max_total_gb`, `interval_s`, `batch_size`, `cameras` |
| `total_bytes`, `totals_ts` | indexed bytes on disk, and when that was last computed |
| `streams[]` | per camera and quality: `bytes`, `protected_bytes`, `segments`, `oldest_start_ts` |
| `last_run` | `ts`, `duration_s`, `result` (`ok`, `dry_run`, `error`), `urgent`, per-rule counts `age_main`, `age_sub`, `budget_main`, `budget_sub`, and `error` on failure |
| `deleted` | `total_segments`, `total_bytes` since start, `last_segments`, `last_bytes` for the last pass |
| `would_delete` | the last dry run: `segments`, `bytes`, per-rule counts, `batch_full`, `ts` |
| `skipped_total`, `errors_total` | rows refused by the path checks, failed passes |

Auth trusts only the `x-apphub-token` header. The value must be an HS256 JWT signed with the
App Hub secret (`/run/secrets/apphub_jwt`), with `exp` set, `aud: cams-record`, and a `roles` claim containing
`admin` (clip endpoints also accept `cams`).

```bash
curl -s -H "x-apphub-token: $TOKEN" http://127.0.0.1:8097/cams-record/api/status | jq
```

## Web Push detection alerts

The Cameras PWA can subscribe to **detection alerts** via Web Push (VAPID). Full setup,
iOS Home Screen requirements, schema v5, and the cams-detect webhook contract are in
[`docs/web-push.md`](docs/web-push.md).

Summary:

- **Frontend** (`apphub/`): service worker at `/cams/sw.js`, manifest, notification control
  on the Live view (subscribe / unsubscribe / test), deep link `#event=<id>`.
- **Backend** (`cams-record`): stores subscriptions per JWT `sub`, sends on new
  `cams_detect.events` (poller and/or internal webhook), prunes 404/410 endpoints,
  dedupes by `event_id`. Private VAPID key stays in `secrets/vapid_private_key`.
- **iPhone**: alerts only work from the **installed** Home Screen PWA, not Safari tabs.
- **Companion work**: deploy updated `apphub/` assets to App Hub; optionally teach
  cams-detect to POST `/api/internal/detection-event` for lower latency (poller works alone).

## Development

```bash
pip install -r requirements-dev.txt
```

```bash
ruff check .
```

```bash
python -m pytest -q -m "not integration"
```

```bash
python -m pytest -q -m integration
```

- The MariaDB tests use `CAMS_TEST_DB_HOST`, `_PORT`, `_USER`, `_PASSWORD` and `_NAME` (default
  `cams_record` / `cams_record` @ `127.0.0.1:3306/cams_record_test`). They are skipped when
  the server is unreachable. CI runs a `mariadb:10.11` service and fails if anything is
  skipped.
- The integration test loops a testsrc H.264+AAC file through the real ffmpeg segmenter for
  3 × 60 s. It checks that at least 3 full segments are indexed for main and sub, with start/end
  matching the file names, no gaps > 1 s, and a row for every file. Set
  `CAMS_TEST_SEGMENT_TIME=6` for a 25 s local run.
- CI (GitHub Actions) has three jobs: ruff, tests (unit and integration against MariaDB), and a
  `DOCKER_BUILDKIT=0` image build with a smoke test.

## Detection clips and footage protection

`GET /cams-record/api/clip?camera=front-door&start=2026-11-04T21:32:00Z&end=2026-11-04T21:33:00Z&quality=sub&download=0`
returns an inline MP4; `download=1` sends an attachment. Timestamps accept epoch seconds or
ISO8601 with a timezone (encode a literal `+` as `%2B`). Windows must be positive and at most
five minutes. HTTP Range requests return 206. Download names use America/Chicago by default.
`GET /api/clip/info` accepts the same query and returns, for example,
`{"sub":"available","main":"gone"}`. Both prefixed and proxy-stripped paths work.

These two endpoints accept `admin` **or** `cams`; status and metrics remain admin-only.
JWTs must have `exp` and `aud: cams-record` (override with `auth.app_id` / `AUTH_APP_ID`).
Only the App Hub injected `x-apphub-token` header is trusted.
A 425 response includes `detail`, `retry_after` (seconds), and `Retry-After`: retry once the
recorder closes and indexes the live segment. A 404 means a gap or deleted/missing footage,
with a quality-specific message such as `main quality footage no longer available`.
Stream copy preserves the source codecs, tags HEVC as `hvc1`, and retries without audio if
copying audio fails. Keyframe boundaries can affect exact clip start time.

Cache files are atomically published under `recording.state_dir/clips` by default
(`/tmp/cams-record/clips`), never under the recordings root. Requests prune old files and
evict least recently used files over the size budget. In-flight responses hold a temporary
hard link until streaming finishes. Two ffmpeg builds can run at once, with a timeout per
attempt; `nice -n 10` and `ionice -c3` are used when installed.

| Config key | Default | Environment override |
| --- | --- | --- |
| `clips.cache_dir` | empty = state_dir/clips | `CLIP_CACHE_DIR` |
| `clips.max_cache_gb` | 2 (decimal GB) | `CLIP_CACHE_GB` |
| `clips.max_age_s` | 86400 | YAML only |
| `clips.timeout_s` | 120 per attempt | `CLIP_TIMEOUT_S` |
| `clips.timezone` | America/Chicago | `CLIP_TIMEZONE` |
| `detection_protection.enabled` | true | `DETECTION_PROTECTION_ENABLED` |
| `detection_protection.interval_s` | 60 | `DETECTION_PROTECTION_INTERVAL_S` |
| `detection_protection.days` | 14 | `DETECTION_PROTECTION_DAYS` |
| `detection_protection.lookback_s` | 600 | YAML only |
| `detection_protection.pre_s`, `post_s` | 5 each | YAML only |
| `detection_protection.fallback_s` | 10 | YAML only |
| `detection_protection.max_window_s` | 120 | YAML only |

Automatic migration **v3** adds nullable `segments.protected_until DATETIME` (UTC).
Retention skips permanent manual `protected=1` rows and rows with an unexpired
`protected_until`; expired temporary protection becomes deletable again. Protected-byte
metrics include both kinds. The poller reads `cams_detect.events` using its exact MariaDB
columns `camera_id`, `start_ts`, `end_ts`, `updated_at`. A successful-poll watermark plus a
10-minute overlap catches updated end times and newly indexed segments. On restart it
processes recent events, rather than scanning the full history. Poll failures log a warning
and do not interrupt recording or retention.

`cams_record.protection.event_window` computes start minus 5 seconds through event end
plus 5 seconds; an open event uses start plus 10 seconds as its provisional end. The
result is capped at 120 seconds. Both qualities overlapping that window receive
`protected_until = max(existing, poll_time + 14 days)`.

The compose file uses host networking, DB host `127.0.0.1`, and user `cams_record`.
Run this as a MariaDB administrator (adjust user/host if your installation differs):

```sql
GRANT SELECT ON cams_detect.events TO 'cams_record'@'127.0.0.1';
```

No write grant on cams_detect is required. Check `SELECT CURRENT_USER()` when connecting
as cams_record if MariaDB resolves the account to a different host (such as localhost).


Segment file identity and upgrade (schema v4): segments are indexed by their relative
`path` within the single indexer writer. Repeated CSV/reconcile/shutdown-sweep jobs
refresh the earliest row for that path and preserve its original timestamps and
protection. The shutdown sweep remembers only the last 16 listed paths; after a
long run it can submit older files again with whole-second filename timestamps,
where the live CSV used subsecond clock mapping. The former timestamp unique key
allowed these to become duplicate rows.

Migration v4 runs automatically on connect and only adds the non-unique
`idx_segments_path_id (path, id)` lookup index using `IF NOT EXISTS`. It does not
change or delete any rows or files, and does not require cleaning up existing
duplicates. Index creation can briefly wait for metadata locks on a busy live DB.
Existing duplicates remain safe: protection expands by path, retention rejects a
path if any row (including a tombstone) protects it, deletion marks every row of
the file, retention budgets count files once, and clip selection uses each path
once. Run only one recorder/indexer writer per recording root/database; path
deduplication is serialized in that writer, rather than enforced by a unique key.

## Per-camera access

Every camera route checks App Hub's `cams.json` as well as the JWT role: a camera the user may
not see answers exactly like an unknown one, with a 404. `docker-compose.yml` needs
`APPHUB_CAMERA_REGISTRY_DIR` set to a dedicated directory containing only `cams.json`. Rules, configuration and rollout
order are in [docs/camera-access.md](docs/camera-access.md).

## Camera objects (cams_config)

Cameras are moving to one camera object per camera, stored in the MariaDB database `cams_config`:
streams, which stream does which job (live, detect, verify, snapshot, record), access, detection,
recording and notification settings. cams-record owns the schema (`cams_record/camconfig/`).
`python -m cams_record.camconfig import` loads today's files into it after proving nothing would
change. `camera_source` (`CAMERA_SOURCE`) picks where cameras come from: `file` (default), `shadow`
(files, while reporting every difference cams_config would make) or `db`; with `db`,
`access.publish_registry` also writes App Hub's `cams.json` from it. Design, setup SQL, import,
shadow, switching and rollback: [docs/camera-model.md](docs/camera-model.md).

## Timeline, playback and range exports (Phase 3)

All routes below accept the same App Hub `admin` or `cams` JWT as `/api/clip`.
Browser requests use `/cams-record/api/…`; the proxy keeps the prefix.
Times accept epoch seconds or ISO8601 with an explicit timezone.

| GET route | Purpose |
|---|---|
| `/api/timeline?cam=front-door&start=…&end=…` | Main/sub recorded spans, gap tolerance and latest indexed ends; `camera` aliases `cam`; maximum 8 days |
| `/api/playback?cam=front-door&start=…&end=…` | Sub-quality media offsets and relative HLS playlist URL; maximum 26 hours |
| `/api/hls/{cam}/index.m3u8?start=…&end=…` | VOD playlist with UTC program times on one continuous media timeline; discontinuities only at codec changes |
| `/api/hls/{cam}/seg/{id}.ts?o=…` | Sub segment remuxed to MPEG-TS, with copied video and audio; private cache. `o` (required, 0–30 h in milliseconds) is the entry's media offset, and timestamps start at `o` + 10 s |
| `/api/export/info?cam=front-door&start=…&end=…` | Availability, estimated bytes, recommended quality and export busy state |
| `/api/export?cam=front-door&start=…&end=…&quality=main&fallback=1` | Attachment download; falls back to sub only when main is gone; `X-Export-Quality` identifies the result |

Timeline and playback default to the current local day in `clips.timezone` when both times
are omitted. Playback uses whole indexed files, dropping restart overlaps, so its first/last
entry can extend beyond the requested window. Gaps consume no media time.
The App Hub camera page provides day navigation, zoom/pan, detections, playback and range handles.

Exports allow `clips.export_max_s` seconds (default 1800, environment `CLIP_EXPORT_MAX_S`);
existing clips remain limited to 300 seconds. Only one export runs at once, including its
response download; busy requests return 429 with `Retry-After: 15`. Unindexed footage returns
425. Exports require at least 2 GB free after the estimated output, live in
`recording.state_dir/exports`, and are deleted after the response. All video/audio assembly
uses stream copy, with an audio-free retry if copying audio fails; no video transcoding occurs.
HLS remuxes run at most three at once and use a separate 512 MiB / 6 hour LRU cache.
