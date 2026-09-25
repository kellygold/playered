# Image Lab

Image Lab is the first production workspace in Image23MF Studio. It imports a bounded raster into
a persisted project, exposes the shared crop/canvas transform, and renders a server-generated
preview from the exact draft configuration that later stages consume.

The global header also exposes **Calibration**, a project-independent physical-evidence review
center. It can be opened from either the empty import view or an active image project and returns to
the prior studio state without discarding work. See
[printability-calibration.md](printability-calibration.md) for its restart-safe run and guarded
catalog-promotion workflow.

## Import entry points

- Choose a PNG, JPEG, or WebP from the file picker.
- Drop an image anywhere in the window.
- Paste an image from the clipboard while focus is outside a form control.

The client rejects files larger than 64 MB before upload. The API independently verifies the file
signature, decode limits, dimensions, frame count, color normalization, and transparency. Upload
progress represents bytes sent to the loopback API and can be canceled without creating a partial
project. A corrupt or unsupported image returns an actionable error without replacing the current
workspace.

Replacing a source always requires confirmation. A replacement creates a new project; the prior
project remains persisted in the local workspace.

## Crop and canvas controls

The source crop is stored as normalized coordinates and interpreted through the versioned shared
transform. Four fit modes are available:

- **Cover** fills the canvas and crops overflow.
- **Contain** preserves the crop and allows transparent margins.
- **Stretch** maps the crop directly to the canvas shape.
- **Extend** preserves source scale on a transparent canvas.

The crop frame can be moved or resized with a pointer. When the crop frame has focus, arrow keys
move it, Shift plus an arrow uses a larger step, and Option/Alt plus an arrow resizes it. Numeric
percent fields provide a precise fallback. Canvas width and height are measured in millimetres and
can be ratio-locked.

Every committed crop, fit, canvas, palette, cleanup, or editor-operation change is persisted through
one ordered, generation-guarded autosave path. Pointer/slider previews remain provisional until
commit. The header distinguishes pending, saved, and failed state; the last failed exact snapshot
can be retried without rebuilding it from newer UI state.

## Preview lifecycle

Import immediately starts a persisted preview job. Later edits mark the visible server preview as
stale until **Render preview** is selected. New generations supersede older preview jobs, and stale
results never replace a newer draft. Render work can be canceled without discarding the project or
its last successful preview. Refreshing or restarting the API can recover the latest persisted
preview head through the project workspace contract.

A restored preview is Current only when both its configuration and typed editor sequence
fingerprints match the active draft. The exact canvas exposes Original, Quantized, Processed, Risk,
and Mask pixels without interpolation, plus pan/zoom/fit, pixel and millimetre inspection, split
reveal, and reduced-motion-aware flicker. Geometry and Slicer views remain visibly unavailable until
M2 publishes aligned proof rasters.

## Accessibility and recovery

Import, fit-mode selection, numeric editing, preview rendering, cancellation, and replacement are
keyboard operable. Status and error regions use live semantics, crop handles have explicit labels,
focus indicators remain visible, and reduced-motion preferences disable nonessential animation.
Import and replacement dialogs preserve the current project on cancellation or failure.

Automated frontend coverage exercises picker, drop, paste, all fit modes, numeric and keyboard crop
edits, replacement confirmation, upload progress/cancellation, preview cancellation, corrupt input,
and the oversize guard. Python integration tests separately cover the persisted import and preview
contracts.
