# Editor command replay

The editor stores user intent as a strict, ordered command log. It does not store a painted
preview as the source of truth and it does not reinterpret the old free-form
`operation_type/selection/parameters` records. Replaying the same baseline, active plane, physical
dimensions, palette, config fingerprint, and command sequence produces the same label bytes,
region graph, statistics, and fingerprints.

## Contract

`image23mf.contracts.editor` defines schema version 1 as a discriminated union:

| Command | Selection | Effect |
| --- | --- | --- |
| `canvas_selection` | Canonical source-space rectangle/lasso/brush/regions | Saves non-destructive authoring state outside the render fingerprint |
| `local_raster_edit` | Embedded canonical selection snapshot | Deterministic fill, color cleanup, physical morphology/thickening, clone, or affine resize/warp |
| `keep_regions` | One or more region IDs | Protects the selected source pixels from every later destructive command |
| `island_merge` | Region IDs plus island-classification fingerprint | Runs one explicit dominant, perceptual, or explicit-color merge policy |
| `hole_correction` | Feature IDs plus hole-classification fingerprint | Fills, collapses, or recolors a classified hole |
| `contour_cleanup` | Whole graph | Runs an ordered physical open/close/majority/boundary request |
| `thicken_region` | Exactly one region ID | Dilates the source by a bounded physical radius into explicit editable labels |

Every selector contains both the source region-graph fingerprint and the job-config fingerprint.
Island and hole selectors also contain the classification fingerprint. Region and feature IDs are
canonical, unique, and schema-validated. Unknown fields are rejected. The command ID is durable;
callers that do not already have an identity can use `derive_command_id()` to derive a stable
content-addressed ID. Each command also records an explicit authored timestamp, structured
provenance, and source (`manual`, `automatic`, or `model`). Each command and the complete ordered
sequence have canonical SHA-256 fingerprints.

The commands are intentionally sequential. A command is checked against the graph immediately
before it runs. If command 1 changes a region, command 2 must have been authored against command
1's output graph. Reordering or editing an earlier command makes a later selector stale and raises
`StaleSelectorError`; the engine never guesses which new region the user meant.

## Keep and bounded edits

`keep_regions` snapshots the selected region pixels into a protection mask. A later command may
read or border those pixels but may not alter their label or active state. A proposed overlap raises
`ProtectedPixelError` with the command ID and exact pixel count. Protection follows the selected
pixels even if neighboring regions are subsequently restructured.

`thicken_region` is the first bounded manual geometry operation. It uses the same anisotropic,
physical-unit kernel as contour cleanup. It can change only active pixels whose labels are included
in `editable_labels`; the source label itself is excluded. This supports requests such as widening
a thin line or making a small feature larger without a global morphology pass.

`local_raster_edit` embeds the complete canonical selection that the user accepted, rather than a
pointer to mutable UI state. Fill can explicitly activate transparent pixels; color cleanup replaces
only declared source labels; morphology uses anisotropic physical kernels and explicit editable or
replacement labels; clone uses a physical offset and an explicit overwrite allowlist; affine edits
use deterministic nearest-neighbour inverse mapping around the selected physical bounds. Clone and
affine destinations reject canvas overflow unless the command explicitly records clipping. Discrete
print labels do not blend through feather weights: feather remains preview guidance while the saved
binary selection is the mutation authority.

New local raster operations also carry strict regional-edit provenance. The selection hash, parent
revision, and versioned local engine persist through draft history and project bundles, and the
revision drawer labels them as deterministic replay. Provider-backed regional edits use the
companion contract in [regional-edit-provenance.md](regional-edit-provenance.md).

## Replay result

Use the HTTP- and storage-independent entry point:

```python
result = replay_editor_commands(
    baseline_labels,
    active=active_plane,
    colors=label_colors,
    width_mm=width_mm,
    height_mm=height_mm,
    config_fingerprint=config_sha256,
    commands=ordered_commands,
)
```

`ReplayResult` returns:

- final `LabelField`, active plane, and exact `RegionAnalysis`;
- aggregate changed and protected masks;
- an `EditorReplayRecord` with a canonical JSON representation and fingerprint;
- one step record per command in original order;
- before/after state and graph fingerprints, exact changed pixels and area, active/inactive label
  transitions, per-label area deltas, engine-record fingerprints, and region lineage;
- the final serialized region graph so callers can publish it directly and recompute island, hole,
  clearance, and risk artifacts against the edited output.

Active pixels remain exhaustive: every active cell has exactly one declared label and exactly one
region assignment. Inactive-to-active hole corrections are represented explicitly as transitions
from no label to a target label. Changed-mask counts and transition totals are cross-validated.

## Persistence and legacy behavior

The replay package deliberately does not import SQLite or processing services. For the current
generic draft-operation table, `command_to_storage_payload()` emits a lossless
`editor_command_v1` envelope. `load_persisted_commands()` validates the command, selector echo,
source, and optional fingerprint before replay.

Old free-form operations cannot be safely migrated because their selectors were not bound to a
source graph or config. They raise `LegacyOperationError` with their index and operation type. They
must be shown to the user as requiring re-selection; they are never silently dropped and never
converted through coordinate or label guesses. Direct schema-v1 typed payloads remain accepted,
which gives a future storage migration a lossless path away from the generic envelope.

## Processing integration

The preview worker includes the ordered command-sequence fingerprint in every derivation key, runs
replay immediately after the baseline label field is produced, and uses the returned label field
and active plane as the only input to downstream preview, region, clearance, island, hole, and risk
artifacts. It publishes the baseline quantized preview, processed preview, raw processed labels,
changed/protected masks, and canonical replay record with linked fingerprints. A replay failure is
returned as a user-correctable draft error; it never falls back to the baseline image or partially
publishes edited artifacts.
