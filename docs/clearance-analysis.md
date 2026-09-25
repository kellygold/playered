# Physical clearance, thin-line, neck, and gap analysis

Linear issue: `24K-45`

Every current preview job publishes a hash-verified `clearance-analysis` JSON artifact bound to the
exact `region-graph`. The classifier is non-destructive. It distinguishes elongated thin regions,
local constrictions between wider supports, and physically narrow gaps between separate same-color
regions; it never silently erases any of them.

## Distance and width evidence

Each region is analyzed in its own exact assignment crop. A separable squared Euclidean distance
transform uses the physical X/Y pixel spacing and an exterior pad, so anisotropic pixels retain
their real dimensions. Deterministic clearance ridges are the pixels that locally maximize this
distance across an opposing horizontal, vertical, or diagonal pair.

Widths on those ridges use exact horizontal and vertical same-region run spans in millimetres. The
smaller orthogonal span is a conservative raster-width estimate: a one-pixel stroke remains one
physical pixel wide instead of being doubled by a center-to-background convention. The serialized
settings name both methods, the independent line/neck/gap thresholds, their legacy shared-width
fallback, and the option fingerprint. Values equal to a threshold are
kept; a warning requires a value below the threshold by more than floating-point tolerance.

## Feature classes

- A `thin_line` is long and elongated, with its ridge's 75th-percentile and maximum width below the
  configured limits. Compact dust cannot satisfy the physical length/aspect tests. The valid
  actions are review, protect as intentional, or widen—not island deletion.
- A `narrow_neck` is a connected sub-threshold ridge portion touching at least two independently
  printable ridge components, each of which reaches the configured wider-support ratio. A thin
  branch attached to only one wide body is therefore not mislabeled as a neck.
- A `narrow_gap` is the exact axis-aligned cell-to-cell distance between separate regions carrying
  the same palette label. Small raster radii use bounded physical offsets; high-resolution/small-
  canvas cases switch to a deterministic multi-source Euclidean Voronoi boundary, avoiding
  radius-squared work. Corner contact and zero
  separation are not called gaps, and gap evidence must extend at least the configured physical line
  length so nearby dust does not flood the review list.

Every feature has a content-addressed ID, canonical region IDs/labels, pixel and physical bounds,
minimum/median/maximum width, physical length, evidence sample count, and canvas-border state. Its
risk warning carries stable identity, measurements, severity, and only the actions allowed by the
taxonomy.

## Persistence and validation

The service verifies artifact bytes and validates the graph fingerprint, region IDs, labels, pixel
bounds, and physical bounds on every reopen. In-process reconstruction can additionally recompute
the full analysis from the private assignment raster and reject any mismatch. The assignment itself
is not duplicated in JSON.

At the 1024×683, 200 mm-wide Wager panel scale, the development-machine reference run found 79 thin
lines, 334 local necks, and 67 extended narrow gaps in about 3.4 seconds; the analysis artifact was
about 209 KB. These values are reproducible fixture evidence, not a cross-machine timing promise.
The native-resolution catalog crop covering the tightrope/pole area produces protected dark-line
evidence while leaving every source label byte unchanged.

## Verification

Tests cover exact anisotropic distance transforms, one- and two-pixel threshold behavior, compact
dust, horizontal and diagonal extent, two-lobe necks, one-sided branches, same-label gaps,
threshold-equal gaps, corner contact, transparency, graph/reference validation, canonical
reconstruction, option failures, stable actions, and the private Wager tightrope regression crop.
The API flow proves current-preview publication, exact download, evaluated risk coverage, nullable
legacy compatibility, supersession safety, and process-restart recovery.
