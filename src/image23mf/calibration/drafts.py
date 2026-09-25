"""Restart-safe assembly of physical calibration evidence."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from image23mf.calibration.artifact import (
    CalibrationArtifactManifest,
    CalibrationOutcome,
    CalibrationRunRecord,
    CalibrationRunStatus,
    generate_calibration_artifact,
    validate_calibration_record,
)
from image23mf.calibration.catalogs import (
    CalibrationCatalogRepository,
    CalibrationCatalogVersionResource,
)
from image23mf.calibration.evidence import (
    ATTESTATION_STATEMENT,
    CalibrationAttestation,
    CalibrationEvidenceManifest,
    CalibrationEvidenceMember,
    CalibrationEvidenceRole,
    CalibrationFilamentEvidence,
    CalibrationPinnedProfileEvidence,
    CalibrationSlicerEvidence,
    build_calibration_evidence_bundle,
    create_calibration_evidence_member,
)
from image23mf.calibration.models import PrintabilityProfile
from image23mf.calibration.registry import (
    CalibrationImportResult,
    CalibrationRegistry,
)
from image23mf.storage import ContentAddressedStore, StoredBlob
from image23mf.storage.repositories import canonical_json, immediate_transaction, new_id

_USER_ROLES = frozenset(
    {
        CalibrationEvidenceRole.PROJECT_3MF,
        CalibrationEvidenceRole.SLICED_GCODE,
        CalibrationEvidenceRole.SLICER_SETTINGS,
        CalibrationEvidenceRole.PHOTO,
    }
)
_SINGLETON_ROLES = frozenset(
    {
        CalibrationEvidenceRole.PROJECT_3MF,
        CalibrationEvidenceRole.SLICED_GCODE,
        CalibrationEvidenceRole.SLICER_SETTINGS,
    }
)


class CalibrationDraftError(RuntimeError):
    """A calibration evidence draft cannot be read or changed safely."""


class CalibrationDraftNotFoundError(CalibrationDraftError):
    """The requested draft does not exist."""


class CalibrationDraftConflictError(CalibrationDraftError):
    """The requested mutation is stale or targets a finalized draft."""


class CalibrationDraftInvalidError(CalibrationDraftError):
    """Draft values or attachments do not satisfy the evidence contract."""


class CalibrationDraftCorruptError(CalibrationDraftError):
    """Retained draft state or bytes no longer match their sealed identity."""


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CalibrationDraftMetadata(DraftModel):
    slicer_application: Optional[str] = Field(default=None, min_length=1, max_length=120)
    slicer_version: Optional[str] = Field(default=None, min_length=1, max_length=120)
    slicer_executable_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    machine_profile_name: Optional[str] = Field(default=None, min_length=1, max_length=300)
    machine_profile_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    process_profile_name: Optional[str] = Field(default=None, min_length=1, max_length=300)
    process_profile_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    filament_profile_name: Optional[str] = Field(default=None, min_length=1, max_length=300)
    filament_profile_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    filament_id: Optional[str] = Field(default=None, max_length=120)
    filament_manufacturer: Optional[str] = Field(default=None, min_length=1, max_length=120)
    filament_family: str = Field(default="", max_length=120)
    filament_name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    filament_material: Optional[str] = Field(default=None, min_length=1, max_length=80)
    filament_finish: str = Field(default="", max_length=80)
    filament_color_hex: Optional[str] = Field(default=None, pattern=r"^#[0-9A-F]{6}$")


class CalibrationDraftUpdate(DraftModel):
    expected_generation: int = Field(gt=0)
    record: CalibrationRunRecord
    metadata: CalibrationDraftMetadata


class CalibrationDraftAttestRequest(DraftModel):
    expected_generation: int = Field(gt=0)
    confirmed: Literal[True]


class CalibrationDraftMutationRequest(DraftModel):
    expected_generation: int = Field(gt=0)


class CalibrationDraftCreateRequest(DraftModel):
    profile_id: str = Field(min_length=1, max_length=120)
    expected_catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class CalibrationDraftBlocker(DraftModel):
    code: str
    field: str
    message: str


class CalibrationDraftReadiness(DraftModel):
    evidence_state: Literal[
        "preparation", "unverified_draft", "attested_draft", "sealed_physical_evidence"
    ]
    ready_to_attest: bool
    ready_to_finalize: bool
    blockers: tuple[CalibrationDraftBlocker, ...]


class CalibrationDraftMemberResource(DraftModel):
    id: str
    role: CalibrationEvidenceRole
    ordinal: int
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    media_type: str
    extension: str
    download_url: str
    created_at: str


class CalibrationDraftResource(DraftModel):
    id: str
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: PrintabilityProfile
    artifact: CalibrationArtifactManifest
    record: CalibrationRunRecord
    metadata: CalibrationDraftMetadata
    generation: int = Field(gt=0)
    attested_at: Optional[str]
    attested_candidate_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attestation_statement: str
    finalized_run_id: Optional[str]
    members: tuple[CalibrationDraftMemberResource, ...]
    readiness: CalibrationDraftReadiness
    created_at: str
    updated_at: str


class CalibrationDraftCollection(DraftModel):
    items: tuple[CalibrationDraftResource, ...]
    total: int = Field(ge=0)


class CalibrationDraftFinalizeResult(DraftModel):
    draft: CalibrationDraftResource
    imported: CalibrationImportResult


class CalibrationSpecimenResource(DraftModel):
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_id: str
    catalog_version: str
    profile: PrintabilityProfile
    artifact: CalibrationArtifactManifest
    record_template: CalibrationRunRecord
    evidence_state: Literal["not_observed"] = "not_observed"
    evidence_message: str = (
        "Generated preparation files are not physically observed calibration evidence."
    )
    svg_url: str
    png_url: str
    bundle_url: str


class CalibrationDraftService:
    def __init__(self, connection: sqlite3.Connection, blob_store: ContentAddressedStore) -> None:
        self.connection = connection
        self.blob_store = blob_store
        self.catalogs = CalibrationCatalogRepository(connection)

    def specimen(self, profile_id: str) -> CalibrationSpecimenResource:
        catalog = self.catalogs.active()
        profile = _profile(catalog, profile_id)
        generated = generate_calibration_artifact(profile)
        prefix = f"/api/calibration/specimens/{profile_id}"
        return CalibrationSpecimenResource(
            catalog_fingerprint=catalog.fingerprint,
            catalog_id=catalog.catalog.catalog_id,
            catalog_version=catalog.catalog.catalog_version,
            profile=profile,
            artifact=generated.manifest,
            record_template=generated.record_template,
            svg_url=f"{prefix}/coupon.svg",
            png_url=f"{prefix}/coupon.png",
            bundle_url=f"{prefix}/bundle",
        )

    def specimen_bytes(self, profile_id: str, kind: str) -> tuple[bytes, str, str]:
        catalog = self.catalogs.active()
        profile = _profile(catalog, profile_id)
        generated = generate_calibration_artifact(profile)
        if kind == "svg":
            return generated.svg_bytes, "image/svg+xml", f"{profile.id}-coupon.svg"
        if kind == "png":
            return generated.png_bytes, "image/png", f"{profile.id}-coupon.png"
        if kind != "bundle":
            raise CalibrationDraftInvalidError(f"unknown specimen representation: {kind}")
        return (
            _specimen_bundle(catalog, generated),
            "application/zip",
            f"{profile.id}-specimen.zip",
        )

    def create(self, request: CalibrationDraftCreateRequest) -> CalibrationDraftResource:
        active = self.catalogs.active()
        if active.fingerprint != request.expected_catalog_fingerprint:
            raise CalibrationDraftConflictError(
                "the active calibration catalog changed before the run was started"
            )
        profile = _profile(active, request.profile_id)
        generated = generate_calibration_artifact(profile)
        identifier = new_id("calibration_draft")
        coupon_members = (
            create_calibration_evidence_member(
                role=CalibrationEvidenceRole.COUPON_SVG,
                ordinal=0,
                filename="coupon.svg",
                media_type="image/svg+xml",
                payload=generated.svg_bytes,
            ),
            create_calibration_evidence_member(
                role=CalibrationEvidenceRole.COUPON_PNG,
                ordinal=0,
                filename="coupon.png",
                media_type="image/png",
                payload=generated.png_bytes,
            ),
        )
        payloads = {
            CalibrationEvidenceRole.COUPON_SVG: generated.svg_bytes,
            CalibrationEvidenceRole.COUPON_PNG: generated.png_bytes,
        }
        stored = {
            member.role: self.blob_store.put_bytes(
                payloads[member.role],
                namespace="artifacts",
                extension=member.extension,
                media_type=member.media_type,
            )
            for member in coupon_members
        }
        with immediate_transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO calibration_run_drafts(
                    id, catalog_fingerprint, profile_id, artifact_fingerprint,
                    artifact_json, record_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    active.fingerprint,
                    profile.id,
                    generated.manifest.fingerprint(),
                    generated.manifest.canonical_json(),
                    generated.record_template.canonical_json(),
                    canonical_json(CalibrationDraftMetadata().model_dump(mode="json")),
                ),
            )
            for member in coupon_members:
                self._insert_member(identifier, member, stored[member.role])
        return self.get(identifier)

    def get(self, draft_id: str) -> CalibrationDraftResource:
        row = self._row(draft_id)
        return self._resource(row)

    def list(self) -> CalibrationDraftCollection:
        rows = self.connection.execute(
            "SELECT * FROM calibration_run_drafts ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        items = tuple(self._resource(row) for row in rows)
        return CalibrationDraftCollection(items=items, total=len(items))

    def update(self, draft_id: str, request: CalibrationDraftUpdate) -> CalibrationDraftResource:
        row = self._mutable_row(draft_id, request.expected_generation)
        artifact = _artifact(row)
        _validate_draft_record(request.record, artifact)
        with immediate_transaction(self.connection):
            changed = self.connection.execute(
                """
                UPDATE calibration_run_drafts
                SET record_json = ?, metadata_json = ?, generation = generation + 1,
                    attested_at = NULL, attested_candidate_sha256 = NULL,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND generation = ? AND finalized_run_id IS NULL
                """,
                (
                    request.record.canonical_json(),
                    canonical_json(request.metadata.model_dump(mode="json")),
                    draft_id,
                    request.expected_generation,
                ),
            ).rowcount
            if changed != 1:
                raise CalibrationDraftConflictError("calibration draft changed before save")
        return self.get(draft_id)

    def upload(
        self,
        draft_id: str,
        *,
        role: CalibrationEvidenceRole,
        ordinal: int,
        media_type: str,
        payload: bytes,
        expected_generation: int,
    ) -> CalibrationDraftResource:
        self._mutable_row(draft_id, expected_generation)
        if role not in _USER_ROLES:
            raise CalibrationDraftInvalidError("generated coupon members cannot be replaced")
        if role in _SINGLETON_ROLES and ordinal != 0:
            raise CalibrationDraftInvalidError(f"{role.value} evidence must use ordinal zero")
        filename = _canonical_filename(role, ordinal, media_type)
        try:
            member = create_calibration_evidence_member(
                role=role,
                ordinal=ordinal,
                filename=filename,
                media_type=media_type,
                payload=payload,
            )
        except ValueError as error:
            raise CalibrationDraftInvalidError(str(error)) from error
        blob = self.blob_store.put_bytes(
            payload,
            namespace="artifacts",
            extension=member.extension,
            media_type=member.media_type,
        )
        with immediate_transaction(self.connection):
            current = self.connection.execute(
                "SELECT generation, finalized_run_id FROM calibration_run_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if (
                current is None
                or current["generation"] != expected_generation
                or current["finalized_run_id"] is not None
            ):
                raise CalibrationDraftConflictError("calibration draft changed before upload")
            self.connection.execute(
                """
                DELETE FROM calibration_run_draft_members
                WHERE draft_id = ? AND role = ? AND ordinal = ?
                """,
                (draft_id, role.value, ordinal),
            )
            self._insert_member(draft_id, member, blob)
            self._advance(draft_id, expected_generation, clear_attestation=True)
        return self.get(draft_id)

    def remove(
        self,
        draft_id: str,
        *,
        role: CalibrationEvidenceRole,
        ordinal: int,
        expected_generation: int,
    ) -> CalibrationDraftResource:
        self._mutable_row(draft_id, expected_generation)
        if role not in _USER_ROLES:
            raise CalibrationDraftInvalidError("generated coupon members cannot be removed")
        with immediate_transaction(self.connection):
            deleted = self.connection.execute(
                """
                DELETE FROM calibration_run_draft_members
                WHERE draft_id = ? AND role = ? AND ordinal = ?
                """,
                (draft_id, role.value, ordinal),
            ).rowcount
            if deleted != 1:
                raise CalibrationDraftNotFoundError("calibration draft member not found")
            if role == CalibrationEvidenceRole.PHOTO:
                later_photos = self.connection.execute(
                    """
                    SELECT id, ordinal, extension FROM calibration_run_draft_members
                    WHERE draft_id = ? AND role = ? AND ordinal > ?
                    ORDER BY ordinal
                    """,
                    (draft_id, role.value, ordinal),
                ).fetchall()
                for photo in later_photos:
                    self.connection.execute(
                        """
                        UPDATE calibration_run_draft_members
                        SET ordinal = ?, filename = ? WHERE id = ?
                        """,
                        (
                            photo["ordinal"] - 1,
                            f"photo-{photo['ordinal'] - 1:02d}{photo['extension']}",
                            photo["id"],
                        ),
                    )
            self._advance(draft_id, expected_generation, clear_attestation=True)
        return self.get(draft_id)

    def attest(
        self, draft_id: str, request: CalibrationDraftAttestRequest
    ) -> CalibrationDraftResource:
        row = self._mutable_row(draft_id, request.expected_generation)
        resource = self._resource(row)
        blockers = tuple(
            blocker
            for blocker in resource.readiness.blockers
            if blocker.code != "attestation_missing"
        )
        if blockers:
            raise CalibrationDraftInvalidError(
                "calibration draft is incomplete: " + "; ".join(item.message for item in blockers)
            )
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with immediate_transaction(self.connection):
            changed = self.connection.execute(
                """
                UPDATE calibration_run_drafts
                SET attested_at = ?, attested_candidate_sha256 = ?, generation = generation + 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND generation = ? AND finalized_run_id IS NULL
                """,
                (timestamp, resource.candidate_sha256, draft_id, request.expected_generation),
            ).rowcount
            if changed != 1:
                raise CalibrationDraftConflictError("calibration draft changed before attestation")
        return self.get(draft_id)

    def finalize(
        self, draft_id: str, request: CalibrationDraftMutationRequest
    ) -> CalibrationDraftFinalizeResult:
        row = self._row(draft_id)
        if row["finalized_run_id"] is not None:
            resource = self._resource(row)
            registry = CalibrationRegistry(
                self.connection,
                self.blob_store,
                self.catalogs.get(resource.catalog_fingerprint).catalog,
            )
            return CalibrationDraftFinalizeResult(
                draft=resource,
                imported=CalibrationImportResult(
                    run=registry.get(row["finalized_run_id"]), duplicate=True
                ),
            )
        if row["generation"] != request.expected_generation:
            raise CalibrationDraftConflictError(
                "stale calibration draft generation: "
                f"expected {request.expected_generation}, current {row['generation']}"
            )
        resource = self._resource(row)
        if not resource.readiness.ready_to_finalize:
            raise CalibrationDraftInvalidError(
                "calibration draft is not ready: "
                + "; ".join(item.message for item in resource.readiness.blockers)
            )
        manifest, payloads = self._evidence(resource)
        bundle = build_calibration_evidence_bundle(manifest, payloads)
        with immediate_transaction(self.connection):
            locked = self.connection.execute(
                """
                SELECT generation, attested_at, attested_candidate_sha256, finalized_run_id
                FROM calibration_run_drafts WHERE id = ?
                """,
                (draft_id,),
            ).fetchone()
            if (
                locked is None
                or locked["generation"] != request.expected_generation
                or locked["attested_at"] != resource.attested_at
                or locked["attested_candidate_sha256"] != resource.candidate_sha256
                or locked["finalized_run_id"] is not None
            ):
                raise CalibrationDraftConflictError("calibration draft changed before finalization")
            imported = CalibrationRegistry(
                self.connection,
                self.blob_store,
                self.catalogs.get(resource.catalog_fingerprint).catalog,
            ).import_bundle(bundle, manage_transaction=False)
            changed = self.connection.execute(
                """
                UPDATE calibration_run_drafts
                SET finalized_run_id = ?, generation = generation + 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND generation = ? AND finalized_run_id IS NULL
                """,
                (imported.run.id, draft_id, request.expected_generation),
            ).rowcount
            if changed != 1:
                raise CalibrationDraftConflictError("calibration draft changed during finalization")
        return CalibrationDraftFinalizeResult(draft=self.get(draft_id), imported=imported)

    def member(self, draft_id: str, member_id: str) -> tuple[CalibrationDraftMemberResource, str]:
        self._row(draft_id)
        row = self.connection.execute(
            "SELECT * FROM calibration_run_draft_members WHERE id = ? AND draft_id = ?",
            (member_id, draft_id),
        ).fetchone()
        if row is None:
            raise CalibrationDraftNotFoundError("calibration draft member not found")
        resource = _member_resource(draft_id, row)
        _verify_blob(self.blob_store, row)
        return resource, row["relative_path"]

    def _evidence(
        self, resource: CalibrationDraftResource
    ) -> tuple[CalibrationEvidenceManifest, dict[str, bytes]]:
        catalog = self.catalogs.get(resource.catalog_fingerprint)
        metadata = resource.metadata
        record = resource.record.model_copy(update={"status": CalibrationRunStatus.COMPLETED})
        filament_payload = {
            "filament_id": metadata.filament_id,
            "manufacturer": metadata.filament_manufacturer,
            "family": metadata.filament_family,
            "name": metadata.filament_name,
            "material": metadata.filament_material,
            "finish": metadata.filament_finish,
            "color_hex": metadata.filament_color_hex,
        }
        filament_payload["snapshot_sha256"] = hashlib.sha256(
            canonical_json(filament_payload).encode("utf-8")
        ).hexdigest()
        filament = CalibrationFilamentEvidence.model_validate(filament_payload)
        profiles = tuple(
            sorted(
                (
                    CalibrationPinnedProfileEvidence(
                        role="machine",
                        name=metadata.machine_profile_name,
                        sha256=metadata.machine_profile_sha256,
                    ),
                    CalibrationPinnedProfileEvidence(
                        role="process",
                        name=metadata.process_profile_name,
                        sha256=metadata.process_profile_sha256,
                    ),
                    CalibrationPinnedProfileEvidence(
                        role="filament",
                        name=metadata.filament_profile_name,
                        sha256=metadata.filament_profile_sha256,
                    ),
                ),
                key=lambda item: (item.role, item.name, item.sha256),
            )
        )
        slicer = CalibrationSlicerEvidence(
            application=metadata.slicer_application,
            version=metadata.slicer_version,
            executable_sha256=metadata.slicer_executable_sha256,
            profiles=profiles,
        )
        attestation = CalibrationAttestation(
            operator=record.operator,
            attested_at=resource.attested_at,
            statement=ATTESTATION_STATEMENT,
        )
        member_rows = self.connection.execute(
            """
            SELECT * FROM calibration_run_draft_members
            WHERE draft_id = ? ORDER BY role, ordinal
            """,
            (resource.id,),
        ).fetchall()
        members = tuple(_evidence_member(row) for row in member_rows)
        payloads = {
            member.archive_path: self.blob_store.path_for(row["relative_path"]).read_bytes()
            for member, row in zip(members, member_rows)
        }
        manifest = CalibrationEvidenceManifest(
            catalog_id=catalog.catalog.catalog_id,
            catalog_version=catalog.catalog.catalog_version,
            catalog_fingerprint=catalog.fingerprint,
            profile=resource.profile,
            artifact=resource.artifact,
            record=record,
            slicer=slicer,
            filaments=(filament,),
            attestation=attestation,
            members=members,
        )
        return manifest, payloads

    def _resource(self, row: sqlite3.Row) -> CalibrationDraftResource:
        try:
            artifact = _artifact(row)
            record = CalibrationRunRecord.model_validate_json(row["record_json"])
            metadata = CalibrationDraftMetadata.model_validate_json(row["metadata_json"])
        except Exception as error:
            raise CalibrationDraftCorruptError(
                "retained calibration draft JSON is invalid"
            ) from error
        if artifact.fingerprint() != row["artifact_fingerprint"]:
            raise CalibrationDraftCorruptError("retained calibration draft artifact is invalid")
        catalog = self.catalogs.get(row["catalog_fingerprint"])
        profile = _profile(catalog, row["profile_id"])
        if artifact.profile_fingerprint != _profile_fingerprint(profile):
            raise CalibrationDraftCorruptError("retained calibration draft profile is invalid")
        try:
            _validate_draft_record(record, artifact)
        except (ValueError, CalibrationDraftInvalidError) as error:
            raise CalibrationDraftCorruptError(
                "retained calibration draft record is invalid"
            ) from error
        member_rows = self.connection.execute(
            """
            SELECT * FROM calibration_run_draft_members
            WHERE draft_id = ? ORDER BY role, ordinal
            """,
            (row["id"],),
        ).fetchall()
        for member_row in member_rows:
            _verify_blob(self.blob_store, member_row)
        members = tuple(_member_resource(row["id"], item) for item in member_rows)
        candidate_sha256 = _candidate_sha256(
            row["catalog_fingerprint"], artifact, record, metadata, members
        )
        if row["attested_at"] is not None and row["attested_candidate_sha256"] != candidate_sha256:
            raise CalibrationDraftCorruptError(
                "calibration draft attestation is not bound to its current evidence candidate"
            )
        blockers = _readiness_blockers(
            record,
            metadata,
            members,
            attested_at=row["attested_at"],
            finalized_run_id=row["finalized_run_id"],
        )
        return CalibrationDraftResource(
            id=row["id"],
            catalog_fingerprint=row["catalog_fingerprint"],
            profile=profile,
            artifact=artifact,
            record=record,
            metadata=metadata,
            generation=row["generation"],
            attested_at=row["attested_at"],
            attested_candidate_sha256=row["attested_candidate_sha256"],
            candidate_sha256=candidate_sha256,
            attestation_statement=ATTESTATION_STATEMENT,
            finalized_run_id=row["finalized_run_id"],
            members=members,
            readiness=CalibrationDraftReadiness(
                evidence_state=_evidence_state(row, members),
                ready_to_attest=(
                    row["finalized_run_id"] is None
                    and not tuple(item for item in blockers if item.code != "attestation_missing")
                ),
                ready_to_finalize=not blockers,
                blockers=blockers,
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row(self, draft_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM calibration_run_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise CalibrationDraftNotFoundError(f"calibration draft not found: {draft_id}")
        return row

    def _mutable_row(self, draft_id: str, expected_generation: int) -> sqlite3.Row:
        row = self._row(draft_id)
        if row["finalized_run_id"] is not None:
            raise CalibrationDraftConflictError("calibration draft is already finalized")
        if row["generation"] != expected_generation:
            raise CalibrationDraftConflictError(
                "stale calibration draft generation: "
                f"expected {expected_generation}, current {row['generation']}"
            )
        return row

    def _insert_member(
        self,
        draft_id: str,
        member: CalibrationEvidenceMember,
        blob: StoredBlob,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO calibration_run_draft_members(
                id, draft_id, role, ordinal, filename, sha256, relative_path,
                byte_size, media_type, extension
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("calibration_draft_member"),
                draft_id,
                member.role.value,
                member.ordinal,
                member.filename,
                blob.sha256,
                blob.relative_path,
                blob.byte_size,
                blob.media_type,
                blob.extension,
            ),
        )

    def _advance(self, draft_id: str, expected_generation: int, *, clear_attestation: bool) -> None:
        attestation_sql = (
            "attested_at = NULL, attested_candidate_sha256 = NULL"
            if clear_attestation
            else "attested_at = attested_at, attested_candidate_sha256 = attested_candidate_sha256"
        )
        changed = self.connection.execute(
            f"""
            UPDATE calibration_run_drafts
            SET generation = generation + 1,
                {attestation_sql},
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ? AND generation = ? AND finalized_run_id IS NULL
            """,  # noqa: S608 -- fixed internal SQL fragment, never user input.
            (draft_id, expected_generation),
        ).rowcount
        if changed != 1:
            raise CalibrationDraftConflictError("calibration draft changed before mutation")


def _profile(catalog: CalibrationCatalogVersionResource, profile_id: str) -> PrintabilityProfile:
    profile = catalog.catalog.profile(profile_id)
    if profile is None:
        raise CalibrationDraftNotFoundError(f"printability profile not found: {profile_id}")
    return profile


def _artifact(row: sqlite3.Row) -> CalibrationArtifactManifest:
    return CalibrationArtifactManifest.model_validate_json(row["artifact_json"])


def _profile_fingerprint(profile: PrintabilityProfile) -> str:
    return hashlib.sha256(
        json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _candidate_sha256(
    catalog_fingerprint: str,
    artifact: CalibrationArtifactManifest,
    record: CalibrationRunRecord,
    metadata: CalibrationDraftMetadata,
    members: tuple[CalibrationDraftMemberResource, ...],
) -> str:
    payload = {
        "schema_version": 1,
        "catalog_fingerprint": catalog_fingerprint,
        "artifact_fingerprint": artifact.fingerprint(),
        "record": record.model_dump(mode="json"),
        "metadata": metadata.model_dump(mode="json"),
        "members": [
            {
                "role": item.role.value,
                "ordinal": item.ordinal,
                "filename": item.filename,
                "sha256": item.sha256,
                "byte_size": item.byte_size,
                "media_type": item.media_type,
            }
            for item in members
        ],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _evidence_state(row: sqlite3.Row, members) -> str:
    if row["finalized_run_id"] is not None:
        return "sealed_physical_evidence"
    if row["attested_at"] is not None:
        return "attested_draft"
    if row["generation"] > 1 or any(item.role in _USER_ROLES for item in members):
        return "unverified_draft"
    return "preparation"


def _validate_draft_record(
    record: CalibrationRunRecord, artifact: CalibrationArtifactManifest
) -> None:
    if record.status != CalibrationRunStatus.TEMPLATE:
        raise CalibrationDraftInvalidError(
            "mutable calibration drafts must remain templates until server finalization"
        )
    validate_calibration_record(record, artifact)
    if (
        record.printer_id != artifact.printer_id
        or record.nozzle_id != artifact.nozzle_id
        or record.material_class != artifact.material_class
    ):
        raise CalibrationDraftInvalidError("calibration draft setup does not match its specimen")


def _readiness_blockers(
    record: CalibrationRunRecord,
    metadata: CalibrationDraftMetadata,
    members: tuple[CalibrationDraftMemberResource, ...],
    *,
    attested_at: Optional[str],
    finalized_run_id: Optional[str],
) -> tuple[CalibrationDraftBlocker, ...]:
    if finalized_run_id is not None:
        return ()
    blockers = []

    def require(value, code: str, field: str, message: str) -> None:
        if value is None or value == "":
            blockers.append(CalibrationDraftBlocker(code=code, field=field, message=message))

    require(record.record_id, "record_id_missing", "record.record_id", "Add a stable run ID.")
    require(
        record.layer_height_mm,
        "layer_height_missing",
        "record.layer_height_mm",
        "Record the printed layer height.",
    )
    require(record.plate_id, "plate_missing", "record.plate_id", "Record the build plate.")
    require(record.filament, "filament_missing", "record.filament", "Record the filament name.")
    require(record.operator, "operator_missing", "record.operator", "Record the operator.")
    require(record.printed_on, "printed_on_missing", "record.printed_on", "Record the print date.")
    require(
        record.slicer_profile,
        "slicer_profile_missing",
        "record.slicer_profile",
        "Record the exact slicer process profile.",
    )
    for observation in record.observations:
        if observation.outcome == CalibrationOutcome.UNTESTED:
            blockers.append(
                CalibrationDraftBlocker(
                    code="observation_untested",
                    field=f"record.observations.{observation.feature_id}",
                    message=f"Score {observation.feature_id} as pass, fail, or uncertain.",
                )
            )
        elif observation.outcome == CalibrationOutcome.FAIL and (
            observation.measured_dimension_mm is None
        ):
            blockers.append(
                CalibrationDraftBlocker(
                    code="failed_measurement_missing",
                    field=f"record.observations.{observation.feature_id}.measured_dimension_mm",
                    message=(
                        f"Record a measurement for failed {observation.feature_id}; "
                        "use 0 if it vanished."
                    ),
                )
            )
    metadata_fields = (
        (metadata.slicer_application, "slicer_application", "Record the slicer application."),
        (metadata.slicer_version, "slicer_version", "Record the slicer version."),
        (
            metadata.slicer_executable_sha256,
            "slicer_executable_sha256",
            "Record the slicer executable SHA-256.",
        ),
        (metadata.machine_profile_name, "machine_profile_name", "Name the machine profile."),
        (metadata.machine_profile_sha256, "machine_profile_sha256", "Hash the machine profile."),
        (metadata.process_profile_name, "process_profile_name", "Name the process profile."),
        (metadata.process_profile_sha256, "process_profile_sha256", "Hash the process profile."),
        (metadata.filament_profile_name, "filament_profile_name", "Name the filament profile."),
        (
            metadata.filament_profile_sha256,
            "filament_profile_sha256",
            "Hash the filament profile.",
        ),
        (
            metadata.filament_manufacturer,
            "filament_manufacturer",
            "Record the filament manufacturer.",
        ),
        (metadata.filament_name, "filament_name", "Record the filament snapshot name."),
        (metadata.filament_material, "filament_material", "Record the filament material."),
        (metadata.filament_color_hex, "filament_color_hex", "Record the filament color."),
    )
    for value, field, message in metadata_fields:
        require(value, f"{field}_missing", f"metadata.{field}", message)
    if record.filament and metadata.filament_name and record.filament != metadata.filament_name:
        blockers.append(
            CalibrationDraftBlocker(
                code="filament_name_mismatch",
                field="metadata.filament_name",
                message="The record and filament snapshot must name the same filament.",
            )
        )
    if (
        record.filament
        and metadata.filament_profile_name
        and record.filament != metadata.filament_profile_name
    ):
        blockers.append(
            CalibrationDraftBlocker(
                code="filament_profile_mismatch",
                field="metadata.filament_profile_name",
                message="The record and pinned filament profile names must match exactly.",
            )
        )
    if (
        metadata.filament_material
        and metadata.filament_material.lower() != record.material_class.lower()
    ):
        blockers.append(
            CalibrationDraftBlocker(
                code="filament_material_mismatch",
                field="metadata.filament_material",
                message="The filament material must match the profile material class.",
            )
        )
    if (
        record.slicer_profile
        and metadata.process_profile_name
        and record.slicer_profile != metadata.process_profile_name
    ):
        blockers.append(
            CalibrationDraftBlocker(
                code="process_profile_mismatch",
                field="metadata.process_profile_name",
                message="The record and pinned process profile names must match.",
            )
        )
    positions = {(item.role, item.ordinal) for item in members}
    for role in (
        CalibrationEvidenceRole.COUPON_SVG,
        CalibrationEvidenceRole.COUPON_PNG,
        CalibrationEvidenceRole.PROJECT_3MF,
        CalibrationEvidenceRole.SLICED_GCODE,
        CalibrationEvidenceRole.SLICER_SETTINGS,
    ):
        if (role, 0) not in positions:
            blockers.append(
                CalibrationDraftBlocker(
                    code=f"{role.value}_missing",
                    field=f"members.{role.value}",
                    message=f"Attach the required {role.value.replace('_', ' ')} evidence.",
                )
            )
    photo_ordinals = sorted(
        item.ordinal for item in members if item.role == CalibrationEvidenceRole.PHOTO
    )
    if not photo_ordinals:
        blockers.append(
            CalibrationDraftBlocker(
                code="photo_missing",
                field="members.photo",
                message="Attach at least one physical print photo.",
            )
        )
    elif photo_ordinals != list(range(len(photo_ordinals))):
        blockers.append(
            CalibrationDraftBlocker(
                code="photo_ordinals_noncanonical",
                field="members.photo",
                message="Photo evidence must use consecutive positions starting at zero.",
            )
        )
    if attested_at is None:
        blockers.append(
            CalibrationDraftBlocker(
                code="attestation_missing",
                field="attestation",
                message="Confirm the physical-observation attestation after all evidence is final.",
            )
        )
    return tuple(blockers)


def _canonical_filename(role: CalibrationEvidenceRole, ordinal: int, media_type: str) -> str:
    if role == CalibrationEvidenceRole.PROJECT_3MF:
        return "project.3mf"
    if role == CalibrationEvidenceRole.SLICED_GCODE:
        return "sliced.gcode"
    if role == CalibrationEvidenceRole.SLICER_SETTINGS:
        return "slicer-settings.json"
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(media_type)
    if extension is None:
        raise CalibrationDraftInvalidError("unsupported physical photo media type")
    return f"photo-{ordinal:02d}{extension}"


def _member_resource(draft_id: str, row: sqlite3.Row) -> CalibrationDraftMemberResource:
    return CalibrationDraftMemberResource(
        id=row["id"],
        role=CalibrationEvidenceRole(row["role"]),
        ordinal=row["ordinal"],
        filename=row["filename"],
        sha256=row["sha256"],
        byte_size=row["byte_size"],
        media_type=row["media_type"],
        extension=row["extension"],
        download_url=f"/api/calibration/drafts/{draft_id}/members/{row['id']}",
        created_at=row["created_at"],
    )


def _evidence_member(row: sqlite3.Row) -> CalibrationEvidenceMember:
    return CalibrationEvidenceMember(
        role=CalibrationEvidenceRole(row["role"]),
        ordinal=row["ordinal"],
        archive_path=f"payload/{row['filename']}",
        filename=row["filename"],
        sha256=row["sha256"],
        byte_size=row["byte_size"],
        media_type=row["media_type"],
        extension=row["extension"],
    )


def _verify_blob(store: ContentAddressedStore, row: sqlite3.Row) -> None:
    blob = StoredBlob(
        sha256=row["sha256"],
        relative_path=row["relative_path"],
        byte_size=row["byte_size"],
        media_type=row["media_type"],
        extension=row["extension"],
    )
    try:
        valid = store.verify(blob)
    except FileNotFoundError:
        valid = False
    if not valid:
        raise CalibrationDraftCorruptError(
            f"calibration draft member is missing or corrupt: {row['filename']}"
        )


def _specimen_bundle(catalog, generated) -> bytes:
    metadata = {
        "schema_version": 1,
        "evidence_state": "not_observed",
        "evidence_message": (
            "Generated preparation files are not physically observed calibration evidence."
        ),
        "catalog_id": catalog.catalog.catalog_id,
        "catalog_version": catalog.catalog.catalog_version,
        "catalog_fingerprint": catalog.fingerprint,
        "profile": generated.record_template.profile_id,
        "artifact_fingerprint": generated.manifest.fingerprint(),
    }
    members = (
        ("specimen.json", canonical_json(metadata).encode("utf-8")),
        ("coupon.svg", generated.svg_bytes),
        ("coupon.png", generated.png_bytes),
        ("manifest.json", (generated.manifest.canonical_json() + "\n").encode("utf-8")),
        (
            "record-template.json",
            (generated.record_template.canonical_json() + "\n").encode("utf-8"),
        ),
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for filename, payload in members:
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, payload)
    return output.getvalue()
