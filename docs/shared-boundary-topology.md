# Shared-boundary topology

Linear: `24K-60`

`image23mf.geometry.topology.build_shared_boundary_topology` converts the final exhaustive
`LabelField` into canonical Geometry IR contours, islands, and shared edges. It is the ownership
boundary between image processing/vector evidence and physical extrusion.

## One ownership source

Potrace runs on derived per-color masks and its ID-sorted `construction` paths are retained in the
resulting Geometry IR. Those paths are useful source evidence, but they never independently claim
physical area. Every printable boundary is polygonized from the single authoritative label field.
Consequently:

- every source pixel belongs to exactly one output component;
- gap and overlap area are exactly zero by construction;
- both colors on an interface receive coordinates from the same pixel-grid edge;
- source-raster Y is inverted into physical Y exactly once;
- smoothing or tolerance differences between Potrace masks cannot open a seam.

The builder verifies that `SourceLabel.processed_labels_sha256` matches the exact label bytes,
that present label indices have one source label and consistent material, and that Potrace evidence
is unique, canonical, and construction-only. Declared palette values may be absent; they do not
create fake labels or empty islands.

## Contours and junctions

Components use four-neighbor connectivity. This keeps diagonally touching checkerboard cells as
distinct printable islands. Pixel-cell perimeter edges are oriented with filled area on the left,
traced into closed loops, and simplified only across safe collinear vertices. Vertices touched by
three or more regions remain explicit so T- and cross-junctions cannot become endpoint-on-edge
ambiguities.

Positive loops become counter-clockwise exteriors; negative loops become clockwise holes parented
to their exterior. Physical area after six-decimal Geometry IR quantization must agree with the
component's exact pixel area within a perimeter-scaled quantization bound. Path and document model
validation then rejects repeated vertices, open paths, self-intersections, invalid hole nesting,
filled-region overlap, missing shared declarations, and noncanonical ownership.

Shared edges are derived directly from unlike neighboring cells, grouped by island pair and axis,
and merged into maximal straight runs. Each edge stores sorted adjacent island IDs and is owned by
the lexicographically first island. The final `GeometryDocument` independently re-derives all shared
overlaps, so missing, duplicated, or geometrically inconsistent seam evidence fails closed.

## Determinism and evidence

The result includes per-component pixel/area/provenance evidence, exact full-canvas coverage,
source vector path IDs, the label-field SHA-256, and a topology artifact SHA-256. Components are
discovered in source row-major order; all persisted Geometry IR collections use content-addressed
ID order. Repeated inputs therefore produce identical documents and result fingerprints.

## Verification

```shell
.venv/bin/pytest -q tests/test_shared_boundary_topology.py
```

The suite covers uniform and complete canvas coverage, checkerboards, nested rings and holes,
T-junctions, edge contacts, source-Y inversion, hostile self-intersecting construction evidence,
absent palette values, provenance mismatch, duplicate evidence, and coordinate-collapse limits.
