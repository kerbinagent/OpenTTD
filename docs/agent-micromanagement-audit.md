# Agent micromanagement audit

This is a recommendation record, **not a list of shipped features**. It reviews
master `58a7244e2b` against the six overnight Luna players' original public
request/receipt logs, before the official cutoff
`2026-10-18T11:59:50.116372Z`. The audit and independent architecture review were
read-only; they made no live calls or gameplay changes.

The existing [automatic depot direction](depot-placement.md) resolves facing at
the player's selected site. It deliberately builds no reciprocal road bits or
rail turns and proves no complete route. The next useful conveniences similarly
remove parameter arithmetic and opaque-token transcription. Players retain
decisions about markets, sites, corridors, modes, cargo, fleet size, financing,
loading, maintenance timing and whether to act.

## Recommended scope

| Status | Recommendation | Smallest sound shape | Evidence / current source |
| --- | --- | --- | --- |
| Cheap next change | Physical-edge geometry helpers | Pure shared functions turn two chosen rail boundary edges into an existing track name, or chosen road edges into an existing mask. Return an ordinary action; submit through the existing journal. | [E1](#e1-geometry-and-reciprocal-road-bits); [native edge table](../src/table/track_data.h#L54), [accepted track names](../src/integration/actions_common.cpp#L105) |
| Cheap next change, narrowly scoped | New-intent order-token preparation | Accept full selected vehicle references and actor-chosen order parameters; use the existing actuator resolver, require the same incarnation, copy tokens unchanged, then save one canonical new intent. Keep submission/retry separate. | [E2](#e2-order-token-transcription); [resolver](../src/integration/actions.cpp#L1066), [SavedActions](../backend/player_actions.py), [order checks](../src/integration/actions_orders.cpp#L94) |
| Cheap next change | Precise contract diagnostics | Reuse generated rules; report one bounded field path and finite reason. Publish conflicting-field rules and public-effective limits. Reject incompatible choices instead of silently correcting them. | [E3](#e3-contract-mistakes-and-effective-limits); [validator](../backend/catalog.py#L110), [public observation cap](../backend/service.py#L594) |
| Detailed native design next | Automatic road-stop orientation | Opt-in `road.stop.build direction:"auto"`; preserve the selected footprint, layout, type, station join and spec. Choose a unique supported legal physical facing/axis; reject none or ambiguity. | [E4](#e4-stop-orientation); [construction family](../src/integration/actions_construction.cpp#L503), [depot precedent](depot-placement.md) |
| Detailed native design next | Declarative depot diversion; optional running-state companion | One native execute-time operation for `service`, `halt` or `cancel`; optionally a separate desired stopped/running flag. Preserve existing toggles. Matching desired state is a successful no-op through the ordinary native command path. | [E5](#e5-contextual-depot-and-running-controls); [native depot behavior](../src/vehicle.cpp#L2603), [start/stop toggle](../src/vehicle_cmd.cpp#L585) |
| Defer behind explicit geometry helpers | Facility-aware entrance connector | Only if needed: one selected facility entrance, one compatible neighboring road and one ordinary command, with frozen dependencies. No GUI callback chain. | [E1](#e1-geometry-and-reciprocal-road-bits); [GUI road connection](../src/road_gui.cpp#L170), [road-access distinction](capture-controls.md#road-access) |
| Defer as a small, separately reviewed extension | Station/waypoint reference destinations | Explicit `destination_reference`, mutually exclusive with existing tile `destination`; validate kind/incarnation directly. Leave scalar destinations as tile indices. Defer depot/hangar unions. | [E6](#e6-reference-destination-composition); [current destination parser](../src/integration/actions_orders.cpp#L143) |
| Small follow-up | Read/test admission feedback | Finite rate-limit categories and bounded retry hints; optionally pace and retry the identical read/estimate body within a deadline. Never turn an estimate into a build. | [E3](#e3-contract-mistakes-and-effective-limits); [estimate gate](../src/integration/actions.cpp#L1298) |
| Separate proposal or defer | Route reachability, bundles and orchestration | Measure a need before adding path queries or resumable multiaction execution. Do not add implicit corridors, repair loops, consist builders, sell-after-depot workflows or business notebooks. | Existing [one-command invocation](../src/integration/actions_internal.h#L112), [action estimates](action-estimates.md), [player guidance](../tests/integration/luna_play/TEXT_PLAYER.md) |

Geometry helpers need only the physical boundary names `NE`, `SE`, `SW`, `NW`;
avoid multiple heading/coordinate dialects. Their concrete output belongs in the
saved request, so exact retries do not recalculate defaults. A local connector
calculation is not evidence that the road exists or the route works.

Token preparation must happen only for a new intent. A version change afterward
remains an authoritative rejection. A saved retry loads its original references
and tokens with **zero resolver calls**. No new server refresh-and-execute
endpoint, binding language or second action ledger is needed.

## Native follow-up constraints

For automatic stops, through directions `0/2` describe one physical axis and
`1/3` the other; bays have four facings. Ordinary legal placement on blank land
does not itself identify useful orientation. Require bounded physical support
and native placement success, including footprint/spec/type constraints. Freeze
the selected direction and relevant dependencies before queueing; reject changed
geometry instead of choosing again. Keep `station.site` coherent with the shared
build schema. Build no connector implicitly and return only ordinary actuator
feedback to capture players.

Declarative depot controls require a real native command leaf. Existing
`DontCancel` is insufficient: an already matching diversion returns an error,
and no cancel-only operation exists. A client read-then-toggle races with other
players. An HTTP-only successful no-op would complicate command completion,
estimates, replay and after-action image causality. Define scheduled-depot-row
cancellation explicitly, since native cancellation can skip that row. Never
silently start a stopped vehicle. Desired running state means the native stopped
flag, not physical motion or guaranteed departure; actual transitions retain
ownership, crash, aircraft and NewGRF constraints.

Both native follow-ups need a concrete independent design review before full
implementation, followed by correctness review. Tests should cover unique,
absent and ambiguous geometry; changed queued dependencies; estimate purity;
desired-state matrices; already-depot/airborne/crashed cases; completed-ID replay
after an intervening opposite operation; and after-action capture causality.

## Representative transcript evidence

These are preserved **local records**, not committed fixtures. Paths are under
`/tmp/openttd-overnight-512-players/`; line numbers refer to original JSONL rows.
Every timestamp below is UTC on **2026-10-18**, derived from the row's Unix time.
Successful construction or command completion alone does not establish a paying
route, and one `cannot_reach_station` flag does not establish permanent failure.

### E1. Geometry and reciprocal road bits

Passenger-rail submitted five track pieces at its chosen junction `(447,463)`.
For example, request `npr-hondbourne-branch-right-20261018-0504` at
[line 1153](/tmp/openttd-overnight-512-players/passenger-rail/public-io.jsonl:1153),
`05:02:28.945921Z`, completed for 75 at line 1168. This supports removing track-name
arithmetic; it does not prove all five pieces were needed.

Passenger-road request `8c94ca449af840ddb43156ba074be3b3` at
[line 579](/tmp/openttd-overnight-512-players/passenger-road/public-io.jsonl:579),
`04:53:44.886310Z`, repaired `(124,139)` to `(124,140)` for 71 at line 592.
Observations at lines 464 and 594 show the latter tile's road bits changing from
14 to 15: a concrete missing reciprocal bit.

### E2. Order-token transcription

Passenger-mixed request `pm-ked-bus7-copy-orders` at
[line 4185](/tmp/openttd-overnight-512-players/passenger-mixed/public-io.jsonl:4185),
`07:42:46.229362Z`, supplied a 67-character destination `order_version`; line 4186
returned HTTP400. Its second attempt at line 4193 had the same length error.
`pm-ked-bus7-copy-orders-v3`, line 4199 at `07:43:42.296775Z`, preserved the final
hex digit and completed at line 4212, tick430977. Of nine logged copy submissions,
two had this truncation and one omitted both required tokens. Existing canonical
journaling preserves a supplied body; it does not assemble these tokens.

### E3. Contract mistakes and effective limits

Goods-mixed request `e63f4684c19741a29245646f05344bea` at
[line 2595](/tmp/openttd-overnight-512-players/goods-mixed/public-io.jsonl:2595),
`05:02:00.826204Z`, supplied both `depot_action:"service"` and legacy `service:true`
on a nearest-depot order. Line 2602 rejected `invalid_parameters`; its explicit
depot variant at line 2651 repeated the conflict. Native construction rejects
these duplicate policy forms before destination resolution. This is not evidence
of a missing depot-order action.

Goods-rail's `vehicles limit:256` at
[line 15](/tmp/openttd-overnight-512-players/goods-rail/public-io.jsonl:15),
`04:33:43.399529Z`, returned HTTP400 at line 16. Nineteen adjacent request/error
pairs across five actors used limits 200 or 256. Native schemas allow 256 for
these queries, while the documented HTTP cap is 100. Expose that public-effective
limit without changing the native catalog identity or indiscriminately raising
response limits. Separately, Goods-mixed's second estimate at line 123,
`04:34:46.974033Z`, received 429 only about 40ms after the preceding estimate;
bounded pacing addresses that refusal without changing the proposal.

### E4. Stop orientation

Goods-mixed request `7a937725241c4c5387ba9f5404fd18cf` at
[line 1845](/tmp/openttd-overnight-512-players/goods-mixed/public-io.jsonl:1845),
`04:47:28.035888Z`, selected through-stop `(95,90)`, direction0. Line 1852 rejected
`road facing in the wrong direction`. The selected tile had road bits5 at line
1878. Request `e5bd45375511447a9054f042d2c6fcc9` at line 1903,
`04:47:39.324305Z`, selected direction1 and completed for 150 at line 1916.

### E5. Contextual depot and running controls

Goods-rail request `goods-rail-cancel-unreachable-depot-20261018-0922` at
[line 8707](/tmp/openttd-overnight-512-players/goods-rail/public-io.jsonl:8707),
`09:23:04.804741Z`, sent `service:false` after an active service diversion.
It completed at line 8722, but line 8724 still reported depot execution and line
8730 reported the train stopped inside the depot. Native semantics change service
to halt; the request name indicates cancellation intent. A second cancellation-
named request at line 7783, `08:42:20.602018Z`, likewise led to a depot stop.

**C6 erratum:** Goods-mixed request `e75e87b60c7d4f1ea2f05f2c1eebc6a2` at
[line 435](/tmp/openttd-overnight-512-players/goods-mixed/public-io.jsonl:435),
`04:37:06.679698Z`, tried unsupported `stopped:false` on `vehicle.start_stop`.
Line 436 definitively returned HTTP400 before admission. Later `Request not
found` does **not** make this an uncertain execution. The corrected new request
at line 437 omitted the field and was accepted at line 438.

### E6. Reference-destination composition

Goods-rail's selected station resolver call at
[line 10159](/tmp/openttd-overnight-512-players/goods-rail/public-io.jsonl:10159),
`10:19:10.971731Z`, returned a full station49/gen79 reference. Its order estimate at
line 10163, `10:19:22.004021Z`, placed that reference in tile `destination` and
received HTTP400 at line 10164. This is a real composition gap, though existing
tile addressing works. There is no action request ID for this estimate.

**Passenger-mixed erratum:** `pm-ked-order-a`,
[line 131](/tmp/openttd-overnight-512-players/passenger-mixed/public-io.jsonl:131),
`04:35:36.285649Z`, used the correct tile158829, confirmed at line 126. It also
requested `stop_location:"middle"` on a bus; [native validation](../src/order_cmd.cpp#L679)
rejects middle/near for non-trains. The successful line145 request both used
equivalent coordinates and omitted that field. Do not attribute the first failure
to scalar tile addressing. The later scalar0 at line139 was a genuine
station-pool-ID versus tile mistake.

## Existing capabilities and evidence limits

Seven logged wagon-attachment submissions and seven clones completed; no
`vehicle.refit` submission appears before cutoff. Seven sale failures report the
existing stopped-inside-depot precondition. These records do not justify new
consist, refit or deferred-sale orchestration. Existing `orders`, actuator
references, canonical requests, chat cursors/events and
[text-player guidance](../tests/integration/luna_play/TEXT_PLAYER.md) already
address discovery, exact retries and incremental inbox reading.

All proposed conveniences retain ordinary ownership and incarnation checks.
Capture players receive pixels and permitted actuator feedback; helpers must not
secretly fetch text world observations. Error details should contain bounded
schema facts, not native exceptions, input dumps or unrequested game attributes.
HTTP request counts are I/O volume, not model turns, thinking time or token cost.

Local supporting reports: `/tmp/ottd-micro-construction-audit.md`,
`/tmp/ottd-micro-orders-audit.md`, `/tmp/ottd-micro-protocol-audit.md`, and the
independent `/tmp/ottd-micro-architecture-review.md`. This record preserves their
recommendations; future implementation needs its own validation and must not
redefine the original overnight result.
