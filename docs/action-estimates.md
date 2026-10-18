# Action estimates

`POST /v1/actions/estimate` asks the authoritative engine to test one existing
typed action using normal human Shift estimation. Text and capture players use
the same endpoint and action parameters. Capture players receive direct command
feedback and continue examining game state through PNGs; no screenshot lease is
required to estimate a chosen typed action.

```json
{
  "action": {
    "name": "sign.place",
    "parameters": {"tile": {"x": 12, "y": 20}, "name": "Candidate location"}
  }
}
```

The optional `preconditions` field currently accepts only `{}`. There is no
`request_id`, action receipt, checkpoint receipt or gameplay event. Every call
recomputes against the current state. Submit a separate ordinary action with
its normal request identity to actually act.

An admitted native test returns HTTP 200 even when ordinary gameplay rules
reject the proposed action:

```json
{
  "estimate": {
    "success": false,
    "cost": 0,
    "error": {"code": "gameplay_rejected", "message": "Native gameplay feedback"},
    "advisory": true,
    "test_and_execute_may_differ": false
  }
}
```

The finite result contains success, signed native-money cost, an optional native
error, and the two advisory flags. Cost is never charged by estimation. Negative
cost denotes estimated income. Native error text is bounded UTF-8; escaped JSON
is separately bounded to 4096 bytes. Created references, capacity/refit data,
orders, map attributes, balances and simulation ticks are omitted in both modes.

`advisory` is always true. An estimate does not reserve terrain, entities, money
or pool slots. Native Shift estimation also skips the final company funds
check. `test_and_execute_may_differ` identifies native commands whose NoTest
trait explicitly permits different test and execute outcomes. Normal execution
can fail or produce a different result later.

Native NoEst commands, including company settings and company joining policy,
return `estimate_unavailable`; they never fall back to execution. The optional
image-bound `ui.apply` operation also has no estimate path. Invalid/stale handles,
order/page versions and ineligible company targets retain their ordinary guards.
Policy or validation failure means no native test was performed and uses the
normal error envelope instead of an estimate.

Malformed public shape/schema returns 400. Native preparation, stale-version or
eligibility failures use generic 409 `estimate_unavailable`; private diagnostics
are not returned to distinguish them. Rate denial is 429. An unavailable or
recovering authority, or checkpoint-busy session binding, returns 503.

The engine admits at most one estimate start per second across the world,
independently of the text/refit/control-token budget. `estimate_rate_limited`
means callers should back off. There is no per-company fairness or hard latency
guarantee; loaded NewGRF callbacks and existing industry dependency scans can
take longer. Input and construction bounds are the existing typed action bounds.
Native discovery advertises the endpoint under `conventions.action_estimate`
with `limits.estimate_interval_ms` and `limits.estimate_result_bytes`.

The private RPC is `action.estimate` with the normal actor/world/epoch/engine
fences and bound company. It runs on the simulation thread and refuses recovery,
unhealthy/replaced worlds, checkpoint publication, and renderer-worker routing.
It never enters the action ledger, submission queue or renderer intent path.

Internally, a request-local invocation chooses between the existing native Post
and an exact native test adapter at the same single-command dispatch leaf. The
submit branch retains its existing arguments and completion behavior. The test
adapter preserves native Location and ClientID conversions and calls
`Unsafe(..., estimate_only=true)` without a callback or UI PostResult handling.
It does not set Shift globals, call QueryCost, duplicate action preparation, or
modify the native command core. Future multi-command actions need a separate
estimate design; a single-leaf quote must not silently become a partial quote.

Both random streams are restored around target validation and preparation,
including generated town names and industry seeds. Native test mode restores
town ratings and existing PSA values. Native Shift auxiliary effects can remain:
new empty PSA owner storage, consumed pool generations, native temporary-object
bookkeeping and logging. Temporary build/refit vehicles receive normal native
cleanup, and monotonic generations are never rewound. The guarantee is no
gameplay execution or charge, rather than a byte-for-byte rollback of every
engine auxiliary structure.
