from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from image23mf.bambu.cli import (
    BambuSliceProfiles,
    BambuStudioCliValidator,
    BambuValidationStatus,
    PinnedBambuProfile,
)
from image23mf.bambu.mural_package import (
    MURAL_MANIFEST_PATH,
    BambuMuralPackageError,
    InvalidBambuMural3MFError,
    MuralBuildPlate,
    PrimeTowerReserve,
    build_bambu_mural_3mf,
    read_bambu_mural_3mf,
)
from image23mf.bambu.package import (
    CORE_NS,
    MODEL_SETTINGS_PATH,
    PRODUCTION_NS,
    ROOT_MODEL_PATH,
    BambuProject,
    BambuProjectSettings,
    Material,
    Mesh,
    profile_plan_for_project,
)
from image23mf.bambu.profiles import resolve_p2s_profile_set
from image23mf.bambu.report import SlicerValidationExpectation, parse_slicer_plate
from image23mf.external import ToolRunner
from image23mf.external.registry import ExternalToolRegistry, ToolId, ToolSpec

PLATE_NAMES = (
    "01 - Upper Left",
    "02 - Upper Center",
    "03 - Upper Right",
    "04 - Lower Left",
    "05 - Lower Center",
    "06 - Lower Right",
)


def materials() -> tuple[Material, ...]:
    return (
        Material("Bone White", "#CBC6B8", 1, preset="Bambu PLA Matte"),
        Material("Mandarin Orange", "#F99963", 2, preset="Bambu PLA Matte"),
        Material("Marine Blue", "#0078BF", 3, preset="Bambu PLA Matte"),
        Material("Charcoal", "#000000", 4, preset="Bambu PLA Matte"),
    )


def box(
    name: str,
    *,
    x: float,
    y: float,
    width: float,
    depth: float,
    bottom: float,
    top: float,
    material_index: int,
) -> Mesh:
    vertices = (
        (x, y, bottom),
        (x + width, y, bottom),
        (x + width, y + depth, bottom),
        (x, y + depth, bottom),
        (x, y, top),
        (x + width, y, top),
        (x + width, y + depth, top),
        (x, y + depth, top),
    )
    triangles = (
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    )
    return Mesh(name, vertices, triangles, material_index)


def plates() -> tuple[MuralBuildPlate, ...]:
    result = []
    for index, name in enumerate(PLATE_NAMES, start=1):
        row = (index - 1) // 3 + 1
        column = (index - 1) % 3 + 1
        base_width = 150 + index
        tile_meshes = (
            box(
                "Structural base",
                x=0,
                y=0,
                width=base_width,
                depth=180,
                bottom=0,
                top=0.4,
                material_index=0,
            ),
            box(
                "Raised pigment",
                x=10 + index * 2,
                y=12 + index,
                width=15 + index,
                depth=18 + index,
                bottom=0.4,
                top=0.8,
                material_index=index % 4,
            ),
        )
        result.append(
            MuralBuildPlate(
                tile_id=f"tile-r{row:02d}-c{column:02d}",
                row=row,
                column=column,
                build_plate_index=index,
                name=name,
                meshes=tile_meshes,
                origin_x_mm=2,
                origin_y_mm=10,
                source_geometry_fingerprint=hashlib.sha256(
                    f"geometry-document-{index}".encode()
                ).hexdigest(),
            )
        )
    return tuple(result)


def settings() -> BambuProjectSettings:
    return BambuProjectSettings(
        printer_model="Bambu Lab P2S",
        nozzle_diameter=0.4,
        layer_height=0.2,
        bed_type="Textured PEI Plate",
    )


def reserve() -> PrimeTowerReserve:
    return PrimeTowerReserve(x_mm=205, y_mm=205, width_mm=49, height_mm=49)


def build(**changes) -> bytes:
    arguments = {
        "name": "The Wager — six-panel mural",
        "plates": plates(),
        "materials": materials(),
        "settings": settings(),
        "prime_tower_reserve": reserve(),
    }
    arguments.update(changes)
    return build_bambu_mural_3mf(**arguments)


def rewrite(payload: bytes, member: str, change) -> bytes:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            data = source.read(info.filename)
            target.writestr(info, change(data) if info.filename == member else data)
    return output.getvalue()


def append_member(payload: bytes, member: str, data: bytes) -> bytes:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.writestr(member, data)
    return output.getvalue()


def rewrite_members(payload: bytes, changes: dict[str, object]) -> bytes:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            data = source.read(info.filename)
            change = changes.get(info.filename)
            target.writestr(info, change(data) if callable(change) else data)
    return output.getvalue()


def q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def prime_tower_xy_bounds(gcode: bytes) -> tuple[float, float, float, float] | None:
    active = False
    in_tower_grid = False
    x = y = None
    points: list[tuple[float, float]] = []
    for raw_line in gcode.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.upper().startswith("; FEATURE:"):
            active = line.split(":", 1)[1].strip().lower() == "prime tower"
            in_tower_grid = False
            continue
        if line.upper() == "; CP EMPTY GRID START":
            in_tower_grid = active
            continue
        if line.upper() == "; CP EMPTY GRID END":
            in_tower_grid = False
            continue
        if not in_tower_grid or not re.match(r"^G[01]\b", line, re.IGNORECASE):
            continue
        parameters = {
            key.upper(): float(value)
            for key, value in re.findall(r"(?:^|\s)([XYE])([-+]?(?:\d+(?:\.\d*)?|\.\d+))", line)
        }
        x = parameters.get("X", x)
        y = parameters.get("Y", y)
        if parameters.get("E", 0) > 0 and x is not None and y is not None:
            points.append((x, y))
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def test_writes_six_distinct_row_major_plates_with_prime_tower_room() -> None:
    first = build()
    second = build(plates=iter(plates()), materials=iter(materials()))
    assert first == second

    parsed = read_bambu_mural_3mf(first)
    assert parsed.name == "The Wager — six-panel mural"
    assert [plate.tile_id for plate in parsed.plates] == [
        "tile-r01-c01",
        "tile-r01-c02",
        "tile-r01-c03",
        "tile-r02-c01",
        "tile-r02-c02",
        "tile-r02-c03",
    ]
    assert [plate.name for plate in parsed.plates] == list(PLATE_NAMES)
    assert [plate.build_plate_index for plate in parsed.plates] == list(range(1, 7))
    assert len({item.geometry_sha256 for item in parsed.evidence}) == 6
    assert len({item.placed_bounds_mm for item in parsed.evidence}) == 6
    assert len({item.preview_sha256 for item in parsed.evidence}) == 6
    assert len({mesh_id for item in parsed.evidence for mesh_id in item.mesh_object_ids}) == 12
    assert parsed.prime_tower_reserve == reserve()
    assert all(item.placed_bounds_mm[3] < reserve().x_mm for item in parsed.evidence)

    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.testzip() is None
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        manifest = json.loads(archive.read(MURAL_MANIFEST_PATH))
        assert manifest["prime_tower"] == {
            "policy": "enabled_slicer_generated_tower",
            "inserted_geometry": False,
            "x_mm": 205.0,
            "y_mm": 205.0,
            "width_mm": 49.0,
            "height_mm": 49.0,
            "tower_width_mm": 35.0,
        }
        assert [item["name"] for item in manifest["plates"]] == list(PLATE_NAMES)
        assert all(
            archive.read(f"Metadata/plate_{index}.png").startswith(b"\x89PNG")
            for index in range(1, 7)
        )


def test_prime_tower_can_be_inset_without_losing_reserved_rectangle_evidence() -> None:
    inset = replace(
        reserve(),
        tower_width_mm=29,
        tower_x_mm=208,
        tower_y_mm=208,
    )
    payload = build(prime_tower_reserve=inset)

    parsed = read_bambu_mural_3mf(payload)

    assert parsed.prime_tower_reserve == inset
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = json.loads(archive.read(MURAL_MANIFEST_PATH))
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert manifest["prime_tower"]["x_mm"] == 205.0
    assert manifest["prime_tower"]["tower_x_mm"] == 208.0
    assert manifest["prime_tower"]["tower_y_mm"] == 208.0
    assert settings["prime_tower_width"] == "29"
    assert settings["wipe_tower_x"] == ["208"] * 6
    assert settings["wipe_tower_y"] == ["208"] * 6


def test_root_and_settings_assign_each_unique_tile_to_exactly_one_plate() -> None:
    payload = build()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read(ROOT_MODEL_PATH))
        assemblies = root.findall(f"{q(CORE_NS, 'resources')}/{q(CORE_NS, 'object')}")
        items = root.findall(f"{q(CORE_NS, 'build')}/{q(CORE_NS, 'item')}")
        assembly_ids = [3, 6, 9, 12, 15, 18]
        mesh_ids = [1, 2, 4, 5, 7, 8, 10, 11, 13, 14, 16, 17]
        assert [item.get("id") for item in assemblies] == [str(value) for value in assembly_ids]
        assert [item.get("objectid") for item in items] == [str(value) for value in assembly_ids]
        component_ids = [
            int(component.get("objectid", "-1"))
            for assembly in assemblies
            for component in assembly.findall(
                f"{q(CORE_NS, 'components')}/{q(CORE_NS, 'component')}"
            )
        ]
        assert component_ids == mesh_ids
        assert len(component_ids) == len(set(component_ids))
        assert [
            component.get(q(PRODUCTION_NS, "path"))
            for assembly in assemblies
            for component in assembly.findall(
                f"{q(CORE_NS, 'components')}/{q(CORE_NS, 'component')}"
            )
        ] == [f"/3D/Objects/object_{index}.model" for index in range(1, 7) for _ in range(2)]
        for index in range(1, 7):
            child = ET.fromstring(archive.read(f"3D/Objects/object_{index}.model"))
            resources = child.find(q(CORE_NS, "resources"))
            assert resources is not None
            assert resources.find(q(CORE_NS, "basematerials")) is None
            assert child.find(q(CORE_NS, "build")).attrib == {}

        model_settings = ET.fromstring(archive.read(MODEL_SETTINGS_PATH))
        settings_objects = model_settings.findall("object")
        settings_plates = model_settings.findall("plate")
        assert [item.get("id") for item in settings_objects] == [
            str(value) for value in assembly_ids
        ]
        assert [
            plate.find("metadata[@key='plater_name']").get("value") for plate in settings_plates
        ] == list(PLATE_NAMES)
        assert [
            plate.find("model_instance/metadata[@key='object_id']").get("value")
            for plate in settings_plates
        ] == [str(value) for value in assembly_ids]


def test_allows_identical_content_but_rejects_non_row_major_names_and_overlap() -> None:
    duplicate = list(plates())
    duplicate[1] = replace(duplicate[1], meshes=duplicate[0].meshes)
    parsed = read_bambu_mural_3mf(build(plates=tuple(duplicate)))
    assert parsed.evidence[0].geometry_sha256 == parsed.evidence[1].geometry_sha256
    assert parsed.evidence[0].tile_id != parsed.evidence[1].tile_id
    assert (
        parsed.evidence[0].source_geometry_fingerprint
        != parsed.evidence[1].source_geometry_fingerprint
    )

    wrong_order = list(plates())
    wrong_order[0], wrong_order[1] = wrong_order[1], wrong_order[0]
    with pytest.raises(BambuMuralPackageError, match="row-major order"):
        build(plates=tuple(wrong_order))

    wrong_name = list(plates())
    wrong_name[0] = replace(wrong_name[0], name="Upper Left")
    with pytest.raises(BambuMuralPackageError, match="zero-padded build order"):
        build(plates=tuple(wrong_name))

    overlapping = list(plates())
    overlapping[0] = replace(overlapping[0], origin_x_mm=60, origin_y_mm=30)
    with pytest.raises(BambuMuralPackageError, match="prime-tower reserve"):
        build(
            plates=tuple(overlapping),
            prime_tower_reserve=PrimeTowerReserve(205, 0, 49, 256),
        )


def test_reader_rejects_duplicate_plate_assignment_and_object_regression() -> None:
    payload = build()

    def duplicate_assignment(data: bytes) -> bytes:
        root = ET.fromstring(data)
        plates_xml = root.findall("plate")
        instance = plates_xml[1].find("model_instance/metadata[@key='object_id']")
        assert instance is not None
        instance.set("value", "3")
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    with pytest.raises(InvalidBambuMural3MFError, match="assignment"):
        read_bambu_mural_3mf(rewrite(payload, MODEL_SETTINGS_PATH, duplicate_assignment))

    def duplicate_object(data: bytes) -> bytes:
        root = ET.fromstring(data)
        assemblies = root.findall(f"{q(CORE_NS, 'resources')}/{q(CORE_NS, 'object')}")
        components = assemblies[1].findall(f"{q(CORE_NS, 'components')}/{q(CORE_NS, 'component')}")
        components[0].set("objectid", "1")
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    with pytest.raises(InvalidBambuMural3MFError, match="geometry does not match"):
        read_bambu_mural_3mf(rewrite(payload, ROOT_MODEL_PATH, duplicate_object))


def test_reader_rejects_preview_and_manifest_evidence_tampering(tmp_path) -> None:
    payload = build()
    path = tmp_path / "wager-six-plates.3mf"
    path.write_bytes(payload)
    parsed = read_bambu_mural_3mf(path)
    provenance = {item.tile_id: item.source_geometry_fingerprint for item in parsed.evidence}
    anchored = read_bambu_mural_3mf(
        payload,
        expected_manifest_sha256=parsed.manifest_sha256,
        expected_plate_provenance=provenance,
    )
    assert anchored.manifest_sha256 == parsed.manifest_sha256

    with pytest.raises(InvalidBambuMural3MFError, match="persisted project state"):
        read_bambu_mural_3mf(payload, expected_manifest_sha256="0" * 64)
    wrong_provenance = dict(provenance)
    wrong_provenance["tile-r01-c01"] = "f" * 64
    with pytest.raises(InvalidBambuMural3MFError, match="provenance"):
        read_bambu_mural_3mf(payload, expected_plate_provenance=wrong_provenance)

    with pytest.raises(InvalidBambuMural3MFError, match="preview"):
        read_bambu_mural_3mf(
            rewrite(payload, "Metadata/plate_3.png", lambda data: data + b"tampered")
        )

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        preview = Image.open(io.BytesIO(archive.read("Metadata/plate_3.png"))).convert("RGBA")
    preview.putpixel((0, 0), (255, 0, 255, 255))
    changed_preview = io.BytesIO()
    preview.save(changed_preview, format="PNG", optimize=False, compress_level=9)
    changed_preview_bytes = changed_preview.getvalue()

    def rehash_preview(data: bytes) -> bytes:
        manifest = json.loads(data)
        manifest["plates"][2]["preview_sha256"] = hashlib.sha256(changed_preview_bytes).hexdigest()
        return (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )

    with pytest.raises(InvalidBambuMural3MFError, match="canonical"):
        read_bambu_mural_3mf(
            rewrite_members(
                payload,
                {
                    "Metadata/plate_3.png": lambda _data: changed_preview_bytes,
                    MURAL_MANIFEST_PATH: rehash_preview,
                },
            )
        )

    def tamper_bounds(data: bytes) -> bytes:
        manifest = json.loads(data)
        manifest["plates"][4]["placed_bounds_mm"][3] += 1
        return json.dumps(manifest).encode()

    with pytest.raises(InvalidBambuMural3MFError, match="canonical"):
        read_bambu_mural_3mf(rewrite(payload, MURAL_MANIFEST_PATH, tamper_bounds))

    with pytest.raises(InvalidBambuMural3MFError, match="unexpected ZIP members"):
        read_bambu_mural_3mf(append_member(payload, "Metadata/untrusted.json", b"{}"))

    def add_project_setting(data: bytes) -> bytes:
        settings_payload = json.loads(data)
        settings_payload["untrusted_override"] = "1"
        return json.dumps(settings_payload).encode()

    with pytest.raises(InvalidBambuMural3MFError, match="project settings are not canonical"):
        read_bambu_mural_3mf(
            rewrite(payload, "Metadata/project_settings.config", add_project_setting)
        )


@pytest.mark.skipif(
    not os.environ.get("BAMBU_STUDIO_CLI"),
    reason="set BAMBU_STUDIO_CLI to run the installed six-plate slicer contract",
)
def test_installed_bambu_studio_slices_every_mural_plate(tmp_path) -> None:
    executable = Path(os.environ["BAMBU_STUDIO_CLI"]).expanduser().resolve()
    package = tmp_path / "wager-six-plates.3mf"
    package.write_bytes(build())
    parsed = read_bambu_mural_3mf(package)
    flattened = BambuProject(
        name=parsed.name,
        meshes=tuple(mesh for plate in parsed.plates for mesh in plate.meshes),
        materials=parsed.materials,
        settings=parsed.settings,
    )
    plan = profile_plan_for_project(flattened)
    resolved = resolve_p2s_profile_set(plan, executable.parents[1] / "Resources")
    machine = tmp_path / Path(resolved.machine.profile.relative_path).name
    machine.write_bytes(resolved.machine.payload)
    process = tmp_path / Path(resolved.process.profile.relative_path).name
    process_payload = json.loads(resolved.process.payload)
    process_payload.update(
        {
            "enable_prime_tower": "1",
            "prime_tower_width": str(reserve().tower_width_mm),
            "wipe_tower_x": [str(reserve().x_mm)] * len(parsed.plates),
            "wipe_tower_y": [str(reserve().y_mm)] * len(parsed.plates),
        }
    )
    process.write_text(json.dumps(process_payload), encoding="utf-8")
    filament_paths = []
    for index, profile in enumerate(resolved.filaments, start=1):
        destination = tmp_path / f"extruder-{index}-{Path(profile.profile.relative_path).name}"
        destination.write_bytes(profile.payload)
        filament_paths.append(destination)

    output_name = "wager-six-plates.gcode.3mf"
    command = [
        executable,
        "--load-settings",
        f"{machine};{process}",
        "--load-filaments",
        ";".join(str(path) for path in filament_paths),
        "--filament-colour",
        ";".join(material.color for material in parsed.materials),
        "--no-check",
        "--arrange",
        "0",
        "--slice",
        "0",
        "--debug",
        "2",
        "--outputdir",
        str(tmp_path),
        "--export-3mf",
        output_name,
        str(package),
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(tmp_path / output_name) as output:
        slice_info = ET.fromstring(output.read("Metadata/slice_info.config"))
        output_plates = slice_info.findall("plate")
        assert len(output_plates) == 6
        names_by_output_index: dict[int, str] = {}
        slots_by_name: dict[str, set[int]] = {}
        for plate in output_plates:
            index_node = plate.find("metadata[@key='index']")
            object_node = plate.find("object")
            assert index_node is not None and object_node is not None
            output_index = int(index_node.get("value", "0"))
            name = object_node.get("name", "")
            assert output_index not in names_by_output_index
            names_by_output_index[output_index] = name
            slots_by_name[name] = {
                int(item.get("id", "0"))
                for item in plate.findall("filament")
                if item.get("used_for_object") == "true"
            }
            ranges = plate.findall("layer_filament_lists/layer_filament_list")
            assert ranges
            assert any(item.get("layer_ranges", "").strip() for item in ranges)
        expected_names_by_output_index = {
            index: name for index, name in enumerate(PLATE_NAMES, start=1)
        }
        assert names_by_output_index == expected_names_by_output_index

        output_model_settings = ET.fromstring(output.read(MODEL_SETTINGS_PATH))
        labels_by_output_index = {
            int(plate.find("metadata[@key='plater_id']").get("value", "0")): plate.find(
                "metadata[@key='plater_name']"
            ).get("value", "")
            for plate in output_model_settings.findall("plate")
        }
        assert labels_by_output_index == names_by_output_index

        gcode_hashes: set[str] = set()
        model_bounds: set[tuple[float, float, float, float, float, float]] = set()
        for output_index, name in names_by_output_index.items():
            source_index = PLATE_NAMES.index(name) + 1
            expected_slots = {1, source_index % 4 + 1}
            assert slots_by_name[name] == expected_slots
            member = f"Metadata/plate_{output_index}.gcode"
            gcode = output.read(member)
            gcode_hashes.add(hashlib.sha256(gcode).hexdigest())
            report = parse_slicer_plate(io.BytesIO(gcode), member=member)
            assert report.has_extrusion
            assert report.observed_layer_count == 4
            assert set(report.used_extruders) == expected_slots
            assert report.model_extrusion_bounds is not None
            bounds = report.model_extrusion_bounds
            assert bounds.min_x_mm == pytest.approx(2.21, abs=0.05)
            assert bounds.min_y_mm == pytest.approx(10.21, abs=0.05)
            assert bounds.max_x_mm == pytest.approx(151.79 + source_index, abs=0.05)
            assert bounds.max_y_mm == pytest.approx(189.79, abs=0.05)
            assert bounds.min_z_mm == pytest.approx(0.2, abs=0.01)
            assert bounds.max_z_mm == pytest.approx(0.8, abs=0.01)
            model_bounds.add(
                (
                    bounds.min_x_mm,
                    bounds.min_y_mm,
                    bounds.min_z_mm,
                    bounds.max_x_mm,
                    bounds.max_y_mm,
                    bounds.max_z_mm,
                )
            )
            tower_bounds = prime_tower_xy_bounds(gcode)
            if len(expected_slots) > 1:
                assert report.tool_changes
                assert tower_bounds is not None
                assert reserve().x_mm <= tower_bounds[0] <= tower_bounds[2] <= reserve().right_mm
                assert reserve().y_mm <= tower_bounds[1] <= tower_bounds[3] <= reserve().top_mm
            else:
                assert not report.tool_changes
        assert len(gcode_hashes) == 6
        assert len(model_bounds) == 6

        output_settings = json.loads(output.read("Metadata/project_settings.config"))
        assert output_settings["enable_prime_tower"] == "1"
        assert float(output_settings["prime_tower_width"]) == reserve().tower_width_mm
        assert output_settings["wipe_tower_x"] == [str(reserve().x_mm)] * 6
        assert output_settings["wipe_tower_y"] == [str(reserve().y_mm)] * 6

    # Bambu's implicit auto-arrange can rotate content among otherwise correctly labelled plates.
    # Keeping this real regression makes the production `--arrange 0` contract non-optional.
    auto_output_name = "wager-six-plates-auto-arranged.gcode.3mf"
    auto_command = list(command)
    arrange_index = auto_command.index("--arrange")
    del auto_command[arrange_index : arrange_index + 2]
    auto_command[auto_command.index(output_name)] = auto_output_name
    auto_result = subprocess.run(
        auto_command,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert auto_result.returncode == 0, auto_result.stdout + auto_result.stderr
    with zipfile.ZipFile(tmp_path / auto_output_name) as output:
        auto_slice_info = ET.fromstring(output.read("Metadata/slice_info.config"))
        auto_names = {
            int(plate.find("metadata[@key='index']").get("value", "0")): plate.find("object").get(
                "name", ""
            )
            for plate in auto_slice_info.findall("plate")
        }
        auto_model_settings = ET.fromstring(output.read(MODEL_SETTINGS_PATH))
        auto_labels = {
            int(plate.find("metadata[@key='plater_id']").get("value", "0")): plate.find(
                "metadata[@key='plater_name']"
            ).get("value", "")
            for plate in auto_model_settings.findall("plate")
        }
        assert auto_labels != auto_names


@pytest.mark.skipif(
    not os.environ.get("BAMBU_STUDIO_CLI"),
    reason="set BAMBU_STUDIO_CLI to run the production mural validator contract",
)
def test_production_validator_preserves_ordered_mural_identity_and_towers(tmp_path) -> None:
    executable = Path(os.environ["BAMBU_STUDIO_CLI"]).expanduser().resolve()
    package = tmp_path / "wager-six-plates.3mf"
    package.write_bytes(build())
    parsed = read_bambu_mural_3mf(package)
    flattened = BambuProject(
        name=parsed.name,
        meshes=tuple(mesh for plate in parsed.plates for mesh in plate.meshes),
        materials=parsed.materials,
        settings=parsed.settings,
    )
    resolved = resolve_p2s_profile_set(
        profile_plan_for_project(flattened), executable.parents[1] / "Resources"
    )
    profile_directory = tmp_path / "pinned-profiles"
    profile_directory.mkdir()

    def pin(name: str, payload: bytes) -> PinnedBambuProfile:
        path = profile_directory / name
        path.write_bytes(payload)
        return PinnedBambuProfile.pin(path)

    profiles = BambuSliceProfiles(
        machine=pin("machine.json", resolved.machine.payload),
        process=pin("process.json", resolved.process.payload),
        filaments=tuple(
            pin(f"filament-{index}.json", profile.payload)
            for index, profile in enumerate(resolved.filaments, start=1)
        ),
    )
    runner = ToolRunner(temp_root=tmp_path / "validator-runs")
    registry = ExternalToolRegistry(
        runner=runner,
        specs={
            ToolId.BAMBU_STUDIO: ToolSpec(
                id=ToolId.BAMBU_STUDIO,
                name="Bambu Studio",
                purpose="mural 3MF validation",
                candidates=(str(executable),),
                version_arguments=("--version",),
                version_pattern=r"BambuStudio-(\d+(?:\.\d+)+)",
                minimum_version=(2, 0),
                allow_nonzero_version_probe=True,
            )
        },
        version_timeout_seconds=5,
    )
    validator = BambuStudioCliValidator(
        runner=runner,
        registry=registry,
        timeout_seconds=300,
    )

    result = validator.validate(
        package,
        profiles=profiles,
        expectation=SlicerValidationExpectation(
            expected_colors=tuple(material.color for material in parsed.materials)
        ),
    )

    assert result.status == BambuValidationStatus.VALIDATED
    assert result.command[result.command.index("--arrange") :][:2] == ("--arrange", "0")
    assert result.artifact is not None
    assert result.artifact.plate_names == PLATE_NAMES
    assert len(result.artifact.plates) == 6
    assert len(result.artifact.prime_tower_bounds) == 6
    for source_index, (plate, tower) in enumerate(
        zip(result.artifact.plates, result.artifact.prime_tower_bounds), start=1
    ):
        expected_slots = {1, source_index % 4 + 1}
        assert plate.observed_layer_count == 4
        assert set(plate.used_extruders) == expected_slots
        assert plate.model_extrusion_bounds is not None
        assert plate.model_extrusion_bounds.max_x_mm == pytest.approx(
            151.79 + source_index, abs=0.05
        )
        if len(expected_slots) > 1:
            assert tower is not None
            assert reserve().x_mm <= tower.min_x_mm <= tower.max_x_mm <= reserve().right_mm
            assert reserve().y_mm <= tower.min_y_mm <= tower.max_y_mm <= reserve().top_mm
        else:
            assert tower is None
