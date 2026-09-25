"""Application service that derives trusted mural provenance from workspace state."""

from __future__ import annotations

import sqlite3

from pydantic import ValidationError

from image23mf import __version__
from image23mf.contracts.job import JobConfig, load_job_config
from image23mf.contracts.mural import (
    MuralPlanPreviewResource,
    MuralPlanResource,
    MuralPlanSettings,
    SaveMuralPlanRequest,
)
from image23mf.engine.transform import PixelSize
from image23mf.mural.planner import (
    BedEnvelope,
    BedRectangle,
    MuralPlanRequest,
    MuralSourceProvenance,
    build_mural_plan,
)
from image23mf.mural.provenance import (
    InvalidMuralPlanSourceError,
    MuralProvenanceValidator,
)
from image23mf.mural.repository import MuralPlanRepository
from image23mf.profiles import PrintSetupRequest, ProfileCatalogService, ProfileValidationError
from image23mf.storage import (
    ArtifactRepository,
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    JobRepository,
    RecordNotFoundError,
    RevisionRepository,
)
from image23mf.storage.models import ArtifactRecord


class MuralPlanningError(ValueError):
    """Workspace state cannot produce the requested reproducible mural plan."""


class MuralPlanningService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        blob_store: ContentAddressedStore,
        profiles: ProfileCatalogService,
    ) -> None:
        self.connection = connection
        self.blob_store = blob_store
        self.profiles = profiles
        self.plans = MuralPlanRepository(connection, blob_store)
        self.provenance = MuralProvenanceValidator(connection, blob_store)

    def get(self, project_id: str) -> MuralPlanResource | None:
        record = self.plans.get(project_id)
        if record is None:
            return None
        stale_reason = None
        try:
            self.provenance.validate(project_id, record.request)
        except (InvalidMuralPlanSourceError, RecordNotFoundError) as error:
            stale_reason = str(error)
        return MuralPlanResource.from_record(record, stale_reason=stale_reason)

    def save(
        self,
        project_id: str,
        save_request: SaveMuralPlanRequest,
    ) -> MuralPlanResource:
        request = self._request(project_id, save_request)
        record = self.plans.save(
            project_id,
            request,
            expected_generation=save_request.expected_generation,
        )
        return MuralPlanResource.from_record(record)

    def preview(
        self,
        project_id: str,
        settings: MuralPlanSettings,
    ) -> MuralPlanPreviewResource:
        request = self._request(project_id, settings)
        return MuralPlanPreviewResource(request=request, plan=build_mural_plan(request))

    def _request(
        self,
        project_id: str,
        settings: MuralPlanSettings,
    ) -> MuralPlanRequest:
        artifact = ArtifactRepository(self.connection).get(settings.processed_artifact_id)
        if ArtifactRepository(self.connection).owner_project_id(artifact.id) != project_id:
            raise MuralPlanningError("processed mural artifact belongs to a different project")
        statistics = self.provenance.preview_statistics(artifact)
        config, revision_id, draft_generation, engine_version = self._source_state(
            project_id, artifact
        )
        source_asset = AssetRepository(self.connection, self.blob_store).get(config.source_asset_id)
        validated = self._validated_setup(config)
        profile_fingerprint = validated.profile_catalog_fingerprint
        if statistics.profile_catalog_fingerprint != profile_fingerprint:
            raise MuralPlanningError(
                "the preview was generated with a different printer profile catalog; "
                "render a current preview before planning"
            )
        area = validated.printer.printable_area
        exclusions = tuple(
            BedRectangle(
                x_mm=item.x_mm,
                y_mm=item.y_mm,
                width_mm=item.width_mm,
                height_mm=item.depth_mm,
            )
            for item in area.excluded_rectangles
        )
        try:
            return MuralPlanRequest(
                source=MuralSourceProvenance(
                    source_asset_id=source_asset.id,
                    source_asset_sha256=source_asset.sha256,
                    processed_artifact_id=artifact.id,
                    processed_artifact_sha256=artifact.sha256,
                    processed_size=PixelSize(
                        width=statistics.preview.width,
                        height=statistics.preview.height,
                    ),
                    canonical_transform=statistics.transform,
                    config_sha256=config.fingerprint(),
                    engine_version=engine_version,
                    revision_id=revision_id,
                    draft_generation=draft_generation,
                ),
                layout=settings.layout,
                bed=BedEnvelope(
                    printer_id=validated.printer.id,
                    plate_id=validated.plate.id,
                    profile_catalog_fingerprint=profile_fingerprint,
                    width_mm=area.width_mm,
                    height_mm=area.depth_mm,
                    edge_clearance_mm=settings.edge_clearance_mm,
                    excluded_rectangles=exclusions,
                ),
                reserved_rectangles=settings.reserved_rectangles,
            )
        except ValidationError as error:
            raise MuralPlanningError(
                "mural dimensions, clearance, or reserved plate areas are invalid"
            ) from error

    def delete(self, project_id: str, *, expected_generation: int) -> bool:
        return self.plans.delete(project_id, expected_generation=expected_generation)

    def _source_state(
        self,
        project_id: str,
        artifact: ArtifactRecord,
    ) -> tuple[JobConfig, str | None, int | None, str]:
        if artifact.revision_id is not None:
            revision = RevisionRepository(self.connection).get(artifact.revision_id)
            if revision.project_id != project_id:
                raise MuralPlanningError("mural revision belongs to a different project")
            try:
                config = load_job_config(dict(revision.config))
            except (TypeError, ValueError) as error:
                raise MuralPlanningError("mural revision configuration is invalid") from error
            return config, revision.id, None, revision.engine_version
        if artifact.job_id is None:
            raise MuralPlanningError("processed mural artifact has no preview provenance")
        job = JobRepository(self.connection).get(artifact.job_id)
        if job.project_id != project_id:
            raise MuralPlanningError("mural preview belongs to a different project")
        draft = DraftRepository(self.connection).get(project_id)
        if draft is None:
            raise MuralPlanningError("project has no current draft")
        try:
            config = load_job_config(dict(draft.config))
        except (TypeError, ValueError) as error:
            raise MuralPlanningError("current draft configuration is invalid") from error
        return config, None, draft.generation, __version__

    def _validated_setup(self, config: JobConfig):
        try:
            return self.profiles.validate_pinned(
                PrintSetupRequest(
                    printer_id=config.printer.printer_id,
                    nozzle_id=config.printer.nozzle_id,
                    plate_id=config.printer.plate_id,
                    layer_height_mm=config.printer.layer_height_mm,
                    canvas_width_mm=config.canvas.width_mm,
                    canvas_height_mm=config.canvas.height_mm,
                    base_thickness_mm=config.geometry.base_thickness_mm,
                    art_thickness_mm=config.geometry.art_thickness_mm,
                ),
                catalog_id=config.printer.profile_catalog_id,
                catalog_version=config.printer.profile_catalog_version,
                nozzle_diameter_mm=config.printer.nozzle_mm,
            )
        except ProfileValidationError as error:
            raise MuralPlanningError(
                "the saved print setup no longer matches the installed printer profiles"
            ) from error
