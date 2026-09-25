"""Durable, non-destructive startup reconciliation evidence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from image23mf.contracts.jobs import JobStage, JobState, JobType
from image23mf.storage import ContentAddressedStore, JobRepository
from image23mf.storage.repositories import canonical_json, immediate_transaction, new_id
from image23mf.workspace_health import WorkspaceHealthService, WorkspaceStatus


class RecoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReconciliationStatus(str, Enum):
    HEALTHY = "healthy"
    ATTENTION = "attention"
    CRITICAL = "critical"


class RecoveryDisposition(str, Enum):
    RETRY_REQUIRED = "retry_required"


class InterruptedJobEvidence(RecoveryModel):
    job_id: str
    project_id: str
    type: JobType
    state_before_restart: JobState
    stage_before_restart: JobStage
    state_after_recovery: JobState
    disposition: RecoveryDisposition = RecoveryDisposition.RETRY_REQUIRED
    resumable: bool = False
    retryable: bool = True
    action: str


class StartupReconciliationReport(RecoveryModel):
    id: str
    status: ReconciliationStatus
    started_at: str
    completed_at: str
    interrupted_jobs: tuple[InterruptedJobEvidence, ...]
    stale_temp_file_count: int = Field(ge=0)
    orphan_file_count: int = Field(ge=0)
    database_integrity: bool
    foreign_key_integrity: bool
    automatic_deletions: int = Field(default=0, ge=0)
    recommended_actions: tuple[str, ...]


class ReconciliationHistory(RecoveryModel):
    items: tuple[StartupReconciliationReport, ...]


class StartupRecoveryService:
    """Reconcile abandoned work without deleting bytes or claiming it resumed."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        blob_store: ContentAddressedStore,
    ) -> None:
        self.connection = connection
        self.blob_store = blob_store

    def reconcile(self) -> StartupReconciliationReport:
        started_at = _utc_now()
        # Age zero intentionally inventories every surviving temp file. Reconciliation is
        # evidence-only: cleanup still requires a separately reviewed GC fingerprint.
        health = WorkspaceHealthService(self.connection, self.blob_store).inspect(
            stale_temp_age_seconds=0
        )
        with immediate_transaction(self.connection):
            rows = self.connection.execute(
                """
                SELECT id, project_id, job_type, state, stage
                FROM jobs
                WHERE state IN ('queued', 'running')
                ORDER BY created_at, id
                """
            ).fetchall()
            recovered_ids = set(
                JobRepository(self.connection).recover_interrupted_in_current_transaction()
            )
            interrupted = tuple(
                InterruptedJobEvidence(
                    job_id=row["id"],
                    project_id=row["project_id"],
                    type=JobType(row["job_type"]),
                    state_before_restart=JobState(row["state"]),
                    stage_before_restart=JobStage(row["stage"]),
                    state_after_recovery=JobState.FAILED,
                    action=_retry_action(JobType(row["job_type"])),
                )
                for row in rows
                if row["id"] in recovered_ids
            )
            status = _status_for(
                health.status,
                interrupted_count=len(interrupted),
                stale_temp_file_count=health.stale_temp_file_count,
                orphan_file_count=health.orphan_file_count,
            )
            actions = []
            if interrupted:
                actions.append(
                    "Review the interrupted jobs and retry them from the current project state."
                )
            if health.stale_temp_file_count or health.orphan_file_count:
                actions.append(
                    "Review a workspace cleanup dry run; no temporary or orphaned bytes "
                    "were deleted."
                )
            if not health.database_integrity or not health.foreign_key_integrity:
                actions.append(
                    "Stop editing and preserve the workspace before restoring a verified backup."
                )
            if not actions:
                actions.append("No recovery action is required.")
            report = StartupReconciliationReport(
                id=new_id("reconciliation"),
                status=status,
                started_at=started_at,
                completed_at=_utc_now(),
                interrupted_jobs=interrupted,
                stale_temp_file_count=health.stale_temp_file_count,
                orphan_file_count=health.orphan_file_count,
                database_integrity=health.database_integrity,
                foreign_key_integrity=health.foreign_key_integrity,
                automatic_deletions=0,
                recommended_actions=tuple(actions),
            )
            self.connection.execute(
                """
                INSERT INTO startup_reconciliations(
                    id, status, started_at, completed_at, interrupted_job_count,
                    stale_temp_file_count, orphan_file_count, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.id,
                    report.status.value,
                    report.started_at,
                    report.completed_at,
                    len(report.interrupted_jobs),
                    report.stale_temp_file_count,
                    report.orphan_file_count,
                    canonical_json(report.model_dump(mode="json")),
                ),
            )
        return report

    def latest(self) -> StartupReconciliationReport | None:
        row = self.connection.execute(
            """
            SELECT report_json FROM startup_reconciliations
            ORDER BY completed_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
        return (
            None
            if row is None
            else StartupReconciliationReport.model_validate_json(row["report_json"])
        )

    def history(self, *, limit: int = 20) -> ReconciliationHistory:
        if not 1 <= limit <= 100:
            raise ValueError("reconciliation history limit must be between 1 and 100")
        rows = self.connection.execute(
            """
            SELECT report_json FROM startup_reconciliations
            ORDER BY completed_at DESC, id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return ReconciliationHistory(
            items=tuple(
                StartupReconciliationReport.model_validate_json(row["report_json"]) for row in rows
            )
        )


def _retry_action(job_type: JobType) -> str:
    return {
        JobType.PREVIEW: "Render the preview again from the recovered draft.",
        JobType.GEOMETRY: "Regenerate geometry from a current successful preview.",
        JobType.EXPORT: "Start a new export from verified geometry evidence.",
        JobType.VALIDATION: "Run slicer validation again from the saved export.",
    }[job_type]


def _status_for(
    workspace_status: WorkspaceStatus,
    *,
    interrupted_count: int,
    stale_temp_file_count: int,
    orphan_file_count: int,
) -> ReconciliationStatus:
    if workspace_status == WorkspaceStatus.CRITICAL:
        return ReconciliationStatus.CRITICAL
    if interrupted_count or stale_temp_file_count or orphan_file_count:
        return ReconciliationStatus.ATTENTION
    return ReconciliationStatus.HEALTHY


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
