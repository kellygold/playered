from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from image23mf.bambu import (
    profile_plan_for_project,
    read_bambu_3mf,
    resolve_p2s_profile_set,
)
from image23mf.bambu.cli import (
    BambuSliceProfiles,
    BambuStudioCliValidator,
    BambuValidationStatus,
    PinnedBambuProfile,
)
from image23mf.external import ExternalToolRegistry, ToolId, ToolRunner, ToolSpec
from image23mf.quality.release_fixture import (
    build_physical_release_3mf,
    build_physical_release_geometry,
)

GOLDEN_PATH = Path(__file__).parent / "goldens" / "release_gate_v1.json"
RUN_INSTALLED = os.environ.get("IMAGE23MF_RUN_INSTALLED_BAMBU") == "1"
RESOURCES_ROOT = Path("/Applications/BambuStudio.app/Contents/Resources")


def release_geometry(*, layer_height_mm: float):
    return build_physical_release_geometry(layer_height_mm=layer_height_mm)


def release_package(*, nozzle_mm: float, layer_height_mm: float):
    labels, topology, document, quality = release_geometry(layer_height_mm=layer_height_mm)
    package = build_physical_release_3mf(
        nozzle_mm=nozzle_mm,
        layer_height_mm=layer_height_mm,
    )
    return labels, topology, document, quality, package


def fake_bambu(tmp_path: Path) -> Path:
    executable = tmp_path / "fake BambuStudio"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, zipfile\n"
        "if '--version' in sys.argv:\n"
        "    print('BambuStudio 02.07.01.62')\n"
        "    raise SystemExit(0)\n"
        "outdir = pathlib.Path(sys.argv[sys.argv.index('--outputdir') + 1])\n"
        "name = sys.argv[sys.argv.index('--export-3mf') + 1]\n"
        "outdir.mkdir(parents=True, exist_ok=True)\n"
        "with zipfile.ZipFile(outdir / name, 'w') as archive:\n"
        "    archive.writestr('Metadata/plate_1.gcode', "
        "b'; release fixture\\nG1 X1 Y1 E0.25\\n')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def fake_validator(tmp_path: Path, executable: Path) -> BambuStudioCliValidator:
    runner = ToolRunner(temp_root=tmp_path / "tool-runs")
    registry = ExternalToolRegistry(
        runner=runner,
        specs={
            ToolId.BAMBU_STUDIO: ToolSpec(
                id=ToolId.BAMBU_STUDIO,
                name="Bambu Studio",
                purpose="release fixture",
                candidates=(str(executable),),
                version_arguments=("--version",),
                version_pattern=r"BambuStudio\s+(\d+(?:\.\d+)+)",
                minimum_version=(2, 0),
            )
        },
    )
    return BambuStudioCliValidator(runner=runner, registry=registry, timeout_seconds=5)


def fixture_profiles(tmp_path: Path, count: int) -> BambuSliceProfiles:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pinned = []
    for name in ("machine", "process", *(f"filament-{index}" for index in range(count))):
        path = tmp_path / f"{name}.json"
        role = name.split("-", maxsplit=1)[0]
        path.write_text(json.dumps({"name": name, "type": role}), encoding="utf-8")
        pinned.append(PinnedBambuProfile.pin(path))
    return BambuSliceProfiles(
        machine=pinned[0],
        process=pinned[1],
        filaments=tuple(pinned[2:]),
    )


def installed_profiles(package: bytes, tmp_path: Path) -> BambuSliceProfiles:
    project = read_bambu_3mf(package)
    resolved = resolve_p2s_profile_set(profile_plan_for_project(project), RESOURCES_ROOT)
    tmp_path.mkdir(parents=True, exist_ok=True)

    def pin(name: str, payload: bytes) -> PinnedBambuProfile:
        path = tmp_path / f"{name}.json"
        path.write_bytes(payload)
        return PinnedBambuProfile.pin(path)

    return BambuSliceProfiles(
        machine=pin("machine", resolved.machine.payload),
        process=pin("process", resolved.process.payload),
        filaments=tuple(
            pin(f"filament-{index}", item.payload) for index, item in enumerate(resolved.filaments)
        ),
    )


@pytest.mark.parametrize("nozzle,layer", ((0.2, 0.1), (0.4, 0.2)))
def test_release_golden_runs_complete_deterministic_pipeline_with_fake_slice(
    tmp_path, nozzle, layer
):
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    labels, topology, document, quality, package = release_package(
        nozzle_mm=nozzle,
        layer_height_mm=layer,
    )
    profile_key = f"{nozzle:.1f}/{layer:.2f}"
    actual = {
        "label_sha256": hashlib.sha256(labels.pixels).hexdigest(),
        "island_count": len(document.islands),
        "contour_count": len(document.contours),
        "shared_edge_count": len(document.shared_edges),
        "part_count": len(document.parts),
        "mesh_count": len(document.meshes),
        "geometry_fingerprint": document.fingerprint(),
        "quality_fingerprint": quality.fingerprint,
        "package_sha256": hashlib.sha256(package).hexdigest(),
    }
    assert actual == {**expected["common"], **expected["profiles"][profile_key]}
    assert topology.coverage.gap_area_mm2 == topology.coverage.overlap_area_mm2 == 0
    assert expected["manual_print_gate"] == "not_observed"

    project_path = tmp_path / "release-gate.3mf"
    project_path.write_bytes(package)
    validation = fake_validator(tmp_path, fake_bambu(tmp_path)).validate(
        project_path,
        profiles=fixture_profiles(tmp_path / "profiles", len(document.materials)),
    )
    assert validation.status == BambuValidationStatus.VALIDATED
    assert validation.source_sha256 == actual["package_sha256"]
    assert validation.artifact and validation.artifact.uncompressed_gcode_bytes > 0


@pytest.mark.skipif(
    not RUN_INSTALLED,
    reason="set IMAGE23MF_RUN_INSTALLED_BAMBU=1 for the installed release slice gate",
)
@pytest.mark.parametrize("nozzle,layer", ((0.2, 0.1), (0.4, 0.2)))
def test_release_golden_slices_with_installed_bambu_profiles(tmp_path, nozzle, layer):
    _labels, _topology, document, _quality, package = release_package(
        nozzle_mm=nozzle,
        layer_height_mm=layer,
    )
    project_path = tmp_path / f"release-{nozzle:.1f}.3mf"
    project_path.write_bytes(package)
    result = BambuStudioCliValidator().validate(
        project_path,
        profiles=installed_profiles(package, tmp_path / "installed-profiles"),
    )
    assert result.status == BambuValidationStatus.VALIDATED
    assert result.artifact and result.artifact.uncompressed_gcode_bytes > 0
    assert len(result.profiles) == 2 + len(document.materials)
