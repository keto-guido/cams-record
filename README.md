# cams-record

Phase 1 of the self-hosted NVR on the Synology DVA3221 ("Jefferson"): a **headless continuous
recorder** that writes 60 s MP4 segments from go2rtc and indexes them in the NAS's **MariaDB**.
There is no UI, retention or playback yet.

- One supervised `ffmpeg` per (camera, quality), reading go2rtc RTSP
  (`rtsp://127.0.0.1:8554/<stream>`, host network). It **stream-copies only** and never
  transcodes. Audio is copied only when the source track is AAC.
- 60 s segments cut on the first keyframe after each clock minute (`-segment_atclocktime`).
- A segment is indexed only after ffmpeg closes it (CSV `-segment_list`). The indexer then runs
  `ffprobe` (nice 10, one at a time) and inserts a row: duration, codec, size, resolution, fps.
- Restart policy: exponential backoff (1 s up to 60 s), plus a stall watchdog that restarts
  ffmpeg when no segment closes within 3 × segment_time. Every restart is logged and counted
  (`restarts_24h` in `recorder_status` and `/api/status`, `cams_record_restarts_total` in
  `/api/metrics`).
- Startup reconcile: files on disk without a row are inserted, and rows whose file is gone get
  `missing=1`.
- HTTP on `127.0.0.1:8099` only: `GET /healthz` (no auth) and `GET /api/status` (App Hub JWT
  in `x-apphub-token`, admin role required).

## Contents

- [Layout and timestamps](#layout-and-timestamps)
- [Configuration](#configuration)
- [Database](#database)
- [Deploy (Jefferson)](#deploy-jefferson)
- [Rollout and acceptance](#rollout-and-acceptance)
- [Gap check SQL](#gap-check-sql)
- [Rollback](#rollback)
- [HTTP API](#http-api)
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
  left without a row. Nothing is ever deleted.

## Configuration

`config/cameras.yaml` (required; copy `config/cameras.example.yaml`):

```yaml
cameras:
  front-door:                 # same id as cams_detect.cameras.camera_id
    main_stream: front-door   # go2rtc stream names
    sub_stream: front-door-sub
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
| `go2rtc.rtsp_url` | `rtsp://127.0.0.1:8554/{stream}` | |
| `db.host` / `port` / `database` / `user` | `127.0.0.1` / `3306` / `cams_record` / `cams_record` | env `DB_HOST`, `DB_PORT`, … |
| `db.password_file` | `/run/secrets/db_password` | |
| `auth.secret_file` | `/run/secrets/apphub_jwt` | the App Hub's shared HS256 secret |
| `http.host` / `port` | `127.0.0.1` / `8099` | |
| `base_path` | `/cams-record` | requests with or without the prefix both work |

The config is read once at startup. After editing it, run `docker compose restart cams-record`.

## Database

The service connects to database `cams_record` on the NAS's MariaDB 10 (port 3306) and creates
its tables itself. Migrations are tracked in `schema_version`, the same way as in cams-detect.

| table | purpose |
|---|---|
| `recorder_cameras` | camera config mirror: `id` (= `cams_detect.cameras.camera_id`), streams, flags |
| `segments` | one row per file: `camera_id, quality, start_ts, end_ts` (DATETIME(3) UTC), `duration_ms, path, size_bytes, codec, width, height, fps, has_motion, keep_until, missing` |
| `recorder_status` | per (camera, quality): `state, last_segment_ts, restarts_24h, last_error` |

`segments` has `UNIQUE(camera_id, quality, start_ts)`, `INDEX(camera_id, quality, start_ts,
end_ts)`, `INDEX(end_ts)` and `INDEX(path)`. The path index makes reconcile lookups cheap.

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

   ```bash
   chmod 600 secrets/*
   ```

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
   `rtsp://127.0.0.1:8554/front-door` plays.

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

   You should see `mariadb: applied schema migration v1`,
   `front-door/main: recording rtsp://127.0.0.1:8554/front-door …` and, after about a minute,
   rows in `segments`.

6. **App Hub** (optional in Phase 1): proxy `/cams-record` to `http://127.0.0.1:8099`, with the
   App Hub injecting its JWT in `x-apphub-token`. Then `GET /cams-record/api/status` works for
   admins.

Compose limits: `cpu_shares: 256` (DSM has no CFS quota, so `cpus:` is not used),
`mem_limit: 256m`, `restart: unless-stopped`, and `stop_grace_period: 30s` so that every
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
     SELECT COUNT(*) FROM segments WHERE camera_id='front-door' AND missing=0;
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
  WHERE missing = 0
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

These routes are served on `127.0.0.1:8099`, under `/cams-record` (the prefix is optional).

| route | auth | |
|---|---|---|
| `GET /healthz` | none | `{"ok": true}`; used by the Docker healthcheck |
| `GET /api/status` | admin JWT | per camera/quality: `state` (`starting`/`recording`/`backoff`/`stopped`), `last_segment_ts`, `last_segment_age_s`, `mb_per_hour`, `restarts_24h`, `last_error`; also disk free on the root, indexer queue, DB state, last reconcile |
| `GET /api/metrics` | admin JWT | Prometheus text: restarts, segments, last segment age, disk free, index backlog |

Auth trusts only the `x-apphub-token` header. The value must be an HS256 JWT signed with the
App Hub secret (`/run/secrets/apphub_jwt`), with `exp` set and a `roles` claim that contains
`admin`. The verification is the same as in cams-detect.

```bash
curl -s -H "x-apphub-token: $TOKEN" http://127.0.0.1:8099/cams-record/api/status | jq
```

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
