"""Deterministic multi-build-plate Bambu packages for ordered mural tiles.

The ordinary Bambu writer intentionally models one build plate.  This module keeps the
multi-plate contract separate: every row-major mural tile owns one assembly, one model-settings
plate assignment, one placed bounds record, and one generated preview.  A reserved rectangle is
evidence for Bambu Studio's slicer-generated prime tower; no tower mesh is inserted here.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Union
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw

from image23mf.bambu.package import (
    CONTENT_TYPES_NS,
    CORE_NS,
    IDENTITY_3MF,
    JSON_CONTENT_TYPE,
    MODEL_CONTENT_TYPE,
    MODEL_RELATIONSHIP,
    MODEL_RELS_PATH,
    MODEL_SETTINGS_PATH,
    PRODUCTION_NS,
    PROFILE_CONTRACT_PATH,
    PROJECT_SETTINGS_PATH,
    RELATIONSHIP_CONTENT_TYPE,
    RELATIONSHIP_NS,
    ROOT_MODEL_PATH,
    ROOT_RELS_PATH,
    UUID_NAMESPACE,
    Bambu3MFError,
    BambuProject,
    BambuProjectSettings,
    InvalidBambu3MFError,
    Material,
    Mesh,
    MeshLike,
    _format_number,
    _metadata,
    _model_root,
    _normalize_mesh,
    _normalize_project,
    _normalize_settings,
    _parse_json_object,
    _production_uuids,
    _profile_contract,
    _project_settings,
    _qualified,
    _relationships,
    _require_name,
    _stable_uuid,
    _xml_bytes,
    _zip_member,
)

MURAL_PACKAGE_SCHEMA_VERSION = 1
MURAL_MANIFEST_PATH = "Metadata/image23mf_mural.json"
PNG_CONTENT_TYPE = "image/png"
PLATE_WIDTH_MM = 256.0
PLATE_HEIGHT_MM = 256.0
PLATE_GRID_STEP_MM = PLATE_WIDTH_MM * 1.2
TILE_ID_PATTERN = re.compile(r"^tile-r([0-9]{2})-c([0-9]{2})$")

InputSource = Union[bytes, bytearray, memoryview, str, Path, BinaryIO]


class BambuMuralPackageError(Bambu3MFError):
    """A mural cannot be represented by the supported multi-plate profile."""


class InvalidBambuMural3MFError(InvalidBambu3MFError, BambuMuralPackageError):
    """A multi-plate package is missing or contradicts retained mural evidence."""


@dataclass(frozen=True)
class PrimeTowerReserve:
    """Build-plate rectangle kept empty for a slicer-generated prime tower."""

    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    tower_width_mm: float = 35.0
    tower_x_mm: float | None = None
    tower_y_mm: float | None = None

    @property
    def right_mm(self) -> float:
        return self.x_mm + self.width_mm

    @property
    def top_mm(self) -> float:
        return self.y_mm + self.height_mm

    @property
    def effective_tower_x_mm(self) -> float:
        return self.x_mm if self.tower_x_mm is None else self.tower_x_mm

    @property
    def effective_tower_y_mm(self) -> float:
        return self.y_mm if self.tower_y_mm is None else self.tower_y_mm


@dataclass(frozen=True)
class MuralBuildPlate:
    """One ordered mural tile and its local geometry placement."""

    tile_id: str
    row: int
    column: int
    build_plate_index: int
    name: str
    meshes: tuple[MeshLike, ...]
    origin_x_mm: float
    origin_y_mm: float
    source_geometry_fingerprint: str


@dataclass(frozen=True)
class MuralPlateEvidence:
    tile_id: str
    row: int
    column: int
    build_plate_index: int
    name: str
    assembly_object_id: int
    mesh_object_ids: tuple[int, ...]
    source_geometry_fingerprint: str
    geometry_sha256: str
    origin_x_mm: float
    origin_y_mm: float
    placed_bounds_mm: tuple[float, float, float, float, float, float]
    preview_path: str
    preview_sha256: str


@dataclass(frozen=True)
class BambuMuralProject:
    """Canonical independently-read multi-plate project."""

    name: str
    plates: tuple[MuralBuildPlate, ...]
    materials: tuple[Material, ...]
    settings: BambuProjectSettings
    prime_tower_reserve: PrimeTowerReserve
    evidence: tuple[MuralPlateEvidence, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class _NormalizedPlate:
    tile_id: str
    row: int
    column: int
    build_plate_index: int
    name: str
    meshes: tuple[Mesh, ...]
    origin_x_mm: float
    origin_y_mm: float
    source_geometry_fingerprint: str
    assembly_object_id: int
    mesh_object_ids: tuple[int, ...]
    geometry_sha256: str
    placed_bounds_mm: tuple[float, float, float, float, float, float]


def build_bambu_mural_3mf(
    *,
    name: str,
    plates: Iterable[MuralBuildPlate],
    materials: Iterable[Material],
    settings: BambuProjectSettings,
    prime_tower_reserve: PrimeTowerReserve,
) -> bytes:
    """Return one deterministic Bambu project with exactly one mural tile per plate."""

    project_name = _require_name(name, "name")
    normalized_settings = _normalize_settings(settings)
    reserve = _normalize_reserve(prime_tower_reserve)
    raw_materials = tuple(materials)
    raw_plates = tuple(plates)
    if not raw_plates:
        raise BambuMuralPackageError("at least one mural build plate is required")

    # Reuse the single-plate normalizer for the material/profile contract.  One temporary mesh
    # is enough to validate every material slot without changing the per-plate geometry below.
    first_meshes = tuple(raw_plates[0].meshes)
    if not first_meshes:
        raise BambuMuralPackageError("every mural build plate must contain geometry")
    material_probe = _normalize_project(
        project_name,
        first_meshes,
        raw_materials,
        normalized_settings,
    )
    normalized_materials = material_probe.materials
    normalized_plates = _normalize_plates(
        raw_plates,
        material_count=len(normalized_materials),
        reserve=reserve,
    )
    flattened_meshes = tuple(mesh for plate in normalized_plates for mesh in plate.meshes)
    flattened = BambuProject(
        name=project_name,
        meshes=flattened_meshes,
        materials=normalized_materials,
        settings=normalized_settings,
    )
    # All output part names are plate-qualified and therefore globally unique.
    flattened = _normalize_project(
        flattened.name,
        flattened.meshes,
        flattened.materials,
        flattened.settings,
    )

    preview_bytes = {
        _preview_path(plate.build_plate_index): _render_plate_preview(
            plate,
            flattened.materials,
            reserve,
        )
        for plate in normalized_plates
    }
    evidence = tuple(
        MuralPlateEvidence(
            tile_id=plate.tile_id,
            row=plate.row,
            column=plate.column,
            build_plate_index=plate.build_plate_index,
            name=plate.name,
            assembly_object_id=plate.assembly_object_id,
            mesh_object_ids=plate.mesh_object_ids,
            source_geometry_fingerprint=plate.source_geometry_fingerprint,
            geometry_sha256=plate.geometry_sha256,
            origin_x_mm=plate.origin_x_mm,
            origin_y_mm=plate.origin_y_mm,
            placed_bounds_mm=plate.placed_bounds_mm,
            preview_path=_preview_path(plate.build_plate_index),
            preview_sha256=hashlib.sha256(
                preview_bytes[_preview_path(plate.build_plate_index)]
            ).hexdigest(),
        )
        for plate in normalized_plates
    )
    manifest = _mural_manifest(
        project_name,
        normalized_settings,
        flattened.materials,
        reserve,
        evidence,
    )
    manifest_bytes = _json_bytes(manifest)
    seed = uuid.uuid5(UUID_NAMESPACE, manifest_bytes.decode("utf-8")).hex
    object_members = {
        _object_path(plate.build_plate_index): _mural_object_model(
            plate,
            flattened.materials,
            seed,
        )
        for plate in normalized_plates
    }
    members = {
        "[Content_Types].xml": _content_types(),
        ROOT_RELS_PATH: _relationships(f"/{ROOT_MODEL_PATH}"),
        ROOT_MODEL_PATH: _mural_root_model(normalized_plates, seed),
        MODEL_RELS_PATH: _mural_relationships(len(normalized_plates)),
        MODEL_SETTINGS_PATH: _mural_model_settings(normalized_plates, flattened.materials),
        PROJECT_SETTINGS_PATH: _mural_project_settings(
            flattened,
            reserve,
            len(normalized_plates),
        ),
        PROFILE_CONTRACT_PATH: _profile_contract(flattened),
        MURAL_MANIFEST_PATH: manifest_bytes,
        **object_members,
        **preview_bytes,
    }
    order = _member_order(len(normalized_plates))
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in order:
            archive.writestr(_zip_member(member), members[member], compresslevel=9)
    payload = output.getvalue()
    # The independent reader is a mandatory build-time closure gate.
    read_bambu_mural_3mf(payload)
    return payload


def read_bambu_mural_3mf(
    source: InputSource,
    *,
    expected_manifest_sha256: str | None = None,
    expected_plate_provenance: Mapping[str, str] | None = None,
) -> BambuMuralProject:
    """Parse and cross-check a deterministic multi-plate mural package.

    Internal hashes prove that the archive is self-consistent.  Callers with persisted project
    state can additionally provide the expected manifest digest and/or tile provenance mapping as
    an external trust anchor; an opaque source fingerprint cannot authenticate itself.
    """

    archive, buffer = _open_source(source)
    try:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise InvalidBambuMural3MFError("mural package contains duplicate ZIP members")
        manifest_bytes = _read_member(archive, MURAL_MANIFEST_PATH)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
            raise InvalidBambuMural3MFError("mural manifest does not match persisted project state")
        manifest = _parse_json_object(manifest_bytes, MURAL_MANIFEST_PATH)
        plate_payloads = _manifest_plates(manifest)
        if expected_plate_provenance is not None:
            actual_provenance = {
                str(payload["tile_id"]): str(payload["source_geometry_fingerprint"])
                for payload in plate_payloads
            }
            if actual_provenance != dict(expected_plate_provenance):
                raise InvalidBambuMural3MFError(
                    "mural tile provenance does not match persisted project state"
                )
        expected_members = _member_order(len(plate_payloads))
        if set(names) != set(expected_members) or len(names) != len(expected_members):
            missing = [member for member in expected_members if member not in names]
            if missing:
                raise InvalidBambuMural3MFError(f"mural package is missing {missing[0]}")
            raise InvalidBambuMural3MFError("mural package contains unexpected ZIP members")

        _validate_content_types(_read_member(archive, "[Content_Types].xml"))
        _validate_relationships(
            _read_member(archive, ROOT_RELS_PATH),
            f"/{ROOT_MODEL_PATH}",
            ROOT_RELS_PATH,
        )
        _validate_mural_relationships(
            _read_member(archive, MODEL_RELS_PATH),
            len(plate_payloads),
        )

        seed = uuid.uuid5(UUID_NAMESPACE, manifest_bytes.decode("utf-8")).hex
        placeholder_meshes: dict[int, Mesh] = {}
        object_uuids: set[uuid.UUID] = set()
        for payload in plate_payloads:
            index = int(payload["build_plate_index"])
            meshes, uuids = _parse_mural_object_model(
                _read_member(archive, _object_path(index)),
                path=_object_path(index),
                expected_mesh_ids=tuple(int(value) for value in payload["mesh_object_ids"]),
                seed=seed,
            )
            if placeholder_meshes.keys() & meshes.keys():
                raise InvalidBambuMural3MFError("mural object files reuse a mesh object ID")
            if object_uuids & uuids:
                raise InvalidBambuMural3MFError(
                    "mural object files contain duplicate production UUIDs"
                )
            object_uuids.update(uuids)
            placeholder_meshes.update(meshes)
        model_settings_bytes = _read_member(archive, MODEL_SETTINGS_PATH)
        model_settings = _parse_model_settings(
            model_settings_bytes,
            plate_payloads,
            placeholder_meshes,
        )
        root_uuids = _parse_mural_root(
            _read_member(archive, ROOT_MODEL_PATH),
            plate_payloads,
            model_settings["mesh_ids_by_plate"],
            seed=seed,
        )
        if root_uuids & object_uuids:
            raise InvalidBambuMural3MFError("mural package contains duplicate production UUIDs")

        settings_payload = _parse_json_object(
            _read_member(archive, PROJECT_SETTINGS_PATH), PROJECT_SETTINGS_PATH
        )
        material_names = settings_payload.get("image23mf_material_names")
        material_colors = settings_payload.get("filament_colour")
        if (
            not isinstance(material_names, list)
            or not isinstance(material_colors, list)
            or not material_names
            or len(material_names) != len(material_colors)
        ):
            raise InvalidBambuMural3MFError("mural project settings lack material identities")
        material_values = [
            (str(name), str(color)) for name, color in zip(material_names, material_colors)
        ]
        materials, settings = _parse_materials_and_settings(
            material_values,
            settings_payload,
            model_settings["mesh_material_indices"],
        )
        mesh_id_order = tuple(sorted(placeholder_meshes))
        named_meshes = tuple(
            Mesh(
                name=model_settings["mesh_names"][mesh_id],
                vertices=mesh.vertices,
                triangles=mesh.triangles,
                material_index=model_settings["mesh_material_indices"][mesh_id],
            )
            for mesh_id in mesh_id_order
            for mesh in (placeholder_meshes[mesh_id],)
        )
        flattened = _normalize_project(
            str(manifest["project_name"]),
            named_meshes,
            materials,
            settings,
        )
        profile_contract = _parse_json_object(
            _read_member(archive, PROFILE_CONTRACT_PATH), PROFILE_CONTRACT_PATH
        )
        expected_profile = json.loads(_profile_contract(flattened))
        if profile_contract != expected_profile:
            raise InvalidBambuMural3MFError(
                "mural profile contract conflicts with project settings"
            )
        reserve = _reserve_from_manifest(manifest)
        expected_settings = json.loads(
            _mural_project_settings(flattened, reserve, len(plate_payloads))
        )
        if settings_payload != expected_settings:
            raise InvalidBambuMural3MFError("mural project settings are not canonical")
        flattened_by_id = dict(zip(mesh_id_order, flattened.meshes))
        plates: list[MuralBuildPlate] = []
        evidence: list[MuralPlateEvidence] = []
        normalized_plates: list[_NormalizedPlate] = []
        for payload in plate_payloads:
            index = int(payload["build_plate_index"])
            mesh_ids = tuple(int(value) for value in payload["mesh_object_ids"])
            meshes = tuple(flattened_by_id[mesh_id] for mesh_id in mesh_ids)
            plate = MuralBuildPlate(
                tile_id=str(payload["tile_id"]),
                row=int(payload["row"]),
                column=int(payload["column"]),
                build_plate_index=index,
                name=str(payload["name"]),
                meshes=meshes,
                origin_x_mm=float(payload["origin_x_mm"]),
                origin_y_mm=float(payload["origin_y_mm"]),
                source_geometry_fingerprint=str(payload["source_geometry_fingerprint"]),
            )
            geometry_sha = _geometry_sha256(meshes)
            if geometry_sha != payload["geometry_sha256"]:
                raise InvalidBambuMural3MFError(
                    f"plate {index} geometry does not match its retained fingerprint"
                )
            bounds = _placed_bounds(meshes, plate.origin_x_mm, plate.origin_y_mm)
            if list(bounds) != payload["placed_bounds_mm"]:
                raise InvalidBambuMural3MFError(
                    f"plate {index} bounds do not match its retained evidence"
                )
            _validate_placement(bounds, reserve, index)
            preview_path = str(payload["preview_path"])
            preview = _read_member(archive, preview_path)
            if hashlib.sha256(preview).hexdigest() != payload["preview_sha256"]:
                raise InvalidBambuMural3MFError(
                    f"plate {index} preview does not match its retained fingerprint"
                )
            _verify_png(preview, index)
            normalized_preview_plate = _NormalizedPlate(
                tile_id=plate.tile_id,
                row=plate.row,
                column=plate.column,
                build_plate_index=index,
                name=plate.name,
                meshes=meshes,
                origin_x_mm=plate.origin_x_mm,
                origin_y_mm=plate.origin_y_mm,
                source_geometry_fingerprint=plate.source_geometry_fingerprint,
                assembly_object_id=int(payload["assembly_object_id"]),
                mesh_object_ids=mesh_ids,
                geometry_sha256=geometry_sha,
                placed_bounds_mm=bounds,
            )
            normalized_plates.append(normalized_preview_plate)
            if preview != _render_plate_preview(
                normalized_preview_plate,
                materials,
                reserve,
            ):
                raise InvalidBambuMural3MFError(
                    f"plate {index} preview is not the canonical geometry rendering"
                )
            plates.append(plate)
            evidence.append(
                MuralPlateEvidence(
                    tile_id=plate.tile_id,
                    row=plate.row,
                    column=plate.column,
                    build_plate_index=index,
                    name=plate.name,
                    assembly_object_id=int(payload["assembly_object_id"]),
                    mesh_object_ids=mesh_ids,
                    source_geometry_fingerprint=plate.source_geometry_fingerprint,
                    geometry_sha256=geometry_sha,
                    origin_x_mm=plate.origin_x_mm,
                    origin_y_mm=plate.origin_y_mm,
                    placed_bounds_mm=bounds,
                    preview_path=preview_path,
                    preview_sha256=str(payload["preview_sha256"]),
                )
            )
        if model_settings_bytes != _mural_model_settings(tuple(normalized_plates), materials):
            raise InvalidBambuMural3MFError("mural model settings are not canonical")
        canonical_manifest = _mural_manifest(
            str(manifest["project_name"]), settings, materials, reserve, tuple(evidence)
        )
        if manifest != canonical_manifest:
            raise InvalidBambuMural3MFError("mural manifest is not canonical")
        return BambuMuralProject(
            name=str(manifest["project_name"]),
            plates=tuple(plates),
            materials=materials,
            settings=settings,
            prime_tower_reserve=reserve,
            evidence=tuple(evidence),
            manifest_sha256=manifest_sha256,
        )
    except BambuMuralPackageError:
        raise
    except (Bambu3MFError, KeyError, TypeError, ValueError, IndexError) as error:
        raise InvalidBambuMural3MFError(f"invalid mural package: {error}") from error
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise InvalidBambuMural3MFError("mural 3MF ZIP package could not be read") from error
    finally:
        archive.close()
        if buffer is not None:
            buffer.close()


def _normalize_reserve(reserve: PrimeTowerReserve) -> PrimeTowerReserve:
    if not isinstance(reserve, PrimeTowerReserve):
        raise BambuMuralPackageError("prime_tower_reserve must be a PrimeTowerReserve")
    values = (
        reserve.x_mm,
        reserve.y_mm,
        reserve.width_mm,
        reserve.height_mm,
        reserve.tower_width_mm,
        reserve.effective_tower_x_mm,
        reserve.effective_tower_y_mm,
    )
    if any(not math.isfinite(float(value)) for value in values):
        raise BambuMuralPackageError("prime tower reserve must use finite millimetres")
    normalized_values = tuple(round(float(value), 6) for value in values)
    normalized = PrimeTowerReserve(
        x_mm=normalized_values[0],
        y_mm=normalized_values[1],
        width_mm=normalized_values[2],
        height_mm=normalized_values[3],
        tower_width_mm=normalized_values[4],
        tower_x_mm=None if reserve.tower_x_mm is None else normalized_values[5],
        tower_y_mm=None if reserve.tower_y_mm is None else normalized_values[6],
    )
    if normalized.x_mm < 0 or normalized.y_mm < 0:
        raise BambuMuralPackageError("prime tower reserve must remain on the build plate")
    if normalized.width_mm <= 0 or normalized.height_mm <= 0:
        raise BambuMuralPackageError("prime tower reserve must have positive area")
    if normalized.tower_width_mm <= 0:
        raise BambuMuralPackageError("prime tower width must be positive")
    if normalized.tower_width_mm > min(normalized.width_mm, normalized.height_mm):
        raise BambuMuralPackageError("prime tower must fit inside its reserved rectangle")
    if (
        normalized.effective_tower_x_mm < normalized.x_mm
        or normalized.effective_tower_y_mm < normalized.y_mm
        or normalized.effective_tower_x_mm + normalized.tower_width_mm > normalized.right_mm
        or normalized.effective_tower_y_mm + normalized.tower_width_mm > normalized.top_mm
    ):
        raise BambuMuralPackageError("prime tower position must fit inside its reserved rectangle")
    if normalized.right_mm > PLATE_WIDTH_MM or normalized.top_mm > PLATE_HEIGHT_MM:
        raise BambuMuralPackageError("prime tower reserve must remain on the build plate")
    return normalized


def _normalize_plates(
    plates: tuple[MuralBuildPlate, ...],
    *,
    material_count: int,
    reserve: PrimeTowerReserve,
) -> tuple[_NormalizedPlate, ...]:
    expected_positions = sorted((plate.row, plate.column) for plate in plates)
    actual_positions = [(plate.row, plate.column) for plate in plates]
    if actual_positions != expected_positions:
        raise BambuMuralPackageError("mural plates must be supplied in row-major order")
    if len(set(actual_positions)) != len(plates):
        raise BambuMuralPackageError("mural plates contain duplicate grid positions")
    if [plate.build_plate_index for plate in plates] != list(range(1, len(plates) + 1)):
        raise BambuMuralPackageError("mural build plate indices must be contiguous and row-major")
    rows = max(plate.row for plate in plates)
    columns = max(plate.column for plate in plates)
    expected_grid = [
        (row, column) for row in range(1, rows + 1) for column in range(1, columns + 1)
    ]
    if actual_positions != expected_grid:
        raise BambuMuralPackageError("mural grid must contain every row-major tile position")
    names: set[str] = set()
    tile_ids: set[str] = set()
    next_object_id = 1
    result: list[_NormalizedPlate] = []
    for plate in plates:
        match = TILE_ID_PATTERN.fullmatch(plate.tile_id)
        if match is None or (int(match.group(1)), int(match.group(2))) != (
            plate.row,
            plate.column,
        ):
            raise BambuMuralPackageError("mural tile ID does not match its row and column")
        if plate.tile_id in tile_ids:
            raise BambuMuralPackageError("mural package contains duplicate tile IDs")
        tile_ids.add(plate.tile_id)
        name = _require_name(plate.name, f"plates[{plate.build_plate_index}].name")
        if not name.startswith(f"{plate.build_plate_index:02d} - "):
            raise BambuMuralPackageError(
                "mural plate names must begin with their zero-padded build order"
            )
        if name in names:
            raise BambuMuralPackageError("mural plate names must be unique")
        names.add(name)
        origin_x = _finite_mm(plate.origin_x_mm, "origin_x_mm")
        origin_y = _finite_mm(plate.origin_y_mm, "origin_y_mm")
        meshes = tuple(
            _normalize_mesh(mesh, index, material_count) for index, mesh in enumerate(plate.meshes)
        )
        if not meshes:
            raise BambuMuralPackageError("every mural build plate must contain geometry")
        qualified = tuple(
            Mesh(
                name=f"{name} / {mesh.name}",
                vertices=mesh.vertices,
                triangles=mesh.triangles,
                material_index=mesh.material_index,
            )
            for mesh in meshes
        )
        mesh_ids = tuple(range(next_object_id, next_object_id + len(qualified)))
        assembly_object_id = next_object_id + len(qualified)
        next_object_id = assembly_object_id + 1
        bounds = _placed_bounds(qualified, origin_x, origin_y)
        _validate_placement(bounds, reserve, plate.build_plate_index)
        fingerprint = str(plate.source_geometry_fingerprint)
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise BambuMuralPackageError(
                "source geometry fingerprints must be lowercase SHA-256 values"
            )
        result.append(
            _NormalizedPlate(
                tile_id=plate.tile_id,
                row=plate.row,
                column=plate.column,
                build_plate_index=plate.build_plate_index,
                name=name,
                meshes=qualified,
                origin_x_mm=origin_x,
                origin_y_mm=origin_y,
                source_geometry_fingerprint=fingerprint,
                assembly_object_id=assembly_object_id,
                mesh_object_ids=mesh_ids,
                geometry_sha256=_geometry_sha256(qualified),
                placed_bounds_mm=bounds,
            )
        )
    return tuple(result)


def _finite_mm(value: float, field: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise BambuMuralPackageError(f"{field} must be finite")
    return round(number, 6)


def _placed_bounds(
    meshes: tuple[MeshLike, ...], origin_x: float, origin_y: float
) -> tuple[float, float, float, float, float, float]:
    vertices = [vertex for mesh in meshes for vertex in mesh.vertices]
    return (
        round(min(float(vertex[0]) for vertex in vertices) + origin_x, 6),
        round(min(float(vertex[1]) for vertex in vertices) + origin_y, 6),
        round(min(float(vertex[2]) for vertex in vertices), 6),
        round(max(float(vertex[0]) for vertex in vertices) + origin_x, 6),
        round(max(float(vertex[1]) for vertex in vertices) + origin_y, 6),
        round(max(float(vertex[2]) for vertex in vertices), 6),
    )


def _validate_placement(
    bounds: tuple[float, float, float, float, float, float],
    reserve: PrimeTowerReserve,
    index: int,
) -> None:
    min_x, min_y, min_z, max_x, max_y, _max_z = bounds
    if min_x < 0 or min_y < 0 or min_z < 0 or max_x > PLATE_WIDTH_MM or max_y > PLATE_HEIGHT_MM:
        raise BambuMuralPackageError(f"plate {index} geometry exceeds the P2S build plate")
    overlap_x = min(max_x, reserve.right_mm) - max(min_x, reserve.x_mm)
    overlap_y = min(max_y, reserve.top_mm) - max(min_y, reserve.y_mm)
    if overlap_x > 1e-6 and overlap_y > 1e-6:
        raise BambuMuralPackageError(
            f"plate {index} geometry overlaps the slicer prime-tower reserve"
        )


def _geometry_sha256(meshes: tuple[MeshLike, ...]) -> str:
    return hashlib.sha256(
        _json_bytes(
            [
                {
                    "material_index": mesh.material_index,
                    "triangles": mesh.triangles,
                    "vertices": mesh.vertices,
                }
                for mesh in meshes
            ]
        )
    ).hexdigest()


def _object_path(index: int, *, absolute: bool = False) -> str:
    path = f"3D/Objects/object_{index}.model"
    return f"/{path}" if absolute else path


def _mural_relationships(plate_count: int) -> bytes:
    root = ET.Element("Relationships", {"xmlns": RELATIONSHIP_NS})
    for index in range(1, plate_count + 1):
        ET.SubElement(
            root,
            "Relationship",
            {
                "Target": _object_path(index, absolute=True),
                "Id": f"rel-{index}",
                "Type": MODEL_RELATIONSHIP,
            },
        )
    return _xml_bytes(root)


def _mural_object_model(
    plate: _NormalizedPlate,
    materials: tuple[Material, ...],
    seed: str,
) -> bytes:
    del materials
    root = _model_root()
    resources = ET.SubElement(root, _qualified(CORE_NS, "resources"))
    for mesh_id, mesh in zip(plate.mesh_object_ids, plate.meshes):
        object_element = ET.SubElement(
            resources,
            _qualified(CORE_NS, "object"),
            {
                "id": str(mesh_id),
                _qualified(PRODUCTION_NS, "UUID"): _stable_uuid(seed, f"mesh:{mesh_id}"),
                "type": "model",
            },
        )
        mesh_element = ET.SubElement(object_element, _qualified(CORE_NS, "mesh"))
        vertices = ET.SubElement(mesh_element, _qualified(CORE_NS, "vertices"))
        for x, y, z in mesh.vertices:
            ET.SubElement(
                vertices,
                _qualified(CORE_NS, "vertex"),
                {"x": _format_number(x), "y": _format_number(y), "z": _format_number(z)},
            )
        triangles = ET.SubElement(mesh_element, _qualified(CORE_NS, "triangles"))
        for v1, v2, v3 in mesh.triangles:
            ET.SubElement(
                triangles,
                _qualified(CORE_NS, "triangle"),
                {
                    "v1": str(v1),
                    "v2": str(v2),
                    "v3": str(v3),
                },
            )
    ET.SubElement(root, _qualified(CORE_NS, "build"))
    return _xml_bytes(root)


def _mural_project_settings(
    project: BambuProject,
    reserve: PrimeTowerReserve,
    plate_count: int,
) -> bytes:
    payload = json.loads(_project_settings(project))
    payload.update(
        {
            "enable_prime_tower": "1",
            "prime_tower_width": _format_number(reserve.tower_width_mm),
            "wipe_tower_x": [_format_number(reserve.effective_tower_x_mm)] * plate_count,
            "wipe_tower_y": [_format_number(reserve.effective_tower_y_mm)] * plate_count,
            "image23mf_material_names": [material.name for material in project.materials],
        }
    )
    return _json_bytes(payload)


def _mural_root_model(plates: tuple[_NormalizedPlate, ...], seed: str) -> bytes:
    root = _model_root()
    resources = ET.SubElement(root, _qualified(CORE_NS, "resources"))
    columns = math.ceil(math.sqrt(len(plates)))
    build = ET.SubElement(
        root,
        _qualified(CORE_NS, "build"),
        {_qualified(PRODUCTION_NS, "UUID"): _stable_uuid(seed, "mural-build")},
    )
    for plate in plates:
        assembly = ET.SubElement(
            resources,
            _qualified(CORE_NS, "object"),
            {
                "id": str(plate.assembly_object_id),
                _qualified(PRODUCTION_NS, "UUID"): _stable_uuid(
                    seed, f"assembly:{plate.build_plate_index}"
                ),
                "type": "model",
            },
        )
        components = ET.SubElement(assembly, _qualified(CORE_NS, "components"))
        local_x = plate.origin_x_mm - PLATE_WIDTH_MM / 2
        local_y = plate.origin_y_mm - PLATE_HEIGHT_MM / 2
        for mesh_id in plate.mesh_object_ids:
            ET.SubElement(
                components,
                _qualified(CORE_NS, "component"),
                {
                    _qualified(PRODUCTION_NS, "path"): _object_path(
                        plate.build_plate_index,
                        absolute=True,
                    ),
                    "objectid": str(mesh_id),
                    _qualified(PRODUCTION_NS, "UUID"): _stable_uuid(
                        seed, f"component:{plate.build_plate_index}:{mesh_id}"
                    ),
                    "transform": _translation_3mf(local_x, local_y, 0),
                },
            )
        virtual_column = (plate.build_plate_index - 1) % columns
        virtual_row = (plate.build_plate_index - 1) // columns
        ET.SubElement(
            build,
            _qualified(CORE_NS, "item"),
            {
                "objectid": str(plate.assembly_object_id),
                _qualified(PRODUCTION_NS, "UUID"): _stable_uuid(
                    seed, f"build-item:{plate.build_plate_index}"
                ),
                "transform": (
                    "1 0 0 0 1 0 0 0 1 "
                    f"{_format_number(PLATE_WIDTH_MM / 2 + virtual_column * PLATE_GRID_STEP_MM)} "
                    f"{_format_number(PLATE_HEIGHT_MM / 2 - virtual_row * PLATE_GRID_STEP_MM)} 0"
                ),
                "printable": "1",
            },
        )
    return _xml_bytes(root)


def _translation_3mf(x: float, y: float, z: float) -> str:
    return f"1 0 0 0 1 0 0 0 1 {_format_number(x)} {_format_number(y)} {_format_number(z)}"


def _translation_4x4(x: float, y: float, z: float) -> str:
    return f"1 0 0 {_format_number(x)} 0 1 0 {_format_number(y)} 0 0 1 {_format_number(z)} 0 0 0 1"


def _mural_model_settings(
    plates: tuple[_NormalizedPlate, ...], materials: tuple[Material, ...]
) -> bytes:
    root = ET.Element("config")
    for plate in plates:
        object_element = ET.SubElement(root, "object", {"id": str(plate.assembly_object_id)})
        _metadata(object_element, "name", plate.name)
        _metadata(object_element, "extruder", 1)
        _metadata(
            object_element,
            "face_count",
            sum(len(mesh.triangles) for mesh in plate.meshes),
        )
        local_x = plate.origin_x_mm - PLATE_WIDTH_MM / 2
        local_y = plate.origin_y_mm - PLATE_HEIGHT_MM / 2
        for local_index, (mesh_id, mesh) in enumerate(zip(plate.mesh_object_ids, plate.meshes)):
            material = materials[mesh.material_index]
            part = ET.SubElement(
                object_element, "part", {"id": str(mesh_id), "subtype": "normal_part"}
            )
            _metadata(part, "name", mesh.name)
            _metadata(part, "matrix", _translation_4x4(local_x, local_y, 0))
            _metadata(part, "source_file", "image23mf-mural.3mf")
            _metadata(part, "source_object_id", local_index)
            _metadata(part, "source_volume_id", 0)
            _metadata(part, "source_offset_x", local_x)
            _metadata(part, "source_offset_y", local_y)
            _metadata(part, "source_offset_z", 0)
            _metadata(part, "extruder", material.extruder)
            _metadata(part, "image23mf_material_index", mesh.material_index)
            if material.material_id is not None:
                _metadata(part, "image23mf_material_id", material.material_id)
            if material.palette_color_id is not None:
                _metadata(part, "image23mf_palette_color_id", material.palette_color_id)
            if material.filament_id is not None:
                _metadata(part, "image23mf_filament_id", material.filament_id)
            ET.SubElement(
                part,
                "mesh_stat",
                {
                    "face_count": str(len(mesh.triangles)),
                    "edges_fixed": "0",
                    "degenerate_facets": "0",
                    "facets_removed": "0",
                    "facets_reversed": "0",
                    "backwards_edges": "0",
                },
            )
    for plate in plates:
        plate_element = ET.SubElement(root, "plate")
        _metadata(plate_element, "plater_id", plate.build_plate_index)
        _metadata(plate_element, "plater_name", plate.name)
        _metadata(plate_element, "locked", "false")
        instance = ET.SubElement(plate_element, "model_instance")
        _metadata(instance, "object_id", plate.assembly_object_id)
        _metadata(instance, "instance_id", 0)
        _metadata(instance, "identify_id", plate.assembly_object_id)
    return _xml_bytes(root)


def _render_plate_preview(
    plate: _NormalizedPlate,
    materials: tuple[Material, ...],
    reserve: PrimeTowerReserve,
) -> bytes:
    size = 512
    image = Image.new("RGBA", (size, size), (18, 22, 27, 255))
    draw = ImageDraw.Draw(image)
    scale = size / PLATE_WIDTH_MM

    def point(vertex) -> tuple[float, float]:
        x = (float(vertex[0]) + plate.origin_x_mm) * scale
        y = size - (float(vertex[1]) + plate.origin_y_mm) * scale
        return x, y

    triangles = []
    for mesh in plate.meshes:
        color = materials[mesh.material_index].color[:7]
        for triangle in mesh.triangles:
            vertices = tuple(mesh.vertices[index] for index in triangle)
            triangles.append((sum(float(vertex[2]) for vertex in vertices) / 3, color, vertices))
    for _z, color, vertices in sorted(triangles, key=lambda item: item[0]):
        points = [point(vertex) for vertex in vertices]
        if abs(_polygon_area(points)) > 0.01:
            draw.polygon(points, fill=color)
    reserve_box = (
        reserve.x_mm * scale,
        size - reserve.top_mm * scale,
        reserve.right_mm * scale,
        size - reserve.y_mm * scale,
    )
    draw.rectangle(reserve_box, outline=(110, 123, 134, 255), width=2)
    draw.text((8, 8), plate.name, fill=(238, 242, 245, 255))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _polygon_area(points: list[tuple[float, float]]) -> float:
    return (
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(points, (*points[1:], points[0]))
        )
        / 2
    )


def _content_types() -> bytes:
    root = ET.Element("Types", {"xmlns": CONTENT_TYPES_NS})
    for extension, content_type in (
        ("rels", RELATIONSHIP_CONTENT_TYPE),
        ("model", MODEL_CONTENT_TYPE),
        ("json", JSON_CONTENT_TYPE),
        ("png", PNG_CONTENT_TYPE),
    ):
        ET.SubElement(root, "Default", {"Extension": extension, "ContentType": content_type})
    return _xml_bytes(root)


def _mural_manifest(
    name: str,
    settings: BambuProjectSettings,
    materials: tuple[Material, ...],
    reserve: PrimeTowerReserve,
    evidence: tuple[MuralPlateEvidence, ...],
) -> dict:
    plates = []
    for item in evidence:
        plates.append(
            {
                "tile_id": item.tile_id,
                "row": item.row,
                "column": item.column,
                "build_plate_index": item.build_plate_index,
                "name": item.name,
                "assembly_object_id": item.assembly_object_id,
                "mesh_object_ids": list(item.mesh_object_ids),
                "source_geometry_fingerprint": item.source_geometry_fingerprint,
                "geometry_sha256": item.geometry_sha256,
                "placed_bounds_mm": list(item.placed_bounds_mm),
                "origin_x_mm": item.origin_x_mm,
                "origin_y_mm": item.origin_y_mm,
                "preview_path": item.preview_path,
                "preview_sha256": item.preview_sha256,
            }
        )
    return {
        "schema_version": MURAL_PACKAGE_SCHEMA_VERSION,
        "project_name": name,
        "printer_model": settings.printer_model,
        "nozzle_diameter_mm": settings.nozzle_diameter,
        "layer_height_mm": settings.layer_height,
        "bed_type": settings.bed_type,
        "materials_sha256": hashlib.sha256(
            _json_bytes(_material_identities(materials))
        ).hexdigest(),
        "prime_tower": {
            "policy": "enabled_slicer_generated_tower",
            "inserted_geometry": False,
            "x_mm": reserve.x_mm,
            "y_mm": reserve.y_mm,
            "width_mm": reserve.width_mm,
            "height_mm": reserve.height_mm,
            "tower_width_mm": reserve.tower_width_mm,
            **(
                {"tower_x_mm": reserve.effective_tower_x_mm}
                if reserve.tower_x_mm is not None
                else {}
            ),
            **(
                {"tower_y_mm": reserve.effective_tower_y_mm}
                if reserve.tower_y_mm is not None
                else {}
            ),
        },
        "plates": plates,
    }


def _material_identities(materials: tuple[Material, ...]) -> list[dict]:
    """Hash physical slot identity independently of an installed profile's display name."""
    return [
        {
            "name": material.name,
            "color": material.color,
            "extruder": material.extruder,
            "filament_type": material.filament_type,
            "material_id": material.material_id,
            "palette_color_id": material.palette_color_id,
            "filament_id": material.filament_id,
        }
        for material in materials
    ]


def _member_order(plate_count: int) -> tuple[str, ...]:
    return (
        "[Content_Types].xml",
        ROOT_RELS_PATH,
        ROOT_MODEL_PATH,
        MODEL_RELS_PATH,
        *(_object_path(index) for index in range(1, plate_count + 1)),
        MODEL_SETTINGS_PATH,
        PROJECT_SETTINGS_PATH,
        PROFILE_CONTRACT_PATH,
        MURAL_MANIFEST_PATH,
        *(_preview_path(index) for index in range(1, plate_count + 1)),
    )


def _preview_path(index: int) -> str:
    return f"Metadata/plate_{index}.png"


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _open_source(source: InputSource):
    if isinstance(source, (bytes, bytearray, memoryview)):
        buffer = io.BytesIO(bytes(source))
        return zipfile.ZipFile(buffer), buffer
    if hasattr(source, "read"):
        return zipfile.ZipFile(source), None
    return zipfile.ZipFile(Path(source)), None


def _read_member(archive: zipfile.ZipFile, member: str) -> bytes:
    try:
        return archive.read(member)
    except KeyError as error:
        raise InvalidBambuMural3MFError(f"mural package is missing {member}") from error


def _manifest_plates(manifest: dict) -> tuple[dict, ...]:
    if manifest.get("schema_version") != MURAL_PACKAGE_SCHEMA_VERSION:
        raise InvalidBambuMural3MFError("mural package uses an unsupported schema version")
    values = manifest.get("plates")
    if not isinstance(values, list) or not values:
        raise InvalidBambuMural3MFError("mural manifest must contain build plates")
    if [item.get("build_plate_index") for item in values] != list(range(1, len(values) + 1)):
        raise InvalidBambuMural3MFError("mural manifest plate order is not canonical")
    return tuple(values)


def _reserve_from_manifest(manifest: dict) -> PrimeTowerReserve:
    payload = manifest.get("prime_tower")
    if not isinstance(payload, dict):
        raise InvalidBambuMural3MFError("mural manifest lacks prime-tower reserve evidence")
    if (
        payload.get("policy") != "enabled_slicer_generated_tower"
        or payload.get("inserted_geometry") is not False
    ):
        raise InvalidBambuMural3MFError("mural prime tower must be reserved, not inserted")
    return _normalize_reserve(
        PrimeTowerReserve(
            x_mm=payload["x_mm"],
            y_mm=payload["y_mm"],
            width_mm=payload["width_mm"],
            height_mm=payload["height_mm"],
            tower_width_mm=payload["tower_width_mm"],
            tower_x_mm=payload.get("tower_x_mm"),
            tower_y_mm=payload.get("tower_y_mm"),
        )
    )


def _validate_content_types(data: bytes) -> None:
    root = ET.fromstring(data)
    if root.tag != _qualified(CONTENT_TYPES_NS, "Types"):
        raise InvalidBambuMural3MFError("mural content types use the wrong namespace")
    values = {item.get("Extension"): item.get("ContentType") for item in root}
    expected = {
        "rels": RELATIONSHIP_CONTENT_TYPE,
        "model": MODEL_CONTENT_TYPE,
        "json": JSON_CONTENT_TYPE,
        "png": PNG_CONTENT_TYPE,
    }
    if values != expected:
        raise InvalidBambuMural3MFError("mural package content types are incomplete")


def _validate_relationships(data: bytes, target: str, member: str) -> None:
    root = ET.fromstring(data)
    if root.tag != _qualified(RELATIONSHIP_NS, "Relationships"):
        raise InvalidBambuMural3MFError(f"{member} uses the wrong namespace")
    children = list(root)
    expected = {"Target": target, "Id": "rel-1", "Type": MODEL_RELATIONSHIP}
    if len(children) != 1 or children[0].attrib != expected:
        raise InvalidBambuMural3MFError(f"{member} does not resolve canonical geometry")


def _validate_mural_relationships(data: bytes, plate_count: int) -> None:
    root = ET.fromstring(data)
    if root.tag != _qualified(RELATIONSHIP_NS, "Relationships"):
        raise InvalidBambuMural3MFError(f"{MODEL_RELS_PATH} uses the wrong namespace")
    expected = [
        {
            "Target": _object_path(index, absolute=True),
            "Id": f"rel-{index}",
            "Type": MODEL_RELATIONSHIP,
        }
        for index in range(1, plate_count + 1)
    ]
    if [item.attrib for item in root] != expected:
        raise InvalidBambuMural3MFError(
            f"{MODEL_RELS_PATH} does not resolve every canonical tile object"
        )


def _required_attribute(element: ET.Element, key: str, path: str) -> str:
    value = element.get(key)
    if value is None:
        raise InvalidBambuMural3MFError(f"{path} is missing required {key} metadata")
    return value


def _parse_mural_object_model(
    data: bytes,
    *,
    path: str,
    expected_mesh_ids: tuple[int, ...],
    seed: str,
) -> tuple[dict[int, Mesh], set[uuid.UUID]]:
    root = ET.fromstring(data)
    if root.tag != _qualified(CORE_NS, "model"):
        raise InvalidBambuMural3MFError(f"{path} uses the wrong core namespace")
    resources = root.find(_qualified(CORE_NS, "resources"))
    if resources is None:
        raise InvalidBambuMural3MFError(f"{path} does not contain resources")
    if resources.find(_qualified(CORE_NS, "basematerials")) is not None:
        raise InvalidBambuMural3MFError(f"{path} must use native per-volume extruder metadata")
    objects = resources.findall(_qualified(CORE_NS, "object"))
    actual_ids = tuple(int(_required_attribute(item, "id", path)) for item in objects)
    if actual_ids != expected_mesh_ids:
        raise InvalidBambuMural3MFError(f"{path} part IDs conflict with its tile assignment")
    meshes: dict[int, Mesh] = {}
    for mesh_id, object_element in zip(actual_ids, objects):
        expected_uuid = _stable_uuid(seed, f"mesh:{mesh_id}")
        if object_element.get(_qualified(PRODUCTION_NS, "UUID")) != expected_uuid:
            raise InvalidBambuMural3MFError(f"{path} mesh identity is not canonical")
        if object_element.get("pid") is not None or object_element.get("pindex") is not None:
            raise InvalidBambuMural3MFError(f"{path} contains redundant object material metadata")
        mesh_element = object_element.find(_qualified(CORE_NS, "mesh"))
        if mesh_element is None:
            raise InvalidBambuMural3MFError(f"{path} part does not contain a mesh")
        vertices_element = mesh_element.find(_qualified(CORE_NS, "vertices"))
        triangles_element = mesh_element.find(_qualified(CORE_NS, "triangles"))
        if vertices_element is None or triangles_element is None:
            raise InvalidBambuMural3MFError(f"{path} mesh is incomplete")
        vertices = tuple(
            tuple(float(_required_attribute(vertex, axis, path)) for axis in "xyz")
            for vertex in vertices_element
        )
        triangles = []
        for triangle in triangles_element:
            values = tuple(
                int(_required_attribute(triangle, axis, path)) for axis in ("v1", "v2", "v3")
            )
            if any(triangle.get(key) is not None for key in ("pid", "p1", "p2", "p3")):
                raise InvalidBambuMural3MFError(
                    f"{path} contains redundant triangle material metadata"
                )
            triangles.append(values)
        meshes[mesh_id] = _normalize_mesh(
            Mesh("pending", vertices, tuple(triangles), 0),
            mesh_id - 1,
            1,
        )
    build = root.find(_qualified(CORE_NS, "build"))
    if build is None or list(build) or build.attrib:
        raise InvalidBambuMural3MFError(f"{path} child build metadata is not canonical")
    identifiers = {
        uuid.UUID(_required_attribute(item, _qualified(PRODUCTION_NS, "UUID"), path))
        for item in objects
    }
    if len(identifiers) != len(objects):
        raise InvalidBambuMural3MFError(f"{path} contains duplicate production UUIDs")
    return meshes, identifiers


def _parse_model_settings(
    data: bytes,
    plate_payloads: tuple[dict, ...],
    meshes: dict[int, Mesh],
) -> dict:
    root = ET.fromstring(data)
    if root.tag != "config":
        raise InvalidBambuMural3MFError("mural model settings root must be config")
    objects = root.findall("object")
    plates = root.findall("plate")
    if len(objects) != len(plate_payloads) or len(plates) != len(plate_payloads):
        raise InvalidBambuMural3MFError("mural settings must describe every build plate once")
    mesh_names: dict[int, str] = {}
    mesh_material_indices: dict[int, int] = {}
    mesh_ids_by_plate: list[tuple[int, ...]] = []
    seen_assemblies: set[int] = set()
    for payload, object_element, plate_element in zip(plate_payloads, objects, plates):
        assembly_id = int(payload["assembly_object_id"])
        if int(object_element.get("id", "-1")) != assembly_id or assembly_id in seen_assemblies:
            raise InvalidBambuMural3MFError("mural settings reuse or misassign an assembly")
        seen_assemblies.add(assembly_id)
        object_meta = _metadata_values(object_element)
        if object_meta.get("name") != payload["name"]:
            raise InvalidBambuMural3MFError("mural assembly name conflicts with its plate")
        expected_mesh_ids = tuple(int(value) for value in payload["mesh_object_ids"])
        parts = object_element.findall("part")
        actual_mesh_ids = tuple(int(part.get("id", "-1")) for part in parts)
        if actual_mesh_ids != expected_mesh_ids:
            raise InvalidBambuMural3MFError("mural assembly parts conflict with its tile")
        mesh_ids_by_plate.append(actual_mesh_ids)
        for part in parts:
            mesh_id = int(part.get("id", "-1"))
            metadata = _metadata_values(part)
            name = metadata.get("name")
            if not name or mesh_id in mesh_names:
                raise InvalidBambuMural3MFError("mural package reuses a mesh object")
            material_index = int(metadata.get("image23mf_material_index", "-1"))
            if mesh_id not in meshes or material_index < 0:
                raise InvalidBambuMural3MFError("mural part material mapping is stale")
            mesh_names[mesh_id] = name
            mesh_material_indices[mesh_id] = material_index
        plate_meta = _metadata_values(plate_element)
        instances = plate_element.findall("model_instance")
        if (
            plate_meta.get("plater_id") != str(payload["build_plate_index"])
            or plate_meta.get("plater_name") != payload["name"]
            or len(instances) != 1
            or _metadata_values(instances[0]).get("object_id") != str(assembly_id)
        ):
            raise InvalidBambuMural3MFError("mural plate assignment is missing or duplicated")
    if set(mesh_names) != set(meshes):
        raise InvalidBambuMural3MFError("mural package has unassigned mesh objects")
    return {
        "mesh_names": mesh_names,
        "mesh_material_indices": mesh_material_indices,
        "mesh_ids_by_plate": tuple(mesh_ids_by_plate),
    }


def _metadata_values(parent: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in parent.findall("metadata"):
        key = item.get("key")
        value = item.get("value")
        if key is None or value is None or key in result:
            raise InvalidBambuMural3MFError("mural settings contain malformed metadata")
        result[key] = value
    return result


def _parse_mural_root(
    data: bytes,
    plate_payloads: tuple[dict, ...],
    mesh_ids_by_plate: tuple[tuple[int, ...], ...],
    *,
    seed: str,
) -> set[uuid.UUID]:
    root = ET.fromstring(data)
    if root.tag != _qualified(CORE_NS, "model"):
        raise InvalidBambuMural3MFError("mural root model uses the wrong namespace")
    resources = root.find(_qualified(CORE_NS, "resources"))
    build = root.find(_qualified(CORE_NS, "build"))
    if resources is None or build is None:
        raise InvalidBambuMural3MFError("mural root model lacks resources or build")
    assemblies = resources.findall(_qualified(CORE_NS, "object"))
    items = build.findall(_qualified(CORE_NS, "item"))
    if len(assemblies) != len(plate_payloads) or len(items) != len(plate_payloads):
        raise InvalidBambuMural3MFError("mural root must contain one assembly per plate")
    if build.get(_qualified(PRODUCTION_NS, "UUID")) != _stable_uuid(seed, "mural-build"):
        raise InvalidBambuMural3MFError("mural build identity is not canonical")
    columns = math.ceil(math.sqrt(len(plate_payloads)))
    for payload, mesh_ids, assembly, item in zip(
        plate_payloads, mesh_ids_by_plate, assemblies, items
    ):
        plate_index = int(payload["build_plate_index"])
        assembly_id = str(payload["assembly_object_id"])
        if assembly.get("id") != assembly_id or item.get("objectid") != assembly_id:
            raise InvalidBambuMural3MFError("mural build item references the wrong assembly")
        if assembly.get(_qualified(PRODUCTION_NS, "UUID")) != _stable_uuid(
            seed, f"assembly:{plate_index}"
        ) or item.get(_qualified(PRODUCTION_NS, "UUID")) != _stable_uuid(
            seed, f"build-item:{plate_index}"
        ):
            raise InvalidBambuMural3MFError("mural plate identity is not canonical")
        components = assembly.findall(
            f"{_qualified(CORE_NS, 'components')}/{_qualified(CORE_NS, 'component')}"
        )
        if tuple(int(component.get("objectid", "-1")) for component in components) != mesh_ids:
            raise InvalidBambuMural3MFError("mural assembly geometry does not match its plate")
        expected_path = _object_path(plate_index, absolute=True)
        if any(
            component.get(_qualified(PRODUCTION_NS, "path")) != expected_path
            for component in components
        ):
            raise InvalidBambuMural3MFError("mural assembly references a noncanonical object file")
        for mesh_id, component in zip(mesh_ids, components):
            if component.get(_qualified(PRODUCTION_NS, "UUID")) != _stable_uuid(
                seed, f"component:{plate_index}:{mesh_id}"
            ):
                raise InvalidBambuMural3MFError("mural component identity is not canonical")
            expected_component_transform = _translation_3mf(
                float(payload["origin_x_mm"]) - PLATE_WIDTH_MM / 2,
                float(payload["origin_y_mm"]) - PLATE_HEIGHT_MM / 2,
                0,
            )
            if component.get("transform") != expected_component_transform:
                raise InvalidBambuMural3MFError(
                    "mural component transform conflicts with plate placement"
                )
        values = [float(value) for value in item.get("transform", "").split()]
        if len(values) != 12 or values[:9] != [float(value) for value in IDENTITY_3MF.split()[:9]]:
            raise InvalidBambuMural3MFError("mural build item uses an unsupported transform")
        virtual_column = (plate_index - 1) % columns
        virtual_row = (plate_index - 1) // columns
        expected_x = PLATE_WIDTH_MM / 2 + virtual_column * PLATE_GRID_STEP_MM
        expected_y = PLATE_HEIGHT_MM / 2 - virtual_row * PLATE_GRID_STEP_MM
        if not math.isclose(values[9], expected_x, abs_tol=1e-6) or not math.isclose(
            values[10], expected_y, abs_tol=1e-6
        ):
            raise InvalidBambuMural3MFError("mural build transform conflicts with plate placement")
    return _production_uuids(root, ROOT_MODEL_PATH)


def _parse_materials_and_settings(
    material_values: list[tuple[str, str]],
    payload: dict,
    mesh_material_indices: dict[int, int],
) -> tuple[tuple[Material, ...], BambuProjectSettings]:
    count = len(material_values)
    arrays = {
        key: payload.get(key)
        for key in (
            "filament_colour",
            "filament_type",
            "filament_settings_id",
            "image23mf_extruders",
            "image23mf_material_ids",
            "image23mf_palette_color_ids",
            "image23mf_filament_ids",
            "image23mf_part_material_indices",
        )
    }
    if any(not isinstance(value, list) for value in arrays.values()):
        raise InvalidBambuMural3MFError("mural project settings lack filament mappings")
    if any(len(arrays[key]) != count for key in arrays if key != "image23mf_part_material_indices"):
        raise InvalidBambuMural3MFError("mural filament mappings do not cover every material")
    expected_part_indices = [
        mesh_material_indices[index] for index in sorted(mesh_material_indices)
    ]
    if arrays["image23mf_part_material_indices"] != expected_part_indices:
        raise InvalidBambuMural3MFError("mural project part mapping conflicts with geometry")
    materials = tuple(
        Material(
            name=name,
            color=color,
            extruder=int(arrays["image23mf_extruders"][index]),
            filament_type=str(arrays["filament_type"][index]),
            preset=str(arrays["filament_settings_id"][index]),
            material_id=arrays["image23mf_material_ids"][index],
            palette_color_id=arrays["image23mf_palette_color_ids"][index],
            filament_id=arrays["image23mf_filament_ids"][index],
        )
        for index, (name, color) in enumerate(material_values)
    )
    if arrays["filament_colour"] != [material.color for material in materials]:
        raise InvalidBambuMural3MFError("mural material colors conflict with project settings")
    settings = BambuProjectSettings(
        printer_model=str(payload["printer_model"]),
        nozzle_diameter=float(payload["nozzle_diameter"][0]),
        layer_height=float(payload["layer_height"]),
        bed_type=str(payload["curr_bed_type"]),
        plate_center_x=0,
        plate_center_y=0,
    )
    return materials, settings


def _verify_png(payload: bytes, plate_index: int) -> None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
            if image.format != "PNG":
                raise InvalidBambuMural3MFError(f"plate {plate_index} preview is not PNG")
    except OSError as error:
        raise InvalidBambuMural3MFError(f"plate {plate_index} preview is invalid") from error
