"""Tamper-evident physical calibration evidence bundles."""

from __future__ import annotations

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.
import hashlib
import io
import json
import stat
import zipfile
from collections.abc import Mapping
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal, Optional

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.calibration.artifact import (
    CalibrationArtifactManifest,
    CalibrationRunRecord,
    CalibrationRunStatus,
    validate_calibration_record,
)
from image23mf.calibration.models import PrintabilityProfile

EVIDENCE_BUNDLE_SCHEMA_VERSION = 1
EVIDENCE_MANIFEST_NAME = "calibration-evidence.json"
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_COUNT = 64
MAX_COMPRESSION_RATIO = 1000
ATTESTATION_STATEMENT = (
    "I personally observed this physical print and recorded these outcomes accurately."
)


class CalibrationEvidenceError(ValueError):
    """Evidence bytes or their declared identities are invalid."""


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CalibrationEvidenceRole(str, Enum):
    COUPON_SVG = "coupon_svg"
    COUPON_PNG = "coupon_png"
    PROJECT_3MF = "project_3mf"
    SLICED_GCODE = "sliced_gcode"
    SLICER_SETTINGS = "slicer_settings"
    PHOTO = "photo"


class CalibrationEvidenceMember(EvidenceModel):
    role: CalibrationEvidenceRole
    ordinal: int = Field(ge=0, le=31)
    archive_path: str = Field(min_length=1, max_length=300)
    filename: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0, le=MAX_MEMBER_BYTES)
    media_type: str = Field(min_length=3, max_length=120)
    extension: str = Field(pattern=r"^\.[a-z0-9]{1,8}$")

    @model_validator(mode="after")
    def identity_is_portable_and_role_compatible(self) -> CalibrationEvidenceMember:
        path = _portable_path(self.archive_path)
        if path.parts[0] != "payload" or len(path.parts) != 2:
            raise ValueError("calibration evidence members must be direct payload children")
        if path.name != self.filename:
            raise ValueError("calibration evidence filename must match its archive path")
        if PurePosixPath(self.filename).name != self.filename:
            raise ValueError("calibration evidence filename must not contain a path")
        if PurePosixPath(self.filename).suffix.lower() != self.extension:
            raise ValueError("calibration evidence extension does not match its filename")
        allowed = _ROLE_FORMATS[self.role]
        if (self.extension, self.media_type) not in allowed:
            raise ValueError(f"unsupported {self.role.value} evidence format")
        return self


class CalibrationAttestation(EvidenceModel):
    operator: str = Field(min_length=1, max_length=200)
    attested_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
    statement: Literal[
        "I personally observed this physical print and recorded these outcomes accurately."
    ] = ATTESTATION_STATEMENT


class CalibrationPinnedProfileEvidence(EvidenceModel):
    role: Literal["machine", "process", "filament"]
    name: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CalibrationSlicerEvidence(EvidenceModel):
    application: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=120)
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profiles: tuple[CalibrationPinnedProfileEvidence, ...] = Field(min_length=3, max_length=16)

    @model_validator(mode="after")
    def profiles_are_canonical_and_complete(self) -> CalibrationSlicerEvidence:
        positions = tuple((item.role, item.name, item.sha256) for item in self.profiles)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("slicer profiles must be unique and canonical")
        roles = {item.role for item in self.profiles}
        if not {"machine", "process", "filament"}.issubset(roles):
            raise ValueError("slicer evidence requires machine, process, and filament profiles")
        return self


class CalibrationFilamentEvidence(EvidenceModel):
    filament_id: Optional[str] = Field(default=None, max_length=120)
    manufacturer: str = Field(min_length=1, max_length=120)
    family: str = Field(default="", max_length=120)
    name: str = Field(min_length=1, max_length=160)
    material: str = Field(min_length=1, max_length=80)
    finish: str = Field(default="", max_length=80)
    color_hex: str = Field(pattern=r"^#[0-9A-F]{6}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def snapshot_hash_is_exact(self) -> CalibrationFilamentEvidence:
        payload = self.model_dump(mode="json", exclude={"snapshot_sha256"})
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.snapshot_sha256 != expected:
            raise ValueError("filament snapshot fingerprint is invalid")
        return self


class CalibrationEvidenceManifest(EvidenceModel):
    schema_version: Literal[1] = EVIDENCE_BUNDLE_SCHEMA_VERSION
    catalog_id: str = Field(min_length=1, max_length=120)
    catalog_version: str = Field(min_length=1, max_length=80)
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: PrintabilityProfile
    artifact: CalibrationArtifactManifest
    record: CalibrationRunRecord
    slicer: CalibrationSlicerEvidence
    filaments: tuple[CalibrationFilamentEvidence, ...] = Field(min_length=1, max_length=16)
    attestation: CalibrationAttestation
    members: tuple[CalibrationEvidenceMember, ...] = Field(min_length=6, max_length=64)

    @model_validator(mode="after")
    def evidence_is_complete_and_canonical(self) -> CalibrationEvidenceManifest:
        if self.record.status != CalibrationRunStatus.COMPLETED:
            raise ValueError("physical evidence requires a completed calibration record")
        if self.record.operator != self.attestation.operator:
            raise ValueError("calibration operator and attestation signer must match")
        if self.profile.id != self.artifact.profile_id or self.profile.id != self.record.profile_id:
            raise ValueError("calibration profile snapshot does not match artifact and record")
        profile_fingerprint = hashlib.sha256(
            json.dumps(
                self.profile.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if profile_fingerprint != self.artifact.profile_fingerprint:
            raise ValueError("calibration profile snapshot fingerprint is invalid")
        if (
            self.record.printer_id != self.profile.printer_id
            or self.record.nozzle_id != self.profile.nozzle_id
            or self.record.material_class != self.profile.material_class
        ):
            raise ValueError("calibration record setup does not match its profile")
        filament_positions = tuple(
            (
                item.manufacturer,
                item.family,
                item.name,
                item.material,
                item.snapshot_sha256,
            )
            for item in self.filaments
        )
        if filament_positions != tuple(sorted(set(filament_positions))):
            raise ValueError("calibration filament snapshots must be unique and canonical")
        filament_names = {item.name for item in self.filaments}
        if self.record.filament not in filament_names:
            raise ValueError("calibration record filament lacks an exact evidence snapshot")
        validate_calibration_record(self.record, self.artifact)
        positions = tuple((item.role.value, item.ordinal) for item in self.members)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("calibration evidence members must use canonical unique positions")
        paths = tuple(item.archive_path for item in self.members)
        if len(paths) != len(set(paths)):
            raise ValueError("calibration evidence archive paths must be unique")
        by_role = {
            role: tuple(item for item in self.members if item.role == role)
            for role in CalibrationEvidenceRole
        }
        for role in (
            CalibrationEvidenceRole.COUPON_SVG,
            CalibrationEvidenceRole.COUPON_PNG,
            CalibrationEvidenceRole.PROJECT_3MF,
            CalibrationEvidenceRole.SLICED_GCODE,
            CalibrationEvidenceRole.SLICER_SETTINGS,
        ):
            if len(by_role[role]) != 1 or by_role[role][0].ordinal != 0:
                raise ValueError(f"physical evidence requires exactly one {role.value} member")
        photos = by_role[CalibrationEvidenceRole.PHOTO]
        if not photos or tuple(item.ordinal for item in photos) != tuple(range(len(photos))):
            raise ValueError("physical evidence requires canonically numbered photos")
        if by_role[CalibrationEvidenceRole.COUPON_SVG][0].sha256 != self.artifact.svg_sha256:
            raise ValueError("coupon SVG does not match the calibration artifact")
        if by_role[CalibrationEvidenceRole.COUPON_PNG][0].sha256 != self.artifact.png_sha256:
            raise ValueError("coupon PNG does not match the calibration artifact")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ParsedCalibrationEvidence(EvidenceModel):
    manifest: CalibrationEvidenceManifest
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_byte_size: int = Field(gt=0)
    payloads: Mapping[str, bytes]


def build_calibration_evidence_bundle(
    manifest: CalibrationEvidenceManifest,
    payloads: Mapping[str, bytes],
) -> bytes:
    """Build a deterministic ZIP after verifying every declared payload."""

    _validate_payloads(manifest, payloads)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        _write_member(archive, EVIDENCE_MANIFEST_NAME, manifest.canonical_json().encode("utf-8"))
        for member in manifest.members:
            _write_member(archive, member.archive_path, payloads[member.archive_path])
    payload = output.getvalue()
    if len(payload) > MAX_BUNDLE_BYTES:
        raise CalibrationEvidenceError("calibration evidence bundle exceeds the size limit")
    return payload


def create_calibration_evidence_member(
    *,
    role: CalibrationEvidenceRole,
    ordinal: int,
    filename: str,
    media_type: str,
    payload: bytes,
) -> CalibrationEvidenceMember:
    """Create and validate one canonical evidence member from uploaded bytes."""

    extension = PurePosixPath(filename).suffix.lower()
    member = CalibrationEvidenceMember(
        role=role,
        ordinal=ordinal,
        archive_path=f"payload/{filename}",
        filename=filename,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        media_type=media_type,
        extension=extension,
    )
    _validate_role_payload(member, payload)
    return member


def parse_calibration_evidence_bundle(payload: bytes) -> ParsedCalibrationEvidence:
    """Parse a closed, safe ZIP and verify its canonical manifest and members."""

    if not payload or len(payload) > MAX_BUNDLE_BYTES:
        raise CalibrationEvidenceError("calibration evidence bundle size is invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            infos = _validated_infos(archive)
            manifest_info = infos.get(EVIDENCE_MANIFEST_NAME)
            if manifest_info is None:
                raise CalibrationEvidenceError("calibration evidence manifest is missing")
            if manifest_info.file_size > 4 * 1024 * 1024:
                raise CalibrationEvidenceError("calibration evidence manifest is too large")
            try:
                manifest = CalibrationEvidenceManifest.model_validate_json(
                    archive.read(manifest_info)
                )
            except Exception as error:
                raise CalibrationEvidenceError(
                    "calibration evidence manifest is invalid"
                ) from error
            expected = {EVIDENCE_MANIFEST_NAME, *(item.archive_path for item in manifest.members)}
            if set(infos) != expected:
                raise CalibrationEvidenceError("calibration evidence member set is not closed")
            payloads = {
                member.archive_path: archive.read(infos[member.archive_path])
                for member in manifest.members
            }
    except CalibrationEvidenceError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise CalibrationEvidenceError("calibration evidence is not a readable ZIP") from error
    _validate_payloads(manifest, payloads)
    return ParsedCalibrationEvidence(
        manifest=manifest,
        bundle_sha256=hashlib.sha256(payload).hexdigest(),
        bundle_byte_size=len(payload),
        payloads=payloads,
    )


def _validate_payloads(
    manifest: CalibrationEvidenceManifest,
    payloads: Mapping[str, bytes],
) -> None:
    expected = {item.archive_path for item in manifest.members}
    if set(payloads) != expected:
        raise CalibrationEvidenceError("calibration evidence payload set is not closed")
    for member in manifest.members:
        data = payloads[member.archive_path]
        if len(data) != member.byte_size:
            raise CalibrationEvidenceError(f"calibration evidence size mismatch: {member.filename}")
        if hashlib.sha256(data).hexdigest() != member.sha256:
            raise CalibrationEvidenceError(f"calibration evidence hash mismatch: {member.filename}")
        _validate_role_payload(member, data)


def _validate_role_payload(member: CalibrationEvidenceMember, data: bytes) -> None:
    if member.role == CalibrationEvidenceRole.COUPON_SVG:
        if b"<svg" not in data[:4096] or b"</svg>" not in data[-4096:]:
            raise CalibrationEvidenceError("calibration coupon SVG is not recognizable")
    elif member.role in {
        CalibrationEvidenceRole.COUPON_PNG,
        CalibrationEvidenceRole.PHOTO,
    }:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except (OSError, UnidentifiedImageError) as error:
            raise CalibrationEvidenceError(
                f"calibration image is not readable: {member.filename}"
            ) from error
    elif member.role == CalibrationEvidenceRole.PROJECT_3MF:
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as project:
                names = set(project.namelist())
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise CalibrationEvidenceError("calibration project is not a readable 3MF") from error
        if "[Content_Types].xml" not in names or not any(
            name.startswith("3D/") and name.endswith(".model") for name in names
        ):
            raise CalibrationEvidenceError("calibration project lacks required 3MF parts")
        try:
            for name in names:
                _portable_path(name)
        except ValueError as error:
            raise CalibrationEvidenceError("calibration project has an unsafe part path") from error
    elif member.role == CalibrationEvidenceRole.SLICED_GCODE:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CalibrationEvidenceError("calibration G-code is not UTF-8 text") from error
        if not any(
            line.lstrip().upper().startswith(("G0", "G1", "G28", "M")) for line in text.splitlines()
        ):
            raise CalibrationEvidenceError("calibration G-code contains no machine commands")
    elif member.role == CalibrationEvidenceRole.SLICER_SETTINGS:
        try:
            settings = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CalibrationEvidenceError(
                "calibration slicer settings are not valid JSON"
            ) from error
        if not isinstance(settings, dict) or not settings:
            raise CalibrationEvidenceError("calibration slicer settings must be a JSON object")


def _validated_infos(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_MEMBER_COUNT:
        raise CalibrationEvidenceError("calibration evidence member count is invalid")
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        try:
            name = _portable_path(info.filename).as_posix()
        except ValueError as error:
            raise CalibrationEvidenceError(
                "calibration evidence has an unsafe member path"
            ) from error
        if name in result:
            raise CalibrationEvidenceError("calibration evidence has duplicate members")
        if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
            raise CalibrationEvidenceError("calibration evidence cannot contain links/directories")
        if info.file_size <= 0 or info.file_size > MAX_MEMBER_BYTES:
            raise CalibrationEvidenceError("calibration evidence member size is invalid")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise CalibrationEvidenceError("calibration evidence expands beyond the size limit")
        if info.compress_size == 0 and info.file_size > 0:
            raise CalibrationEvidenceError("calibration evidence compression metadata is invalid")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise CalibrationEvidenceError("calibration evidence compression ratio is unsafe")
        result[name] = info
    return result


def _portable_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("calibration evidence paths must be normalized relative POSIX paths")
    return path


def _write_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    archive.writestr(info, payload)


_ROLE_FORMATS = {
    CalibrationEvidenceRole.COUPON_SVG: frozenset({(".svg", "image/svg+xml")}),
    CalibrationEvidenceRole.COUPON_PNG: frozenset({(".png", "image/png")}),
    CalibrationEvidenceRole.PROJECT_3MF: frozenset({(".3mf", "model/3mf")}),
    CalibrationEvidenceRole.SLICED_GCODE: frozenset(
        {(".gcode", "text/x-gcode"), (".gcode", "text/plain")}
    ),
    CalibrationEvidenceRole.SLICER_SETTINGS: frozenset({(".json", "application/json")}),
    CalibrationEvidenceRole.PHOTO: frozenset(
        {
            (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"),
            (".png", "image/png"),
            (".webp", "image/webp"),
        }
    ),
}
