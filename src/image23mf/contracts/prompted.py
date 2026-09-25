"""API contracts for durable, consent-bound prompted-edit alternatives."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from image23mf.contracts.editor import CanvasSelectionState
from image23mf.contracts.jobs import JobResource
from image23mf.contracts.provenance import ProviderRegionalEditProvenance
from image23mf.prompted_edits.models import (
    EgressDisclosure,
    ProviderAlternativeFailure,
    ProviderDescriptor,
)


class PromptedApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PromptedSessionStatus(str, Enum):
    PREPARED = "prepared"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    ACCEPTED = "accepted"


class PromptedAlternativeStatus(str, Enum):
    REVIEW = "review"
    REJECTED = "rejected"
    ACCEPTED = "accepted"


class PreparePromptedEditRequest(PromptedApiModel):
    parent_revision_id: str = Field(min_length=1, max_length=160)
    preview_job_id: str = Field(min_length=1, max_length=160)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,119}$")
    selection: CanvasSelectionState
    prompt: str = Field(min_length=1, max_length=40_000)
    reference_asset_ids: tuple[str, ...] = Field(default=(), max_length=64)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    seed: Optional[int] = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    alternative_count: int = Field(default=1, ge=1, le=16)

    @model_validator(mode="after")
    def references_are_unique(self) -> PreparePromptedEditRequest:
        if len(self.reference_asset_ids) != len(set(self.reference_asset_ids)):
            raise ValueError("prompted-edit reference asset IDs must be unique")
        return self


class PreparePromptedEditResponse(PromptedApiModel):
    session_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: ProviderDescriptor
    disclosure: EgressDisclosure
    status: Literal[PromptedSessionStatus.PREPARED] = PromptedSessionStatus.PREPARED


class ExecutePromptedEditRequest(PromptedApiModel):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disclosure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: Literal[True]


class PromptedAlternativeResource(PromptedApiModel):
    id: str
    index: int = Field(ge=0, le=15)
    status: PromptedAlternativeStatus
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_media_type: str
    output_url: str
    changed_mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_mask_url: str
    changed_mask_preview_url: str
    changed_pixel_count: int = Field(ge=0)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    provenance: ProviderRegionalEditProvenance


class PromptedSessionResource(PromptedApiModel):
    id: str
    project_id: str
    parent_revision_id: str
    retry_of_session_id: Optional[str] = None
    status: PromptedSessionStatus
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: ProviderDescriptor
    disclosure: EgressDisclosure
    prompt: str
    options: dict[str, JsonValue]
    seed: Optional[int]
    requested_alternative_count: int = Field(ge=1, le=16)
    alternatives: tuple[PromptedAlternativeResource, ...] = ()
    failures: tuple[ProviderAlternativeFailure, ...] = ()
    accepted_alternative_id: Optional[str] = None
    accepted_revision_id: Optional[str] = None
    created_at: str
    updated_at: str


class PromptedExecutionStart(PromptedApiModel):
    session: PromptedSessionResource
    job: JobResource


class RejectPromptedEditRequest(PromptedApiModel):
    reason: str = Field(default="Rejected during review.", min_length=1, max_length=500)


class AcceptPromptedAlternativeRequest(PromptedApiModel):
    expected_draft_generation: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=160)
    notes: str = Field(default="", max_length=5000)


class AcceptPromptedAlternativeResponse(PromptedApiModel):
    session: PromptedSessionResource
    revision_id: str
    draft_generation: int = Field(ge=1)


class PromptedAlternativeCollection(PromptedApiModel):
    items: tuple[PromptedSessionResource, ...]
