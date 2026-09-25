from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest
from calibration_evidence_helpers import complete_evidence_bundle
from fastapi.testclient import TestClient

from image23mf.api.app import create_app
from image23mf.calibration import (
    CalibrationEvidenceError,
    CalibrationEvidenceManifest,
    build_calibration_evidence_bundle,
    load_bundled_printability_catalog,
    parse_calibration_evidence_bundle,
)
from image23mf.calibration.registry import (
    CalibrationRegistry,
    CalibrationRegistryConflictError,
    CalibrationRegistryCorruptError,
)
from image23mf.settings import Settings
from image23mf.storage import ContentAddressedStore, open_database
from image23mf.workspace_bundles import WorkspaceBundleService
from image23mf.workspace_health import WorkspaceHealthService


def test_evidence_bundle_is_deterministic_closed_and_tamper_evident() -> None:
    payload, expected = complete_evidence_bundle()
    parsed = parse_calibration_evidence_bundle(payload)

    assert parsed.manifest == expected
    assert parsed.bundle_byte_size == len(payload)
    assert build_calibration_evidence_bundle(expected, parsed.payloads) == payload
    assert parsed.manifest.record.observations[0].measured_dimension_mm == 0
    assert {item.outcome.value for item in parsed.manifest.record.observations} == {
        "pass",
        "fail",
        "uncertain",
    }
    dot = next(item for item in parsed.manifest.artifact.features if item.kind.value == "dot")
    assert dot.rasterized_width_mm != dot.rasterized_height_mm


def test_evidence_bundle_rejects_altered_extra_and_unsafe_members() -> None:
    payload, _ = complete_evidence_bundle()

    altered = _rewrite_zip(payload, replace={"payload/photo.png": b"not the photo"})
    with pytest.raises(CalibrationEvidenceError, match="size mismatch|hash mismatch"):
        parse_calibration_evidence_bundle(altered)

    extra = _rewrite_zip(payload, add={"payload/undeclared.txt": b"extra"})
    with pytest.raises(CalibrationEvidenceError, match="not closed"):
        parse_calibration_evidence_bundle(extra)

    unsafe = _rewrite_zip(payload, add={"../escape.txt": b"escape"})
    with pytest.raises(CalibrationEvidenceError, match="unsafe member path"):
        parse_calibration_evidence_bundle(unsafe)


def test_evidence_manifest_rejects_incomplete_or_cross_profile_records() -> None:
    _, manifest = complete_evidence_bundle()
    payload = manifest.model_dump(mode="json")
    payload["record"]["printer_id"] = "some-other-printer"

    with pytest.raises(ValueError, match="setup does not match"):
        CalibrationEvidenceManifest.model_validate(payload)


def test_registry_import_is_immutable_idempotent_and_verifies_every_blob(tmp_path: Path) -> None:
    payload, manifest = complete_evidence_bundle()
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    registry = CalibrationRegistry(
        connection,
        ContentAddressedStore(workspace),
        load_bundled_printability_catalog(),
    )
    try:
        first = registry.import_bundle(payload)
        second = registry.import_bundle(payload)

        assert first.duplicate is False
        assert second.duplicate is True
        assert first.run.id == manifest.record.record_id
        assert first.run.source_bundle.sha256 == first.run.source_bundle_sha256
        assert len(first.run.process_fingerprint) == 64
        assert len(first.run.artifact.members) == 2
        assert len(first.run.members) == 4
        assert registry.list(profile_id=manifest.profile.id) == (first.run,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE calibration_runs SET printer_id = 'altered' WHERE id = ?",
                (first.run.id,),
            )

        source_path = ContentAddressedStore(workspace).path_for(
            first.run.source_bundle.relative_path
        )
        source_path.write_bytes(b"corrupt")
        with pytest.raises(CalibrationRegistryCorruptError, match="missing or corrupt"):
            registry.get(first.run.id)
    finally:
        connection.close()


def test_registry_rejects_altered_catalog_and_reused_run_identity(tmp_path: Path) -> None:
    payload, manifest = complete_evidence_bundle()
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    registry = CalibrationRegistry(
        connection,
        ContentAddressedStore(workspace),
        load_bundled_printability_catalog(),
    )
    try:
        registry.import_bundle(payload)
        conflicting_payload, conflicting = complete_evidence_bundle(
            run_id=manifest.record.record_id or ""
        )
        changed = conflicting.model_dump(mode="json")
        changed["attestation"]["attested_at"] = "2026-07-17T10:00:01Z"
        parsed = parse_calibration_evidence_bundle(conflicting_payload)
        changed_manifest = CalibrationEvidenceManifest.model_validate(changed)
        conflicting_payload = build_calibration_evidence_bundle(changed_manifest, parsed.payloads)
        with pytest.raises(CalibrationRegistryConflictError, match="already sealed"):
            registry.import_bundle(conflicting_payload)

        catalog_payload = load_bundled_printability_catalog().model_dump(mode="json")
        catalog_payload["catalog_version"] = "altered"
        altered_catalog = type(load_bundled_printability_catalog()).model_validate(catalog_payload)
        altered_registry = CalibrationRegistry(
            connection, ContentAddressedStore(workspace), altered_catalog
        )
        new_payload, _ = complete_evidence_bundle(run_id="calibration-run-p2s-04-physical-0002")
        with pytest.raises(CalibrationRegistryConflictError, match="retained profile catalog"):
            altered_registry.import_bundle(new_payload)
    finally:
        connection.close()


def test_calibration_api_import_inspect_filter_and_download_user_flow(tmp_path: Path) -> None:
    payload, manifest = complete_evidence_bundle()
    app = create_app(Settings(workspace=tmp_path / "workspace"))

    with TestClient(app) as client:
        imported = client.post(
            "/api/calibration/runs/import",
            content=payload,
            headers={"content-type": "application/zip"},
        )
        duplicate = client.post(
            "/api/calibration/runs/import",
            content=payload,
            headers={"content-type": "application/zip"},
        )
        listed = client.get("/api/calibration/runs", params={"profile_id": manifest.profile.id})
        run_id = manifest.record.record_id
        inspected = client.get(f"/api/calibration/runs/{run_id}")
        bundle = client.get(f"/api/calibration/runs/{run_id}/bundle")
        first_member = imported.json()["run"]["artifact"]["members"][0]
        member = client.get(first_member["download_url"])
        missing = client.get("/api/calibration/runs/calibration-run-missing")
        unsupported = client.post(
            "/api/calibration/runs/import",
            content=payload,
            headers={"content-type": "image/png"},
        )

    assert imported.status_code == 201
    assert imported.json()["duplicate"] is False
    assert imported.json()["run"]["integrity"] == "verified"
    assert len(imported.json()["run"]["process_fingerprint"]) == 64
    assert duplicate.status_code == 201
    assert duplicate.json()["duplicate"] is True
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert inspected.status_code == 200
    assert inspected.json()["evidence"]["record"]["observations"][0]["measured_dimension_mm"] == 0
    assert bundle.status_code == 200
    assert bundle.content == payload
    assert member.status_code == 200
    assert member.headers["content-type"].startswith(first_member["media_type"])
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "unsupported_media"


def test_workspace_backup_restores_the_complete_calibration_evidence_chain(
    tmp_path: Path,
) -> None:
    payload, manifest = complete_evidence_bundle()
    source = tmp_path / "source"
    connection = open_database(source / "image23mf.sqlite3")
    try:
        imported = CalibrationRegistry(
            connection,
            ContentAddressedStore(source),
            load_bundled_printability_catalog(),
        ).import_bundle(payload)
    finally:
        connection.close()

    backup = WorkspaceBundleService(source).export(tmp_path / "calibration.workspace")
    target = tmp_path / "restored"
    WorkspaceBundleService(source).restore(backup.path, target)
    restored_connection = open_database(target / "image23mf.sqlite3")
    try:
        restored = CalibrationRegistry(
            restored_connection,
            ContentAddressedStore(target),
            load_bundled_printability_catalog(),
        ).get(manifest.record.record_id or "")
        restored_bytes = (
            ContentAddressedStore(target)
            .path_for(restored.source_bundle.relative_path)
            .read_bytes()
        )
    finally:
        restored_connection.close()

    assert restored.evidence_sha256 == imported.run.evidence_sha256
    assert restored_bytes == payload


def test_workspace_health_and_gc_preserve_every_calibration_evidence_blob(
    tmp_path: Path,
) -> None:
    payload, _ = complete_evidence_bundle()
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        run = (
            CalibrationRegistry(connection, store, load_bundled_printability_catalog())
            .import_bundle(payload)
            .run
        )
        retained_paths = {
            run.source_bundle.relative_path,
            *(item.relative_path for item in run.artifact.members),
            *(item.relative_path for item in run.members),
        }
        for relative_path in retained_paths:
            path = store.path_for(relative_path)
            path.touch()

        service = WorkspaceHealthService(connection, store)
        report = service.inspect()
        plan = service.plan_garbage_collection(minimum_age_seconds=0)

        assert report.orphan_file_count == 0
        assert report.referenced_file_count == len(retained_paths)
        assert retained_paths.isdisjoint(item.relative_path for item in plan.candidates)
    finally:
        connection.close()


def _rewrite_zip(
    payload: bytes,
    *,
    replace: dict[str, bytes] | None = None,
    add: dict[str, bytes] | None = None,
) -> bytes:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload), "r") as source,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            target.writestr(info.filename, (replace or {}).get(info.filename, source.read(info)))
        for name, data in (add or {}).items():
            target.writestr(name, data)
    return output.getvalue()
