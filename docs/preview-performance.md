# Preview performance: 16 September 2026

Measured locally on the same saved 3024 × 2005 two-color photo and settings,
with a 1024-pixel preview containing 37,421 regions. Each run used an isolated
workspace; the user's working project was not modified.

| Measurement | Before | After |
| --- | ---: | ---: |
| Preview processing | 111.87 s | 67.40 s |
| Result validation, serialization and response | 22.91 s | 10.58 s |
| Combined | 134.78 s | 77.98 s |
| Response transfer | 232.68 MB | 19.05 MB (gzip) |

This is about 42% less combined waiting time on this workload. These are local
wall-clock measurements, not guarantees for every image or end-to-end print
build. The decoded response remains 232.68 MB; browser parsing/memory and report
size remain candidates for further work.

## Changes

- Validate assignment/label agreement in one array pass instead of rescanning
  the entire image for every region.
- Cache repeated color-pair distance calculations within island analysis.
- Validate small hole/clearance reference lists with direct lookups instead of
  repeatedly traversing the complete region dictionary.
- Batch independent clearance calculations across up to eight spawned workers.
  Small workloads remain serial, masks are cropped and packed for transport,
  and estimated packed masks above 64 MiB keep the serial path.
- Preserve canonical feature ordering, cancellation and worker error handling.
  Detect abrupt worker exits so a replaced process cannot leave a result pending
  forever. The shared worker implementation also serves geometry builds.
- Serialize the already validated response model directly and compress large
  responses when the client accepts gzip. Artifact integrity checks remain.

A separate clearance-only comparison took 24.92 s with one worker and 13.70 s
with eight. It observed eight child workers and peak aggregate child CPU usage
of 756.8% (roughly 7.6 logical cores). Serial and parallel results matched exactly.

## Evidence

Nine saved artifacts matched the baseline byte-for-byte: region graph, palette
preview image, processed labels, automatic cleanup, hole analysis, island
analysis, clearance analysis, risk report and risk mask. Cancellation, abrupt
worker failure, malformed assignments, compression negotiation and decoding have
focused regression coverage.

Private benchmark inputs and machine-specific receipts are kept outside this
repository under `burner-tools/output/release-review/preview-performance-20260916/`.
The relevant receipts are `baseline.json`, `final.json`, and
`clearance-workers.json`. Do not redistribute the private input photo as a fixture.

A real two-color 50 mm test model completed import, preview, geometry, installed
Bambu Studio slice validation and 3MF download. The downloaded package's SHA-256
matched its API receipt. Slice evidence contains nine layers, two used extruders
and observed extrusion. This verifies software export, not a physical print.

## Validation status

The full Python suite passed: **818 passed, 8 skipped** (222.17 s). Targeted Ruff
checks and whitespace validation passed. No frontend code changed in this patch.

One independent, read-only Claude review examined engine/API commit `eae7273`
in a clean detached worktree. It reached the eight-minute limit without a final
verdict or a concrete finding returned. This is **incomplete review, not a pass**.
The changes are for local use; this evidence does not declare the application
ready for public release.

## Independent review completed: 24 September 2026

At the user's request, a fresh Claude review completed in 103 seconds against
`5435e53..ddd4af1`, in a clean detached worktree. It found **no P0/P1 blocking
defects** and independently ran **21 targeted tests, all passing**. This resolves
the incomplete review recorded above for this performance patch; it is not a
whole-application release certification.

The review covered spawned worker lifecycle and cancellation/error handling,
source-label validation, deterministic clearance output, resource bounds, and
JSON/compression behavior. It did not independently repeat the full suite or
large-photo benchmark. The existing 818-pass suite and exact-artifact comparison
remain the author evidence. A fresh isolated import → preview → geometry → real
Bambu Studio slicing → verified 3MF download smoke test also passed on 24 September.

Nonblocking concerns were triaged against the source:

- The worker budget is per job. `LocalWorkerManager` defaults to two concurrent
  jobs, and the API uses that default: two parallel CPU stages can therefore
  create up to 16 child workers. The eight-worker limit is not a global cap.
- Abrupt-exit detection uses CPython's private `Pool._pool` attribute; the crash
  regression test must remain part of Python-upgrade validation.
- Direct JSON serialization matches the current response contract. Future field
  aliases or route-level exclude options need explicit handling in this path.
- Legacy `x-gzip` clients receive uncompressed JSON; ordinary gzip works.

The bulky decoded browser report and the physical-print observations documented
in `release-golden-gate.md` remain separate from this patch's review. The saved
local workspace was backed up before restart and all three drafts stayed unchanged.

Machine-specific receipts, the complete reviewer verdict, and the fresh smoke
result are under `burner-tools/output/release-review/preview-independent-20260924/`.
