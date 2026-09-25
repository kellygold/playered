# Photo preview performance

Status: locally validated — 12 September 2026.

A 3,024 × 2,005 photo rendered to a 1,024 × 1,024, two-color preview produced 37,421 regions and 69,123 printability findings. The original preview job took 758.8 seconds and was observed using more than 19 GB of process memory. Its progress label stayed at “quantizing” through cleanup and printability analysis.

## Changes

- Hole evidence stores an owned crop of each center. Full-canvas masks are reconstructed when an explicit correction needs them. Transparent-void discovery also releases each temporary mask instead of retaining all masks.
- Shared-ring neighbor selection ranks candidates once per ring/color and excludes the current center, preserving the original selection order.
- Region lineage totals visit each component's own overlap edges instead of scanning every overlap for every component.
- Warning overlay generation combines label-wide severity bits once per label, preserving cumulative per-region bits and exact/approximate finding counts.
- Preview jobs report cleanup and analysis separately, with checkpoints between the expensive operations.

## Evidence

The same photo and settings completed preview processing in **141.4 seconds**, approximately **5.4× faster**. Preview-process peak RSS was 3.30 GB. After fetching the complete result, peak RSS was 3.86 GB.

The following persisted artifacts match the original run byte for byte: preview image, processed labels, region graph, automatic cleanup, island analysis, clearance analysis, hole analysis, risk report, and risk mask. The photo and printability decisions were preserved.

65 targeted tests passed across regions, holes, islands, automatic cleanup, risks, workers, and the processing API. Ruff and whitespace checks passed. The new 1,600-hole memory regression sets a 32 MiB traced-allocation ceiling; the old implementation used 59.6 MB on that fixture and fails the ceiling. Tests also cover excluding the current center from cached neighbor selection, cumulative mixed label/region severities, bounds-only warnings, and the reported stage during cleanup and hole analysis.

```sh
python -m pytest tests/test_risks.py tests/test_regions.py tests/test_holes.py \
  tests/test_automatic_cleanup.py tests/test_islands.py tests/test_workers.py \
  tests/test_processing_api.py -q
```

## Remaining bottleneck

The full result endpoint still returned **232.7 MB** and took **48.4 seconds** in the local in-process HTTP test, beyond preview processing time. Large photographic reports need a smaller initial response and deferred detail loading. The preview still contains the original tiny regions and printability findings; this performance change does not make the photo print-ready or validate a 3MF export.

Local benchmark receipts and the photo remain in the existing Burner Tools `output/release-review/photo-preview-performance/` workspace.
