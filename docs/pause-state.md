# Pause state and gameplay

Both text and capture clients can read `GET /v1/world/status`. Capabilities
advertise it through `world_status: {endpoint: "/v1/world/status", event:
"world.status"}`. This is shared control metadata; it exposes no company report,
simulation tick, native command identifier or pause/resume control.

The response contains `world_id`, `world_epoch`, `engine_instance_id`, a
run-scoped `event_cursor`, and:

```json
{
  "pause": {
    "paused": true,
    "reasons": ["manual"],
    "command_policy": "no_construction",
    "revision": 3
  }
}
```

Reasons use this fixed order: `manual`, `saving_loading`, `joining`, `error`,
`insufficient_clients`, `game_script`, `link_graph`. `other` covers a paused
native state with no recognized functional reason. Native bookkeeping for a
command executed during pause does not create another reason or notification.

Policies are the native `no_actions`, `no_construction`, `no_landscaping` and
`all_actions` levels. They describe the saved native policy **while `paused` is
true**, rather than an always-active permission. For example, `paused:false`
with `command_policy:"no_construction"` permits ordinary construction; do not
wait for the configured policy string to change in an unpaused world. This policy
cannot be changed during a network game; OpenTTD marks this setting
`NoNetwork`. Choose it before hosting. Native company
settings remain exceptions even under the `no_actions` label. Each actual
command still passes the native predicate and all ordinary ownership, target,
snapshot and permission checks. No second action-name allowlist governs pause.

Read status before subscribing to `world.status` events from its returned
cursor. Apply only a newer revision within the same world epoch and engine
instance. A new fence or an expired event cursor requires a fresh status read;
engine instance identifiers have no chronological ordering. Baseline events
identify the current state, and `history_gap` means intermediate transitions
were lost. An unavailable status read must not be presented as a current cached
paused/unpaused value. Revisions measure metadata changes, not elapsed game time.

Commands permitted by the native pause policy can execute while paused. Pure UI
navigation, text entry and PNG viewing retain their ordinary rules. A pause does
not extend a screenshot's lease or permit host pause/resume or fast-forward.

A command queued before a pause can become forbidden before publication. The
integration then rejects it before execution or charging. A typed receipt uses
`world_paused`; a captured gesture retains the ordinary generic visual error.
Exact retries preserve that rejection. After resume, deliberately submit a new
request ID with current targets if the operation is still wanted. Native human
network queues can instead retain such a packet until resume; this API provides
the same permitted actions with an explicit rejection for that queue race.
Terminal handling keeps the existing checkpoint drain and recovery guarantees.
