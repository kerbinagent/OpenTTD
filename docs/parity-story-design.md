# GameScript player interaction design for review

The design below received independent architecture review before implementation.
Implemented family ownership is `src/integration/actions_story.cpp` and focused
tests. Root owns the central command/schema registry, native hooks, actor/visual
context and backend positive projections. Exact implementation schemas and hook
handoff are in `parity-story-integration.md`; they supersede draft details below.

## Native behavior and the actual gap

`CmdGoalQuestion` validates a script question and, during execution, filters by
`_local_company` / `_network_own_client_id` before creating a `GoalQuestionWindow`
(`goal.cpp:243-272`). A dedicated server therefore has no retained question
presentation. `CmdGoalQuestionAnswer` only validates the button enum; it accepts
an arbitrary uint16 question ID and emits a GameScript event (`goal.cpp:285`).
A public action cannot safely expose that primitive without checking an actual
presentation and its offered buttons.

Story pages and elements already have authoritative, saved pools, including
company, title, ordered elements and button metadata (`story_base.h:140-177`).
Their updates do not allocate a new incarnation, so handles alone do not detect
changed instructions. Native ordinary players may browse any valid company's
storybook through the toolbar company menu (`toolbar_gui.cpp:185,622,634`;
`story_gui.cpp:124`). Reading and interacting have different permissions:
`CmdStoryPageButton` permits interaction only on the actor's own or global page
(`story.cpp:477-484`).

The native vehicle-selection gesture canonicalizes to a primary vehicle, accepts
any owner's vehicle, and checks the button's requested vehicle type
(`vehicle_gui.cpp:3510`; `story_gui.cpp:907-922`). Generic owned-vehicle action
readers are unsuitable for this read-only selection.

## Public surface

All objects reject unknown properties. Ordinary entity handles remain
`{id,generation,engine_instance_id}`. New story page/element IDs are 0..63999.
Question handles use the same shape, with native question ID 0..65535 and an
authoritative issuance generation rather than a pool generation. Every action
also retains the normal world/epoch envelope and request deduplication.

| Action | Required fields | One normal native Post |
| --- | --- | --- |
| `goal.question.answer` | `question_id:handle, button:enum` | `GoalQuestionAnswer(private_validated_id,native_button)` |
| `story.button.press` | `element_id:handle, page_version:string` | `StoryPageButton(TileIndex{},element_id,VehicleID::Invalid())` |
| `story.button.select_tile` | `element_id:handle, page_version:string, tile` | `StoryPageButton(tile,element_id,VehicleID::Invalid())` |
| `story.button.select_vehicle` | `element_id:handle, page_version:string, vehicle_id:handle` | `StoryPageButton(TileIndex{},element_id,selected_primary_vehicle)` |

Question button strings are exactly the 18 native choices: `cancel`, `ok`, `no`,
`yes`, `decline`, `accept`, `ignore`, `retry`, `previous`, `next`, `stop`, `start`,
`go`, `continue`, `restart`, `postpone`, `surrender`, `close`. Admission requires
that the selected button was actually offered; Cancel is not an implicit answer
to every question. Informational presentations with zero buttons have no answer
action. Closing a window without answering is a local UI effect, not a fabricated
Cancel GameScript event.

The three story actions discriminate the native element type; no unused tile or
vehicle parameter is accepted on a push action. Tile selection is an explicit
valid map tile. Vehicle selection accepts any owner's live primary company
vehicle and enforces `StoryPageButtonData::GetVehicleType()` when specified.
Do not inspect an entire consist or pin moving location/speed for this selection.
It is not an order edit, refit or ownership-sensitive vehicle mutation.

Buttons are not consumed after success: repeated deliberate presses are legal.
Exact request retries still emit at most one native command through the existing
ledger. Native command completion means the script event was submitted; it does
not promise that GameScript accepted the player's business choice.

| Text query | Parameters | Positive result |
| --- | --- | --- |
| `goal.questions` | optional `after_id,after_generation,limit` | Company-inbox handles, type, offered buttons, text preview; paired ID/generation continuation |
| `goal.question` | `question_id:handle`; optional `text_offset,text_limit` | Same metadata plus a complete bounded text chunk and continuation offset |
| `story.pages` | optional `book_company_id:int[0,14]\|null,after_id,limit` | Page handle, book company, sort order, date, title preview and `interactive`; default actor's book, null selects global book |
| `story.elements` | `page_id:handle`; optional `after_id,limit` | Page metadata, full `page_version`, element handles/order/type/text preview and typed location/goal/button metadata, cursor |
| `story.element` | `element_id:handle`; optional `text_offset,text_limit` | Current parent page handle/version, element metadata and full bounded text chunk |
| `story.vehicle_candidates` | `element_id:handle,page_version`; optional `after_id,limit` | Minimal selectable primary-vehicle handles, owner, type, display name and location; no other company's private orders/cargo/settings |

Use existing list defaults and limits, with at most 32 returned rows and 4096
examined discovery slots per call. Text previews can be 512 UTF-8 bytes. Detail
chunks default to 4096 and are at most 8192 UTF-8 bytes, with an explicit next
byte offset and total decoded size. Offsets must lie on UTF-8 boundaries. A long
ordinary prompt or story paragraph is readable through chunks; it is not silently
discarded because it does not fit one response. Keep the existing total response
byte budget. Neither query nor capture consumes simulation or interactive RNG.

Story reading permits the selected valid company or global book, matching the
normal toolbar. Orphaned pages belonging to a deleted company must not become
readable through a stronger path than the normal book selector. Mutation checks
actual actor company independently of which book was read. Expose global/own
button metadata normally and mark other-company buttons noninteractive.

`story.vehicle_candidates` exists only to discover the public selection outcome:
ordinary text vehicle lists are currently owned-only. It derives any requested
type from the visible button; there is no arbitrary query expression. Visual
agents never receive this or other structured query payloads.

## Company question inbox and exact native popup proof

The headless authority retains an independent text-company question inbox, not a
model of each native client's popup stack. Each accepted company/global issuance
contains native ID, monotonic authority issuance generation, original target,
type, offered buttons and encoded text. Capture eligible company incarnations
at issuance, sharing the immutable body across recipients. Replacement companies
do not inherit old prompts. Client-private prompts stay in the real recipient's
native windows and are never copied into company text observations.

Keep an issuance deque per native ID/company incarnation. Duplicate IDs do not
replace or quarantine earlier issuances. Text answer handles select an exact
issuance and may answer an older one. The normal queued operation retains that
selected handle in its existing target capture. During native Execute,
`ExecutingQuestionGeneration(company,id)` recovers the selected generation from
that already executing text operation. Retirement removes exactly that row;
an intervening close that already removed it causes no retirement, rather than
consuming an unrelated duplicate. No native command argument or GameScript
event field is added.

A generic native company answer pops one newest company-inbox issuance for that
ID. Deity CloseQuestion pops one newest issuance per company. This is an explicit
independent inbox policy; actual native clients can move their windows into a
different z-order and continue their normal close behavior. Do not claim that
virtual inbox order mirrors all native popup orders. The native event retains
only qid, company and button.

Retention has no TTL, LRU or duplicate count cap. Its cost follows accepted
ordinary native issuances; unlike the earlier quarantine proposal, uint16 IDs
are not claimed to bound duplicate storage. Share immutable bodies, paginate
observations, and prune dead company recipients at same-ID mutation or reset.
Keep at most 4096 inspected rows/slots and 32 returned rows per discovery call,
plus the existing response-byte budget. `goal.questions` uses paired
`after_id,after_generation` and paired next fields so one ID can span responses.
For compatibility `after_id` alone skips that whole ID. A complete world
presentation reset clears rows but never resets the issue counter within an
engine boot. Save/load neither persists questions nor replays old answers.

Visual capture instead attests the actual clicked live `GoalQuestionWindow`.
A single gesture origin is established from the real hit-tested window/widget
before native input. Inside the synchronous captured Post, before native OnClick
closes its source, a concrete getter copies that exact window's immutable qid,
type, offered mask and BLAKE2 commitment over encoded text. It never looks up a
popup by qid. One closed private DTO is sufficient:

```text
{source:"native_goal_question_window_v1",question_id,type,buttons,text_hash}
```

The authority validates exact shape, qid, offered answer, native type/mask and
64-lowercase-hex commitment, together with the existing authenticated worker,
owner connection/company, screenshot, frame, world/epoch and request fences.
It does not recreate the visible popup from the text inbox or compare replica
issuance counters with authority. No stack mirror, persistent local popup
registry, native wire origin field or new public selector is required. Client-
private visual prompts work because only their actual recipient has the native
window. Explicit private owner context is supplied by the bound renderer and
validated `prepared.owner`, never a public client parameter or mutable global.

## Required native hooks and family seams

Hooks are conditional on integration build and initialization-safe presentation
tracking. Successful `CmdGoalQuestion` Execute records before native local-player
filtering, so questions issued before the first bridge Tick are retained.
`CmdGoalQuestionAnswer` Execute retires one issuance before native client early
returns. World initialization clears only rows, retaining the monotonic counter.
The hooks neither emit commands nor bypass native GameScript event production.

Family seams remain DispatchStoryAction, QueryStoryState, CaptureStoryTargets,
StoryIntentState and ValidateStoryNativeTargets. StoryPage/StoryPageElement/Goal
watermarks fence saved-pool targets. Public question handles use authority issue
generations; native question witness validation bypasses this independent text
inbox generation domain. Root owns the one native/action registry, existing
operation target context and special-case witness verifier.

## Story page context and target freshness

Hash the entire current page context, not just the selected button: page title,
date, company, and ordered element IDs/types/references/encoded text. Changing
an instruction above an unchanged Accept button invalidates an earlier action.
The public `page_version` also binds authority boot/page/element incarnations;
the cross-process digest omits those process-local fields.

Stream native values through the existing BLAKE2 pattern used for order
versions. Do not allocate a JSON copy of the page for proof or add a persistent
inverse index. Native page creation allows 128 elements, but loaded older pages
must not be silently truncated at 128: scan the native element pool to completion
for the page digest, bounded by its hard 64000-slot namespace, then paginate the
view. A temporary bounded list of matching element pointers can be avoided by
streaming in deterministic pool-index order with `sort_value` included. Listing
returns sort values so clients can retain normal display order. Discovery can
keep its 4096-slot per-query cursor; computing a required full-page digest is
a separately explicit native-pool work bound.

Page/element deletion and reallocation are fenced by handles and authority
watermarks. Story element update is fenced by full-page version. Vehicle
selection uses its live incarnation, primary/type checks and source-frame
watermark, while avoiding irrelevant moving-position invalidation. Root must
special-case its normal generic owned-vehicle mutation check and whole-consist
walk for this read-only native StoryPageButton reference.

Visual integration only opens normal story/goal windows, permits their actual
navigation/button/map/vehicle gestures, and captures the resulting normal
commands. Add the narrow GoalQuestion/StoryBook window/widget/toolbar allowlist
entries and existing source-frame provenance. No structured question/page
payload, inferred button target, semantic overlay, or native ID is exposed to a
visual caller. Location and goal links are ordinary camera/report effects;
they need no new simulation action. Tile/vehicle arming is local UI state; the
typed API directly supplies the eventual semantic selection in one command.

## Review decisions and alternatives

Independent preimplementation review approved replacing the initial overlap
quarantine with an independent company issuance inbox and exact actual-popup
visual witness. This supports ordinary same-qid duplicates without adding a
native protocol identity or claiming semantic inbox order equals client z-order.
Native-origin generic answers and deity closes consume one newest virtual row;
selected text answers consume only their exact retained generation.

Rejected broader alternatives: scraping windows on the headless authority;
direct Game::NewEvent bypassing normal commands/deduplication; public arbitrary
qid answers; mirroring native movable popup stacks; a new wire identity or
presentation event service; a second saved story-page registry; and a shadow
fleet selector for ordinary story selection.

Focused validation covers duplicate-ID exact handles and paired pagination,
older selected issuance through the real queue, absent selected issuance being
a no-op, exact live clicked duplicate popup/offered widget, source-preserving
button-drag rejection, client-private visual prompts without text disclosure,
world reset freshness, other-company book reads with page-specific interaction,
changed instructions/referenced Goals, any-owner typed primary vehicle selection,
long UTF-8 chunks and pages exceeding 128 loaded elements. Real rendered-widget
smoke and final matching-build results remain separately identified evidence.
