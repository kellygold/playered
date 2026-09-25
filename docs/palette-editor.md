# Palette editor and durable history

Linear issue: `24K-38`

Palette Lab edits the ordered, named `PaletteColor` records in the versioned job configuration.
Every row has an explicit one-based display index, stable ID, name, normalized sRGB value, lock
state, and optional owned-filament reference. Drag-and-drop, accessible move buttons, and
`Alt` + arrow keys all change the same saved order.

## Editing and color sources

The editor supports direct swatch and hex replacement, names, lock toggles, Chromium's native
screen eyedropper, and the owned-filament chooser. Choosing physical filament applies its exact
catalog/library color and durable `filament_id`; replacing or sampling a different color clears a
stale physical association. The bundled Bambu starter colors can be imported idempotently from the
same screen without replacing custom records.

Automatic fitting operates on the canonical rendered crop, not the uncropped upload. The user
selects two through eight colors. Locked rows retain their exact indices and six-digit sRGB values;
only unlocked positions move through the deterministic CIELAB fitter. Reducing the count is blocked
when that would discard a locked row. The fitted result is still an ordinary saved palette, so
preview classification never depends on hidden centroid state.

## Autosave, reload, and undo

Each discrete palette action appends a `palette-edit` operation containing the action, selected
color (when applicable), and complete before/after palette snapshots. Draft autosave sends the
current job configuration and ordered operation log under the optimistic draft generation. Saves
are serialized in the browser; a stale local-only generation is refreshed and retried once. The
visible state is immediate while Saved/Saving/failed statuses report the durable state honestly.

Undo restores the latest saved `before_palette` and removes that operation from the ordered draft
log. It is therefore itself persisted rather than being an in-memory visual reversal. Starting a
preview preserves the operation log. The most recent project ID is stored in local browser storage,
while all project data and normalized source bytes remain in SQLite/content-addressed local files.
On reload the app reopens the project, source image, palette, operation history, and completed
preview from the API.

## Evidence

Engine/API tests prove crop-aware locked fitting, generation conflicts, operation-log survival,
preview preservation, and source-image retrieval. Component tests cover rename, lock, owned
filament, eyedropper, auto-fit, keyboard reorder, and undo. App integration tests exercise an
autosave/reload/durable-undo cycle. Real Chromium QA uses the native 2520×1680 Wager fixture at
desktop and 390 px mobile widths with no console/page errors or horizontal overflow.
