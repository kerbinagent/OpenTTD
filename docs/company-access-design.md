# Company access and loan integration

Independent engine/backend architecture review approved this finite interface. The family lives in `src/integration/actions_access.cpp`; its declarations, central registry, schemas, target capture and CMake registration are integrated.

## Actions

- `company.join_policy.set {policy: "any" | "listed"}` posts exactly one ordinary `CompanyAllowListControl` command under the actor's immutable bound company. It has no target-company parameter.
- `company.participant.authorize {participant: {id, generation, engine_instance_id}, allowed: boolean}` posts exactly one AddKey/RemoveKey command. `participant.id` is **ClientID, not the NetworkClientInfo pool index**. The family explicitly constructs ClientID and resolves with GetByClientID. The current engine instance, exact native-info incarnation and ACTIVE matching socket are mandatory. Native keys never appear in public parameters.
- Existing `company.loan.increase` and `company.loan.decrease` accept `mode: "amount" | "interval" | "max"`, default amount. Amount mode requires positive signed64 `amount`; the other modes forbid amount. Existing no-mode canonical requests retain their meaning.

Removing a key changes future join permission. It does not kick or move an already connected player. Policy/key changes are idempotent normal native operations, still correlated through the existing single request ledger and normal command execution result.

## Readback

The independently approved text-only `company.access` query accepts `{}` or `{participant: exact_live_handle}`. It returns SnapshotBase and `join_policy: any|listed` for the actor's own bound company. With a participant it additionally echoes that handle and returns `listed` (the key is in the own-company allowlist) and `permitted` (allow_any or listed). Those three participant fields are absent when no handle is supplied. The same exact live handle resolver performs one bounded lookup; there is no offline key, other-company selector or stored identity change.

## Participant identity and public admission

Existing actor bindings are exactly identity/company/run/mode, with a company incarnation fence. They do not bind a native public key. This change does not add native credentials to actor bindings or allow an actor to switch company.

The existing private NativeRoster response gains `native_handle` on ACTIVE remote native connections. Backend retains its stable social participant identity for chat/display and attaches the optional handle to active human native participants after its existing renderer-key filtering. A local server entry has no socket and receives no actionable handle; the server owner can already join companies without invitation.

For a **new** public authorize request, backend refreshes the native roster and requires the supplied handle to match a currently active human native participant after worker filtering. Look up exact historical canonical duplicates before this lookup, so a completed retry remains available after the participant disconnects. No additional database column, private request envelope, resolver lifecycle or identity service is introduced.

The native family resolves the handle to its current key at admission. Existing private captured_targets retains exact ClientID, native-info generation and key. Publication recomputes this lease and rejects disconnect, reconnect, incarnation or key replacement. Backend input cannot supply an arbitrary key or an offline participant. Private keys and proof fields must never be copied into public results/events/errors.

The native command's own validation checks only key length. Ordinary remote-client ingress additionally restricts key changes to connected clients (`network_server.cpp:1170`). Server-local integration Posts bypass that ingress path, so the family performs that live-key restriction explicitly.

## Native visual intent

CompanyAllowListControl is explicitly admitted through these canonical actions. It has no map footprint. Its exact typed decode/encode roundtrip is checked. Policy actions require an empty key and a valid policy enum. Add/remove actions require a key belonging to at least one ACTIVE native connection at admission and publication.

The ordinary native client list now has explicit Add/Revoke controls and own-company Any/Listed policy controls. Renderer input admits these finite roles only after a genuine screenshot records the exact window, row model, local connection and selected native key. Gesture hit testing repeats the normal client-list button lookup; a changed model rejects the gesture. The policy dropdown retains its exact originating window. This private screenshot witness grants only the current gesture and is cleared afterwards. Join, move, new-company, admin and native chat controls remain denied.

On the authority, the private proof contains the key and sorted matching active ClientIDs; several live connections may share one native key because permission is inherently per key. Native-info generations are never compared across processes. Existing renderer owner/key/company fences remain mandatory, and NetworkClientRequestMove remains blocked in renderer role. The backend's existing worker filtering prevents public semantic authorization from selecting renderer participants. No public key or private witness is returned as a visual result.

`AccessIntentState` must be called by central NativeIntentState, which is recomputed immediately before visual publication. `CaptureAccessTargets` must be called and its result appended in central CaptureActionTargets so the normal text lease revalidation repeats the guards.

## Loans and unsafe legacy residuals

All loan modes use normal IncreaseLoan/DecreaseLoan Posts. Amount retains native interval-multiple/limits/funds validation. Signed64 input matters because the global maximum loan is inflation-adjusted (`economy.cpp:739`) and can exceed the old INT32 action limit; native Money is signed64.

Decrease interval repays `min(current_loan, LOAN_INTERVAL)`, so it handles an exact residual below the normal 10,000 base-currency increment. Native decrease max computes at least one whole increment even for a smaller residual, potentially overpaying it. The adapter therefore admits decrease max only when loan is zero or at least LOAN_INTERVAL. Use interval for a final residual. The zero-loan case retains normal native already-repaid rejection.

Native increase interval can similarly overshoot a positive remaining headroom below LOAN_INTERVAL. The adapter rejects that state and directs the caller to max, which borrows the exact remaining permitted headroom. A reached/exceeded maximum is left to native validation.

Both safety predicates run at admission and immediately before publication. They do **not** pin the whole loan value, maximum value, cash balance or derived max amount: normal native max computes its amount and checks funds at execution. No upstream economy behavior is patched and no max/interval command is silently translated into another mode.

## Root wiring and verification

Central registration and backend projection are integrated. Exact canonical terminal retries precede newly changed schema validation. Backend native-handle projection, worker filtering and fresh new-admission checks are separate from the native screenshot-row witness.

Meaningful native tests: same ClientID handle versus deliberately supplied pool index; stale engine/info incarnation; target disconnect/reconnect before publication; exact old receipt retry after target leaves; connected spectator invite and revoke without forced move; own company restriction; empty/invalid keys through private native visual capture; multiple live connections sharing a key; policy any/listed; amount legacy/default and signed64 schema; interval/max branches reject amount; decrease residual max rejects while interval clears exactly; positive sub-interval borrowing headroom rejects interval while max succeeds; moving funds between capture and execution retains native behavior.

Independent design and correctness reviews passed. On native build `bd6fa099…`, a real connected-player GUI fixture executed Add, Listed, Revoke and Any with authoritative readback and identical exact-retry results: `/tmp/ottd-live-yxj59x2w/company-access-summary.json`. That fixture exposed a first-capture lifecycle reset erasing the row witness. The reviewed fix moves reset before fresh screenshot construction and rejects later lifecycle mismatches; clean first-gesture runtime verification remains pending the next combined build. See `integration_validation.md` for build-specific acceptance status.
