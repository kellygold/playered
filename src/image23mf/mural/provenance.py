"""Fail-closed validation for the processed master behind a mural plan."""

from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from image23mf import __version__
from image23mf.contracts.jobs import JobState, JobType
from image23mf.contracts.processing import PreviewStatistics
from image23mf.mural.planner import MuralPlanRequest
from image23mf.storage import (
    ArtifactRepository,
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    JobRepository,
    RevisionRepository,
    StoredBlob,
)
from image23mf.storage.models import ArtifactRecord
from image23mf.storage.repositories import RepositoryError


class InvalidMuralPlanSourceError(RepositoryError):
    """The plan provenance is forged, stale, cross-project, or unavailable."""


class MuralProvenanceValidator:
    """Validate every persisted identity against durable records and verified bytes."""

    def __init__(self, connection, blob_store: ContentAddressedStore) -> None:
        self.connection = connection
        self.blob_store = blob_store
        self.assets = AssetRepository(connection, blob_store)
        self.artifacts = ArtifactRepository(connection)

    def validate(self, project_id: str, request: MuralPlanRequest) -> None:
        source = request.source
        try:
            asset = self.assets.get(source.source_asset_id)
            processed = self.artifacts.get(source.processed_artifact_id)
        except RepositoryError as error:
            raise InvalidMuralPlanSourceError(str(error)) from error
        if asset.sha256 != source.source_asset_sha256:
            raise InvalidMuralPlanSourceError(
                "mural plan source SHA-256 does not match the stored source asset"
            )
        if not _blob_verified(self.blob_store, asset.stored_blob()):
            raise InvalidMuralPlanSourceError("mural plan source asset blob is missing or corrupt")
        if self.artifacts.owner_project_id(processed.id) != project_id:
            raise InvalidMuralPlanSourceError(
                "processed mural artifact belongs to a different project"
            )
        if processed.kind != "palette-preview-image":
            raise InvalidMuralPlanSourceError(
                "processed mural artifact must be a palette-preview-image"
            )
        if processed.sha256 != source.processed_artifact_sha256:
            raise InvalidMuralPlanSourceError(
                "processed mural artifact SHA-256 does not match the stored artifact"
            )
        self._validate_processed_png(processed, request)
        self._validate_revision_or_draft(project_id, processed, request)
        statistics = self.preview_statistics(processed)
        self._validate_statistics_against_request(statistics, request)

    def _validate_processed_png(
        self,
        processed: ArtifactRecord,
        request: MuralPlanRequest,
    ) -> None:
        if processed.media_type != "image/png" or Path(processed.relative_path).suffix != ".png":
            raise InvalidMuralPlanSourceError("processed mural artifact must be a PNG image")
        if not _blob_verified(self.blob_store, _stored_blob(processed)):
            raise InvalidMuralPlanSourceError("processed mural artifact blob is missing or corrupt")
        try:
            with Image.open(
                self.blob_store.path_for(processed.relative_path), formats=["PNG"]
            ) as image:
                image.load()
                dimensions = image.size
        except (OSError, UnidentifiedImageError) as error:
            raise InvalidMuralPlanSourceError(
                "processed mural artifact is not a readable PNG"
            ) from error
        expected = request.source.processed_size
        if dimensions != (expected.width, expected.height):
            raise InvalidMuralPlanSourceError(
                "processed mural artifact dimensions do not match the canonical master"
            )

    def _validate_revision_or_draft(
        self,
        project_id: str,
        processed: ArtifactRecord,
        request: MuralPlanRequest,
    ) -> None:
        source = request.source
        if source.revision_id is not None:
            try:
                revision = RevisionRepository(self.connection).get(source.revision_id)
            except RepositoryError as error:
                raise InvalidMuralPlanSourceError(str(error)) from error
            if revision.project_id != project_id or processed.revision_id != revision.id:
                raise InvalidMuralPlanSourceError(
                    "processed mural revision belongs to a different project or artifact"
                )
            if revision.source_asset_id != source.source_asset_id:
                raise InvalidMuralPlanSourceError("mural revision source asset does not match")
            if revision.config_sha256 != source.config_sha256:
                raise InvalidMuralPlanSourceError("mural revision configuration is stale")
            if revision.engine_version != source.engine_version:
                raise InvalidMuralPlanSourceError("mural revision engine provenance does not match")
            return

        draft = DraftRepository(self.connection).get(project_id)
        if draft is None:
            raise InvalidMuralPlanSourceError("mural draft no longer exists")
        if processed.job_id is None or processed.revision_id is not None:
            raise InvalidMuralPlanSourceError(
                "draft mural master must come from a preview job, not a published revision"
            )
        job = JobRepository(self.connection).get(processed.job_id)
        if (
            job.project_id != project_id
            or job.type != JobType.PREVIEW
            or job.state != JobState.SUCCEEDED
        ):
            raise InvalidMuralPlanSourceError(
                "draft mural master must come from a successful preview for this project"
            )
        if job.supersession_key is not None and not JobRepository(self.connection).is_current(
            job.id
        ):
            raise InvalidMuralPlanSourceError("draft mural master preview has been superseded")
        if draft.generation != source.draft_generation:
            raise InvalidMuralPlanSourceError("mural draft generation is stale")
        if draft.config_sha256 != source.config_sha256:
            raise InvalidMuralPlanSourceError("mural draft configuration fingerprint is stale")
        if draft.config.get("source_asset_id") != source.source_asset_id:
            raise InvalidMuralPlanSourceError("mural draft source asset does not match")
        if source.engine_version != __version__:
            raise InvalidMuralPlanSourceError("mural draft engine provenance is stale")

    def preview_statistics(
        self,
        processed: ArtifactRecord,
    ) -> PreviewStatistics:
        """Load the one verified statistics/preview chain paired to a processed master."""

        if processed.job_id is not None:
            candidates = self.artifacts.list_for_job(processed.job_id)
        elif processed.revision_id is not None:
            candidates = self.artifacts.list_for_revision(processed.revision_id)
        else:  # pragma: no cover - artifact ownership validation rejects this first
            candidates = ()
        statistics_records = tuple(
            item
            for item in candidates
            if item.kind == "preview-statistics" and item.derivation_key == processed.derivation_key
        )
        if len(statistics_records) != 1:
            raise InvalidMuralPlanSourceError(
                "processed mural artifact requires one matching preview-statistics artifact"
            )
        record = statistics_records[0]
        preview_records = tuple(
            item
            for item in candidates
            if item.kind == "preview-image" and item.derivation_key == processed.derivation_key
        )
        if len(preview_records) != 1:
            raise InvalidMuralPlanSourceError(
                "preview statistics require one matching preview-image artifact"
            )
        preview = preview_records[0]
        if not _blob_verified(self.blob_store, _stored_blob(preview)):
            raise InvalidMuralPlanSourceError("preview image blob is missing or corrupt")
        if not _blob_verified(self.blob_store, _stored_blob(record)):
            raise InvalidMuralPlanSourceError("preview statistics blob is missing or corrupt")
        try:
            statistics = PreviewStatistics.model_validate_json(
                self.blob_store.path_for(record.relative_path).read_bytes()
            )
        except (OSError, ValidationError, ValueError) as error:
            raise InvalidMuralPlanSourceError("preview statistics are malformed") from error
        if statistics.preview_sha256 != preview.sha256:
            raise InvalidMuralPlanSourceError(
                "preview statistics image fingerprint does not match its preview artifact"
            )
        return statistics

    @staticmethod
    def _validate_statistics_against_request(
        statistics: PreviewStatistics,
        request: MuralPlanRequest,
    ) -> None:
        source = request.source
        if statistics.source_asset_id != source.source_asset_id:
            raise InvalidMuralPlanSourceError("preview statistics source asset does not match")
        if statistics.config_sha256 != source.config_sha256:
            raise InvalidMuralPlanSourceError("preview statistics configuration is stale")
        if (
            statistics.preview.width != source.processed_size.width
            or statistics.preview.height != source.processed_size.height
        ):
            raise InvalidMuralPlanSourceError("preview statistics dimensions do not match")
        if statistics.transform != source.canonical_transform:
            raise InvalidMuralPlanSourceError(
                "preview statistics canonical transform does not match"
            )
        if statistics.profile_catalog_fingerprint != request.bed.profile_catalog_fingerprint:
            raise InvalidMuralPlanSourceError("printer profile catalog provenance does not match")


def _stored_blob(artifact: ArtifactRecord) -> StoredBlob:
    return StoredBlob(
        sha256=artifact.sha256,
        relative_path=artifact.relative_path,
        byte_size=artifact.byte_size,
        media_type=artifact.media_type,
        extension=Path(artifact.relative_path).suffix,
    )


def _blob_verified(store: ContentAddressedStore, blob: StoredBlob) -> bool:
    try:
        return store.verify(blob)
    except FileNotFoundError:
        return False
