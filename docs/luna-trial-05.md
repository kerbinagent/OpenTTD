# URL-only Luna trial 05

**In progress. Business acceptance has not yet been assessed.** This fresh trial
tests whether agents can start from a public guide URL and their goal, then
operate paying businesses through text and native-image observations.

The world is `url-luna05`: 128×128, seed 20261018, temperate, starting year 1950.
Runtime source is `1b62c3f95b`; native executable SHA256 is
`eada3a7bbe726c1feeeba7f8bbc317d410841ea2176d1b91da1efa45eb013db1`.
The original overnight world remained stopped.

The common budget began **2026-10-18 16:24:44.521172 UTC**, before agents spawned;
reading the guides, downloading the client and joining count toward it. The
latest mutation cutoff is **17:54:44.521172 UTC**. Each company may submit at most
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
separate read-only sampler records all four companies every 30 seconds, with at
least 1.1 seconds between starts. Its reports never feed player strategy.
Queries and responses are paired; failures, accounting gaps and restarts remain
explicit. Actual model-host image outputs and exact action/chat receipts are
separate evidence sources.

Local evidence root: `/tmp/ottd-url-luna05-audit`. Final outcomes and remaining
interface failures will replace this progress status after the bounded trial.
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
therefore **qualified**, even if later business operation succeeds. Guide/schema
wording is being corrected for subsequent fresh sessions; this trial retains its
original frozen assets and the assistance record.

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
local evidence root. The trial continues toward its original maximum cutoff.
