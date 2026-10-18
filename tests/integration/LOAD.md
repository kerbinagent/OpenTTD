# Live load measurement

`load_world.py` is an opt-in benchmark driver, separate from the local product
launcher. It starts real dedicated servers, the real single-worker ASGI backend,
and four native framebuffer clients. HTTP requests use real loopback sockets.
Install `backend/requirements.txt` in the Python environment used to run it.

```sh
python tests/integration/load_world.py \
  --binary /absolute/path/to/integration-build/openttd \
  --baseset /absolute/path/to/installed/baseset \
  --output /tmp/openttd-load-reference
```

The default workload measures 1,800 steady seconds after bootstrap and warmup:

- One generated, seeded 512 × 512 world; eight normally provisioned companies;
  500 buses with two station orders and their stopped flag cleared. Four separate
  routes per company reduce artificial depot/station congestion. Construction,
  purchases, clones, orders, starts and borrowing use ordinary gameplay actions.
- Four visual agent clients request captures and PNGs every 1.02 seconds
  (about 0.9804 requests per second per client).
  Fifty additional clients each independently POST capture, GET the resulting
  PNG and maintain authenticated SSE. These are 50 browser-protocol clients,
  sharing four authenticated company-view identities. They exercise the same
  HTTP pattern as the browser without a JavaScript rendering engine. They do
  not represent 50 distinct identities or 50 render processes. The backend must
  coalesce same-view captures while a still-valid frame is younger than one
  second; this harness never shares image IDs outside the normal API.
  A parked native view can repeat its frame across requests. Unique native image
  IDs and source PNG creation age distinguish request rate from fresh frames.
  The current park lease is eight seconds, so fresh native frames may arrive at
  about 0.125 Hz while parked. A passing request workload does not certify one
  fresh frame per second.
- One ordinary company rename admission per second, rotating all eight
  companies, and one world chat message per second. Action completion is
  excluded from admission latency. Model inference is excluded.
- Three additional independent worlds each run one normally ordered bus and
  their own ASGI backend, authenticated API probe, state directory and native
  server. This validates four simultaneous groups; only the primary group
  carries the full 500-vehicle/viewer workload.

Map generation uses normal very-flat terrain and a recorded seed. The normal
newgame loan ceiling is 2,000,000 and each company borrows through gameplay;
there are no money cheats, save edits, stepped ticks or fast-forward. The map
finder may reject a seed if it cannot find sufficient clear flat route sites.
The road-heavy performance fixture does not replace the independent four-mode
construction/passenger-service functional tests.

For a setup smoke test, use `--vehicles 32 --viewers 4 --worlds 1 --duration 20
--warmup 5 --fault-seconds 0`. Its summary is explicitly `NON_TARGET_SMOKE`.
Defaults reserve native/API loopback port pairs 16979/16980, 16999/17000,
17019/17020 and 17039/17040; override the two base ports together if needed.

To reuse normal construction as an immutable benchmark fixture, supply
`--initial-save /private/completed.sav --provenance /private/provenance.json`
with `--vehicles 500`. The primary world copies and fsyncs that save into its
new results directory; it starts with a fresh world ID, epoch, ledger, database,
credentials and renderer keys. The original normal map, companies, vehicles,
finances and calendar continue from the saved state. Setup validates the actual
512 × 512 map, exactly 500 owned running/ordered heads, eight company counts and
vehicle IDs against purchase provenance, then skips construction and borrowing.
Other worlds still generate and construct their own one-bus routes.

Trusted operator provenance version 1 records `save_sha256`,
`source_binary_sha256`, source world ID/epoch, completed checkpoint ID/tick,
`completed_checkpoint: true`, routes and
`construction` with positive gameplay cost plus each normal vehicle purchase's
company, ID and execution tick. Save and provenance must be private owned regular
files. No source ledger, database, session or key is imported. The actual loaded
date/tick and natural vehicle-age bounds are written to `fixture-validation.json`;
exact age is unavailable in the current vehicle DTO. Age bounds use purchase
ticks and 74 native ticks per calendar day with one day of update uncertainty.
Record any overlapping benchmark workload in provenance rather than claiming an
otherwise idle reference host.
The driver checks provenance structure, checksums and the loaded entities. It
does not independently attest the claimed construction history or checkpoint
completion; retain the source action ledger when that history must be audited.

Each native process selects its owned config explicitly with `-c` and disables
host search paths with `-X`. Keep the complete pinned baseset and language files
beside the copied executable. Before starting renderers, setup checks the four
visual companies' join policy and uses the public action API to permit joining
when necessary. This setup and its request IDs are recorded outside the timed
workload. A frozen copy without Git metadata reports that metadata as unavailable;
its measured executable, backend and driver hashes remain the runtime identities.

Results are owner-only `configuration.json`, `manifest.json`, raw
`samples.jsonl`, provisional `summary.json`, and authoritative
`audited-summary.json` (or `failure.json`). The manifest identifies
preserved process/state/log directories. Credentials remain in private files
inside those directories and are never printed in measurements. Remove those
directories manually after inspecting a run; the driver stops only its own
processes. A failed bootstrap never counts as a benchmark result.

Latency percentiles use nearest rank and include sample counts. Measured action,
chat, capture, PNG and API-probe attempts record status, bytes, latency and
success; timeouts and failures remain in raw data and error counts instead of
disappearing from the denominator. Setup observations/polls and SSE handshakes
are not part of those latency distributions. Timed action/chat generators must
offer one request per second; expected, attempted, successful, late and missed
counts are reported, and unresolved requests cannot pass.
Chat delivery measures POST start to receipt of the corresponding SSE event by
each viewer, with duplicate events ignored and missing deliveries counted.
SSE reconnects carry their last event cursor. Five-second connection samples can
show reconnect gaps caused by the configured 60-second SSE lifetime; exact gap
durations are not instrumented. Per-message delivery pairs remain measured
through reconnects.

Every five seconds the driver records owned/active/moving head counts, distinct
tile-changing heads per movement window, backend pending action/chat counts,
per-process cumulative CPU time and RSS, API probes and simulation ticks.
Kernel peak RSS covers each listed engine, backend and renderer process since
startup, including bootstrap, burst, steady load, drain and fault phases. It
excludes the harness process and is not a simultaneous aggregate memory peak.
Active means not stopped, not crashed, and at least two orders; it does not mean
every vehicle moves during every sampled instant. Movement is reported
separately. Normal-speed ratio is tick delta × 27 ms / elapsed wall time, using
this checkout's native tick period. Both whole-run and sampled minimum ratios
are reported. Native command queue depth is not instrumented; backend ledger
pending depth is identified separately. Queue ranges and process peaks can also
be derived from these raw samples without rerunning load.

New measurements label `tick_measurement_format` as `rpc_brackets_v1`. Each
world's `hello` request records client monotonic start and receipt timestamps,
the native tick, and the validated world, epoch and engine-instance fence.
The existing `observed_monotonic` timestamp uses that same receipt observation.
It measures receipt time, not the exact instant when the engine read its tick.
Missing or malformed timing or identity data invalidates a bracket-enabled run;
older runs without the format marker retain their original speed checks and
report these additional diagnostics as unavailable.

For consecutive requests, the engine's observation interval lies between
`max(0, current_start - previous_receipt)` and
`current_receipt - previous_start`. Tick progress divided by these interval
bounds gives conservative speed bounds. A speed upper bound below 0.95 proves
a slow interval within those brackets; a bound that straddles 0.95 leaves the
timing inconclusive. These are diagnostics: they never promote a failed receipt
ratio to a pass. The original receipt-based speed formula, minimum threshold,
sampling cadence and workload denominators remain in force. The brackets do
not identify the cause of a stall or supply exact native timestamps.

The authoritative audit requires the full configuration, all 500 active heads,
p95 action admission below 500 ms, p95 chat delivery below one second, no missing
chat deliveries or client errors, and each world's total and minimum sampled
five-second normal-speed ratios at least 0.95. Screenshot latency, achieved
frame counts, memory, CPU and queue samples remain measurements rather than
invented pass thresholds. Loopback excludes WAN/TLS, browser painting and model execution.
Hardware, binary hash, git revision, scenario settings and Python version are
recorded; compare runs only with these differences in view.

After the timed interval, a separate fault phase kills one renderer and leaves
an authenticated PNG socket unread with a small receive buffer and 64 pipelined
requests. It records PNG size, offered response bytes, the host TCP send-buffer
bounds, and early and later world/API/action probes. An unread socket alone does
not prove that the server's send buffer or response slots saturated; blocked
sends and send timeouts are not instrumented. This phase establishes an offered
slow-reader fault, with that limit stated explicitly. Its results remain separate
from the 30-minute steady workload. `--fault-seconds 0` skips this phase.

Before a full run, `--burst-images` can synchronize the first owned PNG GET from
all configured browser and visual clients. With 50 viewers, all 54 GETs must
succeed before steady measurement starts. Each client obtains its image through
its normal capture POST. Process memory includes kernel peak RSS since startup,
so this burst's memory peak remains observable after it ends.

An independent audit can recompute the completed run's measurements without
changing its original summary:

```sh
python tests/integration/load_world.py --analyze /tmp/openttd-load-reference
```

This writes `audited-summary.json`, records the analysis version and hashes of
the evidence, includes late steady-origin HTTP/chat records, and reconciles
attempts against successes. Its whole-interval speed audit always drops the first
and last steady samples and divides the interior tick delta by the full actual
interval. This is a conservative lower bound: unobserved edges receive zero
credit. Newer per-world observation timestamps improve sampled-window timing;
the first and final short-window minima remain unproven. The audit states this
distinction explicitly instead of presenting the interior measurement as exact
boundary coverage.

The recorded 2026-10-17 reference run used an AMD Ryzen Threadripper 3970X
(32 cores, 64 logical CPUs, 62.7 GiB reported RAM). Its steady interval was
21:21:16.750–21:51:16.750 UTC, lasting 1,800.000 seconds. The primary group had
500 active buses and eight companies; three concurrent sentinel worlds each
had their own backend and one active bus. Fifty browser-protocol clients and
four visual clients shared four company views. Another full benchmark overlapped
the first nine minutes; its drain and fault phase ended at 21:30:49.514 UTC.

| Recorded measurement | Result |
| --- | --- |
| Gameplay admissions and ledger completions | 1,800 / 1,800; p95 ACK 128 ms |
| Chat admissions and exact viewer delivery pairs | 1,800; 90,000 / 90,000; p95 delivery 215 ms |
| Measured steady HTTP/client errors | 0 |
| Screenshot POST plus PNG fetch, p95 | 290 ms browser; 309 ms visual |
| PNG fetch alone, p95 | 64 ms browser; 67 ms visual |
| Active heads; backend pending action/chat range | 500 throughout; 1–8 / 1–4 |
| Primary boundary-sampled normal-speed ratio | 0.99952 |
| Conservative whole-interval speed lower bounds, four worlds | 0.99120–0.99166 |
| Primary five-second ratios | minimum 0.93265; p01 0.97610; median 1.00000 |
| Other worlds' minimum five-second ratios | 0.98163–0.98229 |
| Primary backend lifetime peak RSS; sampled thread peak | 103.8 MiB; 84 threads |

The preserved strict verdict is `TARGET_NOT_MET`: two of the primary world's
359 completed five-second windows fell below the predeclared 0.95 minimum.
The other 357 windows met that threshold, as did every measured sentinel window.
Every other audit check passed. The thresholds remain unchanged. The distinct
native frame count was 213 per view; frame creation age had p95 7.58 seconds,
reflecting the parked-view behavior described above. Sampled SSE counts ranged
from 32 to 50 while all 90,000 accepted message/viewer pairs arrived.

This run measured native binary SHA-256
`257ad478d3a47edf178f425ccd1db25c4085aa110b42ee242babb3f41493f20a`,
with 64 API slots, 64 image response slots and eight image lookup threads.
It predates the later recovery-v3 native changes. The later native compile began
at 21:52:02 UTC, after this run closed; it was not part of this measurement.
Backend runtime/schema hashes and the driver hash are in `configuration.json`.
Session evidence is retained in `/tmp/openttd-load-api64-target`, including
`measurement-addendum.json` and the separately labelled thread samples. Its
closed raw `samples.jsonl` SHA-256 is
`8f1f8fc727367b322949ba3ced23e289b85188fb8534f200ad45c8aecb1fb7fb`.

## Frozen runtime04 result, 2026-10-18

A separate run on native SHA-256
`4e26eb857becb40324cd72a1b00ffc1812c5490e743c5655de85c452e9e8eb07`
and its frozen runtime04 backend/driver measured 03:27:47–03:57:47 UTC
(1,800.000141 seconds). It used the normally constructed saved fixture:
500 active buses in eight companies, 50 browser-protocol viewers and four
visual clients sharing four company views. Three independent one-bus sentinel
worlds brought the total to four engine/backend groups.

| Recorded measurement | Result |
| --- | --- |
| Declared harness verdict | `TARGET_PASS`; all 18 unchanged checks pass |
| Gameplay admissions and ledger completions | 1,800 / 1,800; p95 ACK 73.3 ms |
| Chat admissions and exact viewer delivery pairs | 1,800; 90,000 / 90,000; p95 delivery 199.3 ms |
| Measured steady HTTP/client errors; sampling gaps | 0; 0 |
| Active heads | 500 throughout all 360 steady samples |
| Primary minimum completed five-second speed ratio | 0.99465 |
| Sentinel minimum completed five-second speed ratios | 0.99481 / 0.98243 / 0.97801 |
| Conservative whole-interval speed lower bound | 0.99168 in each world |
| Sampled simultaneous SSE connections | Minimum 37; below 50 in 8 of 360 steady samples |

The viewer check proves 50 distinct clients and every accepted message/viewer
pair, not 50 simultaneous streams throughout. Tick brackets were valid, with
1,439 corroborated windows and one short, timing-inconclusive edge; unobserved
boundary minima remain unproven. Native queue depth and actual send/slot
saturation remain uninstrumented. The separate unread-PNG fault offered
24,912,768 response bytes and observed continued action/API/tick progress;
it does not prove saturated backpressure. Parked-frame freshness and the
four-renderer scope above still apply.

The frozen benchmark manifest is
`/tmp/openttd-load-final-runtime-kyvwldir/benchmark-runtime-manifest.json`
(SHA-256 `0dbb62ca4c2c632ee5dbeb49857ee91f348609735b235f7f6dea81cb1a7cb073`).
Raw results and the authoritative audit are in
`/tmp/openttd-load-runtime04-rpcbrackets-38979-20261018`.
Independent review verified unchanged frozen inputs and preserved historical
evidence; the final attestation is
`/tmp/openttd-load-runtime04-final-attestation.json`
(SHA-256 `671c2c3cc978345002a1c4540aed68e1b192e78d3f4aa35d4162fb1f6ac9866c`).
Concurrent Luna, permanent-world and private browser-QA activity is recorded
separately; this is no isolated comparison or explanation of the historical
failed windows. Those failures retain their original verdicts. The benchmark
used its earlier frozen frontend assets and protocol clients; it does not
validate the later `c144f07853` playable browser UI or close the Luna gameplay gate.
