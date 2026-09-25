from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from image23mf.api.app import create_app
from image23mf.calibration import (
    RECOMMENDATION_ORDER,
    CalibrationArtifactManifest,
    CalibrationFeatureKind,
    CalibrationOutcome,
    CalibrationRunRecord,
    PrintabilityOverrides,
    PrintabilityProfileCatalog,
    PrintabilityProfileService,
    ResolvePrintabilityRequest,
    UnknownPrintabilityProfileError,
    generate_calibration_artifact,
    load_bundled_printability_catalog,
    validate_calibration_record,
    write_calibration_bundle,
)
from image23mf.settings import Settings

EXPECTED_CATALOG_FINGERPRINT = "215157c562737bc0d97391257aca309a49ca65415bfc0935fc3c333b74a5c08c"


def _profile(nozzle: str):
    catalog = load_bundled_printability_catalog()
    return next(item for item in catalog.profiles if item.nozzle_id == nozzle)


def test_bundled_profiles_are_versioned_provisional_and_reviewable() -> None:
    catalog = load_bundled_printability_catalog()

    assert catalog.schema_version == 1
    assert catalog.catalog_id == "image23mf-bundled-printability"
    assert catalog.catalog_version == "2026.07.16"
    assert catalog.fingerprint() == EXPECTED_CATALOG_FINGERPRINT
    assert catalog.source.captured_on == "2026-07-16"
    assert "provisional" in catalog.source.method.lower()
    assert [profile.nozzle_diameter_mm for profile in catalog.profiles] == [0.2, 0.4]
    assert {profile.evidence_status.value for profile in catalog.profiles} == {
        "pending_print_calibration"
    }
    assert all(
        recommendation.basis.value == "engineering_baseline"
        for profile in catalog.profiles
        for recommendation in (
            getattr(profile.recommendations, name) for name in RECOMMENDATION_ORDER
        )
    )


def test_initial_02_and_04_values_have_explicit_nozzle_relative_rationale() -> None:
    fine = _profile("nozzle-0.2-hardened-steel")
    standard = _profile("nozzle-0.4-hardened-steel")

    assert fine.recommendations.minimum_island_diameter_mm.value == 0.3
    assert standard.recommendations.minimum_island_diameter_mm.value == 0.6
    assert fine.recommendations.minimum_line_width_mm.value == 0.24
    assert standard.recommendations.minimum_line_width_mm.value == 0.48
    assert fine.recommendations.maximum_tiny_hole_diameter_mm.value == 0.4
    assert standard.recommendations.maximum_tiny_hole_diameter_mm.value == 0.8
    assert fine.recommendations.smoothing_radius_mm.value == 0
    assert standard.recommendations.smoothing_radius_mm.value == 0
    assert "never silently" in standard.recommendations.smoothing_radius_mm.rationale
    assert fine.sweep.sample_count == standard.sweep.sample_count == 6


def test_profile_resolution_preserves_visible_user_overrides_including_zero() -> None:
    service = PrintabilityProfileService.bundled()
    request = ResolvePrintabilityRequest(
        printer_id="bambu-p2s",
        nozzle_id="nozzle-0.4-hardened-steel",
        material_class="pla",
        overrides=PrintabilityOverrides(
            minimum_line_width_mm=0.52,
            smoothing_radius_mm=0,
        ),
    )
    resolved = service.resolve(request)

    assert resolved.warning is not None
    assert tuple(item.name for item in resolved.values) == RECOMMENDATION_ORDER
    line = resolved.value("minimum_line_width_mm")
    smoothing = resolved.value("smoothing_radius_mm")
    island = resolved.value("minimum_island_diameter_mm")
    assert (line.value, line.profile_value, line.source) == (0.52, 0.48, "user_override")
    assert "User override" in line.rationale
    assert smoothing.source == "user_override"
    assert smoothing.value == 0
    assert island.source == "profile"
    assert island.value == 0.6


def test_unknown_profile_reports_every_available_choice() -> None:
    service = PrintabilityProfileService.bundled()
    request = ResolvePrintabilityRequest(
        printer_id="bambu-p2s",
        nozzle_id="nozzle-0.6",
        material_class="pla",
    )

    with pytest.raises(UnknownPrintabilityProfileError) as raised:
        service.resolve(request)

    assert raised.value.request == request
    assert raised.value.available_profile_ids == tuple(
        profile.id for profile in service.catalog.profiles
    )


def test_catalog_rejects_duplicate_keys_wrong_units_and_false_validation_claims() -> None:
    source = load_bundled_printability_catalog()
    payload = json.loads(source.model_dump_json())
    payload["profiles"].append(dict(payload["profiles"][0]))
    with pytest.raises(ValidationError, match="profile IDs must be unique"):
        PrintabilityProfileCatalog.model_validate(payload)

    payload = json.loads(source.model_dump_json())
    payload["profiles"][0]["recommendations"]["minimum_line_width_mm"]["unit"] = "mm2"
    with pytest.raises(ValidationError, match="minimum_line_width_mm must use mm"):
        PrintabilityProfileCatalog.model_validate(payload)

    payload = json.loads(source.model_dump_json())
    payload["profiles"][0]["evidence_status"] = "print_validated"
    with pytest.raises(ValidationError, match="printed-calibration recommendations"):
        PrintabilityProfileCatalog.model_validate(payload)


def test_generator_produces_deterministic_exact_scale_svg_png_and_manifest() -> None:
    profile = _profile("nozzle-0.2-hardened-steel")
    first = generate_calibration_artifact(profile, raster_mm_per_pixel=0.1)
    second = generate_calibration_artifact(profile, raster_mm_per_pixel=0.1)

    assert first.manifest == second.manifest
    assert first.svg_bytes == second.svg_bytes
    assert first.png_bytes == second.png_bytes
    assert first.manifest.width_mm == 160
    assert first.manifest.height_mm == 112
    assert (first.manifest.width_px, first.manifest.height_px) == (1600, 1120)
    assert len(first.manifest.features) == 30
    assert {
        kind: sum(feature.kind == kind for feature in first.manifest.features)
        for kind in CalibrationFeatureKind
    } == {kind: 6 for kind in CalibrationFeatureKind}
    assert all(
        abs(feature.rasterized_dimension_mm - feature.nominal_dimension_mm) <= 0.1
        for feature in first.manifest.features
    )
    assert b'width="160mm"' in first.svg_bytes
    assert b"shape-rendering:geometricPrecision" in first.svg_bytes
    with Image.open(io.BytesIO(first.png_bytes), formats=["PNG"]) as image:
        assert image.mode == "RGBA"
        assert image.size == (1600, 1120)
        assert {pixel[:3] for pixel in image.getdata()} == {(0, 0, 0), (255, 255, 255)}
    assert (
        CalibrationArtifactManifest.model_validate_json(first.manifest.canonical_json())
        == first.manifest
    )


def test_record_template_requires_complete_provenance_and_every_feature() -> None:
    profile = _profile("nozzle-0.4-hardened-steel")
    artifact = generate_calibration_artifact(profile, raster_mm_per_pixel=0.1)
    template = artifact.record_template

    assert template.status.value == "template"
    assert template.record_id is None
    assert len(template.observations) == 30
    assert {item.outcome for item in template.observations} == {CalibrationOutcome.UNTESTED}
    validate_calibration_record(template, artifact.manifest)

    incomplete = template.model_dump(mode="json")
    incomplete["status"] = "completed"
    with pytest.raises(ValidationError, match="full print provenance"):
        CalibrationRunRecord.model_validate(incomplete)

    completed = template.model_dump(mode="json")
    completed.update(
        {
            "status": "completed",
            "record_id": "calibration-run-p2s-04-pla-first",
            "layer_height_mm": 0.2,
            "plate_id": "textured-pei",
            "filament": "Bambu PLA Basic Black",
            "operator": "Kelly",
            "printed_on": "2026-07-16",
            "slicer_profile": "0.20mm Standard @BBL P2S",
        }
    )
    completed["observations"] = [
        {**observation, "outcome": "pass"} for observation in completed["observations"]
    ]
    completed["observations"][0]["outcome"] = "fail"
    completed["observations"][0]["measured_dimension_mm"] = 0
    record = CalibrationRunRecord.model_validate(completed)
    validate_calibration_record(record, artifact.manifest)

    missing = record.model_copy(update={"observations": record.observations[:-1]})
    with pytest.raises(ValueError, match="every artifact feature"):
        validate_calibration_record(missing, artifact.manifest)

    wrong_setup = record.model_copy(update={"printer_id": "different-printer"})
    with pytest.raises(ValueError, match="setup does not match"):
        validate_calibration_record(wrong_setup, artifact.manifest)


def test_bundle_writer_and_cli_emit_all_reviewable_evidence(tmp_path: Path) -> None:
    profile = _profile("nozzle-0.4-hardened-steel")
    artifact = generate_calibration_artifact(profile, raster_mm_per_pixel=0.1)
    paths = write_calibration_bundle(artifact, tmp_path / "direct")

    assert len(paths) == 4
    assert all(path.is_file() for path in paths)
    assert paths[2].read_text(encoding="utf-8").endswith("\n")
    assert paths[3].read_text(encoding="utf-8").endswith("\n")

    cli_output = tmp_path / "cli"
    completed = subprocess.run(
        [
            str(Path(".venv/bin/python")),
            "scripts/generate_printability_calibration.py",
            "--profile",
            profile.id,
            "--output",
            str(cli_output),
            "--raster-mm-per-pixel",
            "0.1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert profile.id in completed.stdout
    assert len(tuple(cli_output.iterdir())) == 4


def test_printability_profile_api_exposes_rationale_and_actionable_errors(tmp_path: Path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        catalog = client.get("/api/printability-profiles")
        resolved = client.post(
            "/api/printability-profiles/resolve",
            json={
                "printer_id": "bambu-p2s",
                "nozzle_id": "nozzle-0.2-hardened-steel",
                "material_class": "pla",
                "overrides": {"minimum_line_width_mm": 0.26},
            },
        )
        unsupported = client.post(
            "/api/printability-profiles/resolve",
            json={
                "printer_id": "bambu-p2s",
                "nozzle_id": "nozzle-0.8",
                "material_class": "pla",
            },
        )

    assert catalog.status_code == 200
    assert len(catalog.json()["profiles"]) == 2
    assert resolved.status_code == 200
    line = next(
        item for item in resolved.json()["values"] if item["name"] == "minimum_line_width_mm"
    )
    assert line["value"] == 0.26
    assert line["source"] == "user_override"
    assert resolved.json()["warning"]
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "validation_error"
    assert len(unsupported.json()["error"]["details"]["available_profile_ids"]) == 2
