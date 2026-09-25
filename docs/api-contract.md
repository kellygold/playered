# HTTP and job contract

Linear issue: `24K-18`

The local API is versioned through its OpenAPI document at `/openapi.json`. Every handled failure
uses one envelope:

```json
{
  "error": {
    "code": "not_found",
    "message": "The requested job does not exist.",
    "request_id": "...",
    "retryable": false,
    "details": {}
  }
}
```

`X-Request-ID` is returned on successful and failed responses. Public messages are actionable but
do not contain stack traces, absolute internal paths, SQL, or secrets.

## Foundation resources

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/api/health` | Process liveness, application version, workspace, tools |
| `GET` | `/api/capabilities` | Typed local tool and feature availability |
| `GET` | `/api/jobs/{job_id}` | Current durable job resource and published artifact IDs |
| `POST` | `/api/jobs/{job_id}/cancel` | Idempotently cancel queued/running work |
| `GET` | `/api/projects/{project_id}/artifacts/{artifact_id}` | Download project-owned bytes after ownership/hash/size verification |

## Filament library resources

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/api/filaments` | Search/filter custom and owned physical colors |
| `POST` | `/api/filaments` | Add a custom filament color |
| `GET` | `/api/filaments/{filament_id}` | Read one durable filament record |
| `PATCH` | `/api/filaments/{filament_id}` | Edit identity, color, metadata, or owned state |
| `DELETE` | `/api/filaments/{filament_id}` | Delete an unreferenced filament |
| `GET` | `/api/filament-catalogs/bambu-lab-starter` | Read the versioned starter catalog |
| `POST` | `/api/filament-catalogs/bambu-lab-starter/import` | Import selected catalog colors atomically |

The starter catalog is explicit and fingerprinted; importing it never overwrites custom colors.
Import is idempotent and promotes an existing matching record to owned. Filament identity is the
case-insensitive manufacturer/family/name plus canonical uppercase hex color. Saved draft and
revision palettes may omit physical spool references, but any supplied `filament_id` must resolve.
Referenced colors cannot be deleted. Projects therefore reopen without dangling palette links, and
bundle export includes the exact referenced physical records.

## Printer and printability profile resources

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/api/profiles` | Versioned printer/nozzle/plate catalog and provenance |
| `POST` | `/api/profiles/validate` | Resolved compatible print setup or all actionable issues |
| `GET` | `/api/printability-profiles` | Versioned thresholds, evidence status, sweeps, and rationale |
| `POST` | `/api/printability-profiles/resolve` | Stable profile values plus visible user overrides |
| `GET` | `/api/calibration/specimens/{profile_id}` | Explicit not-observed specimen metadata and source links |
| `GET` | `/api/calibration/specimens/{profile_id}/{coupon.svg\|coupon.png\|bundle}` | Deterministic specimen source or ZIP |
| `POST` | `/api/calibration/drafts` | Start a restart-safe, exact-catalog-pinned evidence draft |
| `GET` | `/api/calibration/drafts` | Retained incomplete and finalized draft collection |
| `GET` | `/api/calibration/drafts/{draft_id}` | Candidate hash, evidence state, and field-level blockers |
| `PUT` | `/api/calibration/drafts/{draft_id}` | Generation-guarded record and setup update |
| `PUT/DELETE` | `/api/calibration/drafts/{draft_id}/members/{role}/{ordinal}` | Stage or remove bounded role-validated evidence |
| `POST` | `/api/calibration/drafts/{draft_id}/attest` | Bind operator confirmation to the current candidate hash |
| `POST` | `/api/calibration/drafts/{draft_id}/finalize` | Atomically build, verify, and seal the canonical run |
| `POST` | `/api/calibration/runs/import` | Validate and seal one complete physical-evidence ZIP |
| `GET` | `/api/calibration/runs` | Verified run collection, optionally filtered by profile |
| `GET` | `/api/calibration/runs/{run_id}` | Exact evidence, observations, hashes, and download links |
| `GET` | `/api/calibration/runs/{run_id}/bundle` | Byte-identical original evidence ZIP |
| `GET` | `/api/calibration/runs/{run_id}/members/{member_id}` | One verified evidence member |
| `GET` | `/api/printability-profile-catalogs` | Active and superseded immutable catalogs |
| `GET` | `/api/printability-profile-catalogs/{fingerprint}` | One exact retained catalog snapshot |
| `POST` | `/api/calibration/proposals` | Deterministic transition proposal from exact run IDs |
| `GET` | `/api/calibration/proposals` | Pending/accepted/rejected proposal collection |
| `GET` | `/api/calibration/proposals/{proposal_id}` | Proposal diff and every contribution |
| `POST` | `/api/calibration/proposals/{proposal_id}/accept` | Guarded atomic catalog promotion |
| `POST` | `/api/calibration/proposals/{proposal_id}/reject` | Terminal reviewed rejection |

Printability profile resolution never starts processing or applies cleanup. An optional catalog
fingerprint resolves an exact retained version so reopened drafts cannot silently adopt a promoted
head. Provisional profiles
carry a warning, every value carries basis/confidence/rationale, and overrides retain the replaced
profile value beside the user value. Unsupported printer/nozzle/material keys return the standard
422 envelope with every available profile ID. See
[printability-calibration.md](printability-calibration.md) for coupon and evidence workflow.

Calibration imports accept a raw ZIP body with a bounded wire size and a closed member contract.
They are idempotent for the same sealed run and return 409 if a run identity is reused or retained
bytes fail verification. API resources deliberately omit content-store paths; all evidence is
retrieved through verified download URLs. Importing physical observations does not alter the active
catalog—proposal review and catalog promotion are separate explicit operations.

Proposal acceptance requires the exact expected active-catalog fingerprint plus a non-empty
reviewer and reason. Stale reviews return 409 without changing the proposal or catalog head.
Accepted versions never erase their parent. Saved project cleanup config continues to resolve its
exact retained catalog fingerprint after activation; only new projects use the new head. The current
coupon can promote dot/island, hole, line, neck, and gap transitions only, so an accepted proposal
remains partially validated while ring-wall, long-line, and smoothing recommendations retain their
engineering basis.

## Image Lab resources

| Method | Path | Result |
| --- | --- | --- |
| `POST` | `/api/projects/import` | Normalize image bytes and create a persisted project/draft |
| `GET` | `/api/projects/{project_id}` | Reopen the project, source provenance, and current draft |
| `GET` | `/api/assets/{asset_id}` | Retrieve a verified normalized source for project reload |
| `PUT` | `/api/projects/{project_id}/draft` | Autosave config plus ordered operations under a generation guard |
| `POST` | `/api/projects/{project_id}/draft/history/undo` | Undo one exact persisted command under generation/cursor guards |
| `POST` | `/api/projects/{project_id}/draft/history/redo` | Redo one exact persisted command on the active lineage |
| `GET` | `/api/projects/{project_id}/revisions` | Compact newest-first immutable revision summaries |
| `GET` | `/api/projects/{project_id}/revisions/{revision_id}` | Reopen one exact immutable revision |
| `POST` | `/api/projects/{project_id}/revisions` | Atomically publish the guarded draft and continue editing |
| `POST` | `/api/projects/{project_id}/revisions/{revision_id}/branch` | Replace the guarded draft with an editable branch of a revision |
| `POST` | `/api/projects/{project_id}/palette/auto` | Fit a crop-aware palette while preserving locked indices |
| `POST` | `/api/projects/{project_id}/previews` | Save the guarded draft and enqueue a superseding preview |
| `GET` | `/api/jobs/{job_id}/events` | Server-sent job snapshots until a terminal state |
| `GET` | `/api/jobs/{job_id}/result` | Ordered artifacts and versioned preview statistics |

Import uses the raw request body so local drag/drop and clipboard images do not require multipart
form parsing. `X-Filename` preserves the user-visible source name; signatures, not that name or the
request MIME type, select PNG/JPEG/WebP decoding. The response includes the normalized source
metadata and generation `1` draft needed to start editing. Reopened projects also expose their
persisted latest preview head, so a completed or restart-recovered job remains discoverable after
the browser or API process loses in-memory state.

Starting a preview requires the complete versioned `JobConfig` and the caller's expected draft
generation. A stale generation returns `stale_draft`; invalid printer/profile combinations return
all suggestion-bearing compatibility issues; unknown filament references return `validation_error`
with the missing IDs. Each newer preview atomically supersedes the prior
project preview head, so delayed native or image work cannot publish a stale result.

Draft resources expose their ordered operation log, exact configuration SHA-256, and typed editor
sequence SHA-256. Every committed editor configuration path uses the same generation-guarded save
contract; preview freshness requires both fingerprints to match. This makes reload, snapshot-based
undo, and stale-preview detection deterministic. The automatic-palette
resource returns only normalized saved colors and fit evidence; exact locked positions are copied
unchanged. See [palette-editor.md](palette-editor.md) for the interaction and persistence contract.

### Immutable revision lifecycle

Revision publication accepts a required trimmed `label`, optional `notes`, the exact
`expected_draft_generation`, and an optional `preview_job_id`. A supplied preview must be the
successful, current, derivation-matching preview for that exact draft, and every source artifact
must still pass content-addressed size/SHA verification. The API then copies those artifacts into
revision ownership with source-job provenance. A wrong, stale, failed, foreign, or corrupt supplied
preview returns `conflict` and rolls back the entire publication. Omitting the preview intentionally
publishes configuration and operations with zero artifacts and explicit `missing` preview evidence;
clients must not present that as a verified visual snapshot.

Successful publication returns `{revision, draft, project}` with status `201`. In one transaction it
creates the immutable revision, copies the exact ordered operations, persists preview evidence,
updates `project.active_revision_id`, and replaces the consumed draft with a continuation draft.
The continuation is byte-equivalent to the published configuration/operations, uses the revision as
`base_revision_id`, and advances the draft generation by one. A stale generation returns
`stale_draft`; validation or any storage failure leaves the prior draft and active revision intact.

`GET /revisions` is deliberately compact and keyset-paginated with a bounded `limit`, optional
opaque `cursor`, stable `total`, and optional `next_cursor`. Each newest-first summary contains
identity, parent, label, configuration/editor-sequence fingerprints, operation/artifact counts,
preview evidence, active status, and publication time, but not full config, operation, or artifact
arrays. The page, active pointer, and count come from one SQLite read snapshot. The detail resource
reopens those exact immutable arrays plus engine/source identity, notes, and evidence. Its
preview evidence records `fresh`, `missing`, `stale`, or `legacy` status, a human-readable reason,
source draft generation, editor-sequence fingerprint, optional source job/derivation/manifest
fingerprints, and artifact count.

Revision operations, artifacts, publication evidence, and history seals become insert/update/delete
immutable only after the full revision is assembled in its publication transaction. Direct row
replacement is rejected, while intentional whole-project deletion may still cascade. Fresh artifact
manifests are recomputed on read, and artifact downloads require the owning project path so a foreign
project cannot use an otherwise valid artifact ID as an ownership oracle.

Every non-palette editor operation is bound to the final normalized configuration fingerprint.
Draft save and preview-start validate the complete candidate inside the generation-guarded write
transaction before inserting or updating anything; branch and publish apply the same sequence
validation. A mismatch returns HTTP `422` with code `incompatible_editor_history` and redacted
details `{project_id, reason, operation_count, conflict_count, action}`. It never exposes selector
fingerprints, never advances the draft generation, and leaves subsequent project reads healthy.
Palette audit operations are configuration history and remain exempt from raster replay.

### Persisted editor command history

Each draft exposes a compact `history` summary with its lineage, cursor and tip node IDs, active
position, 100-step undo horizon, adjacent command labels, and an exact state fingerprint. The
fingerprint binds the history schema, lineage, base revision, source asset, complete job config,
and ordered operations; it is distinct from the raster-only editor-sequence fingerprint.

`PUT /draft` accepts optional `history_command` metadata: version `1`, a UUID `id`, semantic
`command_type`, user-facing `label`, `before_state_sha256`, and `expected_cursor_node_id`. The
server derives the exact after-state, validates typed transitions (including the atomic
`clear_manual_and_apply` operation), and commits the materialized draft, immutable DAG node,
cursor, generation, and retry receipt in one SQLite transaction. Repeating the same UUID with the
same canonical request returns the original result; reusing it with different input or sending a
stale generation/cursor returns `409` without retrying or overwriting another client.
The public command types are `config_change`, `palette_change`, `manual_operation`, and
`clear_manual_and_apply`; generic compatibility commands are server-internal and rejected in API
requests.

Undo and redo accept a UUID `request_id`, `expected_draft_generation`, and
`expected_cursor_node_id`. They validate and replay the content-addressed target snapshot before
advancing both cursor and draft generation. An edit after undo selects a new tip while retaining
the abandoned descendants as local audit history. Publication closes the current lineage and
creates a non-crossable continuation anchor at the new base revision; explicit revision branching
also starts a fresh lineage. The active undo walk loads one target at a time and stops at 100
commands, while older and abandoned SQLite nodes remain retained.
Because retained states must remain undoable and auditable, a filament referenced by any current,
published, or retained-history palette cannot be deleted. The API returns `409` until that retained
history is explicitly removed by a future archival workflow; it never leaves a silently broken
undo target.

Existing pre-v10 drafts and current-state-only bundle restores synthesize one exact, non-undoable
anchor on first local access. Portable export/remapping of the complete DAG is deliberately
deferred to 24K-100; bundle restore never pretends that omitted history was preserved.

Branching accepts the current `expected_draft_generation` and returns
`{base_revision, draft, project}`. It advances the mutable generation and copies the selected
revision's exact config and ordered operations into a new draft whose `base_revision_id` is that
revision. It never mutates the selected revision or changes the project's active published pointer.
Cross-project revision paths return `not_found`; concurrent draft replacement returns
`stale_draft` without changing either draft.

Raster editor operations use the lossless `editor_command_v1` envelope. Region commands bind their
configuration and current graph fingerprints plus a selected/similar scope; similar scopes include
canonical anchors, a one-thousandth-grid physical predicate, the exact accepted region IDs, and a
resolution fingerprint. Replay validates the scope immediately before each command, enforces
palette/activity/protected-pixel invariants atomically, and rejects stale, ambiguous, oversized,
invalid, or true no-op operations instead of partially publishing artifacts. Classified-hole fills
bind the backend-published hole-analysis fingerprint rather than attempting to recreate a Python
model fingerprint in the browser.

Polling `GET /api/jobs/{job_id}` and the SSE event resource expose the same durable `JobResource`.
Successful preview results contain automatic-cleanup record/mask/preview artifacts, exact editor
replay and editor-mask artifacts, `clearance-analysis`, `hole-analysis`, `island-analysis`,
`palette-metrics`, continuous/quantized/processed preview images, `preview-statistics`,
`printability-settings`, processed labels, `region-assignment`, `region-graph`, and `risk-report` in
stable kind order. `region-assignment` is a SHA-verified, row-major signed-int32 little-endian plane:
each active pixel contains its exact zero-based index in the ordered region-graph tuple and each
inactive pixel contains `-1`. Its metadata binds dimensions, region count, encoding, and graph
fingerprint; the verified graph binds the active-pixel count. Clients must validate those bindings and every region pixel count
before enabling canvas picking; older previews remain inspectable through the warnings/region list
but cannot claim exact canvas-to-region selection.
The automatic-cleanup record pins the configured island policy and physical smoothing, exact
before/after graph and label fingerprints, and changed-mask hash/count. Statistics
schema v1 pins the config and profile-catalog fingerprints, canonical transform, source/preview
dimensions, physical pixel scale, alpha counts, and continuous-preview hash. Palette metrics schema
v1 pins the config, quantization, and metric-option fingerprints plus exhaustive coverage, error,
adjacency, and fragmentation data.
The printability-settings artifact pins the complete resolved calibration values, catalog/profile
identity, evidence status, rationale, and whether each value came from the profile or an explicit
user override. Draft cleanup configuration separately pins profile identity and override-field
provenance, so nozzle changes refresh only non-overridden defaults.
Artifacts are content-addressed and hash/size verified on retrieval; projects, drafts, completed
jobs, artifacts, statistics, and metrics remain reopenable after process restart. Pre-v2 previews
remain readable with nullable metrics, and pre-v3 previews remain readable with a nullable region
graph. Region-graph schema v1 supplies stable connected-component IDs, exact physical measurements,
neighbor boundary lengths, border contact, and versioned width estimates; see
[region-graph.md](region-graph.md).
Pre-v4 previews remain readable with a nullable risk report. Risk-report schema v1 binds stable
codes, severity, measurements, classifier coverage, explanations, and valid suggestions to the
exact graph fingerprint; see [risk-taxonomy.md](risk-taxonomy.md).
Pre-v5 previews remain readable with a nullable island analysis. Island-analysis schema v1 binds
physical small-region criteria, exemption state, and measured neighbors to that same graph; see
[small-island-policies.md](small-island-policies.md).
Pre-v6 previews remain readable with a nullable clearance analysis. Clearance-analysis schema v1
binds physical thin-line, neck, and gap evidence to stable regions; see
[clearance-analysis.md](clearance-analysis.md).
Pre-v7 previews remain readable with a nullable hole analysis. Hole-analysis schema v1 binds
enclosure topology, physical centers, surviving local walls, and safe correction bounds to the
graph; see [hole-ring-correction.md](hole-ring-correction.md).

Feature tickets add project, image, preview, geometry, and export creation resources without
changing these shared envelopes.

## Job states

```text
queued ──> running ──> succeeded
   │          ├──────> failed
   │          ├──────> canceled
   │          └──────> superseded
   ├─────────────────> failed
   ├─────────────────> canceled
   └─────────────────> superseded
```

Terminal jobs cannot transition again. Repeating the same cancellation or supersession returns the
same terminal resource. Progress is monotonic while running. A successful job has progress `1` and
stage `complete`; failed jobs include a stable failure code; canceled jobs include both completion
and cancellation timestamps. SQLite triggers enforce type, stage, terminal timestamp, failure, and
progress consistency even outside the repository.

Superseding jobs also expose a stable key and monotonic generation. Only the current persisted head
may publish artifacts. Application restart converts unreconstructable queued/running work to a
retryable `worker_restarted` failure.

Artifact database records are not sufficient proof of availability: retrieval verifies the
content-addressed file's size and SHA-256 digest first and returns `blob_unavailable` on corruption.
