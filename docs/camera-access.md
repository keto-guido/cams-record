# Per-camera access contract

This is the contract between App Hub and cams-record. It has two implementations that must agree:
`cams_record/camera_access.py` and `apphub/camera_acl.js`. App Hub's own module, `camera-access.js`,
must follow it too. `apphub/camera_acl_vectors.json` is the shared test data, and any implementation
can be checked against it.

## Registry: `cams.json`

The file is a JSON array of `{id, name, snap?, users?: string[], roles?: string[]}`.

| Input | Result |
| --- | --- |
| Bad JSON, unreadable file, or not an array | No cameras (fail closed) |
| Entry is not an object | Dropped |
| `id` does not match `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$` | Dropped |
| `name` is not a non-empty string | Dropped |
| `snap` present but not 1–64 of `A-Za-z0-9._-`, or contains `..` | Dropped |
| Several entries share an `id` | All of them dropped |
| `users` or `roles` present but not an array of non-empty strings, including `null` | Admin-only, never open |
| Unknown keys | Ignored |

A dropped camera is unknown to everyone, including admin.

## Decision

Input is an authenticated user and their roles. Reaching this check already requires the Cameras app.
Matching is exact and case-sensitive.

1. Empty user: deny.
2. Role `admin`: allow any registered camera.
3. `users` and `roles` both empty or missing: allow.
4. User is in `users`, or any role is in `roles`: allow.
5. Otherwise deny.

**Streams.** `snap` is a separate go2rtc stream, and two cameras can share one. A stream name needs
access to every camera whose `id` or `snap` equals it. A name matching no camera is denied. Every
route that reaches go2rtc or recordings applies this rule, so a client-supplied name is never trusted.

**Configured stream sources (recorder, `CameraAccess.allowed` only).** Recorded footage is
authorized for the **logical** camera id (cameras.yaml key / registry id). Behavior:

1. Parent id must pass registry `can_stream` (exact id/snap).
2. Each configured `main_stream` / `sub_stream` keeps every `source_owners` grant via
   `can_stream(owner)`.
3. If a source is an **explicit** registered camera id or `snap`, also require `can_stream(src)`
   exactly — shared/restricted stream auth is not weakened.
4. If a source is only a cameras.yaml alias (not registered as id/snap), inherit those parent
   grants. No helper registry row is required. Parent relation comes from the structured
   `main_stream`/`sub_stream` map, never from stripping a `-main` suffix (so mismatched pairs
   like `main-st-south` / `main-st-south-main`, and logical camera ids that themselves end in
   `-main`, work).

Missing cameras.yaml mapping or an unknown camera denies access, including for admins.
Requesting `cam=<helper>` without a registry row does **not** inherit parent capability.
`resolve_camera` defaults `main_stream` to the camera id when unset — tests and configs that
rely on that default miss `main != sub != id` regressions; real cameras.yaml must set both
streams explicitly when they differ.

Live App Hub routes (`/cams/api/stream/<id>`, snap, list) stay exact id/snap `canStream` /
`list` — this change does **not** invent unregistered go2rtc stream aliases. HD remains
`/api/export` and `/api/clip` on the parent id with `quality=main`. Shared
`apphub/camera_acl_vectors.json` is unchanged. After this backend ships, removing fake helper
rows from `cams.json` removes the extra live-grid tiles; App Hub code stays the same (pinned
consumer historically hub `89277156` ↔ record `8e320496`).

**Responses.** Unknown and forbidden cameras give the same 404. The API never returns `users` or
`roles`; clients see only `{id, name}`.

**Freshness.** The registry is re-read when the file's mtime, size or inode changes, checked on
each request, so a revocation applies to the next request. Open `live.mp4` connections are
re-checked every 15 seconds. A clip or export download that has already started runs to completion.

## Where identity comes from

| Component | Source of user and roles |
| --- | --- |
| `cams_record/web.py` | The verified `x-apphub-token` JWT: `sub` and `roles`. If `x-apphub-user` is present and differs from `sub`, the request is denied. |
| `apphub/cams.js` | `s.user` plus the hub's `C.cameras` helper, which supplies live roles. |

`x-apphub-roles` is not read by cams-record. The hub sends the same roles in the signed JWT, so the
header adds nothing and would be the weaker source.

`cams.js` runs inside the hub and receives the raw client request, so `x-apphub-*` headers are
client-controlled there. It never reads them.

`C.cameras` has this interface, with every method taking the session user:
`list(user)`, `get(user, id)`, `can(user, id)` and `canStream(user, name)`. Without it, `cams.js`
falls back to `fileAccess()`, which has no roles: open cameras and users named in `users` work, but
role grants and the admin rule do not. It can under-grant, never over-grant.

Pinned `C.cameras` interface (unchanged App Hub behavior; helpers leave the grid when removed
from `cams.json`):

| Method | Required behavior |
| --- | --- |
| `list(user)` | Visible registry cameras only: `{id, name}`. Do not register go2rtc helper stream names. |
| `get(user, id)` / `can(user, id)` | Exact registry id. No parent boost for a missing helper id. |
| `canStream(user, name)` | Exact id/snap owners (shared vectors). Live grid streams the logical camera id. Recorder alias inheritance is cams-record `CameraAccess.allowed` only. |

## Routes

| Route | Check |
| --- | --- |
| `GET /cams/`, `/cams/api/list` | Filtered to visible cameras |
| `/cams/api/snap/{id}` | Camera, plus its `snap` stream |
| `/cams/api/stream/{id}/*` | Camera and stream; `hls/*` also needs a session id issued for this camera |
| `/cams-record/api/timeline`, `playback`, `hls/{cam}/*`, `export`, `export/info`, `clip`, `clip/info` | Camera id, before any database or file access |
| `/cams-record/api/hls/{cam}/seg/{id}.ts` | As above; the segment must also belong to `{cam}` |
| Detection push alerts (poller and webhook) | Camera id, per subscriber, with the roles saved from their last verified JWT; see `docs/web-push.md` |

go2rtc HLS session ids are not tied to a camera, so `cams.js` records each id's camera when it serves
`index.m3u8`, and refuses that id for any other camera.

`/cams-detect` is **not covered**. See below.

## Configuration

| Key | Default | Environment |
| --- | --- | --- |
| `access.enforce` | `true` | `ACCESS_ENFORCE` |
| `access.registry_file` | `/apphub-data/cams.json` | `ACCESS_REGISTRY_FILE` |
| `access.config_acl_max_age_s` | `60` | `ACCESS_CONFIG_ACL_MAX_AGE_S` |

Mount a dedicated directory containing only `cams.json`, not the file: the hub saves by atomic rename, and a
single-file bind mount would keep reading the old inode. The container user needs read access.

If the file is missing or unreadable, every camera route returns 404, including for admins.
`access.enforce: false` turns the checks off and is only for local development and tests.

### `camera_source: db` (cams_config ACL)

When cams-record runs with `camera_source: db`, `can_view_camera` and `CameraAccess.allowed` take
access from cams_config camera objects instead of `cams.json` (file and shadow modes are unchanged):

- Only **enabled** cameras are visible (disabled are hidden from everyone, including admin).
- Role `admin` sees every enabled camera.
- `visibility: everyone`: any cams user (checked before grants; a bad grant row cannot close it).
- `visibility: restricted`: granted users and roles; with no grants, admin-only.
- A malformed grant entry is ignored on its own; sibling valid grants still apply.
- Malformed visibility is admin-only (never open).
- Registry `snap` comes from `use.snapshot` → that stream's go2rtc name, and only when it is not
  the main/HD stream; a bad snap name drops the alias only, never the camera.

While the database is down, the last-good camera objects stay in effect for
`access.config_acl_max_age_s` seconds after the last **successful full load** (or the cache file's
`saved_at` on a cold start). A successful revision probe alone does not refresh that clock. A cache
file with no `saved_at` is already stale. After expiry only admins pass. Non-admins never fall back
to `cams.json` for these checks.

## Rollout

Deploy in this order:

1. App Hub with `C.cameras`, and `apphub/camera_acl.js` next to `cams.js`.
2. cams-record with `APPHUB_CAMERA_REGISTRY_DIR` set.
3. Add `users` or `roles` to cameras only after `/cams-detect` enforces this contract.

## Open gap: `/cams-detect`

The hub proxies `/cams-detect` straight to the cams-detect service, and neither this repository nor
App Hub filters it. Until that service enforces this contract, a user can still read restricted
cameras' data from it. That covers `/api/cameras`, `/api/events`, `/api/events/{id}/snapshot*.jpg`,
`/api/frame/{id}.jpg` and the `/api/stream` event stream. Snapshots are looked up by event id, so
that service has to resolve the event's camera before answering.

The registry bind source must be a dedicated directory containing only `cams.json`.
Configure App Hub to write its live registry there; never mount its general data or secrets directory.
Mount this dedicated directory read-only in cams-record so atomic replacements remain visible.

Recorded footage uses `CameraAccess.allowed` as above before querying recordings. Explicit
id/snap sources still require `can_stream(src)`; config-only aliases inherit via `source_owners`
without fake helper cameras. Missing cameras.yaml mappings still deny, including for admins.
