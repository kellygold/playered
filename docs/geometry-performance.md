# Geometry performance

Locally measured on 12 September 2026, using the same saved 1,024 × 1,024 two-color photo preview with 474 regions. No artwork, palette, cleanup settings, or printer settings were changed.

## Changes

- Use conservative spatial indexes to find potentially intersecting edges. The original exact intersection and tolerance predicates still decide validity.
- Replace repeated boundary membership scans with a set.
- Index hole-edge checks and evaluate the existing point-in-polygon ray test with NumPy. Boundary tolerance remains explicit.
- Normalize canonical JSON values once, preserving canonical bytes and content IDs.
- Extrude independent islands in a macOS-compatible spawn process pool. Complex artwork uses up to eight workers, reserves two logical CPUs, and leaves small inputs serial. Larger pieces start first; results return to canonical island order. Cancellation and exceptions terminate and reap child workers.

## Measurements

The original geometry job took **578.290 seconds** (9 minutes 38 seconds). The optimized, uncached adapter pipeline completed in **45.234 seconds**, approximately **12.8× faster**. The latter includes vectorization, topology, extrusion, validation, and geometry preview, with small additional diagnostic file writes; it excludes API publication and 3MF export/slicing.

| Optimized stage | Seconds |
| --- | ---: |
| Vectorization | 0.063 |
| Topology | 12.133 |
| Extrusion, assembly, and serialization | 26.030 |
| Mesh validation | 2.866 |
| Geometry preview | 4.090 |

An isolated comparison of the same optimized extrusion routine took **10.175 seconds with one worker** and **6.006 seconds with eight**, with identical output fingerprints. The eight child processes reached an observed combined **485.7% CPU**. Most of the overall improvement comes from removing unnecessary comparisons; the entire pipeline is not parallel, and spawning workers for tiny models would add overhead.

The complete Geometry IR and validation report match the original build byte for byte. Validation accepts the resulting geometry. Original export/slicer validation took another **147.264 seconds**; the optimized export path has not been independently timed, and the 45-second measurement must not be presented as a complete 3MF download time.

Local benchmark scripts and receipts are under the existing ignored Burner Tools `output/release-review/geometry-performance/` directory. Personal image assets are not included in this repository.

## Verification

The full Python suite completed with 798 passed, 8 skipped, and one worktree-environment failure: a calibration CLI test hardcodes `.venv/bin/python`. After linking the existing virtual environment into the isolated worktree, that test passed on rerun, giving 799 passing tests in total. No dependency installation or test-code workaround was needed.

Coverage includes exhaustive-versus-indexed bounding-box comparisons, a large-boundary comparison budget, tolerance-sensitive scalar-versus-vectorized containment, exact serial/parallel mesh equality, distinct worker processes, cancellation, worker exceptions, and child cleanup. Existing geometry-invalid fixtures, raster comparisons, provenance, packaging, API, and worker tests were retained. Ruff and whitespace checks pass.

An independent Claude review of implementation commit `21521be` against `005628e` found no P0/P1 defects. It independently checked candidate pruning, tolerance-sensitive containment, canonical bytes, worker exception propagation, cancellation, and spawning from a non-main thread. Review receipts remain alongside the local benchmarks. An abruptly killed child (as opposed to a raised exception) requires cancellation to escape the standard pool iterator; automatic crash detection is not added here.
