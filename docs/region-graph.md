# Connected-region graph and lineage

Linear issue: `24K-43`

Preview derivation v3 introduced a versioned `region-graph` JSON artifact derived from the exact
palette-label bytes and alpha plane used by the palette preview. The API verifies the stored hash
before parsing the graph and returns it as `region_graph`; older preview results remain readable
with a null graph.

## Identity and topology

A region is one four-connected set of active pixels carrying one label. Transparent pixels are not
regions and never join two visible components. Each ID is a content address of the region's label
and exact global-coordinate scanline runs. It therefore has two useful properties:

- repeated analysis of the same label field produces byte-identical graphs; and
- adding, deleting, or changing an unrelated region cannot renumber an unchanged region.

IDs describe exact raster identity, not semantic objects. Moving, recoloring, splitting, merging, or
changing any pixel in a region intentionally creates a new ID. The lineage API relates those IDs
through their exact overlap instead of pretending the region stayed byte-identical.

## Measurements

Each node records:

- label and normalized uppercase color;
- exact pixel count and physical area;
- pixel-edge count, physical perimeter, and `4πA/P²` compactness;
- pixel and millimetre bounding boxes;
- per-side canvas-border contact and total contact length;
- sorted neighboring region IDs;
- minimum, median, 95th-percentile, and maximum orthogonal run-span estimates.

Each graph edge records both the number of shared pixel edges and their physical boundary length.
Horizontal and vertical boundaries use their correct, potentially different, physical pixel scales.
Edges against transparent space contribute to perimeter but not adjacency; only contact with the
outer canvas contributes to border-contact fields.

The width values explicitly name their method `orthogonal-run-spans-v1`. They are deterministic
first-pass estimates, not a medial-axis clearance claim. The neck/line classifier can add a more
specialized distance-transform measurement without changing or reinterpreting this contract.

## Lineage

`derive_region_lineage` compares two analyses with the same pixel and physical dimensions. It emits
exact overlap pixels, area, before/after shares, and deterministic events:

- `unchanged` for the same stable ID;
- `modified` for a one-to-one replacement;
- `split`, `merged`, or `restructured` for connected overlap groups; and
- `created` or `deleted` when no active overlap exists.

The assignment raster used for lineage is a read-only `int32` array retained only by the in-process
analysis object. It is deliberately absent from the JSON graph: region runs already define public
geometry, while avoiding a second large serialized raster.

## Algorithm and verification

The engine scans equal-label row runs, joins only overlapping runs in adjacent rows, and uses
deterministic union/find roots. It then measures boundaries with vectorized raster comparisons and
collects vertical runs for width estimates. Memory scales with the raster plus run/component count,
not one Python object per pixel.

Analytic tests cover a square-with-hole background, anisotropic pixels, exact perimeter and shared
boundary lengths, border contact, transparent and fully transparent fields, stable IDs after an
unrelated edit, and every lineage event. Generative tests prove that every active pixel belongs to
exactly one unique region and that repeat analysis is identical. The API user flow proves graph
publication, hash-verified download, exact pixel closure, and persistence across process restart.
