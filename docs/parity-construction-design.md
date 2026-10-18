# Construction family contract for review

Implementation ownership: `src/integration/actions_construction.cpp` and
`src/tests/test_integration_construction.cpp`. Root owns central schema and
command registry, common target/proof integration, result decoding and build
registration. Independent architecture review approved this contract on
2026-10-17 with the explicit dependency-limit caveat below. The family source
and focused test translation units compile independently; central wiring and
linked runtime validation are separate integration work.

## Shared types and entry points

All public action/query objects reject additional properties. `tile` means the
existing native tile index or strict `{x,y}` object. `handle` means strict
`{id,generation,engine_instance_id}` with positive generation, nonempty boot ID,
and ID in 0..63999 for this family's entities. A handle is checked again at
publication. `name` is a UTF-8 string, at most 128 bytes in the integration
boundary; native per-name character limits and uniqueness remain authoritative.
`page` means optional `after_id` integer -1..UINT32_MAX and `limit` integer
1..256, defaults -1/64; scans are independently capped at 4096 slots/steps.

Entry points use the root-created `Integration::Detail` internal header:

```
optional<ActionResult> DispatchConstructionAction(name, parameters);
optional<Json> QueryConstructionState(name, parameters);
optional<Json> CaptureConstructionTargets(name, parameters);
optional<Json> ConstructionIntentState(command, canonical_native_data);
optional<vector<TileIndex>> ConstructionIntentTiles(command, canonical_native_data);
```

`nullopt` means unhandled. Errors throw shared `InvalidParameters`; central code
owns conversion to public errors. Helpers do not change company context, register
commands or submit a second command. Common parameter/schema validation runs
before dispatch. Existing construction actions needing additional parameters
are routed here before the historical dispatcher branch. Native validation
still determines allowed construction and charges costs.

## Actions and exact native calls

Required parameters precede `;`; parameters after it are optional. Unspecified
booleans default false unless stated. Integers are bounded again against loaded
specs at runtime, even where their wire range is wider.

| Action | Fields and schema | Normal `Command<...>::Post` |
| --- | --- | --- |
| `object.build` | `tile, object_type:int[0,63999]; view:int[0,255]=0` | `BuildObject(tile,ObjectType(object_type),view)` |
| `house.build` | `tile, house_type:int[0,4095]; protected:bool, replace:bool` | `PlaceHouse(tile,HouseID(house_type),protected,replace)` |
| `town.found` | `tile; size:small\|medium\|random=small, city:bool, layout:original\|better_roads\|grid_2x2\|grid_3x3\|random=current_setting, name:string` | `FoundTown(tile,size,city,layout,false,name_parts,name)` |
| `town.action` | `town_id:handle, town_action:advertise_small\|advertise_medium\|advertise_large\|road_rebuild\|build_statue\|fund_buildings\|buy_rights\|bribe` | `TownAction(town.xy,town_id,native_enum)`; leading tile is the normal Location-flag Post argument |
| `industry.fund` | `industry_type:int[0,239]; tile, layout:int[0,UINT32_MAX]=0` | `BuildIndustry(tile_or_zero,type,layout,false,interactive_seed)` |
| `sign.place` | `tile, name:string` | `PlaceSign(tile,name)` |
| `sign.rename` | `sign_id:handle, name:string` | `RenameSign(sign_id,name,Colours::Invalid)` |
| `sign.delete` | `sign_id:handle` | `RenameSign(sign_id,"",Colours::Invalid)` |
| `sign.move` | `sign_id:handle, tile` | `MoveSign(sign_id,tile)` |
| `depot.rename` | `depot_id:handle, name:string` | `RenameDepot(depot_id,name)` |
| `waypoint.rename` | `station_id:handle, name:string` | `RenameWaypoint(station_id,name)` |
| `station.label.move` | `station_id:handle, tile` | `MoveStationName(station_id,tile)` |
| `waypoint.label.move` | `station_id:handle, tile` | `MoveWaypointNAme(station_id,tile)` |
| `road.waypoint.remove` | `start, end` | `RemoveFromRoadWaypoint(start,end)` |
| `road.pieces.build` | `tile, road_bits:int[1,15]; road_type:int[0,62]=0, toggle_directions:none\|northbound\|southbound\|both=none` | `BuildRoad(tile,RoadBits(road_bits),road_type,toggle_mask,TownID::Invalid())` |
| existing `rail.station.build` | Existing fields plus `spec_class:int[0,65534]=DFLT, spec_index:int[0,65534]=0` | Existing `BuildRailStation` with selected class/index |
| existing `rail.waypoint.build` | Existing fields plus `spec_class:int[0,65534]=WAYP, spec_index:int[0,65534]=0` | Existing `BuildRailWaypoint` with selected class/index |
| existing `road.stop.build` | Existing fields plus `spec_class:int[0,65534]=DFLT, spec_index:int[0,65534]=0` | Existing `BuildRoadStop` with selected class/index |
| existing `road.waypoint.build` | Existing fields plus `spec_class:int[0,65534]=WAYP, spec_index:int[0,65534]=0, road_type:int[0,62]=0` | Existing `BuildRoadWaypoint` with selected class/index; road type is the normal picker availability context and is not a native command argument |
| existing `terrain.plant_tree` | `tile; tree_type:int[0,40]` | `PlantTree(tile,tile,tree_type_or_TREE_INVALID,false)` |

`road_bits` uses the already documented native road-bit ordering
NW/SW/SE/NE. `toggle_directions` is explicitly a toggle, not a desired-state
setter; its current direction state belongs in the private precondition proof.
No public `is_ai`, owner override or native town-authority ID is accepted.
Existing `road.build` remains backward compatible.

Object type 3 is purchased land and type 4 is headquarters. They use the one
normal object operation; aliases would add no outcome. Direct statue object
construction is forbidden by the native command; the normal town action builds
one. Scenario-only objects are unavailable. Object views are checked against
the selected spec. Houses use manual-building availability: enabled house spec
and allowed house-placement setting; year ranges are descriptive, not an extra
integration restriction absent from `CmdPlaceHouse`.

Town founding never accepts large size or random location. Native settings
determine permitted layouts. Omitted/empty name generates native name parts
with the interactive random source, as the GUI does; a supplied name passes
through normally. Random parameters become part of the captured native command,
not a public knob. Do not consume simulation RNG during observation/capture.

Industry operation rejects a supplied tile/layout when the current raw-industry
setting makes that type a prospecting action; for fixed-site funding, `tile` is
required. Native settings, callbacks and rules decide availability. Layout is
the native starting layout and may fall back to another layout, not a promise
of exact geometry. Seed is generated internally as in the GUI and frozen in
the captured native command. A paid prospect may create nothing. Result is cost
and native success/rejection; it must not claim an industry was necessarily
created. Visual receipts do not reveal a remote chosen site.

Labels may target the authenticated company's station/waypoint or a neutral
station/waypoint when the native command permits. This local helper must not
relax `Parameters::Station` globally: build joins remain strictly owned. Depot
rename requires owned depot tile. Sign helpers use `CompanyCanEditSign`, which
permits normal other-company signs but protects deity signs. Town handles need
existence/incarnation checks, not invented ownership.

No multi-command transaction or batch scheduler is added. Area object/house
placement, area clearing/planting, level raise/lower, and larger constructions
can be composed under existing quotas with explicit partial success. This
family adds the missing unique outcomes and selected-spec variants.

## Queries and positive result shapes

All are text-only and begin with existing `tick,company_id`. Catalog rows expose
public picker choices, never internal rendering tables or serialized specs.
Native availability remains authoritative after the observation.

| Query | Parameters | `items` row fields |
| --- | --- | --- |
| `catalog.objects` | `page` | `object_type,name,width,height,views,base_build_cost,site_costs_extra,removable,built_on_water,water_only`; normal available specs, including HQ/owned land, excluding scenario-only/statue |
| `catalog.houses` | `page` | `house_type,name,width,height,population,introduction_year,last_year,placement_enabled,protected_option`; normal enabled selectable house roots, not continuation tiles |
| `catalog.industries` | `page` | `industry_type,name,cost,mode:fund\|prospect,layout_count,accepts_cargo_ids,produces_cargo_ids,available`; normal GUI-available types and current policy; layout count does not imply an exact chosen layout |
| `catalog.trees` | `page` | `tree_type,name,variant,required_tropic_zone`; current landscape's native selectable species; random is expressed by omission in the action |
| `catalog.station_specs` | `page; kind:station\|waypoint=station` | `catalog_id,spec_class,spec_index,class_name,name,available,disallowed_platforms,disallowed_lengths`; include default null spec index0 |
| `catalog.roadstop_specs` | `page; kind:bus\|truck\|waypoint=bus, road_type:int[0,62]=0` | `catalog_id,spec_class,spec_index,class_name,name,available,drive_through_only,road_only,tram_only`; include native default/null choices |
| `depots` | `page` | `depot_id:handle,name,location,vehicle_type`; owned depots |
| `waypoints` | `page` | `station_id:handle,name,location,kind:rail\|road\|buoy,owner_company_id:null\|int`; owned and neutral waypoints |
| `signs` | `page` | `sign_id:handle,name,location,owner_company_id:null\|int,editable`; public map signs, including protected script signs marked uneditable |

Every paged response includes `next_after_id`, last examined slot or null. For
class/spec catalogs use `catalog_id=(class_id<<16)|spec_index` as the cursor;
both class transitions and examined specs consume the 4096 scan budget. The
cursor fits UINT32_MAX; class/spec identifiers are configuration-scoped, not
pool entities. No catalog ID can resolve across world/epoch boundaries.

House placement and town-founding settings, town action availability/cost and
town handles belong in the existing town/company report expansion owned by the
reports agent. The construction helper does not duplicate `towns` or
`industries` entity queries. Catalog names above are reserved for this family.
Default station/roadstop null-spec entries are real choices; unknown null
entries must not be treated as default. Station layout/render data are internal;
users choose platform count/length/axis, not internal render-layout IDs.

Picker availability helpers mirror the current normal GUI callback logic with
explicit road type and player context. Native BuildRailStation/BuildRoadStop
already repeat availability callbacks at execution. Discovery must label or
filter selection availability without inventing placement feasibility or
running commands/consuming simulation RNG.
Rail/road waypoint native commands do not repeat those callbacks, so typed
semantic admission must use the same normal-picker predicate for them too.
Keep these small predicates local to the construction helper unless an existing
shared NewGRF helper can be used; extracting GUI code is not required for this
family's ownership boundary.

Industry layout vectors can be large. Catalog rows must not eagerly nest their
offsets. A future bounded `catalog.industry_layout` detail query can expose
configuration-only offsets without map locations if needed; it is not required
for this first family implementation. The native 256-tile dependency cap can
also reject a large valid NewGRF layout. This is an operational capability
restriction, not proof of universal NewGRF parity; the independent review must
resolve how that bound is advertised or enlarged before any such claim.
The inherited 16-tile station/stop/waypoint dimension bounds are another
operational limit: native `station_spread` can reach 64, and NewGRF callbacks may
make a single large layout behave differently from joined smaller pieces. The
first family slice preserves that bound; full configured-world parity requires
the central quota/schema pass to address it explicitly.

## Capture, footprint and result integration requirements

`CaptureConstructionTargets` returns a private deterministic array for handled
actions, appended to the central actor/tile capture. It revalidates explicit
handles and records only relevant identities/selection state. It never returns
a public bypass flag or a full game-state dump. Root's central native decoder
must perform exact canonical argument decoding before any family state helper.

| Family | Identity/state capture | Explicit spatial dependency closure |
| --- | --- | --- |
| Objects/HQ/land | Actor/company incarnation; selected spec/type/view; existing HQ location and old object's incarnation when relocating | New rotated spec footprint, clear-effects closure of its occupied tiles, and old HQ's entire footprint. NewGRF dimensions swap x/y when view bit0 is set. |
| Houses | Selected enabled root spec, footprint dimensions; affected existing houses when replacing | Entire selected 1x1/2x1/1x2/2x2 footprint and full existing multi-tile houses/objects/structures cleared there. |
| Fixed-site industry funding | Actor/type/layout/normal policy; native candidate layouts | Union of candidate native layout tile offsets around anchor plus possible native platform-leveling rectangle and three-tile terrain dependency halo, then root clear-effects dependencies; fail bounded capture if genuinely oversized. Do not choose a feasible layout on caller's behalf. |
| Town founding / industry prospecting | Selected normal command inputs, company incarnation, relevant native policy and anchor when present | Explicit anchor only for town founding. Prospecting has no player-selected tile. Engine-generated settlement geometry and remote prospect location are native effects, not a fabricated screenshot-local footprint. |
| Town action | Town incarnation; relevant company-specific action/rating/counters/settings used for the chosen action | No generic world-map scan. Statue placement and road rebuilding are normal engine-selected effects. |
| Signs/labels/depot names | Fenced pool target, current relevant name/position/owner; station spread for label movement | Old and requested label/sign anchor for move; no simulation infrastructure demolition. |
| Road pieces | Tile and actual road/tram ownership/type/bits and disallowed direction state | Tile plus existing normal clear-effects closure; toggle proof must change if another actor changes direction before submission. |
| NewGRF station/stop selection | Selected class/index/kind and normal availability; existing explicit join target or normal join-search candidates | Existing central selected size footprint plus normal join perimeter/reuse logic. Selecting a different spec does not reduce closure. |
| Tree species | Selected native species/current landscape | Existing tile/clear-effects closure; no hidden-area planner. |

Root extends `NativeDescription` and watermark inspection for TownID/SignID
targets and handles shared StationID semantics. New pool kinds must be checked
against source-frame incarnation watermarks for visual submissions. The existing
global 256-tile native footprint and 16KiB operation-state caps remain explicit;
engine-generated effects are classified by trusted command-specific code, not
an arbitrary public exemption. Their normal native validation still runs in
the authoritative ordered command path. The architecture reviewer has approved
this explicit-target versus native-generated-effect distinction.
The geometry helper inspects at most 65,536 industry layout entries and keeps
at most 256 distinct dependency tiles. Both are operational bounds. A native
candidate outside the map can fail while another candidate succeeds, so its
out-of-map entries do not by themselves invalidate the entire funding intent.

Text result decoding should add new fenced handles for `PlaceSign` and
`FoundTown`, and StationID results for label moves when useful. Existing generic
success/cost/error is sufficient for commands without returned entity IDs.
BuildIndustry does not return an industry ID; discovering a new fixed-site
industry requires a later normal observation, not a guessed ID. Visual result
projection remains generic and screenshot-bound.

Tests should exercise normal blocked/allowed settings, HQ relocation closure,
multi-tile object/house sizes, sign and neutral waypoint permissions, stale
entity/boot handles, NewGRF default/null handling, bounded sparse catalog paging,
road-direction toggles, and command result fidelity. No production world is
restarted or mutated during this implementation work.
The isolated native test executable linked this family's real source and the
actual extracted common helpers against existing engine objects: four focused
cases passed with 32 assertions. This establishes permission/staleness and
intent-dependency behavior; it does not replace the later central registry,
transport, visual and all-family integration checks.

## Central registry integration checklist

The implementation provides 15 new action names and extends five existing ones.
Add these central `Spec` rows (native metadata is part of the same registry):

```cpp
{"object.build", {"tile", "object_type"}, {"view"}, {Native<Commands::BuildObject>()}},
{"house.build", {"tile", "house_type"}, {"protected", "replace"}, {Native<Commands::PlaceHouse>()}},
{"town.found", {"tile"}, {"size", "city", "layout", "name"}, {Native<Commands::FoundTown>()}},
{"town.action", {"town_id", "town_action"}, {}, {Native<Commands::TownAction>()}},
{"industry.fund", {"industry_type"}, {"tile", "layout"}, {Native<Commands::BuildIndustry>()}},
{"sign.place", {"tile", "name"}, {}, {Native<Commands::PlaceSign>()}},
{"sign.rename", {"sign_id", "name"}, {}, {Native<Commands::RenameSign>()}},
{"sign.delete", {"sign_id"}, {}, {Native<Commands::RenameSign>()}},
{"sign.move", {"sign_id", "tile"}, {}, {Native<Commands::MoveSign>()}},
{"depot.rename", {"depot_id", "name"}, {}, {Native<Commands::RenameDepot>()}},
{"waypoint.rename", {"station_id", "name"}, {}, {Native<Commands::RenameWaypoint>()}},
{"station.label.move", {"station_id", "tile"}, {}, {Native<Commands::MoveStationName>()}},
{"waypoint.label.move", {"station_id", "tile"}, {}, {Native<Commands::MoveWaypointNAme>()}},
{"road.waypoint.remove", {"start", "end"}, {}, {Native<Commands::RemoveFromRoadWaypoint>()}},
{"road.pieces.build", {"tile", "road_bits"}, {"road_type", "toggle_directions"}, {Native<Commands::BuildRoad>()}},
```

Extend `rail.station.build`, `rail.waypoint.build`, `road.stop.build` with optional
`spec_class,spec_index`; extend `road.waypoint.build` with those and `road_type`;
extend `terrain.plant_tree` with `tree_type`. The five historical command cases
may remain as fallbacks, but the family dispatcher must run first.

Nine query rows are `catalog.objects`, `catalog.houses`, `catalog.industries`,
`catalog.trees`, `catalog.station_specs`, `catalog.roadstop_specs`, `depots`,
`waypoints`, `signs`. All accept optional `after_id,limit`; station specs also
accept `kind`; road-stop specs also accept `kind,road_type`. No new query has
required fields.

Add explicit field-schema cases from the action/query table before the historical
boolean fallback. `layout` is name-dependent: town founding uses the string enum,
industry funding uses uint32, and the existing airport uses integer 0..255.
`kind` is name-dependent between station and road-stop catalogs. New
`town_id,sign_id,depot_id` use the same fenced handle object as station handles.
`size,town_action,toggle_directions` are enums; `object_type,house_type,
industry_type,tree_type,spec_class,spec_index,view,road_bits` are bounded integers.
`protected,replace,city` are booleans. Target metadata should recognize town,
sign and depot handles instead of labeling all of them company operations.

After central shape validation, call the family dispatch/query entry points;
append its target array to existing capture; merge its native intent-state object
under the dedicated `construction` key. Capture and native intent helpers must
run under authenticated company context. `ConstructionIntentState` performs
canonical exact decode for its supported command cases and omits process-local
generations/boot IDs in cross-process comparisons. Typed target captures retain
fenced handles. Root's W(F) checks still fence source-frame target incarnations.

Native descriptor extensions must compute spec-dependent object/house geometry,
old HQ closure, industry candidate-layout union or procedural classification,
sign/town scalar references, and label/sign old anchors. Neutral label commands
must not pass through strict owned-build-join validation. `TownAction` has a
Location flag: its Post leading location is not part of `CommandTraits::Args`.
This family does not add or change the native descriptor registry itself.

New `PlaceSign` result data starts with `SignID`; successful `FoundTown` return
data is `(Money,TownID)`. Add fenced handles using live returned entities;
do not treat an admission response as successful construction. Label moves return
`StationID`. `BuildIndustry` has no returned industry ID. Positive backend
projections must retain exactly these newly documented fields and never return
private capture/proof objects.
