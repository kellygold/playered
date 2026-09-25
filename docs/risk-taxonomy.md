# Printability risk taxonomy and report contract

Linear issue: `24K-47`

Every current preview publishes a hash-verified `risk-report` JSON artifact tied to the exact
`region-graph` fingerprint. The report is evidence, not a generic quality score: every warning names
its measured feature, severity, affected stable region IDs/labels/bounds, classifier provenance, and
only actions permitted for that risk code.

## Stable codes and severity

Schema v1 reserves eight codes:

| Code | Meaning |
| --- | --- |
| `small_island` | A separate region falls below a physical-area policy. |
| `tiny_hole` | An enclosed region or void falls below a physical-area policy. |
| `hollow_ring` | A ring wall/center combination risks leaving an empty perimeter artifact. |
| `narrow_neck` | A connection between larger regions is narrower than a physical threshold. |
| `thin_line` | An elongated intentional-or-accidental feature is too narrow for the setup. |
| `narrow_gap` | Neighboring features leave a gap narrower than a physical threshold. |
| `excess_fragmentation` | One label is split into unusually many four-connected paths. |
| `color_absent` | A configured palette label has zero active pixel assignments. |

Severity is `info`, `warning`, or `error`; reports sort errors first, then warnings, then
information. Warning IDs are content addresses of code, classifier feature key, affected region
IDs, and labels. Updating an explanation or threshold does not churn the ID for the same measured
feature, while a topology change intentionally does.

## Measured evidence

Evidence is a canonical list of stable measurement keys with `measured`, `threshold`, or `context`
roles. Units are enforced per key: widths/perimeters/nozzle use millimetres, area uses square
millimetres, coverage uses a 0–1 ratio, and component density uses count per 100 mm². A report with a
wrong unit, duplicate key/role pair, inconsistent summary, unknown graph reference, duplicate
warning identity, or noncanonical ordering is rejected.

Current analysis evaluates all eight stable codes: physical small islands, enclosed holes, hollow
rings, thin lines, narrow necks, narrow gaps, graph-level color absence, and fragmentation. The provisional
fragmentation defaults are 64 components per label or 20 components per 100 mm², with error
severity beyond four times either limit. These are transparent options bound into the report
fingerprint, not claimed printer calibration; 24K-53 owns calibrated 0.2/0.4 mm defaults.

## Honest classifier coverage

The report partitions the full taxonomy into `evaluated_codes` and `pending_codes`. A warning may
only use an evaluated code, and the two lists must be a complete canonical partition. Current
preview reports have an empty pending partition; a code with zero warnings was still evaluated.
Legacy previews keep their honest partial partitions.

## Valid suggestions

Suggestions use a closed action vocabulary with enforced safety flags. The valid subsets are:

- island: review, keep, or merge into the dominant, perceptually closest, or explicitly selected
  palette color;
- hole: review, keep, fill, or explicitly recolor the center;
- ring: review, keep, fill/recolor the center, or collapse the bounded local ring;
- neck: review, keep, or widen;
- line: review, protect the intentional line, or widen;
- gap: review, keep, or close the gap;
- fragmentation: review, keep, smooth, or use either measured merge policy;
- absent color: keep, remove the palette slot, or replace/reclassify the color.

Any operation that changes labels, geometry, or palette state is marked destructive and requires
confirmation. `review`, `keep`, and `preserve_line` do not claim to modify pixels. Later UI tickets
can render and invoke these actions without inventing code-specific policy in the client.

## Verification

Contract tests cover all eight codes, action matrices, unit/safety validation, stable warning IDs,
canonical serialization, graph-reference validation, duplicate rejection, and summary integrity.
A controlled label fixture proves deterministic fragmentation and color-absence classification; the
committed 0.4 mm geometry fixture proves warnings retain real stable region IDs. The API user flow
proves risk-report publication, exact graph/report binding, verified download, nullable legacy
compatibility, supersession safety, and process-restart recovery.
