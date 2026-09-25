import threading
import time

from image23mf.contracts.jobs import JobFailure, JobStage, JobState, JobType
from image23mf.storage import (
    ArtifactRepository,
    ContentAddressedStore,
    ProjectRepository,
    open_database,
)
from image23mf.workers import (
    LocalWorkerManager,
    WorkerArtifact,
    WorkerJobError,
    WorkerResult,
)


def manager_fixture(tmp_path, *, max_workers: int = 2):
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    connection = open_database(database_path)
    try:
        project = ProjectRepository(connection).create("Worker proof")
    finally:
        connection.close()
    manager = LocalWorkerManager(
        database_path=database_path,
        blob_store=ContentAddressedStore(workspace),
        max_workers=max_workers,
    )
    return manager, project, database_path


def artifact_result(context, *, key: str, payload: bytes) -> WorkerResult:
    blob = context.blob_store.put_bytes(
        payload,
        namespace="artifacts",
        extension=".bin",
        media_type="application/octet-stream",
    )
    return WorkerResult(
        artifacts=(
            WorkerArtifact(
                kind="fixture-result",
                derivation_key=key,
                blob=blob,
                metadata={"key": key},
            ),
        )
    )


def test_worker_runs_off_request_thread_reports_progress_and_publishes_atomically(tmp_path) -> None:
    manager, project, database_path = manager_fixture(tmp_path)
    request_thread = threading.get_ident()
    worker_thread = []

    def work(context):
        worker_thread.append(threading.get_ident())
        context.report(stage=JobStage.ANALYZING, progress=0.5)
        return artifact_result(context, key="success", payload=b"complete")

    try:
        queued = manager.submit(
            project_id=project.id,
            job_type=JobType.PREVIEW,
            initial_stage=JobStage.QUANTIZING,
            work=work,
        )
        result = manager.wait_for_terminal(queued.id)

        assert result.state == JobState.SUCCEEDED
        assert result.progress == 1
        assert worker_thread and worker_thread[0] != request_thread
        assert len(result.artifact_ids) == 1
        connection = open_database(database_path)
        try:
            artifact = ArtifactRepository(connection).get(result.artifact_ids[0])
            assert artifact.job_id == result.id
            assert artifact.metadata == {"key": "success"}
        finally:
            connection.close()
    finally:
        manager.shutdown()


def test_cancel_signal_stops_cooperative_work_and_publishes_no_artifacts(tmp_path) -> None:
    manager, project, _ = manager_fixture(tmp_path)
    started = threading.Event()

    def work(context):
        started.set()
        while True:
            context.check_canceled()
            time.sleep(0.01)

    try:
        queued = manager.submit(
            project_id=project.id,
            job_type=JobType.GEOMETRY,
            initial_stage=JobStage.VECTORIZING,
            work=work,
        )
        assert started.wait(2)
        canceled = manager.cancel(queued.id)
        manager.wait_until_idle(queued.id)

        assert canceled.state == JobState.CANCELED
        assert manager.get(queued.id).state == JobState.CANCELED
        assert manager.get(queued.id).artifact_ids == ()
    finally:
        manager.shutdown()


def test_worker_crash_becomes_sanitized_retryable_failure(tmp_path) -> None:
    manager, project, _ = manager_fixture(tmp_path)

    def work(_context):
        raise RuntimeError("secret implementation detail")

    try:
        queued = manager.submit(
            project_id=project.id,
            job_type=JobType.EXPORT,
            initial_stage=JobStage.PACKAGING,
            work=work,
        )
        failed = manager.wait_for_terminal(queued.id)

        assert failed.state == JobState.FAILED
        assert failed.failure is not None
        assert failed.failure.code == "worker_crash"
        assert failed.failure.retryable
        assert "secret implementation detail" not in failed.failure.message
        assert failed.failure.details == {"exception_type": "RuntimeError"}
    finally:
        manager.shutdown()


def test_actionable_worker_failure_is_preserved_without_becoming_a_crash(tmp_path) -> None:
    manager, project, _ = manager_fixture(tmp_path)

    def work(_context):
        raise WorkerJobError(
            JobFailure(
                code="slicer_profile_error",
                message="The pinned process profile no longer matches the installed version.",
                retryable=False,
                details={"action": "Select a supported P2S profile."},
            )
        )

    try:
        queued = manager.submit(
            project_id=project.id,
            job_type=JobType.EXPORT,
            initial_stage=JobStage.PACKAGING,
            work=work,
        )
        failed = manager.wait_for_terminal(queued.id)

        assert failed.state == JobState.FAILED
        assert failed.failure is not None
        assert failed.failure.code == "slicer_profile_error"
        assert failed.failure.retryable is False
        assert failed.failure.details == {"action": "Select a supported P2S profile."}
    finally:
        manager.shutdown()


def test_two_jobs_execute_concurrently_without_sharing_connections_or_results(tmp_path) -> None:
    manager, project, _ = manager_fixture(tmp_path, max_workers=2)
    barrier = threading.Barrier(3)

    def work_for(key: str):
        def work(context):
            barrier.wait(timeout=2)
            return artifact_result(context, key=key, payload=key.encode())

        return work

    try:
        first = manager.submit(
            project_id=project.id,
            job_type=JobType.PREVIEW,
            initial_stage=JobStage.ANALYZING,
            work=work_for("first"),
        )
        second = manager.submit(
            project_id=project.id,
            job_type=JobType.GEOMETRY,
            initial_stage=JobStage.MESHING,
            work=work_for("second"),
        )
        barrier.wait(timeout=2)

        first_result = manager.wait_for_terminal(first.id)
        second_result = manager.wait_for_terminal(second.id)

        assert first_result.state == JobState.SUCCEEDED
        assert second_result.state == JobState.SUCCEEDED
        assert set(first_result.artifact_ids).isdisjoint(second_result.artifact_ids)
    finally:
        manager.shutdown()


def test_slow_superseded_preview_cannot_publish_after_newer_result(tmp_path) -> None:
    manager, project, database_path = manager_fixture(tmp_path, max_workers=2)
    old_started = threading.Event()
    release_old = threading.Event()

    def old_work(context):
        old_started.set()
        release_old.wait(timeout=3)
        # Deliberately ignores cancellation to model an uncooperative native call.
        return artifact_result(context, key="old", payload=b"stale")

    def new_work(context):
        return artifact_result(context, key="new", payload=b"current")

    supersession_key = f"preview:{project.id}"
    try:
        old = manager.submit(
            project_id=project.id,
            job_type=JobType.PREVIEW,
            initial_stage=JobStage.QUANTIZING,
            request_key="config:old",
            supersession_key=supersession_key,
            work=old_work,
        )
        assert old_started.wait(2)
        new = manager.submit(
            project_id=project.id,
            job_type=JobType.PREVIEW,
            initial_stage=JobStage.QUANTIZING,
            request_key="config:new",
            supersession_key=supersession_key,
            work=new_work,
        )
        current = manager.wait_for_terminal(new.id)
        release_old.set()
        manager.wait_until_idle(old.id)

        assert manager.get(old.id).state == JobState.SUPERSEDED
        assert current.state == JobState.SUCCEEDED
        assert current.generation == old.generation + 1
        connection = open_database(database_path)
        try:
            assert ArtifactRepository(connection).list_for_revision("unused") == ()
            old_count = connection.execute(
                "SELECT count(*) FROM artifacts WHERE job_id = ?", (old.id,)
            ).fetchone()[0]
            new_count = connection.execute(
                "SELECT count(*) FROM artifacts WHERE job_id = ?", (new.id,)
            ).fetchone()[0]
            assert old_count == 0
            assert new_count == 1
        finally:
            connection.close()
    finally:
        release_old.set()
        manager.shutdown()


def test_restart_recovery_is_explicit_and_does_not_claim_lost_work_succeeded(tmp_path) -> None:
    manager, project, database_path = manager_fixture(tmp_path)
    connection = open_database(database_path)
    try:
        from image23mf.storage import JobRepository

        jobs = JobRepository(connection)
        queued = jobs.create(project_id=project.id, job_type=JobType.PREVIEW)
        running = jobs.create(project_id=project.id, job_type=JobType.VALIDATION)
        jobs.start(running.id, stage=JobStage.VALIDATING)
    finally:
        connection.close()

    try:
        recovered = manager.recover_interrupted()
        assert {item.id for item in recovered} == {queued.id, running.id}
        assert all(item.state == JobState.FAILED for item in recovered)
        assert all(item.failure and item.failure.code == "worker_restarted" for item in recovered)
    finally:
        manager.shutdown()
