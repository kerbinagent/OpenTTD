# Custom GameScript news

`news.custom` exposes accepted custom GameScript news to text players. It is a
company-shared history for the current authority process, with no saved-game or
restart recovery. It does not reproduce client popup order, local dismissal,
news preferences or other native news producers. Capture players read native
news in their rendered game. `CreateCustomNewsItem` is not a player action.

The Execute hook runs after native validation and before local-company filtering.
It captures only on the integration authority, including before the first bridge
tick. Ordinary native clients and renderer workers do not retain this history.
The native command outcome and native client delivery are unchanged.

## List and recipient cursors

The text-only query accepts the strict object `{after?: NewsCursor, limit?: 1..32}`.
No company selector, filter, acknowledgement or mutation is available.

```json
{"id": 12, "generation": 348, "engine_instance_id": "opaque-current-boot"}
```

A `NewsCursor` has exactly these three fields. `id` is a recipient-local sequence
from 0 through INT64_MAX; a row target requires a positive ID. `generation` binds
the authenticated company's native incarnation. Cursors from another company,
company-slot incarnation, engine boot or beyond the latest issuance are rejected.
A new company does not receive an earlier broadcast. Two participants in the same
company share its retained history. Other companies' private news does not
advance this company's sequence.

The result has `tick`, `company_id`, `history_scope: "current_engine_instance"`,
`history_start`, `latest`, `gap`, `items`, `next` and `complete`. The three cursor
fields describe this recipient only. `history_start.id` is the inclusive loss
watermark; retained rows come strictly after it. Without `after`, the reader
starts at 0 and sees `gap: true` if history has already been lost. A cursor below
the watermark resumes from that watermark with `gap: true`. Rows are oldest
first, exclusive of `after`. Empty polls return a stable `next`. `complete`
means `next.id == latest.id` for this response, not a pinned future snapshot.

Rows contain only `news_id`, `issued_tick`, `calendar_date` (`year`, month 1..12,
`day`), `news_type` and `reference`. Lists do not decode bodies or expose previews,
encoded bytes, rendered lengths or content versions. The finite news types are:

- `arrival_company`, `arrival_other`, `accident`, `accident_other`, `company_info`
- `industry_open`, `industry_close`, `economy`, `industry_company`, `industry_other`
- `industry_nobody`, `advice`, `new_vehicles`, `acceptance`, `subsidies`, `general`

A reference is `null`, `{kind: "tile", location: {x,y}}`, or
`{kind: "engine", engine_id}`. Entity references have
`{kind: "town"|"industry"|"station"|"vehicle", target: Handle, location: {x,y}|null}`.
The ID, native generation and location are captured at issuance. These values
remain historical: reads never resolve an old numeric reference into a new
handle. Ordinary actions still reject stale handles. Inline text arguments have
the live native presentation semantics described below, independently of this
explicit historical reference.

## Selected text

Select an exact retained row through the existing `display.text` query:

```json
{"kind":"news_custom_text","target":{"id":12,"generation":348,"engine_instance_id":"opaque-current-boot"},"limit":2048}
```

Only `kind`, `target`, `after_byte`, `limit` and `expected_content_version` are
accepted for this branch. The usual native display result includes `kind`,
`target`, plain UTF-8 `text`, `content_version`, `total_bytes`,
`next_after_byte`, `tick` and `company_id`. Nonzero offsets require the expected
64-character lowercase hex content version. Invalid UTF-8 boundaries, evicted
rows and stale versions are rejected. The shared native world-text/refit gate
admits at most one render start per second across these selected readers.

The retained body is owned encoded input, as in native human news. Visible text
uses current native names, localized strings, units and the recipient company's
normal query context. A rename, language change, deletion or inline numeric-ID
reuse can change its text and version. Restart at byte 0 after a stale version;
this interface does not pin an immutable rendering across pages. Explicit
historical reference metadata never rebinds, even if an inline name changes.

## Bounds and reset

Each company incarnation retains at most 1,024 rows and 4 MiB of encoded body
bytes. One retained body may contain at most 65,536 encoded bytes, checked before
copying. Oversized observations still advance the recipient sequence, discard
the previous prefix and set the loss watermark to that issuance. A later normal
item resumes the ring. Count and byte overflow evict the oldest prefix. Sequence
exhaustion makes this recipient's history unavailable without wrapping IDs or
rejecting the native news command.

A broadcast copies at most 15 times 65,536 encoded body bytes and performs no
observer-side decoding, callback or company-context switch. Logical retained
encoded bytes are at most 60 MiB across 15 companies, plus bounded row metadata
and allocator overhead. Lists return at most 32 rows within 40 KiB; selected text
pages return at most 2,048 UTF-8 bytes within the existing 40 KiB response bound.
These limits do not cap one native render's CPU time, recursion or temporary
allocation. This is the same selected native-render contract as current town,
goal and league `display.text` sources.

The existing early `InitializeGame` presentation reset clears rows before pool
cleanup. A still-live company incarnation keeps its sequence and moves its loss
watermark to the previous latest issuance. Numeric company-slot reuse gets a
fresh generation and empty namespace. Disconnect/reconnect preserves retained
history in the same process; local native news deletion does not clear it.

The common native formatter now validates DECIMAL precision 0..18 before power
arithmetic and keeps the normal invalid-parameter fallback and argument order.
DEPOT formatting validates the four native transport types, checks depot IDs,
and safely handles an unnamed depot without a town. Aircraft station formatting
and valid ordinary names retain their native behavior. These narrow guards do
not establish a general bounded or hardened text formatter.
