# Workspace health and safe cleanup

Linear issue: `24K-26`

Image23MF Studio can audit a local workspace without changing it and can reclaim only files from an
explicitly reviewed cleanup plan. These APIs are intended to back a future workspace-health screen;
they are already available through the typed browser client.

## Health report

`GET /api/workspace/health` reports:

- SQLite structural and foreign-key integrity;
- the database migration version versus the running application;
- disk capacity and per-namespace byte/file totals;
- missing or corrupt database-referenced assets and artifacts;
- unreferenced content-addressed files, cache entries, stale temporary files, and managed-directory
  symlinks;
- a severity, plain-language explanation, and concrete recovery advice for each issue.

Missing source assets and corrupt referenced files are critical. Missing derived artifacts are
warnings because they can normally be regenerated. Orphaned content is informational and is never
deleted by the health check.

## Two-step garbage collection

1. `POST /api/workspace/gc/plan?minimum_age_seconds=86400` creates a read-only dry-run plan.
2. The caller displays its paths, reasons, counts, and reclaimable bytes for review.
3. `POST /api/workspace/gc/apply` sends the exact `plan_sha256` and safety age from that plan.

Planning defaults to a 24-hour minimum age. Eligible categories are unreferenced files under
`assets/` or `artifacts/`, cache files, and stale temporary files. Database-referenced content is
excluded even when old.

Apply recomputes the plan and returns HTTP 409 if the workspace changed after review. Immediately
before each deletion it checks the live database references, managed path, regular-file status,
modification time, byte count, and SHA-256 again. Changed or newly referenced files are preserved
and reported as skipped. Symlinks are never followed. There is no endpoint that applies cleanup
without a reviewed plan fingerprint.

## Recovery guidance

- Preserve a critical workspace before attempting repair.
- Restore sources from a verified project bundle or re-import the original image.
- Regenerate missing derived artifacts, or restore an artifact-inclusive bundle.
- Open a schema newer than the application only with the application version that created it.
- Generate a fresh dry run whenever an apply reports a stale plan or skipped candidate.
