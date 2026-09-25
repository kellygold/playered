"""Typed project import, workspace, preview, and statistics resources."""

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from image23mf.contracts.job import JobConfig
from image23mf.contracts.jobs import JobResource
from image23mf.contracts.provenance import validate_operation_provenance
from image23mf.engine.clearance import ClearanceAnalysis
from image23mf.engine.holes import HoleAnalysis
from image23mf.engine.ingestion import SourceImageMetadata
from image23mf.engine.islands import IslandAnalysis
from image23mf.engine.palette_metrics import PaletteMetrics
from image23mf.engine.regions import RegionGraph
from image23mf.engine.risks import RiskReport
from image23mf.engine.transform import CanonicalTransform
from image23mf.invalidation import ReprocessingPlan

PREVIEW_STATISTICS_SCHEMA_VERSION = 1


class ProcessingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PixelDimensions(ProcessingModel):
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class PhysicalDimensions(ProcessingModel):
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    mm_per_pixel_x: float = Field(gt=0)
    mm_per_pixel_y: float = Field(gt=0)


class AlphaStatistics(ProcessingModel):
    opaque_pixels: int = Field(ge=0)
    translucent_pixels: int = Field(ge=0)
    transparent_pixels: int = Field(ge=0)

    @property
    def pixel_count(self) -> int:
        return self.opaque_pixels + self.translucent_pixels + self.transparent_pixels


class PreviewStatistics(ProcessingModel):
    schema_version: Literal[1] = PREVIEW_STATISTICS_SCHEMA_VERSION
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_asset_id: str
    source: PixelDimensions
    preview: PixelDimensions
    physical: PhysicalDimensions
    alpha: AlphaStatistics
    transform: CanonicalTransform
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectResource(ProcessingModel):
    id: str
    name: str
    description: str
    active_revision_id: Optional[str]
    preferences: dict[str, Any]
    created_at: str
    updated_at: str
    archived_at: Optional[str]


class ImageAssetResource(ProcessingModel):
    id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    original_filename: str
    byte_size: int = Field(ge=0)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    metadata: SourceImageMetadata
    created_at: str


class DraftHistoryResource(ProcessingModel):
    lineage_id: str
    cursor_node_id: str
    tip_node_id: str
    cursor: int = Field(ge=0)
    total: int = Field(ge=0)
    limit: int = Field(gt=0)
    can_undo: bool
    can_redo: bool
    undo_label: Optional[str] = None
    redo_label: Optional[str] = None
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DraftResource(ProcessingModel):
    project_id: str
    base_revision_id: Optional[str]
    config: JobConfig
    operations: tuple["RegionOperationResource", ...] = ()
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    editor_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1)
    updated_at: str
    history: DraftHistoryResource


class ProjectWorkspaceResource(ProcessingModel):
    project: ProjectResource
    source_asset: Optional[ImageAssetResource]
    draft: Optional[DraftResource]
    latest_preview_job: Optional[JobResource]


class ProjectSummaryResource(ProcessingModel):
    project: ProjectResource
    thumbnail_url: str
    thumbnail_kind: Literal["preview", "source"]
    source_filename: str
    source_width_px: int = Field(gt=0)
    source_height_px: int = Field(gt=0)
    canvas_width_mm: float = Field(gt=0)
    canvas_height_mm: float = Field(gt=0)
    color_count: int = Field(ge=0)
    current_revision_id: Optional[str] = None
    current_revision_label: Optional[str] = None
    status: Literal[
        "archived",
        "draft",
        "preview_processing",
        "preview_ready",
        "needs_attention",
    ]
    validation: Literal[
        "not_requested",
        "processing",
        "validated",
        "failed",
    ]


class ProjectSummaryCollection(ProcessingModel):
    items: tuple[ProjectSummaryResource, ...]
    total: int = Field(ge=0)


class StartPreviewRequest(ProcessingModel):
    config: JobConfig
    expected_draft_generation: int = Field(ge=1)


class RegionOperationResource(ProcessingModel):
    operation_type: str = Field(min_length=1, max_length=120)
    selection: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    source: Literal["automatic", "manual", "model"] = "manual"
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def durable_payload_excludes_secrets(self) -> "RegionOperationResource":
        validate_operation_provenance(
            selection=self.selection,
            parameters=self.parameters,
            provenance=self.provenance,
        )
        return self


class SaveDraftRequest(ProcessingModel):
    config: JobConfig
    operations: tuple[RegionOperationResource, ...] = ()
    expected_draft_generation: int = Field(ge=1)
    history_command: Optional["DraftHistoryCommandRequest"] = None


class DraftHistoryCommandRequest(ProcessingModel):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=36, max_length=36)
    command_type: Literal[
        "config_change",
        "palette_change",
        "manual_operation",
        "clear_manual_and_apply",
    ]
    label: str = Field(min_length=1, max_length=120)
    before_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_cursor_node_id: str = Field(min_length=1, max_length=128)

    @field_validator("id")
    @classmethod
    def id_is_uuid(cls, value: str) -> str:
        return str(UUID(value))


class DraftHistoryMutationRequest(ProcessingModel):
    request_id: str = Field(min_length=36, max_length=36)
    expected_draft_generation: int = Field(ge=1)
    expected_cursor_node_id: str = Field(min_length=1, max_length=128)

    @field_validator("request_id")
    @classmethod
    def request_id_is_uuid(cls, value: str) -> str:
        return str(UUID(value))


class AutoPaletteRequest(ProcessingModel):
    config: JobConfig
    color_count: int = Field(ge=2, le=8)


class AutoPaletteResource(ProcessingModel):
    colors: tuple[str, ...]
    iterations: int = Field(ge=1)
    converged: bool
    sample_size: int = Field(ge=1)
    visible_pixel_count: int = Field(ge=1)
    unique_color_count: int = Field(ge=1)
    options_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class PreviewStartResponse(ProcessingModel):
    draft: DraftResource
    job: JobResource


class ArtifactResource(ProcessingModel):
    id: str
    job_id: Optional[str]
    revision_id: Optional[str]
    kind: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    derivation_key: str
    media_type: str
    byte_size: int = Field(ge=0)
    metadata: dict[str, Any]
    download_url: str
    created_at: str


class PreviewJobResult(ProcessingModel):
    job: JobResource
    artifacts: tuple[ArtifactResource, ...]
    statistics: Optional[PreviewStatistics]
    palette_metrics: Optional[PaletteMetrics] = None
    region_graph: Optional[RegionGraph] = None
    risk_report: Optional[RiskReport] = None
    island_analysis: Optional[IslandAnalysis] = None
    clearance_analysis: Optional[ClearanceAnalysis] = None
    hole_analysis: Optional[HoleAnalysis] = None
    reprocessing_plan: Optional[ReprocessingPlan] = None
