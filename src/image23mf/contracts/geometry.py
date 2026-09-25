"""Versioned project-scoped geometry generation resources."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.contracts.jobs import JobResource, JobState
from image23mf.contracts.processing import ArtifactResource


class GeometryApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class StartGeometryRequest(GeometryApiModel):
    schema_version: Literal[1] = 1
    preview_job_id: str = Field(min_length=1, max_length=128)
    expected_draft_generation: int = Field(ge=1)


class GeometryStartResponse(GeometryApiModel):
    job: JobResource


class GeometryJobResult(GeometryApiModel):
    job: JobResource
    artifacts: tuple[ArtifactResource, ...]
    geometry_svg: Optional[ArtifactResource] = None
    geometry_ir: Optional[ArtifactResource] = None
    geometry_mesh: Optional[ArtifactResource] = None
    geometry_report: Optional[ArtifactResource] = None
    geometry_preview: Optional[ArtifactResource] = None
    export_ready: bool

    @model_validator(mode="after")
    def readiness_requires_complete_verified_geometry(self) -> GeometryJobResult:
        complete = (
            self.job.state == JobState.SUCCEEDED
            and self.geometry_svg is not None
            and self.geometry_ir is not None
            and self.geometry_mesh is not None
            and self.geometry_report is not None
            and self.geometry_preview is not None
        )
        if self.export_ready != complete:
            raise ValueError("export readiness requires a successful complete geometry bundle")
        return self
