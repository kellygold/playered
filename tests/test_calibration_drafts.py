from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from calibration_evidence_helpers import complete_evidence_bundle
from fastapi.testclient import TestClient
from PIL import Image

from image23mf.api.app import create_app
from image23mf.calibration.artifact import CalibrationRunStatus
from image23mf.calibration.catalogs import (
    CalibrationCatalogRepository,
    PersistentPrintabilityProfileService,
)
from image23mf.calibration.drafts import (
    CalibrationDraftAttestRequest,
    CalibrationDraftConflictError,
    CalibrationDraftCorruptError,
    CalibrationDraftCreateRequest,
    CalibrationDraftInvalidError,
    CalibrationDraftMetadata,
    CalibrationDraftMutationRequest,
    CalibrationDraftService,
    CalibrationDraftUpdate,
)
from image23mf.calibration.evidence import (
    CalibrationEvidenceRole,
    parse_calibration_evidence_bundle,
)
from image23mf.calibration.registry import CalibrationRegistryError
from image23mf.settings import Settings
from image23mf.storage import ContentAddressedStore, open_database
from image23mf.workspace_bundles import (
    WorkspaceBundleIntegrityError,
    WorkspaceBundleService,
)


def _draft_metadata(manifest) -> CalibrationDraftMetadata:
    profiles = {item.role: item for item in manifest.slicer.profiles}
    filament = manifest.filaments[0]
    return CalibrationDraftMetadata(
        slicer_application=manifest.slicer.application,
        slicer_version=manifest.slicer.version,
        slicer_executable_sha256=manifest.slicer.executable_sha256,
        machine_profile_name=profiles["machine"].name,
        machine_profile_sha256=profiles["machine"].sha256,
        process_profile_name=profiles["process"].name,
        process_profile_sha256=profiles["process"].sha256,
        filament_profile_name=profiles["filament"].name,
        filament_profile_sha256=profiles["filament"].sha256,
        filament_id=filament.filament_id,
        filament_manufacturer=filament.manufacturer,
        filament_family=filament.family,
        filament_name=filament.name,
        filament_material=filament.material,
        filament_finish=filament.finish,
        filament_color_hex=filament.color_hex,
    )


def _png_bytes(color: tuple[int, int, int] = (24, 36, 48)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 4), color).save(output, format="PNG")
    return output.getvalue()


def _complete_draft(workspace: Path, connection, run_id: str):
    bundle, manifest = complete_evidence_bundle(
        run_id=run_id,
        observation_mode="monotonic",
    )
    parsed = parse_calibration_evidence_bundle(bundle)
    service = CalibrationDraftService(connection, ContentAddressedStore(workspace))
    active = CalibrationCatalogRepository(connection).active()
    draft = service.create(
        CalibrationDraftCreateRequest(
            profile_id=manifest.profile.id,
            expected_catalog_fingerprint=active.fingerprint,
        )
    )
    draft = service.update(
        draft.id,
        CalibrationDraftUpdate(
            expected_generation=draft.generation,
            record=manifest.record.model_copy(
                update={
                    "status": CalibrationRunStatus.TEMPLATE,
                    "artifact_fingerprint": draft.artifact.fingerprint(),
                }
            ),
            metadata=_draft_metadata(manifest),
        ),
    )
    declared = {(member.role, member.ordinal): member for member in parsed.manifest.members}
    for role, ordinal in (
        (CalibrationEvidenceRole.PROJECT_3MF, 0),
        (CalibrationEvidenceRole.SLICED_GCODE, 0),
        (CalibrationEvidenceRole.SLICER_SETTINGS, 0),
        (CalibrationEvidenceRole.PHOTO, 0),
    ):
        member = declared[(role, ordinal)]
        draft = service.upload(
            draft.id,
            role=role,
            ordinal=ordinal,
            media_type=member.media_type,
            payload=parsed.payloads[member.archive_path],
            expected_generation=draft.generation,
        )
    return service, draft, manifest


def test_resumable_draft_attestation_is_generation_bound_and_finalization_is_atomic(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    PersistentPrintabilityProfileService(database_path)
    connection = open_database(database_path)
    service, draft, manifest = _complete_draft(
        workspace,
        connection,
        "calibration-run-p2s-04-draft-0001",
    )

    assert draft.readiness.evidence_state == "unverified_draft"
    assert draft.readiness.ready_to_attest is True
    assert [item.code for item in draft.readiness.blockers] == ["attestation_missing"]
    assert any(item.measured_dimension_mm == 0 for item in draft.record.observations)
    recovered_generation = draft.generation
    connection.close()

    restarted = open_database(database_path)
    service = CalibrationDraftService(restarted, ContentAddressedStore(workspace))
    recovered = service.get(draft.id)
    assert recovered.generation == recovered_generation
    assert recovered.candidate_sha256 == draft.candidate_sha256

    attested = service.attest(
        draft.id,
        CalibrationDraftAttestRequest(
            expected_generation=recovered.generation,
            confirmed=True,
        ),
    )
    assert attested.readiness.evidence_state == "attested_draft"
    assert attested.attested_candidate_sha256 == attested.candidate_sha256
    assert attested.readiness.ready_to_finalize is True

    changed_record = attested.record.model_copy(update={"notes": "Rechecked under task light."})
    changed = service.update(
        draft.id,
        CalibrationDraftUpdate(
            expected_generation=attested.generation,
            record=changed_record,
            metadata=attested.metadata,
        ),
    )
    assert changed.attested_at is None
    assert changed.attested_candidate_sha256 is None
    assert changed.candidate_sha256 != attested.candidate_sha256
    with pytest.raises(CalibrationDraftConflictError, match="stale"):
        service.attest(
            draft.id,
            CalibrationDraftAttestRequest(
                expected_generation=attested.generation,
                confirmed=True,
            ),
        )

    reattested = service.attest(
        draft.id,
        CalibrationDraftAttestRequest(
            expected_generation=changed.generation,
            confirmed=True,
        ),
    )
    finalized = service.finalize(
        draft.id,
        CalibrationDraftMutationRequest(expected_generation=reattested.generation),
    )
    assert finalized.imported.duplicate is False
    assert finalized.imported.run.id == manifest.record.record_id
    assert finalized.draft.finalized_run_id == manifest.record.record_id
    assert finalized.draft.readiness.evidence_state == "sealed_physical_evidence"
    assert finalized.draft.record.status == CalibrationRunStatus.TEMPLATE
    retried = service.finalize(
        draft.id,
        CalibrationDraftMutationRequest(expected_generation=reattested.generation),
    )
    assert retried.imported.duplicate is True
    assert retried.imported.run.id == finalized.imported.run.id
    assert retried.draft == finalized.draft
    with pytest.raises(CalibrationDraftConflictError, match="already finalized"):
        service.update(
            draft.id,
            CalibrationDraftUpdate(
                expected_generation=finalized.draft.generation,
                record=finalized.draft.record,
                metadata=finalized.draft.metadata,
            ),
        )
    restarted.close()


def test_draft_readiness_requires_the_exact_pinned_filament_profile_name(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    PersistentPrintabilityProfileService(database_path)
    connection = open_database(database_path)
    service, draft, _ = _complete_draft(
        workspace,
        connection,
        "calibration-run-p2s-04-profile-name-0001",
    )

    mismatched = service.update(
        draft.id,
        CalibrationDraftUpdate(
            expected_generation=draft.generation,
            record=draft.record,
            metadata=draft.metadata.model_copy(
                update={"filament_profile_name": "A different pinned filament profile"}
            ),
        ),
    )

    blocker = next(
        item for item in mismatched.readiness.blockers if item.code == "filament_profile_mismatch"
    )
    assert blocker.field == "metadata.filament_profile_name"
    assert "match exactly" in blocker.message
    assert mismatched.readiness.ready_to_attest is False
    assert mismatched.readiness.ready_to_finalize is False
    with pytest.raises(CalibrationDraftInvalidError, match="pinned filament profile"):
        service.attest(
            mismatched.id,
            CalibrationDraftAttestRequest(
                expected_generation=mismatched.generation,
                confirmed=True,
            ),
        )
    connection.close()


def test_finalize_api_maps_registry_validation_failures_to_typed_invalid_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        catalogs = client.get("/api/printability-profile-catalogs").json()
        active = next(item for item in catalogs["items"] if item["active"])
        profile_id = active["catalog"]["profiles"][0]["id"]
        created = client.post(
            "/api/calibration/drafts",
            json={
                "profile_id": profile_id,
                "expected_catalog_fingerprint": active["fingerprint"],
            },
        ).json()

        def reject_invalid_evidence(*_args, **_kwargs):
            raise CalibrationRegistryError("calibration evidence invariant failed")

        monkeypatch.setattr(CalibrationDraftService, "finalize", reject_invalid_evidence)
        response = client.post(
            f"/api/calibration/drafts/{created['id']}/finalize",
            json={"expected_generation": created["generation"]},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["message"] == "calibration evidence invariant failed"


def test_incomplete_draft_and_attachments_survive_workspace_backup_restore(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    PersistentPrintabilityProfileService(database_path)
    connection = open_database(database_path)
    service = CalibrationDraftService(connection, ContentAddressedStore(workspace))
    active = CalibrationCatalogRepository(connection).active()
    profile_id = active.catalog.profiles[0].id
    created = service.create(
        CalibrationDraftCreateRequest(
            profile_id=profile_id,
            expected_catalog_fingerprint=active.fingerprint,
        )
    )
    photo_payload = _png_bytes()
    staged = service.upload(
        created.id,
        role=CalibrationEvidenceRole.PHOTO,
        ordinal=0,
        media_type="image/png",
        payload=photo_payload,
        expected_generation=created.generation,
    )
    staged_member = next(
        member for member in staged.members if member.role == CalibrationEvidenceRole.PHOTO
    )
    connection.close()

    bundle = WorkspaceBundleService(workspace).export(tmp_path / "workspace.bundle").path
    restored_workspace = tmp_path / "restored"
    WorkspaceBundleService(workspace).restore(bundle, restored_workspace)

    restored_connection = open_database(restored_workspace / "image23mf.sqlite3")
    restored_service = CalibrationDraftService(
        restored_connection, ContentAddressedStore(restored_workspace)
    )
    restored = restored_service.get(created.id)
    restored_member, relative_path = restored_service.member(created.id, staged_member.id)
    assert restored.generation == staged.generation
    assert restored.candidate_sha256 == staged.candidate_sha256
    assert restored.readiness == staged.readiness
    assert restored_member == staged_member
    assert (restored_workspace / relative_path).read_bytes() == photo_payload
    restored_connection.close()


def test_backup_and_draft_reads_refuse_corrupt_staged_attachment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    PersistentPrintabilityProfileService(database_path)
    connection = open_database(database_path)
    service = CalibrationDraftService(connection, ContentAddressedStore(workspace))
    active = CalibrationCatalogRepository(connection).active()
    created = service.create(
        CalibrationDraftCreateRequest(
            profile_id=active.catalog.profiles[0].id,
            expected_catalog_fingerprint=active.fingerprint,
        )
    )
    staged = service.upload(
        created.id,
        role=CalibrationEvidenceRole.PHOTO,
        ordinal=0,
        media_type="image/png",
        payload=_png_bytes(),
        expected_generation=created.generation,
    )
    relative_path = connection.execute(
        "SELECT relative_path FROM calibration_run_draft_members WHERE draft_id = ? AND role = ?",
        (staged.id, CalibrationEvidenceRole.PHOTO.value),
    ).fetchone()[0]
    (workspace / relative_path).write_bytes(b"tampered-photo")

    with pytest.raises(CalibrationDraftCorruptError, match="identity|corrupt"):
        service.get(staged.id)
    connection.close()
    with pytest.raises(WorkspaceBundleIntegrityError, match="identity is corrupt"):
        WorkspaceBundleService(workspace).export(tmp_path / "corrupt.bundle")


def test_removing_a_photo_compacts_ordinals_for_the_next_resumable_upload(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    PersistentPrintabilityProfileService(database_path)
    connection = open_database(database_path)
    service = CalibrationDraftService(connection, ContentAddressedStore(workspace))
    active = CalibrationCatalogRepository(connection).active()
    current = service.create(
        CalibrationDraftCreateRequest(
            profile_id=active.catalog.profiles[0].id,
            expected_catalog_fingerprint=active.fingerprint,
        )
    )
    for ordinal, color in enumerate(((10, 20, 30), (40, 50, 60), (70, 80, 90))):
        current = service.upload(
            current.id,
            role=CalibrationEvidenceRole.PHOTO,
            ordinal=ordinal,
            media_type="image/png",
            payload=_png_bytes(color),
            expected_generation=current.generation,
        )

    current = service.remove(
        current.id,
        role=CalibrationEvidenceRole.PHOTO,
        ordinal=1,
        expected_generation=current.generation,
    )
    photos = [item for item in current.members if item.role == CalibrationEvidenceRole.PHOTO]
    assert [(item.ordinal, item.filename) for item in photos] == [
        (0, "photo-00.png"),
        (1, "photo-01.png"),
    ]

    current = service.upload(
        current.id,
        role=CalibrationEvidenceRole.PHOTO,
        ordinal=2,
        media_type="image/png",
        payload=_png_bytes((100, 110, 120)),
        expected_generation=current.generation,
    )
    assert [
        item.ordinal for item in current.members if item.role == CalibrationEvidenceRole.PHOTO
    ] == [0, 1, 2]
    connection.close()


def test_specimen_bundle_is_deterministic_and_explicitly_not_observed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    PersistentPrintabilityProfileService(database_path)
    connection = open_database(database_path)
    service = CalibrationDraftService(connection, ContentAddressedStore(workspace))
    profile_id = CalibrationCatalogRepository(connection).active().catalog.profiles[0].id

    first, media_type, filename = service.specimen_bytes(profile_id, "bundle")
    second, _, _ = service.specimen_bytes(profile_id, "bundle")

    assert first == second
    assert media_type == "application/zip"
    assert filename.endswith("-specimen.zip")
    with zipfile.ZipFile(BytesIO(first)) as archive:
        assert archive.namelist() == [
            "specimen.json",
            "coupon.svg",
            "coupon.png",
            "manifest.json",
            "record-template.json",
        ]
        metadata = json.loads(archive.read("specimen.json"))
    assert metadata["evidence_state"] == "not_observed"
    assert "not physically observed" in metadata["evidence_message"]
    connection.close()


def test_calibration_draft_http_surface_recovers_stale_and_incomplete_work(tmp_path: Path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        catalogs = client.get("/api/printability-profile-catalogs").json()
        active = next(item for item in catalogs["items"] if item["active"])
        profile_id = active["catalog"]["profiles"][0]["id"]
        specimen = client.get(f"/api/calibration/specimens/{profile_id}")
        created = client.post(
            "/api/calibration/drafts",
            json={
                "profile_id": profile_id,
                "expected_catalog_fingerprint": active["fingerprint"],
            },
        )
        listed = client.get("/api/calibration/drafts")
        stale = client.put(
            f"/api/calibration/drafts/{created.json()['id']}",
            json={
                "expected_generation": created.json()["generation"] + 1,
                "record": created.json()["record"],
                "metadata": created.json()["metadata"],
            },
        )
        premature = client.post(
            f"/api/calibration/drafts/{created.json()['id']}/attest",
            json={"expected_generation": created.json()["generation"], "confirmed": True},
        )

    assert specimen.status_code == 200
    assert specimen.json()["evidence_state"] == "not_observed"
    assert created.status_code == 201, created.text
    assert created.json()["readiness"]["evidence_state"] == "preparation"
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert stale.status_code == 409
    assert premature.status_code == 422
    assert "incomplete" in premature.json()["error"]["message"]
