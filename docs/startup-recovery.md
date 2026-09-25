# Startup recovery and interruption evidence

Linear issue: `24K-74`

Image23MF Studio reconciles durable work whenever the API process starts. The reconciliation is
deliberately conservative: it records what survived, fails jobs whose worker disappeared, and never
deletes files or claims to resume a callback that no longer exists.

## Startup contract

- Jobs left `queued` or `running` become `failed` with `worker_restarted`.
- The report retains each job's exact pre-restart state and stage.
- Interrupted jobs are `retryable`, but `resumable` is false. The UI tells the user which normal
  project action recreates that work.
- Every surviving temporary file and unreferenced content-addressed artifact is counted.
- `automatic_deletions` is always zero. Cleanup remains the separately reviewed, fingerprinted
  two-step flow documented in `workspace-health.md`.
- Database integrity and foreign-key results are captured with the recovery evidence.
- Every report is stored as immutable JSON in `startup_reconciliations`; later checks append rather
  than rewriting history.

## User notifications

The studio shows a startup warning only for interrupted work or failed integrity
checks. Leftover temporary/unreferenced files alone do not require user attention.
Click the local-engine status to inspect the workspace report and preview optional
cleanup. Inspection does not delete files. The stored reconciliation retains its
original status and evidence even when no banner is shown.

## API evidence

- `GET /api/workspace/recovery/latest` returns the newest durable report.
- `GET /api/workspace/recovery/history?limit=20` returns newest-first immutable evidence.
- `POST /api/workspace/recovery/reconcile` performs the same safe, non-destructive check on demand.

The workspace health endpoint additionally reports disk free space and thresholds, cache size and
disposition, and detected external-tool availability, version, path, compatibility, and advice.

## Atomic publication boundary

Worker publication has explicit evidence checkpoints around one SQLite transaction:

1. content-addressed blobs verified;
2. database transaction opened;
3. artifact rows written;
4. job and artifacts committed.

Process-death tests terminate a spawned Python process at every checkpoint. Death before commit
leaves no artifact rows, startup fails the interrupted job, and the completed blob remains visible
as an orphan for reviewed cleanup. Death after commit leaves a successful job and its artifact
intact, so startup does not produce a false interrupted-job report.
