"""Write deterministic intermediate 3MF projects for Bambu Studio conversion.

These packages carry import hints, not a complete GUI-loadable preset. The export
service must have Bambu serialize the final editable project before publication.

The writer deliberately accepts a very small mesh protocol.  Geometry generation owns
neither this package nor the Bambu project format, so a later intermediate representation
can adapt by exposing ``name``, ``vertices``, ``triangles``, and ``material_index``.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import uuid
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, Union, runtime_checkable
from xml.etree import ElementTree as ET

from image23mf.bambu.profiles import (
    PROFILE_CONTRACT_PATH,
    BambuP2SProfilePlan,
    BambuProfileError,
    resolve_p2s_profile_plan,
)

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
BAMBU_NS = "http://schemas.bambulab.com/package/2021"
RELATIONSHIP_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MODEL_RELATIONSHIP = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
MODEL_CONTENT_TYPE = "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
RELATIONSHIP_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
JSON_CONTENT_TYPE = "application/json"

CONTENT_TYPES_PATH = "[Content_Types].xml"
ROOT_RELS_PATH = "_rels/.rels"
ROOT_MODEL_PATH = "3D/3dmodel.model"
MODEL_RELS_PATH = "3D/_rels/3dmodel.model.rels"
OBJECT_MODEL_PATH = "3D/Objects/object_1.model"
MODEL_SETTINGS_PATH = "Metadata/model_settings.config"
PROJECT_SETTINGS_PATH = "Metadata/project_settings.config"

MEMBER_ORDER = (
    CONTENT_TYPES_PATH,
    ROOT_RELS_PATH,
    ROOT_MODEL_PATH,
    MODEL_RELS_PATH,
    OBJECT_MODEL_PATH,
    MODEL_SETTINGS_PATH,
    PROJECT_SETTINGS_PATH,
    PROFILE_CONTRACT_PATH,
)

ASSEMBLY_OBJECT_ID = 100
BASE_MATERIALS_ID = 1000
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
IDENTITY_3MF = "1 0 0 0 1 0 0 0 1 0 0 0"
IDENTITY_4X4 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"
APPLICATION = "Image23MF Studio 0.1"
UUID_NAMESPACE = uuid.UUID("7e41096c-084e-55eb-9be5-a4e9a8b361e7")
COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")

ET.register_namespace("", CORE_NS)
ET.register_namespace("p", PRODUCTION_NS)

Vertex = tuple[float, float, float]
Triangle = tuple[int, int, int]
InputSource = Union[bytes, bytearray, memoryview, str, os.PathLike[str], BinaryIO]
OutputTarget = Union[str, os.PathLike[str], BinaryIO]


class Bambu3MFError(BambuProfileError):
    """Base error for invalid project inputs or packages."""


class InvalidBambu3MFError(Bambu3MFError):
    """Raised when a package cannot be parsed as the supported Bambu project profile."""


@runtime_checkable
class MeshLike(Protocol):
    """Minimal geometry contract consumed by :func:`build_bambu_3mf`."""

    name: str
    vertices: Sequence[Sequence[float]]
    triangles: Sequence[Sequence[int]]
    material_index: int


@dataclass(frozen=True)
class Mesh:
    """Validated homogeneous mesh part."""

    name: str
    vertices: tuple[Vertex, ...]
    triangles: tuple[Triangle, ...]
    material_index: int = 0


@dataclass(frozen=True)
class Material:
    """A printable material and its Bambu extruder assignment."""

    name: str
    color: str
    extruder: int
    filament_type: str = "PLA"
    preset: str = "Bambu PLA Basic"
    material_id: str | None = None
    palette_color_id: str | None = None
    filament_id: str | None = None


@dataclass(frozen=True)
class BambuProjectSettings:
    """Small, portable subset of Bambu project settings required on import."""

    printer_model: str = "Bambu Lab P2S"
    nozzle_diameter: float = 0.4
    layer_height: float = 0.2
    bed_type: str = "Textured PEI Plate"
    plate_center_x: float = 128.0
    plate_center_y: float = 128.0
    prime_tower_position: tuple[float, float] | None = None


DEFAULT_SETTINGS = BambuProjectSettings()


@dataclass(frozen=True)
class BambuProject:
    """Canonical in-memory representation of one Bambu build plate."""

    name: str
    meshes: tuple[Mesh, ...]
    materials: tuple[Material, ...]
    settings: BambuProjectSettings = BambuProjectSettings()


def _qualified(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _require_name(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Bambu3MFError(f"{field} must be a non-empty string")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise Bambu3MFError(f"{field} contains an XML control character")
    return value.strip()


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise Bambu3MFError(f"{field} must be a finite number")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise Bambu3MFError(f"{field} must be a finite number") from error
    if not math.isfinite(result):
        raise Bambu3MFError(f"{field} must be a finite number")
    return 0.0 if result == 0 else result


def _positive_float(value: object, field: str) -> float:
    result = _finite_float(value, field)
    if result <= 0:
        raise Bambu3MFError(f"{field} must be greater than zero")
    return result


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Bambu3MFError(f"{field} must be an integer")
    return value


def _optional_name(value: object, field: str) -> str | None:
    return None if value is None else _require_name(value, field)


def _normalize_material(material: Material, index: int) -> Material:
    if not isinstance(material, Material):
        raise Bambu3MFError(f"materials[{index}] must be a Material")
    name = _require_name(material.name, f"materials[{index}].name")
    if not isinstance(material.color, str) or not COLOR_PATTERN.fullmatch(material.color):
        raise Bambu3MFError(
            f"materials[{index}].color must use #RRGGBB or #RRGGBBAA hexadecimal notation"
        )
    extruder = _integer(material.extruder, f"materials[{index}].extruder")
    if extruder < 1:
        raise Bambu3MFError(f"materials[{index}].extruder must be at least 1")
    return Material(
        name=name,
        color=material.color.upper(),
        extruder=extruder,
        filament_type=_require_name(material.filament_type, f"materials[{index}].filament_type"),
        preset=_require_name(material.preset, f"materials[{index}].preset"),
        material_id=_optional_name(material.material_id, f"materials[{index}].material_id"),
        palette_color_id=_optional_name(
            material.palette_color_id, f"materials[{index}].palette_color_id"
        ),
        filament_id=_optional_name(material.filament_id, f"materials[{index}].filament_id"),
    )


def _normalize_mesh(mesh: MeshLike, index: int, material_count: int) -> Mesh:
    for attribute in ("name", "vertices", "triangles", "material_index"):
        if not hasattr(mesh, attribute):
            raise Bambu3MFError(f"meshes[{index}] does not implement MeshLike.{attribute}")
    name = _require_name(mesh.name, f"meshes[{index}].name")
    material_index = _integer(mesh.material_index, f"meshes[{index}].material_index")
    if not 0 <= material_index < material_count:
        raise Bambu3MFError(f"meshes[{index}].material_index is outside the material table")

    vertices: list[Vertex] = []
    for vertex_index, vertex in enumerate(mesh.vertices):
        if isinstance(vertex, (str, bytes)) or len(vertex) != 3:
            raise Bambu3MFError(f"meshes[{index}].vertices[{vertex_index}] must have 3 values")
        vertices.append(
            (
                _finite_float(vertex[0], f"meshes[{index}].vertices[{vertex_index}][0]"),
                _finite_float(vertex[1], f"meshes[{index}].vertices[{vertex_index}][1]"),
                _finite_float(vertex[2], f"meshes[{index}].vertices[{vertex_index}][2]"),
            )
        )
    if len(vertices) < 3:
        raise Bambu3MFError(f"meshes[{index}] must contain at least 3 vertices")

    triangles: list[Triangle] = []
    for triangle_index, triangle in enumerate(mesh.triangles):
        if isinstance(triangle, (str, bytes)) or len(triangle) != 3:
            raise Bambu3MFError(f"meshes[{index}].triangles[{triangle_index}] must have 3 indices")
        values = tuple(
            _integer(value, f"meshes[{index}].triangles[{triangle_index}][{position}]")
            for position, value in enumerate(triangle)
        )
        if len(set(values)) != 3:
            raise Bambu3MFError(
                f"meshes[{index}].triangles[{triangle_index}] repeats a vertex index"
            )
        if any(value < 0 or value >= len(vertices) for value in values):
            raise Bambu3MFError(
                f"meshes[{index}].triangles[{triangle_index}] references a missing vertex"
            )
        triangles.append(values)  # type: ignore[arg-type]
    if not triangles:
        raise Bambu3MFError(f"meshes[{index}] must contain at least 1 triangle")
    return Mesh(
        name=name,
        vertices=tuple(vertices),
        triangles=tuple(triangles),
        material_index=material_index,
    )


def _normalize_settings(settings: BambuProjectSettings) -> BambuProjectSettings:
    if not isinstance(settings, BambuProjectSettings):
        raise Bambu3MFError("settings must be BambuProjectSettings")
    tower = settings.prime_tower_position
    if tower is not None:
        if len(tower) != 2:
            raise Bambu3MFError("prime_tower_position must contain x and y")
        tower = tuple(_finite_float(value, "prime_tower_position") for value in tower)
        if any(value < 0 or value > 256 for value in tower):
            raise Bambu3MFError("prime_tower_position must be inside the P2S bed")
    return BambuProjectSettings(
        prime_tower_position=tower,
        printer_model=_require_name(settings.printer_model, "settings.printer_model"),
        nozzle_diameter=_positive_float(settings.nozzle_diameter, "settings.nozzle_diameter"),
        layer_height=_positive_float(settings.layer_height, "settings.layer_height"),
        bed_type=_require_name(settings.bed_type, "settings.bed_type"),
        plate_center_x=_finite_float(settings.plate_center_x, "settings.plate_center_x"),
        plate_center_y=_finite_float(settings.plate_center_y, "settings.plate_center_y"),
    )


def _normalize_project(
    name: str,
    meshes: Iterable[MeshLike],
    materials: Iterable[Material],
    settings: BambuProjectSettings,
) -> BambuProject:
    normalized_materials = tuple(
        _normalize_material(material, index) for index, material in enumerate(materials)
    )
    if not normalized_materials:
        raise Bambu3MFError("at least one material is required")
    material_names = [material.name for material in normalized_materials]
    if len(set(material_names)) != len(material_names):
        raise Bambu3MFError("material names must be unique")
    for field in ("material_id", "palette_color_id"):
        values = [getattr(material, field) for material in normalized_materials]
        if any(value is not None for value in values) and any(value is None for value in values):
            raise Bambu3MFError(f"materials must provide {field} for every slot or no slots")
        present = [value for value in values if value is not None]
        if len(set(present)) != len(present):
            raise Bambu3MFError(f"materials must use unique {field} values")
    expected_extruders = list(range(1, len(normalized_materials) + 1))
    actual_extruders = [material.extruder for material in normalized_materials]
    if actual_extruders != expected_extruders:
        raise Bambu3MFError("materials must be ordered one per contiguous extruder starting at 1")
    normalized_meshes = tuple(
        _normalize_mesh(mesh, index, len(normalized_materials)) for index, mesh in enumerate(meshes)
    )
    if not normalized_meshes:
        raise Bambu3MFError("at least one mesh is required")
    mesh_names = [mesh.name for mesh in normalized_meshes]
    if len(set(mesh_names)) != len(mesh_names):
        raise Bambu3MFError("mesh names must be unique")
    project = BambuProject(
        name=_require_name(name, "name"),
        meshes=normalized_meshes,
        materials=normalized_materials,
        settings=_normalize_settings(settings),
    )
    try:
        _profile_plan(project)
    except BambuProfileError as error:
        raise Bambu3MFError(str(error)) from error
    return project


def _profile_plan(project: BambuProject) -> BambuP2SProfilePlan:
    return resolve_p2s_profile_plan(
        printer_model=project.settings.printer_model,
        nozzle_diameter_mm=project.settings.nozzle_diameter,
        layer_height_mm=project.settings.layer_height,
        bed_type=project.settings.bed_type,
        materials=project.materials,
    )


def _format_number(value: float) -> str:
    text = format(value, ".15g")
    return "0" if text in {"-0", "-0.0"} else text


def _canonical_project_payload(project: BambuProject) -> bytes:
    payload = {
        "materials": [
            {
                "color": material.color,
                "extruder": material.extruder,
                "filament_type": material.filament_type,
                "name": material.name,
                "preset": material.preset,
                "material_id": material.material_id,
                "palette_color_id": material.palette_color_id,
                "filament_id": material.filament_id,
            }
            for material in project.materials
        ],
        "meshes": [
            {
                "material_index": mesh.material_index,
                "name": mesh.name,
                "triangles": mesh.triangles,
                "vertices": mesh.vertices,
            }
            for mesh in project.meshes
        ],
        "name": project.name,
        "settings": {
            "bed_type": project.settings.bed_type,
            "layer_height": project.settings.layer_height,
            "nozzle_diameter": project.settings.nozzle_diameter,
            "plate_center_x": project.settings.plate_center_x,
            "plate_center_y": project.settings.plate_center_y,
            "printer_model": project.settings.printer_model,
        },
    }
    if project.settings.prime_tower_position is not None:
        payload["settings"]["prime_tower_position"] = project.settings.prime_tower_position
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _stable_uuid(seed: str, role: str) -> str:
    return str(uuid.uuid5(UUID_NAMESPACE, f"{seed}:{role}"))


def _xml_bytes(root: ET.Element) -> bytes:
    ET.indent(root, space=" ")
    return (
        ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=True) + b"\n"
    )


def _model_root() -> ET.Element:
    root = ET.Element(
        _qualified(CORE_NS, "model"),
        {
            "unit": "millimeter",
            _qualified("http://www.w3.org/XML/1998/namespace", "lang"): "en-US",
            "requiredextensions": "p",
            "xmlns:BambuStudio": BAMBU_NS,
        },
    )
    ET.SubElement(root, _qualified(CORE_NS, "metadata"), {"name": "Application"}).text = APPLICATION
    ET.SubElement(
        root, _qualified(CORE_NS, "metadata"), {"name": "BambuStudio:3mfVersion"}
    ).text = "1"
    return root


def _content_types() -> bytes:
    # Bambu Studio's OPC reader expects the package namespace to be the default namespace.
    # ElementTree would otherwise emit ``ns0:Types`` because the 3MF core namespace is
    # registered globally as default.
    root = ET.Element("Types", {"xmlns": CONTENT_TYPES_NS})
    ET.SubElement(
        root,
        "Default",
        {"Extension": "rels", "ContentType": RELATIONSHIP_CONTENT_TYPE},
    )
    ET.SubElement(
        root,
        "Default",
        {"Extension": "model", "ContentType": MODEL_CONTENT_TYPE},
    )
    ET.SubElement(
        root,
        "Default",
        {"Extension": "json", "ContentType": JSON_CONTENT_TYPE},
    )
    return _xml_bytes(root)


def _relationships(target: str) -> bytes:
    # As above, this intentionally serializes with a default namespace rather than ns0.
    root = ET.Element("Relationships", {"xmlns": RELATIONSHIP_NS})
    ET.SubElement(
        root,
        "Relationship",
        {"Target": target, "Id": "rel-1", "Type": MODEL_RELATIONSHIP},
    )
    return _xml_bytes(root)


def _object_model(project: BambuProject, seed: str) -> bytes:
    root = _model_root()
    resources = ET.SubElement(root, _qualified(CORE_NS, "resources"))
    base_materials = ET.SubElement(
        resources, _qualified(CORE_NS, "basematerials"), {"id": str(BASE_MATERIALS_ID)}
    )
    for material in project.materials:
        ET.SubElement(
            base_materials,
            _qualified(CORE_NS, "base"),
            {"name": material.name, "displaycolor": material.color},
        )
    for mesh_index, mesh in enumerate(project.meshes, start=1):
        object_element = ET.SubElement(
            resources,
            _qualified(CORE_NS, "object"),
            {
                "id": str(mesh_index),
                _qualified(PRODUCTION_NS, "UUID"): _stable_uuid(seed, f"mesh:{mesh_index}"),
                "type": "model",
                "pid": str(BASE_MATERIALS_ID),
                "pindex": str(mesh.material_index),
            },
        )
        mesh_element = ET.SubElement(object_element, _qualified(CORE_NS, "mesh"))
        vertices_element = ET.SubElement(mesh_element, _qualified(CORE_NS, "vertices"))
        for x, y, z in mesh.vertices:
            ET.SubElement(
                vertices_element,
                _qualified(CORE_NS, "vertex"),
                {"x": _format_number(x), "y": _format_number(y), "z": _format_number(z)},
            )
        triangles_element = ET.SubElement(mesh_element, _qualified(CORE_NS, "triangles"))
        for v1, v2, v3 in mesh.triangles:
            ET.SubElement(
                triangles_element,
                _qualified(CORE_NS, "triangle"),
                {
                    "v1": str(v1),
                    "v2": str(v2),
                    "v3": str(v3),
                    "pid": str(BASE_MATERIALS_ID),
                    "p1": str(mesh.material_index),
                    "p2": str(mesh.material_index),
                    "p3": str(mesh.material_index),
                },
            )
    return _xml_bytes(root)


def _root_model(project: BambuProject, seed: str) -> bytes:
    root = _model_root()
    resources = ET.SubElement(root, _qualified(CORE_NS, "resources"))
    assembly = ET.SubElement(
        resources,
        _qualified(CORE_NS, "object"),
        {
            "id": str(ASSEMBLY_OBJECT_ID),
            _qualified(PRODUCTION_NS, "UUID"): _stable_uuid(seed, "assembly"),
            "type": "model",
        },
    )
    components = ET.SubElement(assembly, _qualified(CORE_NS, "components"))
    for mesh_index, _mesh in enumerate(project.meshes, start=1):
        ET.SubElement(
            components,
            _qualified(CORE_NS, "component"),
            {
                _qualified(PRODUCTION_NS, "path"): f"/{OBJECT_MODEL_PATH}",
                "objectid": str(mesh_index),
                _qualified(PRODUCTION_NS, "UUID"): _stable_uuid(seed, f"component:{mesh_index}"),
                "transform": IDENTITY_3MF,
            },
        )
    build = ET.SubElement(
        root,
        _qualified(CORE_NS, "build"),
        {_qualified(PRODUCTION_NS, "UUID"): _stable_uuid(seed, "build")},
    )
    ET.SubElement(
        build,
        _qualified(CORE_NS, "item"),
        {
            "objectid": str(ASSEMBLY_OBJECT_ID),
            _qualified(PRODUCTION_NS, "UUID"): _stable_uuid(seed, "build-item:1"),
            "transform": (
                "1 0 0 0 1 0 0 0 1 "
                f"{_format_number(project.settings.plate_center_x)} "
                f"{_format_number(project.settings.plate_center_y)} 0"
            ),
            "printable": "1",
        },
    )
    return _xml_bytes(root)


def _metadata(parent: ET.Element, key: str, value: object) -> None:
    ET.SubElement(parent, "metadata", {"key": key, "value": str(value)})


def _model_settings(project: BambuProject) -> bytes:
    root = ET.Element("config")
    object_element = ET.SubElement(root, "object", {"id": str(ASSEMBLY_OBJECT_ID)})
    _metadata(object_element, "name", project.name)
    _metadata(object_element, "extruder", 1)
    for mesh_index, mesh in enumerate(project.meshes, start=1):
        material = project.materials[mesh.material_index]
        part = ET.SubElement(
            object_element, "part", {"id": str(mesh_index), "subtype": "normal_part"}
        )
        _metadata(part, "name", mesh.name)
        _metadata(part, "matrix", IDENTITY_4X4)
        _metadata(part, "source_object_id", mesh_index - 1)
        _metadata(part, "source_volume_id", 0)
        _metadata(part, "extruder", material.extruder)
        _metadata(part, "image23mf_material_index", mesh.material_index)
        if material.material_id is not None:
            _metadata(part, "image23mf_material_id", material.material_id)
        if material.palette_color_id is not None:
            _metadata(part, "image23mf_palette_color_id", material.palette_color_id)
        if material.filament_id is not None:
            _metadata(part, "image23mf_filament_id", material.filament_id)
    plate = ET.SubElement(root, "plate")
    _metadata(plate, "plater_id", 1)
    _metadata(plate, "locked", "false")
    model_instance = ET.SubElement(plate, "model_instance")
    _metadata(model_instance, "object_id", ASSEMBLY_OBJECT_ID)
    _metadata(model_instance, "instance_id", 0)
    return _xml_bytes(root)


def _project_settings(project: BambuProject) -> bytes:
    settings = project.settings
    plan = _profile_plan(project)
    filament_profile_names = [filament.profile.name for filament in plan.filaments]
    filament_count = len(project.materials)
    flush_volumes_matrix = [
        "0" if source == target else "140"
        for source in range(filament_count)
        for target in range(filament_count)
    ]
    payload = {
        "curr_bed_type": settings.bed_type,
        "default_filament_profile": filament_profile_names,
        "default_print_profile": plan.process.name,
        "filament_colour": [material.color for material in project.materials],
        "filament_ids": [
            "GFA01" if material.preset.startswith("Bambu PLA Matte") else "GFA00"
            for material in project.materials
        ],
        "filament_self_index": [
            str(extruder) for extruder in range(1, filament_count + 1) for _nozzle in range(2)
        ],
        "filament_settings_id": filament_profile_names,
        "filament_type": [material.filament_type for material in project.materials],
        "flush_volumes_matrix": flush_volumes_matrix,
        "flush_volumes_vector": ["140"] * (filament_count * 2),
        "image23mf_extruders": [material.extruder for material in project.materials],
        "image23mf_filament_ids": [material.filament_id for material in project.materials],
        "image23mf_material_ids": [material.material_id for material in project.materials],
        "image23mf_palette_color_ids": [
            material.palette_color_id for material in project.materials
        ],
        "image23mf_part_material_indices": [mesh.material_index for mesh in project.meshes],
        "layer_height": _format_number(settings.layer_height),
        "nozzle_diameter": [_format_number(settings.nozzle_diameter)],
        "printer_model": settings.printer_model,
        "printer_settings_id": plan.machine.name,
        "printer_variant": _format_number(settings.nozzle_diameter),
        "version": "1",
    }
    if settings.prime_tower_position is not None:
        x, y = settings.prime_tower_position
        payload.update(wipe_tower_x=[_format_number(x)], wipe_tower_y=[_format_number(y)])
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _profile_contract(project: BambuProject) -> bytes:
    return (
        json.dumps(
            _profile_plan(project).as_contract(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode()


def _package_members(project: BambuProject) -> dict[str, bytes]:
    seed = uuid.uuid5(UUID_NAMESPACE, _canonical_project_payload(project).decode()).hex
    return {
        CONTENT_TYPES_PATH: _content_types(),
        ROOT_RELS_PATH: _relationships(f"/{ROOT_MODEL_PATH}"),
        ROOT_MODEL_PATH: _root_model(project, seed),
        MODEL_RELS_PATH: _relationships(f"/{OBJECT_MODEL_PATH}"),
        OBJECT_MODEL_PATH: _object_model(project, seed),
        MODEL_SETTINGS_PATH: _model_settings(project),
        PROJECT_SETTINGS_PATH: _project_settings(project),
        PROFILE_CONTRACT_PATH: _profile_contract(project),
    }


def _zip_member(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    info.external_attr = 0
    return info


def build_bambu_3mf(
    *,
    name: str,
    meshes: Iterable[MeshLike],
    materials: Iterable[Material],
    settings: BambuProjectSettings = DEFAULT_SETTINGS,
) -> bytes:
    """Return a deterministic Bambu-compatible 3MF package."""

    project = _normalize_project(name, meshes, materials, settings)
    members = _package_members(project)
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in MEMBER_ORDER:
            archive.writestr(_zip_member(member), members[member], compresslevel=9)
    return output.getvalue()


def write_bambu_3mf(
    destination: OutputTarget,
    *,
    name: str,
    meshes: Iterable[MeshLike],
    materials: Iterable[Material],
    settings: BambuProjectSettings = DEFAULT_SETTINGS,
) -> None:
    """Write a deterministic Bambu-compatible 3MF package to a path or binary stream."""

    payload = build_bambu_3mf(name=name, meshes=meshes, materials=materials, settings=settings)
    if hasattr(destination, "write"):
        destination.write(payload)  # type: ignore[union-attr]
    else:
        Path(destination).write_bytes(payload)  # type: ignore[arg-type]


def profile_plan_for_project(project: BambuProject) -> BambuP2SProfilePlan:
    """Return the measured installed-profile plan for a canonical project."""

    if not isinstance(project, BambuProject):
        raise Bambu3MFError("project must be a BambuProject")
    normalized = _normalize_project(
        project.name,
        project.meshes,
        project.materials,
        project.settings,
    )
    return _profile_plan(normalized)


def _open_source(source: InputSource) -> tuple[zipfile.ZipFile, io.BytesIO | None]:
    buffer: io.BytesIO | None = None
    try:
        if isinstance(source, (bytes, bytearray, memoryview)):
            buffer = io.BytesIO(bytes(source))
            return zipfile.ZipFile(buffer), buffer
        return zipfile.ZipFile(source), None
    except (OSError, TypeError, zipfile.BadZipFile) as error:
        if buffer is not None:
            buffer.close()
        raise InvalidBambu3MFError("source is not a readable 3MF ZIP package") from error


def _parse_xml(data: bytes, member: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise InvalidBambu3MFError(f"{member} is not well-formed XML") from error


def _parse_json_object(data: bytes, member: str) -> dict[str, object]:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidBambu3MFError(f"{member} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise InvalidBambu3MFError(f"{member} must be a JSON object")
    return payload


def _read_member(archive: zipfile.ZipFile, member: str) -> bytes:
    try:
        return archive.read(member)
    except KeyError as error:
        raise InvalidBambu3MFError(f"package is missing {member}") from error


def _validate_relationship(data: bytes, member: str, target: str) -> None:
    root = _parse_xml(data, member)
    if root.tag != _qualified(RELATIONSHIP_NS, "Relationships"):
        raise InvalidBambu3MFError(f"{member} uses the wrong relationships namespace")
    relationships = list(root)
    if len(relationships) != 1 or relationships[0].tag != _qualified(
        RELATIONSHIP_NS, "Relationship"
    ):
        raise InvalidBambu3MFError(f"{member} must contain exactly one model relationship")
    relationship = relationships[0]
    expected = {"Target": target, "Id": "rel-1", "Type": MODEL_RELATIONSHIP}
    if relationship.attrib != expected:
        raise InvalidBambu3MFError(f"{member} does not resolve the expected model target")


def _metadata_values(parent: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for element in parent.findall("metadata"):
        key = element.get("key")
        value = element.get("value")
        if key is None or value is None or key in values:
            raise InvalidBambu3MFError("model settings contain malformed or duplicate metadata")
        values[key] = value
    return values


def _required_attribute(element: ET.Element, name: str, member: str) -> str:
    value = element.get(name)
    if value is None:
        raise InvalidBambu3MFError(f"{member} contains an element without {name}")
    return value


def _parse_integer(value: str, field: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise InvalidBambu3MFError(f"{field} must be an integer") from error
    return result


def _production_uuids(root: ET.Element, member: str) -> set[uuid.UUID]:
    identifiers: set[uuid.UUID] = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in {"object", "component", "build", "item"}:
            continue
        value = element.get(_qualified(PRODUCTION_NS, "UUID"))
        if value is None:
            raise InvalidBambu3MFError(f"{member} contains an element without a production UUID")
        try:
            identifier = uuid.UUID(value)
        except ValueError as error:
            raise InvalidBambu3MFError(f"{member} contains an invalid production UUID") from error
        if identifier in identifiers:
            raise InvalidBambu3MFError(f"{member} contains duplicate production UUIDs")
        identifiers.add(identifier)
    return identifiers


def _parse_root_model(data: bytes, expected_parts: int) -> tuple[float, float, set[uuid.UUID]]:
    root = _parse_xml(data, ROOT_MODEL_PATH)
    if root.tag != _qualified(CORE_NS, "model"):
        raise InvalidBambu3MFError("root model uses the wrong core namespace")
    resources = root.find(_qualified(CORE_NS, "resources"))
    build = root.find(_qualified(CORE_NS, "build"))
    if resources is None or build is None:
        raise InvalidBambu3MFError("root model must contain resources and a build")
    objects = resources.findall(_qualified(CORE_NS, "object"))
    if len(objects) != 1 or objects[0].get("id") != str(ASSEMBLY_OBJECT_ID):
        raise InvalidBambu3MFError("root model must contain the canonical assembly object")
    components = objects[0].find(_qualified(CORE_NS, "components"))
    if components is None or len(components) != expected_parts:
        raise InvalidBambu3MFError("assembly component count does not match model parts")
    for index, component in enumerate(components, start=1):
        if (
            component.get("objectid") != str(index)
            or component.get(_qualified(PRODUCTION_NS, "path")) != f"/{OBJECT_MODEL_PATH}"
        ):
            raise InvalidBambu3MFError("assembly components do not resolve canonical part IDs")
    items = build.findall(_qualified(CORE_NS, "item"))
    if len(items) != 1 or items[0].get("objectid") != str(ASSEMBLY_OBJECT_ID):
        raise InvalidBambu3MFError("build must contain exactly one assembly item")
    transform = _required_attribute(items[0], "transform", ROOT_MODEL_PATH).split()
    if len(transform) != 12 or transform[:9] != IDENTITY_3MF.split()[:9] or transform[11] != "0":
        raise InvalidBambu3MFError("build item uses an unsupported transform")
    try:
        return float(transform[9]), float(transform[10]), _production_uuids(root, ROOT_MODEL_PATH)
    except ValueError as error:
        raise InvalidBambu3MFError("build item transform contains a non-number") from error


def _parse_object_model(
    data: bytes,
) -> tuple[list[tuple[str, str]], list[Mesh], set[uuid.UUID]]:
    root = _parse_xml(data, OBJECT_MODEL_PATH)
    if root.tag != _qualified(CORE_NS, "model"):
        raise InvalidBambu3MFError("object model uses the wrong core namespace")
    resources = root.find(_qualified(CORE_NS, "resources"))
    if resources is None:
        raise InvalidBambu3MFError("object model does not contain resources")
    material_element = resources.find(_qualified(CORE_NS, "basematerials"))
    if material_element is None or material_element.get("id") != str(BASE_MATERIALS_ID):
        raise InvalidBambu3MFError("object model does not contain the canonical material table")
    material_values = [
        (
            _required_attribute(base, "name", OBJECT_MODEL_PATH),
            _required_attribute(base, "displaycolor", OBJECT_MODEL_PATH),
        )
        for base in material_element.findall(_qualified(CORE_NS, "base"))
    ]
    if not material_values:
        raise InvalidBambu3MFError("object model material table is empty")
    meshes: list[Mesh] = []
    objects = resources.findall(_qualified(CORE_NS, "object"))
    for object_index, object_element in enumerate(objects, start=1):
        if object_element.get("id") != str(object_index):
            raise InvalidBambu3MFError("object model part IDs must be contiguous and stable")
        material_index = _parse_integer(
            _required_attribute(object_element, "pindex", OBJECT_MODEL_PATH), "object pindex"
        )
        mesh_element = object_element.find(_qualified(CORE_NS, "mesh"))
        if mesh_element is None:
            raise InvalidBambu3MFError("object part does not contain a mesh")
        vertices_element = mesh_element.find(_qualified(CORE_NS, "vertices"))
        triangles_element = mesh_element.find(_qualified(CORE_NS, "triangles"))
        if vertices_element is None or triangles_element is None:
            raise InvalidBambu3MFError("mesh does not contain vertices and triangles")
        vertices: list[Vertex] = []
        for vertex in vertices_element:
            try:
                values = tuple(
                    float(_required_attribute(vertex, axis, OBJECT_MODEL_PATH)) for axis in "xyz"
                )
            except ValueError as error:
                raise InvalidBambu3MFError("mesh vertex contains a non-number") from error
            vertices.append(values)  # type: ignore[arg-type]
        triangles: list[Triangle] = []
        for triangle in triangles_element:
            values = tuple(
                _parse_integer(
                    _required_attribute(triangle, axis, OBJECT_MODEL_PATH), f"triangle {axis}"
                )
                for axis in ("v1", "v2", "v3")
            )
            triangle_materials = tuple(
                _parse_integer(
                    _required_attribute(triangle, key, OBJECT_MODEL_PATH), f"triangle {key}"
                )
                for key in ("p1", "p2", "p3")
            )
            if triangle.get("pid") != str(BASE_MATERIALS_ID) or triangle_materials != (
                material_index,
                material_index,
                material_index,
            ):
                raise InvalidBambu3MFError("triangle material metadata conflicts with its part")
            triangles.append(values)  # type: ignore[arg-type]
        try:
            mesh = _normalize_mesh(
                Mesh("pending", tuple(vertices), tuple(triangles), material_index),
                object_index - 1,
                len(material_values),
            )
        except Bambu3MFError as error:
            raise InvalidBambu3MFError(str(error)) from error
        meshes.append(mesh)
    if not meshes:
        raise InvalidBambu3MFError("object model does not contain mesh parts")
    return material_values, meshes, _production_uuids(root, OBJECT_MODEL_PATH)


def read_bambu_3mf(source: InputSource) -> BambuProject:
    """Parse and validate the deterministic Bambu project profile produced here."""

    archive, buffer = _open_source(source)
    try:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise InvalidBambu3MFError("package contains duplicate ZIP members")
        for member in MEMBER_ORDER:
            if member not in names:
                raise InvalidBambu3MFError(f"package is missing {member}")
        content_types = _parse_xml(_read_member(archive, CONTENT_TYPES_PATH), CONTENT_TYPES_PATH)
        if content_types.tag != _qualified(CONTENT_TYPES_NS, "Types"):
            raise InvalidBambu3MFError("content types use the wrong OPC namespace")
        content_map = {child.get("Extension"): child.get("ContentType") for child in content_types}
        if (
            content_map.get("rels") != RELATIONSHIP_CONTENT_TYPE
            or content_map.get("model") != MODEL_CONTENT_TYPE
            or content_map.get("json") != JSON_CONTENT_TYPE
        ):
            raise InvalidBambu3MFError(
                "package does not declare 3MF model, relationship, and JSON types"
            )
        _validate_relationship(
            _read_member(archive, ROOT_RELS_PATH), ROOT_RELS_PATH, f"/{ROOT_MODEL_PATH}"
        )
        _validate_relationship(
            _read_member(archive, MODEL_RELS_PATH),
            MODEL_RELS_PATH,
            f"/{OBJECT_MODEL_PATH}",
        )

        project_settings = _parse_json_object(
            _read_member(archive, PROJECT_SETTINGS_PATH), PROJECT_SETTINGS_PATH
        )
        profile_contract = _parse_json_object(
            _read_member(archive, PROFILE_CONTRACT_PATH), PROFILE_CONTRACT_PATH
        )

        material_values, placeholder_meshes, object_uuids = _parse_object_model(
            _read_member(archive, OBJECT_MODEL_PATH)
        )
        plate_x, plate_y, root_uuids = _parse_root_model(
            _read_member(archive, ROOT_MODEL_PATH), len(placeholder_meshes)
        )
        if root_uuids & object_uuids:
            raise InvalidBambu3MFError("model parts contain duplicate production UUIDs")

        settings_root = _parse_xml(_read_member(archive, MODEL_SETTINGS_PATH), MODEL_SETTINGS_PATH)
        if settings_root.tag != "config":
            raise InvalidBambu3MFError("model settings root must be config")
        object_settings = settings_root.find(f"object[@id='{ASSEMBLY_OBJECT_ID}']")
        if object_settings is None:
            raise InvalidBambu3MFError("model settings do not describe the assembly")
        object_metadata = _metadata_values(object_settings)
        project_name = object_metadata.get("name")
        if not project_name:
            raise InvalidBambu3MFError("model settings do not name the project")
        parts = object_settings.findall("part")
        if len(parts) != len(placeholder_meshes):
            raise InvalidBambu3MFError("model settings part count does not match geometry")

        colors = project_settings.get("filament_colour")
        filament_types = project_settings.get("filament_type")
        presets = project_settings.get("filament_settings_id")
        default_presets = project_settings.get("default_filament_profile")
        bambu_filament_ids = project_settings.get("filament_ids")
        filament_self_index = project_settings.get("filament_self_index")
        flush_matrix = project_settings.get("flush_volumes_matrix")
        flush_vector = project_settings.get("flush_volumes_vector")
        extruder_slots = project_settings.get("image23mf_extruders")
        material_ids = project_settings.get("image23mf_material_ids")
        palette_color_ids = project_settings.get("image23mf_palette_color_ids")
        filament_ids = project_settings.get("image23mf_filament_ids")
        part_material_indices = project_settings.get("image23mf_part_material_indices")
        filament_arrays = (
            colors,
            filament_types,
            presets,
            default_presets,
            bambu_filament_ids,
            extruder_slots,
            material_ids,
            palette_color_ids,
            filament_ids,
        )
        if not all(isinstance(value, list) for value in filament_arrays):
            raise InvalidBambu3MFError("project settings do not contain filament arrays")
        material_count = len(material_values)
        if any(len(value) != material_count for value in filament_arrays):
            raise InvalidBambu3MFError("project filament arrays do not match the material table")
        if not isinstance(part_material_indices, list) or len(part_material_indices) != len(parts):
            raise InvalidBambu3MFError("project part mapping does not match model parts")
        if (
            not isinstance(filament_self_index, list)
            or len(filament_self_index) != 2 * material_count
        ):
            raise InvalidBambu3MFError(
                "filament self-index array must contain two entries per slot"
            )
        if not isinstance(flush_matrix, list) or len(flush_matrix) != material_count**2:
            raise InvalidBambu3MFError("flush volume matrix must contain one value per slot pair")
        if not isinstance(flush_vector, list) or len(flush_vector) != 2 * material_count:
            raise InvalidBambu3MFError("flush volume vector must contain two values per slot")
        expected_self_index = [
            str(extruder) for extruder in range(1, material_count + 1) for _nozzle in range(2)
        ]
        if filament_self_index != expected_self_index:
            raise InvalidBambu3MFError("filament self-index array conflicts with material slots")
        try:
            invalid_flush_value = any(
                not isinstance(value, str) or not value or float(value) < 0
                for value in (*flush_matrix, *flush_vector)
            )
        except ValueError:
            invalid_flush_value = True
        if invalid_flush_value:
            raise InvalidBambu3MFError("flush volume arrays must contain non-negative numbers")
        if extruder_slots != list(range(1, material_count + 1)):
            raise InvalidBambu3MFError("project extruder slots must be contiguous and ordered")
        if default_presets != presets:
            raise InvalidBambu3MFError("default filament profiles conflict with material presets")
        for field, values, allow_partial in (
            ("material IDs", material_ids, False),
            ("palette color IDs", palette_color_ids, False),
            ("filament IDs", filament_ids, True),
        ):
            if any(
                value is not None and (not isinstance(value, str) or not value) for value in values
            ):
                raise InvalidBambu3MFError(f"project {field} contain an invalid value")
            if (
                not allow_partial
                and any(value is None for value in values)
                and any(value is not None for value in values)
            ):
                raise InvalidBambu3MFError(f"project {field} must cover every slot or no slots")
        for field, values in (
            ("material IDs", material_ids),
            ("palette color IDs", palette_color_ids),
        ):
            present = [value for value in values if value is not None]
            if len(present) != len(set(present)):
                raise InvalidBambu3MFError(f"project {field} must be unique")

        extruders_by_material: dict[int, int] = {}
        meshes: list[Mesh] = []
        for index, (part, mesh) in enumerate(zip(parts, placeholder_meshes), start=1):
            if part.get("id") != str(index):
                raise InvalidBambu3MFError("model settings part IDs do not match geometry")
            metadata = _metadata_values(part)
            mesh_name = metadata.get("name")
            if not mesh_name:
                raise InvalidBambu3MFError("model settings part does not have a name")
            extruder = _parse_integer(metadata.get("extruder", ""), "part extruder")
            material_index = _parse_integer(
                metadata.get("image23mf_material_index", ""), "part material index"
            )
            if (
                material_index != mesh.material_index
                or part_material_indices[index - 1] != material_index
            ):
                raise InvalidBambu3MFError("part material mapping conflicts with geometry")
            if extruder != extruder_slots[material_index]:
                raise InvalidBambu3MFError("part extruder conflicts with its material slot")
            for metadata_key, values in (
                ("image23mf_material_id", material_ids),
                ("image23mf_palette_color_id", palette_color_ids),
                ("image23mf_filament_id", filament_ids),
            ):
                expected_value = values[material_index]
                actual_value = metadata.get(metadata_key)
                if actual_value != expected_value:
                    raise InvalidBambu3MFError(
                        f"part {metadata_key} conflicts with its material slot"
                    )
            known_extruder = extruders_by_material.setdefault(mesh.material_index, extruder)
            if known_extruder != extruder:
                raise InvalidBambu3MFError("one material is assigned to conflicting extruders")
            meshes.append(Mesh(mesh_name, mesh.vertices, mesh.triangles, mesh.material_index))

        materials: list[Material] = []
        for index, (material_name, display_color) in enumerate(material_values):
            if colors[index] != display_color:
                raise InvalidBambu3MFError(
                    "project filament color conflicts with material metadata"
                )
            materials.append(
                Material(
                    name=material_name,
                    color=display_color,
                    extruder=extruders_by_material.get(index, index + 1),
                    filament_type=str(filament_types[index]),
                    preset=str(presets[index]),
                    material_id=material_ids[index],
                    palette_color_id=palette_color_ids[index],
                    filament_id=filament_ids[index],
                )
            )

        try:
            settings = BambuProjectSettings(
                printer_model=str(project_settings["printer_model"]),
                nozzle_diameter=float(project_settings["nozzle_diameter"][0]),
                layer_height=float(project_settings["layer_height"]),
                bed_type=str(project_settings["curr_bed_type"]),
                plate_center_x=plate_x,
                plate_center_y=plate_y,
                prime_tower_position=(
                    (
                        float(project_settings["wipe_tower_x"][0]),
                        float(project_settings["wipe_tower_y"][0]),
                    )
                    if "wipe_tower_x" in project_settings or "wipe_tower_y" in project_settings
                    else None
                ),
            )
            project = _normalize_project(project_name, meshes, materials, settings)
            expected_contract = _profile_plan(project).as_contract()
            if profile_contract != expected_contract:
                raise InvalidBambu3MFError(
                    "Image23MF profile contract conflicts with canonical project settings"
                )
            return project
        except InvalidBambu3MFError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, Bambu3MFError) as error:
            raise InvalidBambu3MFError(f"invalid Bambu project settings: {error}") from error
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise InvalidBambu3MFError("3MF ZIP package could not be read") from error
    finally:
        archive.close()
        if buffer is not None:
            buffer.close()


def read_bambu_profile_plan(source: InputSource) -> BambuP2SProfilePlan:
    """Validate a generated package and return its measured installed-profile plan."""

    return _profile_plan(read_bambu_3mf(source))
