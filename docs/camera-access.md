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

**Configured stream sources (recorder).** cams-record authorizes recorded footage for the
**logical** camera id (the cameras.yaml key / registry id). Each camera's structured
`main_stream` and `sub_stream` names are checked against registry owners. A go2rtc stream name
inherits a logical camera's registry ACL when that camera lists the name as `main_stream` or
`sub_stream` — the names need not equal the camera id or follow `<id>-main` (live example:
mismatched low-res / HD pair `main-st-south` / `main-st-south-main` under a different logical
id). Parent relation always comes from that mapping, never from stripping a `-main` suffix.

No cameras.yaml owner and no registry id/snap hit → the stream is unknown and denied, including
for admins. Requesting `cam=<stream-helper>` without a registry row does **not** inherit parent
capability. Restricted parent denied ⇒ its configured streams denied, with the same admin
exception as the parent (`admin` may access registered parents).

Live view must keep using the logical camera id (low-res path). HD is for export/clip
`quality=main` on that id, not a separate live tile.

**Publication / App Hub list.** Shared registry publication consumed by App Hub (`C.cameras.list`,
`fileAccess().list`, `/cams/`, `/cams/api/list`) must include **only** logical cameras. Internal
go2rtc helper names (`main_stream` / `sub_stream` values that are not themselves camera ids) must
not appear — including both `*-main` HD helpers and mismatched low-res aliases. Do **not** filter
by a bare `-main` suffix: a logical camera whose id ends in `-main` is a real UX camera and must
remain listed. Do not keep fake helper registry rows merely to satisfy recorder source checks; the
structured mapping above replaces that hack. cams-record exposes `publishable_cameras(cameras,
sources)` / `is_configured_stream_helper()` / `CameraAccess.publishable()` as the publication
contract; App Hub's own `camera-access.js` / `C.cameras` (pinned consumer of cams-record's
registry contract; App Hub revision historically tied to cams-record `@8e320496`, e.g. hub change
`89277156`) must follow the same rule when refreshing the wrapper. This repository does not
modify App Hub itself.

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

Pinned publication interface for App Hub wrappers refreshing against this contract:

| Method | Required behavior |
| --- | --- |
| `list(user)` | Visible **logical** cameras only: `{id, name}` (optional `snap` internally). Never emit go2rtc `main_stream`/`sub_stream` helper ids. Do not drop logical ids that end in `-main`. |
| `get(user, id)` / `can(user, id)` | Exact registry id. No parent boost for a missing stream-helper camera id. |
| `canStream(user, name)` | Exact id/snap owners (shared vectors). Live grid streams the logical camera id (low-res). Recorder stream inheritance is cams-record-only via cameras.yaml `main_stream`/`sub_stream`. |

## Routes

| Route | Check |
| --- | --- |
| `GET /cams/`, `/cams/api/list` | Filtered to visible cameras |
| `/cams/api/snap/{id}` | Camera, plus its `snap` stream |
| `/cams/api/stream/{id}/*` | Camera and stream; `hls/*` also needs a session id issued for this camera |
| `/cams-record/api/timeline`, `playback`, `hls/{cam}/*`, `export`, `export/info`, `clip`, `clip/info` | Camera id, before any database or file access |
| `/cams-record/api/hls/{cam}/seg/{id}.ts` | As above; the segment must also belong to `{cam}` |

go2rtc HLS session ids are not tied to a camera, so `cams.js` records each id's camera when it serves
`index.m3u8`, and refuses that id for any other camera.

`/cams-detect` is **not covered**. See below.

## Configuration

| Key | Default | Environment |
| --- | --- | --- |
| `access.enforce` | `true` | `ACCESS_ENFORCE` |
| `access.registry_file` | `/apphub-data/cams.json` | `ACCESS_REGISTRY_FILE` |

Mount a dedicated directory containing only `cams.json`, not the file: the hub saves by atomic rename, and a
single-file bind mount would keep reading the old inode. The container user needs read access.

If the file is missing or unreadable, every camera route returns 404, including for admins.
`access.enforce: false` turns the checks off and is only for local development and tests.

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

Recorded footage also checks both configured `main_stream` and `sub_stream` against registry owners
by id or snap, and against every logical camera that lists that name in cameras.yaml, before
querying recordings. HD export/download and mismatched stream aliases therefore work without
registering fake helper cameras. Unmapped / unregistered sources still deny access, including for
admins. Shared raw streams still require every owning logical camera (and any id/snap registry
hit) to allow the viewer.
