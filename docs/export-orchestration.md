# Durable Bambu export orchestration

Linear: `24K-95`

`image23mf.exporting.ExportService` is the backend boundary between a verified Geometry IR
artifact and a downloadable Bambu Studio project. Export is an asynchronous durable job; a 3MF is
not downloadable merely because package bytes were created.

## Source authority

`POST /api/projects/{project_id}/exports` accepts the exact geometry artifact ID and SHA-256. The
artifact may belong to a current draft geometry job or an immutable revision. If `revision_id` is
provided, the revision and artifact must both belong to that project and to each other. The service
then verifies the content-addressed blob, exact SHA, canonical Geometry IR bytes, available mesh
capability, and complete material mapping before scheduling work. Cross-project identifiers fail as
not found rather than disclosing ownership.

The client need not know canonical material IDs. An empty mapping is resolved server-side from the
Geometry IR material table into deterministic contiguous extruders using the measured Bambu PLA
Matte preset. Explicit expert mappings remain supported and must cover the table exactly.

## Gated pipeline

The worker repeats source verification to close the request/worker time-of-check gap, then:

1. independently measures every mesh with the configured minimum thickness and P2S build bounds;
2. refuses packaging when any quality error exists, retaining affected geometry IDs and finding
   codes in an actionable job failure;
3. maps canonical parts/materials into a deterministic one-plate Bambu project and round-trips the
   generated package through the independent reader;
4. resolves flattened, SHA-pinned installed P2S machine/process/filament profiles;
5. invokes the cancelable Bambu CLI validator and requires extrusion-bearing sliced-artifact
   evidence, not merely exit code zero;
6. publishes quality JSON, source 3MF, validation JSON, and bounded validation log records in one
   SQLite transaction only after every gate succeeds.

The measured defaults are P2S 0.2 mm nozzle / 0.10 mm layers and P2S 0.4 mm nozzle / 0.20 mm
layers on Textured PEI. Unsupported nozzle/layer combinations fail contract validation.

### Single-plate prime-tower placement

Multi-color exports reserve space at the rear left of the 256 mm bed. Artwork stays
centered when it fits; otherwise it translates into the remaining space without
rotation, scaling, or changes to mesh vertices. The reserve grows with color count
and finer layers. If no layout fits, export asks for a smaller canvas or mural tiles.
The reserve is an initial estimate for PLA, not proof of the final tower footprint.

The package retains the tower coordinates. The CLI reapplies them after loading
installed presets, which otherwise replace them with Bambu's default `(15, 220)`.
That default put four- and six-color photo towers beyond the bed in Bambu 2.8.2.61.
These authored single-plate layouts run **with** Bambu's collision checks enabled;
the existing legacy/mural policy is unchanged. Exit success, actual extrusion, and
all expected colors are still mandatory. Export and validator cache versions are
bumped so prior results do not substitute for the new checks.

## Durability and cache behavior

The canonical request SHA is the job request key. An identical queued/running job is reused; an
identical successful job is reused only after all four output blobs and derivation keys re-verify.
A failed attempt is retryable as a new generation. A corrupt generated cache blob is removed only
after failed content verification so deterministic regeneration can restore it. A newer export
intent supersedes and cancels the prior project export; the worker transaction also checks the
current generation, preventing an uncooperative stale process from publishing late.

Process restart recovery marks abandoned queued/running work as an explicit retryable
`worker_restarted` failure. Cancellation, quality failure, profile failure, timeout, slice failure,
and unexpected worker crashes publish no partial artifact records.

`GET /api/projects/{project_id}/exports/{job_id}` returns the durable job and verified bundle.
Downloads continue through the existing project-scoped artifact endpoint, which re-verifies the
stored blob before serving it.

## Physical validation boundary

Software evidence does **not** claim a successful physical print. Quality and validation reports
carry `manual_print_gate: "not_observed"`. First-layer behavior, color appearance, feature
survival, adhesion, and assembled-panel observations remain an explicit manual gate for 24K-72.

## Verification

```shell
.venv/bin/pytest -q tests/test_exporting.py tests/test_workers.py tests/test_api.py
```

The export suite covers both measured nozzle profiles, a real downloaded/parsed 3MF with a fake
validator, atomic success/failure/cancellation, exact cache hits, corrupt-output retry, quality
failure, supersession, current-draft artifacts, cross-project isolation, and safe downloads. The
worker suite covers durable restart recovery and actionable domain failures.
