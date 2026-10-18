# OpenTTD human and agent integration

The integration extends the native multiplayer simulation with authenticated external company control, structured observations, genuine rendered views, and persistent conversations. The requirements source is `openttd_requirements.docx` version 0.1. Browser gameplay in this milestone is viewing and chat; construction remains available through a matching native client and the agent action API.

## Baseline and build

The upstream baseline is `c0d5c0b3fc2588ac001e301e97ce795aefb8a90c`, dated 2026-10-15. It uses C++20 and CMake. Build the experimental integration explicitly:

```sh
cmake -S . -B build-integration -DOPTION_INTEGRATION=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build-integration --parallel 8
ctest --test-dir build-integration --output-on-failure
```

PNG and LZMA are required for the rendered-view and persistence tests. Install OpenGFX in an OpenTTD base-set search directory. A graphical build can use SDL2 for humans and the `framebuffer` video driver for private rendering workers.

Native clients compare both the baseline hash and the modification marker during admission. A modified server does not automatically admit a pristine upstream binary. Ship a matching human client until a real compatibility test establishes otherwise; preserving packet layouts alone is insufficient evidence.

The live compatibility test on this baseline confirms that requirement: the pristine `20261015-master-gc0d5c0b3fc` client receives native `WrongRevision` error 8 from the integration server. Matching clients join successfully. Keep the pristine executable separate when repeating this test.

## Process boundaries

Each world has one authoritative engine process, one backend process and SQLite database, and separate native rendering workers. Four worlds use four independent groups. The backend exposes the versioned JSON API and authenticated event stream. A reverse proxy supplies HTTPS for deployment.

The engine owns gameplay, prices, ownership checks, company state, and shared time. The backend owns identities, immutable observation modes, durable request history, conversation membership, moderation, and public response projection. Rendering workers receive authoritative native multiplayer state privately and send only rendered images to public callers. Model inference belongs in external clients.

The engine adapter is a private Unix socket with mode `0600` and same-user admission. It uses version 1 newline-delimited JSON with bounded frames, clients, work per tick, and output. It is an internal service interface: public bearer credentials cannot become administrator, console, script, or save-file access.

## Action and observation contract

An action carries a stable identity, company, immutable run mode, unique request ID, and a backend-assigned request sequence. The backend derives these from authentication. The sequence identifies retries and retirement; it does not specify gameplay execution order.

One `ActionSpec` catalog defines names, parameter schemas, target kinds, supported modes, and permitted result fields. The 103 typed gameplay actions are shared by text and capture players; their writes use the same dispatcher and normal `Command<...>::Post` path. Typed writes do not require an image ID or screenshot lease. There is no public native-command-byte dispatcher. Capability publication identifies the implemented ordinary company actions and separately excludes operator/internal operations and local GUI preferences.

Agent commands enter bounded company queues and share native authoritative distribution and execution. Server-local request correlation is an opaque token; it is absent from native packets. Actual execution produces the outcome. Accepted or queued responses never mean that construction succeeded.

The initial scheduler admits at most one integration gameplay command per frame, ahead of native commands, and requires `network.frame_freq=0`. It rechecks entity generations immediately before publication. Skipping a command only on the server after publication would desynchronize replicas.

Object targets use `{id, generation, engine_instance_id}` handles. Every allocation receives a fresh pool generation; the opaque instance field identifies the engine boot that produced the handle. Both checks occur before a fresh operation can target an object. After a restart, text players can use a new observation and either mode can resolve one explicitly chosen target through `POST /v1/control/reference`. A raw ID resolves its current occupant, not a promised continuation of an object from a previous boot. Historical receipts keep their original handles, including the earlier two-field shape, and exact retries return their stored outcome without repeating the operation. The current private RPC envelope never replaces a handle's captured instance. Structured world queries are bounded and available only to text sessions.

The control-reference operation returns only a single actuator reference and applicable opaque order/page mutation tokens, or an engine ID. It has no image prerequisite and returns no lists, names, positions, cargo, prices, finances or other world/report attributes. Its closed selector schema is published as `conventions.control_reference`. Created references can also accompany successful creation commands. Capture typed-action receipts permit ordinary success, actual charged cost, native gameplay errors and created references; capacity and other structured world attributes are excluded. See the [capture-player contract](capture-controls.md) for reference selectors and the optional native UI route.

## Rendered observation

A rendering worker uses the normal game drawing and windows with a real framebuffer. Its role blocks locally originated gameplay before command validation and network sends, while allowing authoritative command replay. Camera and report controls use an explicit allowlist; console, settings, save, and company-switch shortcuts are unavailable.

Screenshots are metadata-free PNG artifacts in a private directory. The worker retains a bounded number of images and limits capture frequency. Private IPC returns an immutable artifact reference; public delivery returns binary PNG through an opaque authenticated image ID. Image bytes and filesystem paths do not enter the public JSON API.

Image inspection references a screenshot and screen pixel. The worker verifies camera, window layout, replica lifecycle, and selected object identity before opening the corresponding native window. Its response does not expose resolved coordinates, object attributes or report values. Capture players examine the resulting PNG. Shared typed actions and the separate single-target control-reference operation provide actuator feedback and references without enabling structured world readers. Public chat and participant metadata remain available normally.

`ui.apply` is an optional native-input interface tied to the run's latest image. It supports click, drag, button-free pointer movement, approved text replacement and two finite tool-state controls. The worker handles ordinary native GUI input while intercepting command submission. It captures at most one native command into a private immutable bundle; the backend persists that bundle before authoritative dispatch and never repeats the gesture on retry. A gesture that only selects a tool or changes a window has an empty command bundle. UI receipts provide workflow status, while subsequent genuine PNGs provide UI feedback; this restricted receipt shape is separate from ordinary typed-command outcomes.

For native UI input, the worker parks its own replica at the captured frame, while the authoritative simulation continues. The authority checks source-frame allocation watermarks, company and authenticated native owner, relevant object state, and affected tile dependencies before publication. Native callback ownership supplies ordinary rendered success/error feedback. Automatic callbacks that would build additional connections or attach wagons are blocked with visible feedback; players can submit the individual typed commands or use fresh image-bound construction/depot gestures for those steps. There is no multi-command atomicity claim.

The default image lease for optional native UI input is at most eight seconds, shortened below the authoritative native lag limit; the Luna profile requests a 40-second ceiling. Expired views require a new image for a new gesture. Typed writes and control-reference calls do not require a screenshot lease. Repeated capture requests may return the parked image without extending its lease or cache expiry. The backend coalesces requests for the same view to at most one native capture request per second. Tabs using the same run share its camera and image. Independent interactive views require measured worker capacity.

## Conversations and native identities

When integration is enabled, native conversation ingress is intercepted before delivery. The engine records authenticated sender key and company, scope, body, and tick in a bounded pending queue. The backend polls `chat.pending`, applies membership and moderation rules once, and persists the message, recipient snapshot, and delivery work before acknowledging ingress. Queue exhaustion rejects additional chat to its sender without pausing gameplay.

`NativeRoster` supplies authenticated native keys and current company membership. The backend associates these with persistent human identities; nicknames are display labels. Agent company grants remain operator-provisioned. Identity-direct and company-direct conversations are separate scopes.

Native delivery contains exact recipient keys. The engine sends existing native server-message packets with a sanitized human or agent label, without expanding company scope or broadcasting an unknown destination. Agent-only companies remain valid conversation participants with no native sockets. Native chat bodies have one pending/acknowledgement channel rather than a duplicate game-event feed.

## Checkpoints and request retention

SQLite durably owns accepted public requests and their historical results. The engine retains active deduplication receipts in memory; action admission and completion do not rewrite files on the simulation thread.

A checkpoint captures the game snapshot together with sparse terminal receipts and a retired request prefix. A private sidecar names the exact save generation and checks its bytes. The checkpoint becomes durable only after the save and sidecar are published successfully. Earlier generations remain available until publication completes.

Private sidecar format 2 qualifies saved and executed native frame numbers with their engine boot IDs. Frames are compared within one boot; game ticks and the exact save identity remain comparable across restarts. This permits another checkpoint immediately after a restarted native server resets its network frame counter.

The backend may acknowledge a terminal prefix only after persisting every outcome in that prefix. The engine retires known receipts only when they are terminal and included in a durable checkpoint. Backend-only rejected requests need no dummy engine command. Any later request at or below the retired prefix is refused execution; the backend serves its historical result.

Recovery verifies the exact loaded save, fences new writes, and reconciles accepted requests. Work missing from the checkpoint becomes uncertain and is never blindly replayed. Explicit recovery may classify it as rejected or rolled back and publish a correction. This bounded rollback policy avoids inventing a complete replay journal for native and agent interleavings. The measured checkpoint/recovery window must accompany release claims.

## Architecture review decisions

Independent design review precedes full implementation of every major piece and is separate from correctness review. The initial reviews selected these simpler boundaries:

- One backend and database per world instead of a shared scheduler with one engine socket.
- One typed action catalog instead of separate handwritten capability and projection registries.
- One request/reply IPC exchange with status lookup and bounded event polling instead of simultaneous push and RPC protocols.
- Live engine deduplication and save-bound receipts instead of a complete manifest rewrite on every action.
- One backend request sequence instead of competing admission and execution counters.
- One native chat pending queue and one backend ACL owner instead of post-delivery observation and duplicated routing policy.
- Private image artifacts instead of oversized base64 JSON frames.

Implementation review must revisit these decisions when measurements or a concrete missing capability justify a change.

The earlier native UI review removed generic callback-child forwarding, inherited leases, and parent receipt machinery. Individual actions or fresh native gestures compose those steps. The shared-actuator review then separated perception permissions from write permissions: capture players use the same typed action path while structured world readers remain text-only. The launcher review also requires learning each renderer's authenticated key from its private handshake and confirming that exact key in the authoritative roster; player nicknames never establish process identity.

The restart review selected explicit engine-instance fields on public handles rather than persistent allocation counters or salted generations. Historical exact retries precede current action-schema validation and preserve their original receipts; fresh operations require current handles from text observations or explicit control-reference resolution. Recovery resolution compares the same canonical request bytes as admission before changing an uncertain outcome.

The measured load reviews kept image downloads as authenticated reads of immutable cached bytes, independent of worker RPC and control leases. They raised existing bounded response/API admission to fit the known 54-client burst, while keeping image lookups at eight threads. No additional queue or capture-cache concurrency protocol was introduced. Measurements and remaining fault limits are recorded in [the validation ledger](integration_validation.md).

## Running and checking a local world

Use [the local launcher](../tools/README.md) to start a native server, ASGI backend, and two native rendering workers with persistent credentials. It creates separate human, text-agent, and visual-agent company bindings. Restart with the same private state directory. It refuses missing or corrupt checkpoints and incomplete provisioning rather than creating a replacement world under existing credentials.

The backend's sole checkpoint timer defaults to 30 seconds. It drains admitted work, creates a save-bound checkpoint, persists outcomes and acknowledges the safe receipt prefix. That configured interval is not a measured recovery bound: snapshot/write duration and failed checkpoints also contribute. The launcher stops at the last completed checkpoint boundary.

Native unit tests run through `openttd_test` or CTest. Opt-in live tests are in `tests/integration/`: `test_engine_live.py` exercises real engine construction, four working passenger services, ownership, retry, stale allocation and restart behavior; `native_chat_live.py` uses actual native clients and tests private scopes and pristine-client admission. Set `OPENTTD_TEST_BINARY`, `OPENTTD_TEST_BASESET`, and, when needed, `OPENTTD_TEST_LIBRARY_PATH` for local dependency paths. The benchmark and its exact claim boundaries are documented in [LOAD.md](../tests/integration/LOAD.md).

## Acceptance and reference configuration

The integration acceptance gate requires a native human, a text agent, and a visual agent in separate companies, working routes and modifications, all four transport modes, visual isolation, cross-client private conversations, retry consistency, and save/restart recovery. A road-only demonstration is an intermediate integration test.

The provisional load gate remains 30 minutes at normal speed on a 512 by 512 map, 500 active vehicles, eight mixed companies, 50 browser viewers, and four visual agents at up to one screenshot per second. Measure p95 chat delivery below one second and acknowledgement below 500 ms, plus screenshot latency, simulation speed, queue depth, and four independent worlds. A revised target requires measured evidence before release.

The initial local reference host reports an AMD Ryzen Threadripper 3970X, 32 cores and 64 threads, approximately 64 GiB RAM, and Linux under WSL2. Record build type, dependencies, CPU allocation, configuration, and concurrent host load with every benchmark. Unit and syntax tests do not establish the multiplayer or load gates.

See [the validation ledger](integration_validation.md) for completed acceptance evidence, exact build attribution and workload results.
