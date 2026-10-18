# Actions and image-only perception for capture players

Text and capture players share the same 103 typed gameplay actions through
`POST /v1/actions`. A capture run keeps `mode: "visual"` on the wire. Its world
perception comes from genuine PNGs; it can choose and submit typed construction,
vehicle, order, finance and other ordinary company commands without GUI clicks
or an `image_id`. The normal company permissions, target incarnations, mutation
versions, native costs, publication checks and durable request IDs apply to
both modes.

Typed command receipts may report success, actual charged cost, a native
gameplay error and created object references. They do not expose cargo,
capacities, station contents, map attributes, financial reports or other
structured world observations. Inspect the command outcome; accepted and queued
acknowledgements do not establish success. Structured world
readers remain text-only. Public chat and participant metadata remain available.

## Road access

These construction rules apply to typed actions in both text and capture modes.
Compass directions refer to the screen diagonals. A road depot or terminal/bay
stop's `direction` selects its entrance neighbor, rather than the position of a
nearby road or the center of its sprite:

| `direction` | Entrance neighbor relative to the structure | Reciprocal road bit needed on that neighbor |
| --- | --- | --- |
| `0` / NE | `x−1, y` | SW / `2` |
| `1` / SE | `x, y+1` | NW / `1` |
| `2` / SW | `x+1, y` | NE / `8` |
| `3` / NW | `x, y−1` | SE / `4` |

Road-bit masks are NW=`1`, SW=`2`, SE=`4`, NE=`8`; combine needed bits by adding
their masks. For an illustrative depot at `(10,20)` with direction `0`, its
entrance is at `(9,20)`, where the reciprocal bit is `2`. These coordinates are
an example, not a suitable site or a promised feasible construction. Matching
bits are necessary access geometry; native slope, road-type, ownership and other
rules still apply, and a complete traversable route must exist beyond them.

With `drive_through: true`, a road stop's typed `direction` instead selects its
axis: `0` or `2` selects X, along changing x coordinates, with road bits `10`;
`1` or `3` selects Y, along changing y coordinates, with road bits `5`. A
through stop must match its straight road alignment. Typed directions remain
`0..3`; the optional native UI picker's through-stop ordinals `4/5` are different
values used only by that picker.

Typed `road.depot.build` and `road.stop.build` construct the structure without
running the GUI's automatic neighbor-road connection. The normal native GUI
can submit a separate follow-up command to add the missing reciprocal bit when
the entrance neighbor is already a normal road tile with road/tram bits. That
callback does not choose the correct facing or build access across empty land.
The optional captured UI route blocks gameplay-producing follow-up callbacks.
Use `road.pieces.build` for an exact needed mask, or compose other ordinary road
commands. `road.build` derives its line axis from its endpoint coordinates;
identical endpoints select X, so a same-tile line build does not select an
arbitrary entrance connector. Each intended construction step has its own
request identity, native cost and outcome; retry an uncertain step with its
original saved request. Multiple steps can partially complete.

A successful construction receipt establishes that the facility was built.
A vehicle heading and positive displayed speed do not establish that it has
reached the road or a destination: it can turn around within an isolated depot
tile. Occupying a depot tile also differs from the native `in_depot` state.
Verify actual travel and arrival/loading or delivery and transport income.
Text players can inspect direction and road bits through existing `map.region`
observations; capture players inspect genuine PNGs and native facility/tile
windows, following the same image-only perception contract.

## One selected actuator reference

`POST /v1/control/reference` resolves one explicit target to the current
reference needed for an action. Its request does not need an image ID. Discovery
publishes the closed selector schema and positive result fields under
`conventions.control_reference` in the native catalog. The public player helper
can call it with its existing generic `post` primitive.

Send the selector directly as the request body. HTTP 200 returns the result
object without a wrapper. Invalid shapes return HTTP 400; missing, unauthorized,
ambiguous or admission-gated targets return HTTP 409 with
`control_reference_unavailable`; transport/catalog failures return HTTP 503.

| Target kind | Explicit selector | Actuator metadata |
| --- | --- | --- |
| `vehicle` | Native `id`, or owned primary `vehicle_type` and displayed `unit_number` | `reference`; owned primary vehicles also return `order_version` |
| `station`, `waypoint`, `depot` | Native `id`, or one `tile` | `reference` |
| `engine` | Native `id`, or `vehicle_type` and exact displayed `name` | `engine_id` |
| `group`, `town`, `sign`, `company` | Native `id` | `reference` |
| `story_page`, `story_element` | Native `id` | `reference`, `page` (the containing page reference), and `page_version` |
| `question` | One unambiguous live company question `id` | `reference` |

Reference vehicle types are `rail`, `road`, `ship`, and `aircraft`. A displayed
unit number is not a native pool ID. The resolver performs no fuzzy matching,
nearest-target selection, listing or attribute lookup. Do not enumerate selectors
as a substitute for PNG inspection. Engine resolution
identifies a loaded model; the subsequent native command decides whether it is
currently available for that operation. Duplicate question popups can instead
be selected through optional native UI input.

References follow the target's normal actuator permissions: groups and depots
are owned, signs must be editable, and story targets must be interactive for
the actor or global. Direct vehicle IDs can identify owned consist parts or
another company's primary vehicle for ordinary story selection; they return
no part/foreign order token or permission to mutate another company's assets.
Unit-number/name scans and order/page token computation share the process-wide
one-second admission interval used by expensive text/refit operations. An
admission-gated lookup can be repeated after waiting more than one second; it
never changes a saved action.

Entity references retain `{id,generation,engine_instance_id}`. Copy them and
opaque order/page tokens unchanged into action parameters. Refresh a token
before a new mutation when its order list, sharing cohort or story page has
changed. A new reference deliberately selects the current target; resolving an
ID after restart does not prove continuity with its previous occupant. Exact
retries retain the original body, references and tokens and return the recorded
request outcome. The helper never refreshes or rewrites a saved action.

Successful creation commands return their documented references where
applicable, including vehicles, groups, signs, towns, stations/waypoints and
depots. Station/depot acknowledgements refer to the action-selected site; they
do not enumerate nearby objects. No world/report attributes accompany a
reference response.

## Optional native UI input

`ui.apply` remains available for image-bound native mouse, text and explicit
control input. Its image lease applies to this route and normal view controls;
typed writes and single-target reference calls have no screenshot prerequisite.
UI receipts retain workflow-only feedback, with the next genuine PNG showing
the UI result.

The following two UI controls select ordinary native tool states. They do not
return object IDs, map coordinates, tool state or structured game values.

Every request uses the normal action envelope and a fresh `request_id`. Its
`parameters` contain the exact inspected `image_id`, `gesture: "control"`, the
named `control`, and integer image pixels `x,y`. The pixel must hit a visible
point in the intended native window. It identifies that window; it need not hit
the small button being selected. The window must be unshaded and the selected
native option must be visible and enabled. An overlapping window is not a valid
anchor for the window underneath it.

```json
{"request_id":"arm-orders-1","action":{"name":"ui.apply","parameters":{
  "image_id":"INSPECTED_IMAGE_ID","gesture":"control",
  "control":"orders.go_to","x":620,"y":310
}}}
```

`orders.go_to` selects ordinary **Go To** in the anchored Orders window of an
owned primary vehicle. Selecting it again leaves it armed. It does not select
an order row, choose a destination or insert an order. Inspect the next PNG and
choose the destination through a subsequent image-bound map gesture. The
native current row selection, order rules and error feedback still apply.

```json
{"request_id":"stop-orientation-1","action":{"name":"ui.apply","parameters":{
  "image_id":"INSPECTED_IMAGE_ID","gesture":"control",
  "control":"road_stop.orientation","orientation":"drive_through_ne_sw",
  "x":620,"y":310
}}}
```

`road_stop.orientation` selects an explicit option in the anchored ordinary bus
or truck stop picker. Its matching road/tram toolbar tool must already be
active. The six names map directly to the native choices:

| Value | Native picker ordinal | Meaning |
| --- | ---: | --- |
| `terminal_ne` | 0 | Bay entrance facing northeast, upper right on screen |
| `terminal_se` | 1 | Bay entrance facing southeast, lower right on screen |
| `terminal_sw` | 2 | Bay entrance facing southwest, lower left on screen |
| `terminal_nw` | 3 | Bay entrance facing northwest, upper left on screen |
| `drive_through_ne_sw` | 4 | Through stop along northeast–southwest, native X axis |
| `drive_through_se_nw` | 5 | Through stop along southeast–northwest, native Y axis |

Trams and stop types that offer only drive-through choices reject terminal
choices. The control does not pick an orientation that fits a road, inspect a
construction site, change the selected stop type, build anything or retry a
placement. The player chooses those options and targets from images.

The two request shapes are closed: only `road_stop.orientation` accepts
`orientation`. Neither accepts `button`, `ctrl`, `text`, end coordinates,
object IDs, native widget numbers or arbitrary control names.

Both named UI controls use the existing screenshot lease, one-shot renderer capture,
durable action ledger and exact-request retry lifecycle. They permit only an
empty command bundle; an attempted native gameplay command is rejected before
authority publication. Expired images require a newly captured and inspected
image. An exact retry returns the recorded request outcome and never repeats
the control. A completed receipt reports input handling; the next genuine PNG
provides the UI feedback.

The native UI implementation calls the existing non-toggling Orders selection
method or road-stop picker handler. These two controls address ordinary widget
ambiguity; capture players can use shared typed actuators for the same gameplay
outcomes instead.

Earlier GUI-only Luna exercises and native control smoke tests retain their
original runtime and protocol attribution in the [validation ledger](integration_validation.md).
They do not establish autonomous business success under this shared-actuator
contract. Keep those historical outcomes separate from fresh text/capture
company acceptance.
