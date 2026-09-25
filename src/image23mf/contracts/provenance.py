"""Strict provenance and secret-exclusion contracts for regional image edits."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from image23mf.contracts.editor import CanvasSelectionState, canonical_fingerprint


class ProvenanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ConsentEvent(ProvenanceModel):
    """Auditable approval for the exact remote-edit disclosure."""

    event_id: str = Field(pattern=r"^consent_[A-Za-z0-9_-]{8,80}$")
    recorded_at: datetime
    scope: Literal["regional_image_edit"] = "regional_image_edit"
    disclosure: str = Field(min_length=1, max_length=500)
    policy_version: str = Field(min_length=1, max_length=80)


class LocalRegionalEditProvenance(ProvenanceModel):
    schema_version: Literal[1] = 1
    execution_kind: Literal["local"] = "local"
    selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_revision_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    engine_id: str = Field(min_length=1, max_length=120)
    engine_version: str = Field(min_length=1, max_length=120)
    reproducibility: Literal["deterministic"] = "deterministic"
    reproducibility_reason: str = Field(min_length=1, max_length=500)


class ProviderRegionalEditProvenance(ProvenanceModel):
    schema_version: Literal[1] = 1
    execution_kind: Literal["provider"] = "provider"
    selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_revision_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=40_000)
    provider_id: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=200)
    model_version: str = Field(min_length=1, max_length=200)
    seed: Optional[int] = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    options: dict[str, Any] = Field(default_factory=dict)
    reference_sha256: tuple[str, ...] = Field(default=(), max_length=64)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consent_event: ConsentEvent
    reproducibility: Literal["exact_seeded", "best_effort"]
    reproducibility_reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def reproducibility_claim_is_supported(self) -> ProviderRegionalEditProvenance:
        if len(self.reference_sha256) != len(set(self.reference_sha256)):
            raise ValueError("reference hashes must be unique")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.reference_sha256):
            raise ValueError("reference hashes must be lowercase SHA-256 values")
        if self.reproducibility == "exact_seeded" and self.seed is None:
            raise ValueError("exact seeded provenance requires a recorded seed")
        return self


RegionalEditProvenance = Annotated[
    Union[LocalRegionalEditProvenance, ProviderRegionalEditProvenance],
    Field(discriminator="execution_kind"),
]
REGIONAL_EDIT_PROVENANCE_ADAPTER = TypeAdapter(RegionalEditProvenance)


_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "authtoken",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "privatekey",
        "credential",
        "credentials",
        "cookie",
        "setcookie",
        "signedurl",
    }
)
_SIGNED_QUERY_KEYS = frozenset(
    {
        "x-amz-signature",
        "x-goog-signature",
        "signature",
        "sig",
        "token",
        "access_token",
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk|ghp|github_pat|AIza)[-_A-Za-z0-9]{12,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _looks_like_signed_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.query:
        return False
    return any(key.lower() in _SIGNED_QUERY_KEYS for key, _ in parse_qsl(parsed.query))


def assert_no_sensitive_data(value: Any, *, path: str = "provenance") -> None:
    """Reject credentials and expiring signed references before durable storage."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _normalized_key(key_text) in _SENSITIVE_KEYS:
                raise ValueError(f"sensitive field is not allowed in {path}: {key_text}")
            assert_no_sensitive_data(item, path=f"{path}.{key_text}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            assert_no_sensitive_data(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
            raise ValueError(f"credential-like value is not allowed in {path}")
        if _looks_like_signed_url(value):
            raise ValueError(f"signed or tokenized URL is not allowed in {path}")


def validate_operation_provenance(
    *,
    selection: Mapping[str, Any],
    parameters: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> Optional[RegionalEditProvenance]:
    """Validate an operation envelope and return typed regional provenance when present."""

    assert_no_sensitive_data(selection, path="operation.selection")
    assert_no_sensitive_data(parameters, path="operation.parameters")
    assert_no_sensitive_data(provenance, path="operation.provenance")
    regional = provenance.get("regional_edit")
    if regional is None:
        return None
    typed = REGIONAL_EDIT_PROVENANCE_ADAPTER.validate_python(regional)
    embedded = selection.get("selection", selection)
    try:
        canonical_selection = CanvasSelectionState.model_validate(embedded)
    except Exception as error:
        raise ValueError(
            "regional edit provenance requires an embedded canonical canvas selection"
        ) from error
    if canonical_fingerprint(canonical_selection) != typed.selection_sha256:
        raise ValueError("regional edit selection fingerprint does not match its payload")
    return typed
