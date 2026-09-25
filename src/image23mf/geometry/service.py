"""Project-scoped durable geometry orchestration service."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
from pathlib import Path
from typing import Optional

import numpy as np

from image23mf.contracts.geometry import GeometryJobResult, GeometryStartResponse
from image23mf.contracts.jobs import JobState, JobType
from image23mf.contracts.processing import ArtifactResource, PreviewStatistics
from image23mf.engine.labels import LabelField
from image23mf.geometry.jobs import (
    GEOMETRY_IR_KIND,
    GEOMETRY_MESH_KIND,
    GEOMETRY_PREVIEW_KIND,
    GEOMETRY_REPORT_KIND,
    GEOMETRY_SVG_KIND,
    GeometryJobPipeline,
)
from image23mf.geometry.production import (
    ProductionGeometryInput,
    production_geometry_adapters,
    production_geometry_request,
)
from image23mf.storage import (
    ArtifactRepository,
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    JobRepository,
    ProjectRepository,
    RecordNotFoundError,
    StoredBlob,
    draft_state_fingerprint,
    open_database,
)
from image23mf.workers import LocalWorkerManager


class GeometryRequestError(ValueError):
    """A geometry request does not identify current, verified project evidence."""


class GeometryService:
    def __init__(
        self,
        *,
        database_path: Path,
        blob_store: ContentAddressedStore,
        pipeline: Optional[GeometryJobPipeline] = None,
    ) -> None:
        self.database_path = database_path
        self.blob_store = blob_store
        self.pipeline = pipeline or GeometryJobPipeline(adapters=production_geometry_adapters())

    def start(
        self,
        *,
        project_id: str,
        preview_job_id: str,
        expected_draft_generation: int,
        manager: LocalWorkerManager,
    ) -> GeometryStartResponse:
        payload = self._verified_input(
            project_id=project_id,
            preview_job_id=preview_job_id,
            expected_draft_generation=expected_draft_generation,
        )
        request = production_geometry_request(payload, preview_job_id=preview_job_id)
        job = self.pipeline.submit(
            manager,
            project_id=project_id,
            request=request,
            supersession_key=f"geometry:{project_id}",
        )
        return GeometryStartResponse(job=job)

    def result(self, *, project_id: str, job_id: str) -> GeometryJobResult:
        connection = open_database(self.database_path)
        try:
            ProjectRepository(connection).get(project_id)
            job = JobRepository(connection).get(job_id)
            if job.project_id != project_id or job.type != JobType.GEOMETRY:
                raise RecordNotFoundError(f"geometry job not found in project: {job_id}")
            records = ArtifactRepository(connection).list_for_job(job_id)
        finally:
            connection.close()
        resources = tuple(self._verified_artifact(item, project_id=project_id) for item in records)
        by_kind = {item.kind: item for item in resources}
        return GeometryJobResult(
            job=job,
            artifacts=resources,
            geometry_svg=by_kind.get(GEOMETRY_SVG_KIND),
            geometry_ir=by_kind.get(GEOMETRY_IR_KIND),
            geometry_mesh=by_kind.get(GEOMETRY_MESH_KIND),
            geometry_report=by_kind.get(GEOMETRY_REPORT_KIND),
            geometry_preview=by_kind.get(GEOMETRY_PREVIEW_KIND),
            export_ready=job.state == JobState.SUCCEEDED
            and all(
                kind in by_kind
                for kind in (
                    GEOMETRY_SVG_KIND,
                    GEOMETRY_IR_KIND,
                    GEOMETRY_MESH_KIND,
                    GEOMETRY_REPORT_KIND,
                    GEOMETRY_PREVIEW_KIND,
                )
            ),
        )

    def _verified_input(
        self,
        *,
        project_id: str,
        preview_job_id: str,
        expected_draft_generation: int,
    ) -> ProductionGeometryInput:
        connection = open_database(self.database_path)
        try:
            ProjectRepository(connection).get(project_id)
            drafts = DraftRepository(connection)
            draft = drafts.get(project_id)
            if draft is None:
                raise GeometryRequestError("the project has no editable draft")
            if draft.generation != expected_draft_generation:
                raise GeometryRequestError(
                    "the draft changed; render a current preview before generating geometry"
                )
            config = drafts.load_config(project_id)
            jobs = JobRepository(connection)
            preview = jobs.get(preview_job_id)
            current = jobs.current_head(
                project_id=project_id,
                supersession_key=f"preview:{project_id}",
            )
            if (
                preview.project_id != project_id
                or preview.type != JobType.PREVIEW
                or preview.state != JobState.SUCCEEDED
                or current is None
                or current.id != preview.id
            ):
                raise GeometryRequestError(
                    "geometry requires the latest successful preview for this project"
                )
            artifacts = ArtifactRepository(connection).list_for_job(preview_job_id)
            by_kind = {item.kind: item for item in artifacts}
            labels_record = by_kind.get("processed-labels")
            assignment_record = by_kind.get("region-assignment")
            statistics_record = by_kind.get("preview-statistics")
            if labels_record is None or assignment_record is None or statistics_record is None:
                raise GeometryRequestError("the preview is missing required geometry evidence")
            labels_bytes = self._verified_bytes(labels_record)
            assignment_bytes = self._verified_bytes(assignment_record)
            statistics = PreviewStatistics.model_validate_json(
                self._verified_bytes(statistics_record)
            )
            asset = AssetRepository(connection, self.blob_store).get(config.source_asset_id)
        finally:
            connection.close()
        if statistics.config_sha256 != draft.config_sha256:
            raise GeometryRequestError(
                "the preview configuration is stale; update the preview before generating geometry"
            )
        width = _positive_integer(labels_record.metadata, "width")
        height = _positive_integer(labels_record.metadata, "height")
        if len(labels_bytes) != width * height:
            raise GeometryRequestError("processed label byte count does not match its dimensions")
        if len(assignment_bytes) != width * height * 4:
            raise GeometryRequestError(
                "region assignment byte count does not match label dimensions"
            )
        assignments = np.frombuffer(assignment_bytes, dtype="<i4").reshape(height, width)
        geometry_labels = bytearray(labels_bytes)
        inactive = assignments.reshape(-1) < 0
        if inactive.any():
            values = np.frombuffer(geometry_labels, dtype=np.uint8)
            values[inactive] = 0
        label_field = LabelField(
            width=width,
            height=height,
            label_values=tuple(range(len(config.palette.colors))),
            pixels=bytes(geometry_labels),
        )
        return ProductionGeometryInput(
            source_asset_id=config.source_asset_id,
            source_asset_sha256=asset.sha256,
            processed_labels_sha256=hashlib.sha256(label_field.pixels).hexdigest(),
            labels=label_field,
            config=config,
            operations_fingerprint=draft_state_fingerprint(
                draft.config_sha256,
                draft.operations,
                base_revision_id=draft.base_revision_id,
            ),
        )

    def _verified_bytes(self, record) -> bytes:
        blob = StoredBlob(
            sha256=record.sha256,
            relative_path=record.relative_path,
            byte_size=record.byte_size,
            media_type=record.media_type,
            extension=Path(record.relative_path).suffix,
        )
        if not self.blob_store.verify(blob):
            raise GeometryRequestError(f"{record.kind} artifact failed content verification")
        return self.blob_store.path_for(record.relative_path).read_bytes()

    def _verified_artifact(self, record, *, project_id: str) -> ArtifactResource:
        self._verified_bytes(record)
        return ArtifactResource(
            id=record.id,
            job_id=record.job_id,
            revision_id=record.revision_id,
            kind=record.kind,
            sha256=record.sha256,
            derivation_key=record.derivation_key,
            media_type=record.media_type,
            byte_size=record.byte_size,
            metadata=dict(record.metadata),
            download_url=f"/api/projects/{project_id}/artifacts/{record.id}",
            created_at=record.created_at,
        )


def _positive_integer(metadata: dict, key: str) -> int:
    value = metadata.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GeometryRequestError(f"processed label metadata {key} must be a positive integer")
    return value
