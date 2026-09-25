"""Durable job resources with deterministic state transitions."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from image23mf.contracts.jobs import (
    TERMINAL_JOB_STATES,
    TERMINAL_STAGE,
    InvalidJobTransitionError,
    JobFailure,
    JobResource,
    JobStage,
    JobState,
    JobType,
    assert_job_transition,
)
from image23mf.storage.repositories import (
    RecordNotFoundError,
    canonical_json,
    immediate_transaction,
    new_id,
)


@dataclass(frozen=True)
class EnqueuedJob:
    job: JobResource
    superseded_job_id: Optional[str] = None


class JobRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(
        self,
        *,
        project_id: str,
        job_type: JobType,
        revision_id: Optional[str] = None,
        request_key: Optional[str] = None,
        supersession_key: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> JobResource:
        return self.enqueue(
            project_id=project_id,
            job_type=job_type,
            revision_id=revision_id,
            request_key=request_key,
            supersession_key=supersession_key,
            job_id=job_id,
        ).job

    def enqueue(
        self,
        *,
        project_id: str,
        job_type: JobType,
        revision_id: Optional[str] = None,
        request_key: Optional[str] = None,
        supersession_key: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> EnqueuedJob:
        identifier = job_id or new_id("job")
        normalized_supersession = supersession_key.strip() if supersession_key else None
        if supersession_key is not None and not normalized_supersession:
            raise ValueError("supersession_key cannot be blank")
        superseded_job_id = None
        with immediate_transaction(self.connection):
            if revision_id is not None:
                revision = self.connection.execute(
                    "SELECT project_id FROM revisions WHERE id = ?", (revision_id,)
                ).fetchone()
                if revision is None:
                    raise RecordNotFoundError(f"revision not found: {revision_id}")
                if revision["project_id"] != project_id:
                    raise ValueError("job revision must belong to its project")
            generation = 0
            if normalized_supersession is not None:
                head = self.connection.execute(
                    """
                    SELECT job_id, generation FROM job_heads
                    WHERE project_id = ? AND supersession_key = ?
                    """,
                    (project_id, normalized_supersession),
                ).fetchone()
                if head is not None:
                    generation = int(head["generation"]) + 1
                    changed = self.connection.execute(
                        """
                        UPDATE jobs
                        SET state = 'superseded', stage = 'superseded',
                            finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                            canceled_at = NULL, error_code = NULL, error_message = NULL,
                            error_details_json = NULL
                        WHERE id = ? AND state IN ('queued', 'running')
                        """,
                        (head["job_id"],),
                    ).rowcount
                    if changed == 1:
                        superseded_job_id = head["job_id"]
                else:
                    generation = 1
            self.connection.execute(
                """
                INSERT INTO jobs(
                    id, project_id, revision_id, job_type, state, stage, progress, request_key,
                    supersession_key, generation
                ) VALUES (?, ?, ?, ?, 'queued', 'queued', 0, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    revision_id,
                    job_type.value,
                    request_key,
                    normalized_supersession,
                    generation,
                ),
            )
            if normalized_supersession is not None:
                self.connection.execute(
                    """
                    INSERT INTO job_heads(project_id, supersession_key, job_id, generation)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_id, supersession_key) DO UPDATE SET
                        job_id = excluded.job_id,
                        generation = excluded.generation,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (project_id, normalized_supersession, identifier, generation),
                )
        return EnqueuedJob(job=self.get(identifier), superseded_job_id=superseded_job_id)

    def get(self, job_id: str) -> JobResource:
        row = self.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError(f"job not found: {job_id}")
        artifact_ids = tuple(
            item["id"]
            for item in self.connection.execute(
                "SELECT id FROM artifacts WHERE job_id = ? ORDER BY kind, created_at, id", (job_id,)
            )
        )
        return _job_from_row(row, artifact_ids)

    def start(self, job_id: str, *, stage: JobStage) -> JobResource:
        if stage in {
            JobStage.QUEUED,
            JobStage.COMPLETE,
            JobStage.FAILED,
            JobStage.CANCELED,
            JobStage.SUPERSEDED,
        }:
            raise ValueError("running jobs require a non-terminal processing stage")
        return self._transition(job_id, JobState.RUNNING, stage=stage)

    def report_progress(self, job_id: str, *, stage: JobStage, progress: float) -> JobResource:
        if not 0 <= progress < 1:
            raise ValueError("running job progress must be between 0 and 1")
        if stage in {
            JobStage.QUEUED,
            JobStage.COMPLETE,
            JobStage.FAILED,
            JobStage.CANCELED,
            JobStage.SUPERSEDED,
        }:
            raise ValueError("running jobs require a non-terminal processing stage")
        with immediate_transaction(self.connection):
            current = self.get(job_id)
            if current.state != JobState.RUNNING:
                raise InvalidJobTransitionError(
                    f"cannot report progress for job in {current.state} state"
                )
            if progress < current.progress:
                raise InvalidJobTransitionError("job progress cannot move backwards")
            self.connection.execute(
                "UPDATE jobs SET stage = ?, progress = ? WHERE id = ?",
                (stage.value, progress, job_id),
            )
        return self.get(job_id)

    def succeed(self, job_id: str) -> JobResource:
        return self._transition(job_id, JobState.SUCCEEDED, stage=JobStage.COMPLETE)

    def fail(self, job_id: str, failure: JobFailure) -> JobResource:
        return self._transition(
            job_id,
            JobState.FAILED,
            stage=JobStage.FAILED,
            failure=failure,
        )

    def cancel(self, job_id: str) -> JobResource:
        current = self.get(job_id)
        if current.state == JobState.CANCELED:
            return current
        return self._transition(job_id, JobState.CANCELED, stage=JobStage.CANCELED)

    def supersede(self, job_id: str) -> JobResource:
        current = self.get(job_id)
        if current.state == JobState.SUPERSEDED:
            return current
        return self._transition(job_id, JobState.SUPERSEDED, stage=JobStage.SUPERSEDED)

    def is_current(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job.supersession_key is None:
            return True
        row = self.connection.execute(
            """
            SELECT job_id, generation FROM job_heads
            WHERE project_id = ? AND supersession_key = ?
            """,
            (job.project_id, job.supersession_key),
        ).fetchone()
        return row is not None and row["job_id"] == job.id and row["generation"] == job.generation

    def current_head(self, *, project_id: str, supersession_key: str) -> Optional[JobResource]:
        row = self.connection.execute(
            """
            SELECT job_id FROM job_heads
            WHERE project_id = ? AND supersession_key = ?
            """,
            (project_id, supersession_key),
        ).fetchone()
        return None if row is None else self.get(row["job_id"])

    def recover_interrupted(self) -> tuple[JobResource, ...]:
        with immediate_transaction(self.connection):
            recovered_ids = self.recover_interrupted_in_current_transaction()
        return tuple(self.get(job_id) for job_id in recovered_ids)

    def recover_interrupted_in_current_transaction(self) -> tuple[str, ...]:
        """Fail abandoned jobs inside a caller-owned reconciliation transaction."""

        if not self.connection.in_transaction:
            raise RuntimeError("interrupted-job recovery requires an active transaction")
        recovered_ids = []
        details = canonical_json(
            {
                "retryable": True,
                "details": {"reason": "The local worker process restarted."},
            }
        )
        recovered_ids = [
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM jobs WHERE state IN ('queued', 'running') ORDER BY created_at"
            )
        ]
        if recovered_ids:
            placeholders = ",".join("?" for _ in recovered_ids)
            self.connection.execute(
                f"""
                UPDATE jobs
                SET state = 'failed', stage = 'failed',
                    error_code = 'worker_restarted',
                    error_message = 'The local worker restarted before this job finished.',
                    error_details_json = ?,
                    finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    canceled_at = NULL
                WHERE id IN ({placeholders})
                """,  # noqa: S608
                (details, *recovered_ids),
            )
        return tuple(recovered_ids)

    def _transition(
        self,
        job_id: str,
        target: JobState,
        *,
        stage: JobStage,
        failure: Optional[JobFailure] = None,
    ) -> JobResource:
        with immediate_transaction(self.connection):
            current = self.get(job_id)
            if current.state == target:
                return current
            assert_job_transition(current.state, target)
            if target == JobState.FAILED and failure is None:
                raise ValueError("failed transition requires failure details")
            if target != JobState.FAILED and failure is not None:
                raise ValueError("failure details are only valid for failed jobs")

            is_terminal = target in TERMINAL_JOB_STATES
            progress = 1.0 if target == JobState.SUCCEEDED else current.progress
            self.connection.execute(
                """
                UPDATE jobs
                SET state = ?, stage = ?, progress = ?,
                    error_code = ?, error_message = ?, error_details_json = ?,
                    started_at = CASE
                        WHEN ? = 'running' THEN COALESCE(
                            started_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        )
                        ELSE started_at
                    END,
                    finished_at = CASE
                        WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ELSE NULL
                    END,
                    canceled_at = CASE
                        WHEN ? = 'canceled' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ELSE NULL
                    END
                WHERE id = ? AND state = ?
                """,
                (
                    target.value,
                    stage.value,
                    progress,
                    failure.code if failure else None,
                    failure.message if failure else None,
                    canonical_json(
                        {
                            "retryable": failure.retryable,
                            "details": failure.details,
                        }
                    )
                    if failure
                    else None,
                    target.value,
                    is_terminal,
                    target.value,
                    job_id,
                    current.state.value,
                ),
            )
        return self.get(job_id)


def _job_from_row(row: sqlite3.Row, artifact_ids: tuple[str, ...]) -> JobResource:
    state = JobState(row["state"])
    failure = None
    if state == JobState.FAILED:
        stored = json.loads(row["error_details_json"] or "{}")
        failure = JobFailure(
            code=row["error_code"] or "unknown_job_failure",
            message=row["error_message"] or "The job failed without a message.",
            retryable=bool(stored.get("retryable", False)),
            details=stored.get("details", {}),
        )
    stage = JobStage(row["stage"])
    if state in TERMINAL_JOB_STATES and stage != TERMINAL_STAGE[state]:
        raise RuntimeError("stored terminal job has an inconsistent stage")
    return JobResource(
        id=row["id"],
        project_id=row["project_id"],
        revision_id=row["revision_id"],
        type=JobType(row["job_type"]),
        state=state,
        stage=stage,
        progress=row["progress"],
        request_key=row["request_key"],
        supersession_key=row["supersession_key"],
        generation=row["generation"],
        failure=failure,
        artifact_ids=artifact_ids,
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        canceled_at=row["canceled_at"],
    )
