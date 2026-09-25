import multiprocessing
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from image23mf.contracts.jobs import JobStage, JobState, JobType
from image23mf.recovery import StartupRecoveryService
from image23mf.storage import (
    ArtifactRepository,
    ContentAddressedStore,
    JobRepository,
    ProjectRepository,
    StoredBlob,
    open_database,
)
from image23mf.workers import (
    LocalWorkerManager,
    PublicationCheckpoint,
    WorkerArtifact,
    WorkerResult,
)

FORCED_DEATH_EXIT_CODE = 86


def _die_during_publication(
    workspace_value: str,
    job_id: str,
    checkpoint_value: str,
    blob_payload: dict[str, object],
) -> None:
    workspace = Path(workspace_value)
    target = PublicationCheckpoint(checkpoint_value)

    def hook(checkpoint: PublicationCheckpoint, _job_id: str) -> None:
        if checkpoint == target:
            os._exit(FORCED_DEATH_EXIT_CODE)

    manager = LocalWorkerManager(
        database_path=workspace / "image23mf.sqlite3",
        blob_store=ContentAddressedStore(workspace),
        publication_hook=hook,
    )
    blob = StoredBlob(**blob_payload)
    manager._publish_current_result(  # noqa: SLF001 - process-death contract seam
        job_id,
        WorkerResult(
            artifacts=(
                WorkerArtifact(
                    kind="forced-death-proof",
                    derivation_key=f"forced-death:{job_id}",
                    blob=blob,
                ),
            )
        ),
    )
    os._exit(0)


@pytest.mark.parametrize(
    ("checkpoint", "committed"),
    (
        (PublicationCheckpoint.BLOBS_VERIFIED, False),
        (PublicationCheckpoint.TRANSACTION_OPENED, False),
        (PublicationCheckpoint.ARTIFACT_ROWS_WRITTEN, False),
        (PublicationCheckpoint.DATABASE_COMMITTED, True),
    ),
)
def test_forced_process_death_has_unambiguous_atomic_recovery(
    tmp_path, checkpoint: PublicationCheckpoint, committed: bool
) -> None:
    workspace = tmp_path / checkpoint.value
    store = ContentAddressedStore(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create(f"Death at {checkpoint.value}")
        jobs = JobRepository(connection)
        job = jobs.create(project_id=project.id, job_type=JobType.EXPORT)
        jobs.start(job.id, stage=JobStage.PACKAGING)
        blob = store.put_bytes(
            checkpoint.value.encode(),
            namespace="artifacts",
            extension=".3mf",
            media_type="model/3mf",
        )
    finally:
        connection.close()

    process = multiprocessing.get_context("spawn").Process(
        target=_die_during_publication,
        args=(
            str(workspace),
            job.id,
            checkpoint.value,
            asdict(blob),
        ),
    )
    process.start()
    process.join(timeout=10)
    assert not process.is_alive()
    assert process.exitcode == FORCED_DEATH_EXIT_CODE

    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        report = StartupRecoveryService(connection, store).reconcile()
        recovered = JobRepository(connection).get(job.id)
        artifacts = ArtifactRepository(connection).list_for_job(job.id)
        assert store.path_for(blob.relative_path).is_file()
        if committed:
            assert recovered.state == JobState.SUCCEEDED
            assert len(artifacts) == 1
            assert report.interrupted_jobs == ()
            assert report.orphan_file_count == 0
        else:
            assert recovered.state == JobState.FAILED
            assert recovered.failure is not None
            assert recovered.failure.code == "worker_restarted"
            assert artifacts == ()
            assert [item.job_id for item in report.interrupted_jobs] == [job.id]
            assert report.orphan_file_count == 1
    finally:
        connection.close()
