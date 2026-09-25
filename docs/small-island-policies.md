# Small-island classification and merge policies

Linear issue: `24K-44`

Every current preview publishes a hash-verified `island-analysis` JSON artifact bound to the exact
post-cleanup `region-graph` fingerprint. Before typed manual replay, the preview also classifies the
quantized field for the saved automatic policy. `review` and `keep` remain byte-identical;
`dominant_neighbor` and `perceptual_neighbor` merge eligible non-border candidates. The canonical
`automatic-cleanup` artifact records before/after graph fingerprints, decisions, changed-pixel
count, and the exact changed-mask hash. A later explicit editor action can still apply any policy to
a selected region through typed replay.

## Physical classification

A connected region becomes a candidate when its physical area is below the configured area
threshold, its area-equivalent circular diameter is below the configured diameter threshold, or
both. Equality is kept: only values strictly below a threshold trigger a candidate. Both thresholds,
the physical pixel scale, and the classifier options are serialized so a saved result is exactly
reconstructable.

The conservative line safeguard exempts a candidate when its physical bounding-box aspect
ratio and longest dimension both meet explicit limits. This protects obvious intentional strokes
from area cleanup without claiming a medial-axis measurement. The dedicated clearance classifier
now supplies the more precise width/continuity evidence; see
[clearance-analysis.md](clearance-analysis.md).

Each candidate records stable region identity, palette label/color, triggers, area, equivalent
diameter, perimeter, compactness, physical bounds, bounding-box dimensions, canvas-border contact,
and every touching neighbor with shared-boundary length and CIE76 distance. Transparent pixels are
never regions or merge targets.

## Explicit policies

- `review` records that the user must decide and changes no pixels.
- `keep` accepts the candidate as-is and changes no pixels.
- `dominant_neighbor` chooses the eligible label with the greatest total shared boundary. Equal
  totals choose the lowest palette label.
- `perceptual_neighbor` chooses the eligible touching label with the lowest CIE76 distance, then the
  greatest shared boundary, then the lowest label.
- `explicit_color` assigns the selected declared palette label, including a non-touching color when
  that is the user's deliberate choice.

Bulk operations are simultaneous. Selected islands are excluded as targets for one another, which
prevents order-dependent swaps and cascading merges. A candidate with no eligible touching target is
reported unchanged. Requests are validated completely before mutation, so an invalid region,
palette target, or long-line exemption makes the whole operation fail atomically.

## Exactness and persistence

The public artifact does not duplicate full risk-warning payloads. The service reconstructs and
validates classification from the graph and settings, then materializes canonical `small_island`
warnings only while composing the risk report. This reduced the 1024×683 Wager island artifact from
about 4.7 MB to about 1.3 MB while retaining all candidate evidence. On that fixture, 1,653
candidates were classified in about 0.46 seconds on the recorded development machine; exact timing
is evidence, not a cross-machine service-level guarantee.

Applying a policy preserves inactive/transparent pixels and the exhaustive declared label domain.
The returned assignment and graph are cross-checked, and lineage proves every split, merge,
modification, creation, or deletion caused by the operation.

## Verification

Tests cover area-only and diameter-only thresholds, exact physical measurements, long-thin
exemption, border islands, transparent isolation, deterministic dominant-boundary ties,
perceptual selection, explicit colors, review/keep behavior, no-target handling, adjacent selected
dust, atomic invalid requests, lineage, source immutability, canonical reconstruction, and the
committed 0.4 mm geometry fixture. The API flow proves island-analysis publication, graph binding,
verified artifact download, small-island risk coverage, legacy nullability, supersession safety,
and process-restart recovery.
