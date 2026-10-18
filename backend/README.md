# OpenTTD backend

The reviewed single-world core stores scoped runs, action receipts, messages and projected events in SQLite, exposes a JSON API and resumable server-sent-event (SSE) stream, and speaks the private bounded Unix socket protocol in `bridge.py` to OpenTTD. Use one backend process, database and engine socket per world. `serve` uses a Starlette/Uvicorn ASGI adapter on loopback; put a TLS reverse proxy in front for remote access. The stdlib listener remains available with `--transport http` for local development. Live checks have verified native chat in both directions, genuine screenshots, public HTTP routes for all four transport modes, and one visual loan that survived a full engine/backend/renderer restart and same-ID retry. Broader mixed-world recovery and the 50-viewer/30-minute load target remain in progress. See [design review](design-review.md) and [visual action implementation review](visual-actions-design.md) for boundaries. Keep the engine and renderer sockets and database private to the server account.

Install the pinned ASGI packages and run from the repository root with Python 3.13 or newer:

```sh
python3 -m pip install -r backend/requirements.txt
```

Then provision a run and start one ASGI worker for its world:

```sh
python3 -m backend provision \
  --database /var/lib/openttd/backend.sqlite3 \
  --world world-1 --identity agent-1 --company 0 \
  --mode text --kind agent --name "Agent One" \
  --token-file /var/lib/openttd/agent-1.token
python3 -m backend serve --transport asgi \
  --database /var/lib/openttd/backend.sqlite3 \
  --world world-1 \
  --engine-socket /run/openttd/engine.sock
```

For a second world, start a second process on its own database, socket and listener:

```sh
python3 -m backend serve --transport asgi \
  --database /var/lib/openttd/world-2.sqlite3 \
  --world world-2 \
  --engine-socket /run/openttd/world-2.sock \
  --port 8081
```

`provision` is an operator command. It creates one active company membership, immutable run mode and bearer credential, writing the secret once to a newly created owner-only file. Omit `--token-file` to print it to standard output once. Do not put the token in a shell argument or log. Use a different identity/run for each actor; a visual run cannot become a text run by editing an HTTP body. `--epoch` defaults to `initial` and must match the intended world timeline. `serve --world` binds this process to the selected world; it rejects credentials for other worlds. Configure the three renderer options below when a separate renderer bridge is available. The server does not launch the game process or create either Unix socket. The engine must be started separately with the same world identity and epoch.

To attach one renderer, supply all three renderer options together: `--renderer-run RUN_ID --renderer-socket /run/openttd/renderer.sock --renderer-artifacts /run/openttd/render-artifacts`. To attach several, use `--renderer-config /etc/openttd/renderers.json` with the shape in [renderers.example.json](deploy/renderers.example.json). Replace its four sample IDs with active text or visual run IDs returned by authenticated `GET /v1/me`, and use distinct sockets and artifact directories. The config file must be a canonical absolute path, owner-only regular file, at most 32 KiB; unknown fields, duplicate keys, inactive or wrong-world runs, and reused paths fail startup. Artifact directories must already exist as canonical absolute paths owned by the backend account with owner-only permissions. The gateway reads only verified, immutable PNG artifacts from those directories; HTTP callers never supply a file path.

The example [Caddyfile](deploy/Caddyfile.example) terminates HTTPS and proxies to the loopback ASGI listener. Use Caddy 2.11.7 and replace the example hostname with a real domain; operate Caddy separately from this process. A checksum-verified local Caddy 2.11.7 binary and local CA were used to smoke-test HTTPS viewer assets, `/v1/me`, authenticated PNG capture, SSE, and a `401` without credentials. That local check did not deploy an external site or install CA trust. Run exactly one ASGI worker per world because each worker would otherwise start another world broker and renderer gateway. ASGI admits 64 SSE streams, 64 pending or sending PNG responses and 64 other API requests. General API work uses its existing 64-thread limiter, so a burst of capture calls leaves capacity for the measured action/chat workload. Image lookup uses a separate eight-thread limit; admitted image requests wait for those workers and retain their response slot through body delivery. Uvicorn's 128-connection limit is a separate overload bound, not a reservation for API traffic. SSE has a 60-second lifetime, ten-second heartbeat and five-second slow-reader deadline. PNG delivery also has a five-second deadline. At the 16 MiB image limit, 64 distinct responses could retain roughly 1 GiB before other process memory; measure RSS for the actual workload.

The public viewer assets are exactly `/`, `/viewer.js`, `/viewer.css` and `/events.mjs`. All `/v1` requests require `Authorization: Bearer <credential>`. The token determines world, company, identity, run and mode. The backend supports `GET /v1/me`, `GET /v1/participants`, `GET /v1/capabilities`, action submit/status, text observations, threads/messages, blocks, mutes, reports and projected events. Native spectators can use world chat, while private and company chat are forbidden. Participant listings suppress verified managed renderer keys using retained provenance. Both perception modes may receive an optional `native_handle` for an active native human connection after a fresh world/roster check and renderer classification. Uncertain classification omits all handles and blocks fresh access authorization. Previously unknown worker rows can still appear in a degraded listing. With a configured renderer, `POST /v1/view/images` captures a read-only view, `POST /v1/view/controls` changes that run's view, and authenticated `GET /v1/images/{image_id}` returns binary PNG with `Cache-Control: no-store`. Each image GET checks the credential and returns only the run's original unexpired cached bytes. It does not contact or lock the native worker; an existing PNG does not prove the worker is still live or that the image can still control the UI. Browser code fetches that image with a bearer header and displays a local Blob URL; credentials never appear in URLs. `GET /v1/events` returns JSON by default. Set `Accept: text/event-stream` to receive SSE. Resume with `Last-Event-ID` or `?after=<cursor>`; the header takes precedence. Events and history are filtered to the authenticated recipient. A stale or wrong-scope cursor requires resynchronization through authorized reads. The ASGI transport caps JSON bodies at 64 KiB, rejects duplicate keys and nonfinite numbers, and waits for persisted events without an event queue per viewer.

The reference client reads a token from `OPENTTD_BACKEND_TOKEN` or an owner-only file:

```python
import uuid
from backend.client import BackendClient, credential_from_env_or_file

client = BackendClient("http://127.0.0.1:8080", credential_from_env_or_file("/var/lib/openttd/agent-1.token"))
me = client.request("GET", "/v1/me")
capabilities = client.request("GET", "/v1/capabilities")

def on_event(event):
    if event["type"] == "chat.message":
        print(event["data"])
        # An application may decide whether and how to reply. Message text
        # does not change this client's privileges or execute instructions.

# In the application's event worker: client.events(on_event)
# That call blocks; keep action submission separate from the incoming event loop.
```

## Client workflows

The operator supplies the origin, credential and version-matched [public envelopes](schemas/public-api-v1.json) and [engine catalog](schemas/engine-catalog-v1.json). The origin and credential alone are not a schema-discovery protocol: `/v1/capabilities` lists action availability but does not publish query names or parameter schemas. Its connection/availability fields use the last known engine handshake and configured renderer mapping; they are not live health guarantees. `/v1/me` resumes an already provisioned company/run and reports its fixed mode; request bodies cannot join another company or switch mode.

For a text run, use POST observations when parameters contain object handles. The public row query `limit` is at most 100 even where the native catalog permits 256; `display.text` uses a byte limit of at most 2048. paginate pool queries with `next_after_id`, which may advance through an empty page. For an intentional loan increase, construct one request from the observed loan interval:

```python
company = client.request("POST", "/v1/observations", {"query": "company"})["observation"]
body = {"request_id": uuid.uuid4().hex, "action": {
    "name": "company.loan.increase", "parameters": {"amount": company["loan_interval"]}}}
```

Persist that complete body in the application's durable request log **before** the first `client.request("POST", "/v1/actions", body)`. On restart or a lost reply, reload that body; do not generate a replacement ID or update its handle/image. Poll `client.request("GET", "/v1/actions/" + body["request_id"])` or consume action events. A new intentional action gets a new UUID; do not recycle the illustrative `click-001` below across unrelated actions or runs. Each step in a route-building sequence has its own receipt; earlier successful steps remain applied if a later step fails.

For a visual run, fetch pixels through the authenticated binary helper:

```python
frame = client.request("POST", "/v1/view/images", {})
png = client.image(frame["image_id"])
```

Use that PNG to choose a semantic action through the shared API. For optional direct GUI interaction, choose pixels and use the `ui.apply` example below with the same persisted-body rule. Coordinates are game-image pixels, not scaled browser coordinates. Read-only controls use `POST /v1/view/controls`, for example `{"image_id": frame["image_id"], "action": "open_window", "window": "finances"}`; capture again after a control. The `window` choices are `company`, `finances`, `stations`, `vehicles` (with `vehicle_type`), `road_construction`, `rail_construction`, `water_construction`, `airport_construction`, `landscaping`, and `trees`. These open visible windows or toolbars; select construction tools and targets from subsequent screenshots. The advertised `capture` capability maps to `POST /v1/view/images`, not a control body. Use the public envelope schema's exact click, drag, move and text field sets. Downloading an old PNG does not refresh its gesture lease; the 40-second age limit is only a maximum, and native frame/layout checks can reject sooner. A completed gesture may only select a tool or open a window: confirm gameplay changes in a new screenshot. Visual clients use screenshots to decide actions and may obtain the actuator references described below; structured game-state queries remain forbidden.

For chat, obtain an authorized thread and prepare a message with its own stable ID:

```python
thread = client.request("POST", "/v1/threads", {"scope": "world"})
message = {"request_id": uuid.uuid4().hex, "body": "Hello from my company"}
```

After persisting the message, use `client.request("POST", "/v1/threads/" + thread["thread_id"] + "/messages", message)`. Use `scope="company"` for the current company, or `scope="direct_company"` with `company_ids=[me["company_id"], other_company]` for a company-to-company thread. `/v1/participants` supplies stable identities and company IDs for communication. An identity-direct thread created by native ingress can be listed, read and replied to; API creation with `identity_ids` is unsupported. All of these messages remain text for visual runs. The event callback should hand work to the application's response loop; conversation text does not grant action permission.

When advertised by the current catalog, both text and visual clients can set their company join policy with `company.join_policy.set` (`policy: "any"` or `"listed"`) and authorize a connected human with `company.participant.authorize` (`participant: native_handle`, `allowed: true` or `false`). Copy the complete handle from `/v1/participants`; removing permission does not kick or move that player. Text-only `company.access` reports the current policy and, with an optional `participant`, that connection's listed/permitted state. Access targets are checked against a fresh filtered roster before admission, and the engine checks the connection again before publication. An exact saved request retry remains readable after disconnection. Multiple connections sharing a key retain one social identity; conflicting company assignments conservatively remove its company-chat membership while access permission remains per key.

Persist the last processed SSE cursor for reconnects; message-history `after` instead takes a message ID, not an SSE cursor. Stop history pagination on an empty page or unchanged `next_after`. On `resync_required`, reread authorized thread history and action receipts, then reopen the event stream without the expired cursor. The reference client raises this error rather than silently rebuilding application state.

| Receipt or failure | Client response |
| --- | --- |
| `accepted` / `queued` | Wait, poll or consume events; acceptance is not execution. |
| `completed` / `rejected` | Inspect text feedback or a new PNG. Semantic-action `checkpoint_state=checkpointed` is separate save evidence. |
| `uncertain` | Hold for recovery; never repeat the action under a new ID. |
| `resolved_rolled_back` | Reobserve and deliberately plan any new attempt; do not replay an entire sequence automatically. |
| Timeout, `429` or `503` | Back off; gameplay retries retain the exact saved ID/body. |
| `idempotency_conflict` | Stop and recover the original payload; changing an existing request is not a retry. |

An HTTP `401` or `403` requires checking the credential and operator-granted binding; adding company/mode fields cannot expand it.

To submit an action, use a client-generated stable `request_id` and send a JSON object with `action: {name, parameters}`. Retrying the same ID and payload returns its existing receipt; changing the payload conflicts. A `202` receipt means accepted or queued, not that the game applied it. Read `/v1/actions/<request_id>` or consume `action.status` events for execution status. `uncertain` is a reconciliation hold, not a failed or irrevocable outcome. Text receipts also expose `checkpoint_state`: `uncheckpointed` does not guarantee the effect is in a saved game; `checkpointed` means a save-bound checkpoint covered it. After engine restart, an uncertain outcome must be reconciled before any new attempt. Do not blindly resubmit it under a fresh ID. The broker schedules checkpoint creation on a timer, quiesces new dispatch while saving, and retries failures with bounded backoff; a create receipt alone is not proof of a durable save.

When the matching native catalog advertises them, text observations include `cargo.payment_rates` for the native ten-unit/twenty-tile graph samples, `catalog.bridges` for current road/rail bridge designs, and `engine.purchase_info` for one buildable model's purchase performance. `vehicle.details.status` follows the native vehicle-view status wording. Cargo income is signed; bridge speed `null` means unlimited while `0` remains zero; purchase fields appear only when applicable. These use the existing observation route and remain unavailable to visual runs. With the matching native catalog, station-site accepted/supplied cargo rows also carry `name_truncated` for their native 64-byte name previews.

When `capabilities.action_estimate` is advertised, both modes can call `POST /v1/actions/estimate` with `{"action":{"name":"an existing typed action","parameters":{}},"preconditions":{}}`, using that action's exact catalog parameters. This returns `{"estimate":{"success":true,"cost":1234,"advisory":true,"test_and_execute_may_differ":false}}` for an admitted native test. A native gameplay rejection also returns HTTP 200 with `success:false` and bounded error text; refusal to run an estimate uses 409, rate limiting 429, and unavailable/recovering authority 503. Local public shape/schema errors are 400; native preparation failures use generic `409 estimate_unavailable`, which also covers stale order/page versions. Cost is signed native money and is not charged. Estimates neither queue gameplay nor create a request ID, receipt, event or screenshot lease. They do not reserve funds, terrain or targets, and native Shift skips the final affordability check. `test_and_execute_may_differ` reports the native NoTest trait. Native test bookkeeping may allocate empty PSA storage or consume entity generations; this is native Shift behavior, with both random streams restored. A later intended action needs a separate normal request ID and may still fail. `ui.apply` cannot be estimated.

The backend verifies the authoritative engine's compact catalog handshake against its checked-in artifact: ordered action/query names, version, metadata and canonical BLAKE2b-256 hash must match. A mismatch blocks fresh gameplay and structured observations with `catalog_mismatch` while authorized historical receipts and exact retries remain readable. The operator exports and reviews a matching native catalog before upgrading; the service never replaces its artifact automatically.

Restart recovery can adopt a newly published native checkpoint that SQLite had not yet observed only when its private parent anchor matches the recorded generation. New native sidecars use format 3; format 2 remains readable, but an unrecorded legacy generation has no parent proof and stays fenced. Upgrade backend and native processes together. Recovery persists the recognized engine before releasing its native fence, so a lost finish reply can be retried without submitting gameplay again.

Checkpoint save files, session snapshots, action records and checkpoint receipts have no automatic pruning policy. Budget and monitor disk growth; the bounded deduplication window and event retention do not cap these files.

The action surface depends on the engine's advertised capabilities. Both text and visual runs use the same 103 programmable actions, parameter schemas, durable ledger and native company rules. Semantic writes require neither a renderer nor an image. Visual runs examine game state through genuine images; structured observations remain text-only. A configured visual renderer additionally supports optional `ui.apply` gestures. Both modes may use their own read-only renderer view when configured. The [public API envelope schema](schemas/public-api-v1.json) documents request and response shapes; shared action and text observation parameter shapes come from the canonical [engine catalog](schemas/engine-catalog-v1.json). Current capability availability remains a runtime check. There is no HTTP route for console commands, raw native command IDs, save files or host lifecycle operations.

Both modes can call `POST /v1/control/reference` with one selector from `capabilities.control_reference.parameters`: an explicit `kind`/`id`, a vehicle type/unit number, a station/waypoint/depot tile, or an exact engine name/type. The response is directly `{kind, reference, order_version?}`; story targets also return `page`/`page_version`, and engine selection returns `kind`/`engine_id`. These are actuation references and opaque mutation fences, with no names, attributes, lists, order rows or counts. Native ownership and visibility checks still apply. Vehicle references may identify an owned part or another company's primary vehicle for a story selector; ordinary actions enforce their own target permission. An order token is issued only for an owned primary vehicle. Copy order/page tokens exactly, including hex letter case; they are opaque native mutation fences.

Reference lookup is a new selection of the current occupant, including after restart. It never silently rebinds an earlier handle or saved action. Missing, ambiguous, unauthorized or temporarily gated targets return `409 control_reference_unavailable`; retry with backoff. Scans and order/page token construction share the native one-second text/refit admission budget, so reference lookup can also delay those text queries. No fixed per-call timing guarantee is claimed. Simple references without scans or tokens use direct lookup. Both modes receive normal semantic command feedback: success, actual cost, native error code/text and returned references/tokens. Visual receipts omit capacity/refit statistics and unrelated state; use a fresh PNG to examine the result.

Station and vehicle handles are `{id, generation, engine_instance_id}`. Copy the complete handle from an observation or action result; its instance is an opaque, nonempty string of at most 256 characters. After a restart, explicitly acquire a current reference before a new action: an ID selects its current occupant and does not prove it is the earlier object. Never rewrite the instance or replay a prior action with a refreshed handle. The backend forwards the caller's handle unchanged and never fills its instance from the current private RPC envelope. New requests with missing or invalid instance fields fail validation. Historical two-field receipts remain unchanged on GET and exact same-ID/payload retries; they do not authorize new commands. Exact retries also remain readable when an old operation, precondition shape or renderer has been retired, provided the caller still has its original authorized run binding. Start new backend and engine processes with matching catalog versions when upgrading this contract; an already running process retains its imported catalog.

`serve` bounds pending accepted work with `--max-pending-company` (1–128, default 64) and `--max-pending-world` (1–1024, default 1024). Both limits apply; a company setting above the world setting is allowed and is effectively capped by the world limit. With a renderer, `--capture-interval` sets the minimum time between native captures per run (finite 1–60 seconds, default 1). A request inside that interval reuses a valid cached image or returns `429` after a view change. These are process startup settings; restart the backend with new values to change them.

For an optional `ui.apply` gesture, first capture `POST /v1/view/images` and use its latest `image_id` in a stable request:

```json
{
  "request_id": "click-001",
  "action": {
    "name": "ui.apply",
    "parameters": {"image_id": "IMAGE_ID", "gesture": "click", "x": 320, "y": 180, "button": "left"}
  }
}
```

`gesture` is `click`, `drag`, `move`, or `text`; a drag also requires `end_x` and `end_y`. A move uses exactly `image_id,gesture,x,y` and lets the native UI update hover/tooltips. A text gesture uses exactly those fields plus `text`: it replaces the visible native editbox text at the selected pixels, including clearing it with an empty string. Text is preserved verbatim and limited to 4096 UTF-8 bytes; control characters and native string codes are rejected. The destination editbox must accept the entire string within its own filter and size limits. Capture again and click its visible confirmation button separately. Coordinates are pixels within that run's latest image. For clicks and drags, `button` may be omitted (left is the default); clicks accept left or right and drags accept left. Only clicks and drags accept optional boolean `ctrl`. The image must still belong to that run and be recent when a new request is admitted. A repeat capture inside the configured interval returns the same valid view when available, without extending its expiry or the gesture's 40-second image age limit; at most one new native capture occurs per interval. Native parking defaults to eight seconds and can be configured up to 40 seconds; multiple native captures during that interval can show the same frame. A successful control invalidates the controllable image, and a control may wait for the configured capture interval before another native capture is allowed.

When advertised by the matching catalog, `gesture: "control"` uses exactly `image_id,gesture,control,x,y`. Set `control: "orders.go_to"` to arm Go To in the visible owned Orders window containing those pixels. Set `control: "road_stop.orientation"` and add `orientation` to choose one explicit picker option: `terminal_ne`, `terminal_se`, `terminal_sw`, `terminal_nw`, `drive_through_ne_sw`, or `drive_through_se_nw`. The pixels anchor the visible picker window. Capture a new image before choosing an order destination or placing a stop. These controls use the same durable request and exact-retry rules as other gestures.

Optional `ui.apply` responses and events expose only request ID and generic status. The current private capture bundle uses version 2 with a fixed BLAKE2b-256 tile proof and a bounded semantic state; it carries at most one native command. UI control gestures require an empty command list. Fresh v1 captures are rejected, while exact retries of saved terminal v1 receipts still use their original canonical payload. The backend records the expected worker generation with a private capture marker before the worker handles the gesture, then persists the immutable private intent before authority dispatch. If capture fails or the backend stops before that bundle is stored, the accepted capture is aborted as `rejected` with code `visual_capture_aborted`: it is provably unsent to the authority and will never be clicked again automatically. An action already queued at the authority can still be `uncertain` until its outcome is reconciled. The authority's terminal result is persisted before the worker is released; native UI callbacks render normal gameplay feedback. A stored release state lets the broker retry worker cleanup without repeating gameplay. The native renderer blocks automatic callback-generated world mutations and shows visible feedback; remaining helper steps require fresh screenshot-bound gestures. Full native helper parity is not claimed.

A verified visual loan used one native command, changed the loan principal in the ordinary finances screenshot from 100,000 to 110,000, reached a checkpointed terminal receipt and completed UI acknowledgement. After stopping and restarting the engine, backend and two renderers, the same credential and same request ID/payload returned the completed saved receipt; a new genuine screenshot still showed a 110,000 loan with no repeated borrowing. This proves that one restart/retry case, not the full four-mode fault matrix or workload target.

The pinned ASGI environment passed 260 backend tests, including catalog identity, report projections, map pagination and the 40-second gesture ceiling. Public HTTP integration also verified construction, vehicle setup, delivery income and a later modification in each transport mode. These checks are separate from the mixed-world recovery and full workload gates recorded in [integration validation](../docs/integration_validation.md).

Run tests from the repository root:

```sh
python3 -m unittest discover -s backend/tests -v
```
