import sqlite3

import pytest

from image23mf import __version__
from image23mf.contracts.job import JobConfig
from image23mf.storage import (
    ArtifactPublication,
    ArtifactRepository,
    AssetRepository,
    ContentAddressedStore,
    InvalidPublicationError,
    MissingBlobError,
    ProjectRepository,
    RevisionPublisher,
    RevisionRepository,
    open_database,
)


def job_config(asset_id: str) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": 200, "height_mm": 200},
            "palette": {
                "colors": [
                    {"id": "bone", "name": "Bone White", "hex": "#CBC6B8"},
                    {"id": "charcoal", "name": "Charcoal", "hex": "#000000"},
                ]
            },
        }
    )


def source_asset(connection, store: ContentAddressedStore):
    blob = store.put_bytes(
        b"valid-png-fixture", namespace="assets", extension=".png", media_type="image/png"
    )
    return AssetRepository(connection, store).register(
        blob,
        original_filename="wager.png",
        width_px=1200,
        height_px=800,
        metadata={"color_space": "sRGB"},
    )


def artifact(store: ContentAddressedStore, name: str, payload: bytes) -> ArtifactPublication:
    blob = store.put_bytes(
        payload,
        namespace="artifacts",
        extension=".png",
        media_type="image/png",
    )
    return ArtifactPublication(
        kind=name,
        derivation_key=f"fixture:{name}:{blob.sha256}",
        blob=blob,
        metadata={"proof": True},
    )


def test_typed_repositories_publish_revision_and_artifacts_as_one_result(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create(
            "The Wager", description="Six-panel mural", preferences={"units": "mm"}
        )
        source = source_asset(connection, store)
        preview = artifact(store, "processed-preview", b"processed labels")
        mask = artifact(store, "label-field", b"one-label-per-pixel")

        published = RevisionPublisher(connection, store).publish(
            project_id=project.id,
            source_asset_id=source.id,
            config=job_config(source.id),
            engine_version=__version__,
            artifacts=(preview, mask),
            label="First printable revision",
        )

        current = ProjectRepository(connection).get(project.id)
        stored_revision = RevisionRepository(connection).get(published.revision.id)
        stored_artifacts = ArtifactRepository(connection).list_for_revision(stored_revision.id)

        assert current.active_revision_id == stored_revision.id
        assert stored_revision.config_sha256 == job_config(source.id).fingerprint()
        assert stored_revision.config["source_asset_id"] == source.id
        assert {item.kind for item in stored_artifacts} == {
            "processed-preview",
            "label-field",
        }
        assert all(store.path_for(item.relative_path).is_file() for item in stored_artifacts)
        assert {item.id for item in published.artifacts} == {item.id for item in stored_artifacts}
        assert ProjectRepository(connection).list() == (current,)

        archived = ProjectRepository(connection).set_archived(project.id, archived=True)
        assert archived.archived_at is not None
        assert ProjectRepository(connection).list() == ()
        assert ProjectRepository(connection).list(include_archived=True) == (archived,)
        restored = ProjectRepository(connection).set_archived(project.id, archived=False)
        assert restored.archived_at is None
        assert ProjectRepository(connection).list() == (restored,)
    finally:
        connection.close()


def test_artifact_insert_failure_rolls_back_revision_artifacts_and_active_pointer(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Rollback proof")
        source = source_asset(connection, store)
        first = artifact(store, "first", b"first")
        rejected = artifact(store, "rejected", b"second")
        connection.execute(
            """
            CREATE TRIGGER reject_test_artifact
            BEFORE INSERT ON artifacts
            WHEN NEW.kind = 'rejected'
            BEGIN
                SELECT RAISE(ABORT, 'simulated artifact failure');
            END
            """
        )
        connection.commit()

        with pytest.raises(InvalidPublicationError, match="simulated artifact failure"):
            RevisionPublisher(connection, store).publish(
                project_id=project.id,
                source_asset_id=source.id,
                config=job_config(source.id),
                engine_version=__version__,
                artifacts=(first, rejected),
            )

        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert ProjectRepository(connection).get(project.id).active_revision_id is None
    finally:
        connection.close()


def test_wal_reader_sees_atomic_before_or_after_snapshot_never_partial(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    writer = open_database(database_path)
    reader = open_database(database_path)
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(writer).create("Concurrent reader")
        source = source_asset(writer, store)
        preview = artifact(store, "preview", b"preview")

        reader.execute("BEGIN")
        assert ProjectRepository(reader).get(project.id).active_revision_id is None

        published = RevisionPublisher(writer, store).publish(
            project_id=project.id,
            source_asset_id=source.id,
            config=job_config(source.id),
            engine_version=__version__,
            artifacts=(preview,),
        )

        # The already-open WAL snapshot remains internally consistent.
        assert ProjectRepository(reader).get(project.id).active_revision_id is None
        assert RevisionRepository(reader).list_for_project(project.id) == ()
        reader.commit()

        assert ProjectRepository(reader).get(project.id).active_revision_id == published.revision.id
        assert len(RevisionRepository(reader).list_for_project(project.id)) == 1
        assert len(ArtifactRepository(reader).list_for_revision(published.revision.id)) == 1
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize("missing", ["source", "artifact"])
def test_missing_blob_prevents_any_database_publication(tmp_path, missing: str) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Missing bytes")
        source = source_asset(connection, store)
        preview = artifact(store, "preview", b"preview")
        target = source.relative_path if missing == "source" else preview.blob.relative_path
        store.path_for(target).unlink()

        with pytest.raises(MissingBlobError, match="blob is missing"):
            RevisionPublisher(connection, store).publish(
                project_id=project.id,
                source_asset_id=source.id,
                config=job_config(source.id),
                engine_version=__version__,
                artifacts=(preview,),
            )

        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert ProjectRepository(connection).get(project.id).active_revision_id is None
    finally:
        connection.close()


def test_cross_project_parent_is_rejected_without_orphans(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        projects = ProjectRepository(connection)
        first = projects.create("First")
        second = projects.create("Second")
        source = source_asset(connection, store)
        publisher = RevisionPublisher(connection, store)
        parent = publisher.publish(
            project_id=first.id,
            source_asset_id=source.id,
            config=job_config(source.id),
            engine_version=__version__,
        )

        with pytest.raises(InvalidPublicationError, match="different project"):
            publisher.publish(
                project_id=second.id,
                source_asset_id=source.id,
                config=job_config(source.id),
                engine_version=__version__,
                parent_revision_id=parent.revision.id,
            )

        assert len(RevisionRepository(connection).list_for_project(first.id)) == 1
        assert RevisionRepository(connection).list_for_project(second.id) == ()
        assert projects.get(second.id).active_revision_id is None
    finally:
        connection.close()


def test_database_foreign_keys_still_reject_untyped_orphan_artifact(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, revision_id, kind, sha256, derivation_key, media_type,
                    relative_path, byte_size
                ) VALUES ('artifact', 'missing', 'preview', ?, 'key', 'image/png',
                          'artifacts/00/missing.png', 1)
                """,
                ("0" * 64,),
            )
    finally:
        connection.close()


def test_database_enforces_revision_immutability_and_active_project_ownership(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        projects = ProjectRepository(connection)
        first = projects.create("Immutable")
        second = projects.create("Other")
        source = source_asset(connection, store)
        published = RevisionPublisher(connection, store).publish(
            project_id=first.id,
            source_asset_id=source.id,
            config=job_config(source.id),
            engine_version=__version__,
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE revisions SET label = 'mutated' WHERE id = ?",
                (published.revision.id,),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="belong to the project"):
            connection.execute(
                "UPDATE projects SET active_revision_id = ? WHERE id = ?",
                (published.revision.id, second.id),
            )
        connection.rollback()

        assert RevisionRepository(connection).get(published.revision.id).label == ""
        assert projects.get(second.id).active_revision_id is None
    finally:
        connection.close()
