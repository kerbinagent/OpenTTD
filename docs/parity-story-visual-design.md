# Native story and question gestures: design for review

This design received independent preimplementation review and explicit root
implementation authorization. The text family and its native command proofs
already exist. This slice admits the ordinary GUI paths that produce those
commands. Public request/response shapes remain
unchanged: existing screenshot-bound `visual.ui` mouse gestures and normal
images only. No element IDs, question IDs, page text, callback names or semantic
selectors are returned to visual actors. No new `open_window` enum is needed;
the native toolbar already supplies book/company selection.

## Exact native routes

| Native input | Result | Capture boundary |
| --- | --- | --- |
| Toolbar `WID_TN_STORY`, company/global row | Open normal StoryBook | Existing exact live toolbar-dropdown provenance; UI only |
| Toolbar `WID_TN_GOAL`, company/global row | Open normal GoalList | Same; UI only |
| Story page selector/previous/next/scrollbar | Read another page | Normal StoryBook and its page dropdown; UI only |
| Story Location/Goal element | Camera/extra viewport or GoalList | Native handlers; UI only |
| Story push element | `StoryPageButton` | Synchronous Post within the current mouse gesture |
| Story tile/vehicle element, then map/vehicle click | Arm native placement, then `StoryPageButton` | The later native OnPlaceObject/OnVehicleSelect Post is synchronous within that later gesture |
| GoalQuestion offered button | `GoalQuestionAnswer` | Synchronous Post within the current mouse gesture |

`StoryPageButton` and `GoalQuestionAnswer` use the ordinary **null command
callback**. The deferred map selection is a native window event callback, not a
network command completion callback. All posts run inside RendererApplyInput's
existing HandleMouseEvents/capture lifetime. Do not extend
RendererBeginInputCallback's finite CreateGroup/AddVehicleToGroup rename-dialog
allowlist, the native callback table, owner-replay behavior or generic query /
confirmation permissions. No general callback framework is needed.

## Fixed UI admission

Add only toolbar widgets `WID_TN_STORY` and `WID_TN_GOAL`, and interactive native
window classes StoryBook, GoalList and GoalQuestion. Target filtering explicitly
admits the following known fixed widgets, plus existing `INVALID_WIDGET` frame
controls (close/move/resize) where native window layouts use them:

* StoryBook: WID_SB_CAPTION, WID_SB_SEL_PAGE, WID_SB_PAGE_PANEL,
  WID_SB_SCROLLBAR, WID_SB_PREV_PAGE, WID_SB_NEXT_PAGE.
* GoalList: WID_GOAL_CAPTION, WID_GOAL_SELECT_BUTTONS,
  WID_GOAL_GLOBAL_BUTTON, WID_GOAL_COMPANY_BUTTON, WID_GOAL_LIST,
  WID_GOAL_SCROLLBAR.
* GoalQuestion: WID_GQ_CAPTION, WID_GQ_QUESTION and
  WID_GQ_BUTTON_1/2/3. Native layout picks only actually displayed widgets;
  disabled/hidden target checks remain. The selected enum still must be an
  offered button of that exact native popup witness, so no implicit Cancel is added.

Story panel contents are selected by the normal native layout hit test, never
by a renderer-supplied element index. Reading another company's book and using
its camera/Goal links is allowed. Script interaction remains actor-owned/global
through the existing family proof; opening an unrelated book does not change
the authenticated company. Toolbar company selection here opens a book/report,
and does not perform membership moves.

GoalQuestion windows originate only from native delivery. Their text stays on
that rendering client's native UI; no public API creates a popup by ID. Authority
proof receives authenticated prepared.owner. Native question answers use the
actual clicked popup witness described below, independently of the text-company
inbox and replica issuance counters.

## The one additional provenance need: two-step story selection

A class/widget allowlist alone admits the ordinary routes, but has a stale
instruction gap: a player sees and arms a tile/vehicle button, then the script
changes instructions above that same button before the later target click. The
later capture would hash only the new page state, which the player may not have
seen when the tool was armed. The proposed extra state is one small local lease
for the currently armed story tool, analogous to existing exact dialog origins.
There is only one `_thd` placement tool, so no map of tools or callback registry.

```cpp
struct StoryPlacementOrigin {
    InputOrigin source;             // exact live StoryBook, PAGE_PANEL
    StoryPageElementID element;
    std::string page_version;        // same local full-page version as text
};
```

Two narrow private inspection seams avoid extracting or copying story UI state:

1. A guarded integration-only helper implemented next to StoryBookWindow returns
   its active placement element iff this exact live window is `_thd`'s callback,
   that element is tile/vehicle type, and it belongs to the window's selected
   page. Declare it in renderer_input.h under Integration, rather than adding a
   generic Window virtual API. A tiny integration-only const getter on the
   concrete class is sufficient; no native behavior or wire format changes.
2. A story-family helper `StoryPlacementVersion(StoryPageElementID)` checks the
   live own/global tile/vehicle element and returns the existing local full-page
   version. That version already binds boot, page/element/referenced Goal
   incarnations and script-managed visible context. Reuse PageVersion; do not
   create another page registry, content hash or public query.

After a successful approved StoryBook PAGE_PANEL gesture leaves a story
placement armed, retain its exact origin, active element and version. A native
push does not create a placement lease. Other-company tile/vehicle buttons cannot
establish one; reject that arming within the existing InputRelease cleanup scope
so ResetObjectToPlace cancels it without a script event.

Before dispatching any later gesture with a StoryBook placement callback, require
the exact live origin/window, same active element and equal local page version.
If invalid, cancel through normal ResetObjectToPlace, clear the local lease and
reject generically. Do this before InputIsIdle returns so a stale placement does
not leave every future gesture stuck behind an invalid callback. Accepted target
clicks still use native vehicle hit testing, `First()`/primary and requested-type
checks. ANY owner's primary vehicle is valid; no ownership/consist/order state
is added by this UI slice.

After a gesture, clear the lease when native placement ended or changed away
from StoryBook. A fresh approved StoryBook PAGE_PANEL click can replace it with
a different button; an unrelated map/list click cannot establish or refresh it.
Clear it from RendererInputWindowClosed and RendererResetInputProvenance, using
the existing lifecycle hooks. Preserve the current InputRelease cancellation
and one-command capture limits. Selection cancellation is a local UI effect;
it does not fabricate a GameScript answer or selection event.

The authority still compares the normal generation-free story context and
source-frame watermarks on the final captured command. The new local version
only bridges the earlier arming gesture to that capture; it never compares
renderer-local generations to authority generations. Native vehicle motion does
not enter this page version and does not invalidate a valid armed selection.

## Scope, alternatives and verification

Likely ownership is renderer_input.{h,cpp}, a tiny integration-only inspection
addition in story_gui.cpp, and the version helper in actions_story.{h,cpp}.
Root assigned this worker the tiny native GUI inspection and ID0 fix. No backend change,
new public field, new command, CMake target or generic dialog admission.
Root's separately reviewed explicit native-owner propagation must be present
before private client-target question acceptance is claimed.

The simpler class-only change omits the cross-gesture instruction fence. It is
appropriate only if review decides the final screenshot alone is the intended
consent context even when instructions were hidden while selecting. The proposed
one-tool lease preserves the earlier visible context at a small, explicit cost.
A generalized callback/session registry or new GUI protocol is excessive for
two native selection handlers, and a new semantic visual selector violates the
screenshot-only contract.

Focused checks should cover book/company/global menus, page navigation and links;
own/global push and foreign-page interaction rejection; offered/private/unshown/
duplicate-ID question buttons; tile and any-owner type-correct primary selection;
instruction/Goal/incarnation changes between arm and target; page/window close
and reset cancellation; ordinary vehicle motion remaining valid; and an
unrelated dialog/menu still denied. Native null callbacks must remain sufficient.
Real-image smoke should demonstrate at least one push, one tile/vehicle event
and one question answer through actual normal widgets after central wiring.

## Implementation handoff

Implemented in renderer_input.{h,cpp}, story_gui.cpp and actions_story.{h,cpp}.
`RendererStoryPlacementElement(const Window*)` is the private concrete-window
inspection seam. `Detail::StoryPlacementVersion(StoryPageElementID)` reuses the
family's public local full-page version, with normal selected-page ownership.
No completion callback, public API or native command table changed.

Native SetSelectedPage now compares active_button_id with its actual Invalid
sentinel, so element 0's placement is canceled on page change. Invalid local
story callbacks are canceled before InputIsIdle; normal placement completion,
window close, provenance reset and failed input cleanup clear the lease.

All three changed runtime translation units compile with actual debug flags.
The isolated native regression binary passed seven cases with 232 assertions,
including the new local placement-version and detail text/UTF-8 cases. It links
the changed renderer_input/story_gui/story-family objects and actual common
helpers with existing native objects. Independent postimplementation correctness
review is recorded by root after source freeze. Real rendered-widget smoke
remains a separate integration check; compilation and local context tests do
not claim it.

## Duplicate-question witness refinement

A later independently reviewed slice adds one gesture-scoped `InputOrigin` for
the exact native question popup and offered button. Arm it before all native
mouse handlers. A button drag is rejected before dispatch, so it cannot locally
close a popup without a captured answer; ordinary caption drags remain available.
`RendererQuestionCommandWitness(id,button)` is available only during the captured
Post. The concrete getter receives that actual live Window pointer, verifies
class/qid/widget/Nth offered button, and copies immutable native type/buttons/
encoded text before OnClick closes it. It never selects a popup by qid.

The closed private state is `{source:"native_goal_question_window_v1",question_id,
type,buttons,text_hash}`. Authority validates it with the authenticated owner and
existing worker/frame/image/world/request fences, without reconstructing an inbox
or comparing local generations. Gesture RAII, exact window close and reset clear
the source. No popup stack registry or native command/callback extension is added.
The independent company text inbox has separate issuance handles and explicit
newest-first generic close behavior; it is not a native popup z-order mirror.

The new `test_integration_question_window.cpp` creates actual duplicate native
windows with different immutable bodies and button sets, checks exact-pointer
witnesses and offered-widget mapping, and verifies that rejected button drag
preserves the source. Final combined test results and newer same-qid public-API
smoke remain separately recorded after the matching final build.
