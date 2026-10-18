# Land information observations

`land.info {tile}` is a text-mode, company-scoped report for one valid tile.
Its existing schema and fields remain unchanged: native description, owners,
local authority, date and selected specification, per-tile cargo acceptance,
and an advisory `clear_preview`. The current world's capabilities and exported
catalog determine whether this source implementation is deployed.

The report uses the native description and acceptance bodies. House and
industry-tile callbacks run with one request-local persistent-storage overlay.
The same overlay spans house description, acceptance and the clearing test, so
later callbacks can read permitted earlier writes without changing live PSA
values, owner pointers/lists, pool counts or instance generations. Direct
per-tile acceptance is retained, including neutral-industry behavior; catchment
aggregation is not substituted.

House removal follows the native company rules. Human companies bypass the
house deny-destruction callback; AI companies may reach it after the native
player-protection checks. The report preserves that distinction and the native
callback/property precedence. Invalid custom-name and deny callback results
retain their native fallback/boolean meaning while observation avoids their
GUI diagnostic side effects. Ordinary native GUI and command calls retain
their existing live-storage behavior and diagnostics.

`clear_preview` uses one specialized native `QueryCost` entry over the shared
landscape-clear body. It preserves native command flags, tile validation,
clearing quota admission, ownership and vehicle checks, cost/error ordering,
and nested town-rating test bookkeeping. It cannot execute demolition, charge
money or reserve funds; later construction can differ as the world changes.
The internal house observation seam also rejects execution or missing
`QueryCost` flags.

The gameplay-state boundary covers the map, ownership, cash, clearing quota,
real town ratings, both RNG streams and persistent-storage pools/values.
Existing native query auxiliaries remain: cleared-object scratch areas, the
queried tunnel end tile, rating-test scratch, cached display names, temporary
resolver/string state, profiler counters and debug bookkeeping. This is not
byte-for-byte process immutability or a general callback sandbox.

After static tile validation, the report consumes one start from the shared
process-global one-second text/refit/station-coverage/control-metadata gate.
Native callbacks can scan nearby tiles or expand long strings; the interval
bounds starts, not execution time or temporary native formatting allocation.
Existing text and final 40 KiB report guards remain. A report rejected by those
guards leaves callback writes in disposable request-local storage.

The finite audit covers all eleven native tile families for description and
clear-cost dispatch. Other tile families retain their ordinary native paths;
physical changes and pooled-asset deletion stay in the `Execute` phase. This
report-specific work makes no blanket purity guarantee for other readers or
for the separate native Shift semantics of `action.estimate`.
