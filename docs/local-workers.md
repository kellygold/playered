# Local worker execution

Linear issue: `24K-24`

CPU-heavy processing and external tools run on bounded local worker threads, never in FastAPI
request handlers. Each worker opens its own SQLite connection, receives a cooperative
`CancellationToken`, reports monotonic durable progress, and returns content-addressed artifacts
for controlled publication.

## Publication lease

Preview-like work provides a `supersession_key` such as `preview:{project_id}`. Enqueueing creates a
new monotonic generation, atomically moves the `job_heads` pointer, and terminalizes the prior
queued/running generation as `superseded`. The old cancellation token is then signaled.

Workers cannot publish directly. Completion verifies every blob and opens one transaction that:

1. confirms the job is still running;
2. confirms its ID and generation still own the supersession head;
3. inserts all artifact records;
4. marks the job successful.

Consequently an uncooperative slow native call may finish after a newer preview, but its result
cannot become visible. Its unreferenced content blob is safe for later garbage collection.

## Cancellation and crashes

The cancel API both persists the canceled terminal state and signals a live token. Cooperative CPU
work calls `check_canceled()`; `ToolRunner` consumes the same token to terminate external process
groups. Whichever of completion or cancellation acquires the database writer first wins atomically.

Unhandled task exceptions are logged locally and become sanitized `worker_crash` failures without
the exception message crossing the API. Artifact publication failure rolls back all artifact rows
and also becomes a failed job.

## Restart policy

Task callables are intentionally not serialized. At application startup, any queued or running job
from the previous process is marked failed with retryable code `worker_restarted`. The UI can offer
retry without pretending lost work succeeded or leaving a job permanently running. Successful and
other terminal jobs are untouched.
