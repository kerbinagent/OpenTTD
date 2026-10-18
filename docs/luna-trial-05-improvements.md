# Trial05: findings and proposed improvements

**Proposals for user review; no implementation or new simulation has started.**
The user stopped the trial after about 81 minutes. Root and the Parler peer audited
the four companies separately, checked receipts and relevant native screenshots,
and challenged actor explanations against native source. An independent agent
reviewed the proposed architecture before implementation.

[The outcome record](luna-trial-05.md) has finances, scope and qualifications.
Both text companies earned transport income; only the text competitor had a
positive accumulated operating subtotal. Neither capture company delivered cargo
or earned transport income. World/private chat and mechanical depot auto-facing
worked. Full paid cooperation/competition and capture business acceptance did not.

## What explains the capture difficulty

The evidence points to a combination of stale visual feedback, difficult native
UI interactions, generic errors and gameplay mistakes. It does not establish that
model speed alone caused failure. C3's logged HTTP capture 95th percentile was
about 201 ms and selected-world 188 ms. These client completion measurements include
more than server work and exclude model thinking and image interpretation.
C1/C3 received 115/181 actual host image events; image delivery itself worked.

| Finding | Concrete evidence | Meaning and limit |
| --- | --- | --- |
| A desktop read could show pre-action pixels | C3 image `bed73...` showed Stopped before a typed start at 17:21:41 and was returned again at 17:21:52; a distinct 17:22:07 image showed movement. | Legacy interaction capture reuses its parked snapshot. A successful typed action and a later capture call do not guarantee a newer frame. This is independent of whether the route was sound. |
| The UI sequence sometimes did something different from the intended order edit | C3's named GoTo control armed the tool; a later raw click could disarm it. The subsequent image opened station information and still showed the original two orders. | UI workflow completion is not proof of a changed order. Initial typed inserts worked for the same company. |
| Error formatting lost useful native context | C3 received `(invalid parameter) local authority refuses to allow this`; C1 received `owned by (invalid parameter)`. The native parameter encoder is conditional on a local company. | Demonstrated feedback loss, not evidence that those action parameters were malformed. Preserve the actual native refusal. |
| Input errors were too generic | C3 estimated a diagonal `road.build` three times; each returned generic `estimate_unavailable`. | Axis alignment can be explained from the request alone. C3 never submitted a typed long road; GUI partial-build behavior does not explain its route. |
| Native rules were misunderstood | Company-shaped handles were submitted to native-human authorization; C2’s third-order experiment edited a shared list affecting both cloned buses; C1 requested the already-default load option. | Ordinary-road access is not gated by company joining. Shared orders affect both buses. Native no-op rejection and an unselected/disabled menu are not loading-system defects. |
| Some diagnosis remains uncertain | A successful stop estimate was followed 11.36 seconds later by authority refusal. C1 had four legacy capture failures followed by successful reads. | Estimates reserve neither permission nor funds. No rating timeline or capture-failure root cause was established; do not label either an explained engine bug. |

C1's auto depot estimate rejected one site, then chose direction 0 at the actor's
new site and built successfully (`gt-depot-1`,16:40:19). The helper removed facing
micromanagement while leaving site choice to the model. C3's shared-road pieces
and C2's reciprocal contribution succeeded, but neither full intertown arrival
nor a loaded paid shared service was established.

## Recommended first batch

### 1. Fresh desktop observation using the existing observation protocol

Add one read-only desktop variant to the existing selected-observation family.
A concrete proposed surface is:

```http
POST /v1/view/observe/desktop
```

```json
{"observation_generation":"<gateway-issued observation context generation>","observation_request_id":"<client-generated request identity>","after_request_id":"<own completed action>","wait_ms":1500}
```

This is a schematic wire body. The client copies the generation from capabilities
and generates a new request identity using existing formats; model-facing options
are only the optional action barrier and wait budget. Reuse existing request identity/recovery,
run/company ownership and observation-context generation, PNG transport, cache, causal-state feedback and bounded
202 pending behavior. The portable client can expose `selected desktop`; existing
action-plus-observation composition can use this variant without a second action
journal. The result contains genuine current desktop pixels and workflow
metadata. It grants no click/inspect proof; gestures still need an interaction
snapshot.

Keep the configured desktop dimensions and existing pixel/byte bounds in this
first slice. Advertise support only for a renderer implementing the new method;
never silently fall back to the old parked capture.

The internal sequence matters:

1. Under the existing worker coordination, detect a captured/persisted/committed
   UI intent. If one is active, return bounded pending/busy and preserve it.
2. When only an unused interaction snapshot is parked, deliberately abandon that
   snapshot/proof and invalidate the backend's matching latest-image record.
   Preserve windows and native UI state; do not cancel an admitted action.
3. Pin a private catch-up watermark for this observation using the known replay
   head and any explicit action barrier. Release parking **before** waiting for
   replay, then yield to the native loop. Redrawing in the same IPC turn can
   otherwise produce old pixels under a new ID. Do not chase a moving head.
4. Once replay satisfies that watermark, draw/copy the real desktop and return
   without parking it again. Reuse existing observation records and
   deadlines. Paused/static games may legitimately produce identical pixels.

Expose whether a legacy interaction image was reused and its original snapshot
identity/creation or remaining proof lifetime when available. Do not call the
gateway's first-seen time the native image age or renew a lease on download.
Never automatically recapture and replay an old coordinate click.

**Why this first:** it helps already-open vehicle/orders/station/town windows
through one lifecycle change. Four separate panel adapters would optimize
framing but leave the shared parking problem to solve. Selected native-info
adapters for vehicle, orders, station and town/local authority can follow if the
fresh desktop still costs too many navigation or image-reading round trips.
A universal window registry or a second capture framework is unnecessary.

### 2. Useful direct command feedback

Preserve native formatting arguments for integration-owned receipts, or use an
honest bounded fallback when formatting context is unavailable. Do not change
`_local_company` globally, infer a town from a later query, or turn a missing
format argument into a claim of invalid input. Cover both town and owner messages
with a focused non-local-company regression.

For input-derived failures, return a finite reason such as axis alignment rather
than merging malformed requests, temporary admission and native gameplay refusal
into the same generic error. Do not expose world attributes or invent a missing
road edge. Where the native path positively identifies an already-present
setting, explain it; preserve other native refusals and the existing no-op
rejection semantics. Do not infer that every generic order-modification error is
a no-op or silently turn rejections into success.

Keep estimates advisory. The public `test_and_execute_may_differ` flag describes a
native command trait, not immunity to subsequent world changes. Clarify or
simplify its player-facing presentation; a successful quote is not a reservation
of permission, state or cash. Investigate C1's four recoverable capture failures
with existing logs before choosing a fix; keep bounded read recovery separate
from any retry of an uncertain gameplay action.

### 3. Finish the inexpensive mechanics and identity guidance

The source guides already fix pause-policy ambiguity and native-human joining
versus ordinary-road access. Add the remaining concrete examples:

- Clone with `share_orders:true` deliberately shares later edits; inspect the
  returned shared-list evidence before editing.
- Reuse own successful placement/creation records for known station targets and
  current opaque tokens for new edits. Do not guess another visible station's
  coordinates or assume its ownership.
- The named GoTo control arms the tool; clicking its raw button again can toggle
  it off. After a gesture, verify the order list using fresh pixels.
- Verify reachable arrival, waiting/loading and paid delivery before claiming a
  service; compare operating results separately from capital/cash/debt.
- Use neutral public slot labels rather than immutable cooperator/competitor
  names. Admission and task roles were allocated differently in this trial.

These examples explain mechanics without choosing a market, route, station,
vehicle, financing or authority action for the player.

## Later candidates, with complexity kept explicit

| Candidate | Smallest useful shape | Decision for this review |
| --- | --- | --- |
| More direct native panels | Add vehicle/orders/station/town adapters to the existing native-info allowlist, reusing the observation lifecycle. | After fresh desktop QA, measure whether navigation/legibility still warrants them. No universal panel framework. |
| Station-reference order destinations | An optional full authoritative station reference for `order.insert`, alongside current tile syntax; retain generation, ownership and facility checks. Existing build feedback/control references may supply the reference. | A plausible small composition improvement, but current initial orders succeeded from saved coordinates. Retest before claiming it is required. |
| Name-based station targeting | One caller-chosen exact name resolved on the authority, reject ambiguity, return only an actuator reference. | Defer until an established-target selection gap is demonstrated. Do not add it together with a second competing resolver by default. |
| Inspect-to-reference targeting | Reuse an actual native inspect hit test with explicit source/incarnation fencing. | Not a cheap metadata addition: worker and authority instance IDs/generations differ. Never stamp an authority boot onto a replica handle. Read-only map images remain observation-only. |
| Single-tile stop auto-facing | Opt-in native-legal orientation at the caller's chosen site/type/station; small semantic fence. | Independent minimal PRE exists. No wider multi-lane search or automatic site selection. |
| Depot desired state | Explicit service/halt/cancel through one native enum command; preserve legacy toggle guards, do not restart on cancellation. | Independent PRE exists, but this trial does not prove depot toggling caused the failed service. Lower priority than freshness. |
| Reciprocal-road helper | Pure conversion for caller-chosen adjacent tiles, with separate native outcomes, or a documented existing straight-build recipe. | Prefer this to a route planner, automatic repair or generic multi-action transaction. It cannot prove network reachability. |
| Route feasibility diagnostic | Bounded check for an explicitly chosen vehicle/source/target. | Defer: route-search bounds, stale-state semantics and capture information policy need a separate design. Do not promise a uniquely missing edge from generic pathfinder failure. |

There is no need for a new road-access grant, automatic unsharing, longer leases
as the primary remedy, automatic authority-rating repair, or a strategic route
planner. Ordinary road use already works; longer parking can worsen stale
feedback. Keep those decisions with the player.

## Validation before another business trial

First use isolated mechanical QA: typed start/order edit followed by a fresh
read; fresh read without an action barrier; active UI intent preservation;
old/new image-purpose rejection; unchanged pixels while paused; window/armed-tool
preservation; and correctly formatted native refusal. A new observation ID alone
does not prove a newer source frame. Verify actual host image output.

Then run a short fresh text+capture Luna test, with the same financial criteria,
explicit mode/role labels, preserved attempts and no operator route rescue.
Measure onboarding, HTTP/native completion, model/tool round trips, actual image
inputs, loading/delivery and period-based operating results separately. Require
post-change operation after first income. Add paid shared-road cooperation and
overlapping paid competition only after the basic capture business gate works.
Keep this failed trial and its assistance/exposure qualifications visible.

## Review evidence

Local audit artifacts:

- `/tmp/openttd-luna05-peer-design-review.md` and independent C1/C2 mechanical reviews.
- `/tmp/ottd-luna05-c3-construction-analysis.md` and its authority-source companion.
- `/tmp/ottd-luna05-orders-targeting-analysis.md`.
- `/tmp/ottd-luna05-early-result-review.md` and the early-final finance/IO/image artifacts.
- `/tmp/ottd-luna05-improvements-architecture-review.md`.

These are analysis/design deliverables. The proposed gameplay/capture changes
remain unimplemented pending the user's review.
