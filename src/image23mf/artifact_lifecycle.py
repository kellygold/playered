"""Project-scoped artifact inspection and guarded cleanup."""

from __future__ import annotations

from pathlib import Path

from image23mf.artifact_reveal import REVEALABLE_ARTIFACT_KINDS
from image23mf.contracts.artifacts import (
    ArtifactDeletionResource,
    ArtifactInspectionResource,
)
from image23mf.storage import (
    ArtifactRepository,
    ContentAddressedStore,
    RecordNotFoundError,
    StoredBlob,
)
from image23mf.storage.repositories import immediate_transaction


class ArtifactLifecycleError(RuntimeError):
    """Base class for guarded artifact lifecycle failures."""


class ImmutableArtifactError(ArtifactLifecycleError):
    """Published revision evidence cannot be deleted."""


class ArtifactNotRegenerableError(ArtifactLifecycleError):
    """Only artifacts tied to a reproducible job may be deleted."""


SAFE_REGENERABLE_ARTIFACT_KINDS = frozenset({"palette-preview-image"})


def inspect_artifact(
    *,
    connection,
    store: ContentAddressedStore,
    project_id: str,
    artifact_id: str,
) -> ArtifactInspectionResource:
    artifact, job_type = _owned_artifact(connection, project_id, artifact_id)
    integrity = _integrity(store, artifact)
    immutable = artifact.revision_id is not None
    regenerable = (
        not immutable
        and artifact.job_id is not None
        and job_type is not None
        and artifact.kind in SAFE_REGENERABLE_ARTIFACT_KINDS
    )
    return ArtifactInspectionResource(
        artifact_id=artifact.id,
        kind=artifact.kind,
        integrity=integrity,
        immutable=immutable,
        regenerable=regenerable,
        revealable=artifact.kind in REVEALABLE_ARTIFACT_KINDS,
        recovery_action=_recovery_action(job_type) if regenerable else None,
    )


def delete_regenerable_artifact(
    *,
    connection,
    store: ContentAddressedStore,
    project_id: str,
    artifact_id: str,
) -> ArtifactDeletionResource:
    artifact, job_type = _owned_artifact(connection, project_id, artifact_id)
    if artifact.revision_id is not None:
        raise ImmutableArtifactError(
            "Published revision artifacts are immutable history and cannot be deleted."
        )
    if artifact.job_id is None or job_type is None:
        raise ArtifactNotRegenerableError(
            "This artifact is not tied to a reproducible job and cannot be deleted safely."
        )
    if artifact.kind not in SAFE_REGENERABLE_ARTIFACT_KINDS:
        raise ArtifactNotRegenerableError(
            "This artifact is required to reopen its completed job; regenerate the job instead."
        )

    path: Path | None
    try:
        path = store.path_for(artifact.relative_path)
    except FileNotFoundError:
        path = None
    with immediate_transaction(connection):
        deleted = connection.execute(
            """
            DELETE FROM artifacts
            WHERE id = ? AND revision_id IS NULL AND job_id IS NOT NULL
            """,
            (artifact.id,),
        ).rowcount
        if deleted != 1:
            raise ArtifactNotRegenerableError(
                "The artifact changed before cleanup; reload its current state."
            )
        remaining = int(
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE relative_path = ?",
                (artifact.relative_path,),
            ).fetchone()[0]
        )

    deleted_blob = False
    if remaining == 0 and path is not None:
        try:
            path.unlink()
            deleted_blob = True
        except FileNotFoundError:
            pass

    return ArtifactDeletionResource(
        artifact_id=artifact.id,
        deleted_record=True,
        deleted_blob=deleted_blob,
        shared_blob_retained=remaining > 0,
        recovery_action=_recovery_action(job_type),
    )


def _owned_artifact(connection, project_id: str, artifact_id: str):
    artifacts = ArtifactRepository(connection)
    artifact = artifacts.get(artifact_id)
    if artifacts.owner_project_id(artifact_id) != project_id:
        raise RecordNotFoundError(f"artifact not found: {artifact_id}")
    job_type = None
    if artifact.job_id is not None:
        row = connection.execute(
            "SELECT job_type FROM jobs WHERE id = ?", (artifact.job_id,)
        ).fetchone()
        if row is not None:
            job_type = str(row["job_type"])
    return artifact, job_type


def _integrity(store: ContentAddressedStore, artifact) -> str:
    blob = StoredBlob(
        sha256=artifact.sha256,
        relative_path=artifact.relative_path,
        byte_size=artifact.byte_size,
        media_type=artifact.media_type,
        extension=Path(artifact.relative_path).suffix,
    )
    try:
        return "verified" if store.verify(blob) else "corrupt"
    except FileNotFoundError:
        return "missing"


def _recovery_action(job_type: str | None) -> str:
    return {
        "preview": "Generate a new preview to recreate this artifact.",
        "geometry": "Regenerate geometry to recreate this artifact.",
        "export": "Generate output again to recreate this artifact.",
        "validation": "Validate the output again to recreate this artifact.",
    }.get(job_type, "Run the originating job again to recreate this artifact.")
