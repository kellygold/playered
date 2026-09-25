from __future__ import annotations

import io
import json
import math
import os
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from image23mf.bambu import (
    PROFILE_CONTRACT_PATH,
    Bambu3MFError,
    BambuProject,
    BambuProjectSettings,
    InvalidBambu3MFError,
    Material,
    Mesh,
    build_bambu_3mf,
    read_bambu_3mf,
    read_bambu_profile_plan,
    resolve_p2s_profile_set,
    write_bambu_3mf,
)

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
MODEL_RELATIONSHIP = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
MEMBERS = [
    "[Content_Types].xml",
    "_rels/.rels",
    "3D/3dmodel.model",
    "3D/_rels/3dmodel.model.rels",
    "3D/Objects/object_1.model",
    "Metadata/model_settings.config",
    "Metadata/project_settings.config",
    "Metadata/image23mf_profile.json",
]


@dataclass
class AdapterMesh:
    """A future IR only needs to satisfy the writer's tiny structural protocol."""

    name: str
    vertices: list[tuple[float, float, float]]
    triangles: list[tuple[int, int, int]]
    material_index: int


def materials() -> tuple[Material, ...]:
    return (
        Material("Bone & White", "#cbc6b8", 1, preset="Bambu PLA Matte"),
        Material("Charcoal", "#000000", 2, preset="Bambu PLA Matte"),
    )


def meshes():
    return (
        AdapterMesh(
            "Base <plate>",
            [(0, 0, 0), (20, 0, 0), (0, 20, 0), (20, 20, 0)],
            [(0, 1, 2), (1, 3, 2)],
            0,
        ),
        AdapterMesh(
            "Ink",
            [(2, 2, 0.2), (4, 2, 0.2), (2, 4, 0.2)],
            [(0, 1, 2)],
            1,
        ),
    )


def build(**overrides) -> bytes:
    arguments = {
        "name": "The Wager & proof",
        "meshes": meshes(),
        "materials": materials(),
        "settings": BambuProjectSettings(
            printer_model="Bambu Lab P2S",
            nozzle_diameter=0.4,
            layer_height=0.2,
            plate_center_x=128,
            plate_center_y=127.5,
        ),
    }
    arguments.update(overrides)
    return build_bambu_3mf(**arguments)


def q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def rewrite_package(payload: bytes, changes: dict[str, bytes | None]) -> bytes:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for name in source.namelist():
            replacement = changes.get(name, source.read(name))
            if replacement is not None:
                target.writestr(name, replacement)
    return output.getvalue()


def test_writes_deterministic_opc_and_bambu_project_structure():
    first = build()
    second = build(meshes=iter(meshes()), materials=iter(materials()))
    assert first == second

    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == MEMBERS
        assert archive.testzip() is None
        assert {entry.date_time for entry in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert {entry.compress_type for entry in archive.infolist()} == {zipfile.ZIP_DEFLATED}

        content_types = ET.fromstring(archive.read("[Content_Types].xml"))
        assert (
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            in archive.read("[Content_Types].xml")
        )
        assert b"ns0:" not in archive.read("[Content_Types].xml")
        assert content_types.tag == q(CONTENT_TYPES, "Types")
        declarations = {node.get("Extension"): node.get("ContentType") for node in content_types}
        assert declarations == {
            "rels": "application/vnd.openxmlformats-package.relationships+xml",
            "model": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
            "json": "application/json",
        }

        for member, expected_target in (
            ("_rels/.rels", "/3D/3dmodel.model"),
            ("3D/_rels/3dmodel.model.rels", "/3D/Objects/object_1.model"),
        ):
            relationships = ET.fromstring(archive.read(member))
            assert (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                in archive.read(member)
            )
            assert b"ns0:" not in archive.read(member)
            assert relationships.tag == q(RELATIONSHIPS, "Relationships")
            relationship = list(relationships)
            assert len(relationship) == 1
            assert relationship[0].attrib == {
                "Target": expected_target,
                "Id": "rel-1",
                "Type": MODEL_RELATIONSHIP,
            }

        root_model = ET.fromstring(archive.read("3D/3dmodel.model"))
        assert root_model.tag == q(CORE, "model")
        assert root_model.get("unit") == "millimeter"
        assembly = root_model.find(f"{q(CORE, 'resources')}/{q(CORE, 'object')}")
        assert assembly is not None
        assert assembly.get("id") == "100"
        components = assembly.findall(f"{q(CORE, 'components')}/{q(CORE, 'component')}")
        assert [component.get("objectid") for component in components] == ["1", "2"]
        assert {component.get(q(PRODUCTION, "path")) for component in components} == {
            "/3D/Objects/object_1.model"
        }
        build_item = root_model.find(f"{q(CORE, 'build')}/{q(CORE, 'item')}")
        assert build_item is not None
        assert build_item.get("objectid") == "100"
        assert build_item.get("transform") == "1 0 0 0 1 0 0 0 1 128 127.5 0"

        identifiers = [
            node.get(q(PRODUCTION, "UUID"))
            for node in root_model.iter()
            if node.get(q(PRODUCTION, "UUID"))
        ]
        assert len(identifiers) == len(set(identifiers)) == 5
        assert all(uuid.UUID(value).version == 5 for value in identifiers)

        object_model = ET.fromstring(archive.read("3D/Objects/object_1.model"))
        resources = object_model.find(q(CORE, "resources"))
        assert resources is not None
        material_table = resources.find(q(CORE, "basematerials"))
        assert material_table is not None
        assert material_table.get("id") == "1000"
        assert [node.attrib for node in material_table] == [
            {"name": "Bone & White", "displaycolor": "#CBC6B8"},
            {"name": "Charcoal", "displaycolor": "#000000"},
        ]
        objects = resources.findall(q(CORE, "object"))
        assert [node.get("id") for node in objects] == ["1", "2"]
        assert [node.get("pindex") for node in objects] == ["0", "1"]
        assert [
            [
                triangle.attrib
                for triangle in node.findall(f"{q(CORE, 'mesh')}/{q(CORE, 'triangles')}/*")
            ]
            for node in objects
        ] == [
            [
                {"v1": "0", "v2": "1", "v3": "2", "pid": "1000", "p1": "0", "p2": "0", "p3": "0"},
                {"v1": "1", "v2": "3", "v3": "2", "pid": "1000", "p1": "0", "p2": "0", "p3": "0"},
            ],
            [{"v1": "0", "v2": "1", "v3": "2", "pid": "1000", "p1": "1", "p2": "1", "p3": "1"}],
        ]

        model_settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
        parts = model_settings.findall("object/part")
        assert [part.get("id") for part in parts] == ["1", "2"]
        assert [
            {metadata.get("key"): metadata.get("value") for metadata in part} for part in parts
        ] == [
            {
                "name": "Base <plate>",
                "matrix": "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1",
                "source_object_id": "0",
                "source_volume_id": "0",
                "extruder": "1",
                "image23mf_material_index": "0",
            },
            {
                "name": "Ink",
                "matrix": "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1",
                "source_object_id": "1",
                "source_volume_id": "0",
                "extruder": "2",
                "image23mf_material_index": "1",
            },
        ]

        project_settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert project_settings["filament_colour"] == ["#CBC6B8", "#000000"]
        assert project_settings["filament_type"] == ["PLA", "PLA"]
        assert project_settings["filament_settings_id"] == [
            "Bambu PLA Matte @BBL P2S",
            "Bambu PLA Matte @BBL P2S",
        ]
        assert (
            project_settings["default_filament_profile"] == project_settings["filament_settings_id"]
        )
        assert project_settings["filament_ids"] == ["GFA01", "GFA01"]
        assert project_settings["filament_self_index"] == ["1", "1", "2", "2"]
        assert project_settings["flush_volumes_matrix"] == ["0", "140", "140", "0"]
        assert project_settings["flush_volumes_vector"] == ["140"] * 4
        assert project_settings["image23mf_extruders"] == [1, 2]
        assert project_settings["image23mf_material_ids"] == [None, None]
        assert project_settings["image23mf_palette_color_ids"] == [None, None]
        assert project_settings["image23mf_filament_ids"] == [None, None]
        assert project_settings["image23mf_part_material_indices"] == [0, 1]
        assert project_settings["default_print_profile"] == "0.20mm Standard @BBL P2S"
        assert project_settings["printer_settings_id"] == "Bambu Lab P2S 0.4 nozzle"
        assert project_settings["printer_model"] == "Bambu Lab P2S"
        assert project_settings["nozzle_diameter"] == ["0.4"]
        assert project_settings["layer_height"] == "0.2"

        profile_contract = json.loads(archive.read(PROFILE_CONTRACT_PATH))
        assert profile_contract["contract"] == "image23mf.bambu.p2s-profile"
        assert profile_contract["schema_version"] == 1
        assert profile_contract["authority"] == {
            "embedded_project_settings": "import_hint",
            "installed_profiles": "slice_authority",
            "requested_filament_colors": "visual_intent",
        }
        assert profile_contract["installed_profiles"]["machine"]["relative_path"] == (
            "profiles/BBL/machine/Bambu Lab P2S 0.4 nozzle.json"
        )
        assert profile_contract["installed_profiles"]["process"] == {
            "kind": "process",
            "name": "0.20mm Standard @BBL P2S",
            "override_strategy": "resolve_inheritance_then_overlay",
            "overrides": {
                "curr_bed_type": "Textured PEI Plate",
                "detect_thin_wall": "1",
                "min_bead_width": "70%",
                "precise_outer_wall": "0",
                "wall_generator": "arachne",
            },
            "relative_path": "profiles/BBL/process/0.20mm Standard @BBL P2S.json",
            "resolution": "recursive_inheritance_merge",
            "source": "installed_system_profile",
        }


def test_parser_round_trip_preserves_canonical_geometry_and_settings(tmp_path):
    payload = build()
    expected = BambuProject(
        name="The Wager & proof",
        meshes=(
            Mesh(
                "Base <plate>",
                ((0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (0.0, 20.0, 0.0), (20.0, 20.0, 0.0)),
                ((0, 1, 2), (1, 3, 2)),
                0,
            ),
            Mesh("Ink", ((2.0, 2.0, 0.2), (4.0, 2.0, 0.2), (2.0, 4.0, 0.2)), ((0, 1, 2),), 1),
        ),
        materials=(
            Material("Bone & White", "#CBC6B8", 1, "PLA", "Bambu PLA Matte @BBL P2S"),
            Material("Charcoal", "#000000", 2, "PLA", "Bambu PLA Matte @BBL P2S"),
        ),
        settings=BambuProjectSettings(
            printer_model="Bambu Lab P2S",
            nozzle_diameter=0.4,
            layer_height=0.2,
            bed_type="Textured PEI Plate",
            plate_center_x=128,
            plate_center_y=127.5,
        ),
    )
    assert read_bambu_3mf(payload) == expected
    assert read_bambu_3mf(io.BytesIO(payload)) == expected
    assert read_bambu_profile_plan(payload).process.name == "0.20mm Standard @BBL P2S"

    destination = tmp_path / "escaped-project.3mf"
    write_bambu_3mf(
        destination,
        name="The Wager & proof",
        meshes=meshes(),
        materials=materials(),
        settings=expected.settings,
    )
    assert destination.read_bytes() == payload
    assert read_bambu_3mf(destination) == expected


@pytest.mark.parametrize("color_count", (2, 4, 8))
def test_material_slots_keep_palette_identity_absent_colors_and_matrix_dimensions(color_count):
    colors = (
        "#101010",
        "#EED3A9",
        "#083574",
        "#F15A24",
        "#4A7C59",
        "#7B4AB5",
        "#D8C3E8",
        "#47DFB9",
    )[:color_count]
    ordered_materials = tuple(
        Material(
            name=f"Palette {index}",
            color=color,
            extruder=index + 1,
            preset="Bambu PLA Matte @BBL P2S",
            material_id=f"material_{index}",
            palette_color_id=f"palette-{color_count - index}",
            filament_id=None if index % 2 else f"filament_{index}",
        )
        for index, color in enumerate(colors)
    )
    # Only the first and final palette slots own geometry. Intermediate colors remain
    # explicit filament slots instead of disappearing from the package mapping.
    selected_slots = (0,) if color_count == 2 else (0, color_count - 1)
    selected_meshes = tuple(
        AdapterMesh(
            name=f"Part using slot {slot}",
            vertices=[(slot, 0, 0), (slot + 1, 0, 0), (slot, 1, 0)],
            triangles=[(0, 1, 2)],
            material_index=slot,
        )
        for slot in selected_slots
    )

    payload = build_bambu_3mf(
        name=f"{color_count}-color mapping",
        meshes=selected_meshes,
        materials=ordered_materials,
    )
    parsed = read_bambu_3mf(payload)

    assert parsed.materials == ordered_materials
    assert [mesh.material_index for mesh in parsed.meshes] == list(selected_slots)
    assert len(parsed.materials) == color_count
    assert (
        build_bambu_3mf(
            name=parsed.name,
            meshes=parsed.meshes,
            materials=parsed.materials,
            settings=parsed.settings,
        )
        == payload
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["filament_colour"] == list(colors)
        assert settings["image23mf_material_ids"] == [
            material.material_id for material in ordered_materials
        ]
        assert settings["image23mf_palette_color_ids"] == [
            material.palette_color_id for material in ordered_materials
        ]
        assert settings["image23mf_part_material_indices"] == list(selected_slots)
        assert len(settings["default_filament_profile"]) == color_count
        assert len(settings["filament_ids"]) == color_count
        assert len(settings["filament_settings_id"]) == color_count
        assert len(settings["filament_type"]) == color_count
        assert len(settings["filament_self_index"]) == color_count * 2
        assert len(settings["flush_volumes_vector"]) == color_count * 2
        assert len(settings["flush_volumes_matrix"]) == color_count**2
        for index in range(color_count):
            assert settings["flush_volumes_matrix"][index * color_count + index] == "0"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": ""}, "name must be a non-empty string"),
        ({"meshes": []}, "at least one mesh"),
        ({"materials": []}, "at least one material"),
        (
            {"materials": [Material("Bone", "cbc6b8", 1)]},
            "#RRGGBB or #RRGGBBAA",
        ),
        (
            {"materials": [Material("Bone", "#CBC6B8", 0)]},
            "extruder must be at least 1",
        ),
        (
            {"materials": [Material("Bone", "#CBC6B8", 2)]},
            "ordered one per contiguous extruder",
        ),
        (
            {"materials": [materials()[0], replace(materials()[1], name="Bone & White")]},
            "material names must be unique",
        ),
        (
            {"meshes": [meshes()[0], replace(meshes()[1], name="Base <plate>")]},
            "mesh names must be unique",
        ),
        (
            {"meshes": [replace(meshes()[0], material_index=2)]},
            "outside the material table",
        ),
        (
            {"meshes": [replace(meshes()[0], vertices=[(0, 0, math.nan), (1, 0, 0), (0, 1, 0)])]},
            "must be a finite number",
        ),
        (
            {"meshes": [replace(meshes()[0], vertices=[(0, 0, 0), (1, 0, 0)])]},
            "at least 3 vertices",
        ),
        (
            {"meshes": [replace(meshes()[0], triangles=[(0, 0, 1)])]},
            "repeats a vertex index",
        ),
        (
            {"meshes": [replace(meshes()[0], triangles=[(0, 1, 99)])]},
            "references a missing vertex",
        ),
        (
            {"meshes": [replace(meshes()[0], triangles=[(0, 1, True)])]},
            "must be an integer",
        ),
        (
            {"settings": BambuProjectSettings(nozzle_diameter=0)},
            "nozzle_diameter must be greater than zero",
        ),
        (
            {"settings": BambuProjectSettings(nozzle_diameter=0.6)},
            "nozzle_diameter must be one of",
        ),
        (
            {"settings": BambuProjectSettings(nozzle_diameter=0.2, layer_height=0.2)},
            "layer_height for a 0.2 mm nozzle must be one of",
        ),
        (
            {"settings": BambuProjectSettings(printer_model="Bambu Lab X1C")},
            "printer_model must be 'Bambu Lab P2S'",
        ),
        (
            {"settings": BambuProjectSettings(bed_type="Cool Plate")},
            "bed_type must be 'Textured PEI Plate'",
        ),
        (
            {
                "materials": [
                    replace(materials()[0], filament_type="PETG"),
                    materials()[1],
                ]
            },
            "filament_type must be PLA",
        ),
    ],
)
def test_rejects_malformed_writer_inputs(overrides, message):
    with pytest.raises(Bambu3MFError, match=message):
        build(**overrides)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: b"not a zip", "not a readable 3MF ZIP"),
        (
            lambda payload: rewrite_package(payload, {"Metadata/model_settings.config": None}),
            "missing Metadata/model_settings.config",
        ),
        (
            lambda payload: rewrite_package(payload, {"3D/3dmodel.model": b"<broken>"}),
            "not well-formed XML",
        ),
        (
            lambda payload: rewrite_package(
                payload,
                {
                    "_rels/.rels": (
                        b'<?xml version="1.0"?><Relationships '
                        b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        b'<Relationship Target="/wrong.model" Id="rel-1" '
                        b'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                        b"</Relationships>"
                    )
                },
            ),
            "does not resolve the expected model target",
        ),
        (
            lambda payload: rewrite_package(
                payload,
                {
                    "3D/Objects/object_1.model": zipfile.ZipFile(io.BytesIO(payload))
                    .read("3D/Objects/object_1.model")
                    .replace(b'v3="2"', b'v3="99"', 1)
                },
            ),
            "references a missing vertex",
        ),
        (
            lambda payload: rewrite_package(
                payload,
                {
                    "Metadata/project_settings.config": zipfile.ZipFile(io.BytesIO(payload))
                    .read("Metadata/project_settings.config")
                    .replace(b"#CBC6B8", b"#FFFFFF", 1)
                },
            ),
            "filament color conflicts with material metadata",
        ),
        (
            lambda payload: rewrite_package(
                payload,
                {
                    PROFILE_CONTRACT_PATH: zipfile.ZipFile(io.BytesIO(payload))
                    .read(PROFILE_CONTRACT_PATH)
                    .replace(b'"wall_generator":"arachne"', b'"wall_generator":"classic"')
                },
            ),
            "profile contract conflicts with canonical project settings",
        ),
    ],
)
def test_parser_rejects_malformed_packages(mutate, message):
    with pytest.raises(InvalidBambu3MFError, match=message):
        read_bambu_3mf(mutate(build()))


def test_parser_rejects_duplicate_zip_members():
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(build())) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        with pytest.warns(UserWarning, match="Duplicate name"):
            target.writestr("3D/3dmodel.model", source.read("3D/3dmodel.model"))
    with pytest.raises(InvalidBambu3MFError, match="duplicate ZIP members"):
        read_bambu_3mf(output.getvalue())


@pytest.mark.skipif(
    not os.environ.get("BAMBU_STUDIO_CLI"),
    reason="set BAMBU_STUDIO_CLI to run the installed slicer import contract",
)
def test_installed_bambu_studio_imports_generated_project(tmp_path):
    vertices = ((0, 0, 0), (20, 0, 0), (0, 20, 0), (0, 0, 20))
    triangles = ((0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3))
    package = tmp_path / "bambu-cli-proof.3mf"
    write_bambu_3mf(
        package,
        name="Bambu CLI proof",
        meshes=[Mesh("Closed tetrahedron", vertices, triangles)],
        materials=[Material("Bone White", "#CBC6B8", 1)],
    )

    result = subprocess.run(
        [os.environ["BAMBU_STUDIO_CLI"], "--info", str(package)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "number_of_facets = 4" in result.stdout
    assert "manifold = yes" in result.stdout


@pytest.mark.parametrize(("nozzle", "layer_height"), [(0.2, 0.10), (0.4, 0.20)])
@pytest.mark.skipif(
    not os.environ.get("BAMBU_STUDIO_CLI"),
    reason="set BAMBU_STUDIO_CLI to run measured installed P2S profile evidence",
)
def test_installed_bambu_profiles_control_reopened_slice_settings(tmp_path, nozzle, layer_height):
    executable = Path(os.environ["BAMBU_STUDIO_CLI"]).expanduser().resolve()
    resources = executable.parents[1] / "Resources"
    vertices = ((0, 0, 0), (20, 0, 0), (0, 20, 0), (0, 0, 20))
    triangles = ((0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3))
    package = tmp_path / f"p2s-{nozzle:.1f}-profile-proof.3mf"
    write_bambu_3mf(
        package,
        name=f"P2S {nozzle:.1f} profile proof",
        meshes=[Mesh("Closed tetrahedron", vertices, triangles)],
        materials=[Material("Bone White", "#CBC6B8", 1, preset="Bambu PLA Matte")],
        settings=BambuProjectSettings(
            nozzle_diameter=nozzle,
            layer_height=layer_height,
        ),
    )
    plan = read_bambu_profile_plan(package)
    resolved = resolve_p2s_profile_set(plan, resources)
    machine = tmp_path / Path(resolved.machine.profile.relative_path).name
    machine.write_bytes(resolved.machine.payload)
    process = tmp_path / Path(resolved.process.profile.relative_path).name
    process.write_bytes(resolved.process.payload)
    filaments = []
    for index, profile in enumerate(resolved.filaments, start=1):
        destination = tmp_path / f"extruder-{index}-{Path(profile.profile.relative_path).name}"
        destination.write_bytes(profile.payload)
        filaments.append(destination)
    output_name = f"p2s-{nozzle:.1f}-profile-proof.gcode.3mf"
    result = subprocess.run(
        [
            executable,
            "--load-settings",
            f"{machine};{process}",
            "--load-filaments",
            ";".join(str(path) for path in filaments),
            "--slice",
            "0",
            "--debug",
            "2",
            "--outputdir",
            str(tmp_path),
            "--export-3mf",
            output_name,
            str(package),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(tmp_path / output_name) as output:
        reopened = json.loads(output.read("Metadata/project_settings.config"))
        assert output.read("Metadata/plate_1.gcode")
    assert reopened["printer_model"] == "Bambu Lab P2S"
    assert reopened["nozzle_diameter"] == [f"{nozzle:.1f}"]
    assert reopened["layer_height"] == str(layer_height)
    assert reopened["curr_bed_type"] == "Textured PEI Plate"
    assert reopened["wall_generator"] == "arachne"
    assert reopened["detect_thin_wall"] == "1"
    assert reopened["precise_outer_wall"] == "0"
    assert reopened["min_bead_width"] == "70%"
    assert reopened["print_settings_id"] == plan.process.name
    assert reopened["printer_settings_id"] == plan.machine.name
    assert reopened["filament_settings_id"] == [plan.filaments[0].profile.name]
    assert reopened["filament_type"] == ["PLA"]


def test_single_plate_tower_position_round_trips_without_changing_meshes():
    settings = BambuProjectSettings(
        plate_center_x=28, plate_center_y=2, prime_tower_position=(4, 208)
    )
    package = build_bambu_3mf(
        name="Tower layout", meshes=meshes(), materials=materials(), settings=settings
    )
    restored = read_bambu_3mf(package)
    assert restored.settings == settings
    assert restored.meshes[0].vertices == tuple(meshes()[0].vertices)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        payload = json.loads(archive.read("Metadata/project_settings.config"))
    assert payload["wipe_tower_x"] == ["4"]
    assert payload["wipe_tower_y"] == ["208"]
    assert payload["filament_colour"] == ["#CBC6B8", "#000000"]
