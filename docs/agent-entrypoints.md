# Public agent entrypoints

Players can start from `/llms.txt` (structured observations) or
`/llms-capture.txt` (native images) on the game's API origin. These guides and
`/agent/client.zip` are public, version-matched assets. The ZIP contains only an
explicit allowlist of dependency-free player modules, public schemas, guides,
license and a file-hash manifest. It contains no credentials or server state.

An operator enables fresh-company admission explicitly:

```sh
python tools/mixed_world.py --binary /path/to/openttd --baseset /path/to/baseset \
  --state-dir /tmp/my-game --world my-game --profile luna --agent-joins \
  --native-port 3979 --http-port 8080
```

Use the Python environment with `backend/requirements.txt` installed for the
default ASGI transport. The `luna` profile provisions two text and two visual
companies; `mixed` provisions one of each and a separate human company. Admission
claims these existing companies. It creates no new company, renderer or account.
The launcher prints the guide URL once the world is ready. A player prompt can be:

> Read http://127.0.0.1:8080/llms.txt and start playing. Your goal is to build a
> profitable transport company. Cooperate and compete with other companies.

For image-based play, use `llms-capture.txt`. The host needs a tool that actually
shows local PNGs to the model, or the optional capture MCP integration. Typed
commands are available in both modes. Capture players inspect game state through
images; successful downloads alone do not establish image perception.

## Admission and recovery

Joining is disabled by default. With `--agent-joins`, any client that can reach
the enabled endpoint may claim an available agent company. Human credentials are
excluded. Each player uses a separate private local state directory. The client
persists its origin, mode and random claim secret before its first join POST;
the server atomically binds the secret's hash to one slot. A lost reply is
recovered with the same saved claim, including during a native-game outage.
Changed mode conflicts. A new state directory means a new claim and can consume
another company. Slots never expire or recycle automatically.

Fresh claims require a ready world, the chosen existing native company and, for
visual players, its connected renderer. An unavailable first eligible slot
returns `join_not_ready`; admission does not scan every renderer or create a
replacement company. Gameplay continues to enforce normal ownership and native
validity after joining.

The launcher writes a private `agent-join-pool.json` that references its original
private token files. Backend-only deployments can pass the same format with
`python -m backend serve --agent-join-pool /absolute/private/pool.json`:

```json
{"version":1,"world_id":"my-game","world_epoch":"OPERATOR_EPOCH",
 "runs":{"EXISTING_AGENT_RUN":{"token_file":"/absolute/private/agent.token"}}}
```

The pool must contain distinct agent companies in the same world/epoch. Visual
runs require configured renderers. Inputs must be bounded owner-only regular
files at canonical absolute paths. The Store keeps claim hashes and bindings,
not plaintext tokens; existing operator token files remain the durable source.

Restart with the same world state and private files. Revoked claims remain
consumed and return `join_revoked`. Live credential rotation also prevents replay
of the old token. Before restarting after rotation, update the configured token
file: an old file that no longer matches its declared run fails startup. Removing
a pool slot never causes its historical claim to acquire a different company.
Starting without `--agent-joins` disables admission while preserving saved claims.

## Design and validation scope

Independent architecture review preceded implementation. The finite pool avoids
dynamic company/process lifecycle and a second credential store. Both transports
share one admission handler and immutable public asset builder. Native readiness
checks stay outside short SQLite claim transactions, under one admission lock;
exact recovery precedes those checks. Player actions retain the existing journal
and native execution rules.

See [capture panel coverage](capture-panel-coverage.md) for current image limits
and the [micromanagement audit](agent-micromanagement-audit.md) for separate
mechanical follow-ups. URL-only onboarding and successful transport businesses
are separate acceptance gates; the fresh mixed Luna trial records both.

On 2026-10-18, validation passed 436 backend tests and a final 34-test
client/asset/launcher selection. Independent implementation reviews covered
admission, public assets, the portable client and guide examples. Native code
was unchanged for this feature.

A separate live QA world exercised both literal guide bootstraps outside the
checkout with `python -S`: text and visual claims, a readable native finance PNG,
the guide's wide map PNG, two owned buses, order-token preparation/copy and exact
replay. After a complete world/backend/renderer restart, a simulated lost join
reply recovered the original company and token from its saved claim. The final
regenerated client also completed native order-copy. This is mechanical QA, not
evidence of a Luna-operated paying business. Local evidence is retained in
`/tmp/ottd-url-onboarding-acceptance01/evidence.json`; the tested milestone client ZIP
was 356,236 bytes, SHA256
`bcf1ad96ccfb89085c93dc900aeda916ca291b65c822690a6f67eb5e99567ee2`.
Python 3.10 syntax was checked; actual runtime checks used Python 3.13.9.

The subsequent [Luna trial](luna-trial-05.md) exposed ambiguous pause wording:
`pause.command_policy` applies only while `pause.paused` is true. The guides and
schema now state this explicitly, with an unpaused `no_construction` example.
Independent native-source review and a fresh Luna comprehension check passed.
The running trial received the same protocol clarification for all companies;
its unaided URL-only business result remains qualified.
