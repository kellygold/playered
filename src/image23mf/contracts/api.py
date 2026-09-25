"""Stable public HTTP envelopes and capability resources."""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    INCOMPATIBLE_EDITOR_HISTORY = "incompatible_editor_history"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    STALE_DRAFT = "stale_draft"
    INVALID_JOB_STATE = "invalid_job_state"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    BLOB_UNAVAILABLE = "blob_unavailable"
    UNSUPPORTED_MEDIA = "unsupported_media"
    RESOURCE_LIMIT = "resource_limit"
    INTERNAL_ERROR = "internal_error"


class ApiError(ApiModel):
    code: ApiErrorCode
    message: str
    request_id: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ApiErrorEnvelope(ApiModel):
    error: ApiError


class ToolCapability(ApiModel):
    id: str
    name: str
    detected: bool
    available: bool
    compatible: bool
    path: Optional[str] = None
    purpose: str
    version: Optional[str] = None
    unavailable_reason: Optional[str] = None


class CapabilitiesResponse(ApiModel):
    version: str
    tools: tuple[ToolCapability, ...]
    features: dict[str, bool]


class HealthResponse(ApiModel):
    status: str
    version: str
    workspace: str
    capabilities: tuple[ToolCapability, ...]
