# Human company capability matrix

This records the implemented source contract inspected on 2026-10-18, not a
completed gameplay-parity gate. The reference is normal multiplayer company
play, including actions enabled by ordinary game settings. Administration,
cheats, scenario editing, script authoring and local preferences are excluded.
The registry and family helpers implement the outcomes below through normal
native commands; a matching final build, catalog export and runtime acceptance
remain separate requirements. A running frozen world may expose an earlier
catalog: use that world's public capabilities, not this document, as its API.

The [central registry](../src/integration/actions.cpp) contains **103 typed
actions**, plus `ui.apply`, and **60 text queries**. Its action entries admit
**97 distinct native commands**. The [native enum](../src/command_type.h) has
146 real commands, excluding `End`: 97 admitted, six composable conveniences
and 43 excluded/internal commands. These disjoint counts describe coverage,
not a percentage of semantic parity or a runtime success rate.

## Implemented gameplay outcomes

Text and capture companies share the 103 typed actions, schemas and native
company/handle/version safeguards. Capture companies examine world state through
genuine PNGs; structured world queries remain unavailable. Typed writes need no
screenshot. Ordinary command feedback includes success, charged cost, native
errors and created references, without report or refit-capacity data.
`ui.apply` is an optional image-bound path through ordinary native widgets.
This corrected shared-actuator contract has matching native and public HTTP
validation; a frozen runtime must advertise it before players can rely on it.
The native allowlist is derived from the central action registry rather than a
second family-maintained list. Admission is permission to attempt a command;
normal availability, ownership, funds and construction rules still decide it.

| Human outcome | Implemented actions and choices | Permissions and semantic limits |
| --- | --- | --- |
| Road/tram construction, conversion, stops, depots and waypoints | `road.build`, `road.pieces.build`, `road.remove`, `road.convert`, `road.depot.build`, `road.stop.build/remove`, `road.waypoint.build/remove`; explicit road bits/direction toggles, road type, stop dimensions and catalogued class/spec | Native road ownership and drive-through orientation apply. A direction toggle is not a desired-state setter. Waypoint removal can retain its road. Typed depot/stop commands require explicit access-road composition; see [road access](capture-controls.md#road-access). |
| Railway construction, stations, waypoints and signals | `rail.build/remove`, `rail.build_line/remove_line`, `rail.convert`, `rail.depot.build`, `rail.station.build/remove`, `rail.waypoint.build/remove`, `rail.signal.build/remove`; track/type, station dimensions/join, class/spec, signal type/variant/direction/conversion | Native selected-spec availability, station spread, ownership and signal rules apply. Larger geometry and signal-density work can compose smaller operations. |
| Water/air infrastructure, bridges and tunnels | `water.depot.build`, `water.dock.build`, `water.buoy.build`, `water.lock.build`, `water.canal.build`, `airport.build/open_close`, `bridge.build`, `tunnel.build`; airport layout, bridge and transport/type choices | Native terrain and ownership checks remain final. Ordinary demolition uses `terrain.clear`. Sea/river creation remains editor-only. |
| Terrain, trees and normal objects | `terrain.raise/lower/level/clear/plant_tree`, `object.build`; corners, tree species, object type/view | HQ relocation and purchased land use normal object types. Object footprints include native linked effects and old HQ dependencies. Scenario-only objects and direct statue construction are unavailable; statues use `town.action`. |
| Houses, towns and industry funding | `house.build`, `town.found`, `town.action`, `industry.fund`; house protection/replacement, permitted town size/city/layout/name, all eight town actions, fixed-site funding versus prospecting | Ordinary house/town/industry settings apply. No deity funding mode or public RNG seed. A paid prospect can create nothing. Fixed-industry platform validation can inspect distant geometry/infrastructure; its conservative staleness and maximum-map latency need runtime validation. |
| Names, labels and signs | `station.rename`, `depot.rename`, `waypoint.rename`, `station.label.move`, `waypoint.label.move`, `sign.place/rename/delete/move` | Station/depot mutations retain native ownership rules; permitted neutral label targets and editable other-company signs follow native rules. Deity signs remain protected. |
| Vehicle lifecycle and train controls | `vehicle.build/clone/sell/refit/depot/reverse/start_stop/rename`, `train.wagon.move`, `train.unit.flip`, `train.force_proceed`, `vehicle.autoreplace`, `vehicle.service_interval.set` | Refit subtype and selected part count, clone sharing, inherited/custom service intervals and native replacement are exposed. Start/stop and force-proceed retain native toggle semantics. Native depot/consist/engine compatibility applies. |
| Orders and conditional routing | `order.insert/delete/skip/move/copy/unshare/modify/refit`, `order.conditional.insert`; station/waypoint/fixed or nearest depot, loading/unloading, via/non-stop, stop location, service/halt/unbunch, refit and native conditional variables/comparators | Mutations require an observed `order_version`; copying also requires the source version. Positions include implicit rows. Cohort/list changes invalidate stale intent. `order.unshare` defaults to historical `clear`; explicit `keep` copies from a distinct shared peer. |
| Timetables and shared departure behavior | `timetable.set/start/autofill/reset_lateness`; wait/travel duration, speed cap, preserve-wait and shared options | Zero clears supported values. Shared starts distribute departures and shared lateness reset preserves native relative alignment; these are not independent identical per-vehicle resets. Disabling precedes changing active autofill preserve mode. Native units and restrictions are documented in the [orders contract](orders-family-integration.md). |
| Groups and replacement policy | `group.create/delete/rename/parent.set/vehicle.move/protection.set/wagon_removal.set/livery.set`, `autoreplace.set/remove` | Typed all/default/group scopes, owned group handles, recursive flags, inherited liveries and include-shared membership are explicit. Relevant descendants, cohort members and replacement rules are fenced. Raw native vehicle-list selector tuples are not public inputs. |
| Company finance, appearance and settings | `company.loan.increase/decrease`, `company.give_money`, `company.rename`, `company.president.rename`, `company.livery.set`, `company.manager_face.style.set/component.set`, `company.setting.set`, `company.buy` | Loans support amount/interval/max. Face components come from catalogued styles; livery inheritance is explicit. Nine ordinary renewal/service settings are allowlisted. Offered bankrupt-company purchase follows native checks; multiplayer hostile takeover and arbitrary settings are unavailable. |
| Engine offers and visibility | `engine.preview.accept`, `catalog.engine.visibility.set` | Requires a current native preview offer or valid own-company model choice. Hidden state is saved company state, not a replacement for engine availability. |
| Own-company joining permissions | `company.join_policy.set`, `company.participant.authorize` | Policy is any/listed. Authorization derives a key from a live connected participant handle and rechecks the connection; no arbitrary offline key or other-company mutation is accepted. Internal renderer workers are removed from the public roster. |
| Script questions and story buttons | `goal.question.answer`, `story.button.press/select_tile/select_vehicle` | Company-visible question issuance handles and page versions fence text actions. Client-private questions stay in genuine native windows. Visual answers witness the exact clicked popup/button, including duplicate native IDs; they do not infer GUI popup order from the text inbox. Script authoring remains excluded. |

Implementation is split into explicit [orders](../src/integration/actions_orders.cpp),
[management](../src/integration/actions_management.cpp),
[construction](../src/integration/actions_construction.cpp),
[company](../src/integration/actions_company.cpp),
[access](../src/integration/actions_access.cpp) and
[story](../src/integration/actions_story.cpp) helpers. Shared readers and family
entry points are in [actions_internal.h](../src/integration/actions_internal.h).
There is no public raw-command endpoint, generic setting operation, automatic
route planner or general batch scheduler.

## Decision-making observations

All 60 queries are text-only. They return bounded positive DTOs and native
choices rather than packed structures or a second rendering tree. Ordinary
public windows can expose another company's station/vehicle/finance details;
mutation ownership remains stricter. Owned fleet lists, refit operations,
replacement state, own joining policy and private script content retain their
specific permission checks.

| Information available | Registered queries | Scope and paging |
| --- | --- | --- |
| World, terrain and proposed station site | `world`, `map.region`, `land.info`, `station.site` | Terrain/ownership, native land information, acceptance and catchment choices; region cursors and response budgets bound each page. Site observations do not construct a station or guarantee later success. |
| Available transport choices | `catalog.engines`, `engine.purchase_info`, `catalog.cargo`, `cargo.payment_rates`, `catalog.bridges`, `catalog.airports`, `catalog.railtypes`, `catalog.roadtypes`, `catalog.station_specs`, `catalog.roadstop_specs` | Available native models/specs, selected purchase performance, the native 20-point cargo graph and current road/rail bridge choices. [Units and bounds](decision-information.md) are explicit; loaded NewGRFs and settings remain authoritative. |
| Own fleet and public vehicle details | `vehicles`, `vehicle.details`, `vehicle.refit_subtypes` | Boot-fenced primary/part handles, native operational status, cargo, profit, reliability, service and consist state. Detailed parts use a consist version; refit subtype discovery validates owned depot/refit context. |
| Order lists, cohorts and timetables | `orders`, `timetable` | Physical order/cohort pages, typed policies, conditional/refit/timing detail and versions. Default pages are at most 64 rows/members, or 16 with `include_live_detail`; complete-cohort inspection has a published native bound. |
| Stations, facilities and cargo routing | `stations`, `depots`, `waypoints`, `station.details`, `station.cargo.flows`, `station.cargo.waiting`, `station.links` | Own facility discovery plus ordinary public station/cargo information, rating/history, flows, waiting aggregates and links. Continuations and aggregate versions bound changing lists; no omniscient demand forecast. |
| Company finances, appearance and access | `company`, `company.report`, `company.valuation`, `company.appearance`, `company.access`, `catalog.manager_faces`, `engine.preview_offers` | Native financial periods/categories, value and acquisition state, available styles/components, own access policy and preview offers. Loan proceeds, transfers, capex, transport gross and vehicle profit remain different quantities. |
| Groups and renewal rules | `groups`, `group.stats`, `autoreplace` | Authenticated company's hierarchy, scope statistics, liveries and persistent engine replacement rules. |
| Construction choices and editable signs | `catalog.objects`, `catalog.houses`, `catalog.industries`, `catalog.trees`, `signs` | Normal selectable specs, dimensions/views/settings and editable sign handles; entity and spec scans have explicit continuations. |
| Towns, industries and subsidies | `towns`, `town.detail`, `town.authority`, `industries`, `industry.details`, `industry.monthly_stats`, `subsidies` | Public population/growth/cargo, local authority ratings/actions, industry acceptance/production/history and subsidy information. Readers do not execute industry callbacks as an observation shortcut. |
| Goals, questions, stories and leagues | `goal.questions`, `goal.question`, `story.pages`, `story.elements`, `story.element`, `story.vehicle_candidates`, `goals`, `league.performance`, `league.tables`, `league.table.entries` | Own/global visibility, complete text continuations and current page/issuance versions. Text question inbox ordering is explicit and does not mirror independent client popup stacks. |
| Custom GameScript news | `news.custom` | Recipient-scoped current-boot metadata, incarnation-fenced cursors and explicit retention gaps. Selected bodies use live native rendering through `display.text`; [news bounds and semantics](custom-news.md) are explicit. |
| Long human-visible display text | `display.text` | Finite approved text kinds/targets, UTF-8 byte continuation and content-version checks; no arbitrary StringID, format arguments or hidden state. |

Both modes can also read shared pause metadata through `GET /v1/world/status`
and receive `world.status` events. Native command permissions apply while paused;
the saved policy cannot change during multiplayer. Host pause controls remain
outside normal company permissions. See [pause behavior](pause-state.md) for
revision/fence handling and queued-command rejection, and [land information](land-info.md)
for query-local callback writes, native clearing checks and shared admission.

Handles are strict `{id,generation,engine_instance_id}` values from observations,
creation acknowledgements or the finite `control.reference` operation. The
public `POST /v1/control/reference` selects one explicitly chosen current target
and returns its handle and applicable opaque order/page version. Its closed
schema is published under catalog `conventions.control_reference`; it returns
no target attributes, collections or search results. Refresh selects the current
occupant deliberately and never rewrites an old action or promises identity
across engine restarts.
They are checked again at publication; the backend does not fill in a current
boot on behalf of a stale client. Exact historical terminal retries are resolved
before fresh-schema validation, preserving their original canonical receipt.
Page/version changes require fresh metadata. The current
[shared bounds](../src/integration/actions_limits.h) allow at most 10,000 shared
primary vehicles and 1,024 consist parts in complete proofs. Limits on rows,
scanned slots, bytes and private frames are explicit resource limits, not
assertions that every upstream-size operation is supported.

## Composition and excluded commands

These six native conveniences are not directly admitted: `MassStartStop`,
`DepotMassSell`, `DepotMassAutoreplace`, `ChangeTimetableBulk`,
`AddSharedVehiclesToGroup` and `RemoveAllVehiclesGroup`.
Their outcomes can be composed from individual lifecycle,
replacement, timetable, group-assignment and construction actions using the
bounded observations above. The caller must refresh membership/versions and
report partial success. Composition does not promise atomic native bulk
semantics; repeating a toggle is not a desired-state operation. Corresponding
raw bulk GUI commands remain outside visual admission.

`BuildObjectArea` and `PlaceHouseArea` are admitted only for equal in-map
endpoints and a selected 1 × 1 object/house. Their native quota and command
semantics are retained; larger construction areas still compose point actions.

The remaining 43 native commands are excluded/internal:
`ClearOrderBackup`; `IndustrySetFlags`, `IndustrySetExclusivity`,
`IndustrySetText`, `IndustrySetProduction`; `SetCompanyMaxLoan`;
`EngineControl`, `RenameEngine`; `Pause`; `RenameTown`, `TownCargoGoal`,
`TownGrowthRate`, `TownRating`, `TownSetText`, `ExpandTown`, `DeleteTown`;
`MoneyCheat`, `ChangeBankBalance`; `CreateSubsidy`; `CompanyControl`;
`CreateCustomNewsItem`; `CreateGoal`, `RemoveGoal`, `SetGoalDestination`,
`SetGoalText`, `SetGoalProgress`, `SetGoalCompleted`, `GoalQuestion`;
`CreateStoryPage`, `CreateStoryPageElement`, `UpdateStoryPageElement`,
`SetStoryPageTitle`, `SetStoryPageDate`, `ShowStoryPage`, `RemoveStoryPage`,
`RemoveStoryPageElement`; `ScrollViewport`; `ChangeSetting`;
`CreateLeagueTable`, `CreateLeagueTableElement`, `UpdateLeagueTableElementData`,
`UpdateLeagueTableElementScore`, `RemoveLeagueTableElement`.
Company/session creation is operator provisioning; order-backup clearing is
native housekeeping. A `Deity` trait alone is not an exclusion rule: actual
normal-company guards determine the inventory.

## Review evidence and remaining validation

Implementation followed independent design review before central integration.
The written [orders contract](orders-family-integration.md),
[construction contract](parity-construction-design.md),
[story contract](parity-story-design.md) and
[visual story review](parity-story-visual-design.md) record family boundaries,
normal native operations, versions, visibility and private proofs. Additional
pre-implementation reviews cover overlapping questions
(`/tmp/openttd-question-overlap-architecture-review.md`) and fixed-industry
platform dependencies (`/tmp/openttd-industry-platform-architecture-review.md`).
Approval of those designs is not proof of implementation correctness or gameplay
acceptance. Central registry/schema/target integration and backend projections
remain separate review responsibilities.

The latest native gameplay-suite snapshot has SHA-256
`1c5a2eaa11b4443e70a2d231f4e8f78d8765e4e4aef52588aac02772f28c9f43`.
Its actual catalog contains 104 actions / 60 queries with BLAKE2b-256
`083a13031161b652d3892f756d1dc5a097f8b1991383174a075ce2fa09ff85a3`.
All 240 native cases / 116,174 assertions, 12 native live tests and 277 backend
tests passed. Public pause and renderer fixtures retain their separately
recorded pause/news build attribution. Technical checks establish their tested
interfaces; they do not establish successful autonomous businesses or release
closure. Detailed evidence is in the
[validation record](integration_validation.md#expanded-acceptance-criteria).

A subsequent descriptive-only road-access build (`4e26eb85…`) adds two catalog
conventions and the [shared road guide](capture-controls.md#road-access). Its
actual 104-action/60-query export has BLAKE2b-256
`aabfc928e2ad99625d0a061ee5bcc7382d0ebdf4772f53901d55cd012d9cd757`;
all other catalog content is unchanged. The build, actual export and 277 backend
tests passed. Native gameplay suites retain the preceding binary attribution.

The permanent `local-mixed` world now runs that 104-action/60-query build after
its 2026-10-18 upgrade at approximately 03:10 UTC. Historical receipts,
credentials, run bindings and history survived the recorded preservation checks;
authenticated public reads, capture restrictions and actual Chrome checks passed.
The original production action POST was not retried. See the
[upgrade evidence and limits](integration_validation.md#mixed-world-gameplay-gate).

The earlier validated `bd6fa099…` native snapshot has an actual 104-action/56-query
catalog with BLAKE2b-256
`62b25b961f2b7be96d50a37e62cbbc9f47128ffa2e151ee3bb092ce076b57cf1`.
Its matching native suite passed 170 cases and 109,110 assertions; the backend
passed 232 tests, and 12 native live tests passed. Full hashes, artifacts and
historical build attributions are recorded in
[integration_validation.md](integration_validation.md#parity-integration-checks-in-progress).
A separate genuine-PNG road-stop proof succeeded after a 20.000667-second
inspection, completed in 20.728900 seconds, and retained its request identity,
execution tick and station incarnation on exact retry. This does not diagnose
the earlier unknown Luna rejections. The older same-ID text-issuance proof and a
separate genuine-PNG popup proof on native `4e26eb85…` (104 actions / 60 queries)
passed. The latter captured the older visible YES witness and one native answer;
GameScript observed one YES and zero NO answers. Both native popups were gone after
replay; text-inbox retirement remains separate. Exact retry retained its sequence,
intent and execution tick despite checkpoint advancement; a new request ID with
the invalidated image received 404. Twelve PNG downloads represented seven image
IDs. [Proof evidence and limits](integration_validation.md#parity-integration-checks-in-progress)
record the four preserved incomplete attempts and exclude timed lease-boundary,
restart, universal-GUI and autonomous-play claims.

[Maximum-map helper cost](industry-platform-proof.md#measured-maximum-map-cost)
is measured on its attributed debug test build: 20 fresh 4096 × 4096 scans had
a 1,779 ms median and 1,894 ms p95, excluding setup. End-to-end publication
latency and broader conservative-staleness behavior remain separate checks.
The subsequent lifecycle fix and finite UI controls are included in the
corrected shared-actuator snapshot. Historical build/load results do not
transfer to later source.

Both original fresh Luna windows and the separate 23:20–23:50 continuation
failed overall under a GUI-only restriction: neither visual company operated a
revenue service. That restriction exceeded the intended capture contract; these
results do not assess image perception with shared typed actuators. The
continuation preserved both text companies' post-income fleets and further
gross income, but cumulative company net remained negative. Company 1 had one
order on a stopped bus; company 3 had no service assets. Company 0 used company
1-owned road after repairs, without reciprocal use or two operating competitors.
The common static UI reference supplied at 23:35 is explicitly recorded as
assistance. These trials used an earlier frozen runtime and do not establish
the newer snapshot's gameplay result.

The corrected 90-minute [four-Luna trial](luna-trial-03.md) used genuine image
perception with shared typed actions on its frozen `165a44…` runtime. Both text
companies earned transport revenue, expanded after income and earned more;
both capture companies earned zero revenue. Their depot exits were disconnected
from the road network. Direct partner chat reached all four clients, but useful
reciprocal road use and two paid competing services were not established.
Image-use and history-scope qualifications remain explicit in that record.
Full semantic parity, the four-company gate and release closure remain unproved.

The fresh four-Luna trial on runtime04 is running on frozen `4e26eb85…` with the shared road
guide from 03:04:49.222991 to the fixed 04:34:49.222991 UTC cutoff on
2026-10-18. It supplies no acceptance outcome yet.
