"""Immutable SQLite/content-store registry for observed calibration runs."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import json
import sqlite3
from contextlib import nullcontext
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from image23mf.calibration.artifact import CalibrationOutcome
from image23mf.calibration.catalogs import CalibrationCatalogRepository
from image23mf.calibration.evidence import (
    CalibrationEvidenceManifest,
    CalibrationEvidenceMember,
    CalibrationEvidenceRole,
    parse_calibration_evidence_bundle,
)
from image23mf.calibration.models import PrintabilityProfileCatalog
from image23mf.storage.blob_store import ContentAddressedStore, StoredBlob
from image23mf.storage.repositories import canonical_json, immediate_transaction, new_id


class CalibrationRegistryError(RuntimeError):
    """Base class for durable calibration evidence failures."""


class CalibrationRegistryConflictError(CalibrationRegistryError):
    """An immutable evidence identity conflicts with retained evidence."""


class CalibrationRegistryNotFoundError(CalibrationRegistryError):
    """A calibration run does not exist."""


class CalibrationRegistryCorruptError(CalibrationRegistryError):
    """Retained calibration metadata or content-addressed bytes are corrupt."""


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalibrationMemberResource(RegistryModel):
    id: str
    role: CalibrationEvidenceRole
    ordinal: int = Field(ge=0)
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    byte_size: int = Field(gt=0)
    media_type: str
    extension: str

    def stored_blob(self) -> StoredBlob:
        return StoredBlob(
            sha256=self.sha256,
            relative_path=self.relative_path,
            byte_size=self.byte_size,
            media_type=self.media_type,
            extension=self.extension,
        )


class CalibrationSourceBundleResource(RegistryModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    byte_size: int = Field(gt=0)
    media_type: Literal["application/zip"] = "application/zip"
    extension: Literal[".zip"] = ".zip"

    def stored_blob(self) -> StoredBlob:
        return StoredBlob(
            sha256=self.sha256,
            relative_path=self.relative_path,
            byte_size=self.byte_size,
            media_type=self.media_type,
            extension=self.extension,
        )


class CalibrationArtifactResource(RegistryModel):
    id: str
    catalog_id: str
    catalog_version: str
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_id: str
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: dict
    members: tuple[CalibrationMemberResource, ...]
    created_at: str


class CalibrationRunResource(RegistryModel):
    id: str
    artifact: CalibrationArtifactResource
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
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_bundle: CalibrationSourceBundleResource
    evidence: CalibrationEvidenceManifest
    members: tuple[CalibrationMemberResource, ...]
    imported_at: str
    integrity: Literal["verified"] = "verified"


class CalibrationRunCollection(RegistryModel):
    items: tuple[CalibrationRunResource, ...]


class CalibrationImportResult(RegistryModel):
    run: CalibrationRunResource
    duplicate: bool


class CalibrationRegistry:
    def __init__(
        self,
        connection: sqlite3.Connection,
        blob_store: ContentAddressedStore,
        catalog: PrintabilityProfileCatalog,
    ) -> None:
        self.connection = connection
        self.blob_store = blob_store
        self.catalog = catalog
        catalogs = CalibrationCatalogRepository(connection)
        try:
            catalogs.get(catalog.fingerprint())
        except ValueError:
            catalogs.ensure(catalog)

    def import_bundle(
        self, payload: bytes, *, manage_transaction: bool = True
    ) -> CalibrationImportResult:
        if not manage_transaction and not self.connection.in_transaction:
            raise CalibrationRegistryError(
                "caller-managed calibration import requires an active transaction"
            )
        parsed = parse_calibration_evidence_bundle(payload)
        evidence = parsed.manifest
        self._validate_catalog_and_observations(evidence)
        record_id = evidence.record.record_id
        if record_id is None:  # Defensive: completed records already require this.
            raise CalibrationRegistryError("completed calibration evidence lacks a run ID")
        record_json = evidence.record.canonical_json()
        record_sha256 = hashlib.sha256(record_json.encode("utf-8")).hexdigest()
        evidence_sha256 = evidence.fingerprint()
        process_json = canonical_json(_process_identity(evidence))
        process_fingerprint = hashlib.sha256(process_json.encode("utf-8")).hexdigest()

        existing = self._find_run_row(record_id)
        if existing is not None:
            if existing["evidence_sha256"] != evidence_sha256:
                raise CalibrationRegistryConflictError(
                    "that calibration run ID is already sealed with different evidence"
                )
            return CalibrationImportResult(run=self.get(record_id), duplicate=True)

        stored = {
            member.archive_path: self.blob_store.put_bytes(
                parsed.payloads[member.archive_path],
                namespace="artifacts",
                extension=member.extension,
                media_type=member.media_type,
            )
            for member in evidence.members
        }
        source_bundle = self.blob_store.put_bytes(
            payload,
            namespace="artifacts",
            extension=".zip",
            media_type="application/zip",
        )
        artifact_fingerprint = evidence.artifact.fingerprint()
        try:
            transaction = (
                immediate_transaction(self.connection) if manage_transaction else nullcontext()
            )
            with transaction:
                artifact_row = self.connection.execute(
                    "SELECT * FROM calibration_artifacts WHERE artifact_fingerprint = ?",
                    (artifact_fingerprint,),
                ).fetchone()
                if artifact_row is None:
                    artifact_id = new_id("calibration_artifact")
                    self.connection.execute(
                        """
                        INSERT INTO calibration_artifacts(
                            id, catalog_id, catalog_version, catalog_fingerprint,
                            profile_id, profile_fingerprint, artifact_fingerprint, manifest_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact_id,
                            evidence.catalog_id,
                            evidence.catalog_version,
                            evidence.catalog_fingerprint,
                            evidence.artifact.profile_id,
                            evidence.artifact.profile_fingerprint,
                            artifact_fingerprint,
                            evidence.artifact.canonical_json(),
                        ),
                    )
                    for member in evidence.members:
                        if member.role not in {
                            CalibrationEvidenceRole.COUPON_SVG,
                            CalibrationEvidenceRole.COUPON_PNG,
                        }:
                            continue
                        self._insert_member(
                            table="calibration_artifact_members",
                            owner_column="artifact_id",
                            owner_id=artifact_id,
                            member=member,
                            blob=stored[member.archive_path],
                        )
                else:
                    artifact_id = artifact_row["id"]
                    if (
                        artifact_row["manifest_json"] != evidence.artifact.canonical_json()
                        or artifact_row["catalog_id"] != evidence.catalog_id
                        or artifact_row["catalog_version"] != evidence.catalog_version
                        or artifact_row["catalog_fingerprint"] != evidence.catalog_fingerprint
                    ):
                        raise CalibrationRegistryConflictError(
                            "calibration artifact fingerprint conflicts with stored manifest"
                        )

                self.connection.execute(
                    """
                    INSERT INTO calibration_runs(
                        id, artifact_id, catalog_id, catalog_version, catalog_fingerprint,
                        profile_id, profile_fingerprint, printer_id, nozzle_id, material_class,
                        process_fingerprint, process_json,
                        record_sha256, evidence_sha256, source_bundle_sha256, record_json,
                        source_bundle_relative_path, source_bundle_byte_size,
                        source_bundle_media_type, source_bundle_extension, evidence_json,
                        slicer_json, filament_json, attestation_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        record_id,
                        artifact_id,
                        evidence.catalog_id,
                        evidence.catalog_version,
                        evidence.catalog_fingerprint,
                        evidence.profile.id,
                        evidence.artifact.profile_fingerprint,
                        evidence.record.printer_id,
                        evidence.record.nozzle_id,
                        evidence.record.material_class,
                        process_fingerprint,
                        process_json,
                        record_sha256,
                        evidence_sha256,
                        parsed.bundle_sha256,
                        record_json,
                        source_bundle.relative_path,
                        source_bundle.byte_size,
                        source_bundle.media_type,
                        source_bundle.extension,
                        evidence.canonical_json(),
                        canonical_json(evidence.slicer.model_dump(mode="json")),
                        canonical_json(
                            [item.model_dump(mode="json") for item in evidence.filaments]
                        ),
                        canonical_json(evidence.attestation.model_dump(mode="json")),
                    ),
                )
                for member in evidence.members:
                    if member.role in {
                        CalibrationEvidenceRole.COUPON_SVG,
                        CalibrationEvidenceRole.COUPON_PNG,
                    }:
                        continue
                    self._insert_member(
                        table="calibration_run_members",
                        owner_column="run_id",
                        owner_id=record_id,
                        member=member,
                        blob=stored[member.archive_path],
                    )
        except sqlite3.IntegrityError as error:
            existing = self._find_run_row(record_id)
            if existing is not None and existing["evidence_sha256"] == evidence_sha256:
                return CalibrationImportResult(run=self.get(record_id), duplicate=True)
            raise CalibrationRegistryConflictError(
                "calibration evidence conflicts with an immutable retained identity"
            ) from error
        return CalibrationImportResult(run=self.get(record_id), duplicate=False)

    def get(self, run_id: str) -> CalibrationRunResource:
        row = self._find_run_row(run_id)
        if row is None:
            raise CalibrationRegistryNotFoundError(f"calibration run not found: {run_id}")
        return self._resource(row)

    def list(self, *, profile_id: Optional[str] = None) -> tuple[CalibrationRunResource, ...]:
        if profile_id:
            rows = self.connection.execute(
                """
                SELECT * FROM calibration_runs
                WHERE profile_id = ? ORDER BY imported_at DESC, id DESC
                """,
                (profile_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM calibration_runs ORDER BY imported_at DESC, id DESC"
            ).fetchall()
        return tuple(self._resource(row) for row in rows)

    def member(self, run_id: str, member_id: str) -> CalibrationMemberResource:
        run = self.get(run_id)
        match = next(
            (item for item in (*run.artifact.members, *run.members) if item.id == member_id),
            None,
        )
        if match is None:
            raise CalibrationRegistryNotFoundError(
                f"calibration evidence member not found: {member_id}"
            )
        return match

    def _validate_catalog_and_observations(self, evidence: CalibrationEvidenceManifest) -> None:
        if (
            evidence.catalog_id != self.catalog.catalog_id
            or evidence.catalog_version != self.catalog.catalog_version
            or evidence.catalog_fingerprint != self.catalog.fingerprint()
        ):
            raise CalibrationRegistryConflictError(
                "calibration evidence does not match the retained profile catalog"
            )
        profile = self.catalog.profile(evidence.profile.id)
        if profile is None or profile != evidence.profile:
            raise CalibrationRegistryConflictError(
                "calibration evidence profile snapshot is unknown or altered"
            )
        process_profiles = {
            item.name for item in evidence.slicer.profiles if item.role == "process"
        }
        if evidence.record.slicer_profile not in process_profiles:
            raise CalibrationRegistryError(
                "calibration record does not name an exact pinned slicer process profile"
            )
        matching_filaments = [
            item for item in evidence.filaments if item.name == evidence.record.filament
        ]
        if len(matching_filaments) != 1:
            raise CalibrationRegistryError(
                "calibration record must resolve one exact filament snapshot"
            )
        if matching_filaments[0].material.lower() != evidence.record.material_class.lower():
            raise CalibrationRegistryError(
                "calibration filament material does not match the printability profile"
            )
        filament_profiles = {
            item.name for item in evidence.slicer.profiles if item.role == "filament"
        }
        if evidence.record.filament not in filament_profiles:
            raise CalibrationRegistryError(
                "calibration filament does not name an exact pinned slicer filament profile"
            )
        features = {item.id: item for item in evidence.artifact.features}
        for observation in evidence.record.observations:
            feature = features[observation.feature_id]
            if (
                observation.outcome == CalibrationOutcome.FAIL
                and observation.measured_dimension_mm is None
            ):
                raise CalibrationRegistryError(
                    f"failed feature {observation.feature_id} requires a measured "
                    "dimension; use 0 for vanished features"
                )
            if feature.rasterized_width_mm <= 0 or feature.rasterized_height_mm <= 0:
                raise CalibrationRegistryError(
                    f"feature {observation.feature_id} lacks exact rasterized X/Y dimensions"
                )

    def _resource(self, row: sqlite3.Row) -> CalibrationRunResource:
        try:
            evidence = CalibrationEvidenceManifest.model_validate_json(row["evidence_json"])
        except Exception as error:
            raise CalibrationRegistryCorruptError(
                f"stored calibration evidence is invalid: {row['id']}"
            ) from error
        if evidence.fingerprint() != row["evidence_sha256"]:
            raise CalibrationRegistryCorruptError(
                f"stored calibration evidence fingerprint is invalid: {row['id']}"
            )
        expected_record = evidence.record.canonical_json()
        expected_record_sha256 = hashlib.sha256(expected_record.encode("utf-8")).hexdigest()
        if row["record_json"] != expected_record or row["record_sha256"] != expected_record_sha256:
            raise CalibrationRegistryCorruptError(
                f"stored calibration record identity is invalid: {row['id']}"
            )
        expected_columns = {
            "catalog_id": evidence.catalog_id,
            "catalog_version": evidence.catalog_version,
            "catalog_fingerprint": evidence.catalog_fingerprint,
            "profile_id": evidence.profile.id,
            "profile_fingerprint": evidence.artifact.profile_fingerprint,
            "printer_id": evidence.record.printer_id,
            "nozzle_id": evidence.record.nozzle_id,
            "material_class": evidence.record.material_class,
        }
        if any(row[name] != value for name, value in expected_columns.items()):
            raise CalibrationRegistryCorruptError(
                f"stored calibration index fields are invalid: {row['id']}"
            )
        expected_process_json = canonical_json(_process_identity(evidence))
        expected_process_fingerprint = hashlib.sha256(
            expected_process_json.encode("utf-8")
        ).hexdigest()
        if (
            row["process_json"] != expected_process_json
            or row["process_fingerprint"] != expected_process_fingerprint
        ):
            raise CalibrationRegistryCorruptError(
                f"stored calibration process identity is invalid: {row['id']}"
            )
        artifact_row = self.connection.execute(
            "SELECT * FROM calibration_artifacts WHERE id = ?", (row["artifact_id"],)
        ).fetchone()
        if artifact_row is None:
            raise CalibrationRegistryCorruptError(
                f"calibration artifact is missing for run: {row['id']}"
            )
        if (
            artifact_row["manifest_json"] != evidence.artifact.canonical_json()
            or artifact_row["artifact_fingerprint"] != evidence.artifact.fingerprint()
            or artifact_row["catalog_id"] != evidence.catalog_id
            or artifact_row["catalog_version"] != evidence.catalog_version
            or artifact_row["catalog_fingerprint"] != evidence.catalog_fingerprint
            or artifact_row["profile_id"] != evidence.profile.id
            or artifact_row["profile_fingerprint"] != evidence.artifact.profile_fingerprint
        ):
            raise CalibrationRegistryCorruptError(
                f"stored calibration artifact identity is invalid: {row['id']}"
            )
        artifact_members = self._members(
            "calibration_artifact_members", "artifact_id", artifact_row["id"]
        )
        run_members = self._members("calibration_run_members", "run_id", row["id"])
        declared_artifact_members = tuple(
            item
            for item in evidence.members
            if item.role in {CalibrationEvidenceRole.COUPON_SVG, CalibrationEvidenceRole.COUPON_PNG}
        )
        declared_run_members = tuple(
            item
            for item in evidence.members
            if item.role
            not in {CalibrationEvidenceRole.COUPON_SVG, CalibrationEvidenceRole.COUPON_PNG}
        )
        if not _member_identities_match(artifact_members, declared_artifact_members):
            raise CalibrationRegistryCorruptError(
                f"stored calibration artifact members are invalid: {row['id']}"
            )
        if not _member_identities_match(run_members, declared_run_members):
            raise CalibrationRegistryCorruptError(
                f"stored calibration run members are invalid: {row['id']}"
            )
        source_bundle = CalibrationSourceBundleResource(
            sha256=row["source_bundle_sha256"],
            relative_path=row["source_bundle_relative_path"],
            byte_size=row["source_bundle_byte_size"],
            media_type=row["source_bundle_media_type"],
            extension=row["source_bundle_extension"],
        )
        for member in (*artifact_members, *run_members, source_bundle):
            try:
                valid = self.blob_store.verify(member.stored_blob())
            except FileNotFoundError:
                valid = False
            if not valid:
                label = (
                    member.filename
                    if isinstance(member, CalibrationMemberResource)
                    else "source evidence bundle"
                )
                raise CalibrationRegistryCorruptError(
                    f"calibration evidence member is missing or corrupt: {label}"
                )
        try:
            stored_manifest = json.loads(artifact_row["manifest_json"])
        except json.JSONDecodeError as error:
            raise CalibrationRegistryCorruptError(
                "stored calibration artifact JSON is invalid"
            ) from error
        return CalibrationRunResource(
            id=row["id"],
            artifact=CalibrationArtifactResource(
                id=artifact_row["id"],
                catalog_id=artifact_row["catalog_id"],
                catalog_version=artifact_row["catalog_version"],
                catalog_fingerprint=artifact_row["catalog_fingerprint"],
                profile_id=artifact_row["profile_id"],
                profile_fingerprint=artifact_row["profile_fingerprint"],
                artifact_fingerprint=artifact_row["artifact_fingerprint"],
                manifest=stored_manifest,
                members=artifact_members,
                created_at=artifact_row["created_at"],
            ),
            catalog_id=row["catalog_id"],
            catalog_version=row["catalog_version"],
            catalog_fingerprint=row["catalog_fingerprint"],
            profile_id=row["profile_id"],
            profile_fingerprint=row["profile_fingerprint"],
            printer_id=row["printer_id"],
            nozzle_id=row["nozzle_id"],
            material_class=row["material_class"],
            process_fingerprint=row["process_fingerprint"],
            record_sha256=row["record_sha256"],
            evidence_sha256=row["evidence_sha256"],
            source_bundle_sha256=row["source_bundle_sha256"],
            source_bundle=source_bundle,
            evidence=evidence,
            members=run_members,
            imported_at=row["imported_at"],
        )

    def _members(
        self, table: str, owner_column: str, owner_id: str
    ) -> tuple[CalibrationMemberResource, ...]:
        rows = self.connection.execute(
            f"SELECT * FROM {table} WHERE {owner_column} = ? ORDER BY role, ordinal",  # noqa: S608
            (owner_id,),
        ).fetchall()
        return tuple(
            CalibrationMemberResource(
                id=item["id"],
                role=CalibrationEvidenceRole(item["role"]),
                ordinal=item["ordinal"],
                filename=item["filename"],
                sha256=item["sha256"],
                relative_path=item["relative_path"],
                byte_size=item["byte_size"],
                media_type=item["media_type"],
                extension=item["extension"],
            )
            for item in rows
        )

    def _insert_member(
        self,
        *,
        table: str,
        owner_column: str,
        owner_id: str,
        member: CalibrationEvidenceMember,
        blob: StoredBlob,
    ) -> None:
        if (
            blob.sha256 != member.sha256
            or blob.byte_size != member.byte_size
            or blob.media_type != member.media_type
            or blob.extension != member.extension
        ):
            raise CalibrationRegistryError("stored calibration member identity changed")
        self.connection.execute(
            f"""
            INSERT INTO {table}(
                id, {owner_column}, role, ordinal, filename, sha256, relative_path,
                byte_size, media_type, extension
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,  # noqa: S608
            (
                new_id("calibration_member"),
                owner_id,
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

    def _find_run_row(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM calibration_runs WHERE id = ?", (run_id,)
        ).fetchone()


def _member_identities_match(
    stored: tuple[CalibrationMemberResource, ...],
    declared: tuple[CalibrationEvidenceMember, ...],
) -> bool:
    stored_identities = tuple(
        (
            item.role,
            item.ordinal,
            item.filename,
            item.sha256,
            item.byte_size,
            item.media_type,
            item.extension,
        )
        for item in stored
    )
    declared_identities = tuple(
        (
            item.role,
            item.ordinal,
            item.filename,
            item.sha256,
            item.byte_size,
            item.media_type,
            item.extension,
        )
        for item in declared
    )
    return stored_identities == declared_identities


def _process_identity(evidence: CalibrationEvidenceManifest) -> dict:
    return {
        "layer_height_mm": evidence.record.layer_height_mm,
        "plate_id": evidence.record.plate_id,
        "slicer": evidence.slicer.model_dump(mode="json"),
        "filaments": [item.model_dump(mode="json") for item in evidence.filaments],
    }
