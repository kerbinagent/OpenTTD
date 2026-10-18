# Native sign editor capture contract

Independent architecture review approved this slice before implementation.
It uses existing screenshot-bound `ui.apply` gestures and ordinary native
`RenameSign` / `MoveSign` commands. There is no public API, state DTO, command
callback, generic dialog grant or provenance registry addition.

## Exact native editor

`RendererSignEditorEditable(const Window*)` recognizes only a live concrete
`SignWindow` at `QueryString(Sign)`, in normal company play. It checks the exact
native `WID_QES_TEXT` buffer mapping, current SignID and the local incarnation
cached when the editor opens or native previous/next navigation changes its
sign. The live-window scan precedes pointer dereferencing and `dynamic_cast`.
`CompanyCanEditSign` remains the permission rule: own, neutral and other-company
signs are editable; protected GameScript signs are denied in normal company play.
Local generations are never exported or compared across replica processes.

Input admits only the native frame/caption, location, OK, cancel, delete, move
and previous/next controls of that exact editor. Text replacement targets only
`WID_QES_TEXT`, using the existing native Textbuf filter/length roundtrip and
`OnEditboxChanged`. Invalid or truncated text leaves the original buffer intact.
Mouse editbox/OSK activation and editor colour controls remain denied. Default
parent-bound query permissions and the separate FoundTown text profile stay
unchanged. Genuine editors opened by the already registered `CcPlaceSign`
callback need no additional callback grant.

## Separate save and move

`RendererSignEditorCanMove(const Window*)` additionally requires a nonempty live
sign name, an edit buffer exactly equal to that raw name, and no pending colour.
An empty native rename deletes a sign, so an unnamed sign must receive a saved
name before moving. Dirty text likewise requires an ordinary separate OK/rename
gesture. A remote name change also prevents moving an outdated buffer.

The renderer input fence applies only immediately before a left-button gesture
on an actual native map viewport while this exact editor is the placement
callback. Failure cancels placement and rejects the capture before dispatch.
It does not blanket-gate `InputIsIdle`: text, save/cancel, navigation and camera
controls remain available while a dirty editor exists.

`SignWindow::OnPlaceObject` repeats the predicate defensively before either Post.
The accepted renderer path skips the redundant rename and posts exactly one
ordinary `MoveSign`; authoritative sign incarnation, content and source-frame
proofs remain unchanged. The normal non-renderer path retains its original
combined rename-and-move behavior.

## Integration and verification

Native implementation: `src/signs_gui.cpp`. The header's two predicate
declarations are coordinated with the construction-input owner. The narrow
renderer-input integration patch is `/tmp/openttd-sign-input.patch`, for the root
owner to apply serially; this worker did not edit `renderer_input.cpp`.

Native source, proposed input TU and the five real-editor cases appended to
`test_integration_construction.cpp` compile with the actual debug flags. Native
source also passes syntax checking without `WITH_INTEGRATION`. Independent
postimplementation review approved the complete native/input/test package. Tests
cover shared permissions/protected denial, a retained editor across same-ID pool
replacement, exact text roundtrip, dirty/empty/colour movement cancellation,
rename-first movement, and the original human combined operation. The ordinary
null video driver supplies the native editbox focus sink in the unit fixture.
Shared builds, actual test execution and public screenshot/capture acceptance
remain root integration checks; compilation is not a runtime acceptance claim.
