# URL-only Luna trial 05

**Ended early at the user's request after about 81 minutes.** Both text companies
produced transport income; neither capture company delivered cargo or earned
transport income. The text competitor had a positive operating subtotal; the
text cooperator still had an operating loss. Capture business acceptance and full
gameplay parity remain unproved.

The operator observed the stop request at **17:45:28 UTC**. That is the analysis
cutoff, not a claim of instantaneous shutdown: stop signals were sent at
17:46:05.682 UTC and the trial ports were verified closed by 17:46:39 UTC.
The server, sampler and players are stopped. The proposed fresh demo was cancelled
while improvements are reviewed; the original overnight world remains stopped.
No gameplay implementation followed this analysis request.

| Assigned company | Transport income | Delivered cargo | Operating subtotal |
| --- | ---: | ---: | ---: |
| 0, text competitor | 25,219 | 3,230 | +6,229 |
| 1, capture competitor | 0 | 0 | −15,571 |
| 2, text cooperator | 8,522 | 3,914 | −9,438 |
| 3, capture cooperator | 0 | 0 | −14,062 |

These are observed native accounting units. Operating subtotal includes interest
and running costs, while capital spending and Other costs are separate; every
company's cash fell and its loan remained 100,000. Quarter and annual ledgers
agree for all four companies, with no gaps, conflicts, or engine change. The last
credited reports arrived at 17:45:14.533–17:45:17.854 UTC, before the selected
cutoff; the baseline reports also have distinct earlier observation times.
This is an observed interval, not an atomic START/cutoff reconstruction.

All 652 scheduled sampler reads completed; four after the early analysis cutoff
are retained but excluded, leaving 648 credited reads. Public action logs contain
41/68/26/84 distinct pre-cutoff action IDs for companies 0/1/2/3. These are local
POST-log evidence, not a certificate that no uninstrumented submission occurred.
The two capture hosts received 115 and 181 verified image events, respectively,
all matching their saved native PNGs. Delivery does not establish interpretation.

World and company-direct chat worked, with one outsider-isolation check recorded
below. Road contributions and vehicle movement along part of the proposed
corridor were demonstrated, but loaded use of the partner road and a complete
paid shared service were not established. The capture competitor did not establish paid competition. Company
0 extended its route and added two buses after first income, with subsequent
paid operation; company 2 added capacity but remained loss-making.

See [the improvement proposal](luna-trial-05-improvements.md) for the joint
root/peer analysis and the independent architecture review. This record keeps
native/receipt facts separate from actor explanations and uncertain causes.

The world is `url-luna05`: 128×128, seed 20261018, temperate, starting year 1950.
Runtime source is `1b62c3f95b`; native executable SHA256 is
`eada3a7bbe726c1feeeba7f8bbc317d410841ea2176d1b91da1efa45eb013db1`.
The original overnight world remained stopped.

The common budget began **2026-10-18 16:24:44.521172 UTC**, before agents spawned;
reading the guides, downloading the client and joining count toward it. The
original planned mutation cutoff was **17:54:44.521172 UTC**; the user
ended the run earlier as recorded above. Each company may submit at most
500 distinct gameplay IDs, including rejected and UI-only attempts. Exact retries
with unchanged bodies count once. No grants, operator-built routes, resets or
deadline extensions are part of this trial.

| Company | Assigned goal | Read mode | Coordinator |
| --- | --- | --- | --- |
| 0 | Compete for an overlapping passenger market | Text | Root |
| 1 | Compete for that market | Native images | Peer |
| 2 | Cooperate on useful shared road infrastructure | Text | Peer |
| 3 | Cooperate on that infrastructure | Native images | Root |

All four use fresh `gpt-6-luna` contexts. They receive `/llms.txt` or
`/llms-capture.txt`, goals, scope and budgets. No checkout, operator credential,
fixture route or saved game is a player starting material. Admission allocates
preprovisioned companies; immutable launcher display names need not match trial
roles, so players announce and discover roles through game chat.

Both capture players may use typed gameplay actions and explicit actuator
references. Their world perception must use actual host-displayed native images;
paths and successful PNG downloads alone are insufficient. Text players may use
public structured observations. Communication uses world and company-direct
chat. Useful shared-road operation and paid overlapping competition are goals;
mere messages and simultaneous unrelated services do not establish them.

Each company must establish transport income, make a durable business change
afterward, and demonstrate subsequent operation and revenue. Renaming alone does
not qualify. Income, delivery, operating subtotal and capital/cash/debt movement
will be reported separately; company totals do not prove marginal route profit.

Four operator-only public `company.report` baselines completed before START. A
separate read-only sampler recorded all four companies every 30 seconds, with at
least 1.1 seconds between starts. Its reports never feed player strategy.
Queries and responses are paired; failures, accounting gaps and restarts remain
explicit. Actual model-host image outputs and exact action/chat receipts are
separate evidence sources.

Local evidence root: `/tmp/ottd-url-luna05-audit`; early-stop finances are in
`/tmp/ottd-url-luna05-early-final-finance/finance.json`. The independently checked
early-stop numerical review is `/tmp/ottd-luna05-early-result-review.md`.
Earlier trial outcomes are unchanged; URL-only joining alone does not establish
gameplay parity.

## Recorded protocol assistance

At approximately 16:35 UTC, both root-owned players reported holding construction
because `pause.command_policy` was `no_construction`, although `pause.paused` was
false. The guide said to honor the policy without explicitly saying it applies
only while paused. A public status read confirmed an unpaused world with no pause
reasons. Native command admission checks this policy only when a pause mode is
active (`src/command.cpp:212`).

Both coordinators were asked to give all four players the same neutral protocol
clarification: the configured policy does not block unpaused construction; read
current status and use normal native checks. No world setting, route, funding,
budget or cutoff changed. The original URL-only unaided interpretation gate is
therefore **qualified**, even if later business operation succeeds. The corrected
guide/schema wording and its review are described in
[entrypoint validation](agent-entrypoints.md). This trial retains its original
frozen assets and the assistance record.

## Thirty-minute checkpoint

The scheduled cycle at 16:54:44 UTC produced four paired, non-atomic native
reports. All 244 scheduled reads through that round succeeded, with no runtime
restart. Retained quarterly transport totals were:

| Company | Transport income | Delivered cargo units |
| --- | ---: | ---: |
| 0, text competitor | 784 | 326 |
| 1, capture competitor | 0 | 0 |
| 2, text cooperator | 2,736 | 1,249 |
| 3, capture cooperator | 0 | 0 |

These are native accounting units, not net profit or exact simultaneous cutoff
totals. Baseline transport income was zero. Both capture players reported working
interfaces; reachability and an empty service remained business problems under
investigation. No route repair or strategic assistance was supplied.

A finite operator-only read also checked an existing direct-company thread for
companies 0/1: outsider company 2 was denied with `404 thread_unavailable`.
No private message bodies were retained by that test. An initial test assumption
of 403 was corrected to the API's deliberate 404 concealment. This checks that
one API path; broader native/UI isolation is not inferred from it.

Evidence: `progress-30min.json` and `private-thread-isolation.json` under the
local evidence root. At this checkpoint the trial was still running.

## Sixty-minute checkpoint

Cycle 120 completed at 17:24:44–17:24:47 UTC. All 484 scheduled reads through
that round succeeded, with the same native engine instance. These non-atomic
reports retain the following quarterly transport totals; they are not net profit:

| Company | Transport income | Delivered cargo units |
| --- | ---: | ---: |
| 0, text competitor | 17,145 | 2,260 |
| 1, capture competitor | 0 | 0 |
| 2, text cooperator | 5,734 | 2,630 |
| 3, capture cooperator | 0 | 0 |

The original cutoff and budgets remain unchanged. Evidence:
`progress-60min.json`.

## Perception and supervision qualifications

The capture cooperator reported receiving partner message
`e2c54fc6-336c-458d-b18c-49b89e6edf70` containing structured text-map data.
It said it did not use those data for an action and requested image-grounded
descriptions instead. That non-use is a player claim, not independently proved
absence of influence. The record remains explicit in
`capture-chat-qualification.json`. The peer also identified another map/ownership
statement, `1291aa0d-5903-4ded-9876-a7ac22aaadd6`, at 17:24:53 UTC; the non-use
claim above concerns the first message, not independently verified absence of
influence from all received messages. Ordinary voluntary in-game advice remains
part of the communication test. The peer review preserves both disclosures.

At approximately 17:25 UTC, the peer reported that the capture competitor ended
its neutral status turn with a final answer. The peer reports resuming the same
actor with the original objective, state, deadline and budget. This was turn
supervision, with no reported strategic advice, new actor, reset or time extension. Evidence:
`supervision-events.jsonl`.

An independently reviewed image extractor now requires bounded full image
decoding and deduplicates identical recorded output events. Its earlier
header-only sample is superseded. The 17:20:41 UTC capture-cooperator sample
contains 126 verified host-image events, 125 distinct images, all exact native
PNG matches. This establishes delivery to the model host, not interpretation or
a successful business. Final early-cutoff counts are reported above; the
cooperator has 179 unique matched hashes and the competitor 107, with repeated
legitimate deliveries counted separately.
