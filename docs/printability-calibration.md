# Printability calibration profiles

Image23MF keeps nozzle-aware cleanup thresholds in a separate, versioned calibration catalog at
`src/image23mf/calibration/printability-v1.json`. These recommendations are product evidence, not
printer hardware specifications, and they never trigger an operation by themselves.

## Evidence status

The bundled Bambu P2S PLA profiles are deliberately marked `pending_print_calibration`. Their 0.2
and 0.4 mm values are nozzle-relative engineering baselines derived from the physical measurement
contracts already used by the risk engine. No local coupon had been printed and measured when the
profiles were created, so every value exposes:

- `basis: engineering_baseline`;
- `confidence: provisional`;
- the feature kinds that can validate it; and
- a human-readable rationale.

The API and client carry a prominent warning until a profile is backed by completed calibration run
IDs. A future profile must not claim `print_validated` while any recommendation still lacks printed
evidence.

| Recommendation | 0.2 mm nozzle | 0.4 mm nozzle | Initial rationale |
| --- | ---: | ---: | --- |
| Minimum island diameter | 0.30 mm | 0.60 mm | 1.5 nozzle diameters |
| Minimum island area | 0.070686 mm² | 0.282743 mm² | area of the diameter above |
| Maximum tiny-hole diameter | 0.40 mm | 0.80 mm | 2 nozzle diameters |
| Maximum tiny-hole area | 0.125664 mm² | 0.502655 mm² | area of the diameter above |
| Minimum ring wall | 0.30 mm | 0.60 mm | 1.5 nozzle diameters |
| Minimum line width | 0.24 mm | 0.48 mm | 1.2 nozzle diameters |
| Minimum supported neck | 0.30 mm | 0.60 mm | 1.5 nozzle diameters |
| Minimum same-label gap | 0.25 mm | 0.50 mm | 1.25 nozzle diameters |
| Long-line evidence length | 0.80 mm | 1.60 mm | 4 nozzle diameters |
| Automatic smoothing | 0 mm | 0 mm | never silently modify artwork |

These are warning/recommendation seeds, not declarations that every smaller feature will fail. The
dot, hole, line, neck, and gap sweeps bracket each seed so the local process can replace assumption
with evidence.

## Generate a coupon and record template

Run all bundled profiles at the default 0.05 mm raster scale:

```sh
.venv/bin/python scripts/generate_printability_calibration.py
```

Or generate one profile into a chosen directory:

```sh
.venv/bin/python scripts/generate_printability_calibration.py \
  --profile bambu-p2s-0.4-hardened-steel-pla-v1 \
  --output workspace/calibration \
  --raster-mm-per-pixel 0.05
```

Each generated coupon directory contains four reviewable files:

- an exact-mm, two-color SVG coupon;
- a discrete black/white PNG for the current raster ingestion path;
- a canonical schema-v2 manifest with exact printer/nozzle/material identity, physical/raster scale,
  hashes, feature IDs, nominal sizes, rasterized X/Y sizes, positions, and bounds; and
- a blank JSON measurement record containing every feature ID.

The 160 × 112 mm coupon has six samples in each of five rows: isolated dots, enclosed holes,
extended lines, supported necks, and separated same-color blocks. It contains no tiny text or
decorative geometry that could contaminate the measurement. The PNG is 3200 × 2240 at the default
0.05 mm scale; the SVG remains the exact physical reference.

## Physical workflow

1. Generate the bundle for the exact profile being tested.
2. Preserve the requested printer, nozzle, material, plate, layer height, filament, and slicer
   profile in the record.
3. Print without resizing. Record every feature as pass, fail, or uncertain, and add measured
   dimensions where useful.
4. Change the record status to `completed`, assign a stable `calibration-run-*` ID, and validate it
   against the manifest. Completed records reject missing provenance, untested features, missing
   feature IDs, or a mismatched artifact hash.
5. Review the transition point for every sweep. Update recommendations and attach the run ID; then
   advance the catalog version and reviewed date.
6. Keep an engineering baseline marked provisional if the physical evidence is ambiguous. Never
upgrade confidence merely to silence the warning.

The local API also provides a restart-safe assembly workflow. A new draft is pinned to the exact
active catalog fingerprint and includes the generated coupon SVG/PNG immediately. Record edits,
setup provenance, 3MF/G-code/settings uploads, and numbered photos advance an optimistic generation
counter. Any evidence mutation clears the prior attestation. Readiness blockers identify an exact
field or attachment and distinguish preparation, unverified draft, attested draft, and sealed
physical evidence; a generated specimen is always labelled `not_observed`.

Attestation binds the operator and server timestamp to a canonical candidate SHA-256 covering the
catalog, artifact, record, metadata, and ordered attachment identities. Finalization rechecks that
candidate inside one SQLite transaction, builds the canonical evidence ZIP on the server, imports
the immutable run, and marks the draft finalized atomically. Retrying a successful finalization is
idempotent. Incomplete drafts and their content-addressed attachments are included in whole-workspace
backup and resume with the same generation and candidate hash after restart or restore.

The current coupon can physically inform dot/island diameter and area, hole diameter and area,
line width, supported-neck width, and same-label gap width. It does **not** contain a ring-wall
sweep, variable long-line lengths, or a smoothing-policy experiment. Ring width, long-line minimum
length, and automatic smoothing therefore remain engineering-policy values until a matching
artifact is designed and observed. A completed run must not be presented as full validation of
those unrelated recommendations.

## Sealed evidence bundle and durable registry

Observed runs are imported as a closed ZIP, not as loose metadata. The bundle contains the exact
coupon SVG/PNG, generated 3MF, sliced G-code, slicer settings, at least one print photo, a completed
observation record, an operator attestation, the complete catalog/profile snapshot, the slicer
application/executable hash, pinned machine/process/filament profile hashes, and exact filament
snapshots. Every declared member carries a SHA-256 digest and size. Undeclared files, path
traversal, links, duplicate paths, unsafe expansion ratios, incomplete observations, changed setup
identity, and altered hashes fail closed.

Import seals immutable SQLite rows and content-addressed blobs. The original uploaded ZIP is
retained alongside its extracted members, so an operator can download the exact submission rather
than a best-effort reconstruction. Re-importing identical evidence is idempotent; reusing a run ID
for different evidence is a conflict. Reads verify the manifest, index columns, original ZIP, and
every retained member before returning `integrity: verified`.

Whole-workspace backup includes the SQLite rows, original evidence ZIPs, coupon sources, slicer
artifacts, settings, and photos. Verification and restore reject a missing or altered calibration
payload just like a missing project asset.

Project bundles currently preserve a project's selected profile ID and catalog fingerprint, but
they do not embed workspace-global physical calibration runs or their potentially large photos,
G-code, and source ZIPs. Use a verified whole-workspace backup when moving or recovering the local
calibration authority. Importing a project bundle into another workspace therefore retains the pin
as provenance; it does not claim that the destination possesses or has re-verified the referenced
physical evidence.

## API and overrides

- `GET /api/printability-profiles` returns the complete catalog, evidence status, sweeps, rationale,
  and source provenance.
- `POST /api/printability-profiles/resolve` selects by printer, nozzle, and material class, accepts
  an optional exact retained `catalog_fingerprint`, and returns all values in stable contract order.
- `GET /api/calibration/specimens/{profile_id}` returns explicit not-observed specimen metadata;
  `/coupon.svg`, `/coupon.png`, and `/bundle` return deterministic source files.
- `POST /api/calibration/drafts` starts a catalog-pinned draft; `GET /api/calibration/drafts` and
  `GET /api/calibration/drafts/{draft_id}` recover retained work.
- `PUT /api/calibration/drafts/{draft_id}` updates the record and setup metadata under an expected
  generation. Member `PUT`, `DELETE`, and verified download routes use role plus ordinal identity.
- `POST /api/calibration/drafts/{draft_id}/attest` binds the current candidate and
  `POST /api/calibration/drafts/{draft_id}/finalize` atomically seals the immutable run.
- `POST /api/calibration/runs/import` accepts the sealed ZIP as an `application/zip` request body.
- `GET /api/calibration/runs` lists verified runs and accepts an optional `profile_id` filter.
- `GET /api/calibration/runs/{run_id}` returns the evidence, observations, hashes, and download
  links without exposing workspace filesystem paths.
- `GET /api/calibration/runs/{run_id}/bundle` downloads the byte-identical original ZIP; member
  download URLs expose each independently verified source artifact.
- Every resolved value says whether it came from the profile or a user override. An override keeps
  the original profile value, basis, confidence, and rationale beside it for comparison.
- Unsupported combinations return a structured 422 response listing available profile IDs.

The physical process identity is intentionally narrower than printer/nozzle/material alone: layer
height, plate, exact filament snapshot, slicer version/executable, and pinned machine/process/
filament profiles are sealed with the run. A future catalog promotion may use that evidence only
for a matching context and only after explicit human review. Merely importing a run never changes
active defaults or upgrades a confidence label.

## Calibration review center

The global **Calibration** navigation opens the local physical-evidence workspace without requiring
an image project. Its dashboard shows the active immutable catalog, exact profile evidence state,
retained drafts, sealed runs, and pending or reviewed proposals. Language is deliberately distinct:
generated preparation, unverified run draft, attested physical observation, sealed physical
evidence, partially print-validated profile, and fully print-validated profile are not interchangeable.

Starting a run pins the active catalog before showing the generated coupon and preparation bundle.
The four-step run workspace captures exact print/setup provenance, every coupon observation,
role-validated source attachments, readiness blockers, candidate hashes, attestation, and final
sealing. Failed observations accept a measured `0`; uncertain remains a first-class outcome. Stale
generation conflicts reload the retained server draft instead of replaying a mutation. Reopened
historical drafts use their own hash-verified coupon members and never substitute a specimen from
the current active catalog.

Sealed compatible runs can be selected to derive a deterministic proposal. The review panel exposes
every before/after recommendation, transition analysis, included source observation, excluded
observation and reason, measured zero, and catalog/process/proposal hash. Accept and reject require
a reviewer, reason, explicit confirmation, and—on acceptance—the exact active head. Keyboard focus
and errors return to the decision surface; stale-head proposals cannot be accepted.

### Repeatable browser and recovery gate

Run the production calibration workflow in an isolated real Chrome session with:

```sh
make qa-calibration
```

The harness creates a temporary workspace and Chrome profile, starts disposable API and Vite
processes, and destroys those runtime directories after the proof. It never imports its synthetic
QA observations into the user's calibration authority. The retained evidence directory defaults to
`workspace/qa/calibration-review/` and contains the exact browser/API/workspace-CLI logs, JSON
reports, downloaded specimen, and desktop/390 px screenshots.

The gate fails unless it observes all of the following:

- a browser-written incomplete draft resumes with the exact generation and candidate SHA-256 after
  the API process is terminated and restarted;
- every observation is retained, including measured `0` and uncertain outcomes, and every required
  role-bound attachment is hash-visible before attestation and sealing;
- proposal review, Escape/focus return, explicit acceptance, and old/new project catalog pins agree
  with the immutable base and promoted fingerprints;
- desktop and compact proposal states have no configured axe A/AA violations, horizontal overflow,
  console errors, failed requests, or network failures;
- whole-workspace backup, preflight, and restore reproduce the exact catalog history, proposal,
  sealed run, draft, attachment closure, and project pins; and
- deliberate mutation of the retained evidence ZIP returns HTTP 409, after which restoring the
  original bytes returns the exact original SHA-256 and verified HTTP 200 response.

These are synthetic, disposable QA records. Passing this gate proves the software workflow and
integrity controls; it does not replace the separate physical Bambu print-and-measure gate.

## Proposal and catalog-promotion lifecycle

Catalogs are retained as immutable SQLite snapshots keyed by their canonical fingerprint. One
singleton head selects the active catalog, while superseded versions remain available to drafts and
revisions pinned to their exact fingerprint. Application startup seeds the bundled catalog only
when no catalog authority exists; it never replaces a promoted head.

`transition-bracket-v1` derives a review proposal only from completed, hash-valid runs that share
the same profile, source catalog, and exact process fingerprint. For every coupon feature it shows
run ID, feature ID/kind, nominal size, rasterized dimension and X/Y extent, pass/fail/uncertain
outcome, measured value (including zero), notes, and whether that observation is included. An
uncertain result, contradictory result at one size, non-monotonic sequence, or missing fail/pass
bracket blocks that feature kind and records the exclusion reason; it does not disappear from the
evidence view.

The sealed proposal is self-contained for review: it records the base and proposed profile
evidence statuses plus each changed recommendation's complete before/after value, unit, basis,
confidence, rationale, feature kinds, and evidence-run IDs. Unchanged policy recommendations stay
visible in the proposed catalog but are not misrepresented as physical changes.

For a monotonic bracket, the proposal uses the smallest consistently passing dot, line, neck, or
gap as the conservative minimum. The tiny-hole warning uses the largest consistently failing hole.
Island/hole areas are derived from the reviewed diameters. These recommendations become
`printed_calibration` with `moderate` confidence only after acceptance. Because the current coupon
does not test ring wall, long-line length, or smoothing policy, those three recommendations remain
unchanged engineering baselines and the profile can become only `partially_validated`.

Accept and reject are separate terminal operations requiring reviewer and reason. Acceptance also
requires the reviewer to submit the exact expected active-catalog fingerprint. The service
re-verifies all source runs and member blobs inside the review path, inserts the proposed catalog,
records an immutable promotion edge, and moves the head atomically. A competing review based on a
superseded head fails with 409 and leaves its proposal pending. Database triggers reject direct head
mutation without a matching promotion edge and reject mutation/deletion of reviewed proposals,
contributions, promotions, or catalog versions.

The catalog APIs are:

- `GET /api/printability-profile-catalogs` and
  `GET /api/printability-profile-catalogs/{fingerprint}` for active/history inspection;
- `POST /api/calibration/proposals` for deterministic derivation from canonical run IDs;
- `GET /api/calibration/proposals` and `GET /api/calibration/proposals/{proposal_id}` for review;
- `POST /api/calibration/proposals/{proposal_id}/accept` or `/reject` for explicit disposition.

Catalog activation is forward-looking. Existing drafts continue resolving the exact retained
fingerprint already stored in their cleanup configuration; routine autosave, preview, publication,
and reopen never silently move them to a newly active catalog. New projects use the active head.
An unknown pin returns an actionable conflict asking for the source workspace backup or an explicit
reviewed migration.

Saved cleanup configuration pins the selected printability profile and catalog fingerprint. It
also stores the exact set of user-overridden cleanup fields separately from their resolved numeric
values. Changing nozzles therefore refreshes untouched defaults from the new profile while carrying
only explicit overrides forward. Older drafts without that provenance are migrated conservatively:
their saved cleanup numbers are treated as user overrides rather than silently discarded.

Every preview publishes a hash-verified `printability-settings` artifact containing the complete
resolved value list, profile/catalog identity, evidence status, rationale, and per-value source.
The derivation key includes the injected calibration catalog fingerprint, so alternate or updated
catalogs cannot alias bundled-catalog results. Line, supported-neck, and same-label-gap thresholds
remain independent throughout resolution, analysis, warnings, and serialized evidence.

Zero is a valid explicit override. Profile resolution never mutates a label field, starts a cleanup
job, or infers that the user accepted a destructive policy.
