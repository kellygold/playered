from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from image23mf.bambu import (
    ARTWORK_PROCESS_OVERRIDES,
    BambuProfileError,
    resolve_p2s_profile_plan,
    resolve_p2s_profile_set,
)

FIXTURE_RESOURCES = Path(__file__).parent / "fixtures" / "bambu_profiles"


@dataclass(frozen=True)
class ProfileMaterial:
    color: str = "#CBC6B8"
    extruder: int = 1
    filament_type: str = "PLA"
    preset: str = "Bambu PLA Matte"


@pytest.mark.parametrize(
    ("nozzle", "layer_height", "process_name"),
    [
        (0.2, 0.08, "0.08mm High Quality @BBL P2S 0.2 nozzle"),
        (0.2, 0.10, "0.10mm Standard @BBL P2S 0.2 nozzle"),
        (0.2, 0.12, "0.12mm Balanced Quality @BBL P2S 0.2 nozzle"),
        (0.4, 0.08, "0.08mm High Quality @BBL P2S"),
        (0.4, 0.12, "0.12mm High Quality @BBL P2S"),
        (0.4, 0.16, "0.16mm Standard @BBL P2S"),
        (0.4, 0.20, "0.20mm Standard @BBL P2S"),
        (0.4, 0.24, "0.24mm Standard @BBL P2S"),
    ],
)
def test_resolves_every_catalogued_p2s_layer_to_an_exact_system_profile(
    nozzle, layer_height, process_name
):
    plan = resolve_p2s_profile_plan(
        printer_model="Bambu Lab P2S",
        nozzle_diameter_mm=nozzle,
        layer_height_mm=layer_height,
        bed_type="Textured PEI Plate",
        materials=[ProfileMaterial()],
    )

    assert plan.machine.name == f"Bambu Lab P2S {nozzle:.1f} nozzle"
    assert plan.process.name == process_name
    assert plan.process.relative_path == f"profiles/BBL/process/{process_name}.json"
    assert plan.process_overrides == ARTWORK_PROCESS_OVERRIDES
    filament_suffix = " @BBL P2S 0.2 nozzle" if nozzle == 0.2 else " @BBL P2S"
    assert plan.filaments[0].profile.name == f"Bambu PLA Matte{filament_suffix}"


def test_contract_keeps_slice_authority_separate_from_portable_import_hints():
    plan = resolve_p2s_profile_plan(
        printer_model="Bambu Lab P2S",
        nozzle_diameter_mm=0.2,
        layer_height_mm=0.10,
        bed_type="Textured PEI Plate",
        materials=[
            ProfileMaterial(),
            ProfileMaterial(
                color="#000000",
                extruder=2,
                preset="Bambu PLA Basic @BBL P2S 0.2 nozzle",
            ),
        ],
    )

    contract = plan.as_contract()
    assert contract["authority"] == {
        "embedded_project_settings": "import_hint",
        "installed_profiles": "slice_authority",
        "requested_filament_colors": "visual_intent",
    }
    assert contract["target"] == {
        "bed_type": "Textured PEI Plate",
        "layer_height_mm": 0.1,
        "nozzle_diameter_mm": 0.2,
        "printer_model": "Bambu Lab P2S",
    }
    assert contract["installed_profiles"]["filaments"][1] == {
        "base_preset": "Bambu PLA Basic",
        "extruder": 2,
        "filament_type": "PLA",
        "kind": "filament",
        "name": "Bambu PLA Basic @BBL P2S 0.2 nozzle",
        "relative_path": ("profiles/BBL/filament/Bambu PLA Basic @BBL P2S 0.2 nozzle.json"),
        "requested_color": "#000000",
        "resolution": "recursive_inheritance_merge",
        "source": "installed_system_profile",
    }


def test_flattens_installed_inheritance_before_staging_and_then_overlays_artwork_controls():
    plan = resolve_p2s_profile_plan(
        printer_model="Bambu Lab P2S",
        nozzle_diameter_mm=0.4,
        layer_height_mm=0.2,
        bed_type="Textured PEI Plate",
        materials=[ProfileMaterial()],
    )

    resolved = resolve_p2s_profile_set(plan, FIXTURE_RESOURCES)
    machine = json.loads(resolved.machine.payload)
    process = json.loads(resolved.process.payload)
    filament = json.loads(resolved.filaments[0].payload)

    assert resolved.machine.inheritance_chain == (
        "fdm_bbl_3dp_001_common",
        "Bambu Lab P2S 0.4 nozzle",
    )
    assert machine["machine_start_gcode"] == "G28"
    assert machine["nozzle_diameter"] == ["0.4"]
    assert resolved.process.inheritance_chain == (
        "fdm_process_common",
        "fdm_process_single_0.20",
        "0.20mm Standard @BBL P2S",
    )
    assert process["layer_height"] == "0.2"
    assert process["line_width"] == "0.42"
    assert {key: process[key] for key, _value in ARTWORK_PROCESS_OVERRIDES} == dict(
        ARTWORK_PROCESS_OVERRIDES
    )
    assert filament["filament_flow_ratio"] == ["0.98"]
    repeated = resolve_p2s_profile_set(plan, FIXTURE_RESOURCES)
    assert resolved.process.payload == repeated.process.payload
    assert len(resolved.process.sha256) == 64


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"printer_model": "Bambu Lab X1C"}, "printer_model"),
        ({"nozzle_diameter_mm": 0.6}, "nozzle_diameter"),
        ({"layer_height_mm": 0.14}, "layer_height"),
        ({"bed_type": "Cool Plate"}, "bed_type"),
        ({"materials": []}, "at least one material"),
        ({"materials": [ProfileMaterial(filament_type="PETG")]}, "filament_type"),
        ({"materials": [ProfileMaterial(preset="Generic PLA")]}, "material preset"),
    ],
)
def test_rejects_unmeasured_profile_requests(overrides, message):
    request = {
        "printer_model": "Bambu Lab P2S",
        "nozzle_diameter_mm": 0.4,
        "layer_height_mm": 0.2,
        "bed_type": "Textured PEI Plate",
        "materials": [ProfileMaterial()],
    }
    request.update(overrides)

    with pytest.raises(BambuProfileError, match=message):
        resolve_p2s_profile_plan(**request)
