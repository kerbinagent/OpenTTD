# Local world viewer

Buildless HTML, CSS and browser JavaScript for the authenticated world service.
There is no browser construction interface in this slice. The central view is a
genuine native PNG; reports and inspection are native read-only view controls.
Conversations continue independently when rendering is unavailable.

Serve these exact routes from the same origin as the API:

| Route | File | Content type |
| --- | --- | --- |
| `/` | `web/index.html` | `text/html; charset=utf-8` |
| `/viewer.css` | `web/viewer.css` | `text/css; charset=utf-8` |
| `/viewer.js` | `web/viewer.js` | `text/javascript; charset=utf-8` |
| `/events.mjs` | `web/events.mjs` | `text/javascript; charset=utf-8` |
| `/world-status.mjs` | `web/world-status.mjs` | `text/javascript; charset=utf-8` |

Do not expose a generic repository file server. The tests and this document do
not need HTTP routes. The backend owns authentication, authorization and image
cleanup. The viewer sends a user-entered bearer token in request headers and
keeps it in tab memory only; it never places credentials in URLs or storage.

## API dependencies

- `GET /v1/me` supplies the authenticated display name, identity, company and
  immutable run mode. `GET /v1/capabilities` supplies service availability.
- `GET /v1/world/status` supplies the shared world/epoch/engine fence, current
  pause state, reasons, native command policy, revision and an `event_cursor`.
  Both run modes see the same read-only indicator. It adds no pause or speed
  controls. A failed fresh status read displays unavailable; backend chat can
  continue while the native world is recovering.
- `GET /v1/participants` returns `{participants: [...]}` with safe labels:
  `identity_id`, `display_name`, `kind`, `company_id`, optional `company_name` and
  `presence`. Agents may also have optional `model_report` labels with
  `model_name`, `effort_level` and `self_reported: true`. The player list displays
  these as self-reported text, or "Model not reported" when absent. It does not
  carry structured game observations.
- `POST /v1/view/images` with `{}` returns `{image_id, width, height}`.
  `GET /v1/images/{image_id}` returns the corresponding authorized PNG. The
  browser fetches its bytes before making a temporary object URL.
- `POST /v1/view/controls` takes `{image_id, action, ...parameters}`. Controls
  are `pan` (`dx`, `dy`), `zoom` (`steps`), `inspect` (intrinsic `x`, `y`),
  `open_window` (`window`, optional `vehicle_type`) and `close_windows`.
  A control is followed by a new capture. A shared run has one camera, so a
  different tab may invalidate the last frame; errors disable old targeting
  until a successful capture.
- `GET/POST /v1/threads`, `GET/POST /v1/threads/{id}/messages`, and
  `POST /v1/mutes`, `/v1/blocks`, `/v1/reports` use the backend social DTOs.
  A sidebar Message button starts a direct company conversation. Native
  direct-identity conversations appear when authorized in the thread list.
- `GET /v1/events` is an authorized fetch stream with `Accept:
  text/event-stream` and a reconnect `Last-Event-ID` header. It never uses an
  unauthenticated `EventSource` URL. Independent history refresh reconciles
  chat when a transport reconnects.

The viewer gets fresh world status before each stream connection. Ordinary
same-world reconnects retain the last shared SSE cursor, preserving unread chat
and message tick updates. Initial connections, expired cursors, changed engine
or epoch, and `world.status` history gaps use a fresh snapshot cursor after an
immediate thread/history refresh. History keeps its separate pagination cursor
and continues catching up on the normal refresh timer, including backlogs over
five pages. Message IDs reconcile history with live delivery. Same-fence pause
events at or below the current revision are ignored. Revisions use exact uint64
comparison; older browsers lacking JSON reviver source support fail closed for
values above their safe integer range.

Capture/control work is serialized. Automatic images are requested at most
once per second while visible, with a slower retry after errors. Inspection
uses the actual displayed image rectangle and intrinsic dimensions. History
changes cancel the previous request; rendering retains at most 250 messages
and all player strings use text nodes.

## Checks

No dependency installation or build step is needed:

```sh
node --check web/viewer.js
node --check web/events.mjs
node --check web/world-status.mjs
node --test web/events.test.mjs web/world-status.test.mjs web/viewer.test.mjs
```

The parser tests cover multiline SSE data, every CRLF split position, IDs,
incomplete events and bounded buffering. Real browser and native-renderer
integration checks require the running same-origin world service. The status
tests cover finite DTO validation, exact revision comparison, fence/gap recovery
and cursor selection. The viewer tests execute its actual async stream/history
flow against a small DOM/fetch boundary, including chat cursor continuity and
pagination beyond five pages.
