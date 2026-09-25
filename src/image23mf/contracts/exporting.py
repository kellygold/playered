"""Versioned durable 3MF export request and result resources."""

from __future__ import annotations

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.contracts.jobs import JobResource, JobState
from image23mf.contracts.processing import ArtifactResource


class ExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ExportMaterialMapping(ExportModel):
    material_id: str = Field(pattern=r"^material_[0-9a-f]{24}$")
    extruder: int = Field(ge=1, le=16)
    filament_type: Literal["PLA"] = "PLA"
    preset: Literal["Bambu PLA Basic", "Bambu PLA Matte"]


class ExportProfileSettings(ExportModel):
    printer_model: Literal["Bambu Lab P2S"] = "Bambu Lab P2S"
    nozzle_diameter_mm: Literal[0.2, 0.4]
    layer_height_mm: Literal[0.1, 0.2]
    bed_type: Literal["Textured PEI Plate"] = "Textured PEI Plate"

    @model_validator(mode="after")
    def layer_height_matches_measured_profile(self) -> ExportProfileSettings:
        expected = 0.1 if self.nozzle_diameter_mm == 0.2 else 0.2
        if self.layer_height_mm != expected:
            raise ValueError(
                f"{self.nozzle_diameter_mm:.1f} mm nozzle requires the measured "
                f"{expected:.1f} mm export layer profile"
            )
        return self


class StartExportRequest(ExportModel):
    schema_version: Literal[1] = 1
    geometry_artifact_id: str = Field(min_length=1, max_length=128)
    geometry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    material_mapping: tuple[ExportMaterialMapping, ...] = Field(default=(), max_length=16)
    profile: ExportProfileSettings
    minimum_part_thickness_mm: float = Field(gt=0, le=20)

    @model_validator(mode="after")
    def mapping_is_unique_and_canonical(self) -> StartExportRequest:
        material_ids = tuple(item.material_id for item in self.material_mapping)
        extruders = tuple(item.extruder for item in self.material_mapping)
        if len(set(material_ids)) != len(material_ids):
            raise ValueError("material mapping must contain each material exactly once")
        if extruders and extruders != tuple(range(1, len(extruders) + 1)):
            raise ValueError("material mapping must use contiguous ordered extruders starting at 1")
        return self


class ExportStartResponse(ExportModel):
    job: JobResource


class ExportJobResult(ExportModel):
    job: JobResource
    artifacts: tuple[ArtifactResource, ...]
    quality_report: Optional[ArtifactResource] = None
    validation_report: Optional[ArtifactResource] = None
    validation_log: Optional[ArtifactResource] = None
    package: Optional[ArtifactResource] = None
    download_ready: bool

    @model_validator(mode="after")
    def readiness_matches_verified_package(self) -> ExportJobResult:
        required = (
            self.quality_report is not None
            and self.validation_report is not None
            and self.validation_log is not None
        )
        complete = self.job.state == JobState.SUCCEEDED and self.package is not None and required
        if self.download_ready != complete:
            raise ValueError("download readiness requires a successful complete export bundle")
        return self
