import json

import pytest
from pydantic import ValidationError

from image23mf.profiles import (
    PrintSetupRequest,
    ProfileCatalog,
    ProfileCatalogService,
    ProfileIssueCode,
    ProfileValidationError,
    load_bundled_catalog,
)

EXPECTED_BUNDLED_FINGERPRINT = "80e08e424ee6d0cd78512bd123525cec69a61ad6bc412f28a6251032ec93361e"


def issue_codes(error: ProfileValidationError) -> list[ProfileIssueCode]:
    return [issue.code for issue in error.issues]


def test_bundled_p2s_catalog_matches_reviewed_bambu_studio_profiles() -> None:
    catalog = load_bundled_catalog()
    printer = catalog.printer("bambu-p2s")
    assert printer is not None

    assert catalog.schema_version == 1
    assert catalog.source.name == "Bambu Studio bundled system profiles"
    assert catalog.source.version == "02.07.01.62"
    assert catalog.source.captured_on == "2026-07-16"
    assert catalog.fingerprint() == EXPECTED_BUNDLED_FINGERPRINT
    assert printer.slicer_model_name == "Bambu Lab P2S"
    assert (
        printer.printable_area.width_mm,
        printer.printable_area.depth_mm,
        printer.printable_area.height_mm,
    ) == (256, 256, 256)
    assert printer.printable_area.excluded_rectangles == ()

    nozzle_02 = printer.nozzle("nozzle-0.2-hardened-steel")
    nozzle_04 = printer.nozzle("nozzle-0.4-hardened-steel")
    assert nozzle_02 is not None
    assert nozzle_04 is not None
    assert (
        nozzle_02.diameter_mm,
        nozzle_02.min_layer_height_mm,
        nozzle_02.max_layer_height_mm,
        nozzle_02.default_layer_height_mm,
    ) == (0.2, 0.04, 0.14, 0.1)
    assert nozzle_02.recommended_layer_heights_mm == (0.08, 0.1, 0.12)
    assert (
        nozzle_04.diameter_mm,
        nozzle_04.min_layer_height_mm,
        nozzle_04.max_layer_height_mm,
        nozzle_04.default_layer_height_mm,
    ) == (0.4, 0.08, 0.28, 0.2)
    assert nozzle_04.recommended_layer_heights_mm == (0.08, 0.12, 0.16, 0.2, 0.24)
    plate = printer.plate("textured-pei")
    assert plate is not None
    assert plate.slicer_name == "Textured PEI Plate"
    assert plate.first_layer_finish == "textured"


def test_catalog_round_trip_and_fingerprint_are_canonical() -> None:
    catalog = load_bundled_catalog()
    reversed_payload = dict(reversed(list(catalog.model_dump(mode="json").items())))
    restored = ProfileCatalog.model_validate_json(catalog.canonical_json())
    reordered = ProfileCatalog.model_validate(reversed_payload)

    assert restored == catalog
    assert reordered.fingerprint() == catalog.fingerprint()


def test_default_setup_is_valid_and_layer_aligned() -> None:
    service = ProfileCatalogService.bundled()
    request = service.default_request("bambu-p2s")
    validated = service.validate(request)

    assert request == PrintSetupRequest(
        printer_id="bambu-p2s",
        nozzle_id="nozzle-0.4-hardened-steel",
        plate_id="textured-pei",
        layer_height_mm=0.2,
        canvas_width_mm=200,
        canvas_height_mm=200,
        base_thickness_mm=1.2,
        art_thickness_mm=0.6,
    )
    assert validated.nozzle.diameter_mm == 0.4
    assert validated.plate.id == "textured-pei"
    assert validated.profile_catalog_fingerprint == service.catalog.fingerprint()


def test_valid_02_and_04_nozzle_setups_use_the_same_generic_validator() -> None:
    service = ProfileCatalogService.bundled()
    standard = service.default_request("bambu-p2s")
    fine = standard.model_copy(
        update={
            "nozzle_id": "nozzle-0.2-hardened-steel",
            "layer_height_mm": 0.1,
        }
    )

    assert service.validate(standard).nozzle.diameter_mm == 0.4
    assert service.validate(fine).nozzle.diameter_mm == 0.2


def test_unknown_printer_rejects_with_available_profile_suggestion() -> None:
    service = ProfileCatalogService.bundled()
    request = service.default_request("bambu-p2s").model_copy(
        update={"printer_id": "unknown-printer"}
    )

    with pytest.raises(ProfileValidationError) as raised:
        service.validate(request)

    assert issue_codes(raised.value) == [ProfileIssueCode.UNKNOWN_PRINTER]
    assert raised.value.issues[0].field == "printer_id"
    assert raised.value.issues[0].details["available_printer_ids"] == ["bambu-p2s"]
    assert "bambu-p2s" in raised.value.issues[0].suggestion


def test_incompatible_nozzle_and_plate_are_reported_together() -> None:
    service = ProfileCatalogService.bundled()
    request = service.default_request("bambu-p2s").model_copy(
        update={"nozzle_id": "brass-1.0", "plate_id": "glass"}
    )

    with pytest.raises(ProfileValidationError) as raised:
        service.validate(request)

    assert issue_codes(raised.value) == [
        ProfileIssueCode.INCOMPATIBLE_NOZZLE,
        ProfileIssueCode.INCOMPATIBLE_PLATE,
    ]
    assert "nozzle-0.2-hardened-steel" in raised.value.issues[0].suggestion
    assert "textured-pei" in raised.value.issues[1].suggestion


@pytest.mark.parametrize(
    ("height", "suggested"),
    [(0, 0.1), (0.02, 0.04), (0.2, 0.14)],
)
def test_layer_range_errors_include_clamped_suggestion(height: float, suggested: float) -> None:
    service = ProfileCatalogService.bundled()
    request = service.default_request("bambu-p2s").model_copy(
        update={
            "nozzle_id": "nozzle-0.2-hardened-steel",
            "layer_height_mm": height,
        }
    )

    with pytest.raises(ProfileValidationError) as raised:
        service.validate(request)

    layer_issue = next(
        issue for issue in raised.value.issues if issue.code == ProfileIssueCode.LAYER_OUT_OF_RANGE
    )
    assert layer_issue.details["suggested_mm"] == suggested
    assert "0.08, 0.1, 0.12 mm" in layer_issue.suggestion


def test_canvas_limit_reports_bed_and_prime_tower_guidance() -> None:
    service = ProfileCatalogService.bundled()
    request = service.default_request("bambu-p2s").model_copy(
        update={"canvas_width_mm": 257, "canvas_height_mm": 0}
    )

    with pytest.raises(ProfileValidationError) as raised:
        service.validate(request)

    issue = next(
        issue for issue in raised.value.issues if issue.code == ProfileIssueCode.CANVAS_OUT_OF_RANGE
    )
    assert issue.details == {"maximum_width_mm": 256.0, "maximum_height_mm": 256.0}
    assert "prime tower" in issue.suggestion


def test_base_art_total_and_layer_alignment_rules_return_specific_fixes() -> None:
    service = ProfileCatalogService.bundled()
    default = service.default_request("bambu-p2s")

    with pytest.raises(ProfileValidationError) as base_error:
        service.validate(default.model_copy(update={"base_thickness_mm": 0.2}))
    assert ProfileIssueCode.BASE_OUT_OF_RANGE in issue_codes(base_error.value)
    assert base_error.value.issues[0].details["suggested_mm"] == 0.4

    with pytest.raises(ProfileValidationError) as art_error:
        service.validate(default.model_copy(update={"art_thickness_mm": 0.1}))
    assert ProfileIssueCode.ART_OUT_OF_RANGE in issue_codes(art_error.value)
    assert art_error.value.issues[0].details["minimum_mm"] == 0.2

    with pytest.raises(ProfileValidationError) as total_error:
        service.validate(default.model_copy(update={"base_thickness_mm": 8, "art_thickness_mm": 5}))
    assert ProfileIssueCode.TOTAL_THICKNESS_OUT_OF_RANGE in issue_codes(total_error.value)

    with pytest.raises(ProfileValidationError) as alignment_error:
        service.validate(
            default.model_copy(update={"layer_height_mm": 0.08, "art_thickness_mm": 0.6})
        )
    alignment = next(
        issue
        for issue in alignment_error.value.issues
        if issue.code == ProfileIssueCode.THICKNESS_NOT_LAYER_ALIGNED
    )
    assert alignment.field == "art_thickness_mm"
    assert alignment.details["suggested_mm"] == 0.64
    assert "8 layers" in alignment.suggestion


def test_catalog_validation_rejects_duplicate_ids_bad_defaults_and_layer_order() -> None:
    payload = json.loads(load_bundled_catalog().model_dump_json())
    printer = payload["printers"][0]
    printer["nozzles"].append(dict(printer["nozzles"][0]))
    with pytest.raises(ValidationError, match="nozzle ids must be unique"):
        ProfileCatalog.model_validate(payload)

    payload = json.loads(load_bundled_catalog().model_dump_json())
    payload["printers"][0]["default_plate_id"] = "missing"
    with pytest.raises(ValidationError, match="default plate"):
        ProfileCatalog.model_validate(payload)

    payload = json.loads(load_bundled_catalog().model_dump_json())
    payload["printers"][0]["nozzles"][0]["recommended_layer_heights_mm"] = [0.1, 0.08]
    with pytest.raises(ValidationError, match="sorted"):
        ProfileCatalog.model_validate(payload)


def test_service_is_data_driven_for_an_additional_printer_id() -> None:
    payload = json.loads(load_bundled_catalog().model_dump_json())
    printer = payload["printers"][0]
    printer["id"] = "local-custom-printer"
    printer["display_name"] = "Local Custom Printer"
    catalog = ProfileCatalog.model_validate(payload)
    service = ProfileCatalogService(catalog)

    request = service.default_request("local-custom-printer")
    validated = service.validate(request)

    assert validated.printer.id == "local-custom-printer"
    assert validated.request.printer_id == "local-custom-printer"


def test_pinned_job_validation_reports_catalog_and_resolved_diameter_mismatches() -> None:
    service = ProfileCatalogService.bundled()
    request = service.default_request("bambu-p2s")

    with pytest.raises(ProfileValidationError) as raised:
        service.validate_pinned(
            request,
            catalog_id="old-catalog",
            catalog_version="yesterday",
            nozzle_diameter_mm=0.2,
        )

    assert issue_codes(raised.value) == [
        ProfileIssueCode.CATALOG_MISMATCH,
        ProfileIssueCode.NOZZLE_DIAMETER_MISMATCH,
    ]
    assert raised.value.issues[0].details == {
        "available_catalog_id": "image23mf-bundled-printers",
        "available_catalog_version": "2026.07.16",
    }
    assert raised.value.issues[1].details["suggested_mm"] == 0.4
