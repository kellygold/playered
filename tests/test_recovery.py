import sqlite3

import pytest

from image23mf.contracts.jobs import JobStage, JobState, JobType
from image23mf.recovery import ReconciliationStatus, StartupRecoveryService
from image23mf.storage import (
    ContentAddressedStore,
    JobRepository,
    ProjectRepository,
    open_database,
)


def test_startup_reconciliation_fails_interrupted_jobs_and_preserves_all_bytes(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    store = ContentAddressedStore(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Interrupted work")
        jobs = JobRepository(connection)
        queued = jobs.create(project_id=project.id, job_type=JobType.PREVIEW)
        running = jobs.create(project_id=project.id, job_type=JobType.EXPORT)
        jobs.start(running.id, stage=JobStage.PACKAGING)
        orphan = store.put_bytes(
            b"completed-before-db-publication",
            namespace="artifacts",
            extension=".bin",
            media_type="application/octet-stream",
        )
        temp = workspace / "temp" / "interrupted.tmp"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(b"partial")

        report = StartupRecoveryService(connection, store).reconcile()

        assert report.status == ReconciliationStatus.ATTENTION
        assert report.automatic_deletions == 0
        assert report.orphan_file_count == 1
        assert report.stale_temp_file_count == 1
        assert {item.job_id for item in report.interrupted_jobs} == {queued.id, running.id}
        evidence = {item.job_id: item for item in report.interrupted_jobs}
        assert evidence[queued.id].state_before_restart == JobState.QUEUED
        assert evidence[running.id].stage_before_restart == JobStage.PACKAGING
        assert all(item.state_after_recovery == JobState.FAILED for item in evidence.values())
        assert all(item.retryable and not item.resumable for item in evidence.values())
        assert (
            store.path_for(orphan.relative_path).read_bytes() == b"completed-before-db-publication"
        )
        assert temp.read_bytes() == b"partial"
        assert StartupRecoveryService(connection, store).latest() == report
    finally:
        connection.close()


def test_reconciliation_history_is_durable_ordered_and_immutable(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    store = ContentAddressedStore(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        service = StartupRecoveryService(connection, store)
        first = service.reconcile()
        second = service.reconcile()

        history = service.history()
        assert [item.id for item in history.items[:2]] == [second.id, first.id]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE startup_reconciliations SET status = 'attention' WHERE id = ?",
                (first.id,),
            )
        connection.rollback()
    finally:
        connection.close()


def test_reconciliation_history_limit_is_bounded(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        service = StartupRecoveryService(connection, ContentAddressedStore(workspace))
        with pytest.raises(ValueError, match="between 1 and 100"):
            service.history(limit=0)
    finally:
        connection.close()


def test_reconciliation_report_failure_rolls_back_interrupted_job_transition(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Atomic reconciliation")
        jobs = JobRepository(connection)
        job = jobs.create(project_id=project.id, job_type=JobType.GEOMETRY)
        jobs.start(job.id, stage=JobStage.MESHING)
        connection.execute(
            """
            CREATE TRIGGER reject_reconciliation_test
            BEFORE INSERT ON startup_reconciliations
            BEGIN
                SELECT RAISE(ABORT, 'forced reconciliation write failure');
            END
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="forced reconciliation write failure"):
            StartupRecoveryService(connection, ContentAddressedStore(workspace)).reconcile()

        assert jobs.get(job.id).state == JobState.RUNNING
        assert StartupRecoveryService(connection, ContentAddressedStore(workspace)).latest() is None
    finally:
        connection.close()
