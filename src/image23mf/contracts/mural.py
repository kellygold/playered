"""API contracts for durable master-canvas mural planning and packaging."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.contracts.exporting import ExportMaterialMapping, ExportProfileSettings
from image23mf.contracts.jobs import JobResource, JobState
from image23mf.contracts.processing import ArtifactResource
from image23mf.mural.assembly_aids import MuralAssemblyAidsSettings
from image23mf.mural.planner import BedRectangle, MuralLayout, MuralPlan, MuralPlanRequest
from image23mf.mural.repository import MuralPlanRecord


class MuralContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MuralPlanSettings(MuralContract):
    processed_artifact_id: str = Field(min_length=1, max_length=160)
    layout: MuralLayout
    edge_clearance_mm: float = Field(default=0, ge=0, le=100)
    reserved_rectangles: tuple[BedRectangle, ...] = ()


class SaveMuralPlanRequest(MuralPlanSettings):
    expected_generation: int = Field(ge=0)


class MuralPlanPreviewResource(MuralContract):
    request: MuralPlanRequest
    plan: MuralPlan


class MuralPlanResource(MuralContract):
    project_id: str
    schema_version: int = Field(ge=1)
    generation: int = Field(ge=1)
    request: MuralPlanRequest
    plan: MuralPlan
    freshness: Literal["current", "stale"]
    stale_reason: Optional[str] = None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(
        cls,
        record: MuralPlanRecord,
        *,
        stale_reason: Optional[str] = None,
    ) -> "MuralPlanResource":
        return cls(
            project_id=record.project_id,
            schema_version=record.schema_version,
            generation=record.generation,
            request=record.request,
            plan=record.plan,
            freshness="stale" if stale_reason else "current",
            stale_reason=stale_reason,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class StartMuralBuildRequest(MuralContract):
    """Exact optimistic binding from a saved plan to immutable Geometry IR."""

    schema_version: Literal[1] = 1
    expected_plan_generation: int = Field(ge=1)
    expected_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry_artifact_id: str = Field(min_length=1, max_length=128)
    geometry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=120)
    material_mapping: tuple[ExportMaterialMapping, ...] = Field(default=(), max_length=16)
    profile: ExportProfileSettings
    assembly_aids: MuralAssemblyAidsSettings = Field(default_factory=MuralAssemblyAidsSettings)


class MuralBuildStartResponse(MuralContract):
    job: JobResource
    cache_hit: bool


class MuralValidationResource(MuralContract):
    status: Literal[
        "validated",
        "unavailable",
        "failed",
        "timed_out",
        "canceled",
        "profile_error",
        "invalid_artifact",
        "not_run",
    ]
    attempted: bool
    reason: Optional[str] = None
    executable: Optional[str] = None
    version: Optional[str] = None


class MuralBuildResult(MuralContract):
    job: JobResource
    freshness: Literal["current", "stale"]
    stale_reason: Optional[str] = None
    cache_hit: bool = False
    artifacts: tuple[ArtifactResource, ...]
    package: Optional[ArtifactResource] = None
    label_partition: Optional[ArtifactResource] = None
    topology_partition: Optional[ArtifactResource] = None
    seam_qa: Optional[ArtifactResource] = None
    validation_report: Optional[ArtifactResource] = None
    validation_log: Optional[ArtifactResource] = None
    assembly_aids: Optional[ArtifactResource] = None
    assembly_sheet: Optional[ArtifactResource] = None
    validation: MuralValidationResource
    download_ready: bool

    @model_validator(mode="after")
    def readiness_requires_current_complete_seam_safe_package(self) -> "MuralBuildResult":
        complete = (
            self.job.state == JobState.SUCCEEDED
            and self.freshness == "current"
            and self.package is not None
            and self.label_partition is not None
            and self.topology_partition is not None
            and self.seam_qa is not None
            and self.validation_report is not None
            and self.validation_log is not None
            and self.validation.status == "validated"
        )
        aids_requested = bool(
            self.package and self.package.metadata.get("assembly_aids_enabled", False)
        )
        aids_complete = self.assembly_aids is not None and self.assembly_sheet is not None
        if aids_requested != aids_complete:
            raise ValueError(
                "requested mural assembly aids require both metadata and printable sheet"
            )
        if self.download_ready != complete:
            raise ValueError(
                "mural download readiness requires current package, seam evidence, and a "
                "successful installed-slicer validation"
            )
        return self
