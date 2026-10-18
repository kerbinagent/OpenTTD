# Station site observations

`station.site` is a text-mode planning report. It accepts
`{build_action,build_parameters}` for `rail.station.build`, `road.stop.build`,
`water.dock.build` or `airport.build`, using the same parameter schemas as those
actions. Rail stations and road stops accept explicit loaded `spec_class` and
`spec_index` selections, including custom NewGRF specifications. The native
command decides current availability, layouts, spread and construction rules.

Rail and road dimensions admit the normal construction extent of 64. Rail
footprints follow the selected axis; road stops use the requested rectangular
width and length. The report clips catchment coverage to the map and runs the
native GUI rectangular acceptance and production aggregators. It does not merge
an existing joined station's coverage or model irregular airport tile shapes.
A large site can therefore return useful cargo information while native
feasibility reports that the current station-spread setting prevents building.

`feasibility` is the native `QueryCost` test under normal command flags. Its
status is `query_test_passed` or `query_test_failed`. It provides the native
signed cost on success, or a plain UTF-8 error of at most 1024 bytes on failure.
`cash_checked`, `reserved` and `build_result_guaranteed` remain false. This is
separate from the native Shift semantics of `action.estimate`.

Each accepted or supplied cargo row contains `name` and `name_truncated`.
Names are plain UTF-8 previews of at most 64 bytes, clipped between codepoints
after control characters are sanitized. Use `display.text` with `cargo_name`
for the full label. The fixed 64-cargo arrays produce at most 128 rows; the
complete result remains within the existing 40 KiB report budget even with
maximal printable JSON escaping and a full bounded error.

After static selection, geometry and handle validation, the report consumes
one start from the existing process-global, one-second text/refit/control
metadata admission gate. Rail/road catchment rectangles have at most 5184 tiles;
a conservative bound across supported station kinds is 7056. Native callbacks
can perform additional work, including 256 production probes per house or
large neighbor scans. The admission interval bounds starts, not callback
latency or temporary native string-formatting allocations.

Cargo callbacks run with one request-local PSA overlay shared across tiles and
the acceptance and production passes. Reads fall back to live storage, while
per-industry and per-town/GRF writes remain in scratch memory. Later callbacks
see permitted earlier writes in the same query. This path creates no native
PSA pool entries and does not consume their instance generations. Normal GUI
and gameplay cargo calls retain their existing live-storage behavior. QueryCost
station/road-stop availability suppresses malformed-callback GUI diagnostics;
normal command and picker diagnostics retain their existing behavior.

This isolation applies to the station coverage callback path. `land.info` has
older native description, clearing and cargo callback paths that require a
separate purity audit; this change does not establish purity for every reader.
