"""Application services composing persistence, profiles, workers, and the preview engine."""

import base64
import binascii
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

from image23mf import __version__
from image23mf.calibration import (
    PrintabilityOverrides,
    PrintabilityProfileService,
    ResolvedPrintabilitySettings,
    ResolvePrintabilityRequest,
)
from image23mf.contracts.editor import (
    CANVAS_SELECTION_STORAGE_OPERATION_TYPE,
    EditorCommandSequence,
    EditorReplayRecord,
    LocalRasterEditCommand,
)
from image23mf.contracts.job import CleanupOverrideField, JobConfig, MergePolicy, load_job_config
from image23mf.contracts.jobs import JobResource, JobStage, JobState, JobType
from image23mf.contracts.processing import (
    ArtifactResource,
    AutoPaletteResource,
    DraftHistoryCommandRequest,
    DraftResource,
    ImageAssetResource,
    PreviewJobResult,
    PreviewStartResponse,
    PreviewStatistics,
    ProjectResource,
    ProjectSummaryCollection,
    ProjectSummaryResource,
    ProjectWorkspaceResource,
)
from image23mf.contracts.revisions import (
    BranchRevisionResponse,
    PreviewEvidenceResource,
    PublishRevisionResponse,
    RevisionCollection,
    RevisionResource,
    RevisionSummaryResource,
)
from image23mf.derivation import DerivationIdentity
from image23mf.editor import (
    IncompatibleSelectorError,
    load_persisted_commands,
    replay_editor_commands,
    validate_canvas_selection_storage_payload,
    validate_persisted_sequence,
)
from image23mf.editor.selection import rasterize_canvas_selection
from image23mf.engine.automatic_cleanup import (
    AutomaticCleanupRecord,
    AutomaticCleanupResult,
    apply_automatic_cleanup,
)
from image23mf.engine.clearance import (
    ClearanceAnalysis,
    ClearanceAnalysisOptions,
    analyze_clearance_features,
    clearance_risk_findings,
    clearance_worker_count,
    validate_clearance_analysis_against_graph,
)
from image23mf.engine.holes import (
    HoleAnalysis,
    HoleAnalysisOptions,
    classify_holes,
    hole_risk_findings,
    validate_hole_analysis_against_graph,
)
from image23mf.engine.ingestion import ImageIngestionLimits, SourceImageMetadata, ingest_image
from image23mf.engine.islands import (
    IslandAnalysis,
    IslandAnalysisOptions,
    IslandMergePolicy,
    classify_small_islands,
    island_risk_findings,
    validate_island_analysis_against_graph,
)
from image23mf.engine.labels import LabelField
from image23mf.engine.palette import (
    PaletteErrorCode,
    PaletteQuantizationError,
    QuantizationResult,
    classify_palette,
    fit_auto_palette,
)
from image23mf.engine.palette_metrics import PaletteMetrics, compute_palette_metrics
from image23mf.engine.preview import render_preview
from image23mf.engine.regions import RegionGraph, analyze_regions
from image23mf.engine.risks import (
    RiskAnalysisOptions,
    RiskCode,
    RiskReport,
    analyze_printability_risks,
    validate_risk_report_against_graph,
)
from image23mf.invalidation import (
    ReprocessingPlan,
    ReprocessingReason,
    empty_mask,
    is_strictly_interior_selection,
    mask_sha256,
)
from image23mf.profiles import PrintSetupRequest, ProfileCatalogService
from image23mf.storage import (
    ArtifactRecord,
    ArtifactRepository,
    AssetRecord,
    AssetRepository,
    ContentAddressedStore,
    DraftHistoryCommand,
    DraftRecord,
    DraftRepository,
    JobRepository,
    ProjectRecord,
    ProjectRepository,
    RecordNotFoundError,
    RegionOperation,
    RegionOperationRepository,
    RevisionPublicationRecord,
    RevisionPublicationRepository,
    RevisionPublisher,
    RevisionRecord,
    RevisionRepository,
    RevisionSummaryRecord,
    StoredBlob,
    artifact_manifest_sha256,
    draft_state_fingerprint,
    open_database,
)
from image23mf.storage.repositories import canonical_json
from image23mf.workers import JobContext, JobWork, LocalWorkerManager, WorkerArtifact, WorkerResult

PreviewWorkFactory = Callable[
    [JobConfig, str, PrintabilityProfileService, tuple[RegionOperation, ...], Optional[str]],
    JobWork,
]

PREVIEW_ADAPTER_VERSIONS = {
    "automatic_cleanup": "1",
    "editor_replay": "1",
    "palette_classification": "1",
    "preview_pipeline": "10",
    "risk_analysis": "1",
}


@dataclass(frozen=True)
class _ReprocessingBaseline:
    job_id: str
    config_fingerprint: str
    component_fingerprints: dict[str, str]
    engine_version: str
    adapter_versions: dict[str, str]
    dependencies: dict[str, str]
    command_fingerprints: tuple[str, ...]
    quantized_labels: LabelField
    quantized_alpha: bytes
    quantized_palette: tuple[str, ...]
    quantization_options_fingerprint: str
    cleanup_labels: LabelField
    cleanup_active: bytes
    cleanup_changed_mask: bytes
    cleanup_record: AutomaticCleanupRecord
    processed_labels: bytes
    processed_active: bytes


_CLEANUP_RECOMMENDATIONS = {
    CleanupOverrideField.MIN_ISLAND_MM2: "minimum_island_area_mm2",
    CleanupOverrideField.MINIMUM_ISLAND_DIAMETER_MM: "minimum_island_diameter_mm",
    CleanupOverrideField.MAX_HOLE_MM2: "maximum_tiny_hole_area_mm2",
    CleanupOverrideField.MAXIMUM_TINY_HOLE_DIAMETER_MM: ("maximum_tiny_hole_diameter_mm"),
    CleanupOverrideField.MINIMUM_RING_WIDTH_MM: "minimum_ring_width_mm",
    CleanupOverrideField.MINIMUM_LINE_WIDTH_MM: "minimum_line_width_mm",
    CleanupOverrideField.MINIMUM_NECK_WIDTH_MM: "minimum_neck_width_mm",
    CleanupOverrideField.MINIMUM_GAP_WIDTH_MM: "minimum_gap_width_mm",
    CleanupOverrideField.LONG_LINE_MINIMUM_LENGTH_MM: "long_line_minimum_length_mm",
    CleanupOverrideField.SMOOTHING_RADIUS_MM: "smoothing_radius_mm",
}

_ISLAND_MERGE_POLICY = {
    MergePolicy.REVIEW: IslandMergePolicy.REVIEW,
    MergePolicy.KEEP: IslandMergePolicy.KEEP,
    MergePolicy.DOMINANT_NEIGHBOR: IslandMergePolicy.DOMINANT_NEIGHBOR,
    MergePolicy.PERCEPTUAL_NEIGHBOR: IslandMergePolicy.PERCEPTUAL_NEIGHBOR,
}


class InvalidPreviewJobError(ValueError):
    """Raised when a preview-only result resource receives another job type."""


class InvalidRevisionCursorError(ValueError):
    """Raised when revision history pagination receives a malformed cursor."""


class InvalidDraftHistoryTargetError(ValueError):
    """Raised when a persisted target no longer satisfies current config contracts."""


class ProcessingService:
    def __init__(
        self,
        *,
        database_path: Path,
        blob_store: ContentAddressedStore,
        profiles: ProfileCatalogService,
        printability_profiles: PrintabilityProfileService,
        ingestion_limits: Optional[ImageIngestionLimits] = None,
    ) -> None:
        self.database_path = database_path
        self.blob_store = blob_store
        self.profiles = profiles
        self.printability_profiles = printability_profiles
        self.ingestion_limits = ingestion_limits or ImageIngestionLimits()

    def import_project(
        self,
        payload: bytes,
        *,
        filename: str,
        project_name: Optional[str] = None,
    ) -> ProjectWorkspaceResource:
        normalized = ingest_image(payload, filename=filename, limits=self.ingestion_limits)
        blob = self.blob_store.put_bytes(
            normalized.png_bytes,
            namespace="assets",
            extension=".png",
            media_type="image/png",
        )
        connection = open_database(self.database_path)
        project: Optional[ProjectRecord] = None
        try:
            asset = AssetRepository(connection, self.blob_store).register(
                blob,
                original_filename=normalized.metadata.original_filename,
                width_px=normalized.metadata.normalized_width_px,
                height_px=normalized.metadata.normalized_height_px,
                metadata=normalized.metadata.model_dump(mode="json"),
            )
            project = ProjectRepository(connection).create(
                _project_name(project_name, normalized.metadata.original_filename)
            )
            config = self._default_config(asset.id)
            draft = DraftRepository(connection).save(
                project_id=project.id,
                config=config,
                expected_generation=0,
            )
            return _workspace_resource(project, asset, draft, latest_preview_job=None)
        except Exception:
            if project is not None:
                connection.execute("DELETE FROM projects WHERE id = ?", (project.id,))
                connection.commit()
            raise
        finally:
            connection.close()

    def get_workspace(self, project_id: str) -> ProjectWorkspaceResource:
        connection = open_database(self.database_path)
        try:
            project = ProjectRepository(connection).get(project_id)
            draft = DraftRepository(connection).get(project_id)
            asset = None
            if draft is not None:
                config = DraftRepository(connection).load_config(project_id)
                asset = AssetRepository(connection, self.blob_store).get(config.source_asset_id)
            latest_preview = JobRepository(connection).current_head(
                project_id=project_id,
                supersession_key=f"preview:{project_id}",
            )
            return _workspace_resource(
                project,
                asset,
                draft,
                latest_preview_job=latest_preview,
            )
        finally:
            connection.close()

    def list_projects(self, *, include_archived: bool = False) -> ProjectSummaryCollection:
        connection = open_database(self.database_path)
        try:
            items = tuple(
                self._project_summary(connection, project)
                for project in ProjectRepository(connection).list(include_archived=include_archived)
            )
            return ProjectSummaryCollection(items=items, total=len(items))
        finally:
            connection.close()

    def set_project_archived(self, project_id: str, *, archived: bool) -> ProjectSummaryResource:
        connection = open_database(self.database_path)
        try:
            project = ProjectRepository(connection).set_archived(project_id, archived=archived)
            return self._project_summary(connection, project)
        finally:
            connection.close()

    def _project_summary(self, connection, project: ProjectRecord) -> ProjectSummaryResource:
        draft = DraftRepository(connection).get(project.id)
        if draft is None:
            raise RecordNotFoundError(f"draft not found for project: {project.id}")
        config = DraftRepository(connection).load_config(project.id)
        asset = AssetRepository(connection, self.blob_store).get(config.source_asset_id)
        if asset.width_px is None or asset.height_px is None:
            raise ValueError("project source asset is missing dimensions")

        jobs = JobRepository(connection)
        preview = jobs.current_head(project_id=project.id, supersession_key=f"preview:{project.id}")
        thumbnail_url = f"/api/assets/{asset.id}"
        thumbnail_kind = "source"
        preview_artifact = None
        if (
            preview is not None
            and preview.state == JobState.SUCCEEDED
            and preview.request_key is not None
            and preview.created_at >= draft.updated_at
        ):
            for artifact in ArtifactRepository(connection).list_for_job(preview.id):
                if artifact.kind != "palette-preview-image":
                    continue
                if artifact.derivation_key != preview.request_key:
                    continue
                preview_artifact = artifact
                break
        if preview_artifact is not None:
            thumbnail_url = f"/api/projects/{project.id}/artifacts/{preview_artifact.id}"
            thumbnail_kind = "preview"

        export = jobs.current_head(project_id=project.id, supersession_key=f"export:{project.id}")
        if export is None:
            validation = "not_requested"
        elif export.state == JobState.SUCCEEDED:
            validation = "validated"
        elif export.state == JobState.FAILED:
            validation = "failed"
        elif export.state in {JobState.QUEUED, JobState.RUNNING}:
            validation = "processing"
        else:
            validation = "not_requested"

        if project.archived_at is not None:
            status = "archived"
        elif preview is not None and preview.state == JobState.FAILED:
            status = "needs_attention"
        elif preview is not None and preview.state in {JobState.QUEUED, JobState.RUNNING}:
            status = "preview_processing"
        elif preview_artifact is not None:
            status = "preview_ready"
        else:
            status = "draft"

        revision = (
            RevisionRepository(connection).get(project.active_revision_id)
            if project.active_revision_id is not None
            else None
        )
        activity_times = [project.updated_at, draft.updated_at]
        if preview is not None:
            activity_times.append(preview.finished_at or preview.started_at or preview.created_at)
        if export is not None:
            activity_times.append(export.finished_at or export.started_at or export.created_at)
        project_resource = _project_resource(project).model_copy(
            update={"updated_at": max(activity_times)}
        )
        return ProjectSummaryResource(
            project=project_resource,
            thumbnail_url=thumbnail_url,
            thumbnail_kind=thumbnail_kind,
            source_filename=asset.original_filename,
            source_width_px=asset.width_px,
            source_height_px=asset.height_px,
            canvas_width_mm=config.canvas.width_mm,
            canvas_height_mm=config.canvas.height_mm,
            color_count=len(config.palette.colors),
            current_revision_id=revision.id if revision is not None else None,
            current_revision_label=revision.label if revision is not None else None,
            status=status,
            validation=validation,
        )

    def start_preview(
        self,
        *,
        project_id: str,
        config: JobConfig,
        expected_draft_generation: int,
        manager: LocalWorkerManager,
        work_factory: PreviewWorkFactory,
    ) -> PreviewStartResponse:
        validated = self.profiles.validate_pinned(
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
        config = _normalize_printability_config(config, self.printability_profiles)
        connection = open_database(self.database_path)
        try:
            drafts = DraftRepository(connection)
            current = drafts.get(project_id)
            operations = () if current is None else current.operations
            source = AssetRepository(connection, self.blob_store).get(config.source_asset_id)
            previous_head = JobRepository(connection).current_head(
                project_id=project_id,
                supersession_key=f"preview:{project_id}",
            )
            baseline_job_id = (
                previous_head.id
                if previous_head is not None and previous_head.state == JobState.SUCCEEDED
                else None
            )
            draft = drafts.save(
                project_id=project_id,
                config=config,
                operations=operations,
                base_revision_id=None if current is None else current.base_revision_id,
                expected_generation=expected_draft_generation,
                validate_candidate=_validate_editor_sequence,
            )
        finally:
            connection.close()
        job = manager.submit(
            project_id=project_id,
            job_type=JobType.PREVIEW,
            initial_stage=JobStage.NORMALIZING,
            request_key=preview_derivation_key(
                config,
                validated.profile_catalog_fingerprint,
                _pinned_printability_catalog(config),
                operations,
                source_sha256=source.sha256,
            ),
            supersession_key=f"preview:{project_id}",
            work=work_factory(
                config,
                validated.profile_catalog_fingerprint,
                self.printability_profiles,
                operations,
                baseline_job_id,
            ),
        )
        return PreviewStartResponse(draft=_draft_resource(draft), job=job)

    def save_draft(
        self,
        *,
        project_id: str,
        config: JobConfig,
        operations: tuple[RegionOperation, ...],
        expected_draft_generation: int,
        history_command: Optional[DraftHistoryCommandRequest] = None,
    ) -> DraftResource:
        self._validate_config(config)
        config = _normalize_printability_config(config, self.printability_profiles)
        connection = open_database(self.database_path)
        try:
            drafts = DraftRepository(connection)
            current = drafts.get(project_id)
            draft = drafts.save(
                project_id=project_id,
                config=config,
                operations=operations,
                base_revision_id=None if current is None else current.base_revision_id,
                expected_generation=expected_draft_generation,
                validate_candidate=_validate_editor_sequence,
                history_command=(
                    None
                    if history_command is None
                    else DraftHistoryCommand(
                        request_id=history_command.id,
                        schema_version=history_command.schema_version,
                        command_type=history_command.command_type,
                        label=history_command.label,
                        before_state_sha256=history_command.before_state_sha256,
                        expected_cursor_node_id=history_command.expected_cursor_node_id,
                    )
                ),
            )
        finally:
            connection.close()
        return _draft_resource(draft)

    def move_draft_history(
        self,
        *,
        project_id: str,
        direction: str,
        request_id: str,
        expected_draft_generation: int,
        expected_cursor_node_id: str,
    ) -> DraftResource:
        connection = open_database(self.database_path)
        try:

            def validate_target(
                config: JobConfig, operations: tuple[RegionOperation, ...]
            ) -> object:
                self._validate_config(config)
                normalized = _normalize_printability_config(config, self.printability_profiles)
                if normalized.fingerprint() != config.fingerprint():
                    raise InvalidDraftHistoryTargetError(
                        "The stored history target requires configuration normalization."
                    )
                return _validate_editor_sequence(config, operations)

            draft = DraftRepository(connection).move_history(
                project_id=project_id,
                direction=direction,
                request_id=request_id,
                expected_generation=expected_draft_generation,
                expected_cursor_node_id=expected_cursor_node_id,
                validate_candidate=validate_target,
            )
            return _draft_resource(draft)
        finally:
            connection.close()

    def list_revisions(
        self, project_id: str, *, limit: int = 50, cursor: Optional[str] = None
    ) -> RevisionCollection:
        connection = open_database(self.database_path)
        try:
            before = _decode_revision_cursor(cursor) if cursor is not None else None
            connection.execute("BEGIN")
            project = ProjectRepository(connection).get(project_id)
            summaries, total, has_more = RevisionRepository(connection).list_summary_page(
                project_id, limit=limit, before=before
            )
            artifact_map = ArtifactRepository(connection).list_for_revisions(
                [item.revision.id for item in summaries]
            )
            items = tuple(
                _revision_summary_resource(
                    summary,
                    artifacts=artifact_map.get(summary.revision.id, ()),
                    active_revision_id=project.active_revision_id,
                )
                for summary in summaries
            )
            next_cursor = None
            if has_more and summaries:
                last = summaries[-1].revision
                next_cursor = _encode_revision_cursor(last.published_at, last.id)
            connection.commit()
            return RevisionCollection(items=items, total=total, next_cursor=next_cursor)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_revision(self, *, project_id: str, revision_id: str) -> RevisionResource:
        connection = open_database(self.database_path)
        try:
            ProjectRepository(connection).get(project_id)
            revision = RevisionRepository(connection).get(revision_id)
            if revision.project_id != project_id:
                raise RecordNotFoundError(f"revision not found: {revision_id}")
            return _revision_resource(revision, connection=connection)
        finally:
            connection.close()

    def publish_revision(
        self,
        *,
        project_id: str,
        expected_draft_generation: int,
        label: str,
        notes: str = "",
        preview_job_id: Optional[str] = None,
    ) -> PublishRevisionResponse:
        connection = open_database(self.database_path)
        try:
            draft = DraftRepository(connection).get(project_id)
            if draft is None:
                raise RecordNotFoundError(f"draft not found for project: {project_id}")
            config = load_job_config(dict(draft.config))
            validated = self._validate_config(config)
            normalized = _normalize_printability_config(config, self.printability_profiles)
            operations = draft.operations
            editor_sequence_sha256 = _validate_editor_sequence(normalized, operations).fingerprint()
            source = AssetRepository(connection, self.blob_store).get(normalized.source_asset_id)
            derivation = preview_derivation_identity(
                normalized,
                validated.profile_catalog_fingerprint,
                _pinned_printability_catalog(normalized),
                operations,
                source_sha256=source.sha256,
            )
            derivation_key = derivation.key()
            published = RevisionPublisher(connection, self.blob_store).publish_draft_and_continue(
                project_id=project_id,
                expected_generation=expected_draft_generation,
                expected_draft_state_sha256=draft_state_fingerprint(
                    draft.config_sha256,
                    draft.operations,
                    base_revision_id=draft.base_revision_id,
                ),
                publication_config=normalized,
                engine_version=__version__,
                editor_sequence_sha256=editor_sequence_sha256,
                expected_preview_derivation_key=derivation_key,
                expected_preview_derivation_metadata=derivation.metadata(),
                preview_job_id=preview_job_id,
                label=label,
                notes=notes,
            )
            if published.continuation_draft is None:  # pragma: no cover - repository invariant
                raise RuntimeError("publication did not return its continuation draft")
            if published.project is None:  # pragma: no cover - repository invariant
                raise RuntimeError("publication did not return its committed project state")
            return PublishRevisionResponse(
                revision=_revision_resource(published.revision, connection=connection),
                draft=_draft_resource(published.continuation_draft),
                project=_project_resource(published.project),
            )
        finally:
            connection.close()

    def branch_revision(
        self,
        *,
        project_id: str,
        revision_id: str,
        expected_draft_generation: int,
    ) -> BranchRevisionResponse:
        connection = open_database(self.database_path)
        try:
            publisher = RevisionPublisher(connection, self.blob_store)
            branched = publisher.branch_revision(
                project_id=project_id,
                revision_id=revision_id,
                expected_generation=expected_draft_generation,
                validate_replay=_validate_editor_sequence,
            )
            revision = RevisionRepository(connection).get(revision_id)
            return BranchRevisionResponse(
                base_revision=_revision_resource(revision, connection=connection),
                draft=_draft_resource(branched.draft),
                project=_project_resource(branched.project),
            )
        finally:
            connection.close()

    def auto_palette(
        self,
        *,
        project_id: str,
        config: JobConfig,
        color_count: int,
    ) -> AutoPaletteResource:
        validated = self._validate_config(config)
        connection = open_database(self.database_path)
        try:
            ProjectRepository(connection).get(project_id)
            asset = AssetRepository(connection, self.blob_store).get(config.source_asset_id)
        finally:
            connection.close()
        if not self.blob_store.verify(asset.stored_blob()):
            raise ValueError("normalized source image failed content verification")
        source_metadata = SourceImageMetadata.model_validate(asset.metadata)
        normalized_png = self.blob_store.path_for(asset.relative_path).read_bytes()
        rendered = render_preview(
            normalized_png,
            source_metadata=source_metadata,
            config=config,
            profile_catalog_fingerprint=validated.profile_catalog_fingerprint,
        )
        if any(color.locked for color in config.palette.colors[color_count:]):
            raise PaletteQuantizationError(
                PaletteErrorCode.INVALID_LOCK,
                "locked colors cannot be discarded when reducing the palette count",
            )
        locked = {
            index: color.hex
            for index, color in enumerate(config.palette.colors[:color_count])
            if color.locked
        }
        with Image.open(io.BytesIO(rendered.png_bytes), formats=["PNG"]) as encoded:
            encoded.load()
            working = encoded.convert("RGBA")
            try:
                fit = fit_auto_palette(working, color_count, locked_colors=locked)
            finally:
                working.close()
        return AutoPaletteResource(
            colors=fit.colors,
            iterations=fit.iterations,
            converged=fit.converged,
            sample_size=fit.sample_size,
            visible_pixel_count=fit.visible_pixel_count,
            unique_color_count=fit.unique_color_count,
            options_fingerprint=fit.options_fingerprint,
        )

    def _validate_config(self, config: JobConfig):
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

    def get_preview_result(self, job_id: str) -> PreviewJobResult:
        connection = open_database(self.database_path)
        try:
            job = JobRepository(connection).get(job_id)
            records = ArtifactRepository(connection).list_for_job(job_id)
        finally:
            connection.close()
        if job.type != JobType.PREVIEW:
            raise InvalidPreviewJobError(f"job {job_id} is not a preview job")
        artifacts = tuple(
            _artifact_resource(record, project_id=job.project_id) for record in records
        )
        statistics = None
        palette_metrics = None
        region_graph = None
        risk_report = None
        island_analysis = None
        clearance_analysis = None
        hole_analysis = None
        reprocessing_plan = None
        stats_record = next((item for item in records if item.kind == "preview-statistics"), None)
        if stats_record is not None:
            stats_blob = StoredBlob(
                sha256=stats_record.sha256,
                relative_path=stats_record.relative_path,
                byte_size=stats_record.byte_size,
                media_type=stats_record.media_type,
                extension=Path(stats_record.relative_path).suffix,
            )
            if not self.blob_store.verify(stats_blob):
                raise ValueError("preview statistics artifact failed verification")
            statistics = PreviewStatistics.model_validate_json(
                self.blob_store.path_for(stats_record.relative_path).read_bytes()
            )
        metrics_record = next((item for item in records if item.kind == "palette-metrics"), None)
        if metrics_record is not None:
            metrics_blob = StoredBlob(
                sha256=metrics_record.sha256,
                relative_path=metrics_record.relative_path,
                byte_size=metrics_record.byte_size,
                media_type=metrics_record.media_type,
                extension=Path(metrics_record.relative_path).suffix,
            )
            if not self.blob_store.verify(metrics_blob):
                raise ValueError("palette metrics artifact failed verification")
            palette_metrics = PaletteMetrics.model_validate_json(
                self.blob_store.path_for(metrics_record.relative_path).read_bytes()
            )
        graph_record = next((item for item in records if item.kind == "region-graph"), None)
        if graph_record is not None:
            graph_blob = StoredBlob(
                sha256=graph_record.sha256,
                relative_path=graph_record.relative_path,
                byte_size=graph_record.byte_size,
                media_type=graph_record.media_type,
                extension=Path(graph_record.relative_path).suffix,
            )
            if not self.blob_store.verify(graph_blob):
                raise ValueError("region graph artifact failed verification")
            region_graph = RegionGraph.model_validate_json(
                self.blob_store.path_for(graph_record.relative_path).read_bytes()
            )
        island_record = next((item for item in records if item.kind == "island-analysis"), None)
        if island_record is not None:
            island_blob = StoredBlob(
                sha256=island_record.sha256,
                relative_path=island_record.relative_path,
                byte_size=island_record.byte_size,
                media_type=island_record.media_type,
                extension=Path(island_record.relative_path).suffix,
            )
            if not self.blob_store.verify(island_blob):
                raise ValueError("island analysis artifact failed verification")
            island_analysis = IslandAnalysis.model_validate_json(
                self.blob_store.path_for(island_record.relative_path).read_bytes()
            )
            if region_graph is None:
                raise ValueError("island analysis is missing its preview region graph")
            validate_island_analysis_against_graph(island_analysis, region_graph)
        clearance_record = next(
            (item for item in records if item.kind == "clearance-analysis"), None
        )
        if clearance_record is not None:
            clearance_blob = StoredBlob(
                sha256=clearance_record.sha256,
                relative_path=clearance_record.relative_path,
                byte_size=clearance_record.byte_size,
                media_type=clearance_record.media_type,
                extension=Path(clearance_record.relative_path).suffix,
            )
            if not self.blob_store.verify(clearance_blob):
                raise ValueError("clearance analysis artifact failed verification")
            clearance_analysis = ClearanceAnalysis.model_validate_json(
                self.blob_store.path_for(clearance_record.relative_path).read_bytes()
            )
            if region_graph is None:
                raise ValueError("clearance analysis is missing its preview region graph")
            validate_clearance_analysis_against_graph(clearance_analysis, region_graph)
        hole_record = next((item for item in records if item.kind == "hole-analysis"), None)
        if hole_record is not None:
            hole_blob = StoredBlob(
                sha256=hole_record.sha256,
                relative_path=hole_record.relative_path,
                byte_size=hole_record.byte_size,
                media_type=hole_record.media_type,
                extension=Path(hole_record.relative_path).suffix,
            )
            if not self.blob_store.verify(hole_blob):
                raise ValueError("hole analysis artifact failed verification")
            hole_analysis = HoleAnalysis.model_validate_json(
                self.blob_store.path_for(hole_record.relative_path).read_bytes()
            )
            if region_graph is None:
                raise ValueError("hole analysis is missing its preview region graph")
            validate_hole_analysis_against_graph(hole_analysis, region_graph)
        risk_record = next((item for item in records if item.kind == "risk-report"), None)
        if risk_record is not None:
            risk_blob = StoredBlob(
                sha256=risk_record.sha256,
                relative_path=risk_record.relative_path,
                byte_size=risk_record.byte_size,
                media_type=risk_record.media_type,
                extension=Path(risk_record.relative_path).suffix,
            )
            if not self.blob_store.verify(risk_blob):
                raise ValueError("risk report artifact failed verification")
            risk_report = RiskReport.model_validate_json(
                self.blob_store.path_for(risk_record.relative_path).read_bytes()
            )
            if region_graph is None:
                raise ValueError("risk report is missing its preview region graph")
            validate_risk_report_against_graph(risk_report, region_graph)
        reprocessing_record = next(
            (item for item in records if item.kind == "reprocessing-plan"), None
        )
        if reprocessing_record is not None:
            reprocessing_blob = _stored_artifact_blob(reprocessing_record)
            if not self.blob_store.verify(reprocessing_blob):
                raise ValueError("reprocessing plan artifact failed verification")
            reprocessing_plan = ReprocessingPlan.model_validate_json(
                self.blob_store.path_for(reprocessing_record.relative_path).read_bytes()
            )
        if job.state == JobState.SUCCEEDED and statistics is None:
            raise ValueError("successful preview is missing its statistics artifact")
        return PreviewJobResult(
            job=job,
            artifacts=artifacts,
            statistics=statistics,
            palette_metrics=palette_metrics,
            region_graph=region_graph,
            risk_report=risk_report,
            island_analysis=island_analysis,
            clearance_analysis=clearance_analysis,
            hole_analysis=hole_analysis,
            reprocessing_plan=reprocessing_plan,
        )

    def _default_config(self, asset_id: str) -> JobConfig:
        setup = self.profiles.default_request("bambu-p2s")
        printer = self.profiles.catalog.printer(setup.printer_id)
        if printer is None:  # pragma: no cover - bundled default is catalog validated
            raise RuntimeError("bundled default printer disappeared")
        nozzle = printer.nozzle(setup.nozzle_id)
        if nozzle is None:  # pragma: no cover - bundled default is catalog validated
            raise RuntimeError("bundled default nozzle disappeared")
        cleanup = self.printability_profiles.resolve(
            ResolvePrintabilityRequest(
                printer_id=setup.printer_id,
                nozzle_id=setup.nozzle_id,
                material_class="pla",
            )
        )
        return JobConfig.model_validate(
            {
                "source_asset_id": asset_id,
                "canvas": {
                    "width_mm": setup.canvas_width_mm,
                    "height_mm": setup.canvas_height_mm,
                },
                "printer": {
                    "profile_catalog_id": self.profiles.catalog.catalog_id,
                    "profile_catalog_version": self.profiles.catalog.catalog_version,
                    "printer_id": setup.printer_id,
                    "nozzle_id": setup.nozzle_id,
                    "nozzle_mm": nozzle.diameter_mm,
                    "layer_height_mm": setup.layer_height_mm,
                    "plate_id": setup.plate_id,
                },
                "palette": {
                    "colors": [
                        {"id": "light", "name": "Light", "hex": "#CBC6B8"},
                        {"id": "dark", "name": "Dark", "hex": "#000000"},
                    ]
                },
                "cleanup": {
                    "min_island_mm2": cleanup.value("minimum_island_area_mm2").value,
                    "minimum_island_diameter_mm": cleanup.value("minimum_island_diameter_mm").value,
                    "max_hole_mm2": cleanup.value("maximum_tiny_hole_area_mm2").value,
                    "maximum_tiny_hole_diameter_mm": cleanup.value(
                        "maximum_tiny_hole_diameter_mm"
                    ).value,
                    "minimum_ring_width_mm": cleanup.value("minimum_ring_width_mm").value,
                    "minimum_line_width_mm": cleanup.value("minimum_line_width_mm").value,
                    "minimum_neck_width_mm": cleanup.value("minimum_neck_width_mm").value,
                    "minimum_gap_width_mm": cleanup.value("minimum_gap_width_mm").value,
                    "long_line_minimum_length_mm": cleanup.value(
                        "long_line_minimum_length_mm"
                    ).value,
                    "smoothing_radius_mm": cleanup.value("smoothing_radius_mm").value,
                    "merge_policy": "review",
                    "preserve_long_lines": True,
                    "printability_profile_id": cleanup.profile_id,
                    "printability_profile_catalog_fingerprint": (
                        cleanup.profile_catalog_fingerprint
                    ),
                    "override_fields": [],
                },
                "geometry": {
                    "base_thickness_mm": setup.base_thickness_mm,
                    "art_thickness_mm": setup.art_thickness_mm,
                },
            }
        )


def make_preview_work(
    config: JobConfig,
    profile_catalog_fingerprint: str,
    printability_profiles: PrintabilityProfileService,
    operations: tuple[RegionOperation, ...] = (),
    baseline_job_id: Optional[str] = None,
) -> JobWork:
    printability = _resolved_printability(config, printability_profiles)
    editor_commands = _load_editor_commands(operations)

    def work(context: JobContext) -> WorkerResult:
        context.report(stage=JobStage.NORMALIZING, progress=0.2)
        try:
            reprocessing_baseline = _load_reprocessing_baseline(context, baseline_job_id)
        except (KeyError, TypeError, ValueError):
            # Cache evidence is an optimization only. Malformed, stale, or incomplete evidence
            # must fail closed into the unconditional pipeline, never fail the user's preview.
            reprocessing_baseline = None
        connection = open_database(context.database_path)
        try:
            asset = AssetRepository(connection, context.blob_store).get(config.source_asset_id)
        finally:
            connection.close()
        if not context.blob_store.verify(asset.stored_blob()):
            raise ValueError("normalized source image failed content verification")
        source_metadata = SourceImageMetadata.model_validate(asset.metadata)
        if source_metadata.normalized_sha256 != asset.sha256:
            raise ValueError("source metadata hash does not match the normalized asset")
        normalized_png = context.blob_store.path_for(asset.relative_path).read_bytes()
        context.check_canceled()
        rendered = render_preview(
            normalized_png,
            source_metadata=source_metadata,
            config=config,
            profile_catalog_fingerprint=profile_catalog_fingerprint,
        )
        context.report(stage=JobStage.QUANTIZING, progress=0.35)
        with Image.open(io.BytesIO(rendered.png_bytes), formats=["PNG"]) as encoded_preview:
            encoded_preview.load()
            working_preview = encoded_preview.convert("RGBA")
        try:
            reprocessing_plan, reprocessing_affected_mask = _plan_reprocessing(
                baseline=reprocessing_baseline,
                config=config,
                editor_commands=editor_commands,
                transform=rendered.statistics.transform,
                width=working_preview.width,
                height=working_preview.height,
                dependencies={
                    "print_profile_catalog": profile_catalog_fingerprint,
                    "printability_catalog": printability.profile_catalog_fingerprint,
                },
            )
            island_options = IslandAnalysisOptions(
                minimum_area_mm2=config.cleanup.min_island_mm2,
                minimum_equivalent_diameter_mm=printability.value(
                    "minimum_island_diameter_mm"
                ).value,
                preserve_long_lines=config.cleanup.preserve_long_lines,
                long_line_minimum_length_mm=printability.value("long_line_minimum_length_mm").value,
            )
            if reprocessing_plan.mode == "scoped" and reprocessing_baseline is not None:
                context.report(stage=JobStage.CLEANING, progress=0.4)
                quantized = QuantizationResult(
                    palette=reprocessing_baseline.quantized_palette,
                    labels=reprocessing_baseline.quantized_labels,
                    alpha=reprocessing_baseline.quantized_alpha,
                    options_fingerprint=(reprocessing_baseline.quantization_options_fingerprint),
                )
                cleanup_analysis = analyze_regions(
                    reprocessing_baseline.cleanup_labels,
                    colors=dict(enumerate(quantized.palette)),
                    width_mm=config.canvas.width_mm,
                    height_mm=config.canvas.height_mm,
                    active=reprocessing_baseline.cleanup_active,
                )
                quantized_analysis = analyze_regions(
                    quantized.labels,
                    colors=dict(enumerate(quantized.palette)),
                    width_mm=config.canvas.width_mm,
                    height_mm=config.canvas.height_mm,
                    active=bytes(1 if value else 0 for value in quantized.alpha),
                )
                automatic_cleanup = AutomaticCleanupResult(
                    record=reprocessing_baseline.cleanup_record,
                    labels=reprocessing_baseline.cleanup_labels,
                    active=reprocessing_baseline.cleanup_active,
                    changed_mask=reprocessing_baseline.cleanup_changed_mask,
                    analysis=cleanup_analysis,
                    initial_island_analysis=classify_small_islands(
                        quantized_analysis,
                        options=island_options,
                    ),
                )
            else:
                quantized = classify_palette(
                    working_preview,
                    tuple(color.hex for color in config.palette.colors),
                )
                quantized_active = bytes(1 if value else 0 for value in quantized.alpha)
                context.report(stage=JobStage.CLEANING, progress=0.4)
                automatic_cleanup = apply_automatic_cleanup(
                    quantized.labels,
                    active=quantized_active,
                    colors=dict(enumerate(quantized.palette)),
                    width_mm=config.canvas.width_mm,
                    height_mm=config.canvas.height_mm,
                    island_options=island_options,
                    island_policy=_ISLAND_MERGE_POLICY[config.cleanup.merge_policy],
                    smoothing_radius_mm=config.cleanup.smoothing_radius_mm,
                )
            context.report(stage=JobStage.CLEANING, progress=0.48)
            replay = replay_editor_commands(
                automatic_cleanup.labels,
                active=automatic_cleanup.active,
                colors=dict(enumerate(quantized.palette)),
                width_mm=config.canvas.width_mm,
                height_mm=config.canvas.height_mm,
                config_fingerprint=config.fingerprint(),
                commands=editor_commands,
                transform=rendered.statistics.transform,
            )
            processed_alpha = bytes(255 if value else 0 for value in replay.active)
            processed = QuantizationResult(
                palette=quantized.palette,
                labels=replay.labels,
                alpha=processed_alpha,
                options_fingerprint=quantized.options_fingerprint,
                fit=quantized.fit,
            )
            processed_source = working_preview.copy()
            processed_alpha_image = Image.frombytes(
                "L",
                (processed.labels.width, processed.labels.height),
                processed_alpha,
            )
            try:
                processed_source.putalpha(processed_alpha_image)
                context.report(stage=JobStage.ANALYZING, progress=0.55)
                metrics = compute_palette_metrics(
                    processed_source,
                    processed,
                    config_sha256=config.fingerprint(),
                    width_mm=config.canvas.width_mm,
                    height_mm=config.canvas.height_mm,
                )
            finally:
                processed_alpha_image.close()
                processed_source.close()
            region_analysis = replay.analysis
            region_graph = region_analysis.graph
            context.report(stage=JobStage.ANALYZING, progress=0.6)
            island_analysis = classify_small_islands(
                region_analysis,
                options=IslandAnalysisOptions(
                    minimum_area_mm2=config.cleanup.min_island_mm2,
                    minimum_equivalent_diameter_mm=printability.value(
                        "minimum_island_diameter_mm"
                    ).value,
                    preserve_long_lines=config.cleanup.preserve_long_lines,
                    long_line_minimum_length_mm=printability.value(
                        "long_line_minimum_length_mm"
                    ).value,
                ),
            )
            context.report(stage=JobStage.ANALYZING, progress=0.65)
            clearance_analysis = analyze_clearance_features(
                region_analysis,
                max_workers=clearance_worker_count(region_analysis),
                check_canceled=context.check_canceled,
                options=ClearanceAnalysisOptions(
                    nozzle_mm=config.printer.nozzle_mm,
                    minimum_width_mm=printability.value("minimum_line_width_mm").value,
                    minimum_line_width_mm=printability.value("minimum_line_width_mm").value,
                    minimum_neck_width_mm=printability.value("minimum_neck_width_mm").value,
                    minimum_gap_width_mm=printability.value("minimum_gap_width_mm").value,
                    minimum_line_length_mm=printability.value("long_line_minimum_length_mm").value,
                ),
            )
            context.report(stage=JobStage.ANALYZING, progress=0.72)
            hole_analysis = classify_holes(
                region_analysis,
                options=HoleAnalysisOptions(
                    maximum_area_mm2=config.cleanup.max_hole_mm2,
                    maximum_equivalent_diameter_mm=printability.value(
                        "maximum_tiny_hole_diameter_mm"
                    ).value,
                    minimum_surviving_ring_width_mm=printability.value(
                        "minimum_ring_width_mm"
                    ).value,
                ),
            )
            context.report(stage=JobStage.ANALYZING, progress=0.82)
            risk_report = analyze_printability_risks(
                region_graph,
                options=RiskAnalysisOptions(nozzle_mm=config.printer.nozzle_mm),
                findings=(
                    *island_risk_findings(island_analysis, region_graph),
                    *clearance_risk_findings(clearance_analysis, region_analysis),
                    *hole_risk_findings(hole_analysis, region_graph),
                ),
                evaluated_codes=(
                    RiskCode.SMALL_ISLAND,
                    RiskCode.NARROW_NECK,
                    RiskCode.THIN_LINE,
                    RiskCode.NARROW_GAP,
                    RiskCode.TINY_HOLE,
                    RiskCode.HOLLOW_RING,
                ),
            )
        finally:
            working_preview.close()
        context.check_canceled()
        context.report(stage=JobStage.ANALYZING, progress=0.9)
        preview_blob = context.blob_store.put_bytes(
            rendered.png_bytes,
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        statistics_bytes = canonical_json(rendered.statistics.model_dump(mode="json")).encode(
            "utf-8"
        )
        statistics_blob = context.blob_store.put_bytes(
            statistics_bytes,
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        palette_preview_blob = context.blob_store.put_bytes(
            processed.png_bytes(),
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        quantized_preview_blob = context.blob_store.put_bytes(
            quantized.png_bytes(),
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        automatic_cleanup_blob = context.blob_store.put_bytes(
            automatic_cleanup.record.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        automatic_cleanup_mask_blob = context.blob_store.put_bytes(
            automatic_cleanup.changed_mask,
            namespace="artifacts",
            extension=".mask",
            media_type="application/octet-stream",
        )
        automatic_cleanup_mask_preview_blob = context.blob_store.put_bytes(
            _binary_mask_png(
                automatic_cleanup.changed_mask,
                width=automatic_cleanup.labels.width,
                height=automatic_cleanup.labels.height,
            ),
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        processed_labels_blob = context.blob_store.put_bytes(
            replay.labels.pixels,
            namespace="artifacts",
            extension=".labels",
            media_type="application/octet-stream",
        )
        processed_active_blob = context.blob_store.put_bytes(
            replay.active,
            namespace="artifacts",
            extension=".active",
            media_type="application/octet-stream",
        )
        quantized_labels_blob = context.blob_store.put_bytes(
            quantized.labels.pixels,
            namespace="artifacts",
            extension=".labels",
            media_type="application/octet-stream",
        )
        quantized_active_blob = context.blob_store.put_bytes(
            quantized.alpha,
            namespace="artifacts",
            extension=".alpha",
            media_type="application/octet-stream",
        )
        automatic_cleanup_labels_blob = context.blob_store.put_bytes(
            automatic_cleanup.labels.pixels,
            namespace="artifacts",
            extension=".labels",
            media_type="application/octet-stream",
        )
        automatic_cleanup_active_blob = context.blob_store.put_bytes(
            automatic_cleanup.active,
            namespace="artifacts",
            extension=".active",
            media_type="application/octet-stream",
        )
        reprocessing_plan_blob = context.blob_store.put_bytes(
            reprocessing_plan.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        reprocessing_mask_blob = context.blob_store.put_bytes(
            reprocessing_affected_mask,
            namespace="artifacts",
            extension=".mask",
            media_type="application/octet-stream",
        )
        reprocessing_mask_preview_blob = context.blob_store.put_bytes(
            _binary_mask_png(
                reprocessing_affected_mask,
                width=replay.labels.width,
                height=replay.labels.height,
            ),
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        changed_mask_blob = context.blob_store.put_bytes(
            replay.changed_mask,
            namespace="artifacts",
            extension=".mask",
            media_type="application/octet-stream",
        )
        protected_mask_blob = context.blob_store.put_bytes(
            replay.protected_mask,
            namespace="artifacts",
            extension=".mask",
            media_type="application/octet-stream",
        )
        replay_blob = context.blob_store.put_bytes(
            replay.record.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        metrics_blob = context.blob_store.put_bytes(
            metrics.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        region_graph_blob = context.blob_store.put_bytes(
            region_graph.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        region_assignment_blob = context.blob_store.put_bytes(
            region_analysis.assignment.astype("<i4", copy=False).tobytes(order="C"),
            namespace="artifacts",
            extension=".regions",
            media_type="application/octet-stream",
        )
        risk_mask, exact_risk_count, approximate_risk_count = _risk_severity_mask(
            region_analysis.assignment,
            region_graph,
            risk_report,
        )
        risk_mask_blob = context.blob_store.put_bytes(
            risk_mask,
            namespace="artifacts",
            extension=".risks",
            media_type="application/octet-stream",
        )
        island_analysis_blob = context.blob_store.put_bytes(
            island_analysis.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        clearance_analysis_blob = context.blob_store.put_bytes(
            clearance_analysis.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        hole_analysis_blob = context.blob_store.put_bytes(
            hole_analysis.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        risk_report_blob = context.blob_store.put_bytes(
            risk_report.canonical_json().encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        printability_blob = context.blob_store.put_bytes(
            canonical_json(printability.model_dump(mode="json")).encode("utf-8"),
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        context.check_canceled()
        derivation = preview_derivation_identity(
            config,
            profile_catalog_fingerprint,
            printability.profile_catalog_fingerprint,
            operations,
            source_sha256=asset.sha256,
        )
        derivation_key = derivation.key()
        return WorkerResult(
            artifacts=(
                WorkerArtifact(
                    kind="automatic-cleanup",
                    derivation_key=derivation_key,
                    blob=automatic_cleanup_blob,
                    metadata={
                        "schema_version": automatic_cleanup.record.schema_version,
                        "cleanup_fingerprint": automatic_cleanup.record.fingerprint(),
                        "island_policy": automatic_cleanup.record.island_policy.value,
                        "smoothing_radius_mm": automatic_cleanup.record.smoothing_radius_mm,
                        "changed_pixel_count": automatic_cleanup.record.changed_pixel_count,
                        "changed_mask_sha256": (automatic_cleanup.record.changed_mask_sha256),
                        "before_graph_fingerprint": (
                            automatic_cleanup.record.before_graph_fingerprint
                        ),
                        "after_graph_fingerprint": (
                            automatic_cleanup.record.after_graph_fingerprint
                        ),
                    },
                ),
                WorkerArtifact(
                    kind="automatic-cleanup-changed-mask",
                    derivation_key=derivation_key,
                    blob=automatic_cleanup_mask_blob,
                    metadata={
                        "schema_version": automatic_cleanup.record.schema_version,
                        "width": automatic_cleanup.labels.width,
                        "height": automatic_cleanup.labels.height,
                        "encoding": "uint8-binary-row-major",
                        "changed_pixel_count": automatic_cleanup.record.changed_pixel_count,
                        "mask_sha256": automatic_cleanup.record.changed_mask_sha256,
                    },
                ),
                WorkerArtifact(
                    kind="automatic-cleanup-active",
                    derivation_key=derivation_key,
                    blob=automatic_cleanup_active_blob,
                    metadata={
                        "width": automatic_cleanup.labels.width,
                        "height": automatic_cleanup.labels.height,
                        "encoding": "uint8-binary-row-major",
                    },
                ),
                WorkerArtifact(
                    kind="automatic-cleanup-labels",
                    derivation_key=derivation_key,
                    blob=automatic_cleanup_labels_blob,
                    metadata={
                        "width": automatic_cleanup.labels.width,
                        "height": automatic_cleanup.labels.height,
                        "encoding": "uint8-label-index-row-major",
                        "label_values": list(automatic_cleanup.labels.label_values),
                    },
                ),
                WorkerArtifact(
                    kind="automatic-cleanup-mask-preview-image",
                    derivation_key=derivation_key,
                    blob=automatic_cleanup_mask_preview_blob,
                    metadata={
                        "schema_version": automatic_cleanup.record.schema_version,
                        "width": automatic_cleanup.labels.width,
                        "height": automatic_cleanup.labels.height,
                        "encoding": "white-changed-black-unchanged",
                        "changed_pixel_count": automatic_cleanup.record.changed_pixel_count,
                        "mask_sha256": automatic_cleanup.record.changed_mask_sha256,
                    },
                ),
                WorkerArtifact(
                    kind="clearance-analysis",
                    derivation_key=derivation_key,
                    blob=clearance_analysis_blob,
                    metadata={
                        "schema_version": clearance_analysis.schema_version,
                        "analysis_fingerprint": clearance_analysis.fingerprint(),
                        "feature_count": clearance_analysis.summary.feature_count,
                        "thin_line_count": clearance_analysis.summary.thin_line_count,
                        "narrow_neck_count": clearance_analysis.summary.narrow_neck_count,
                        "narrow_gap_count": clearance_analysis.summary.narrow_gap_count,
                    },
                ),
                WorkerArtifact(
                    kind="editor-changed-mask",
                    derivation_key=derivation_key,
                    blob=changed_mask_blob,
                    metadata={
                        "schema_version": replay.record.schema_version,
                        "width": replay.labels.width,
                        "height": replay.labels.height,
                        "encoding": "uint8-binary-row-major",
                        "changed_pixel_count": replay.record.changed_pixel_count,
                        "mask_sha256": replay.record.changed_mask_sha256,
                    },
                ),
                WorkerArtifact(
                    kind="editor-protected-mask",
                    derivation_key=derivation_key,
                    blob=protected_mask_blob,
                    metadata={
                        "schema_version": replay.record.schema_version,
                        "width": replay.labels.width,
                        "height": replay.labels.height,
                        "encoding": "uint8-binary-row-major",
                        "protected_pixel_count": replay.record.protected_pixel_count,
                    },
                ),
                WorkerArtifact(
                    kind="editor-replay",
                    derivation_key=derivation_key,
                    blob=replay_blob,
                    metadata={
                        "schema_version": replay.record.schema_version,
                        "sequence_fingerprint": replay.record.sequence_fingerprint,
                        "replay_fingerprint": replay.record.fingerprint(),
                        "command_count": replay.record.command_count,
                        "changed_pixel_count": replay.record.changed_pixel_count,
                        "before_graph_fingerprint": replay.record.before_graph_fingerprint,
                        "after_graph_fingerprint": replay.record.after_graph_fingerprint,
                    },
                ),
                WorkerArtifact(
                    kind="hole-analysis",
                    derivation_key=derivation_key,
                    blob=hole_analysis_blob,
                    metadata={
                        "schema_version": hole_analysis.schema_version,
                        "analysis_fingerprint": hole_analysis.fingerprint(),
                        "feature_count": hole_analysis.summary.feature_count,
                        "tiny_hole_count": hole_analysis.summary.tiny_hole_count,
                        "hollow_ring_count": hole_analysis.summary.hollow_ring_count,
                        "transparent_center_count": (
                            hole_analysis.summary.transparent_center_count
                        ),
                    },
                ),
                WorkerArtifact(
                    kind="island-analysis",
                    derivation_key=derivation_key,
                    blob=island_analysis_blob,
                    metadata={
                        "schema_version": island_analysis.schema_version,
                        "analysis_fingerprint": island_analysis.fingerprint(),
                        "candidate_count": island_analysis.summary.candidate_count,
                        "risk_count": island_analysis.summary.risk_count,
                        "long_line_exempt_count": (island_analysis.summary.long_line_exempt_count),
                    },
                ),
                WorkerArtifact(
                    kind="palette-metrics",
                    derivation_key=derivation_key,
                    blob=metrics_blob,
                    metadata={
                        "schema_version": metrics.schema_version,
                        "metrics_fingerprint": metrics.fingerprint(),
                    },
                ),
                WorkerArtifact(
                    kind="palette-preview-image",
                    derivation_key=derivation_key,
                    blob=palette_preview_blob,
                    metadata={
                        "quantization_fingerprint": processed.fingerprint(),
                        "baseline_quantization_fingerprint": quantized.fingerprint(),
                        "automatic_cleanup_fingerprint": automatic_cleanup.record.fingerprint(),
                        "editor_replay_fingerprint": replay.record.fingerprint(),
                        "palette": list(processed.palette),
                    },
                ),
                WorkerArtifact(
                    kind="printability-settings",
                    derivation_key=derivation_key,
                    blob=printability_blob,
                    metadata={
                        "profile_id": printability.profile_id,
                        "profile_catalog_fingerprint": (printability.profile_catalog_fingerprint),
                        "evidence_status": printability.evidence_status.value,
                        "resolved_values": [
                            {
                                "name": item.name,
                                "value": item.value,
                                "unit": item.unit.value,
                                "source": item.source,
                                "profile_value": item.profile_value,
                            }
                            for item in printability.values
                        ],
                    },
                ),
                WorkerArtifact(
                    kind="processed-labels",
                    derivation_key=derivation_key,
                    blob=processed_labels_blob,
                    metadata={
                        "schema_version": replay.record.schema_version,
                        "width": replay.labels.width,
                        "height": replay.labels.height,
                        "encoding": "uint8-label-index-row-major",
                        "label_values": list(replay.labels.label_values),
                        "state_fingerprint": replay.record.final_state_fingerprint,
                        "quantization_fingerprint": processed.fingerprint(),
                        "automatic_cleanup_fingerprint": automatic_cleanup.record.fingerprint(),
                    },
                ),
                WorkerArtifact(
                    kind="quantized-preview-image",
                    derivation_key=derivation_key,
                    blob=quantized_preview_blob,
                    metadata={
                        "quantization_fingerprint": quantized.fingerprint(),
                        "palette": list(quantized.palette),
                    },
                ),
                WorkerArtifact(
                    kind="processed-active",
                    derivation_key=derivation_key,
                    blob=processed_active_blob,
                    metadata={
                        "width": replay.labels.width,
                        "height": replay.labels.height,
                        "encoding": "uint8-binary-row-major",
                    },
                ),
                WorkerArtifact(
                    kind="quantized-active",
                    derivation_key=derivation_key,
                    blob=quantized_active_blob,
                    metadata={
                        "width": quantized.labels.width,
                        "height": quantized.labels.height,
                        "encoding": "uint8-alpha-row-major",
                    },
                ),
                WorkerArtifact(
                    kind="quantized-labels",
                    derivation_key=derivation_key,
                    blob=quantized_labels_blob,
                    metadata={
                        "width": quantized.labels.width,
                        "height": quantized.labels.height,
                        "encoding": "uint8-label-index-row-major",
                        "label_values": list(quantized.labels.label_values),
                        "palette": list(quantized.palette),
                        "options_fingerprint": quantized.options_fingerprint,
                        "config_fingerprint": config.fingerprint(),
                        "component_fingerprints": _component_fingerprints(config),
                        **derivation.metadata(),
                    },
                ),
                WorkerArtifact(
                    kind="region-assignment",
                    derivation_key=derivation_key,
                    blob=region_assignment_blob,
                    metadata={
                        "schema_version": region_graph.schema_version,
                        "width": region_graph.width_px,
                        "height": region_graph.height_px,
                        "encoding": "int32-region-index-row-major-little-endian",
                        "inactive_index": -1,
                        "region_count": len(region_graph.regions),
                        "graph_fingerprint": region_graph.fingerprint(),
                    },
                ),
                WorkerArtifact(
                    kind="region-graph",
                    derivation_key=derivation_key,
                    blob=region_graph_blob,
                    metadata={
                        "schema_version": region_graph.schema_version,
                        "graph_fingerprint": region_graph.fingerprint(),
                        "region_count": len(region_graph.regions),
                        "adjacency_count": len(region_graph.adjacency),
                    },
                ),
                WorkerArtifact(
                    kind="risk-mask",
                    derivation_key=derivation_key,
                    blob=risk_mask_blob,
                    metadata={
                        "schema_version": 1,
                        "width": region_graph.width_px,
                        "height": region_graph.height_px,
                        "encoding": "uint8-risk-severity-bitset-row-major",
                        "severity_bits": {"info": 1, "warning": 2, "error": 4},
                        "exact_warning_count": exact_risk_count,
                        "approximate_warning_count": approximate_risk_count,
                        "source_sha256": asset.sha256,
                        "config_fingerprint": config.fingerprint(),
                        "operations_fingerprint": replay.record.sequence_fingerprint,
                        "assignment_sha256": region_assignment_blob.sha256,
                        "graph_fingerprint": region_graph.fingerprint(),
                        "risk_report_fingerprint": risk_report.fingerprint(),
                    },
                ),
                WorkerArtifact(
                    kind="risk-report",
                    derivation_key=derivation_key,
                    blob=risk_report_blob,
                    metadata={
                        "schema_version": risk_report.schema_version,
                        "report_fingerprint": risk_report.fingerprint(),
                        "warning_count": risk_report.summary.total,
                        "evaluated_codes": [code.value for code in risk_report.evaluated_codes],
                        "pending_codes": [code.value for code in risk_report.pending_codes],
                        "printability_profile_id": printability.profile_id,
                        "printability_profile_catalog_fingerprint": (
                            printability.profile_catalog_fingerprint
                        ),
                        "printability_evidence_status": printability.evidence_status.value,
                    },
                ),
                WorkerArtifact(
                    kind="preview-image",
                    derivation_key=derivation_key,
                    blob=preview_blob,
                    metadata={
                        "statistics_schema_version": rendered.statistics.schema_version,
                        "preview_sha256": rendered.statistics.preview_sha256,
                    },
                ),
                WorkerArtifact(
                    kind="reprocessing-affected-mask",
                    derivation_key=derivation_key,
                    blob=reprocessing_mask_blob,
                    metadata={
                        "schema_version": reprocessing_plan.schema_version,
                        "width": replay.labels.width,
                        "height": replay.labels.height,
                        "encoding": "uint8-binary-row-major",
                        "affected_pixel_count": reprocessing_plan.affected_pixel_count,
                        "mask_sha256": reprocessing_plan.affected_mask_sha256,
                    },
                ),
                WorkerArtifact(
                    kind="reprocessing-affected-mask-preview-image",
                    derivation_key=derivation_key,
                    blob=reprocessing_mask_preview_blob,
                    metadata={
                        "schema_version": reprocessing_plan.schema_version,
                        "width": replay.labels.width,
                        "height": replay.labels.height,
                        "encoding": "white-affected-black-unaffected",
                        "affected_pixel_count": reprocessing_plan.affected_pixel_count,
                        "mask_sha256": reprocessing_plan.affected_mask_sha256,
                    },
                ),
                WorkerArtifact(
                    kind="reprocessing-plan",
                    derivation_key=derivation_key,
                    blob=reprocessing_plan_blob,
                    metadata={
                        "schema_version": reprocessing_plan.schema_version,
                        "mode": reprocessing_plan.mode,
                        "reason": reprocessing_plan.reason.value,
                        "baseline_job_id": reprocessing_plan.baseline_job_id,
                        "reused_stages": list(reprocessing_plan.reused_stages),
                        "recomputed_stages": list(reprocessing_plan.recomputed_stages),
                        "affected_mask_sha256": reprocessing_plan.affected_mask_sha256,
                        "output_equivalence": reprocessing_plan.output_equivalence,
                    },
                ),
                WorkerArtifact(
                    kind="preview-statistics",
                    derivation_key=derivation_key,
                    blob=statistics_blob,
                    metadata={
                        "schema_version": rendered.statistics.schema_version,
                        **derivation.metadata(),
                    },
                ),
            )
        )

    return work


def _risk_severity_mask(
    assignment: np.ndarray,
    graph: RegionGraph,
    report: RiskReport,
) -> tuple[bytes, int, int]:
    """Encode exact region-backed risk severity in one byte per canonical pixel.

    Bits are cumulative so a pixel may carry findings at more than one severity. Findings
    identified only by physical bounds remain intentionally absent from this exact plane and
    are counted as approximate evidence for the UI's non-color dashed-bound presentation.
    """

    if assignment.shape != (graph.height_px, graph.width_px):
        raise ValueError("risk mask assignment dimensions do not match the region graph")
    if report.graph_fingerprint != graph.fingerprint():
        raise ValueError("risk mask report does not match the region graph")
    region_index = {region.id: index for index, region in enumerate(graph.regions)}
    label_indices: dict[int, list[int]] = {}
    for index, region in enumerate(graph.regions):
        label_indices.setdefault(region.label, []).append(index)
    severity_bits = {"info": 1, "warning": 2, "error": 4}
    region_bits = np.zeros(len(graph.regions), dtype=np.uint8)
    label_bits: dict[int, int] = {}
    exact_warning_count = 0
    approximate_warning_count = 0
    for warning in report.warnings:
        affected = {
            region_index[region_id]
            for region_id in warning.affected_region_ids
            if region_id in region_index
        }
        affected_labels = {label for label in warning.affected_labels if label in label_indices}
        if affected or affected_labels:
            exact_warning_count += 1
            bit = severity_bits[warning.severity.value]
            for index in affected:
                region_bits[index] |= bit
            # Label-wide findings can cover thousands of regions. Union their
            # severity bits first instead of visiting every region per finding.
            for label in affected_labels:
                label_bits[label] = label_bits.get(label, 0) | bit
        else:
            approximate_warning_count += 1
    for label, bits in label_bits.items():
        region_bits[label_indices[label]] |= bits
    pixels = np.zeros(assignment.shape, dtype=np.uint8)
    active = assignment >= 0
    pixels[active] = region_bits[assignment[active]]
    return pixels.tobytes(order="C"), exact_warning_count, approximate_warning_count


def _component_fingerprints(config: JobConfig) -> dict[str, str]:
    def fingerprint(value: object) -> str:
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

    return {
        "source": fingerprint({"source_asset_id": config.source_asset_id}),
        "crop": fingerprint(
            {
                "crop": config.crop.model_dump(mode="json"),
                "canvas": config.canvas.model_dump(mode="json"),
            }
        ),
        "palette": fingerprint(config.palette.model_dump(mode="json")),
        "cleanup": fingerprint(
            {
                "cleanup": config.cleanup.model_dump(mode="json"),
                "printer": config.printer.model_dump(mode="json"),
            }
        ),
    }


def _stored_artifact_blob(record: ArtifactRecord) -> StoredBlob:
    return StoredBlob(
        sha256=record.sha256,
        relative_path=record.relative_path,
        byte_size=record.byte_size,
        media_type=record.media_type,
        extension=Path(record.relative_path).suffix,
    )


def _load_reprocessing_baseline(
    context: JobContext,
    baseline_job_id: Optional[str],
) -> Optional[_ReprocessingBaseline]:
    if baseline_job_id is None:
        return None
    connection = open_database(context.database_path)
    try:
        job = JobRepository(connection).get(baseline_job_id)
        if job.state != JobState.SUCCEEDED or job.type != JobType.PREVIEW:
            return None
        records = {item.kind: item for item in ArtifactRepository(connection).list_for_job(job.id)}
    finally:
        connection.close()
    required = {
        "automatic-cleanup",
        "automatic-cleanup-active",
        "automatic-cleanup-changed-mask",
        "automatic-cleanup-labels",
        "editor-replay",
        "processed-active",
        "processed-labels",
        "quantized-active",
        "quantized-labels",
    }
    if not required.issubset(records):
        return None
    if any(
        not context.blob_store.verify(_stored_artifact_blob(records[kind])) for kind in required
    ):
        return None

    quantized_record = records["quantized-labels"]
    cleanup_record = records["automatic-cleanup-labels"]
    replay = EditorReplayRecord.model_validate_json(
        context.blob_store.path_for(records["editor-replay"].relative_path).read_bytes()
    )
    derivation = quantized_record.metadata.get("derivation_identity", {})
    component_fingerprints = quantized_record.metadata.get("component_fingerprints", {})
    if not isinstance(derivation, dict) or not isinstance(component_fingerprints, dict):
        return None
    width = int(quantized_record.metadata["width"])
    height = int(quantized_record.metadata["height"])
    label_values = tuple(int(item) for item in quantized_record.metadata["label_values"])
    cleanup_width = int(cleanup_record.metadata["width"])
    cleanup_height = int(cleanup_record.metadata["height"])
    cleanup_values = tuple(int(item) for item in cleanup_record.metadata["label_values"])

    def read(kind: str) -> bytes:
        return context.blob_store.path_for(records[kind].relative_path).read_bytes()

    return _ReprocessingBaseline(
        job_id=job.id,
        config_fingerprint=str(quantized_record.metadata["config_fingerprint"]),
        component_fingerprints={str(k): str(v) for k, v in component_fingerprints.items()},
        engine_version=str(derivation.get("engine_version", "")),
        adapter_versions={
            str(k): str(v) for k, v in dict(derivation.get("adapter_versions", {})).items()
        },
        dependencies={str(k): str(v) for k, v in dict(derivation.get("dependencies", {})).items()},
        command_fingerprints=tuple(step.command_fingerprint for step in replay.steps),
        quantized_labels=LabelField(
            width=width,
            height=height,
            label_values=label_values,
            pixels=read("quantized-labels"),
        ),
        quantized_alpha=read("quantized-active"),
        quantized_palette=tuple(str(item) for item in quantized_record.metadata["palette"]),
        quantization_options_fingerprint=str(quantized_record.metadata["options_fingerprint"]),
        cleanup_labels=LabelField(
            width=cleanup_width,
            height=cleanup_height,
            label_values=cleanup_values,
            pixels=read("automatic-cleanup-labels"),
        ),
        cleanup_active=read("automatic-cleanup-active"),
        cleanup_changed_mask=read("automatic-cleanup-changed-mask"),
        cleanup_record=AutomaticCleanupRecord.model_validate_json(read("automatic-cleanup")),
        processed_labels=read("processed-labels"),
        processed_active=read("processed-active"),
    )


def _plan_reprocessing(
    *,
    baseline: Optional[_ReprocessingBaseline],
    config: JobConfig,
    editor_commands: tuple,
    transform,
    width: int,
    height: int,
    dependencies: Optional[dict[str, str]] = None,
) -> tuple[ReprocessingPlan, bytes]:
    blank = empty_mask(width, height)
    full_stages = ("quantization", "automatic_cleanup", "editor_replay", "analysis")

    def full(reason: ReprocessingReason, message: str) -> tuple[ReprocessingPlan, bytes]:
        return (
            ReprocessingPlan(
                mode="full",
                reason=reason,
                message=message,
                baseline_job_id=None if baseline is None else baseline.job_id,
                recomputed_stages=full_stages,
                affected_pixel_count=0,
                affected_mask_sha256=mask_sha256(blank),
                output_equivalence="full_recompute",
            ),
            blank,
        )

    if baseline is None:
        return full(ReprocessingReason.NO_BASELINE, "No complete compatible preview cache exists.")
    if (
        baseline.engine_version != __version__
        or baseline.adapter_versions != PREVIEW_ADAPTER_VERSIONS
        or (dependencies is not None and baseline.dependencies != dependencies)
    ):
        return full(
            ReprocessingReason.ENGINE_CHANGED,
            "The engine or a processing adapter changed.",
        )
    current_components = _component_fingerprints(config)
    for component, reason, message in (
        ("source", ReprocessingReason.CROP_CHANGED, "The source image changed."),
        ("crop", ReprocessingReason.CROP_CHANGED, "The crop, canvas, or transform changed."),
        ("palette", ReprocessingReason.PALETTE_CHANGED, "The palette changed."),
        ("cleanup", ReprocessingReason.CLEANUP_CHANGED, "Cleanup or print settings changed."),
    ):
        if baseline.component_fingerprints.get(component) != current_components[component]:
            return full(reason, message)
    if baseline.config_fingerprint != config.fingerprint():
        return full(ReprocessingReason.CLEANUP_CHANGED, "An unscoped configuration value changed.")

    fingerprints = tuple(command.fingerprint() for command in editor_commands)
    prior = baseline.command_fingerprints
    if len(fingerprints) != len(prior) + 1 or fingerprints[: len(prior)] != prior:
        return full(
            ReprocessingReason.OPERATIONS_CHANGED,
            "Edits were removed, reordered, replaced, or appended as a non-local operation.",
        )
    command = editor_commands[-1]
    if not isinstance(command, LocalRasterEditCommand) or command.edit.kind not in {
        "fill",
        "color_cleanup",
    }:
        return full(ReprocessingReason.BOUNDARY_EDIT, "The appended edit can change topology.")
    try:
        selection = rasterize_canvas_selection(command.selector.selection, transform)
    except ValueError:
        return full(
            ReprocessingReason.BOUNDARY_EDIT,
            "The local selection requires graph-bound evidence and cannot be scoped safely.",
        )
    if not is_strictly_interior_selection(
        selection.mask,
        labels=baseline.processed_labels,
        active=baseline.processed_active,
        width=width,
        height=height,
    ):
        return full(
            ReprocessingReason.BOUNDARY_EDIT,
            "The edit reaches a region or transparency boundary, so topology is recomputed.",
        )
    return (
        ReprocessingPlan(
            mode="scoped",
            reason=ReprocessingReason.LOCAL_INTERIOR_EDIT,
            message=(
                "The appended local edit is strictly inside one active region; cached "
                "quantization and automatic cleanup are reused while replay and analysis rerun."
            ),
            baseline_job_id=baseline.job_id,
            reused_stages=("quantization", "automatic_cleanup"),
            recomputed_stages=("editor_replay", "analysis"),
            affected_pixel_count=selection.selected_pixel_count,
            affected_mask_sha256=mask_sha256(selection.mask),
            output_equivalence="by_construction",
        ),
        selection.mask,
    )


def preview_derivation_key(
    config: JobConfig,
    profile_catalog_fingerprint: str,
    calibration_fingerprint: str,
    operations: tuple[RegionOperation, ...] = (),
    *,
    source_sha256: Optional[str] = None,
) -> str:
    return preview_derivation_identity(
        config,
        profile_catalog_fingerprint,
        calibration_fingerprint,
        operations,
        source_sha256=source_sha256,
    ).key()


def preview_derivation_identity(
    config: JobConfig,
    profile_catalog_fingerprint: str,
    calibration_fingerprint: str,
    operations: tuple[RegionOperation, ...] = (),
    *,
    source_sha256: Optional[str] = None,
) -> DerivationIdentity:
    editor_sequence = _validate_editor_sequence(config, operations)
    return DerivationIdentity(
        pipeline="preview",
        source_fingerprint=source_sha256 or config.source_asset_id,
        config_fingerprint=config.fingerprint(),
        operations_fingerprint=editor_sequence.fingerprint(),
        engine_version=__version__,
        adapter_versions=PREVIEW_ADAPTER_VERSIONS,
        dependencies={
            "print_profile_catalog": profile_catalog_fingerprint,
            "printability_catalog": calibration_fingerprint,
        },
    )


def _binary_mask_png(mask: bytes, *, width: int, height: int) -> bytes:
    if width <= 0 or height <= 0 or len(mask) != width * height:
        raise ValueError("binary mask dimensions do not match its byte plane")
    image = Image.frombytes("L", (width, height), bytes(255 if value else 0 for value in mask))
    output = io.BytesIO()
    try:
        image.save(output, format="PNG", optimize=False, compress_level=9)
        return output.getvalue()
    finally:
        image.close()


def _load_editor_commands(
    operations: tuple[RegionOperation, ...],
):
    """Load raster commands while preserving unrelated typed draft history.

    Palette edits are already represented by the current config and therefore do not replay
    against labels. Every other operation must use the strict editor-command envelope; silently
    guessing at an older raster edit would make the preview non-reproducible.
    """

    _selection_commands(operations)
    return load_persisted_commands(_editor_operation_payloads(operations))


def _validate_editor_sequence(
    config: JobConfig,
    operations: tuple[RegionOperation, ...],
) -> EditorCommandSequence:
    config_fingerprint = config.fingerprint()
    conflicts = tuple(
        (command.command_id, command.selector.config_fingerprint)
        for command in _selection_commands(operations)
        if command.selector.config_fingerprint != config_fingerprint
    )
    if conflicts:
        raise IncompatibleSelectorError(
            expected_config_fingerprint=config_fingerprint,
            conflicts=conflicts,
            operation_count=len(operations),
        )
    return validate_persisted_sequence(
        _editor_operation_payloads(operations),
        config_fingerprint=config_fingerprint,
    )


def _selection_commands(operations: tuple[RegionOperation, ...]):
    return tuple(
        validate_canvas_selection_storage_payload(
            {
                "operation_type": operation.operation_type,
                "selection": dict(operation.selection),
                "parameters": dict(operation.parameters),
                "source": operation.source,
                "provenance": dict(operation.provenance),
            }
        )
        for operation in operations
        if operation.operation_type == CANVAS_SELECTION_STORAGE_OPERATION_TYPE
    )


def _editor_operation_payloads(
    operations: tuple[RegionOperation, ...],
) -> tuple[dict, ...]:
    payloads = tuple(
        {
            "operation_type": operation.operation_type,
            "selection": dict(operation.selection),
            "parameters": dict(operation.parameters),
            "source": operation.source,
            "provenance": dict(operation.provenance),
        }
        for operation in operations
        if operation.operation_type not in {"palette-edit", CANVAS_SELECTION_STORAGE_OPERATION_TYPE}
    )
    return payloads


def _resolved_printability(
    config: JobConfig,
    profiles: PrintabilityProfileService,
) -> ResolvedPrintabilitySettings:
    explicit = _explicit_cleanup_override_fields(config)
    override_values = {
        recommendation: getattr(config.cleanup, field.value)
        for field, recommendation in _CLEANUP_RECOMMENDATIONS.items()
        if field in explicit
    }
    pinned_catalog = (
        config.cleanup.printability_profile_catalog_fingerprint
        if config.cleanup.printability_profile_id is not None
        else None
    )
    return profiles.resolve(
        ResolvePrintabilityRequest(
            printer_id=config.printer.printer_id,
            nozzle_id=config.printer.nozzle_id,
            material_class="pla",
            overrides=PrintabilityOverrides.model_validate(override_values),
        ),
        catalog_fingerprint=pinned_catalog,
    )


def _normalize_printability_config(
    config: JobConfig,
    profiles: PrintabilityProfileService,
) -> JobConfig:
    resolved = _resolved_printability(config, profiles)
    explicit = tuple(sorted(_explicit_cleanup_override_fields(config), key=lambda item: item.value))
    resolved_fields = {
        field.value: resolved.value(recommendation).value
        for field, recommendation in _CLEANUP_RECOMMENDATIONS.items()
    }
    cleanup = config.cleanup.model_copy(
        update={
            **resolved_fields,
            "printability_profile_id": resolved.profile_id,
            "printability_profile_catalog_fingerprint": (resolved.profile_catalog_fingerprint),
            "override_fields": explicit,
        }
    )
    return config.model_copy(update={"cleanup": cleanup})


def _pinned_printability_catalog(config: JobConfig) -> str:
    fingerprint = config.cleanup.printability_profile_catalog_fingerprint
    if fingerprint is None:
        raise ValueError("normalized config lacks a printability catalog fingerprint")
    return fingerprint


def _explicit_cleanup_override_fields(config: JobConfig) -> set[CleanupOverrideField]:
    if config.cleanup.printability_profile_id is not None:
        return set(config.cleanup.override_fields)
    return {
        field for field in CleanupOverrideField if getattr(config.cleanup, field.value) is not None
    }


def _project_name(requested: Optional[str], filename: str) -> str:
    if requested is not None and requested.strip():
        return requested.strip()
    stem = Path(filename).stem.strip()
    return stem or "Untitled image"


def _workspace_resource(
    project: ProjectRecord,
    asset: Optional[AssetRecord],
    draft: Optional[DraftRecord],
    *,
    latest_preview_job: Optional[JobResource],
) -> ProjectWorkspaceResource:
    return ProjectWorkspaceResource(
        project=_project_resource(project),
        source_asset=_asset_resource(asset) if asset is not None else None,
        draft=_draft_resource(draft) if draft is not None else None,
        latest_preview_job=latest_preview_job,
    )


def _project_resource(project: ProjectRecord) -> ProjectResource:
    return ProjectResource(
        id=project.id,
        name=project.name,
        description=project.description,
        active_revision_id=project.active_revision_id,
        preferences=dict(project.preferences),
        created_at=project.created_at,
        updated_at=project.updated_at,
        archived_at=project.archived_at,
    )


def _asset_resource(asset: AssetRecord) -> ImageAssetResource:
    if asset.width_px is None or asset.height_px is None:
        raise ValueError("image asset is missing dimensions")
    return ImageAssetResource(
        id=asset.id,
        sha256=asset.sha256,
        media_type=asset.media_type,
        original_filename=asset.original_filename,
        byte_size=asset.byte_size,
        width_px=asset.width_px,
        height_px=asset.height_px,
        metadata=SourceImageMetadata.model_validate(asset.metadata),
        created_at=asset.created_at,
    )


def _draft_resource(draft: DraftRecord) -> DraftResource:
    config = load_job_config(dict(draft.config))
    if draft.history is None:
        raise RuntimeError("draft history was not initialized")
    return DraftResource(
        project_id=draft.project_id,
        base_revision_id=draft.base_revision_id,
        config=config,
        operations=tuple(
            {
                "operation_type": item.operation_type,
                "selection": dict(item.selection),
                "parameters": dict(item.parameters),
                "source": item.source,
                "provenance": dict(item.provenance),
            }
            for item in draft.operations
        ),
        config_sha256=draft.config_sha256,
        editor_sequence_sha256=_validate_editor_sequence(config, draft.operations).fingerprint(),
        generation=draft.generation,
        updated_at=draft.updated_at,
        history={
            "lineage_id": draft.history.lineage_id,
            "cursor_node_id": draft.history.cursor_node_id,
            "tip_node_id": draft.history.tip_node_id,
            "cursor": draft.history.cursor,
            "total": draft.history.total,
            "limit": draft.history.limit,
            "can_undo": draft.history.can_undo,
            "can_redo": draft.history.can_redo,
            "undo_label": draft.history.undo_label,
            "redo_label": draft.history.redo_label,
            "state_sha256": draft.history.state_sha256,
        },
    )


def _revision_summary_resource(
    summary: RevisionSummaryRecord,
    *,
    artifacts: tuple[ArtifactRecord, ...],
    active_revision_id: Optional[str],
) -> RevisionSummaryResource:
    revision = summary.revision
    evidence = _revision_summary_evidence_resource(
        summary,
        artifacts=artifacts,
    )
    return RevisionSummaryResource(
        id=revision.id,
        project_id=revision.project_id,
        parent_revision_id=revision.parent_revision_id,
        label=revision.label,
        config_sha256=revision.config_sha256,
        editor_sequence_sha256=evidence.editor_sequence_sha256,
        operation_count=summary.operation_count,
        artifact_count=summary.artifact_count,
        preview_evidence=evidence,
        is_active=revision.id == active_revision_id,
        published_at=revision.published_at,
    )


def _revision_summary_evidence_resource(
    summary: RevisionSummaryRecord,
    *,
    artifacts: tuple[ArtifactRecord, ...],
) -> PreviewEvidenceResource:
    revision = summary.revision
    publication = summary.publication
    if publication is None:
        return PreviewEvidenceResource(
            status="legacy",
            reason="This revision predates persisted preview publication evidence.",
            editor_sequence_sha256=_legacy_editor_sequence_sha256(
                revision, summary.operation_count
            ),
            artifact_count=summary.artifact_count,
        )
    _validate_revision_artifact_evidence(publication, artifacts)
    return _publication_evidence_resource(publication)


def _revision_resource(revision: RevisionRecord, *, connection) -> RevisionResource:
    config = load_job_config(dict(revision.config))
    operation_records = RegionOperationRepository(connection).list_for_revision(revision.id)
    operations = _revision_operations(operation_records)
    artifacts = ArtifactRepository(connection).list_for_revision(revision.id)
    evidence = _revision_evidence_resource(
        revision,
        operations=operations,
        artifacts=artifacts,
        publication=RevisionPublicationRepository(connection).get(revision.id),
    )
    return RevisionResource(
        id=revision.id,
        project_id=revision.project_id,
        source_asset_id=revision.source_asset_id,
        parent_revision_id=revision.parent_revision_id,
        schema_version=revision.schema_version,
        engine_version=revision.engine_version,
        config=config,
        config_sha256=revision.config_sha256,
        editor_sequence_sha256=evidence.editor_sequence_sha256,
        label=revision.label,
        notes=revision.notes,
        operations=tuple(
            {
                "operation_type": item.operation_type,
                "selection": dict(item.selection),
                "parameters": dict(item.parameters),
                "source": item.source,
                "provenance": dict(item.provenance),
            }
            for item in operations
        ),
        artifacts=tuple(
            _artifact_resource(item, project_id=revision.project_id) for item in artifacts
        ),
        preview_evidence=evidence,
        published_at=revision.published_at,
    )


def _revision_operations(records) -> tuple[RegionOperation, ...]:
    return tuple(
        RegionOperation(
            operation_type=item.operation_type,
            selection=item.selection,
            parameters=item.parameters,
            source=item.source,
            provenance=item.provenance,
        )
        for item in records
    )


def _revision_evidence_resource(
    revision: RevisionRecord,
    *,
    operations: tuple[RegionOperation, ...],
    artifacts: tuple[ArtifactRecord, ...],
    publication: Optional[RevisionPublicationRecord],
) -> PreviewEvidenceResource:
    if publication is None:
        sequence_sha256 = _legacy_editor_sequence_sha256(revision, len(operations))
        return PreviewEvidenceResource(
            status="legacy",
            reason="This revision predates persisted preview publication evidence.",
            editor_sequence_sha256=sequence_sha256,
            artifact_count=len(artifacts),
        )
    sequence_sha256 = _validate_editor_sequence(
        load_job_config(dict(revision.config)), operations
    ).fingerprint()
    if publication.editor_sequence_sha256 != sequence_sha256:
        raise ValueError("revision editor sequence fingerprint failed verification")
    _validate_revision_artifact_evidence(publication, artifacts)
    return _publication_evidence_resource(publication)


def _validate_revision_artifact_evidence(
    publication: RevisionPublicationRecord,
    artifacts: tuple[ArtifactRecord, ...],
) -> None:
    if publication.preview_status != "fresh":
        return
    if publication.artifact_count != len(artifacts):
        raise ValueError("revision artifact count does not match publication evidence")
    if publication.artifact_manifest_sha256 != artifact_manifest_sha256(artifacts):
        raise ValueError("revision artifact manifest does not match publication evidence")


def _publication_evidence_resource(
    publication: RevisionPublicationRecord,
) -> PreviewEvidenceResource:
    return PreviewEvidenceResource(
        status=publication.preview_status,
        preview_job_id=publication.preview_job_id,
        derivation_key=publication.preview_derivation_key,
        reason=publication.preview_reason,
        source_draft_generation=publication.source_draft_generation,
        editor_sequence_sha256=publication.editor_sequence_sha256,
        artifact_manifest_sha256=publication.artifact_manifest_sha256,
        artifact_count=publication.artifact_count,
    )


def _legacy_editor_sequence_sha256(revision: RevisionRecord, operation_count: int) -> str:
    material = canonical_json(
        {
            "namespace": "legacy-free-form-revision-history-v1",
            "revision_id": revision.id,
            "config_sha256": revision.config_sha256,
            "operation_count": operation_count,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _encode_revision_cursor(published_at: str, revision_id: str) -> str:
    payload = canonical_json({"published_at": published_at, "id": revision_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_revision_cursor(cursor: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        value = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidRevisionCursorError("revision cursor is invalid") from error
    if not isinstance(value, dict) or set(value) != {"published_at", "id"}:
        raise InvalidRevisionCursorError("revision cursor is invalid")
    published_at = value["published_at"]
    revision_id = value["id"]
    if not isinstance(published_at, str) or not published_at:
        raise InvalidRevisionCursorError("revision cursor is invalid")
    if not isinstance(revision_id, str) or not revision_id:
        raise InvalidRevisionCursorError("revision cursor is invalid")
    return published_at, revision_id


def _artifact_resource(artifact: ArtifactRecord, *, project_id: str) -> ArtifactResource:
    return ArtifactResource(
        id=artifact.id,
        job_id=artifact.job_id,
        revision_id=artifact.revision_id,
        kind=artifact.kind,
        sha256=artifact.sha256,
        derivation_key=artifact.derivation_key,
        media_type=artifact.media_type,
        byte_size=artifact.byte_size,
        metadata=dict(artifact.metadata),
        download_url=f"/api/projects/{project_id}/artifacts/{artifact.id}",
        created_at=artifact.created_at,
    )
