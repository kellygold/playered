"""Immutable revision history and guarded publication API contracts."""

from typing import Literal, Optional

from pydantic import ConfigDict, Field, field_validator

from image23mf.contracts.job import JobConfig
from image23mf.contracts.processing import (
    ArtifactResource,
    DraftResource,
    ProcessingModel,
    ProjectResource,
    RegionOperationResource,
)


class RevisionRequestModel(ProcessingModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PreviewEvidenceResource(ProcessingModel):
    status: Literal["fresh", "missing", "stale", "legacy"]
    preview_job_id: Optional[str] = None
    derivation_key: Optional[str] = None
    reason: str
    source_draft_generation: Optional[int] = Field(default=None, ge=1)
    editor_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_manifest_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_count: int = Field(ge=0)


class RevisionSummaryResource(ProcessingModel):
    id: str
    project_id: str
    parent_revision_id: Optional[str]
    label: str
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    editor_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    preview_evidence: PreviewEvidenceResource
    is_active: bool
    published_at: str


class RevisionCollection(ProcessingModel):
    items: tuple[RevisionSummaryResource, ...]
    total: int = Field(ge=0)
    next_cursor: Optional[str] = None


class RevisionResource(ProcessingModel):
    id: str
    project_id: str
    source_asset_id: str
    parent_revision_id: Optional[str]
    schema_version: int = Field(ge=1)
    engine_version: str
    config: JobConfig
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    editor_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: str
    notes: str
    operations: tuple[RegionOperationResource, ...]
    artifacts: tuple[ArtifactResource, ...]
    preview_evidence: PreviewEvidenceResource
    published_at: str


class PublishRevisionRequest(RevisionRequestModel):
    label: str = Field(min_length=1, max_length=160)
    notes: str = Field(default="", max_length=4000)
    expected_draft_generation: int = Field(ge=1)
    preview_job_id: Optional[str] = Field(default=None, min_length=1, max_length=128)

    @field_validator("label")
    @classmethod
    def label_must_not_be_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("revision label cannot be blank")
        return value


class BranchRevisionRequest(RevisionRequestModel):
    expected_draft_generation: int = Field(ge=0)


class PublishRevisionResponse(ProcessingModel):
    revision: RevisionResource
    draft: DraftResource
    project: ProjectResource


class BranchRevisionResponse(ProcessingModel):
    base_revision: RevisionResource
    draft: DraftResource
    project: ProjectResource
