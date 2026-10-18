# Native decision information

These observations expose information from the ordinary vehicle window,
purchase panel, cargo payment graph and bridge picker. They use the existing
text observation route and company binding. Select cargo/model IDs from their
catalogs; model availability is checked for the acting company.

`vehicle.details` retains its existing visible-primary-vehicle handle rules and
adds `status: {kind, text, text_truncated}`. The native vehicle-window selector
supplies its normal non-hover status, including precedence between crash,
breakdown, stopping, no power, waiting, lost destinations and loading. Its
service/halt/unbunch wording remains in `text`. Text is plain UTF-8 with a
1024-byte preview and an explicit truncation flag. An unavailable native
nearest-depot fallback has `kind: "other"` and empty text.

The finite status kinds are `crashed`, `broken_down`, `stopping`, `no_power`,
`stopped`, `waiting_unbunching`, `train_stuck`, `destination_too_far`,
`cannot_reach_station`, `heading_to_station`, `cannot_reach_depot`,
`heading_to_depot`, `cannot_reach_waypoint`, `heading_to_waypoint`,
`loading_unloading`, `leaving`, `no_orders` and `other`.

`cargo.payment_rates` accepts exactly `{cargo_id}` for a current standard cargo.
It returns the same twenty samples as the native payment-rate graph: ten cargo
units over twenty tiles, with input `transit_periods` 4,8,…,80. Each sample has
`transit_periods`, `display_time` and signed native-money `income`. Display times
are 10,20,…,200 economy days or 20,40,…,400 wallclock seconds; `time_unit` names
the active convention. `amount`, `distance_tiles`, `money_unit` and
`income_scope: "base_delivery"` state what is sampled. Native cargo aging,
inflation and cargo-profit callbacks apply. Subsidies, feeder allocations,
acceptance and running costs are separate. Response budget: 8 KiB.

`catalog.bridges` accepts exactly `{}` and returns at most thirteen current
road/rail bridge choices in native type order. Each item contains `bridge_type`,
`material`, `material_truncated`, `minimum_span_tiles`, `maximum_span_tiles` and
`maximum_speed`. Spans count interior tiles excluding both heads; zero is a
valid minimum. The maximum respects the current global construction limit.
Only the native unlimited-speed sentinel becomes JSON null; speed zero stays
zero. `speed_unit` is `kmh_ish`, matching native internal display-speed values.
Material text is a 512-byte plain UTF-8 preview. Site clearing, foundations,
funds and final construction remain native command validation. Aqueducts use
their own native choice. Response budget: 24 KiB.

`engine.purchase_info` accepts exactly `{engine_id}` for one enabled model that
the actor can currently build. Hidden models remain eligible through the native
Show Hidden behavior. It returns `engine_id`, `vehicle_type`,
`speed_unit: "kmh_ish"` and the applicable fields in `performance`:

- Non-wagons: `designed_year`, `life_calendar_days`, `reliability_percent` using
  the current model reliability shown by the native purchase pane.
- Rail vehicles: `empty_weight_tonnes`. Locomotives also have `power_hp`;
  `maximum_tractive_effort_newtons` appears when the native acceleration/type
  rules display it. Applicable locomotives include `powered_wagon_power_hp`
  and `powered_wagon_extra_weight_tonnes`, per eligible powered wagon.
- Road vehicles under the native non-Original acceleration model:
  `empty_weight_tonnes`, `power_hp`, `maximum_tractive_effort_newtons`.
- Ships: `ocean_speed` and `canal_speed` from native water-class fractions.

Inapplicable fields are omitted. Empty model weight is neither loaded cargo
weight nor a promise about the complete articulated consist. Force values are
Newtons: the native display formatter converts these to kN. Existing prices,
capacities, refit choices, track compatibility and aircraft range remain in
`catalog.engines`. Response budget: 4 KiB.

Every response also includes the existing snapshot tick/company fields.
The cargo graph performs twenty native income calculations; selected-model
performance performs a fixed set of native getters for one model. Loaded
NewGRFs can run their ordinary display/property callbacks during those reads.
Finite calls bound the work shape rather than promise a callback time limit.
