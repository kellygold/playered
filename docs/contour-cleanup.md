# Physical contour cleanup

`image23mf.engine.contours` provides deterministic raster operations for Printability Lab controls
and typed manual commands. The preview pipeline invokes only `boundary_simplify`, and only when the
saved automatic smoothing radius is greater than zero. Open, close, and majority remain explicit
manual commands. Every invocation preserves the serialized request and exact before/after evidence;
automatic preview use is additionally wrapped by the canonical automatic-cleanup record and mask.

## Contract

Every radius is measured in millimetres. The engine builds an elliptical cell-centre kernel using
the label field's independent X/Y physical scale, so non-square pixels do not silently become a
square-pixel approximation. A radius smaller than both pixel dimensions honestly resolves to the
centre pixel and therefore makes no change.

The operations are:

- **Open** — names one subject label and one explicit replacement label. The subject's binary mask
  is eroded then dilated inside the active domain; only removed subject pixels become the declared
  replacement. This is an exact, idempotent opening of the subject mask.
- **Close** — names one subject label and the source labels it may overwrite. The subject's binary
  mask is dilated then eroded; only newly closed pixels whose current labels are explicitly editable
  become the subject. It is an exact, idempotent closing when every non-subject label is editable.
- **Majority** — changes only pixels whose current label is explicitly editable. The winning label,
  physical radius, majority ratio, and pass count are all part of the request. Equality with the
  requested ratio is accepted; ties retain the current label.
- **Boundary simplify** — performs an open-then-close proposal for all labels, but accepts it only
  on existing four-connected color boundaries, for editable source labels, and where the proposed
  label satisfies the explicit physical-neighborhood majority ratio. Every pass is limited to the
  boundary that exists at the start of that pass.

The active/transparency plane is immutable. Inactive label bytes are also retained exactly. Every
active output pixel still has one declared label; the engine never creates an unlabeled raster cell.
There is no implicit operation, default dilation, inferred target color, or hidden calibration value.

## Inspectability

`AppliedContourCleanup` returns:

- the resulting `LabelField` and stable post-operation `RegionAnalysis`;
- the unchanged active plane;
- an exact one-byte-per-pixel changed mask for preview overlays;
- ordered per-step statistics with request parameters, label fingerprints, transition counts,
  physical changed area, per-label area deltas, region counts, and total measured perimeter;
- exact net statistics across the full ordered request; and
- overlap-derived region lineage from the original field to the final field.

`operation_changed_pixel_count` is the sum of work performed by every step. The separate
`changed_pixel_count` is the exact net difference between the original and final label fields, so a
later step that reverses an earlier change remains visible rather than corrupting the final count.

## Safety and limits

Requests are validated atomically before any output is produced. Referenced labels must exist in the
declared palette, label lists must be sorted and unique, and parameters that do not apply to an
operation are rejected instead of ignored. The ordered request is fingerprinted and reconstructable
from canonical JSON.

Physical kernels are capped at 4,096 raster offsets. An oversized radius/resolution combination
fails with an actionable error instead of allocating unbounded work. The Wager production fixture,
resampled to the application's 1024-pixel preview envelope and measured at 200 mm wide, completes a
0.4 mm opening plus full region remeasurement and lineage in under one second on the development
machine. Calibration-driven 0.2/0.4 mm defaults are supplied by the completed 24K-53 profile
catalog and remain overrideable with recorded provenance.
