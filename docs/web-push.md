# Web Push detection alerts

Standards-based **Web Push** (VAPID + Push API) for the Cameras PWA (`/cams/`).
Subscriptions are stored in `cams_record`, bound to the App Hub JWT `sub`, and
detection fan-out is handled by cams-record (poll of `cams_detect.events` and/or an
authenticated internal webhook). **Private VAPID material never goes to the browser
and must never be committed.**

## What shipped in this repo

| Layer | Location | Role |
|---|---|---|
| PWA UI + SW | `apphub/` | Manifest, `/cams/sw.js`, subscribe/unsubscribe control, `#event=<id>` deep link |
| API + storage | `cams_record/` | VAPID, subscription CRUD, test send (own subs only), event context, webhook, poller |
| Schema | migrations **v5**, **v6**, **v7** | `push_subscriptions` (v7 adds `roles_json`, `roles_at`); `push_sent_events` (dedupe; `event_id` is `VARCHAR(64)` after v6) |

There is **no public send endpoint**. User routes require the App Hub
`x-apphub-token` with role `admin` or `cams`. The detection webhook requires
`x-cams-push-secret` matching `secrets/push_webhook_secret`.

## Deployment prerequisites (Jefferson)

1. **HTTPS** on the App Hub origin users open (required for Push / Notification APIs
   and for installed PWAs). Plain HTTP on LAN IPs will not work for production push.
2. **Deploy the App Hub camera app** from this repo’s `apphub/` tree (or merge the
   same files into `keto-guido/apphub`): `cams.js`, `static/cams.js`, `static/cams.css`,
   `static/sw.js`, `static/manifest.webmanifest`, and the icon PNGs.
3. **Generate VAPID keys on the NAS** (do not commit the private key):

   ```bash
   cd /volume1/docker/cams-record
   mkdir -p secrets
   python3 - <<'PY'
   from pathlib import Path
   from py_vapid import Vapid
   from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
   import base64
   v = Vapid()
   v.generate_keys()
   Path("secrets/vapid_private_key").write_bytes(v.private_pem())
   raw = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
   print("VAPID public key (optional config override):")
   print(base64.urlsafe_b64encode(raw).decode().rstrip("="))
   PY
   printf '%s' "$(openssl rand -hex 32)" > secrets/push_webhook_secret
   chmod 600 secrets/vapid_private_key secrets/push_webhook_secret
   ```

   Compose mounts them at `/run/secrets/vapid_private_key` and
   `/run/secrets/push_webhook_secret` (see `docker-compose.yml`).

   cams-record loads that PEM with `Vapid.from_pem` / a `Vapid` instance for
   `pywebpush`. Passing the PEM **text** into `webpush(vapid_private_key=...)` is
   wrong: pywebpush treats a non-path string as DER via `Vapid.from_string` and
   fails with `ValueError: Could not deserialize key data`.
4. **Set `push.vapid_subject`** in `config/config.yaml` to a real contact URI
   (`mailto:you@example.com` or `https://…`). Push services expect this.
5. **Rebuild/restart cams-record** (schema v5 applies on connect). Confirm logs
   include `web push: enabled` when the private key file is readable.
6. **iPhone / iPad**: users must **Add to Home Screen** the Cameras PWA, then open
   that Home Screen icon (standalone display). Safari in a normal browser tab
   **cannot** subscribe to Web Push on iOS/iPadOS. The Live view control explains
   this; it does not imply tab Safari supports iPhone push.

## HTTP API (cams-record)

All under `/cams-record` (prefix optional). Auth = App Hub JWT (`admin` or `cams`)
unless noted.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/push/vapid-public-key` | cams/admin | Application server key for `pushManager.subscribe` |
| GET | `/api/push/status` | cams/admin | Whether push is configured + this user’s subscription count |
| POST | `/api/push/subscribe` | cams/admin | Upsert this user’s subscription (`endpoint`, `keys`) |
| DELETE | `/api/push/subscribe` | cams/admin | Remove this user’s endpoint only |
| POST | `/api/push/test` | cams/admin | Test notification to **this user’s** subscriptions only |
| GET | `/api/push/event-context/{id}` | cams/admin | Metadata for deep link (no images) |
| POST | `/api/internal/detection-event` | `x-cams-push-secret` | cams-detect → record event (deduped) |

Push bodies include title/body/url/tag/camera/label/start_ts/`event_id` only —
**no snapshot bytes or image URLs.** Clicks open `/cams/#event=<id>`.

Dead push endpoints (HTTP 404/410 from the push service) are deleted automatically.

## Who receives an alert

A detection alert goes only to subscribers who may view that camera, decided by the same registry
rule as everything else (`docs/camera-access.md`): the camera id must be in `cams.json` and its
`users` / `roles` (or the `admin` role) must allow the subscriber.

- **Roles** come from the subscriber's last verified App Hub JWT. They are saved when the device
  subscribes and refreshed every time the app opens (`GET /api/push/status`), so granting or
  removing a role applies from that user's next app open.
- Rows created before migration v7, and roles older than `push.roles_max_age_days` (default 30),
  count as **no roles**: the user still gets alerts for open cameras and cameras that list them
  in `users`, but not role-only grants. Admins get alerts for restricted cameras again as soon as
  they open the app. This can only under-notify, never over-notify.
- A camera that is **not in the registry**, or a missing/unreadable registry, notifies **nobody**,
  admins included. The first such event per camera logs
  `push: camera <id> is not in the camera registry; nobody is notified for it`.
- `access.enforce: false` (development only) sends every alert to every subscriber, as before.
- `POST /api/push/test` is not filtered: it only reaches the caller's own devices.
- A camera whose alerts are **paused** (`docs/alert-pause.md`) notifies nobody until the pause ends;
  its events are still claimed, so no backlog is sent on resume. Test pushes ignore pauses.

## Detection integration

### Works without cams-detect code changes

With `push.poll_enabled: true` (default), cams-record polls `cams_detect.events`
every `push.interval_s` (default 15) and sends once per `event_id` via
`push_sent_events`. An event whose `snapshot_path` is still NULL is held until it
started more than `push.snapshot_wait_s` (default 5) seconds ago, so a notification
never arrives before its snapshot can be served; events that never get a snapshot
still notify after the wait (`0` disables the hold; keep it well below `lookback_s`).
Claims written before the `event_uid` switch are keyed by the numeric `events.id`;
the poller skips those events too, so a deploy does not re-push them. Requires the
existing grant:

```sql
GRANT SELECT ON cams_detect.events TO 'cams_record'@'127.0.0.1';
```

Production `cams_detect.events` columns used by the poller / event-context path:

| column | type (production) | notes |
|---|---|---|
| `id` | `BIGINT` auto-increment PK | internal only; **never** sent in new pushes. Legacy fallback only: `/api/push/event-context/<digits>` with no matching uid looks up this PK (old notifications linked to it) and answers with the uid |
| `event_uid` | `VARCHAR(64)` UNIQUE, e.g. `1793897909-7de0b7` | the MQTT / API / push event id; the poller emits it, `push_sent_events.event_id` (`VARCHAR(64)`, migration v6) stores it, and `/api/push/event-context/{event_id}` looks it up |
| `camera_id` | string | |
| `label` | string | |
| `max_score` | float | **not** `score` |
| `start_ts` / `end_ts` | datetime | |
| `snapshot_path` / `snapshot_hires` | string or NULL | event-context only reports `has_snapshot` / `has_snapshot_hires` booleans (no bytes, no image URLs) |

Migration **v6** is a forward `ALTER` for installs that already applied empty v5 tables
(`event_id BIGINT` → `VARCHAR(64)`). The webhook also accepts `max_score` (preferred) or
`score` as an alias.

### Recommended companion change (cams-detect)

For lower latency, after inserting a new detection row, POST:

```http
POST http://127.0.0.1:8097/cams-record/api/internal/detection-event
x-cams-push-secret: <contents of secrets/push_webhook_secret>
Content-Type: application/json

{
  "id": "1793897909-7de0b7",
  "camera_id": "front-door",
  "camera_name": "Front Door",
  "label": "person",
  "max_score": 0.91,
  "start_ts": 1730000000.0,
  "end_ts": null
}
```

Duplicate posts for the same `id` are no-ops (`deduped: true`). Keep the webhook
secret only on the NAS; do not expose this route through the public App Hub proxy
if you can avoid it (it already binds to `127.0.0.1`).

### App Hub companion

- Proxy `/cams-record` remains as today (JWT injection).
- Ship the updated `apphub/` camera assets so `/cams/sw.js`, the manifest, and the
  notification control are live.
- No new public App Hub route is required for sending pushes.

## Config reference

| key | default | notes |
|---|---|---|
| `push.enabled` | `true` | Still inactive until the VAPID private file is present |
| `push.vapid_private_file` | `/run/secrets/vapid_private_key` | PEM; secret |
| `push.vapid_public_key` | `""` | Optional; derived from private when empty |
| `push.vapid_subject` | `mailto:cams@localhost` | Change for production |
| `push.webhook_secret_file` | `/run/secrets/push_webhook_secret` | Secret |
| `push.poll_enabled` | `true` | Poll `cams_detect.events` |
| `push.interval_s` / `lookback_s` | `15` / `120` | Poller timing; `interval_s` dominates push latency |
| `push.snapshot_wait_s` | `5` | Hold events without `snapshot_path` until this old; `0` = no hold |
| `push.labels` | `[]` | Empty = all labels |
| `push.roles_max_age_days` | `30` | Saved subscriber roles older than this are ignored (see *Who receives an alert*) |

Environment overrides: `PUSH_ENABLED`, `PUSH_VAPID_PRIVATE_FILE`,
`PUSH_VAPID_PUBLIC_KEY`, `PUSH_VAPID_SUBJECT`, `PUSH_WEBHOOK_SECRET_FILE`,
`PUSH_POLL_ENABLED`, `PUSH_INTERVAL_S`, `PUSH_LOOKBACK_S`, `PUSH_SNAPSHOT_WAIT_S`.
