"""Public API resources for physical calibration evidence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.calibration.artifact import CalibrationArtifactManifest
from image23mf.calibration.drafts import CalibrationDraftResource
from image23mf.calibration.evidence import (
    CalibrationEvidenceManifest,
    CalibrationEvidenceRole,
)


class CalibrationApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalibrationMemberApiResource(CalibrationApiModel):
    id: str
    role: CalibrationEvidenceRole
    ordinal: int = Field(ge=0)
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    media_type: str
    download_url: str


class CalibrationSourceBundleApiResource(CalibrationApiModel):
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    media_type: str
    download_url: str


class CalibrationArtifactApiResource(CalibrationApiModel):
    id: str
    catalog_id: str
    catalog_version: str
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_id: str
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: CalibrationArtifactManifest
    members: tuple[CalibrationMemberApiResource, ...]
    created_at: str


class CalibrationRunApiResource(CalibrationApiModel):
    id: str
    artifact: CalibrationArtifactApiResource
    catalog_id: str
    catalog_version: str
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_id: str
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    printer_id: str
    nozzle_id: str
    material_class: str
    process_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: CalibrationEvidenceManifest
    members: tuple[CalibrationMemberApiResource, ...]
    source_bundle: CalibrationSourceBundleApiResource
    imported_at: str
    integrity: str


class CalibrationRunCollection(CalibrationApiModel):
    items: tuple[CalibrationRunApiResource, ...]
    total: int = Field(ge=0)


class CalibrationImportApiResource(CalibrationApiModel):
    run: CalibrationRunApiResource
    duplicate: bool


class CalibrationDraftFinalizeApiResource(CalibrationApiModel):
    draft: CalibrationDraftResource
    run: CalibrationRunApiResource
    duplicate: bool


class CreateCalibrationProposalRequest(CalibrationApiModel):
    profile_id: str = Field(min_length=1, max_length=120)
    run_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    expected_catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def runs_are_canonical(self) -> CreateCalibrationProposalRequest:
        if self.run_ids != tuple(sorted(set(self.run_ids))):
            raise ValueError("run IDs must be unique and canonical")
        return self


class AcceptCalibrationProposalRequest(CalibrationApiModel):
    expected_catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class RejectCalibrationProposalRequest(CalibrationApiModel):
    reviewer: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
