# Geometry job orchestration

`image23mf.geometry.jobs.GeometryJobPipeline` composes geometry implementations with the
existing local worker and content-addressed storage contracts. It is intentionally not a
second job system.

## Stage contract

Adapters are injected for these ordered phases:

1. `vectorize` returns normalized SVG bytes and the vector geometry passed downstream.
2. `topology` converts vector paths into canonical contours and islands.
3. `extrude` returns the meshed canonical `GeometryDocument`, its canonical JSON artifact,
   and an optional consumer-oriented mesh serialization.
4. `validate` returns an acceptance decision and machine-readable report bytes.
5. `preview` renders the validated result.
6. `publish` writes content-addressed blobs; `LocalWorkerManager` then commits every
   artifact record and the successful job state in one SQLite transaction.

The topology and validation callables are seams on purpose. Their implementations can land
independently without changing cancellation, cache, or publication behavior.

The persisted job API currently groups the finer phases into its stable public stages:

| Geometry phase | Job stage | Start progress |
| --- | --- | ---: |
| vectorize | vectorizing | 0.05 |
| topology | meshing | 0.25 |
| extrude | meshing | 0.45 |
| validate | validating | 0.65 |
| preview | packaging | 0.82 |
| publish | packaging | 0.95 |

Every adapter receives a `GeometryStageContext`. Long-running Python work should call
`check_canceled()` at bounded intervals, and external tools should receive its
`cancellation` token directly. The pipeline also checks before and after every adapter.

## Derivation keys and reuse

Use `geometry_derivation_key()` with the source hash, processed-label hash, all effective
settings, and every stage implementation/tool version. The function emits a canonical
SHA-256 digest independent of mapping insertion order.

The pipeline first checks a small verified in-memory cache, then completed job artifacts in
SQLite. Missing or corrupted blobs invalidate a cache candidate. A per-derivation
single-flight lock means simultaneous geometry/export jobs compute once and publish their
own job-scoped artifact records from the same verified blobs.

## Failure and atomicity

Each computation gets an isolated directory under the blob store's temporary root. Python
exceptions, validation rejection, and cooperative cancellation remove that directory.
Content-addressed writes use temporary files plus atomic replacement. A failure while
storing one payload can leave an unreferenced immutable content blob, but never a partial
file or a partial artifact manifest. Artifact visibility is controlled by the worker's
single database transaction.

Successful jobs publish these kinds:

- `geometry-svg`
- `geometry-ir` (the complete canonical meshed `GeometryDocument`)
- `geometry-mesh`
- `geometry-report`
- `geometry-preview` when requested

Production adapters choose the mesh serialization and report schema; those choices and
their versions must be included in the derivation key.
