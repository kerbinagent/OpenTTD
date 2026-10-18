# Orders and timetables integration contract

Implementation: `src/integration/actions_orders.cpp`, using the four `Integration::Detail` hooks declared in `actions_internal.h`. Register this source in CMake. The central registry, schema, target inspection and dispatcher integration remain owned by the root integration change.

## Central hook integration

- Call `DispatchOrdersAction` after schema validation and company binding, before the old `order.*` dispatcher. Remove the old duplicate order dispatcher.
- Call `QueryOrdersState` before the old `orders` report. Remove that duplicate report.
- Append a present `CaptureOrdersTargets` result as one private lease entry. Recompute it at publication through the existing lease comparison. A stale supplied version rejects before a normal command is posted. Its JSON contains generation-bearing order versions and must stay private.
- Merge a present `OrdersIntentState` result into `NativeIntentState`; remove the old selected-vehicle manual-order array proof. The family proof includes every physical row, including implicit rows, and the complete shared cohort. It deliberately omits process-local generations for renderer comparison.
- Native target validation must inspect every member of each referenced primary vehicle's `FirstShared()/NextShared()` cohort. Enforce ownership, primary type, and `GetInstanceGeneration() <= watermarks.vehicle` for each, with the same 4096-member bound. This is required for source and destination of `CloneOrder`, and source of `CloneVehicle`, as well as all order/timetable mutations.
- The cohort cap is a published integration resource limit, not an upstream game limit. A valid upstream cohort larger than 4096 currently rejects explicitly. No partial cohort proof is accepted.

Public `order_version` and `source_order_version` are opaque strings of length 68 (`ov1:` plus 64 hexadecimal characters). The digest uses the existing bundled BLAKE2b-256 implementation and includes engine instance, list incarnation, exact serialized physical orders including timetable fields, and sorted cohort vehicle IDs/incarnations. It excludes current order progress, lateness and moving vehicle physics. Every report page returns the version; restart paging if it changes.

Historical exact-canonical terminal retries must remain looked up before newly required public-schema fields are validated. Existing stored requests are not rewritten.

## Registry entries

All vehicle parameters use the existing boot-fenced owned vehicle handle. All actions below require `vehicle_id` and `order_version`.

| Action | Additional required fields | Optional fields | Native command |
|---|---|---|---|
| `order.insert` | destination except nearest-depot branch | position, destination_type, load, unload, routing, stop_location, depot_action, legacy non_stop/service/halt | InsertOrder |
| `order.conditional.insert` | target_position, variable | position, comparator, value | InsertOrder |
| `order.delete` | position | | DeleteOrder |
| `order.skip` | position | | SkipToOrder |
| `order.move` | position, target_position | | MoveOrder |
| `order.copy` | source_vehicle_id, source_order_version | share | CloneOrder |
| `order.unshare` | | mode | CloneOrder |
| `order.modify` | position, field, value | | ModifyOrder |
| `order.refit` | position, mode; cargo_id in cargo branch | | OrderRefit |
| `timetable.set` | position, field, value | | ChangeTimetable |
| `timetable.start` | start_tick | shared | SetTimetableStart |
| `timetable.autofill` | enabled | preserve_wait_time | AutofillTimetable |
| `timetable.reset_lateness` | | shared | SetVehicleOnTime |

`position` and `target_position` refer to **physical** order positions, including implicit rows. Existing-row positions are 0..253, with actual existence checked by the family/native command. Insertion position defaults to append and is 0..254. A conditional insertion's target refers to the **pre-insertion** list; zero is allowed for an empty list. Native insertion adjusts conditional branch destinations.

`order.unshare.mode` is `keep|clear`, default `clear`. Clear preserves historical `CO_UNSHARE` behavior. Keep deterministically selects a distinct shared member and posts `CO_COPY`; it returns `not_shared` if there is no peer. No order-list mutation bypass is used.

`order.insert.destination_type` is `station|waypoint|depot|nearest_depot`, default station. The first three require `destination`; nearest_depot forbids it. Loading/stop-location fields require station. Depot policy fields require depot or nearest_depot. `routing` conflicts with legacy `non_stop`; `depot_action` conflicts with legacy `service`/`halt`. Legacy service and halt cannot both be true. The native command still enforces vehicle/destination compatibility and policy restrictions.

## Typed schemas

Use existing full-object `anyOf` schema branches, with additional properties forbidden. In particular, `order.modify` needs one whole-object branch per field with a singleton `field` enum and that field's typed `value`; no conditional schema DSL is required.

| Field/policy | Values |
|---|---|
| load | if_possible, full, full_any, none |
| unload | if_possible, all, transfer, none |
| routing | stop, non_stop, via, non_stop_via |
| stop_location | near, middle, far |
| depot_action | always, service, halt, unbunch |
| condition_variable / insertion variable | load_percentage, reliability, max_reliability, max_speed, age, remaining_lifetime, requires_service, unconditional, driving_backwards |
| condition_comparator / insertion comparator | eq, ne, lt, le, gt, ge, is_true, is_false |
| condition_value / insertion value | integer 0..2047; percentage variables at most 100 |
| condition_target_position | integer 0..253 |

For `order.modify`, field is exactly one of load, unload, routing, stop_location, depot_action, condition_variable, condition_comparator, condition_value, condition_target_position. Existing native order/type constraints remain authoritative. A variable change deliberately preserves native normalization of its comparator/value.

Conditional insertion builds one complete Order and posts one InsertOrder. Boolean variables default to is_true and require is_true/is_false, with zero numeric value. Numeric variables default to eq/zero and reject Boolean comparators. Unconditional requires eq/zero. Driving backwards requires a train. No timing/refit values are included in the inserted Order; those are separate commands.

`order.refit.mode` is cargo, auto or none. Only cargo takes a required valid cargo_id. Auto/none forbid cargo_id. Native command enforces station/depot and auto-refit applicability.

`timetable.set.field` is wait_ticks, travel_ticks or speed_limit. `value` is integer 0..65535. Zero clears a time; zero or 65535 clears a speed cap. Speeds here use **native km/h-ish** units, as returned by catalog vehicle maximum-speed fields. Aircraft speed limits and conditional travel/speed changes remain normal native rejections. Conditional wait time is supported.

Conditional `max_speed` thresholds use the native conditional **speed** convention, which is different from timetable km/h-ish: the game compares `GetDisplayMaxSpeed() * 10 / 16` against this threshold. Do not copy catalog maximum_speed directly into a conditional threshold. No client display-unit settings affect either API representation.

`timetable.start.start_tick` is uint64 engine tick; native allowable-date bounds apply. `shared` defaults false. Shared starts require a complete timetable and deliberately stagger its members using normal native sorting. `timetable.reset_lateness.shared=true` aligns members relative to the latest member rather than independently setting every member to zero.

`timetable.autofill.enabled` is required Boolean. preserve_wait_time defaults false and is only valid when enabled. Native enable does not clear an existing preserve flag. The family therefore rejects a true-to-false preservation change until a separate disable request completes; enable again with a fresh order version. Private leases additionally include all cohort autofill/preserve flags to prevent a queued request from crossing such a change. Native autofill stops other cohort members' autofill as usual.

Bulk timetable changes are a composition of individual requests with refreshed versions. No atomic bulk completion is claimed: the upstream bulk command ignores some per-row failures.

## Queries

`orders` and `timetable` require vehicle_id. Both accept after_position=-1..253 (default -1), limit=1..64 (default 64), shared_after_id=-1..UINT32_MAX (default -1), shared_limit=1..64 (default 64). Both report order_version, physical order_count/manual_order_count, paged orders/next_after_position and shared_vehicles/next_shared_after_id/shared_count. Cohort handles retain boot fencing.

Rows retain old numeric type/destination/load/unload/non_stop/refit_cargo_id compatibility fields where meaningful, and add kind, implicit, typed routing/loading/stop/depot/refit/conditional details, wait_ticks/travel_ticks, timetabled flags and nullable speed_limit. Timetable adds selected vehicle start_tick, started, lateness_ticks, autofill, preserve_wait_time, complete, duration_ticks and speed_unit=kmh_ish. Reports are text-mode only through the existing central mode guard.

## Native descriptor changes

Register ModifyOrder, OrderRefit, ChangeTimetable, SetVehicleOnTime, AutofillTimetable and SetTimetableStart on the corresponding canonical action entries. They carry no construction footprint.

`NativeArgument(Order)` accepts conditional Orders in addition to existing destination types. Normal native `CmdInsertOrder` validation rejects invalid flags, enum values and unsupported new-order fields. In `DescribeNative`, only OT_GOTO_DEPOT enters depot reference extraction. An OT_CONDITIONAL is never interpreted as a DepotID merely because it is not station/waypoint. The exact typed argument roundtrip, company ownership, pool-generation inspection and native command validation remain mandatory.

The family decoder uses the same exact serialize/decode roundtrip and supports the order/timetable commands above plus CloneVehicle. It does not expose raw packed native bytes, flags, callback selection or private proofs publicly.

## Meaningful verification after central registration

Exercise one-command conditional insertion with pre-insertion targets; typed modify/refit; stale positional order_version after another order edit; changed cohort invalidation; copy/share source_order_version mismatch; explicit keep versus legacy clear unshare; wait/travel/speed set and clear; conditional wait and rejected conditional travel; shared start and relative reset behavior; autofill preserve-mode stop/restart; physical row and shared-member pagination; visual proof mismatch after a shared-member identity replacement; exact historical terminal retry without new version fields.
