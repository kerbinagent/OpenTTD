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

Do not expose a generic repository file server. The tests and this document do
not need HTTP routes. The backend owns authentication, authorization and image
cleanup. The viewer sends a user-entered bearer token in request headers and
keeps it in tab memory only; it never places credentials in URLs or storage.

## API dependencies

- `GET /v1/me` supplies the authenticated display name, identity, company and
  immutable run mode. `GET /v1/capabilities` supplies service availability.
- `GET /v1/participants` returns `{participants: [...]}` with safe labels:
  `identity_id`, `display_name`, `kind`, `company_id`, optional `company_name` and
  `presence`. It does not carry structured game observations.
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
node --test web/events.test.mjs
```

The parser tests cover multiline SSE data, every CRLF split position, IDs,
incomplete events and bounded buffering. Real browser and native-renderer
integration checks require the running same-origin world service.
