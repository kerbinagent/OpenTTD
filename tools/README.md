# Run one local mixed world

`mixed_world.py` starts an integration-enabled native server, one backend, and
two genuine native rendering workers. It provides human, text-agent and visual-agent
companies. For human browser play, use the [native WebAssembly client](../os/emscripten/README.md#fixed-loopback-native-relay)
alongside this launcher. It renders locally and forwards ordinary multiplayer
traffic to the same running world. External agents use the authenticated API;
visual agents can also interact through genuine native PNGs and `ui.apply`.

Build the engine as described in [the integration guide](../docs/agent_integration.md).
Install OpenGFX in a directory containing its `.obg` and graphics files. Use
the Python environment containing the backend's ASGI dependencies:

```sh
python3 -m pip install -r backend/requirements.txt
python3 tools/mixed_world.py \
  --binary /absolute/path/to/openttd \
  --baseset /absolute/path/to/baseset \
  --state-dir /tmp/ottd-local-world \
  --world local-world \
  --native-port 3979 --http-port 8080
```

This is local-only: both listeners bind `127.0.0.1`. ASGI is the default
transport; `--transport http` explicitly selects the dependency-free local
development adapter. Remote use requires a TLS reverse proxy. The launcher
does not install dependencies, register a website, or publish a service.

The new state directory must be empty or absent, with an existing parent.
An existing directory must be owned by you with mode `0700`. Choose a short
canonical path because private Unix sockets have a platform length limit.
The default new map is 512 × 512; `--map-size 128` is useful for local smoke
checks. `--initial-save /absolute/path/world.sav` supplies a starting save for
a new world. On restart, only the verified recorded checkpoint is loaded.

Wait for **World ready**; initial provisioning waits for the backend's first
periodic durable checkpoint and can take about a minute. Then open the printed
browser URL and paste a token from one of these private files:

| File under the state directory | Company | Access |
| --- | --- | --- |
| `credentials/human.token` | 1 (ID 0) | Native PNG browser play and chat when `ui.apply` is advertised |
| `credentials/text-agent.token` | 2 (ID 1) | Structured observation and normal agent actions |
| `credentials/visual-agent.token` | 3 (ID 2) | Genuine image perception and shared typed gameplay actions; optional native UI tools |

Tokens are created once in files with mode `0600`; the launcher never prints
their contents. Copy a token into a client without adding it to a URL, shell
argument, source file, or public log. The matching native human-client command
is printed without credentials. It sets `XDG_DATA_HOME` to the world's linked
base graphics and includes the launcher's `LD_LIBRARY_PATH` when present,
while retaining your normal native-client configuration and preferences.
Native company 1 allows humans to join;
company 2 rejects native joins; company 3 accepts only its renderer's persistent
authenticated key. Native client company numbers are one-based.

Each process has isolated XDG config/data directories and private logs. Renderer
keys are generated once in each worker's `config/openttd/secrets.cfg`, and their
public keys are obtained from each private worker handshake and confirmed
against the authoritative native roster before company access is restricted.
Fresh provisioning temporarily opens the visual company for its first worker
join; restarts retain its existing closed key grant. Player nicknames never
establish key ownership. The renderer map is private operator JSON keyed
by existing run IDs; public API requests cannot select sockets or files.

Press Ctrl+C to stop only the processes owned by this launcher and preserve
the entire directory. Run the same command to restart. A file lock prevents
two launchers from owning one world. Tokens and run IDs remain unchanged.
Four independent worlds use four invocations with different state directories
and port pairs.

The backend alone schedules checkpoints and reconciles request outcomes. The
launcher selects the checkpoint's exact save; native checksum verification and
backend recovery must complete before provisioning continues. Missing or
corrupt checkpoints, changed renderer identities, invalid credentials, and
partial first-time setup fail closed. The launcher never resets a world or
rotates credentials to make startup succeed. Inspect the retained files; use
a different new directory only when you intend to start a different world.

Restart returns to the last completed checkpoint. Changes after that save may
be rolled back; uncertain action outcomes remain subject to the backend's
explicit recovery policy. This launcher does not create a second journal or
promise that stopping the process forces a final checkpoint.

A renderer failure is local: its image stream goes offline while the world,
other renderer and conversations continue. Renderer failures are reported
without automatic restarts. An engine or backend failure stops the owned group.
Inspect `logs/engine.log`, `logs/backend.log`, `logs/r0.log`, and `logs/r2.log`.
Startup occurs on private local listeners before readiness is printed; do not
share credentials until **World ready** appears.

## Human browser play

Build a matching [WebAssembly client](../os/emscripten/README.md) and run its
separate loopback relay against this world's native port:

```sh
python3 tools/browser_client.py /absolute/path/to/public-build \
  --port 18087 --native-port 3979
```

Open http://localhost:18087/. It joins the human company directly; no API token
is needed. Native mouse movement, dragging, tooltips, wheel and keyboard input
run locally. Use **Fullscreen** to enter browser fullscreen with a resized game
surface. The native Players window offers company chat, including companies
controlled through the API with no connected native client. Private company
messages use the existing company audience shared by humans and agents.
Starting or stopping this relay does not restart the world.

## Capture browser controls

The backend's printed URL provides the image-based interface used by visual
agents. Screenshot refresh and gesture round trips make this route slower for
interactive human play than the native WebAssembly client.

Open the printed browser URL and paste your credential into the connection form,
never into the URL. Play controls require a visual-mode session advertising the
corresponding `ui.apply` gesture; the identity's human or agent kind does not
grant this capability. The printed matching native-client command is an
alternative way to play.

1. Choose **Expand view** for more room, then **Play**. Click native buttons or
   left-drag inside the game image. Right-click cancels a native tool. Hold Ctrl
   through a click or drag for the native alternative; release the mouse button
   inside the image before changing modifiers.
2. For a native text field, choose **Text** and compose the full replacement
   first. Choose **Apply text to field**, wait for its fresh capture, then click
   the visible native editbox with Ctrl released. Wait for the updated PNG and
   click native **OK** separately. Text replacement does not submit the native
   dialog automatically.
3. Use the arrow and +/− buttons, or focus the image and use arrow and +/− keys,
   to move the camera. Generic keyboard and mouse-wheel input are not forwarded
   to the game. **Inspect** remains a separate mode for examining a location
   without sending a native gameplay click.

Each completed input refreshes the PNG. **Input completed** is workflow status;
inspect the new image for the native success or error before acting again. It
does not by itself establish that construction succeeded.

If the viewer reports that the image expired before input was accepted, wait
for the fresh image and deliberately choose the next input. For an unconfirmed
outcome, use **Check status** or **Retry same request**, which retains the exact
original request. Do not repeat the gesture as a new request. Reloading or
disconnecting does not cancel admitted input; chat remains available while the
viewer holds camera input pending its outcome.

## Export the native catalog

Use the read-only private operator socket to export a catalog larger than one
RPC frame. Supply the world's recorded ID and epoch and choose a new output in
an existing owner-only directory:

```sh
mkdir -m 700 /tmp/catalog-review
python3 tools/export_engine_catalog.py \
  --socket /absolute/world/ipc/world.sock --world local-world --epoch initial \
  --output /tmp/catalog-review/native-catalog.json
```

The exporter fetches bounded descriptors, verifies their canonical BLAKE2b-256
identity, and checks that discovery stayed unchanged. It rejects an existing
output and publishes a verified file with mode `0600`. It performs no gameplay
or database operations and never replaces the backend's checked-in catalog;
review the exported artifact before updating that file separately.

## Seven-company overnight profile

`--profile overnight` creates a fresh 512 × 512 temperate world in 1950 with
seed `20261021` by default. It provisions a human visual session in company ID 0
with one native renderer (`r0`), plus six text-agent sessions in companies 1–6:
`passenger-road`, `passenger-rail`, `passenger-mixed`, `goods-road`, `goods-rail`,
and `goods-mixed`. Each credential is in its matching private
`credentials/<role>.token` file. The human can use the browser or the printed
matching native-client command; agent roles are ordinary API clients.

Use a new private state directory and unused ports with `--profile overnight`.
The launcher keeps its native world, backend and browser endpoint running until
you stop it; it does not run models or impose a model-work deadline. Give each
agent its own credential and start/stop instructions outside this launcher.
An initial save is accepted only by the default `mixed` profile.

## Four-company Luna profile

`--profile luna` selects a second fixed layout for isolated gameplay exploration:

| Credential file | Company ID | Mode | Assignment |
| --- | --- | --- | --- |
| `credentials/text-cooperator.token` | 0 | Text | Cooperate with company 1 |
| `credentials/visual-cooperator.token` | 1 | Visual | Cooperate with company 0 |
| `credentials/text-competitor.token` | 2 | Text | Compete with company 3 |
| `credentials/visual-competitor.token` | 3 | Visual | Compete with company 2 |

Use a new short private state directory and unused ports, separate from the
normal human world. The Luna profile generates a 128 × 128 temperate world
starting in 1950, with seed `20261018` by default. `--map-size` and `--seed` can
be chosen before a fresh run; do not choose a seed by searching for a prescribed
route. Native starting money, borrowing limits, construction costs and time
progression are unchanged. An initial save is not accepted for this profile.

```sh
python3 tools/mixed_world.py \
  --profile luna \
  --binary /absolute/path/to/openttd \
  --baseset /absolute/path/to/baseset \
  --state-dir /tmp/ottd-luna-world \
  --world luna-exploration \
  --native-port 19979 --http-port 18088
```

The only native clients are the two visual workers, `r1` and `r3`. Their first
join is temporarily open until each authenticated key is confirmed, then only
that worker's key is admitted. Both text companies remain closed to native
joins. The fixed profile is recorded in the manifest; a restart under a
different profile is rejected. Older manifests without a profile retain the
original `mixed` meaning. Omit `--profile` for the unchanged human/text/visual
layout and its matching native human-client preview command.

The launcher provisions companies, credentials and processes. It does not
spawn models, build infrastructure, choose player routes or operate a gameplay
collector. Once all four players are ready, the operator can send a common
start signal. The current exercise allows at most 90 minutes and 500 distinct
gameplay action request IDs per player, combining typed and optional UI
actions, including rejected submissions and UI-only actions. Reference lookups,
observations, captures, view controls and chat are recorded separately. Observe
progress at 30 and 60 minutes and take a final observation at
90 minutes if still running. Thirty minutes is not an automatic failure cutoff
or a reason to reset the world. Exact retries retain their original request ID
and do not create a new intent.

Give each `gpt-6-luna` player a fresh context (`fork_turns="none"`), only its
own credential path/artifact directory, the public schemas and the
[player brief](../tests/integration/luna_play/PLAYER.md) and
[public client instructions](../tests/integration/luna_play/API.md).
The [small helper](../tests/integration/luna_play/client.py) sends normal public
HTTP, preserves action IDs for exact retry, and saves genuine PNGs. It has no
world-state oracle or route planner. Keep private operator artifacts and other
credentials outside the player's authorized scope. Tool instructions alone
are procedural isolation, not an operating-system capability sandbox.

The cooperating pair should negotiate useful shared roads while each operates
its own earning service. The competing pair chooses and announces an
overlapping passenger market. Allow ordinary voluntary peer conversation;
do not give visual players an automatic structured-state relay. Require
meaningful world and company-direct exchanges, with receipt/history evidence
and separate checks that outsiders cannot read private threads.

Capture players use the same 103 typed gameplay actions as text players.
Their world perception comes from genuine PNGs, while they choose their own
typed construction, vehicle, order, finance and other command parameters.
Typed writes through `/v1/actions` do not require an `image_id` or a valid
screenshot lease. Normal command success, actual charged cost, native errors
and created references are permitted feedback; structured cargo, capacity,
map and financial reports remain unavailable to capture runs.

`POST /v1/control/reference` supplies one explicitly selected current actuator
reference and applicable opaque mutation token, without lists or attributes.
The catalog publishes its closed selector schema under
`conventions.control_reference`. The helper already supports it through generic
`post`; its normal `action` primitive works in both modes. Preserve complete
handles and version tokens, refresh them for new mutations when necessary,
and keep exact retries byte-for-byte unchanged. Capture players continue to
use PNGs for examining world state, plus ordinary public chat and participant
metadata.

Native UI input is optional and remains bound to the exact genuine PNG inspected.
`ui.apply` supports native `click`, `drag`, button-free `move`, `text`, and
two explicit UI-only `control` choices.
Click/drag may carry a boolean `ctrl`, scoped to that gesture. Text replaces
only an approved visible native query editbox; it accepts empty strings and
at most 4096 UTF-8 bytes, with the native field's own smaller limits and
character filter enforced without truncation. The player must capture a fresh
PNG and click native OK separately. Move/text forbid buttons, end coordinates
and Ctrl; arbitrary keys, clipboard input and Shift estimates remain denied.
Company joining and spectator moves remain blocked for bound renderers.

For `gesture: "control"`, supply `image_id`, `x,y`, and either
`control: "orders.go_to"` or `control: "road_stop.orientation"`. The anchor
may be any visible pixel in the intended unshaded native window. Go To selects
the ordinary mode in an owned primary vehicle's Orders window and stays armed
when selected again. Road-stop orientation additionally requires an explicit
`orientation` and an already active bus/truck stop picker. Its native ordinal
mapping is `terminal_ne` (0), `terminal_se` (1), `terminal_sw` (2),
`terminal_nw` (3), `drive_through_ne_sw` (4, X axis), and
`drive_through_se_nw` (5, Y axis); northeast is upper right on screen. Terminal
choices are unavailable for trams and drive-through-only stop types.

For example, parameters to `ui.apply` can be
`{"image_id":"INSPECTED_IMAGE_ID","gesture":"control","control":"orders.go_to","x":620,"y":310}`.
Neither control accepts buttons, Ctrl, text or end coordinates. It cannot post
a native gameplay command or choose a destination, orientation or site for the
player. After selection, the next genuine PNG supplies feedback and the player
chooses subsequent map gestures. The existing lease, durable action and exact
retry rules apply. See the [control contract](../docs/capture-controls.md) and
the [public client instructions](../tests/integration/luna_play/API.md).

The existing `open_window` view control also accepts `road_construction`,
`rail_construction`, `water_construction`, `airport_construction`,
`landscaping`, and `trees`. These open ordinary native toolbars. Road/rail use
the current available GUI type or the normal GUI default. They do not choose
a site, supply a target or issue a construction command; the player still
selects options and targets. These optional UI windows do not constrain typed
action parameters or require players to use pixel gestures. Existing
company, finances, stations and vehicle-list windows remain available.

New Luna worlds set the native lag allowance to 2000 ticks and request
`OPENTTD_RENDERER_PARK_MS=40000` for their rendering workers. The ordinary
mixed profile keeps the eight-second renderer default. Forty seconds is a
ceiling: the worker reduces its effective budget for already accumulated lag
and leaves a native catch-up margin. Its private `park_ttl_ms` is a budget
from the original capture, not a fresh duration for each image request.
Receive-buffer pressure, disconnection or other native invalidation may end
the lease earlier. Repeated capture of the same parked frame and PNG GETs do
not reset its deadline. The authority continues simulating while the replica
is parked. Players must handle expiry and inspect a genuinely fresh PNG
before choosing another image-bound gesture. Typed writes and control-reference
calls do not require a screenshot lease.

Earlier Luna trials used a stricter GUI-only control protocol. Preserve their
recorded failures, runtime attribution and continuation evidence in the
[validation ledger](../docs/integration_validation.md); they do not test the
current shared-actuator contract. Optional native UI smoke proofs likewise
remain technical tests, separate from autonomous company acceptance.

An independent, read-only audit can establish normal costs, loading, shared
road use or competing catchments, actual company transport income, and further
income after a material service change. Company quarterly transport income
plus retained history supports a bounded fresh-run revenue baseline. Vehicle
profit can include virtual transfer credit and is supporting evidence rather
than the revenue gate. Track loans, gifts, capital costs and cash separately;
operating income plus operating expenses is not a capex-inclusive profit.
Keep the audit out of player prompts and never use it to repair a route for a
player. Report unfinished strategy, documented missing interface capability
and reproducible runtime failures separately.

`browser_client.py` serves an isolated Emscripten public build and a bounded,
fixed loopback WebSocket-to-native relay. See [the Emscripten instructions](../os/emscripten/README.md#fixed-loopback-native-relay)
for startup, port options and isolated tests.
