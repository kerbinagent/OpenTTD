# Local integration validation

Validation date: 2026-10-18. Requirements: `openttd_requirements.docx`, version 0.1. This records actual local checks; open gates remain explicit until their evidence is complete.

## Expanded acceptance criteria

The requested end state is semantic parity with normal human company gameplay: the same playable outcomes and decision-making information, without requiring identical GUI gestures. The [current capability matrix](parity-capabilities.md) records implemented source families, observations, permissions, composition and exclusions. The historical MVP checks below establish their foundation; neither command counts nor family implementation establish full parity.

The gameplay acceptance exercise uses four fresh Luna agents in separate companies in one ordinary economy: two use structured public observations, and two examine the world through genuine public PNG captures. All four can use the same typed action API; native GUI input is optional for capture players. Ordinary action feedback and finite target-reference/version metadata are available to both modes, while structured world readers remain forbidden to capture players. Agents choose their own markets and routes. Cross-mode prompts encourage cooperation through shared infrastructure and competition in an announced market, with world and company-direct conversations through public chat. Operators supply no route/target plans, economic grants, gameplay actions or hidden-state relays.

The earlier trials below imposed an additional GUI-only restriction on capture companies. That was an implementation and trial-contract mistake, corrected after the user's clarification. Their failures remain recorded, but do not establish that image perception with typed API actuators fails. The corrected shared-actuator snapshot passed 192 native cases, 12 native live cases and 248 backend tests. Its public capture-mode HTTP fixture passed without a renderer configured, exercising typed construction, orders, native feedback, stale references and exact retries while structured observations remained forbidden. Evidence: `/tmp/openttd-shared-capture-public-http-token-case-tests.log` and `/tmp/ottd-live-jmhbwrom/shared-capture-result.json`.

Four fresh Luna companies ran the corrected trial from 2026-10-18 00:59:04.229076 to the unchanged 02:29:04.229076 UTC cutoff on frozen runtime `/tmp/openttd-luna-runtime-03/runtime-manifest.json`. All four had the same 103 typed gameplay actions; two inspected structured observations and two inspected genuine PNGs. Independent audits found real transport revenue and post-income expansion for both text companies, but zero revenue for both capture companies. Direct partner chat reached all four clients; reciprocal paid road use and two operating competitors were not established. The overall exercise failed. The [trial record](luna-trial-03.md) separates accounting, actual image use, protocol qualifications and the disconnected-depot diagnosis. Source work after this freeze did not alter its runtime.

The following source checkpoint adds native advisory action estimates, vehicle status, cargo-payment samples, bridge choices, selected-engine purchase information and selected-spec station-site previews up to native extent 64. Native executable SHA-256 `64e813079ca38bb0e7e584c78e9121b93d954832a39c148608a73559fba1125c` passed 212 cases / 111,450 assertions, 12 live tests in 126.426 seconds, and the public capture-mode route test in 13.970 seconds. The actual exported catalog has 104 actions / 59 queries with BLAKE2b-256 `065c3a37efdf8569c2ab063bdc12ebe0257d860af6b11ebb7a9fbd09144ff4a5`; the matching backend passed all 260 tests. Evidence: `/tmp/openttd-decision-estimate-station-native-final-tests.log`, `/tmp/openttd-decision-estimate-station-native-live-tests.log`, `/tmp/openttd-decision-estimate-station-shared-capture-tests.log`, `/tmp/ottd-live-h2e4pcv1/export-result.json` and `/tmp/openttd-decision-estimate-station-backend-final-attestation.json`. Initial new-test fixture failures remain preserved in their earlier logs; production behavior was not changed to satisfy those fixtures.

Nine changed native station/callback files also passed integration-disabled private compilation with no unresolved integration symbols; the pristine executable remained unchanged. The maximum house-callback test exercised 1,048,576 native probes in approximately 1.45 seconds, which establishes no universal observation-latency bound. The corrected Luna trial used its earlier frozen snapshot throughout.

The next validated checkpoint adds shared pause metadata and notifications, native-policy actions while paused, and recipient-scoped custom GameScript news. Native executable SHA-256 `c6742cbc53fc6eb86785903f24c3382d6333c2a5b5a96435bf30056add93493e` passed all 231 cases / 115,504 assertions and 12 native live tests in 139.183 seconds. The 19 focused pause/news/formatter cases passed 4,057 assertions; that combined process used 0.26 CPU seconds and 57,344 KiB peak RSS, with explicit admission waits included in its 11.81-second wall time. These are fixture measurements, not per-render bounds. The matching actual catalog has 104 actions / 60 queries with BLAKE2b-256 `0652cacfe54fd836b41803610e2dd986c324076f7faf3395a820f9bfbe0b0369`; all 276 pinned backend tests, 19 browser tests and 23 HTTP/ASGI tests passed without skips. Evidence: `/tmp/openttd-pause-news-native-final-attestation.json`, `/tmp/openttd-pause-news-native-live-tests.log`, `/tmp/ottd-live-2ix_w_c3/export-result.json` and `/tmp/openttd-pause-news-backend-final-attestation.json`. One initial native news expectation omitted the canonical tile index; its fixture-only correction and original failed run remain recorded.

The isolated public pause fixture passed in 35.184 seconds on the same binary and backend, without a renderer. Both modes read current pause state and notifications; real SSE subscriptions received pause and resume transitions. Under the fixture's saved `no_construction` policy, company settings and vehicle management executed, construction was rejected, an advisory estimate inserted no action or sequence, and a completed periodic checkpoint published while paused. Exact rejected retries retained their outcome after checkpoint and resume; a deliberate new request succeeded after resume. Evidence: `/tmp/openttd-pause-public-saved-policy-live-tests.log` and `/tmp/ottd-live-mt4vpjso/shared-capture-result.json`. An earlier fixture correctly failed because it tried changing the native `NoNetwork` pause policy during multiplayer; that attempt is preserved separately. All four policy levels are covered by native tests, not four live multiplayer policies. Queued-before-pause publication, renderer input, process restart, event overflow and automatic stream recovery are outside this public fixture's scope.

A separate native queue fixture passed in 3.568 seconds. It observed a construction request queued before pause and still queued after its paused baseline, then rejected with `world_paused` before publication. Paused cash, loan, income, expenses, infrastructure and target tile remained unchanged; permitted company renames completed while paused. Exact rejected retries remained terminal before and after resume; one fresh identity subsequently constructed one owned depot with positive native cost, and its retry created no further depot. Running cash deltas were not used to measure that resumed construction fee. Evidence: `/tmp/openttd-pause-native-queue-live-tests.log` and `/tmp/ottd-live-5mrmqwxm/pause-queue-result.json`. This opt-in fixture fails if its finite race window is missed and establishes no scheduling bound.

An isolated genuine-PNG renderer proof also passed on the frozen `c6742cbc…` pause/news snapshot. While the worker reported `no_construction` pause, typing a company name captured zero native commands; clicking the freshly observed OK button captured exactly one completed, dispatched and acknowledged `RenameCompany` command. The paused simulation tick, company balance and infrastructure stayed unchanged. A road gesture from the observed active tool was rejected before intent publication, with no native dispatch; attribution to the exact pause guard is a source-backed inference, since the public error is generic and the target map tile was not independently identified. Exact rename retry retained its stable receipt fields after capture invalidation; a fresh identity using the old image failed before admission. The rename was uncheckpointed at acknowledgement and checkpointed in the final database. This establishes neither restart recovery nor the 40-second lease boundary, all pause policies or universal GUI input. Evidence: `/tmp/ottd-live-jz4otsfe/paused-renderer-evidence.json` (SHA-256 `8b9be755ae80e6c02bc84c04d9ff5ef45e3b8f4d3028a6d3ad5fd01b066754fd`) and independently reviewed `/tmp/openttd-paused-renderer-final-attestation.json`. Earlier incomplete dependency, startup, parking and drag-schema attempts remain preserved; no production correction was needed for this proof.

The subsequent `land.info` checkpoint isolates house-name, per-tile cargo-acceptance and AI-company clearing callbacks with one existing request-local PSA overlay. The public DTO is unchanged; native clearing quota, ownership, cost and nested town-rating tests remain shared with the normal command. Native query scratch and presentation/debug state can still change. The specialized query rejects non-company contexts before native work and cannot execute demolition. Independent design and production/test source reviews passed; private integration-on/off compiles passed without unresolved integration symbols. Native executable SHA-256 `1c5a2eaa11b4443e70a2d231f4e8f78d8765e4e4aef52588aac02772f28c9f43` passed 9 focused cases / 673 assertions, 240 full cases / 116,174 assertions and 12 native live tests in 126.634 seconds. These fixtures do not claim new runtime coverage of every tile family. The actual catalog remains 104 actions / 60 queries, with BLAKE2b-256 `083a13031161b652d3892f756d1dc5a097f8b1991383174a075ce2fa09ff85a3`; only land-information admission metadata changed, and all 277 pinned backend tests passed without skips. Evidence: `/tmp/openttd-land-info-native-final-attestation.json`, `/tmp/openttd-land-info-native-live-tests.log`, `/tmp/ottd-live-mcivueis/export-result.json` and `/tmp/openttd-land-backend-final-attestation.json`.

Its isolated public HTTP reader fixture passed in 8.019 seconds: strict DTOs, capture-mode 403, two invalid-input paths followed by valid reads within the same one-second interval, an immediate generic rejection followed by a delayed success, and unchanged action row count and next sequence. Generic HTTP 503 does not disclose the native rejection reason; admission attribution also relies on the reviewed source and native tests. Only lifecycle/readiness private RPCs were used. Evidence: `/tmp/openttd-land-info-public-live-tests.log` and `/tmp/ottd-live-s5w1p556/shared-capture-result.json`. This stock fixture establishes neither custom-NewGRF callback isolation by itself nor a hard response-time bound. The shared interval limits starts, not callback runtime. Full four-company gameplay acceptance and the historical workload gate remain open; the separate renderer proof above retains its earlier build attribution.

The subsequent road-access guidance build changes only two static catalog convention strings in native source. Independent design and source reviews approved explicit direction/coordinate/road-bit semantics and composition guidance instead of a planner or automatic connector. Native executable SHA-256 `4e26eb857becb40324cd72a1b00ffc1812c5490e743c5655de85c452e9e8eb07` built successfully; its actual 144,676-byte export has 104 actions / 60 queries and BLAKE2b-256 `aabfc928e2ad99625d0a061ee5bcc7382d0ebdf4772f53901d55cd012d9cd757`. Removing those two conventions reproduces the preceding catalog exactly. The checked-in artifact is byte-identical to the export, and all 277 backend tests passed in 25.411 seconds without skips. Native unit/live suites were not repeated for this descriptive-only change; their preceding binary attribution remains above. Evidence: `/tmp/ottd-live-4avx3b46/export-result.json`, `/tmp/openttd-road-guidance-native-final-attestation.json` and `/tmp/openttd-road-guidance-backend-final-attestation.json`. The [generic road guide](capture-controls.md#road-access) applies to both perception modes; it does not repair or pass the failed assessment.

The fresh [runtime04 four-Luna trial](luna-trial-04.md) completed from 03:04:49.222991 to the unchanged 04:34:49.222991 UTC cutoff on 2026-10-18, using frozen native `4e26eb85…` and the shared road guide. Both text companies and the capture cooperator earned transport revenue; the capture competitor recorded zero income and delivery across all 29 covered quarters. Actual receipt of 220 native PNG image inputs, meaningful partner chat and useful reciprocal road cooperation were verified. Full acceptance was not met: paid competition remained unestablished, the capture companies lack closed 1952 annual categories, and own-company-private chat scope was unexercised. The record preserves the outage, failed 60-minute reads, same-world recovery, fixed cutoff, final publication gap and private-scope qualifications. Earlier failed trials remain unchanged.

Each company must construct and operate a service, earn real company transport income, make a material service change, and earn further income after that change. Loans, gifts, capital spending and operating results are recorded separately. Positive vehicle profit alone is insufficient because feeder transfers can produce virtual credit. Financial comparisons account for period rollover. Cooperation, competing service operation, intended chat delivery and private-message isolation require evidence. Strategy failures, time-budget exhaustion, missing interface capabilities and runtime defects are reported separately.

The first fresh four-agent exercise ran from 21:58 to 22:18 UTC. Both text companies earned passenger income and expanded their working fleets; neither visual company established a service. The overall exercise failed. Its independent audit is `/tmp/ottd-luna-world-02/independent-first-window-audit.json`. A real eight-second capture lease was too short for some model capture-to-submit cycles; not every rejected gesture is attributed to that defect.

The revised renderer supports an operator-configured park of up to 40 seconds, bounded by the authoritative network lag allowance. A separate technical smoke verified a 20-second inspection and native rename, immutable cached-image identity, and rejection after the original lease expired. Evidence: `/tmp/openttd-luna-lease-smoke-proof01/summary.json`. This was an operator fixture, not gameplay acceptance evidence.

The second fresh exercise used `/tmp/openttd-luna-runtime-02/runtime-manifest.json` from 22:46:30 to the fixed 23:18:00 UTC cutoff. An 86-second outage received a recorded 90-second allowance; recovery retained the same runtime, world, credentials and checkpointed request history. The abrupt process-loss cause remains unknown. The original window also failed overall. Its immutable independent report is `/tmp/openttd-luna-audit-02/audit-report.json` (SHA-256 `268329d3eff83ca0204591c2cb9989ca31e4f8c728f717dfaed65825b9b2af05`). Matching checkpoint 63, completed 23:17:56.631 UTC at tick 72482, was copied and inspected in a separate paused authority; no assessment gameplay was performed. Companies 0/2 delivered 507/2,268 cargo units and earned 4,852/4,037 transport income, then verified added buses carrying passengers and earning profit after first income. Both retained startup debt; capital spending, running costs, interest and other fees left total cash results negative. Company 1 built roads, three stops, a depot and one bus, but the bus remained stopped with no orders and no income. Company 3 had no stations, vehicles or income.

The second original window proves company 0 moving loaded buses on company 1-owned road tiles; company 0 also repaired missing road bits, so ownership is not exclusive construction attribution. Reciprocal partner-road use and two operating services in the agreed competing market were absent. All 30 messages (eight world, 22 company-direct) and 152 persisted chat projections had no observed audience violation; both pairs read meaningful initial proposals, but some later replies remained unread. Evidence: `/tmp/openttd-luna-audit-02/chat-projection-audit.json`.

Read-only diagnosis separates normal UI/placement behavior from unresolved integration failures. Native quick-go-to auto-armed company 1's empty Orders window; its explicit Go To click toggled the tool off, so its later station-label click opened a window rather than inserting an order. Company 3's four queued native stop failures match road-orientation or non-drive-through-on-existing-road rules. Three earlier prequeue `visual_action_unavailable` cases remain unknown because their precise private failure codes were not retained; proof staleness is not established as their cause. Evidence: `/tmp/openttd-luna-audit-02/visual-diagnostics.json` (SHA-256 `d12758817b4d59c9b8f5e67c82682118d1ed4252d1c377178973f58ecae609c9`).

A separate continuation ran 23:20–23:50 UTC on the same frozen runtime and companies, without fresh contexts or resetting the original failure. The original 500-submission-per-player cap spanned both phases. At 23:35:21 UTC all four players received the same static native UI control reference, recorded with its hash in `/tmp/ottd-luna-world-03/acceptance-plan.json`; it contained no world data, route, target or finance advice. No operator gameplay or hidden-state assistance was supplied. The continuation also failed: neither visual company operated a revenue service, reciprocal cooperation was absent, and only one competitor operated in the agreed market. Its separate strict-cutoff report is `/tmp/openttd-luna-audit-02-continuation/audit-report.json` (SHA-256 `1ac60a4510c3fea603b2d1436f6dac6c086ff52dd063f409807d69977ae2856b`). Both original failures remain unchanged.

The continuation audit copied checkpoint 125, completed 23:49:31.687 UTC at tick 142641, and inspected it in a separate paused authority using the same frozen runtime. This is 28.313 seconds before cutoff, not an exact 23:50 snapshot. The cumulative company state was:

| Company | Delivered cargo | Transport gross | Operating net after interest and other fees | Net capital spending | Cash |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0, text cooperator | 1,253 | 11,997 | −4,817 | 39,413 | 55,770 |
| 1, visual cooperator | 0 | 0 | −11,884 | 8,393 | 79,723 |
| 2, text competitor | 5,783 | 11,391 | −3,983 | 17,606 | 78,411 |
| 3, visual competitor | 0 | 0 | −11,884 | 0 | 88,116 |

Each company retained its original 100,000 loan. Companies 0/2 had positive transport surplus before interest and other fees, but cumulative cash changes were −44,230/−21,589 after capital spending; positive gross income or individual vehicle profit does not establish overall profitability or an improved profit rate. Annual expense categories were merged from verified checkpoints across the native three-year reporting limit, matched all 21 quarterly revenue entries, and reconciled to cash with unchanged debt. Deltas from the original saved snapshot include the intermission and are not exact 30-minute continuation results.

The text companies' buses added after first income continued carrying passengers and earning income. Company 1 inserted one station order at 23:30:25, before the common UI reference arrived, but its bus remained stopped with one order and zero income. Company 3 still had no service assets. Company 0 again used company 1-owned road while loaded; repairs still limit construction attribution, and reciprocal use remained absent. All 36 cumulative chat messages (eight world, 28 company-direct) and 176 persisted projections showed no audience violation; all six new direct messages between companies 2/3 were read by both, while company 1 recorded no continuation chat reads. The separately timestamped evidence cannot retroactively pass either original window. Full four-company acceptance and release closure remain pending.

## Parity integration checks in progress

The corrected shared-actuator native snapshot has SHA-256 `165a44cab50d587b231c8cd3fd9809b172350b071f454f43dcb63900e28fa3ad`. Its full native suite passes 192 cases and 109,536 assertions (`/tmp/openttd-shared-actuator-native-final-tests.log`), and the 12 isolated native live tests pass in 137.414 seconds (`/tmp/openttd-shared-actuator-native-live-tests.log`). Two new test-fixture setup errors were corrected before that passing run; neither was a production failure.

Its actual catalog is `/tmp/ottd-live-6altblo_/native-shared-actuators-catalog.json`, 138,724 bytes, with BLAKE2b-256 `459cfb8007eeeda4b1c03d48f05ff489c738c489bacd2213b3296e052c98143c` and file SHA-256 `937c9b4e651117c7413970c2991af002158974816c1701cf09fee92059ed375d`. All 103 typed actions admit both modes; all 56 world queries remain text-only. The finite control-reference schema is exported separately. Native hashes matched before and after export; the complete metadata/descriptor frame bounds are 8,976/10,070 bytes. The export fixture closed without changing recovery state. Evidence: `/tmp/ottd-live-6altblo_/export-result.json`.

The first public no-renderer shared-actuator test exposed an actual backend admission defect: a blanket reserved-key check rejected legitimate action parameters named `mode`, including the catalogued maximum-loan option. A source/catalog collision audit also found `company.buy`'s legitimate target `company_id` blocked by the same policy. Both were corrected before the 248-test backend snapshot and passing no-renderer public fixture used by the corrected Luna trial. The earlier 244-test pass did not establish those cases; the corrected fixture is recorded above.

The preceding combined integration-enabled native build has SHA-256 `bcf0b6290876d9e3e36077f76bc9ecc351b12a6aca6aeac08bd68c076c89f2ec`. Its actual export contains 104 actions and 51 queries, with native and independent Python BLAKE2b-256 `fb1a71e3f95fc04920efb45e3ce75270105669455b161378c89e79997a76492a`. All 155 descriptors fit the private frame limit; the largest completed-frame upper bound is 6,945 bytes. Evidence: `/tmp/ottd-live-3kjocbeo/export-result.json`. These results retain that build attribution; a catalog count does not prove gameplay parity.

That combined unit run passed 138 of 139 cases, with 14,286 of 14,287 assertions passing. The remaining case exposed a group-move descriptor rejecting a vehicle-list argument that the native single-vehicle command ignores. The production guard was corrected and the meaningful regression retained. An isolated relink replacing that production object and two language-fixture objects passed 139 cases and 14,292 assertions, without changing shared build executables. Evidence: `/tmp/openttd-parity-combined-unit.log` and `/tmp/openttd-mvp-isolated-fixture-fixes/evidence.json`.

The current source registry counts 103 typed actions plus `ui.apply`, 60 queries and 97 distinct admitted native commands. Six additional native conveniences are composed from individual actions; 43 commands are excluded/internal, accounting for all 146 real baseline enum entries. The two additional admitted area commands retain only their point-sized native forms. Approved implementation includes additional public readers, company access, script question/story behavior, bounded text continuations and terrain/industry dependency validation. Designs were reviewed before implementation; the written family contracts and review artifacts are linked from the capability matrix. The source contract is distinct from the historical validated snapshots below.

The validated snapshot has native SHA-256 `bd6fa09942cdffdd15c14838928235793f7f6c36bafbb8a6c44687adc2e56b87`. Its actual 134,201-byte catalog contains 104 actions and 56 queries, with native and independent Python BLAKE2b-256 `62b25b961f2b7be96d50a37e62cbbc9f47128ffa2e151ee3bb092ce076b57cf1` and file SHA-256 `4a1cafb5a84f688a6e2c6d02ce49fb650c50c82fe0d23464b2aed82fb28d057f`. The artifact is `/tmp/ottd-live-8kpzw9f0/native-metadata-catalog.json`; export evidence is `/tmp/ottd-live-8kpzw9f0/export-result.json`. All 160 descriptors were checked. Metadata and descriptor frame upper bounds were 6,412 and 10,070 bytes, below 65,536 bytes; recovery remained required before and after the read-only export. Native hashes matched before and after.

| Matching validation | Result | Evidence |
| --- | --- | --- |
| Native unit suite | PASS: 170 cases, 109,110 assertions | `/tmp/openttd-final-metadata-unit-tests.log` |
| Backend suite | PASS: 232 tests in 23.900 seconds | `/tmp/openttd-backend-final-104-56-tests.log` |
| Native live suite | PASS: 12 tests in 137.258 seconds | `/tmp/openttd-final-bd6f-live-native-tests.log` |

A separate visual fixture on this snapshot successfully built a native road stop after 20.000667 seconds from genuine PNG download completion to first UI submission; terminal completion took 20.728900 seconds from download completion. Two identical public submissions retained one backend request identity, the execution tick and station incarnation, and the receipt became checkpointed. The backend dispatch flag was one; literal wire-dispatch calls were not counted. Evidence: `/tmp/openttd-visual-staleness-final-proof02/summary.json`. This proves that delayed construction and exact retry in that fixture work; it does not diagnose the three unknown original Luna rejections. The queued older same-ID text question proof also passed, including one native answer and preservation of the newer issuance (`/tmp/openttd-story-duplicate-proof02/text-summary.json`).

A separate genuine-PNG duplicate-popup proof passed on native `4e26eb85…` with the matching 104-action/60-query catalog. An inspected older blue Question offered only YES while a newer yellow Warning with the same native ID offered only NO. The older YES click captured its exact popup witness and one `GoalQuestionAnswer`; the ordinary GameScript report and native log observed one YES and zero NO answers. Both popups disappeared after replay, consistent with native source closing the clicked popup locally and another same-ID window during replay. The native event does not identify an issuance, and company text-inbox retirement remains a separate policy; the older text-issuance proof remains separate evidence.

Exact retry after image invalidation preserved the request sequence, intent and execution tick while checkpoint state advanced from uncheckpointed to checkpointed. A new request ID using the old image received pre-admission 404 `image_unavailable` with zero stored rows. Twelve PNG downloads represented seven unique image IDs; repeated parked IDs retained their original age. The fixture used fixed 40-second parking and a 2,000-tick lag allowance, unchanged age guards, and clean owned-process shutdown. It establishes no timed lease boundary, restart recovery, universal GUI support or autonomous gameplay success.

Evidence: `/tmp/openttd-question-png-proof05/evidence.json` (SHA-256 `016989dc27d238d7551c44c94a256a1e0256645949a63a2638e7d6ba6157f130`); design, source/freeze and independent runtime reviews are `/tmp/openttd-visual-question-proof-design.md`, `/tmp/openttd-visual-question-attempt03-source-freeze-review.md` and `/tmp/openttd-visual-question-runtime-post-review.md`. Four incomplete attempts remain preserved under `/tmp/openttd-question-png-proof01` through `04`: a fixture key-case comparison, eight-second preview timing refusals, an operator's long caption drag, and a prelaunch bind refusal during immediate port reuse whose exact cause remains unconfirmed.

Maximum-map platform dependency cost is [measured separately](industry-platform-proof.md#measured-maximum-map-cost): on the attributed debug test build, 20 fresh 4096 × 4096 scans had a 1,779 ms median and 1,894 ms p95. Those are helper timings excluding setup, not end-to-end gesture latency. The later lifecycle and finite UI controls are included in the corrected shared-actuator snapshot above. The older `bd6fa099…` results remain historical validation, not a final release. Full semantic parity and the four-company gameplay gate remain unproved. The historical foundation measurements below retain their original build attribution.

## Selected native observations and model image delivery

Capture core commit `e386adfdfa` adds the selected map/panel routes and MCP image
tools described in the [capture guide](capture-controls.md). Independent
architecture review preceded implementation; separate correctness reviews covered
native drawing, backend ownership/barriers/retries, transport admission and the
adapter. The attributed native executable is SHA-256
`3de7b4d33cb081b057ca5bcbde52964db90a251ec78369810a68fb4041bc1f94`,
preserved at `/tmp/ottd-capture-native-freeze01/openttd`. It excludes the later
depot-direction change. Evidence and source hashes are collected in
`/tmp/ottd-capture-milestone01/evidence.json`.

| Validation | Observed result |
| --- | --- |
| Native build and unit suite | 240 cases / 116,174 assertions passed, from the build directory with its generated language data |
| Native script regressions | All four passed with the required OpenGFX base set available |
| Integration-disabled compilation | Both changed GUI translation units compiled to private objects; the pristine executable was not rebuilt |
| Backend suite | 380 discovered: 379 passed, one optional JSON Schema dependency skip; that schema case passed separately under the system Python with the dependency installed |
| Public player journal recovery | Three cases passed, including a lost completed reply, exact original references and Boolean/integer conflicts |
| Public selected-capture fixture | Fresh 256-square world: map center/zoom/image-point, exact retries/conflicts, finance layouts, directory sorting/scrolling, causal rename and input-proof separation passed |
| Forced renderer lag | An old native interaction lease held replay back; a new after-action read returned `replica_pending` 74 times, then the identical request succeeded after natural expiry; the full fixture including finance confirmation took 40.78 seconds |

The standard public fixture received 13 PNG responses through 16 selected POST
attempts, including one exact retry, one expected busy response, an identity
conflict and an unknown source-image rejection. It received 2,744,061 pixels / 1,328,803 PNG bytes;
native render count was not instrumented. Genuine directory pixels showed the
top row move from Benfingway to Brefingfield after two rows of scrolling among
24 towns. Separate native PNGs confirmed town labels present, absent and present
again across requests, plus ordinary void borders at valid edge centers; the
invalid freeform-border tile `(0,0)` was rejected. Station, waypoint, sign and
competitor label categories were not semantically exercised.

The lag fixture first recovered the unchanged pre-action PNG under its original
read identity. Its new causally fenced request stayed pending while the renderer
was parked and then returned a distinct image; no input, cancellation or action
replay released the lease. A subsequent native finance image visibly showed
`Forced Lag 2fa869`, matching the completed rename. Evidence:
`/tmp/ottd-capture-live-final01/result.json`,
`/tmp/ottd-capture-live-final01/operator-visual-validation.json`,
`/tmp/ottd-capture-labels01/operator-visual-validation.json`, and
`/tmp/ottd-capture-forced-lag01/result.json`.

An independent Luna player actually received five image blocks and read its
finance values and largest town, selected a feature in an overview and zoomed
into it, and verified the renamed finance title in an after-action image. Its
adapter audit records 14 HTTP attempts: five capability bootstraps, five selected
POSTs, one action POST and three action-status reads. The test host deliberately
started a fresh adapter per tool call; a persistent host can retain bootstrap.
Two malformed host invocations before execution remain recorded in the actor's
findings. Image emission alone is not used as evidence of model perception;
the actor's answers refer to the delivered image digests.

A separate Luna used the older screenshot/control flow to read the same finance
fields: three successful tool calls, two 1280×720 screenshots and five HTTP
requests, delivering 1,843,200 pixels / 1,361,656 bytes. The selected finance task
used one tool call, one 421×479 native image (201,659 pixels / 13,479 bytes), and
one bootstrap GET plus one PNG POST. Both were legible. These representative
tasks used different companies and world sizes; their host timing definitions
also differ. They establish fewer calls and delivered pixels for this report,
not a controlled latency speedup. Evidence:
`/tmp/ottd-luna-selected-capture01/findings.json`, its
`operator-http-audit.json`, and
`/tmp/ottd-luna-legacy-capture01/findings.json`.

Reproduce the public fixture on a disposable, already running visual company:

```sh
python tests/integration/selected_observations.py --origin "$QA_ORIGIN" \
  --token-file "$QA_TOKEN_FILE" --artifacts /tmp/selected-observation-check \
  --center-x 128 --center-y 128 --require-scroll-change
python tests/integration/selected_observations.py --origin "$QA_ORIGIN" \
  --token-file "$QA_TOKEN_FILE" --artifacts /tmp/selected-observation-lag \
  --center-x 128 --center-y 128 --forced-lag
```

The second invocation needs a natural native input lease shorter than its
45-second deadline. These are finite capture/recovery checks, not autonomous
business acceptance. Full semantic parity, economic success for capture players,
broader native-panel coverage and long-running stability of this new snapshot
remain open. The earlier overnight world was cleanly stopped at the user's
request; its save, credentials and logs were preserved.

## Generic depot direction and text-play guidance

The Luna follow-up adds opt-in `direction: "auto"` to the existing road/rail
depot actions, with the same command surface for text and capture players.
The [placement contract](depot-placement.md) explains unique local facing,
actual completed direction feedback and the remaining reciprocal-connection
requirement. Independent architecture and correctness PRE reviews preceded code;
separate POST reviews caught and corrected omitted safe-estimate error projection
and response-schema fields. Strategic choices and evidence-freshness guidance
remain in the [public player supplement](../tests/integration/luna_play/TEXT_PLAYER.md);
no evidence notebook, inbox subsystem or business-policy API was added.

The combined production native executable has SHA-256
`eada3a7bbe726c1feeeba7f8bbc317d410841ea2176d1b91da1efa45eb013db1`,
preserved at `/tmp/ottd-depot-native-freeze01/openttd`. The corresponding catalog
BLAKE2b-256 is
`4e9c1f52af5bd175cdc0a306356d7db378577ea2967c99356d44375e5d4fe89e`.
The combined backend suite passed all 390 tests without skips, including the
optional JSON Schema validation. Ten focused native cases passed 957 assertions:
road/rail facings, exact type/category/access, slope/map edges, RNG/funds/map
preservation during vanilla quotes, explicit-direction bypass, local dependency
comparison, safe errors and actual direction result guards.
The full combined native unit suite then passed 250 cases / 117,128 assertions,
including the earlier capture work and the new fixture cleanup, at
`/tmp/ottd-depot-capture-native-final.log`. The compiled test executable SHA-256
is `d3135873fd66de262d74de483157e53899735e805cf9276ecc97b156458a75b9`.
Source hashes and review/evidence paths are collected in
`/tmp/ottd-depot-final-acceptance01/evidence.json`.

On a fresh disposable 128-square world, the public text fixture passed in 10.2
seconds using 150 HTTP requests, 27 map pages and 17 action submissions including
one exact replay. All four road facings returned the expected actual integer
direction. Intentionally missing reciprocal neighbor bits stayed unchanged;
automatic facing built no connector. Unavailable/ambiguous actions and advisory
quotes retained their safe errors without constructing a depot. Explicit isolated
placement still worked. A completed action replay after adding a second neighbor
retained its original result/tick rather than resolving again. Owned rail also
worked. Evidence: `/tmp/ottd-depot-public-proof01/depot-direction-result.json`
(SHA-256 `1c64fb2fdfc7889626398b6f62022ee610f6067ea3074b1cfa619f21727ec22a`).

A separate visual company used MCP `act_and_observe` to construct one automatic
road depot. Its completed receipt reported actual direction `1`, cost `390` and
the created reference; the tool returned a native map image with causal state
`satisfied`. The new depot is visible in the delivered PNG, SHA-256
`304811f52812238d2411ec2b64ecc9050d1672d2387bc97605ca3122a589a8ef`.
This was an operator-selected mechanical fixture, not an independently chosen
Luna business. Evidence: `/tmp/ottd-depot-visual-compose01/result.json` and its
adapter audit. All three disposable capture/depot test worlds were then stopped
cleanly, preserving their saves and evidence.

Reproduce the public depot fixture with one fresh text-company credential:

```sh
python tests/integration/depot_direction.py --origin "$QA_ORIGIN" \
  --token-file "$QA_TOKEN_FILE" --artifacts /tmp/depot-direction-check
```

Native local-fence cases exercise capture/comparison directly; an entire queued
bridge submission interrupted by a changed neighbor was not added to this gate.
The unpaused public fixture does not prove funds/RNG/internal command-count
invariance; those vanilla checks are separate native tests. Initial native
fixture attempts exposed missing pool-allocation guards and an unregistered
synthetic road category; only fixture setup changed to correct them. Their logs
remain alongside the passing focused run
`/tmp/ottd-depot-native-focused04.log`. The initial combined Python environment
overlay selected an incompatible `typing_extensions`; the corrected run preserves
the ASGI environment and appends optional schema dependencies, with its passing
result at `/tmp/ottd-depot-capture-backend-with-schema.log`.

## Build and host

The baseline is `c0d5c0b3fc2588ac001e301e97ce795aefb8a90c`. The earlier foundation executable has SHA-256 `257ad478d3a47edf178f425ccd1db25c4085aa110b42ee242babb3f41493f20a`. Its native-generated public catalog has SHA-256 `e6bb8f667c2ae37d0a1a072569de7a285756cf1e6add27387e0ee6a2ce17d627`. Python dependencies are pinned in [requirements.txt](../backend/requirements.txt).

The local reference host is an AMD Ryzen Threadripper 3970X, 32 cores/64 threads, approximately 64 GiB RAM, running WSL2 Linux 6.6.87.2. Benchmark records include concurrent workloads and exact executable/backend source hashes. Earlier measurements must retain their original build attribution.

## Historical foundation checks

| Check | Result and evidence |
| --- | --- |
| Native unit suite | 113 cases, 2,618 assertions pass on the foundation executable. Local log: `/tmp/openttd-mvp-final-unit.log`. |
| Backend | Pinned ASGI environment: 159 tests pass without skips. Covers strict handles, historical canonical retries, visual isolation, image ownership/cache validation, synchronized admission, saturation, cancellation and send-timeout release. Independent correctness review passed. |
| Live public schema conformance | 344 checks pass against the restarted world: 34 JSON responses and two genuine PNG downloads across all three principals. Includes public schemas, current three-field handles, legacy two-field receipts, minimal visual outcomes, authorized history and forbidden structured visual observations. Native/backend/schema hashes are unchanged across the audit. Evidence: `/tmp/openttd-public-conformance-20261017/conformance-summary.json`. |
| Public HTTP four-mode services | Pass in 116.247 seconds with identical before/after native, backend and catalog hashes. Road, rail, ship and aircraft each have positive construction/purchase costs, two verified orders, passenger cargo loading and positive whole-vehicle service-income deltas. Exact purchase retry changes neither allocation count nor balance; an ordinary order change and restoration are verified. Evidence: `/tmp/ottd-live-mq7_skrc/public-services-summary.json`. The earlier run passed the gameplay checks but correctly failed its source-integrity guard and is preserved separately. |
| Native live engine suite | Nine cases pass in 110.44 seconds on the preceding build, before the conditional-build-only chat guard. Real construction/purchase costs, loading and delivery income in all four transport modes; ownership; allocation reuse; exact retries; checkpoint/restart; visual recovery conflicts. |
| Cross-boot stale handle | Reproduced equal old/replacement ID and generation before the fix. After the fix, the old handle is rejected and the freshly observed replacement handle works. Evidence: `/tmp/ottd-live-6zaqu5is`. Public handles now include their captured engine instance; the backend never fills it in. |
| Historical retry after restart | Exact request with its original old handle returns the checkpointed receipt. Evidence: `/tmp/ottd-live-yig28s1v`. |
| Visual recovery | Conflicting action or private intent cannot resolve an uncertain request. Exact resolution remains stable after another checkpoint/restart. Evidence: `/tmp/ottd-live-71zj4vsr`. |
| Integration-disabled build | Focused compilation of `network_server.cpp` without `WITH_INTEGRATION` passes; its object has no unresolved integration symbols. Evidence: `/tmp/openttd-mvp-off-audit`. The preserved pristine executable was untouched. |
| Native/API conversations | Actual native clients and API identities exercise public, company, company-direct and identity-direct privacy, and retry-once delivery. Evidence: `/tmp/ottd-live-n7ztq2dm/native-chat-result.json`. |
| Native compatibility | Pristine upstream client is rejected with native `WrongRevision` error 8; matching modified clients join. Ship a matching human client. |
| Browser | Actual Chrome checks after the final shared-world restart cover a genuine 1280 by 720 PNG, authenticated SSE/chat, token clearing, a 390-pixel mobile layout without overflow and no JavaScript errors. Evidence: `/tmp/openttd-final-viewer-qa.json`. |
| HTTPS | Local Caddy/CA smoke verifies viewer, authenticated PNG/SSE, and unauthorized rejection. This is a local transport check, not an external deployment. |
| Visual checkpoint/retry | One screenshot-driven loan changed the rendered loan from 100,000 to 110,000, was checkpointed, and survived a full stack restart and exact retry without another loan. Evidence: `/tmp/openttd-backend-visual-verified`. |

## Mixed-world gameplay gate

The permanent local world is `local-mixed`; its private state is under `.worlds/local-mixed`. Do not publish its credentials or private operator files.

| Actor | Working route and modification |
| --- | --- |
| Native human interface, company 0 | Road passenger service built and operated through ordinary native GUI events. An order change is followed by independently verified further revenue: fresh baseline 4 to 22, with only the test bus in that company. Normal construction costs and infrastructure changes are verified. Evidence: `/tmp/ottd-human-gui-qoh3e_e4`. |
| Text agent, company 1 | Public API passenger railway loads cargo and earns revenue; the agent adds a block signal after service starts. Evidence: `/tmp/openttd-joint-text-proof.json`. |
| Visual agent, company 2 | Screenshot-only construction, bus purchase and two orders establish a passenger route. After its first stop loses passenger acceptance, the agent replaces that stop and changes the orders through native GUI gestures. From the fresh post-change baseline, independently reviewed screenshots prove 99 further gross income, 18 current-year bus profit and passenger cargo. The read-only ledger audit confirms exactly one bus purchase, company 2 isolation and only `ui.apply` actions. Evidence: `/tmp/openttd-visual-public-proof`. |

Four-mode public HTTP acceptance passed on the foundation snapshot. The mixed world restarted on that native build (`257ad478…`) and its revised ASGI admission limits, preserving credential bytes, public identities/company bindings/modes, text vehicle IDs, threads, message history and the final checkpointed visual receipts. Fresh text observations contained complete current handles, and visual structured observations remained forbidden. The visual credential obtained a new genuine PNG without another gesture; Chrome also passed against that restarted world. Evidence: `/tmp/openttd-final-restart-before.json`, `/tmp/openttd-final-restart-after.json` and `/tmp/openttd-visual-public-proof/post-restart.png`. Individual successful commands do not substitute for the working-route gate.

The canonical `local-mixed` world was upgraded at approximately 03:10 UTC on 2026-10-18 to native `4e26eb85…`, with the matching 104-action/60-query catalog (`aabfc928…`). Read-only mapped-process checks confirmed one authority and two renderers on that binary. The independent preservation audit compared all 106 historical action rows exactly (99 completed, seven rejected), with zero new action rows. Three original credential files, eight stored runs and their bindings, thread/message history and event cursors were preserved. All three active roles passed authenticated identity, status, capabilities, history and SSE resume-handshake checks; text mode obtained a current vehicle handle and its two orders, while both capture roles retained structured-observation 403. Evidence: `/tmp/openttd-permanent-post-upgrade-independent/final-attestation.json` (SHA-256 `4c1576658b725a33ed642bb349393ca5f01a047cf66f1038af9e1b0f7bd5fef3`) and `/tmp/openttd-permanent-mapped-native-post.json`. A separate final static review matched the sealed release and qualified rollback files (`/tmp/openttd-permanent-final-runtime-post-review/final-attestation.json`). The original production action POST was not retried. The isolated compatible-rollback restart/retry proof remains separate; these checks establish neither lossless downtime nor every pause policy or fault path.

Actual Chrome checks at 03:24 UTC verified a genuine 1280 × 720 image, visible “World running” status, token clearing, no JavaScript errors and a 390-pixel mobile layout without overflow. The check read existing chat and opened SSE; it published no messages or gameplay. Evidence: `/tmp/openttd-permanent-parity-viewer02-qa.json`. The first sandboxed browser navigation timed out; its `/tmp/openttd-permanent-parity-viewer-qa.log` remains preserved. Repeating the check with the owned Chrome process outside the filesystem sandbox passed without a product correction.

## Playable browser core

The unchanged `c144f07853` frontend passed 34 browser tests and independent source/static review. Separate Chrome/CDP checks exercised real DOM mouse input on native `4e26eb85…`, with the ordinary eight-second capture park and mixed-world lag allowance unchanged. A paid road drag produced one `BuildRoadLong` command (24), increased reported road infrastructure from zero to four and recorded construction cost 344. The retained native outcome image visibly shows the road and its cost.

Browser text replacement completed with zero native commands and left the public company name unchanged. A separate click on native OK produced one `RenameCompany` command (62) and changed the public name to “Browser QA 02”. The saved `023-after-dom-native.png` shows that committed name in native company/finance headers and the status bar, with the edit popup closed; an earlier edited-field image is not saved-name proof. Core evidence and independent release review: `/tmp/openttd-browser-core-root-release-go.json` (SHA-256 `0918fa5550e11bcfb41323782b6f655ed3a8139993a4632760ff198e5a2b791c`), referencing the road, pre-confirmation and post-confirmation audits and images under `/tmp/openttd-web-play-proof02`.

This establishes those finite browser effects, not full GUI support, semantic parity or autonomous gameplay success. Retained displayed dispatch PNGs and posted image IDs support qualified continuity evidence; raw CDP PNG response-body hash binding remains incomplete for all three core actions. The technical operator refreshed through the real DOM before dispatch from previously inspected coordinates, then independently compared the target scene. It does not prove the model inspected every exact fresh dispatch PNG and does not relax the Luna assessment's viewed-exact-PNG rule. The same QA fixture resumed after a premature stop with its original cutoff retained. Ancillary browser claims still await independent final review. These capture checks do not establish native browser readiness; the separate [native client verification](#native-browser-client) covers that route.

## Native browser client

Human browser play now uses the upstream Emscripten/SDL2 game client with local
rendering and native input. A separate fixed loopback WebSocket relay carries
ordinary multiplayer traffic; it neither restarts the world nor uses an API
bearer. The capture API remains available to visual agents. Independent design
and source reviews approved the relay, browser-only company-chat availability,
empty-name startup default and standard document fullscreen with SDL2 resizing.
Seven isolated relay tests passed. See [startup instructions](../os/emscripten/README.md#fixed-loopback-native-relay).

Chrome checks on a separate preserved 128-square world verified native hover
tooltips, title-bar dragging with intermediate movement, right-drag panning,
wheel zoom, native text input, and paid road construction. The road drag cost
486 and increased public road infrastructure from zero to six. A normal API
company rename appeared in the native Players window without a desync. Company
0 sent a private message to API company 1, received its API reply in the native
view, and sent another private message while fullscreen. Unrelated company 2
could not list that thread and received `404 thread_unavailable` for its history.

Fresh-profile autojoin used the default `Browser player`; a saved custom player
name survived reload. Two fullscreen entry/exit cycles retained nonzero native
backing dimensions and correct dragging/click coordinates. The final control
position leaves the native toolbar visible. A 60-second running-world sample
advanced the native date without a disconnect or desync, and the browser made no
capture-API requests. Early failed Chrome starts, the original missing-name
error and legacy-fullscreen bitmap failure remain recorded; the latter two
received product fixes. Headless Chrome needed `--password-store=basic` after
network logs exposed a stalled cookie-store load. Stock filesystem-sync warnings
were retained.

The final four-file build manifest has SHA-256
`1bad240d5b3aae6645a252c32ea31ceb84ffcb2cc26332e85bb50918f93ee5c8`.
Its archive base is `6a96be9db6`, with reviewed browser overlays through
`040410de38`; its ordinary multiplayer revision matches the unchanged
`4e26eb85…` native server. Runtime records are under
`/tmp/openttd-native-browser-qa-evidence07` through `evidence10`.
Mouse-press event handling p95 was 8.943 ms and browser RAF interval p95 was
16.8 ms in the small fixture. These measure Chromium event handling and browser
scheduling, respectively; neither is a measured input-to-game-paint bound nor a
500-vehicle WASM performance result. This browser proof does not close the
separate four-company gameplay acceptance gate.

The final build also joined the existing 512-square overnight world through the
separate localhost relay, showing the human company and all seven companies.
That read-only browser smoke issued no gameplay or chat actions. The original
world epoch and engine instance remained unchanged and unpaused; the disposable
QA world and browsers were closed. The running relay uses the read-only release
whose manifest is SHA-256
`9cdedd25de9dc5389b37194d86bd7deb665e0dbc9e04d6b96cf1f5503511ffcb`.
Operational readiness is recorded in `.worlds/overnight-browser-readiness.json`;
the game and relay remain running independently of the browser.

## Agent model reports

Commit `a9fa600fda` adds authenticated `PUT /v1/me/model` with
`model_name` and `effort_level`, plus `DELETE` to clear the report. Both agent
perception modes may report. Labels belong to the authenticated run, survive
credential rotation and restart, and are not inherited by a replacement run.
The native browser's optional **Agents** panel and the reference viewer show
them explicitly as self-reported. The browser relay projects only display
fields and keeps its API credential on the server.

Independent architecture reviews preceded implementation; separate correctness
reviews passed. Validation passed 290 backend tests, 11 relay tests and 27
JavaScript tests. A read-only backup of the overnight world's database was used
to verify companion startup: all 22 preexisting tables retained identical
contents, and only the three-column `run_model_reports` table was added.

The ongoing overnight backend remains on port 18085. Its temporary metadata-only
companion listens on 18086 using the same database and credentials, with no
engine connection, renderer or broker. An outer route filter rejects gameplay,
static, image and event routes. The main backend supports model reports on its
ordinary API port after a planned upgrade. This feature does not change the
separate gameplay acceptance results or earlier performance measurements.

Finite Chromium checks exercised real authenticated report creation, update and
deletion through the API, literal rendering of HTML-like labels, missing reports,
and clearing old labels after an actual companion outage. A closed panel made
no roster requests during the measured 16-second window. Initial keyboard
activation failed because SDL consumed window keyboard events; commit
`0c7883d3be` isolates keyboard events inside the panel. A fresh browser check
confirmed Enter and Space toggle the native summary control. Fixture evidence:
`/tmp/openttd-native-browser-qa-model01/evidence/report.json` (SHA-256
`d28c0f3210d3bc90f0ce1934970d4497b42b27e053ccbe3f88d24d4ae5983a65`)
and `evidence-v2/report.json` (SHA-256
`d44c8b905e2bb5a5777f4da3cc87c59bc37a12ccb4edad37d16d1224b31e4419`).

The final browser release manifest is SHA-256
`21ffba4bebcc37ba756be4a2d25924f1997691dfdc6ff96af164b1c0fa6a5e26`.
Its native JavaScript, WASM and graphics data match the preceding release byte
for byte. The relay on `http://localhost:18087/` serves the new page and public
display projection. Reload the page to see **Agents**; labels refresh every
15 seconds while the panel is open.

## Checkpoint observations

A read-only 104.7-second sample of the small permanent world observed 420 private checkpoint samples and 105 public tick reads, with no read errors or failed saves. Publication cadence was bracketed at 30.25–31.00 seconds. Three saves completed between polls, giving duration upper bounds of 253–289 milliseconds; their duration was not measured as zero. The peak saved-state age during that interval was bracketed at 30.699–30.942 simulated seconds. The normal tick duration is 27 milliseconds and observed speed was 1.000001 times normal. Evidence: `/tmp/openttd-checkpoint-local-01`.

These are local observations of a small world. They do not establish the 500-vehicle checkpoint window, a universal recovery bound, or restart readiness time.

The default checkpoint policy quiesces agent dispatch when a save is due and rejects new gameplay admissions after 120 seconds without a newly observed successful checkpoint. A queued save response is never treated as durable completion. Native simulation continues while checkpointing is unavailable; the last verified save remains the recovery boundary. Monitor checkpoint age rather than treating the timer or admission grace as a guarantee about unsaved human gameplay.

## Workload gate and limits

See [LOAD.md](../tests/integration/LOAD.md) for workload generation, raw records and the authoritative analyzer. The full target is 1,800 seconds at normal speed, a 512 by 512 primary map, 500 active vehicles, eight companies, 50 browser-protocol viewers, four visual clients, and four independent engine/backend groups. A real browser is checked separately; the concurrent load clients reproduce its HTTP/PNG/SSE traffic.

The first two full runs retain their original image-admission failures. Raising image capacity to 64 responses with eight lookup threads removed that bottleneck in the synchronized 54-image burst and 25-second smoke. Kernel resident-memory high-water marks were approximately 72 MiB for the backend, 75 MiB for the authority and 108–109 MiB per renderer. Evidence: `/tmp/openttd-load-final-burst/audited-summary.json`. A third full run then exposed separate capture/API admission failures under its 32-slot API budget. These failures remain in its raw record.

The backend admits 64 API requests and 64 image responses, with eight image lookup threads, separate SSE admission, a five-second send deadline and the global connection bound. The later 1,800-second reference run completed on the historical `257ad478…` native snapshot. Its strict verdict remains `TARGET_NOT_MET`: two of 359 completed primary five-second windows fell below the predeclared 0.95 speed ratio (minimum 0.93265); the other 357 and all measured sentinel windows met it. Every other audit check passed, including all 1,800 gameplay completions, 90,000 chat/viewer delivery pairs and zero measured steady HTTP/client errors. The first and final short-window minima remain unproven. See the [attributed measurements](../tests/integration/LOAD.md) and `/tmp/openttd-load-api64-target`; this historical result does not transfer to later binaries.

The separate frozen runtime04 run on native `4e26eb85…` and its pinned backend/driver measured 03:27:47–03:57:47 UTC on 2026-10-18 (1,800.000141 seconds). It passed all 18 unchanged declared workload checks: 500 active buses across eight companies, 50 browser-protocol viewers, four visual clients sharing four views, and three independent one-bus sentinel groups. All 1,800 actions completed and all 90,000 exact chat/viewer pairs arrived, with zero measured HTTP/client errors or sampling gaps. Action ACK p95 was 73.3 ms and chat delivery p95 199.3 ms. The primary minimum completed five-second speed ratio was 0.99465; sentinel minima were 0.99481, 0.98243 and 0.97801. Conservative whole-interval lower bounds were 0.99168 for each world.

This is `TARGET_PASS` under those declared checks, with 50 distinct viewers and complete delivery, not proof of 50 simultaneous SSE streams throughout: eight of 360 steady samples fell below 50 connections, reaching 37. First/final boundary minima, native queue depth, actual send/slot saturation, one fresh frame per second and 50 separate renderers remain unproven. Authorized Luna, permanent-world and private browser-QA overlap prevents an isolated comparison or a causal claim about historical failures. Independent review preserved the frozen inputs and historical records. Evidence: `/tmp/openttd-load-runtime04-rpcbrackets-38979-20261018`, `/tmp/openttd-load-runtime04-independent-post-review.json` and `/tmp/openttd-load-runtime04-final-attestation.json` (SHA-256 `671c2c3cc978345002a1c4540aed68e1b192e78d3f4aa35d4162fb1f6ac9866c`). The [full attribution](../tests/integration/LOAD.md#frozen-runtime04-result-2026-10-18) pins the native and runtime manifest. Its protocol clients and earlier frozen frontend assets do not test the later `c144f07853` browser controls; the separate [finite browser proof](#playable-browser-core) does. Native browser verification is recorded [separately](#native-browser-client). The completed [runtime04 Luna trial](luna-trial-04.md) did not meet full gameplay acceptance; this load result does not close that gate.

Request rate, image latency and image freshness are separate measurements. Renderers may retain a parked frame for roughly eight seconds; repeated one-second requests can return that same immutable image. The earlier smoke observed source-image age near eight seconds and three to four unique frames per view in 20 seconds. This does not demonstrate one fresh frame per second or 50 independent rendering workers.

## Observed slow-reader behavior

A separate public HTTP probe offered 64 pipelined requests for one owned 1,186,447-byte PNG through an unread socket with a 2,304-byte effective receive buffer. TCP sampling observed approximately 1.03 MB queued on the server throughout the unread window. After that pressure was observed, a normal company rename and restoration both completed within 1.40 seconds; an API read at 6.13 seconds took 22 milliseconds and ticks had advanced by 228. This proves progress during actual TCP pressure in the small shared world.

The connection remained established until the probe closed its own socket at 8.13 seconds. One new ASGI incomplete-lifecycle error was logged; no timestamped application timeout or graceful ejection was measured. The five-second response wrapper and admission release have deterministic tests, but this probe does not prove that the underlying TCP connection is closed within five seconds. Evidence: `/tmp/openttd-mvp-tcp-probe/0f9faa95bbc1482aa2f8a70d8c0c1137.json`.

## Running locally

Use [the launcher instructions](../tools/README.md) and [the API guide](../backend/README.md). Humans can use the matching native client; external text and visual agents use the authenticated API. The `c144f07853` browser-control source adds native click/drag, Ctrl and text input through existing visual actions, with 34 browser tests and independent source review passing. Actual Chrome road construction and separate text/OK effects are verified within the [finite scope above](#playable-browser-core); human browser play uses the separately verified [native WebAssembly client](#native-browser-client). Remote access requires an independently operated HTTPS proxy. No external site has been deployed.
