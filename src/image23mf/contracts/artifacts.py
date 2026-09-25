"""Artifact integrity and lifecycle contracts."""

from __future__ import annotations

from typing import Literal, Optional

from image23mf.contracts.processing import ProcessingModel


class ArtifactInspectionResource(ProcessingModel):
    artifact_id: str
    kind: str
    integrity: Literal["verified", "missing", "corrupt"]
    immutable: bool
    regenerable: bool
    revealable: bool
    recovery_action: Optional[str] = None  # noqa: UP045 - runtime supports Python 3.9


class ArtifactDeletionResource(ProcessingModel):
    artifact_id: str
    deleted_record: bool
    deleted_blob: bool
    shared_blob_retained: bool
    recovery_action: str


class ArtifactRevealResponse(ProcessingModel):
    artifact_id: str
    supported: bool
    revealed: bool
    reason: Optional[str] = None  # noqa: UP045 - runtime supports Python 3.9
