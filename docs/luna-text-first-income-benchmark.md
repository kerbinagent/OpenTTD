# Public text guide: first transport-income benchmark

Two fresh, unaided `gpt-6-luna` text players each reached independently observed
transport income and delivered cargo in about **2 minutes 25 seconds**, including
guide reading, client download and joining. Both then added a second vehicle and
observed further operation and income. Neither established positive company profit.

This is a two-run observational reference, not a controlled estimate of the
guide's benefit or a reliable timing distribution. No capture player was tested.

## Result and capture target

| Run | Public company | All-in START, UTC | First independent positive reply, UTC | Observed elapsed | Conservative witness bracket |
| --- | --- | --- | --- | ---: | ---: |
| 1 | `text-first-income-01`, company 0 | 18:40:42.317201 | 18:43:07.334853 | 145.018 s | 140.000–145.018 s |
| 2 | `text-first-income-02`, company 2 | 18:41:06.569828 | 18:43:31.593381 | 145.024 s | 140.000–145.024 s |

All dates are 2026-10-18. Run 1's first witness is native tick 10,714, transport
gross 9 and delivered cargo 14. Run 2's is tick 11,263, gross 55 and delivered
cargo 25. These are native accounting units. The actor's own first positive read
can be later; it does not replace the independent witness.

The actor public-I/O clocks also separate onboarding and execution. These are
wall-time seconds from the same START, not times subtracted from the result:

| Phase | Run 1 | Run 2 |
| --- | ---: | ---: |
| Join verified by `/v1/me` | 28.236 s | 25.635 s |
| First successful construction receipt | 75.247 s | 73.448 s |
| First vehicle start receipt | 101.784 s | 92.581 s |
| Actor's own first positive report | 156.904 s | 147.136 s |

Applying the requested **1.5–2 times** target to each observed text result gives
217.526–290.035 seconds and 217.535–290.047 seconds. A practical provisional
capture target is therefore about **3 minutes 38 seconds to 4 minutes 51 seconds**,
rounding the upper end upward. Use the slower observed run for the working bound;
do not silently select the fastest. This is an aspirational target for a future
matched capture benchmark, not an achieved result or a claim about model speed.

The witness is the first retained report showing positive native transport gross
and delivered cargo after a verified zero baseline. It is **not the exact payment
event**. The conservative bracket begins at the previous nonpositive report's
request start and ends at the positive response completion. Here "nonpositive"
means not yet satisfying both conditions: run 2 already reported five delivered
units with zero gross at its preceding sample. A negative response's
completion would be an unsafe lower bound: payment could occur after that report's
snapshot but before its reply arrives. Elapsed time uses the shared host's
monotonic clock; UTC timestamps also remain in the raw records.

## What changed in the public guide

The reviewed text guide is committed as `9133befaa0`. It retains the same public
URL, version-matched client, bounded ZIP bootstrap, private claim recovery and
saved-request semantics. No engine, client or gameplay API was added.

The fast path moves the unpaused-policy interpretation before construction,
combines initial reads in one sequential Python invocation, prints a compact
capability summary, and defers reference sections until relevant. It explains
using existing terminal waits, creation references and order preparation instead
of extra host round trips, rediscovery and hand-copied version tokens. It also
surfaces shared-order, default/no-op and facility-ownership mechanics.

The model still chooses its business, transport, sites, vehicles, capacity,
financing and changes. There is no supplied route, station site, fixture service,
operator repair or new strategic helper. The guide grows from 2,212 to 2,728 words;
that reading-cost tradeoff remains explicit. Faster total timing here does not
prove which wording or reduced round trip caused it.

## Reproducible scope

Each player received only its actual `/llms.txt` URL, an independent-business goal,
scope and budgets in a fresh `gpt-6-luna` context. Requested model is known; effort
was not overridden and was reported unknown. Each could attempt at most 500
distinct gameplay IDs, including rejected attempts, with a 30-minute hard limit.
Exact retries count once. Reading, spawning, bootstrap and joining count from START.

Two separate `luna` profile worlds used the same 128×128 map seed 20261018,
temperate landscape and starting year 1950. Only one player was active per world;
the remaining preprovisioned accounts/renderers were idle and unclaimed. Public
admission independently allocated company 0 and company 2. The actors did not
share a map or affect each other's roads, facilities or market.

Both servers ran from an immutable source snapshot of
`9133befaa0b947d52b619c9d3a5d52240a4dff7b`, with identical served guide and client:

| Artifact | SHA256 |
| --- | --- |
| `/llms.txt` | `48e43232bc3280988de6ee19c7d4e70a13de512482f8f43e6efdd79e31acac2a` |
| `/agent/client.zip`, 364,064 bytes | `12e02c6ea66617c5c14bfbab698a72b941f46420c4d1961f558ecb81671f4434` |
| Existing native executable | `eada3a7bbe726c1feeeba7f8bbc317d410841ea2176d1b91da1efa45eb013db1` |

The native binary's linked revision was reported as `e386adfdfa`; generated source
revision text is not used to override that binary identity. Matching adjacent
language/base resources were frozen with it. Python was the existing local ASGI
environment. The host exposed 64 CPUs under WSL2; two benchmark worlds ran
concurrently with the lead's separate 512×512 web world and compilation/QA.
This concurrency is a benchmark condition, not an isolated-machine measurement.

One pre-START launcher attempt failed because the copied executable lacked its
adjacent language packs. That failed state/log is preserved. Matching resources
were copied before either benchmark START or player admission; this setup failure
is not hidden as a successful launch and is not subtracted from an active timer.

## Observation and outcome limits

An operator used only public `company.report` reads, with private operator
credentials kept out of player prompts and reports. Both possible text slots had
verified zero transport income and delivery baselines before START; the actual
slot was matched by complete native company handle and engine instance. The top
level report `company_id` can describe caller context; target identity comes from
the requested company and native company handle.

After each player reported its public binding, its sampler ran every nominal five
seconds. The initial sampler-start gaps were 49.12 and 55.47 seconds after START;
those are not credited as zero-cost onboarding. The first retained scheduled
reports were nonpositive, so the successful witness brackets above are much
narrower than those initial gaps. The zero-baseline-to-START gaps were 29.32 and
50.21 seconds; observed-ready-to-START gaps were 30.50 and 54.71 seconds. Native
dates/ticks are retained separately because wall time and game time differ.

The final retained sampler files contain 53 and 46 reports, with no query failures.
Each report retains request/completion times, target, native tick and accounting
periods. Repeated snapshots are not summed. Loan principal, sales and asset value
are excluded from transport gross. Raw failures, incomplete tails and later
records would remain explicit rather than becoming invented income evidence.

Each actor recorded 12 distinct gameplay attempts: 11 completed, one ordinary
stop-placement rejection, no observed admitted request with unknown terminal
state. Both used existing depot auto-facing successfully. Both made their capacity
change after positive income and then observed additional delivery/income; this
does not prove that all subsequent income was caused by the added vehicle.
The company's observed income-minus-expenses remained negative in both runs.
For example, the later actor reports showed income/delivered 42/69 with expenses
600 in run 1, and 199/91 with expenses 567 in run 2; these are distinct native
snapshots. Saved receipts and later observations prove a retained operating
change, not crash/restart durability. Run 1's original actor summary paired one
timestamp with the wrong native tick; its public-I/O record supplies the correct
pairing and its missing manual first-income timestamp. The independent audit
preserves those corrections without rewriting the actor account.

The players were allowed to finish when both goals had evidence. They did so
before the hard deadlines (19:10:42 and 19:11:06 UTC); the operator then stopped
only the two benchmark samplers and worlds. Their four ports were confirmed closed
at 18:46:02 UTC. The separate human/web world remained running. No deadline was
extended and no failed run was replaced by a favorable rerun.

The previous four-company trial's 13m32/23m30 text witnesses included pause-policy
confusion, different goals/roles, shared-world interaction and different load.
Keep those full measured times; do not subtract the misunderstanding or describe
this as a controlled before/after speedup. A future capture comparison should match
the start, witness, map/rules and observer method and disclose host concurrency.

## Validation and local evidence

Independent architecture PRE and guide POST reviews passed. The audited bootstrap
remained byte-identical; Python 3.10 grammar and offline startup/order-composition
checks passed. The operator sampler retained its tested spacing, no-catch-up,
cutoff and private-output behavior, with the CLI generalized only to accept the
actual selected company count. A pure offline witness reducer cross-checks the
timing against retained reports and fake-data cases; actor receipts and later
operation are reviewed separately.

Local evidence roots are `/tmp/ottd-text-first-income-benchmark01-audit` and
`/tmp/ottd-text-first-income-benchmark02-audit`; the frozen runtime/setup/stop record
is `/tmp/ottd-text-first-income-runtime`. Raw actor accounts are preserved, with
independent audits correcting any timestamp/tick pairing rather than rewriting
their original reports. No credential or raw model reasoning is exported.
