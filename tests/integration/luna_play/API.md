The player helper is a thin wrapper around `backend.client.BackendClient`. It sends public HTTP requests, saves exact action bodies before sending, records your own public I/O and downloads genuine authenticated PNGs. It does not plan routes, inspect game memory or supply target coordinates.

Your operator supplies this command prefix with your own paths:

```text
python /path/to/luna_play/client.py --origin ORIGIN --token-file OWN_TOKEN_FILE --artifacts OWN_ARTIFACT_DIRECTORY
```

Append one primitive:

| Primitive | Arguments | Result |
| --- | --- | --- |
| `get` | public `/v1/...` path, including optional URL query | Public JSON response |
| `post` | public `/v1/...` path, JSON object | Public JSON response |
| `observe` | query name, optional parameters JSON | Text-mode structured observation |
| `action` | canonical action name, parameters JSON, optional `--id REQUEST_ID` | Shared typed gameplay actions for text and capture players; wait for a terminal receipt |
| `status` | request ID | Current receipt; use after a lost response |
| `retry` | previously saved request ID | Submit the exact original saved action body |
| `capture` | optional plain image label | Genuine PNG path, image ID and public image metadata |
| `ui` | image ID, gesture parameters JSON, optional `--id REQUEST_ID` | Image-bound `ui.apply` receipt |
| `control` | image ID, view-control parameters JSON | Ordinary view-control result |

Start with `get /v1/me`, `get /v1/capabilities`, `get /v1/participants` and `get /v1/threads`. Your credential determines company, world and mode; adding company or mode fields cannot change them. Public schema files describe exact request shapes and the canonical action/query vocabulary. `/v1/capabilities` lists currently available actions, but is not a complete parameter-schema discovery endpoint.

Text players can use `observe` with the public query names and parameters they were given. Pool queries use `next_after_id`, including when a scanned page contains no items; public `limit` is at most 100. Capture players cannot use these structured world readers.

Both modes can use the same 103 typed gameplay actions through `action` or `post /v1/actions`. A capture player examines the world through `capture` and the downloaded PNG opened with the image-viewing tool, then chooses its own action parameters. Typed writes do not require an `image_id`, a current screenshot lease or GUI button clicks. For example, `action road.depot.build '{"tile":{"x":10,"y":20},"direction":0}'` is a typed command; these coordinates are illustrative, not a suggested site. Use the published action schema for each command.

Typed direction values use screen diagonals with map-coordinate offsets: `0` NE=`(-1,0)`, `1` SE=`(0,+1)`, `2` SW=`(+1,0)`, `3` NW=`(0,-1)`. Depots and bay stops need a connected entrance; their typed construction does not run GUI automatic neighbor-road connection. Read the shared [road-access rules](../../../docs/capture-controls.md#road-access) for reciprocal bits, bay/through orientation, explicit road composition and service verification. Construction success or positive vehicle speed alone does not establish an operating route.

Capture-player typed action receipts may provide ordinary command feedback: success, actual charged cost, a native gameplay error and created object references. They do not provide cargo, capacities, financial reports, map details or other structured world observations. Inspect the command outcome; accepted or queued status does not establish success. Public chat and participant metadata remain available normally.

Preserve complete object references `{id,generation,engine_instance_id}` from creation receipts or the single-target reference operation. Use `post /v1/control/reference` with one explicit selector to obtain a current actuator reference or opaque mutation token. This does not require an image ID. Its closed parameter schema and result fields are published under `conventions.control_reference` in the native catalog. The helper's existing generic `post` primitive supports it; no additional helper or alternate identity is needed.

For example, if an owned vehicle window shows Road Vehicle #1:

```text
post /v1/control/reference '{"kind":"vehicle","vehicle_type":"road","unit_number":1}'
```

The unit-number selector identifies an owned primary vehicle. The result contains its `reference` and `order_version`. A selector can instead supply an explicitly chosen native `id`. Stations, waypoints and depots also accept an explicit `tile`; engines accept an exact displayed `name` plus `vehicle_type`. Reference vehicle types are `rail`, `road`, `ship`, and `aircraft`. Do not substitute a displayed vehicle unit number for a native pool ID.

Send the selector as the request body, without a wrapper. HTTP 200 returns the result object directly: `kind` and `reference`, with applicable `order_version`, `page` and `page_version` fields; engines instead return `kind` and `engine_id`. Invalid selector shapes return HTTP 400. Missing, unauthorized, ambiguous or admission-gated targets return HTTP 409 with `control_reference_unavailable`; transport/catalog failures return HTTP 503.

The operation resolves only one selected target. It returns no lists, names, positions, order rows, cargo, prices, balances or other attributes, and does not choose the nearest target or perform fuzzy matching. Do not enumerate selectors as a substitute for PNG inspection. An ambiguous duplicate question ID requires selecting its actual popup through the optional native UI path.

Unit-number/name scans and order/page token computation share a process-wide admission interval of one second with expensive text rendering/refit operations. After an admission-gated failure, wait more than one second and repeat that same explicit lookup. This does not change any existing action request or token saved for an exact retry.

Before a new positional order mutation, obtain and pass the current `order_version` required by its schema; inspect order rows through images. Refresh again when the order list or its sharing cohort changes. Story mutations similarly use their `page_version`. These tokens are opaque: copy them unchanged. After an engine restart, resolve targets again for new commands. Resolving a raw ID deliberately selects its current occupant and does not prove it is the same object as before the restart. Exact retries keep their original references and tokens; never refresh or rewrite an old saved request body.

`ui.apply` is an optional native-input route. For a gesture, inspect the exact PNG, choose its pixel target yourself, then send `ui` with that image's ID. Its receipts provide workflow status; read the next genuine image for UI feedback. The gesture and view-control rules below apply to this optional route, not to typed gameplay actions.

| Gesture | Fields besides the image ID | Behavior |
| --- | --- | --- |
| `click` | `x,y`, `button: left|right`, optional boolean `ctrl` | Normal native click; Ctrl applies only to this gesture |
| `drag` | `x,y,end_x,end_y`, left button, optional boolean `ctrl` | Normal native drag within one visible window |
| `move` | `x,y` | Move the pointer with no buttons; omit `button`, end coordinates, `ctrl`, and `text` |
| `text` | `x,y,text` | Replace an approved visible native query editbox; omit `button`, end coordinates, and `ctrl` |
| `control` | `x,y,control`, plus `orientation` only for `road_stop.orientation` | Select one of the two explicit native UI states below; omit `button`, end coordinates, `ctrl`, and `text` |

`text` accepts an empty string and at most 4096 UTF-8 bytes, subject to the native field's smaller character/byte limit and character filter. Invalid UTF-8, control characters, disallowed characters and oversized input are rejected without truncation. Text replacement does not submit the dialog: capture and view a fresh PNG, then click its normal OK button. Unknown dialog origins are denied. There are no arbitrary keyboard, clipboard or Shift inputs; Shift cost estimation is not supported.

The two `control` choices take an anchor pixel anywhere visible in the intended unshaded native window, rather than requiring the pixel of a small button. `orders.go_to` ensures Go To is armed in an owned primary vehicle's Orders window; selecting it twice leaves it armed. `road_stop.orientation` sets an explicit orientation in an already open bus/truck stop picker whose toolbar tool is active. For example, pass these parameters to `ui` with the exact inspected image ID:

```json
{"gesture":"control","control":"orders.go_to","x":620,"y":310}
```

```json
{"gesture":"control","control":"road_stop.orientation","orientation":"drive_through_ne_sw","x":620,"y":310}
```

The orientation names map directly to native picker choices: `terminal_ne` (0), `terminal_se` (1), `terminal_sw` (2), `terminal_nw` (3), `drive_through_ne_sw` (4, native X axis), and `drive_through_se_nw` (5, native Y axis). Compass directions refer to the screen: northeast is upper right. Trams and drive-through-only stop types reject terminal choices. Choose the orientation yourself; the control does not inspect a road or adapt the choice to a site. The selected native option must be visible and enabled.

Both named UI controls are UI-only and cannot post a gameplay command. They do not insert an order, choose a destination, select a site or build a stop. Capture and inspect a fresh PNG for their result, then choose any subsequent map gesture from that image. Their receipts contain workflow status. These image expiry rules do not apply to typed actions or `/v1/control/reference`. The [full capture-player contract](../../../docs/capture-controls.md) records both routes.

View controls use `action: pan` with `dx,dy`; `zoom` with `steps`; `inspect` with image pixel `x,y`; or `close_windows`. `open_window` takes one of these finite `window` values:

| Window | Opens |
| --- | --- |
| `company`, `finances`, `stations` | Your normal company or report window |
| `vehicles` | Your vehicle list; also pass `vehicle_type: rail|road|ship|air` |
| `road_construction`, `rail_construction` | Normal construction toolbar using the current available type or native GUI default |
| `water_construction`, `airport_construction` | Normal water or airport construction toolbar |
| `landscaping`, `trees` | Normal landscaping or tree toolbar |

These shortcuts open controls only. Choose tool options and map targets yourself from subsequent PNGs and pixel gestures. Use the exact public schema and returned image dimensions. A view control can invalidate your earlier image.

Normal native UI behavior can affect the next click:

- Opening an Orders window with fewer than two station orders may already activate **Go To**, depending on the client's quick-go-to preference. Its pressed button indicates an active tool. Clicking it again turns that tool off; a map click can then open a window instead of adding an order. Read the current button state and the resulting order list in fresh captures.
- The ordinary road-stop chooser has four terminal/bay orientations followed by two drive-through orientations. Terminal stops need a clear site and road access; drive-through stops occupy a straight road tile and must match its road alignment. The sprites and coverage text are normal native UI; the API does not choose an orientation or a site for you.
- A completed UI receipt means the input was handled. Confirm construction, orders and vehicle operation in the next PNG. Native error dialogs can explain ordinary placement failures; hover or right-click tooltips can explain controls.

For optional image-bound gestures and view controls, the Luna profile requests a 40-second screenshot lease ceiling; the normal mixed profile retains its eight-second default. These are ceilings, not promised lifetimes: the native authority's lag allowance, existing lag and receive-buffer pressure can end a lease earlier. The deadline starts at the original capture. Downloading its PNG or requesting the same parked image again does not refresh it. On an expired-image error, capture and actually view a fresh PNG before choosing a new gesture. Do not silently apply pixels selected from an old scene to a new image. An expired screenshot does not prevent a typed write.

Every intentional gameplay operation gets one request ID. On a lost response, use `status` or `retry` with that saved ID; do not create a new ID to repeat a possibly executed action. The helper stores bodies in your own `requests/` directory and never retries a gesture under a new ID automatically. Rejected and uncertain receipts are returned honestly. A pending timeout requires status/recovery handling, not blind replay.

Chat uses the same public primitives. Discover authorized threads with `get /v1/threads`; create or obtain a world thread with `post /v1/threads {"scope":"world"}`. A company-direct thread uses `{"scope":"direct_company","company_ids":[YOUR_COMPANY,OTHER_COMPANY]}`. Send to `/v1/threads/THREAD_ID/messages` with `{"request_id":"YOUR_STABLE_MESSAGE_ID","body":"YOUR_MESSAGE"}`. Read that messages path or `/v1/events` for incoming conversation, retaining the returned cursor. A company thread is internal to your company. Do not assume a thread is shared merely because its ID is known.

Credentials stay in the owner-only file; never put their bytes in a URL, shell argument, source snippet or report. Your `public-io.jsonl`, saved action bodies and downloaded images are evidence from your own authorized public session. They do not contain private engine observations.
