import sqlite3

import pytest

from image23mf.contracts.jobs import (
    InvalidJobTransitionError,
    JobFailure,
    JobStage,
    JobState,
    JobType,
)
from image23mf.storage import JobRepository, ProjectRepository, open_database


def test_running_job_progress_is_monotonic_and_success_is_terminal(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Preview")
        jobs = JobRepository(connection)
        queued = jobs.create(
            project_id=project.id,
            job_type=JobType.PREVIEW,
            request_key="preview:config-hash",
        )
        running = jobs.start(queued.id, stage=JobStage.QUANTIZING)
        progressed = jobs.report_progress(queued.id, stage=JobStage.ANALYZING, progress=0.65)
        succeeded = jobs.succeed(queued.id)

        assert queued.state == JobState.QUEUED
        assert running.started_at is not None
        assert progressed.progress == 0.65
        assert succeeded.state == JobState.SUCCEEDED
        assert succeeded.stage == JobStage.COMPLETE
        assert succeeded.progress == 1
        assert succeeded.finished_at is not None
        with pytest.raises(InvalidJobTransitionError, match="cannot transition"):
            jobs.cancel(queued.id)
        assert jobs.get(queued.id) == succeeded
    finally:
        connection.close()


def test_progress_cannot_move_backwards_or_arrive_after_terminal_state(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Progress")
        jobs = JobRepository(connection)
        job = jobs.create(project_id=project.id, job_type=JobType.GEOMETRY)
        jobs.start(job.id, stage=JobStage.VECTORIZING)
        jobs.report_progress(job.id, stage=JobStage.MESHING, progress=0.8)

        with pytest.raises(InvalidJobTransitionError, match="move backwards"):
            jobs.report_progress(job.id, stage=JobStage.MESHING, progress=0.7)
        canceled = jobs.cancel(job.id)
        with pytest.raises(InvalidJobTransitionError, match="cannot report progress"):
            jobs.report_progress(job.id, stage=JobStage.MESHING, progress=0.9)
        assert jobs.cancel(job.id) == canceled
        assert canceled.canceled_at is not None
        assert canceled.finished_at is not None
    finally:
        connection.close()


def test_failed_and_superseded_jobs_have_deterministic_terminal_resources(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Terminal states")
        jobs = JobRepository(connection)
        failed_job = jobs.create(project_id=project.id, job_type=JobType.EXPORT)
        jobs.start(failed_job.id, stage=JobStage.PACKAGING)
        failure = JobFailure(
            code="package_invalid",
            message="The generated archive failed validation.",
            retryable=False,
            details={"member": "3D/3dmodel.model"},
        )
        failed = jobs.fail(failed_job.id, failure)

        stale_job = jobs.create(project_id=project.id, job_type=JobType.PREVIEW)
        superseded = jobs.supersede(stale_job.id)

        assert failed.state == JobState.FAILED
        assert failed.stage == JobStage.FAILED
        assert failed.failure == failure
        assert failed.finished_at is not None
        assert superseded.state == JobState.SUPERSEDED
        assert superseded.stage == JobStage.SUPERSEDED
        assert jobs.supersede(stale_job.id) == superseded
    finally:
        connection.close()


def test_database_rejects_invalid_job_contract_outside_repository(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Database contract")
        with pytest.raises(sqlite3.IntegrityError, match="unsupported job type"):
            connection.execute(
                """
                INSERT INTO jobs(id, project_id, job_type, state, stage, progress)
                VALUES ('bad', ?, 'teleport', 'queued', 'queued', 0)
                """,
                (project.id,),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="inconsistent"):
            connection.execute(
                """
                INSERT INTO jobs(id, project_id, job_type, state, stage, progress)
                VALUES ('bad', ?, 'preview', 'succeeded', 'complete', 0.5)
                """,
                (project.id,),
            )
        connection.rollback()
    finally:
        connection.close()


def test_new_superseding_job_atomically_replaces_head_and_terminalizes_previous(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Latest preview wins")
        jobs = JobRepository(connection)
        first = jobs.enqueue(
            project_id=project.id,
            job_type=JobType.PREVIEW,
            request_key="config:first",
            supersession_key=f"preview:{project.id}",
        )
        jobs.start(first.job.id, stage=JobStage.QUANTIZING)
        second = jobs.enqueue(
            project_id=project.id,
            job_type=JobType.PREVIEW,
            request_key="config:second",
            supersession_key=f"preview:{project.id}",
        )

        assert second.superseded_job_id == first.job.id
        assert jobs.get(first.job.id).state == JobState.SUPERSEDED
        assert jobs.get(first.job.id).finished_at is not None
        assert second.job.generation == 2
        assert jobs.is_current(second.job.id)
        assert not jobs.is_current(first.job.id)
    finally:
        connection.close()


def test_restart_recovery_fails_unreconstructable_queued_and_running_jobs(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Restart")
        jobs = JobRepository(connection)
        queued = jobs.create(project_id=project.id, job_type=JobType.PREVIEW)
        running = jobs.create(project_id=project.id, job_type=JobType.GEOMETRY)
        jobs.start(running.id, stage=JobStage.MESHING)
        complete = jobs.create(project_id=project.id, job_type=JobType.EXPORT)
        jobs.start(complete.id, stage=JobStage.PACKAGING)
        jobs.succeed(complete.id)

        recovered = jobs.recover_interrupted()

        assert {item.id for item in recovered} == {queued.id, running.id}
        assert all(item.state == JobState.FAILED for item in recovered)
        assert all(item.failure and item.failure.code == "worker_restarted" for item in recovered)
        assert all(item.failure and item.failure.retryable for item in recovered)
        assert jobs.get(complete.id).state == JobState.SUCCEEDED
    finally:
        connection.close()
