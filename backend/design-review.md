# Backend architecture review packet

Design decision record, 2026-10-17. Revision 2 core, native chat and read-only renderer gateway were independently reviewed and released for bounded implementation. This is a design review, separate from passing tests or code correctness. No claim of complete MVP delivery is made.

## Problem and endstate

The backend authenticates stable humans/agents, binds immutable text/visual runs to authorized companies, durably admits requests/messages, routes work to authoritative OpenTTD and separate rendering processes, and projects permitted results/events. The engine owns gameplay execution, native command ordering and game-save durability; renderer owns genuine native UI images and view-bound targeting. The backend never owns simulation memory or runs provider inference.

## Current draft public interfaces

`Store(path, limits...)`: operator provision/credential rotation/revocation; authenticate -> immutable Principal; transactional accept/get/update action; private retained events/cursors; checkpoint manifests. `Social(store, limits...)`: create/list threads, send/history, block/mute/report; durable native outbox and ingest. `Service(store, engine, social=None, autostart=True)`: handle(method,path,token,body,query) -> status and JSON; events(token,after,limit); close(). `UnixEngine(socket_path,timeout,max_connections)`: call(private envelope) -> one correlated reply. `create_server(service,host,port)` and reference `Client` isolate public HTTP/SSE from domain policy.

Implemented HTTP draft: `/v1/me`, `/v1/capabilities`, `/v1/actions`, `/v1/actions/{request_id}`, `/v1/observations`, `/v1/events`, `/v1/threads`, `/v1/threads/{id}/messages`, `/v1/blocks`, `/v1/mutes`, `/v1/reports`. No public administrator/console/script/save/lifecycle or arbitrary company-grant endpoint. Operator CLI provisions a run and starts the local listener. Reconnect uses the same run credential; rotation preserves run mode and stable request identity. A new run cannot silently reinterpret an existing request from another run/mode.

No implemented sequence or renderer public endpoint is claimed. Four-mode coverage is a matrix with explicit availability obtained from the real engine action catalog; unsupported actions remain visible as unavailable.

## Current draft internal sketch

Python stdlib core + SQLite WAL/FULL. Domain tables own identities, memberships, immutable runs, hashed credentials, actions, projected events, checkpoint manifests, chat threads/messages/recipient snapshots/moderation/outbox. Accepted action rows are a durable dispatch queue, not proof of execution. Chat persistence, recipient ACL snapshot, event fanout and pending native delivery use one transaction. Event cursors are per-run HMAC tokens, not global engine event offsets.

HTTP JSON/SSE is a bounded stdlib ThreadingHTTPServer listening on loopback, intended behind TLS proxy. Limits cover body size, concurrent handlers/streams, socket time, retained events, pending actions, message size/rate/outbox. Native-engine RPC is private local Unix NDJSON (64 KiB frames, one request/reply, credential-free envelope). A separate renderer connection will prevent screenshot work from saturating action RPC. The reference urllib client streams incoming chat and invokes an application callback; it does not evaluate player instructions or embed an LLM provider.

Draft broker uses one scheduler and up to four world-worker tasks, per-world grouping, hello/session.bind, dispatch/poll, native event cursor and chat outbox. Public submission persists then acknowledges without waiting for an engine RPC. Queued metadata stores engine instance before sending. Backend reconnect polls action.status; unknown delivery or changed instance becomes uncertain without blind resubmission. Request lifecycle and checkpoint durability are independent. Uncertain requests retain admission capacity until reconciled; safety does not mean pretending an ambiguous action failed.

## Independent design review: principal concern and simpler revision

The independent Sol reviewer found a deployment mismatch: Service models multiple worlds, while UnixEngine and CLI configure exactly one socket. A shared SQLite/worker pool also couples failure domains and adds scheduling complexity before it earns its cost.

Preferred revision for review: **one backend process + SQLite database + Unix engine socket per world**, with a small host/router/TLS proxy or supervisor managing independent worlds. Keep world_id in credentials/envelopes/rows as a security invariant. Replace the generic four-worker scheduler with one bounded world broker and explicit startup world configuration. Four concurrent worlds then mean four independent process groups; this matches the engine scaling model, simplifies routing/recovery, and avoids one database lock affecting unrelated worlds. Later a connection/router service can aggregate worlds without merging their mutable domain state. This is a design proposal; existing drafts are preserved pending the main peer's release.

## Alternatives and why to keep/reconsider them

- Distributed command bus + external database: rejected for the first implementation because one authoritative engine per world already serializes gameplay. Local durable state/outbox and process boundaries reach the same initial endstate with fewer moving parts. A future multihost router can replace transport without rewriting domain rules.
- Public AdminPort adapter: rejected because the native port exposes privileged console/script operations and does not supply stable identity, durable action outcome, private agent threads or mode isolation.
- HTTP directly in the C++ engine: rejected because authentication/web IO/rendering backpressure must not block or mutate simulation state from request handlers.
- In-memory queues only: rejected because accepted request/message state must survive backend restart. The existing action table is the queue; an extra generic event-bus framework would duplicate state.
- One connection per local RPC: proposed initially because framing/reconnect and bounded lifetimes are simple; its handshake overhead is acceptable only if measured. A persistent multiplexed socket is optional later, behind the same envelope protocol, without changing request identity/reconciliation semantics.
- Maintained ASGI framework vs stdlib HTTP: the framework reduces long-term HTTP/SSE protocol maintenance. The current stdlib adapter avoids dependency setup and gives a runnable local integration slice, but must be explicitly limited to a controlled loopback prototype behind a hardened proxy. Freeze domain/HTTP service APIs, not the custom server implementation; do not claim an Internet-ready security boundary based on passing unit tests.
- Generic engine result passthrough with key filtering: too brittle as a lasting policy. Visual outputs need explicit positive DTOs across every success/error/event surface; text query/action outputs should have operation-specific positive schemas/field ownership. Keep a small result vocabulary tied to the frozen engine catalog rather than an unrestricted JSON blob. Existing denylist text projection is a draft, not the final reviewed interface.
- Credential/run service: immutable mode belongs to a run; reconnect and credential rotation preserve the run. New runs intentionally do not silently reuse previous run request bodies. No public API grants ownership by company number alone. Grant/provisioning is operator-owned in this initial slice.
- Full deterministic recovery journal: not silently reduced to backend dedupe. Exact restart replay needs native+agent ordered commands and save-bound engine dedupe/results; a watermark alone is insufficient. Until joint fault tests prove it, ambiguous action status remains uncertain and recovery claims remain gated.

## Boundaries likely to become brittle

1. Action and observation names/parameters currently precede the engine's finalized catalog. Freeze a small typed catalog end to end and generate capability docs from it. Avoid a generic raw command escape hatch and avoid baking backend-only aliases into C++.
2. session.bind, hello, action.status and checkpoint meanings need a shared conformance fixture, not two independent informal implementations.
3. Native chat ingress must map engine-authenticated public key + company to an operator-authorized human principal. A nickname or arbitrary ClientID is not an identity grant. Unbound events remain private/quarantined, not broadcast.
4. Chat recipient snapshots include identity+company so a later grant or another-company run does not inherit private history. Personal blocks cannot become implicit company-wide vetoes. Native bridge delivery must carry explicit companies and scope and never default to broadcast.
5. Engine event retention gaps and malformed events need a durable quarantine/resync policy, otherwise one poison event can block all later events. Public per-recipient cursors remain entirely independent of this private feed.
6. Renderer must return opaque authorized view handles and genuine stripped PNG, with camera/worker/window revision, bounded image/action budget and internal target resolution. Renderer details are main-peer ownership; no unsupported placeholder is advertised as working visual gameplay.

## Validation and current limitations

The current draft has 29 passing stdlib tests covering HTTP JSON/SSE, scoped credentials/rotation/revocation, capacity/dedupe/cursor/checkpoint manifests, social privacy/outbox/ingress, and service projection/reconnect behavior. These are correctness evidence for the draft, not architecture approval or live engine integration. Main peer separately reported baseline C++ 94 tests/2145 assertions passing.

Remaining before a real vertical slice: settle single-world deployment; align exact engine action/query/chat/event catalog; test Unix adapter against live engine; implement positive schema projection; choose native identity mapping; connect genuine renderer; exercise real native-human + agent outcomes and save/restart fault windows. Multi-step sequences, full transport coverage, operator world supervision and provisional workload gates remain explicit remaining work.

## Proposed bounded releases after review

A. Approve/simplify durable auth/actions/events core and single-world broker boundary.
B. Freeze IPC conformance shapes, wire one actual action/query end to end; validate native conflict/order/cost and retry uncertainty.
C. Release durable social integration only after scope/identity/block/tick/delivery semantics review.
D. Release HTTP/SSE/reference client as a local prototype with a replaceable transport adapter.
E. Review renderer/view targeting and full recovery separately with their owners before public exposure.

No user approval question is needed for this internal engineering gate; the coordinating peer releases reviewed chunks.

## Revision 2: reviewed direction from coordinating peer

The main peer independently agrees with the core and single-world simplification, and requests these changes before expansion. The coordinating peer released the revision 2 core and native-chat chunks; the read-only gateway also received an independent review before implementation.

**One request sequence and one dispatch queue.** `action_requests` admission transaction allocates a monotonic `request_sequence` from the per-world row; duplicate requests reuse it without increment. Every submit/status/ack carries sequence plus immutable actor/request/company/mode/payload identity. The engine keeps bounded live dedupe, uses native frame/tick for authoritative execution order, and does not write a second per-action disk journal or allocate a competing execution sequence. The save-bound sidecar contains sparse terminal receipts and `retired_through`. A contiguous prefix retires only after save inclusion and backend durable terminal acknowledgement. Engine never executes a retired sequence. Unretired window is bounded (proposed 8192), not a lifetime cap. Backend persists an outcome before terminal acknowledgement. Lost acknowledgements are retried idempotently.

Recovery is a fence/reconciliation handshake, not blind action replay. Compare loaded save/sidecar and durable backend accepted range. Inflight or effects rolled back beyond the save become `uncertain`, then an explicit reviewed resolution may mark `resolved_rolled_back`; that resolution releases admission capacity without pretending original execution never happened. Sequence numbers are never reused. A locally rejected sequenced action (e.g. revoked before dispatch) needs a no-op terminal receipt/cancel in reconciliation so the retirement prefix cannot be permanently blocked by a backend-only hole. Need exact wire messages for terminal acknowledgements, local cancellation, sparse sidecar receipts and accepted-range reconciliation before this code is released.

**Canonical action catalog and positive DTOs.** The engine-owned ActionSpec catalog supplies names, typed parameters/targets, query and result field schemas. Backend uses it to expose supported names and validate public shape/mode/bounds, not maintain a competing full gameplay validator. Returned fields are private by default. Visual capability response advertises only usable tools/view controls; raw text actions are unavailable to visual runs. Object handles are `{id,generation}` and epoch protects restart boundaries; renderer-bound UI targeting stays a separate approved design.

**Single-world process.** Serve requires an explicit configured world and engine socket; opening a DB with another world fails closed. Keep world IDs in credentials/rows/RPCs for scope validation. One world broker thread drains bounded actions/chat/events for that process; no four-worker generic in-process multiworld scheduler. A proxy/supervisor manages four independent backend+engine+renderer groups. Host lifecycle work is separate, not implemented by accepting arbitrary binaries/paths through HTTP.

**Transport release boundary.** Keep Service API transport-independent. stdlib HTTP remains a local development adapter, not load-gate completion. Proposed bounded SSE capacity at least 64 permits the target 50 viewers in the development slice; wake on appended projected events removes an intentional 1-second polling floor. A maintained ASGI adapter behind TLS remains preferred for release; measured latency/concurrency gates determine acceptance, not slot configuration alone.

**Native chat revision: single routing/moderation authority.** When integration is enabled, engine captures authenticated native ingress *before* native wire delivery. NativeRoster is a trusted engine event, never a public API claim, carrying stable authenticated public key, current company and ephemeral native routing hint. Backend derives/persists stable human identity and membership/run binding from verified roster data; agents remain operator-provisioned. Ingress has stable engine-instance-qualified ID, desired explicit world/company/client-direct scope, authenticated sender key/company and body. Backend resolves that intent to one persisted thread/message and exact authorized recipients, applies rate/block rules once, and atomically persists message/recipient snapshots/events/native outbox. `chat.publish` carries approved stable message_id + exact native target principal/company snapshot; engine dedupes delivery and emits no new ingress echo. The engine owns verified ingress/native UI delivery only, not thread ACL, moderation or a second conversation database.

Native outage policy is bounded and fail-closed: unknown/private scope or missing participant binding never falls through to broadcast; engine retains bounded unacknowledged ingress or gives a native-visible unavailable status. Native client-to-client intent must resolve to stable principals from the captured roster, not the receiver's later reused ClientID. Native spectators require an explicit world-chat-only principal binding (no company-scoped actions); company membership changes revoke old run access while preserving stable identity/history policy. Initial safe subset can gate unreviewed direct/spectator routing explicitly, but cannot claim full MVP communication until exercised.

Revision 2 independent review completed and the main peer released the core and canonical native-chat implementation. A separate gateway review approved immutable run/socket mapping, serialized artifact access, positive PNG validation and bounded caching. Live acceptance and visual construction remain separate gates. See architecture.md for the implemented design.
