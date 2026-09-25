# Exact editor preview workflow

The M1 editor treats the visible processed raster as a verifiable contract, not a decorative image.
Every source is selected by artifact kind and shares one pixel/physical coordinate space:

- Original uses `preview-image`.
- Quantized uses `quantized-preview-image`.
- Processed and Risk use `palette-preview-image`; Risk adds measured report overlays.
- Mask uses the SHA-verified automatic-cleanup mask preview.
- Geometry and Slicer remain unavailable until M2 publishes aligned proof rasters.

Images render with nearest-neighbour sampling. Pan, zoom, fit, pixel grid, pixel/mm picking, split,
and flicker never resample the server artifact. The canvas adapts to its own inline size so nested
rails cannot cause controls to overlap at tablet widths. Reduced-motion preference pauses animated
comparison by default.

Canvas-to-region selection is enabled only after the client downloads and SHA-verifies the current
preview's exact `region-assignment` plane, checks its graph fingerprint and dimensions, validates
all assignment indices, and reconciles every region's pixel count. A click then performs an O(1)
pixel lookup; bounding boxes are used only to draw the selected-region highlight. When assignment
proof is loading, invalid, or absent from a legacy preview, the UI says so and preserves complete
keyboard/list inspection without guessing.

The inspector and canvas share one selection state. Region rows expose stable ID and color,
edge/interior topology, physical area and perimeter, compactness, width distribution, neighbors and
shared boundaries, applied rules/classifiers, measured warning reasons, and suggested operations.
Risk overlays, the accessible warnings list, and region selection stay synchronized; regionless
warnings remain native keyboard-focusable controls.

Rectangle, lasso, physical-radius brush, and exact-region selectors are stored in normalized,
post-EXIF source pixel-edge coordinates. Add/subtract composition, physical expansion/erosion, and
feathering are rasterized deterministically against the current working transform. Each committed
selection is a `canvas_selection_v1` revision operation, so reload and undo/redo restore the authored
state without changing the render-operation fingerprint. Selection-only history therefore preserves
fresh preview and output evidence; any restored config or render-operation mismatch still marks both
artifacts stale. The region list remains the non-pointer alternative for exact region selection.

## Automatic cleanup order

The preview worker runs one deterministic sequence:

1. Quantize the cropped image into an exhaustive label field and immutable active plane.
2. Classify small islands in physical units.
3. Apply the saved automatic island policy to eligible, non-border risk candidates. Review and keep
   change nothing; dominant/perceptual policies use the explicit engine tie-break rules.
4. Apply physical boundary simplification only when smoothing is non-zero.
5. Replay typed manual commands against that exact result.
6. Generate processed pixels, region graph, clearance/island/hole analysis, risks, and metrics from
   the final label field.

Hole thresholds remain diagnostic until a manual fill/correction command is selected. Automatic
cleanup never silently fills holes. Every preview publishes the canonical cleanup record, raw
one-byte-per-pixel changed mask, renderable mask PNG, before/after graph and label fingerprints, and
changed-pixel count.

## Jobs and freshness

The preview coordinator assigns a monotonically increasing local request sequence. Starting a new
request aborts the previous transport, cancels any known server job, and rejects late start, poll,
or result responses. The previous successful image remains visible as stale while replacement work
runs. Stage/progress, cancellation, structured failures, retry, and restored jobs share one state
machine.

A preview is Current only when `preview-statistics.config_sha256` matches the draft configuration
and the `editor-replay` sequence fingerprint matches the draft editor-sequence SHA. Legacy or
missing evidence is conservatively stale.

## Manual region operations

An exact current preview can author graph-bound protect, merge, delete, recolor, thicken, and
classified-hole fill commands. Nothing mutates during selection: the editor first shows the stated
scope predicate, exact affected count/list, and a multi-region canvas overlay, then requires an
explicit confirmation. Merge additionally requires every source component to touch the target
label; delete changes the active plane to transparency; recolor preserves activity; thicken changes
only declared replaceable labels; and protect prevents any later command from changing those exact
pixels.

“Apply to similar” always requires the same palette label and compares physical area, minimum width,
compactness, canvas-edge contact, and optionally neighboring labels. Tolerances use a
cross-language-safe one-thousandth grid. The command persists its anchors, predicate, exact resolved
IDs, and resolution fingerprint; replay recomputes the set against the graph at that exact command
position and rejects drift. A single command is bounded to 256 regions/features, and the UI requires
the user to narrow a larger match before it can be saved.

Once a command is saved, the old graph is visibly stale and every authoring control locks until a
replacement preview finishes. This prevents a second command from being attached to region IDs that
the first command may split, merge, or delete. Suggestions only preselect semantically identical
commands; specialized ring/hole actions are left explicit rather than aliased to a different edit.

## Autosave and recovery

Committed configuration and operation changes enter one serialized save chain. Config-only edits
debounce; palette, cleanup-policy, and operation commits serialize immediately. A stale generation
refreshes once and retries against the newest server generation. Versioned snapshots prevent an
older completion or failure from overwriting newer state.

Pending, saved, and failed states are explicit. Retry resubmits the exact latest failed snapshot.
Preview submission flushes pending autosave first, and page lifecycle transitions flush a committed
debounce. Reload restores crop, canvas, palette, cleanup, operations, and only an exactly matching
preview.

## Revision publication, reopen, and branching

The Revisions drawer keeps mutable draft state separate from immutable checkpoints. It always shows
the current autosave state and whether preview evidence is current, stale, or missing. Publishing is
blocked while a save is pending or failed and until the user supplies a trimmed revision name. An
exact current preview is attached automatically by job ID. Publishing a stale or unrendered draft
requires a second explicit confirmation and records that the revision has no current preview
artifacts; the client never silently drops visual evidence.

Publication returns a continuation draft based on the new revision, so the editor can keep working
without a destructive mode switch. The compact newest-first list distinguishes the active published
revision, the working draft's base, and older revisions. Opening a row fetches the exact full resource
and presents read-only config, operation count, artifact evidence, parent, notes, and identity; no
control edits the immutable snapshot.

Branching an older row requires confirmation that the current mutable draft will be replaced. The
returned draft reproduces that revision's exact config and ordered operations while the project's
active published revision remains unchanged. Publication or branch generation conflicts first
resynchronize the project. A retry recomputes preview freshness and cannot silently reuse an old job;
if no exact preview remains, the no-artifact confirmation is required again.

Opening the drawer locks background-page scrolling and restores the previous body overflow state on
close. The drawer itself scrolls independently. Desktop editor rails remain independently scrollable;
at compact stacked widths the document remains vertically scrollable. Source replacement creates a
new project with an isolated empty revision list while retaining the original project and its
revision history for explicit reopen.

## Configuration changes after manual edits

Manual raster operations are exact selectors bound to the full normalized configuration and cannot
be silently reused after crop, canvas, cleanup, palette, printer, or geometry changes. When any
non-palette operation exists, provisional slider events do not mutate editor state. The final
committed change opens an accessible confirmation that names how many manual edits will be cleared
and how many compatible palette-history steps will remain. Cancel changes nothing. Confirm sends the
requested configuration and filtered operations in one generation-guarded draft snapshot; there is
no intermediate mismatched state. A server-side `incompatible_editor_history` response is the final
atomic backstop for stale or bypassing clients.

## Verification

`make check` covers deterministic engine/API behavior, coordinator races, StrictMode lifecycle,
canvas source selection and transforms, autosave ordering/failure/reload, accessibility semantics,
exact assignment verification and inspector synchronization, and production builds.
`make qa-selection` uses real Chromium pointer and keyboard-accessible controls to prove rectangle
and exact-region composition, revision persistence, reload, undo/redo, derivation freshness, overlay
alignment, and 44-pixel minimum touch targets.
`make qa-local-edits` draws a real source-space rectangle, confirms a typed local fill, proves the
saved edit embeds the accepted selection snapshot, verifies that destructive persistence makes the
old preview stale, renders the new preview, and checks exact changed-pixel replay evidence plus the
live control layout and browser console.
`scripts/live_editor_qa.mjs` adds a real Chromium/API flow: import, exact canvas, split comparison,
crop/canvas/cleanup autosave, non-zero automatic cleanup, replacement-preview artifact inspection,
two exact manual operations, typed-command autosave/replay evidence, two named fresh-artifact
publications, branching the older revision while preserving the newer active publication, drawer and
nested-confirmation focus containment, restart/reopen, revision-list isolation across source
replacement, original-project recovery, independent desktop rail scrolling, and compact-page and
short-viewport dialog scrolling. It also proves selector safety end to end: cancel causes zero draft
writes, confirmation causes exactly one atomic write, palette history survives, incompatible manual
operations are removed, and reload returns a healthy workspace with no alerts.
Set `IMAGE23MF_QA_REPLACEMENT_ONLY=1` (or run `make qa-replacement-scroll`) for the independent
source-replacement gate. It verifies modal scroll locking and cleanup for both cancel and confirm,
then sends real wheel input to both editor rails and the compact document after replacement.
