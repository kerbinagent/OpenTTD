# Visual industry platform proof

Fixed-site visual `BuildIndustry` uses the normal native command and its free platform leveling. Prospecting retains its existing native site selection. Typed `industry.fund` dispatch is unchanged.

A screenshot protects its corner geometry and road/rail infrastructure. Natural grass, tree growth, shore cover, signal state, reservations and ordinary rail-ground animation may follow the current world. Native validation decides vehicle obstructions and newly protected blockers. This matches the normal human funding outcome; it does not promise that every natural pixel stays frozen.

`CaptureIndustryPlatformDependencies` streams all in-map corner heights, including Void, plus these tagged structural fields into BLAKE2b-256:

* Normal road: road/tram types, bits and valid owners, and disallowed directions.
* Plain rail: track bits, rail type, owner and HalfTileWater classification.
* Map dimensions and industry-platform, height-limit, freeform-edge and build-on-slopes settings.

The generation-free private digest is stored once in each native descriptor and compared before publication. Relevant road/tram/rail owner company IDs are deduplicated and checked against the authority's source-frame company watermark. A reused numeric company ID cannot hide a new infrastructure owner. No process-local generation is compared between worker and authority.

The scan uses a fixed 16 KiB buffer and at most 15 owner flags. No all-map JSON, tile vector, tree set, shadow world or test command is created. The selected site keeps its existing selected-site tile proof and native entity checks. Its collector now uses the existing map-bounded bitmap, with native layout/count/coordinate limits replacing the former 256-cell and 65,536-entry limits. The local platform halo remains conservative site context; the global semantic digest covers height cascades beyond it.

Native platform leveling uses `OWNER_TOWN`, `Auto` and `NoWater`. Autoslope is disabled. Houses, stations, industries, objects, depots, locks, tunnels and bridges cannot be removed through that path. Native handlers protect a newly placed blocker outside the selected site and may skip flattening, while the ordinary funding command proceeds as upstream permits. Normal one-bit road stubs can be removed, including company-owned roads; plain rail may allow an unused corner to change. These exceptions are covered by the structural proof and owner checks. Natural coast clearing affects no player-owned water infrastructure; canals and rivers reject terraforming.

This is a rare full-map visual funding operation. Unrelated terrain-height or road/rail structural changes anywhere conservatively make it stale. Natural/timer changes omitted above do not. Scan latency depends on map size and machine; the private wire state remains constant size. `test_integration_terraform.cpp` contains native command/proof regressions and an opt-in maximum-map timing case tagged `industry-platform-benchmark`.

## Measured maximum-map cost

On the matching 2026-10-17 23:41:47 UTC debug build (`-g`, no optimization flag), 20 fresh test processes scanned 4096 × 4096 (16,777,216) tiles. The measured pure dependency-helper scan excludes native map setup: median 1,779 ms, nearest-rank p95 1,894 ms, worst 1,966 ms. Maximum whole-process RSS was 245,760 KiB (240 MiB), including the native map and test process. These measurements describe one helper scan, not end-to-end gesture/publication latency.

Construction source SHA-256: `7b44a73b5fdfa787eff066137c11d3a2dde9c5812411bc56242ea463d4c00b46`. Matching test binary SHA-256: `84f202f72f19379a7cf309de2a391f78508c827c0f249789a6db1cf7d1e5ea49`. Local full samples and additional source hashes are retained in `/tmp/openttd-platform-benchmark-results.json`. The seven focused native terrain/platform cases passed with 16,496 assertions.
