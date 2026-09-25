import io
import json
from pathlib import Path

import pytest

from image23mf.bambu.report import (
    SlicerValidationExpectation,
    build_slicer_validation_report,
    parse_slicer_plate,
)

FIXTURE = Path(__file__).parent / "fixtures" / "slicer" / "bambu-2.7-multicolor.gcode"
GOLDEN = Path(__file__).parent / "goldens" / "bambu_slicer_report_v1.json"


def test_representative_bambu_27_report_matches_committed_golden() -> None:
    with FIXTURE.open("rb") as gcode:
        plate = parse_slicer_plate(gcode, member="Metadata/plate_1.gcode")
    report = build_slicer_validation_report(
        (plate,),
        expectation=SlicerValidationExpectation(expected_colors=("#cbc6b8", "F99963", "#0078BF")),
        stdout="slice complete\nwarning: repaired tiny gap\n",
        stderr=(
            "Warning: one floating region detected.\n"
            "This object requires support under the bridge.\n"
        ),
    )

    assert report.as_contract() == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_missing_expected_color_is_explicit_without_inventing_usage() -> None:
    with FIXTURE.open("rb") as gcode:
        plate = parse_slicer_plate(gcode, member="Metadata/plate_1.gcode")
    report = build_slicer_validation_report(
        (plate,),
        expectation=SlicerValidationExpectation(expected_colors=("#CBC6B8", "#000000")),
    )

    assert report.observed_colors == ("#CBC6B8", "#F99963", "#0078BF")
    assert report.missing_expected_colors == ("#000000",)
    assert report.expected_colors_present is False


def test_sparse_valid_gcode_reports_unknown_metadata_instead_of_guessing() -> None:
    plate = parse_slicer_plate(
        io.BytesIO(b"G90\nM83\nG1 X1 Y2 Z0.2 E0.1\n"),
        member="Metadata/plate_9.gcode",
    )

    assert plate.has_extrusion is True
    assert plate.declared_layer_count is None
    assert plate.observed_layer_count == 0
    assert plate.used_extruders == ()
    assert plate.filament_usage == ()
    assert plate.model_extrusion_bounds is not None


def test_expectations_reject_ambiguous_or_invalid_colors() -> None:
    with pytest.raises(ValueError, match="unique"):
        SlicerValidationExpectation(expected_colors=("#ffffff", "FFFFFF"))
    with pytest.raises(ValueError, match="invalid"):
        SlicerValidationExpectation(expected_colors=("orange",))
