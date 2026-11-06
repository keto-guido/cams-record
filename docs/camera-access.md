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

**HD main companions (recorder).** cams-record `cameras.yaml` names the HD go2rtc stream
`<id>-main` (`main_stream`) and the low-res stream `<id>` (`sub_stream`). The recorder authorizes
recorded footage for the **parent** camera id. When checking configured sources, a stream named
exactly `<parent>-main` inherits that parent's registry ACL **if and only if**:

1. `<parent>` is a real registry camera, and
2. that parent (or another configured camera) lists `<parent>-main` as a `main_stream` /
   `sub_stream` source in cameras.yaml.

This is not an arbitrary suffix bypass: only the `-main` companion used by real low-res camera
definitions inherits. No matching parent mapping → the companion is unknown and denied, including
for admins. Requesting `cam=<id>-main` as the camera id does **not** inherit parent capability;
only a real registry row for that id would. Restricted parent denied ⇒ companion denied, with the
same admin exception as the parent (`admin` may access registered parents).

Live view must keep using the parent id (low-res). HD is for export/clip `quality=main` on the
parent id, not a separate live tile.

**Publication / App Hub list.** Shared registry publication consumed by App Hub (`C.cameras.list`,
`fileAccess().list`, `/cams/`, `/cams/api/list`) must include **only** parent low-res cameras.
Internal `*-main` HD helpers must not appear in that list — they are go2rtc / cameras.yaml source
names, not UX cameras. Do not keep fake `*-main` registry rows merely to satisfy recorder source
checks; the recorder parent-mapping above replaces that hack. cams-record exposes
`publishable_cameras()` / `is_main_companion_id()` as the publication filter contract; App Hub's
own `camera-access.js` / `C.cameras` (pinned consumer of cams-record's registry contract; App Hub
revision historically tied to cams-record `@8e320496`, e.g. hub change `89277156`) must apply the
same filter when refreshing the wrapper. This repository does not modify App Hub itself.

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
| `list(user)` | Visible **parent** cameras only: `{id, name}` (optional `snap` internally). Never emit `*-main` companion/helper ids. |
| `get(user, id)` / `can(user, id)` | Exact registry id. No parent boost for a missing `*-main` camera id. |
| `canStream(user, name)` | Exact id/snap owners (shared vectors). Live grid streams the parent id (low-res). Recorder HD companion inheritance is cams-record-only via configured `main_stream`. |

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
by id or snap before querying recordings. A configured `<parent>-main` HD companion additionally
inherits the parent camera's ACL as described above, so HD export/download works without registering
fake `*-main` companion cameras. Other missing mappings and unregistered sources still deny access,
including for admins. Shared non-companion raw streams still need an owning camera id or snap.
