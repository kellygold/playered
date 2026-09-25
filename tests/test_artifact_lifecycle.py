import json

import pytest

from image23mf import __version__
from image23mf.artifact_lifecycle import (
    ImmutableArtifactError,
    delete_regenerable_artifact,
    inspect_artifact,
)
from image23mf.contracts.job import JobConfig
from image23mf.contracts.jobs import JobType
from image23mf.storage import (
    ArtifactPublication,
    AssetRepository,
    ContentAddressedStore,
    JobRepository,
    ProjectRepository,
    RevisionPublisher,
    open_database,
)
from image23mf.storage.repositories import canonical_json, new_id


def config(asset_id: str) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": 80, "height_mm": 80},
            "palette": {
                "colors": [
                    {"id": "light", "name": "Light", "hex": "#CBC6B8"},
                    {"id": "dark", "name": "Dark", "hex": "#000000"},
                ]
            },
        }
    )


def seed(tmp_path):
    workspace = tmp_path / "workspace"
    store = ContentAddressedStore(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    project = ProjectRepository(connection).create("Artifact lifecycle")
    source_blob = store.put_bytes(
        b"source", namespace="assets", extension=".png", media_type="image/png"
    )
    source = AssetRepository(connection, store).register(
        source_blob, original_filename="source.png", width_px=8, height_px=8
    )
    shared = store.put_bytes(
        b"shared-preview",
        namespace="artifacts",
        extension=".png",
        media_type="image/png",
    )
    published = RevisionPublisher(connection, store).publish(
        project_id=project.id,
        source_asset_id=source.id,
        config=config(source.id),
        engine_version=__version__,
        artifacts=(
            ArtifactPublication(
                kind="palette-preview-image", derivation_key="revision", blob=shared
            ),
        ),
    )
    job = JobRepository(connection).create(project_id=project.id, job_type=JobType.PREVIEW)
    job_artifact_id = new_id("artifact")
    connection.execute(
        """
        INSERT INTO artifacts(
            id, revision_id, job_id, kind, sha256, derivation_key, media_type,
            relative_path, byte_size, metadata_json
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_artifact_id,
            job.id,
            "palette-preview-image",
            shared.sha256,
            "job-preview",
            shared.media_type,
            shared.relative_path,
            shared.byte_size,
            canonical_json({"proof": True}),
        ),
    )
    connection.commit()
    return connection, store, project, published.artifacts[0], job_artifact_id


def test_revision_artifacts_are_verified_and_immutable(tmp_path) -> None:
    connection, store, project, revision_artifact, _job_artifact_id = seed(tmp_path)
    try:
        inspection = inspect_artifact(
            connection=connection,
            store=store,
            project_id=project.id,
            artifact_id=revision_artifact.id,
        )
        assert inspection.integrity == "verified"
        assert inspection.immutable is True
        assert inspection.regenerable is False
        with pytest.raises(ImmutableArtifactError, match="immutable history"):
            delete_regenerable_artifact(
                connection=connection,
                store=store,
                project_id=project.id,
                artifact_id=revision_artifact.id,
            )
        assert store.path_for(revision_artifact.relative_path).is_file()
    finally:
        connection.close()


def test_regenerable_job_artifact_delete_retains_shared_revision_blob(tmp_path) -> None:
    connection, store, project, revision_artifact, job_artifact_id = seed(tmp_path)
    try:
        inspection = inspect_artifact(
            connection=connection,
            store=store,
            project_id=project.id,
            artifact_id=job_artifact_id,
        )
        assert inspection.regenerable is True
        assert inspection.recovery_action == "Generate a new preview to recreate this artifact."

        result = delete_regenerable_artifact(
            connection=connection,
            store=store,
            project_id=project.id,
            artifact_id=job_artifact_id,
        )
        assert result.deleted_record is True
        assert result.deleted_blob is False
        assert result.shared_blob_retained is True
        assert store.path_for(revision_artifact.relative_path).is_file()
        retained = connection.execute(
            "SELECT metadata_json FROM artifacts WHERE id = ?", (revision_artifact.id,)
        ).fetchone()
        assert retained is not None
        assert json.loads(retained["metadata_json"]) == {}
    finally:
        connection.close()
