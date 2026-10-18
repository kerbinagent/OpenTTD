# Story and question family integration handoff

The family lives in `src/integration/actions_story.{h,cpp}`. Central registrations,
native Execute hooks and authenticated owner propagation are integrated by the
root owner. Independent architecture review preceded both the original family
and the subsequent duplicate-question refinement.

Text questions form an independent company inbox. Each accepted company/global
issuance retains its own handle, including concurrent duplicate native IDs.
Visual answers instead attest the exact live clicked native question window;
they do not infer which popup was visible from the text inbox. Client-private
prompts remain solely in the actual recipient's native UI. No quarantine, popup
stack mirror or new native wire identity is used.

## Exact central registrations

All objects reject additional properties. Handles use exactly
`{id,generation,engine_instance_id}` with the existing unsigned generation and
boot string schema. Story page/element IDs are 0..63999; question IDs 0..65535.
`page_version` is exactly 69 bytes: `spv1:` followed by 64 upper-case hex digits.
The native family validates content and role in addition to these public shapes.

| Action | Required fields | Optional | Native commands |
| --- | --- | --- | --- |
| `goal.question.answer` | `question_id:handle`, `button:enum` | none | `GoalQuestionAnswer` |
| `story.button.press` | `element_id:handle`, `page_version:string[69]` | none | `StoryPageButton` |
| `story.button.select_tile` | previous fields, `tile:tile` | none | `StoryPageButton` |
| `story.button.select_vehicle` | previous base fields, `vehicle_id:handle` | none | `StoryPageButton` |

The 18 `button` values, in native enum order, are `cancel`, `ok`, `no`, `yes`,
`decline`, `accept`, `ignore`, `retry`, `previous`, `next`, `stop`, `start`, `go`,
`continue`, `restart`, `postpone`, `surrender`, `close`. Only actually offered
buttons are accepted, and zero-button messages cannot be answered.

| Query | Required | Optional |
| --- | --- | --- |
| `goal.questions` | none | `after_id:int[-1,65535]`, `after_generation:uint64`, `limit:int[1,32]` |
| `goal.question` | `question_id:handle` | `text_offset:int[0,INT32_MAX]`, `text_limit:int[1,8192]` |
| `story.pages` | none | `book_company_id:int[0,14] or null`, `after_id:int[-1,UINT32_MAX]`, `limit:int[1,32]` |
| `story.elements` | `page_id:handle` | `after_id`, `limit`, `text_offset`, `text_limit` with the above bounds |
| `story.element` | `element_id:handle` | `text_offset`, `text_limit` |
| `story.vehicle_candidates` | `element_id:handle`, `page_version:string[69]` | `after_id`, `limit` |

`goal.questions` pages in `(native ID, issuance generation)` order and returns
`next_after_id` plus `next_after_generation`, both null at the end. Pass both
continuation values to preserve duplicate-ID rows. Omitting `after_generation`
keeps legacy `after_id` behavior and skips every issuance of that ID; an initial
`after_id:-1` accepts only generation zero when supplied. Text queries return
company/global prompts for the current company incarnation. Native-client-only
prompts are never projected into this company inbox.

`story.pages` defaults to the actor's native book. Selecting another live company
is normal human read access. That company's pages are noninteractive; global
pages stay interactive even inside its book. Every selected book includes global
pages. `null` selects only global pages.

`story.elements` returns page metadata, `page_version`, paged element metadata,
and a nested `title_text:{text,text_offset,text_bytes,next_text_offset}` chunk.
The title chunk supports empty pages and titles longer than a response without
adding a separate detail query. `story.element` returns one element's complete
text chunk; a Goal element returns the referenced Goal's displayed text and
completion status. Text offsets must lie on UTF-8 boundaries. A byte limit too
small for the next code point is rejected, with no partial UTF-8 sequence.

All discovery examines at most 4096 pool/ID slots and returns at most 32 rows.
Previews are at most 512 UTF-8 bytes. Returned item JSON has a 32 KiB aggregate
budget with continuation before an item that does not fit; a whole observation
has the existing 48 KiB safety bound. Full-page hashing is a separate explicit
work bound: the native 64000-element pool, including loaded pages over the
current 128-element creation limit. No query consumes RNG.

## Central action and native wiring

Include `actions_story.h`; delegate to DispatchStoryAction, QueryStoryState and
CaptureStoryTargets in the same places as other families. Capture returns a
small comparable lease object for root to wrap as `story_family`.

The central generic `Parameters::Vehicle()` requires ownership and is incorrect
for a story vehicle selection. Exempt `story.button.select_vehicle` from that
generic reader/capture-consist walk, and let CaptureStoryTargets validate the
complete handle, primary status and requested type. ANY owner's primary train,
road vehicle, ship or aircraft is a normal selectable target. Do not capture its
orders/consist, location or physics; movement is irrelevant to this read-only
selection. Typed public handles and private W(F) still fence vehicle incarnation.

Private family functions are:

```cpp
std::optional<Json> StoryIntentState(Commands, const CommandDataBuffer &,
    ClientID authenticated_owner = ClientID::Invalid);
std::optional<ActionResult> ValidateStoryNativeTargets(Commands,
    const CommandDataBuffer &, const TargetWatermarks &,
    ClientID authenticated_owner = ClientID::Invalid);
```

`nullopt` means unhandled. `StoryIntentState` throws on malformed/unauthorized
handled commands. `ValidateStoryNativeTargets` returns an accepted/rejected
result for handled commands. Pass no owner for text; pass `_network_own_client_id`
from renderer capture and **validated** `prepared.owner` from the authority.
Never derive owner from a public parameter or a mutable global context.

For `GoalQuestionAnswer`, renderer capture obtains `story_state` from
`RendererQuestionCommandWitness(id,button)` while its exact native popup is
still live inside captured Post. The authority uses
`ValidateStoryQuestionWitness(data,state,prepared.owner)` directly instead of
reconstructing a state from the text inbox. The closed five-field witness is:
`{source:"native_goal_question_window_v1",question_id,type,buttons,text_hash}`.
`text_hash` is 64 lower-case hex BLAKE2 over the native encoded text. Exact
qid/offered button and the native enum/mask/hash shapes are validated. Existing
bound-worker, source-image, authenticated owner, world/epoch and request fences
supply provenance; renderer-local issuance counters are not compared.

Text `goal.question.answer` records its selected issuance generation in the
existing operation target capture. `ExecutingQuestionGeneration(company,id)`
recovers only that already executing text operation at native Execute. Retirement
removes precisely that issuance. If it was already closed, retirement is a no-op
and never consumes another duplicate. This adds no field to the native
`GoalQuestionAnswer(id,button)` command or emitted GameScript event.

Native commands here have no simulation footprint: StoryPageButton passes an
explicit tile to a script event, rather than modifying that tile, and push/
vehicle/question commands carry a dummy zero tile or no tile. Return an empty
NativeIntentTiles vector for these two commands. Do not treat dummy tile zero
as a map dependency, clear effect or broad mutation. Native descriptions must
not apply generic owned-vehicle/whole-consist traversal to StoryPageButton.
The family already validates command-specific tile/vehicle unused fields and
normal UI primary/type requirements.

For handled commands, call the family native target validator after the actor
company source-frame fence. Story buttons add their generation-free context as
`story_state`; question answers use the exact popup witness above. Avoid generic
vehicle/private state serialization. Preserve registry allowlisting, owner/current-company
validation and normal command completion/deduplication.

TargetWatermarks has root-added fields `story_page`, `story_element`, `goal`,
`question`. CompletedFrame must fill them respectively from
StoryPage::GetLastInstanceGeneration(), StoryPageElement::GetLastInstanceGeneration(),
Goal::GetLastInstanceGeneration(), LastStoryQuestionGeneration(). The family
checks every page element and referenced Goal against source W(F), and the
selected vehicle if any. Question public handles use authority issuance
generations; native visual witnesses intentionally bypass this text-inbox
generation domain. No renderer-local issuance counter is compared with authority.

Full-page digests include direct title/date/company/order and all element encoded
content; Goal elements include referenced Goal displayed text and completed
state. Public versions add boot and page/element/Goal incarnations. Private
comparison digests omit all process-local generations/boot IDs. Native dynamic
name/livery/physics substitutions and Goal progress (not displayed on this page)
do not enter the context digest.

## Native hooks and lifecycle

The current native hooks live in `goal.cpp` and the world initialization path.
`TracksPlayerPresentations()` is initialization-safe,
so successful issuance is captured before the first bridge Tick and before
native local-player filters. Only successful Execute calls record questions.

Company/global issuance appends an immutable body to each eligible company
incarnation's deque, with shared body storage across recipients. Client-private
issuance and renderer replicas retain no text-inbox bodies; their actual native
windows remain the presentation. A newly created replacement company never
inherits its predecessor's inbox. There is no TTL, LRU, duplicate count cap or
fixed response-sized retention limit. Queries remain bounded and paged.

An ordinary answer attributed to a selected text issuance retires exactly that
handle. An unattributed native answer pops one newest company-inbox issuance for
that ID. A deity CloseQuestion likewise pops one newest issuance per company,
matching the command's one-window operation without pretending that company
inbox order mirrors each client's movable popup z-order. Native client windows
continue their normal independent close and event behavior. The GameScript
answer event still contains only native ID, company and button.

World initialization/load clears transient rows, preserving the monotonically
increasing authority issuance counter until a real engine boot. Questions are
not saved or replayed; story pools are saved normally. Dead company handles fail
immediately. Bodies for dead company incarnations are pruned lazily on a later
same-ID issue/answer/close or complete reset; no global per-query sweep is added.

## Exact native popup gesture

`RendererApplyInput` arms one exact live question window and button widget before
any native mouse handler. The concrete `GoalQuestionWindow` getter validates
that supplied instance, qid and actual Nth offered enum, then copies its
immutable type/buttons/encoded-text witness inside captured Post, before native
OnClick closes the window. It never chooses a popup through qid lookup. A
button drag is rejected before input dispatch; normal caption drags remain
available. Gesture return, window close and provenance reset clear the origin.
No persistent popup registry, callback expansion or public semantic selector is
introduced.

## Verification

The initial unique-question/story implementation passed focused native tests and
a genuine isolated public-API rendered-widget smoke. The proof bundle is
`/tmp/openttd-story-ui-proof01/summary.json`: native toolbar company selection,
global story interaction in another company's book, company/private offered
answers, element-zero tile selection, page-change cancellation, referenced-Goal
staleness rejection and rearming, native road construction, exact retries and
stale-image rejection. That smoke used GAME SHA `bcf0b629...` and predates the
duplicate-question refinement; it is not evidence for the later code.

The three current duplicate-window cases passed in an isolated native link.
Their source is `src/tests/test_integration_question_window.cpp`; it creates actual concurrent
native windows with one qid, validates each exact immutable content/widget
witness independently, rejects informational answers, and verifies that rejected
button drag leaves its source live. Latest combined test and queued older-text-
issuance acceptance results must be recorded separately after the final build.
