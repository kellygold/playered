from __future__ import annotations

import copy
import io
import sqlite3
from pathlib import Path

import pytest
from calibration_evidence_helpers import complete_evidence_bundle
from fastapi.testclient import TestClient
from PIL import Image

from image23mf.api.app import create_app
from image23mf.calibration.catalogs import (
    CalibrationCatalogRepository,
    PersistentPrintabilityProfileService,
)
from image23mf.calibration.models import (
    CalibrationBasis,
    PrintabilityOverrides,
    ResolvePrintabilityRequest,
    load_bundled_printability_catalog,
)
from image23mf.calibration.promotion import (
    CalibrationPromotionError,
    CalibrationPromotionService,
    CalibrationProposalConflictError,
    CalibrationProposalState,
)
from image23mf.calibration.registry import CalibrationRegistry
from image23mf.settings import Settings
from image23mf.storage import ContentAddressedStore, open_database


def _import_run(workspace: Path, connection, run_id: str, *, mode: str = "monotonic"):
    payload, manifest = complete_evidence_bundle(run_id=run_id, observation_mode=mode)
    run = (
        CalibrationRegistry(
            connection,
            ContentAddressedStore(workspace),
            load_bundled_printability_catalog(),
        )
        .import_bundle(payload)
        .run
    )
    return run, manifest


def test_proposal_derivation_acceptance_and_historical_resolution_are_explicit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    connection = open_database(database_path)
    store = ContentAddressedStore(workspace)
    base_catalog = load_bundled_printability_catalog()
    base_fingerprint = base_catalog.fingerprint()
    try:
        first, _ = _import_run(workspace, connection, "calibration-run-p2s-04-promotion-0001")
        second, _ = _import_run(workspace, connection, "calibration-run-p2s-04-promotion-0002")
        service = CalibrationPromotionService(connection, store)
        proposal = service.derive(
            profile_id=first.profile_id,
            run_ids=tuple(sorted((first.id, second.id))),
            expected_catalog_fingerprint=base_fingerprint,
        )

        assert proposal.state == CalibrationProposalState.PENDING
        assert proposal.proposal.process_fingerprint == first.process_fingerprint
        assert len(proposal.proposal.contributions) == 60
        assert {item.status for item in proposal.proposal.analyses} == {"proposed"}
        assert proposal.proposal.base_evidence_status.value == "pending_print_calibration"
        assert proposal.proposal.proposed_evidence_status.value == "partially_validated"
        assert {item.name for item in proposal.proposal.recommendation_changes} == {
            "maximum_tiny_hole_area_mm2",
            "maximum_tiny_hole_diameter_mm",
            "minimum_gap_width_mm",
            "minimum_island_area_mm2",
            "minimum_island_diameter_mm",
            "minimum_line_width_mm",
            "minimum_neck_width_mm",
        }
        island_change = next(
            item
            for item in proposal.proposal.recommendation_changes
            if item.name == "minimum_island_diameter_mm"
        )
        assert island_change.before.basis == CalibrationBasis.ENGINEERING_BASELINE
        assert island_change.after.basis == CalibrationBasis.PRINTED_CALIBRATION
        assert island_change.before.value != island_change.after.value
        proposed_profile = proposal.proposal.proposed_catalog.profile(first.profile_id)
        assert proposed_profile is not None
        assert proposed_profile.evidence_status.value == "partially_validated"
        assert (
            proposed_profile.recommendations.minimum_island_diameter_mm.basis
            == CalibrationBasis.PRINTED_CALIBRATION
        )
        assert proposed_profile.recommendations.minimum_ring_width_mm.basis == (
            CalibrationBasis.ENGINEERING_BASELINE
        )
        assert proposed_profile.recommendations.long_line_minimum_length_mm.basis == (
            CalibrationBasis.ENGINEERING_BASELINE
        )
        assert proposed_profile.recommendations.smoothing_radius_mm.basis == (
            CalibrationBasis.ENGINEERING_BASELINE
        )
        with pytest.raises(CalibrationProposalConflictError, match="base"):
            service.accept(
                proposal.proposal.id,
                expected_catalog_fingerprint="f" * 64,
                reviewer="Kelly",
                reason="Reviewed physical transition evidence.",
            )
        assert CalibrationCatalogRepository(connection).active().fingerprint == base_fingerprint

        accepted = service.accept(
            proposal.proposal.id,
            expected_catalog_fingerprint=base_fingerprint,
            reviewer="Kelly",
            reason="Reviewed physical transition evidence.",
        )

        assert accepted.proposal.state == CalibrationProposalState.ACCEPTED
        assert accepted.active_catalog.fingerprint == proposal.proposal.proposed_catalog_fingerprint
        catalogs = CalibrationCatalogRepository(connection).list()
        assert len(catalogs.items) == 2
        assert {item.fingerprint for item in catalogs.items} == {
            base_fingerprint,
            proposal.proposal.proposed_catalog_fingerprint,
        }
        with pytest.raises(sqlite3.IntegrityError, match="requires promotion history"):
            connection.execute(
                "UPDATE calibration_catalog_head SET fingerprint = ? WHERE singleton = 'active'",
                (base_fingerprint,),
            )
        with pytest.raises(CalibrationProposalConflictError, match="already reviewed"):
            service.accept(
                proposal.proposal.id,
                expected_catalog_fingerprint=base_fingerprint,
                reviewer="Kelly",
                reason="Duplicate review",
            )
    finally:
        connection.close()

    resolver = PersistentPrintabilityProfileService(database_path)
    request = ResolvePrintabilityRequest(
        printer_id="bambu-p2s",
        nozzle_id="nozzle-0.4-hardened-steel",
        material_class="pla",
        overrides=PrintabilityOverrides(),
    )
    active = resolver.resolve(request)
    historical = resolver.resolve(request, catalog_fingerprint=base_fingerprint)
    assert active.profile_catalog_fingerprint != historical.profile_catalog_fingerprint
    assert (
        active.value("maximum_tiny_hole_diameter_mm").value
        < historical.value("maximum_tiny_hole_diameter_mm").value
    )


def test_uncertain_feature_is_excluded_without_blocking_other_reviewable_transitions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        run, _ = _import_run(
            workspace,
            connection,
            "calibration-run-p2s-04-uncertain-0001",
            mode="uncertain-dot",
        )
        proposal = CalibrationPromotionService(connection, store).derive(
            profile_id=run.profile_id,
            run_ids=(run.id,),
            expected_catalog_fingerprint=run.catalog_fingerprint,
        )
        dot = next(item for item in proposal.proposal.analyses if item.feature_kind.value == "dot")
        proposed = proposal.proposal.proposed_catalog.profile(run.profile_id)
        base = load_bundled_printability_catalog().profile(run.profile_id)

        assert dot.status == "blocked"
        assert "uncertain" in dot.reason
        assert proposed is not None and base is not None
        assert (
            proposed.recommendations.minimum_island_diameter_mm
            == base.recommendations.minimum_island_diameter_mm
        )
        dot_contributions = [
            item for item in proposal.proposal.contributions if item.feature_kind.value == "dot"
        ]
        assert dot_contributions
        assert all(not item.included for item in dot_contributions)
        assert all("uncertain" in (item.exclusion_reason or "") for item in dot_contributions)
    finally:
        connection.close()


def test_rejection_is_terminal_and_does_not_move_catalog_head(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        run, _ = _import_run(workspace, connection, "calibration-run-p2s-04-reject-0001")
        service = CalibrationPromotionService(connection, store)
        proposal = service.derive(
            profile_id=run.profile_id,
            run_ids=(run.id,),
            expected_catalog_fingerprint=run.catalog_fingerprint,
        )
        rejected = service.reject(
            proposal.proposal.id,
            reviewer="Kelly",
            reason="Repeat the print with a different plate.",
        )

        assert rejected.state == CalibrationProposalState.REJECTED
        assert CalibrationCatalogRepository(connection).active().fingerprint == (
            run.catalog_fingerprint
        )
        with pytest.raises(CalibrationProposalConflictError, match="already reviewed"):
            service.reject(
                proposal.proposal.id,
                reviewer="Kelly",
                reason="Duplicate",
            )
    finally:
        connection.close()


def test_cross_process_runs_and_nonmonotonic_feature_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        standard, _ = _import_run(workspace, connection, "calibration-run-p2s-04-process-0001")
        alternate_payload, _ = complete_evidence_bundle(
            run_id="calibration-run-p2s-04-process-0002",
            observation_mode="monotonic",
            layer_height_mm=0.16,
        )
        alternate = (
            CalibrationRegistry(connection, store, load_bundled_printability_catalog())
            .import_bundle(alternate_payload)
            .run
        )
        service = CalibrationPromotionService(connection, store)

        with pytest.raises(CalibrationPromotionError, match="one exact physical process"):
            service.derive(
                profile_id=standard.profile_id,
                run_ids=tuple(sorted((standard.id, alternate.id))),
                expected_catalog_fingerprint=standard.catalog_fingerprint,
            )

        nonmonotonic, _ = _import_run(
            workspace,
            connection,
            "calibration-run-p2s-04-nonmonotonic-0001",
            mode="nonmonotonic-dot",
        )
        proposal = service.derive(
            profile_id=nonmonotonic.profile_id,
            run_ids=(nonmonotonic.id,),
            expected_catalog_fingerprint=nonmonotonic.catalog_fingerprint,
        )
        dot = next(item for item in proposal.proposal.analyses if item.feature_kind.value == "dot")
        assert dot.status == "blocked"
        assert "non-monotonic" in dot.reason
    finally:
        connection.close()


def test_competing_proposal_is_stale_after_another_catalog_is_accepted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        first, _ = _import_run(workspace, connection, "calibration-run-p2s-04-competing-0001")
        second, _ = _import_run(workspace, connection, "calibration-run-p2s-04-competing-0002")
        service = CalibrationPromotionService(connection, store)
        first_proposal = service.derive(
            profile_id=first.profile_id,
            run_ids=(first.id,),
            expected_catalog_fingerprint=first.catalog_fingerprint,
        )
        second_proposal = service.derive(
            profile_id=second.profile_id,
            run_ids=(second.id,),
            expected_catalog_fingerprint=second.catalog_fingerprint,
        )
        service.accept(
            first_proposal.proposal.id,
            expected_catalog_fingerprint=first.catalog_fingerprint,
            reviewer="Kelly",
            reason="Accept first reviewed run.",
        )

        with pytest.raises(CalibrationProposalConflictError, match="active calibration catalog"):
            service.accept(
                second_proposal.proposal.id,
                expected_catalog_fingerprint=second.catalog_fingerprint,
                reviewer="Kelly",
                reason="This review is now stale.",
            )

        assert service.get(second_proposal.proposal.id).state == CalibrationProposalState.PENDING
        assert len(CalibrationCatalogRepository(connection).list().items) == 2
    finally:
        connection.close()


def test_catalog_promotion_api_requires_explicit_current_head_review(tmp_path: Path) -> None:
    base_fingerprint = load_bundled_printability_catalog().fingerprint()
    first_payload, first_manifest = complete_evidence_bundle(
        run_id="calibration-run-p2s-04-api-promotion-0001",
        observation_mode="monotonic",
    )
    second_payload, second_manifest = complete_evidence_bundle(
        run_id="calibration-run-p2s-04-api-promotion-0002",
        observation_mode="monotonic",
    )
    app = create_app(Settings(workspace=tmp_path / "workspace"))

    with TestClient(app) as client:
        image_bytes = io.BytesIO()
        with Image.new("RGB", (12, 12), "navy") as image:
            image.save(image_bytes, format="PNG")
        old_project = client.post(
            "/api/projects/import?project_name=Pinned%20before%20promotion",
            content=image_bytes.getvalue(),
            headers={
                "content-type": "application/octet-stream",
                "x-filename": "before.png",
            },
        ).json()
        for bundle in (first_payload, second_payload):
            imported = client.post(
                "/api/calibration/runs/import",
                content=bundle,
                headers={"content-type": "application/zip"},
            )
            assert imported.status_code == 201, imported.text
        created = client.post(
            "/api/calibration/proposals",
            json={
                "profile_id": first_manifest.profile.id,
                "run_ids": sorted(
                    [first_manifest.record.record_id, second_manifest.record.record_id]
                ),
                "expected_catalog_fingerprint": base_fingerprint,
            },
        )
        assert created.status_code == 201, created.text
        proposal_id = created.json()["proposal"]["id"]
        stale = client.post(
            f"/api/calibration/proposals/{proposal_id}/accept",
            json={
                "expected_catalog_fingerprint": "f" * 64,
                "reviewer": "Kelly",
                "reason": "Stale review should fail.",
            },
        )
        accepted = client.post(
            f"/api/calibration/proposals/{proposal_id}/accept",
            json={
                "expected_catalog_fingerprint": base_fingerprint,
                "reviewer": "Kelly",
                "reason": "Reviewed both physical transition records.",
            },
        )
        catalogs = client.get("/api/printability-profile-catalogs")
        historical = client.get(f"/api/printability-profile-catalogs/{base_fingerprint}")
        resolved = client.post(
            "/api/printability-profiles/resolve",
            json={
                "printer_id": "bambu-p2s",
                "nozzle_id": "nozzle-0.4-hardened-steel",
                "material_class": "pla",
                "overrides": {},
            },
        )
        resolved_historical = client.post(
            "/api/printability-profiles/resolve",
            json={
                "printer_id": "bambu-p2s",
                "nozzle_id": "nozzle-0.4-hardened-steel",
                "material_class": "pla",
                "catalog_fingerprint": base_fingerprint,
                "overrides": {},
            },
        )
        missing_catalog_config = copy.deepcopy(old_project["draft"]["config"])
        missing_catalog_config["cleanup"]["printability_profile_catalog_fingerprint"] = "f" * 64
        missing_catalog = client.put(
            f"/api/projects/{old_project['project']['id']}/draft",
            json={
                "config": missing_catalog_config,
                "operations": [],
                "expected_draft_generation": old_project["draft"]["generation"],
            },
        )
        saved_old = client.put(
            f"/api/projects/{old_project['project']['id']}/draft",
            json={
                "config": old_project["draft"]["config"],
                "operations": [],
                "expected_draft_generation": old_project["draft"]["generation"],
            },
        )
        new_project = client.post(
            "/api/projects/import?project_name=Created%20after%20promotion",
            content=image_bytes.getvalue(),
            headers={
                "content-type": "application/octet-stream",
                "x-filename": "after.png",
            },
        )

    assert stale.status_code == 409
    assert accepted.status_code == 200, accepted.text
    active_fingerprint = accepted.json()["active_catalog"]["fingerprint"]
    assert active_fingerprint != base_fingerprint
    assert catalogs.status_code == 200
    assert catalogs.json()["active_fingerprint"] == active_fingerprint
    assert len(catalogs.json()["items"]) == 2
    assert historical.status_code == 200
    assert historical.json()["active"] is False
    assert resolved.status_code == 200
    assert resolved.json()["profile_catalog_fingerprint"] == active_fingerprint
    assert resolved_historical.status_code == 200
    assert resolved_historical.json()["profile_catalog_fingerprint"] == base_fingerprint
    assert missing_catalog.status_code == 409
    assert missing_catalog.json()["error"]["details"]["catalog_fingerprint"] == "f" * 64
    assert "Restore the source workspace backup" in missing_catalog.json()["error"]["message"]
    assert saved_old.status_code == 200, saved_old.text
    assert (
        saved_old.json()["config"]["cleanup"]["printability_profile_catalog_fingerprint"]
        == base_fingerprint
    )
    assert saved_old.json()["config"]["cleanup"]["maximum_tiny_hole_diameter_mm"] == 0.8
    assert new_project.status_code == 201, new_project.text
    assert (
        new_project.json()["draft"]["config"]["cleanup"]["printability_profile_catalog_fingerprint"]
        == active_fingerprint
    )
