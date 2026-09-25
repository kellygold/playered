"""Provider-neutral contracts for consented regional image editing."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from image23mf.contracts.provenance import (
    ConsentEvent,
    ProviderRegionalEditProvenance,
    assert_no_sensitive_data,
)


class PromptedEditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PromptedEditAsset(PromptedEditModel):
    """Content-addressed in-memory bytes; paths and signed URLs never cross the boundary."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data: bytes = Field(min_length=1, max_length=100 * 1024 * 1024)

    @model_validator(mode="after")
    def hash_matches_bytes(self) -> PromptedEditAsset:
        if hashlib.sha256(self.data).hexdigest() != self.sha256:
            raise ValueError("prompted-edit asset hash does not match its bytes")
        return self

    @classmethod
    def from_bytes(cls, data: bytes, media_type: str) -> PromptedEditAsset:
        return cls(sha256=hashlib.sha256(data).hexdigest(), media_type=media_type, data=data)


class ProviderCapabilities(PromptedEditModel):
    supports_mask: bool = True
    supports_references: bool = False
    supports_multiple_alternatives: bool = False
    supports_seed: bool = False
    guarantees_seeded_replay: bool = False
    maximum_references: int = Field(default=0, ge=0, le=64)
    maximum_alternatives: int = Field(default=1, ge=1, le=16)
    accepted_media_types: tuple[str, ...] = ("image/png",)
    output_media_types: tuple[str, ...] = ("image/png",)

    @model_validator(mode="after")
    def capability_claims_are_consistent(self) -> ProviderCapabilities:
        if self.guarantees_seeded_replay and not self.supports_seed:
            raise ValueError("seeded replay guarantee requires seed support")
        if self.maximum_references and not self.supports_references:
            raise ValueError("reference limit requires reference support")
        if self.maximum_alternatives > 1 and not self.supports_multiple_alternatives:
            raise ValueError("multiple-alternative limit requires alternative support")
        if not self.accepted_media_types or not self.output_media_types:
            raise ValueError("provider must declare accepted and output media types")
        return self


class ProviderPrivacy(PromptedEditModel):
    policy_version: str = Field(min_length=1, max_length=80)
    data_residency: str = Field(min_length=1, max_length=160)
    retention: str = Field(min_length=1, max_length=300)
    training_use: Literal["none", "opt_out", "may_train", "unknown"]
    subprocessors: tuple[str, ...] = Field(default=(), max_length=32)
    policy_url: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def metadata_excludes_sensitive_references(self) -> ProviderPrivacy:
        assert_no_sensitive_data(self.model_dump(mode="json"), path="provider.privacy")
        return self


class ProviderDescriptor(PromptedEditModel):
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,119}$")
    display_name: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=200)
    model_version: str = Field(min_length=1, max_length=200)
    capabilities: ProviderCapabilities
    privacy: ProviderPrivacy


class PromptedEditRequest(PromptedEditModel):
    project_id: str = Field(min_length=1, max_length=160)
    parent_revision_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: PromptedEditAsset
    mask: PromptedEditAsset
    prompt: str = Field(min_length=1, max_length=40_000)
    references: tuple[PromptedEditAsset, ...] = Field(default=(), max_length=64)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    seed: Optional[int] = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    alternative_count: int = Field(default=1, ge=1, le=16)

    @model_validator(mode="after")
    def request_is_safe_and_unambiguous(self) -> PromptedEditRequest:
        if self.mask.media_type != "image/png":
            raise ValueError("regional edit masks must use lossless PNG")
        reference_hashes = tuple(item.sha256 for item in self.references)
        if len(reference_hashes) != len(set(reference_hashes)):
            raise ValueError("prompted-edit references must be content-unique")
        assert_no_sensitive_data(self.prompt, path="prompted_edit.prompt")
        assert_no_sensitive_data(self.options, path="prompted_edit.options")
        return self


class ProviderEditRequest(PromptedEditModel):
    """The only payload a provider adapter may receive after consent."""

    source: PromptedEditAsset
    mask: PromptedEditAsset
    prompt: str
    references: tuple[PromptedEditAsset, ...]
    options: dict[str, JsonValue]
    seed: Optional[int]
    alternative_count: int


class ProviderAlternative(PromptedEditModel):
    index: int = Field(ge=0, le=15)
    output: PromptedEditAsset
    seed: Optional[int] = Field(default=None, ge=0, le=9_223_372_036_854_775_807)


class ProviderAlternativeFailure(PromptedEditModel):
    index: int = Field(ge=0, le=15)
    code: Literal["filtered", "generation_failed", "invalid_output", "unavailable"]
    retryable: bool = False


class ProviderEditResponse(PromptedEditModel):
    alternatives: tuple[ProviderAlternative, ...] = Field(default=(), max_length=16)
    failures: tuple[ProviderAlternativeFailure, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def positions_are_complete_and_unique(self) -> ProviderEditResponse:
        positions = [item.index for item in (*self.alternatives, *self.failures)]
        if not positions:
            raise ValueError("provider response must contain an alternative or failure")
        if len(positions) != len(set(positions)):
            raise ValueError("provider response positions must be unique")
        return self


class EgressDisclosure(PromptedEditModel):
    schema_version: Literal[1] = 1
    disclosure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str
    provider: ProviderDescriptor
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_sha256: tuple[str, ...]
    prompt: str
    options: dict[str, JsonValue]
    data_categories: tuple[
        Literal["source_pixels", "selection_mask", "prompt", "references", "generation_options"],
        ...,
    ]


class ConsentDecision(PromptedEditModel):
    granted: bool
    event_id: Optional[str] = Field(default=None, pattern=r"^consent_[A-Za-z0-9_-]{8,80}$")
    recorded_at: Optional[datetime] = None
    denial_reason: Optional[Literal["user_denied", "policy_denied", "cancelled"]] = None

    @model_validator(mode="after")
    def decision_is_explicit(self) -> ConsentDecision:
        if self.granted:
            if self.event_id is None or self.recorded_at is None or self.denial_reason is not None:
                raise ValueError("granted consent requires event identity and time only")
        elif (
            self.denial_reason is None or self.event_id is not None or self.recorded_at is not None
        ):
            raise ValueError("denied consent requires a denial reason only")
        return self


class PromptedEditAlternative(PromptedEditModel):
    index: int
    output: PromptedEditAsset
    provenance: ProviderRegionalEditProvenance


class PromptedEditExecution(PromptedEditModel):
    status: Literal["complete", "partial", "failed"]
    provider: ProviderDescriptor
    disclosure: EgressDisclosure
    consent_event: ConsentEvent
    alternatives: tuple[PromptedEditAlternative, ...]
    failures: tuple[ProviderAlternativeFailure, ...]

    @model_validator(mode="after")
    def status_matches_results(self) -> PromptedEditExecution:
        expected = (
            "partial"
            if self.alternatives and self.failures
            else "complete"
            if self.alternatives
            else "failed"
        )
        if self.status != expected:
            raise ValueError("prompted-edit status does not match its alternatives")
        return self


def disclosure_fingerprint(value: dict[str, Any]) -> str:
    import json

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
