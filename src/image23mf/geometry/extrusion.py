"""Deterministic, layer-aware extrusion of canonical artwork islands.

The module consumes only validated Geometry IR v1 public models.  It turns every present
island into one watertight positive-volume part, records absent palette materials without
inventing empty geometry, and can assemble the result with an independently generated
structural base.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Optional, Union

import numpy as np
import triangle as triangle_lib
from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.geometry.model import (
    COORDINATE_PRECISION_MM,
    MESH_TOLERANCE_MM,
    TOPOLOGY_TOLERANCE_MM,
    CapabilityState,
    CapabilityStatus,
    Contour2D,
    GeometryCapabilities,
    GeometryDocument,
    Island2D,
    LineSegment,
    Mesh,
    Part,
    Path2D,
    Point2,
    Point3,
    SharedEdge,
    SourceLabel,
    Triangle,
)
from image23mf.geometry.parallel import process_map
from image23mf.geometry.spatial import BoundsIndex

LAYER_ALIGNMENT_TOLERANCE_MM = 0.0001
CORNER_RELIEF_MM = MESH_TOLERANCE_MM * 4


class ArtworkExtrusionError(ValueError):
    """Raised when canonical regions cannot produce the requested printable solids."""


class FlushInlayStrategy(BaseModel):
    """All color regions form one flush colored layer above the structural base."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["flush_inlay"] = "flush_inlay"
    base_top_z_mm: float = Field(gt=0)
    layer_height_mm: float = Field(gt=0)
    artwork_layers: int = Field(default=2, ge=1, le=1000)

    @model_validator(mode="after")
    def physical_heights_are_layer_aligned(self) -> FlushInlayStrategy:
        _validate_strategy_heights(self.base_top_z_mm, self.layer_height_mm)
        _validate_extrusion_height(self.artwork_layers * self.layer_height_mm)
        return self


class ShallowRaisedStrategy(BaseModel):
    """Background remains flush while artwork/support labels receive extra whole layers."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["shallow_raised"] = "shallow_raised"
    base_top_z_mm: float = Field(gt=0)
    layer_height_mm: float = Field(gt=0)
    foundation_layers: int = Field(default=1, ge=1, le=1000)
    raised_layers: int = Field(default=1, ge=1, le=1000)

    @model_validator(mode="after")
    def physical_heights_are_layer_aligned(self) -> ShallowRaisedStrategy:
        _validate_strategy_heights(self.base_top_z_mm, self.layer_height_mm)
        _validate_extrusion_height(self.foundation_layers * self.layer_height_mm)
        _validate_extrusion_height(
            (self.foundation_layers + self.raised_layers) * self.layer_height_mm
        )
        return self


ArtworkZStrategy = Union[FlushInlayStrategy, ShallowRaisedStrategy]


@dataclass(frozen=True)
class ArtworkPartEvidence:
    island_id: str
    source_label_id: str
    material_id: str
    mesh_id: str
    part_id: str
    layer_count: int
    bottom_z_mm: float
    top_z_mm: float


@dataclass(frozen=True)
class MaterialCoverage:
    """Explicit present/absent semantics for every palette material in the document."""

    material_id: str
    palette_color_id: str
    present: bool
    island_ids: tuple[str, ...]
    mesh_ids: tuple[str, ...]
    part_ids: tuple[str, ...]


@dataclass(frozen=True)
class SharedInterfaceEvidence:
    """An intentional zero-volume contact plane between adjacent material solids."""

    shared_edge_id: str
    adjacent_island_ids: tuple[str, str]
    owner_island_id: str
    bottom_z_mm: float
    common_top_z_mm: float


@dataclass(frozen=True)
class ArtworkExtrusionResult:
    source_geometry_fingerprint: str
    strategy: Literal["flush_inlay", "shallow_raised"]
    contact_z_mm: float
    layer_height_mm: float
    meshes: tuple[Mesh, ...]
    parts: tuple[Part, ...]
    part_evidence: tuple[ArtworkPartEvidence, ...]
    material_coverage: tuple[MaterialCoverage, ...]
    shared_interfaces: tuple[SharedInterfaceEvidence, ...]

    def fingerprint(self) -> str:
        payload = {
            "contact_z_mm": self.contact_z_mm,
            "layer_height_mm": self.layer_height_mm,
            "material_coverage": [item.__dict__ for item in self.material_coverage],
            "meshes": [item.model_dump(mode="json") for item in self.meshes],
            "part_evidence": [item.__dict__ for item in self.part_evidence],
            "parts": [item.model_dump(mode="json") for item in self.parts],
            "shared_interfaces": [item.__dict__ for item in self.shared_interfaces],
            "source_geometry_fingerprint": self.source_geometry_fingerprint,
            "strategy": self.strategy,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class MeshAssemblyResult:
    document: GeometryDocument
    mesh_artifact_sha256: str
    artwork_extrusion_sha256: str


def extrude_artwork_regions(
    document: GeometryDocument,
    strategy: ArtworkZStrategy,
    *,
    max_workers: int = 1,
    check_canceled: Optional[Callable[[], None]] = None,
) -> ArtworkExtrusionResult:
    """Extrude every canonical island into one exact-provenance printable part.

    The input must be the topology-stage document, before any mesh entities have been
    attached.  Shared-edge endpoints are inserted into both adjacent rings so boundary
    junctions use identical segmentation even when one source contour subdivides an edge.
    """

    if document.meshes or document.parts:
        raise ArtworkExtrusionError("artwork extrusion requires a topology document without meshes")
    if document.capabilities.topology.status != CapabilityStatus.AVAILABLE:
        raise ArtworkExtrusionError("artwork extrusion requires available topology evidence")
    if document.capabilities.mesh.status == CapabilityStatus.AVAILABLE:
        raise ArtworkExtrusionError("artwork extrusion cannot replace an available mesh artifact")

    paths = {item.id: item for item in document.paths}
    contours = {item.id: item for item in document.contours}
    labels = {item.id: item for item in document.source_labels}
    corner_touch_points = _corner_touch_points(document, paths=paths, contours=contours)
    shared_by_island: dict[str, list[SharedEdge]] = {item.id: [] for item in document.islands}
    for edge in document.shared_edges:
        for island_id in edge.adjacent_island_ids:
            shared_by_island[island_id].append(edge)

    tasks = [
        _ExtrusionTask(
            island=island,
            label=labels[island.source_label_id],
            outer=paths[contours[island.exterior_contour_id].path_id],
            holes=tuple(paths[contours[hole_id].path_id] for hole_id in island.hole_contour_ids),
            shared_edges=shared_by_island[island.id],
            corner_touch_points=corner_touch_points,
            strategy=strategy,
        )
        for island in document.islands
    ]
    # Start the largest pieces first, but restore the canonical island order afterward.
    ordered = sorted(
        enumerate(tasks),
        key=lambda item: (
            -(len(item[1].outer.segments) + sum(len(path.segments) for path in item[1].holes))
        ),
    )
    completed = process_map(
        _extrude_island,
        [task for _, task in ordered],
        max_workers=max_workers,
        check_canceled=check_canceled,
    )
    generated = [
        result
        for _, result in sorted(
            zip((index for index, _ in ordered), completed), key=lambda item: item[0]
        )
    ]

    by_island = {item[0].id: item for item in generated}
    evidence = tuple(
        ArtworkPartEvidence(
            island_id=island.id,
            source_label_id=island.source_label_id,
            material_id=island.material_id,
            mesh_id=mesh.id,
            part_id=part.id,
            layer_count=layer_count,
            bottom_z_mm=bottom_z,
            top_z_mm=top_z,
        )
        for island, _label, mesh, part, layer_count, bottom_z, top_z in generated
    )
    coverage = []
    for material in document.materials:
        matches = [item for item in generated if item[0].material_id == material.id]
        coverage.append(
            MaterialCoverage(
                material_id=material.id,
                palette_color_id=material.palette_color_id,
                present=bool(matches),
                island_ids=tuple(item[0].id for item in matches),
                mesh_ids=tuple(item[2].id for item in matches),
                part_ids=tuple(item[3].id for item in matches),
            )
        )
    interfaces = tuple(
        SharedInterfaceEvidence(
            shared_edge_id=edge.id,
            adjacent_island_ids=edge.adjacent_island_ids,
            owner_island_id=edge.owner_island_id,
            bottom_z_mm=_canonical_mm(strategy.base_top_z_mm),
            common_top_z_mm=min(
                by_island[edge.adjacent_island_ids[0]][6],
                by_island[edge.adjacent_island_ids[1]][6],
            ),
        )
        for edge in document.shared_edges
    )
    _validate_shared_interface_vertices(
        document.shared_edges,
        by_island,
        corner_touch_points=corner_touch_points,
    )
    return ArtworkExtrusionResult(
        source_geometry_fingerprint=document.fingerprint(),
        strategy=strategy.kind,
        contact_z_mm=_canonical_mm(strategy.base_top_z_mm),
        layer_height_mm=_canonical_mm(strategy.layer_height_mm),
        meshes=tuple(sorted((item[2] for item in generated), key=lambda item: item.id)),
        parts=tuple(sorted((item[3] for item in generated), key=lambda item: item.id)),
        part_evidence=evidence,
        material_coverage=tuple(coverage),
        shared_interfaces=interfaces,
    )


@dataclass(frozen=True)
class _ExtrusionTask:
    island: Island2D
    label: SourceLabel
    outer: Path2D
    holes: tuple[Path2D, ...]
    shared_edges: list[SharedEdge]
    corner_touch_points: frozenset[tuple[float, float]]
    strategy: ArtworkZStrategy


def _extrude_island(
    task: _ExtrusionTask,
) -> tuple[Island2D, SourceLabel, Mesh, Part, int, float, float]:
    island, label, strategy = task.island, task.label, task.strategy
    shared_edges, corner_touch_points = task.shared_edges, task.corner_touch_points
    layer_count = _layers_for_label(strategy, label)
    bottom_z = _canonical_mm(strategy.base_top_z_mm)
    top_z = _canonical_mm(bottom_z + layer_count * strategy.layer_height_mm)
    protected_points = {
        (point.x_mm, point.y_mm) for edge in shared_edges for point in (edge.start, edge.end)
    }
    outer, outer_relief = _relieve_corner_touches(
        _split_ring(_path_ring(task.outer), shared_edges),
        touch_points=corner_touch_points,
    )
    outer = _simplify_ring_for_mesh(
        outer,
        protected_points=protected_points | outer_relief,
    )
    holes = []
    for hole_path in task.holes:
        hole, hole_relief = _relieve_corner_touches(
            _split_ring(_path_ring(hole_path), shared_edges),
            touch_points=corner_touch_points,
        )
        holes.append(
            _simplify_ring_for_mesh(
                hole,
                protected_points=protected_points | hole_relief,
            )
        )
    try:
        mesh = _extrude_prism(outer, holes, bottom_z=bottom_z, top_z=top_z)
    except (ValueError, ArtworkExtrusionError) as error:
        raise ArtworkExtrusionError(f"island {island.id} could not be extruded: {error}") from error
    expected_volume = (_signed_area(outer) + sum(_signed_area(hole) for hole in holes)) * (
        top_z - bottom_z
    )
    actual_volume = _signed_mesh_volume(mesh)
    if expected_volume <= MESH_TOLERANCE_MM**3 or not math.isclose(
        actual_volume,
        expected_volume,
        rel_tol=1e-8,
        abs_tol=max(MESH_TOLERANCE_MM**3, expected_volume * 1e-10),
    ):
        raise ArtworkExtrusionError(
            f"island {island.id} mesh volume does not match its filled contour area"
        )
    part = Part.create(
        name=_part_name(label, island),
        role="support" if label.classification == "support" else "artwork",
        mesh_id=mesh.id,
        material_id=island.material_id,
        source_geometry_kind="island",
        source_geometry_id=island.id,
        source_label_id=island.source_label_id,
    )
    return island, label, mesh, part, layer_count, bottom_z, top_z


def assemble_mesh_document(
    document: GeometryDocument,
    *,
    base_mesh: Mesh,
    base_part: Part,
    artwork: ArtworkExtrusionResult,
) -> MeshAssemblyResult:
    """Attach independently generated base/artwork meshes and validate the full IR graph."""

    if artwork.source_geometry_fingerprint != document.fingerprint():
        raise ArtworkExtrusionError("artwork extrusion belongs to a different geometry document")
    if document.meshes or document.parts:
        raise ArtworkExtrusionError("mesh assembly requires a topology document without meshes")
    if base_part.role != "base" or base_part.mesh_id != base_mesh.id:
        raise ArtworkExtrusionError("base part must own the supplied base mesh")
    if base_part.source_geometry_id != document.base.id:
        raise ArtworkExtrusionError("base part provenance does not match the document base")
    if base_part.material_id not in {item.id for item in document.materials}:
        raise ArtworkExtrusionError("base part references a material outside the document")
    base_top = max(vertex.z_mm for vertex in base_mesh.vertices)
    if not math.isclose(
        base_top,
        artwork.contact_z_mm,
        rel_tol=0,
        abs_tol=COORDINATE_PRECISION_MM,
    ):
        raise ArtworkExtrusionError(
            "artwork bottom plane must exactly contact the structural base top plane"
        )
    if any(
        min(vertex.z_mm for vertex in mesh.vertices) != artwork.contact_z_mm
        for mesh in artwork.meshes
    ):
        raise ArtworkExtrusionError("every artwork mesh must begin on the common contact plane")

    meshes = tuple(sorted((base_mesh, *artwork.meshes), key=lambda item: item.id))
    parts = tuple(sorted((base_part, *artwork.parts), key=lambda item: item.id))
    if len({item.id for item in meshes}) != len(meshes):
        raise ArtworkExtrusionError("mesh assembly contains duplicate geometry")
    artifact_payload = {
        "meshes": [item.model_dump(mode="json") for item in meshes],
        "parts": [item.model_dump(mode="json") for item in parts],
    }
    mesh_artifact_sha256 = hashlib.sha256(
        json.dumps(
            artifact_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    capabilities = GeometryCapabilities(
        geometry_ir=document.capabilities.geometry_ir,
        vector_geometry=document.capabilities.vector_geometry,
        topology=document.capabilities.topology,
        mesh=CapabilityState(
            status=CapabilityStatus.AVAILABLE,
            artifact_sha256=mesh_artifact_sha256,
        ),
        package_3mf=CapabilityState(
            status=CapabilityStatus.NOT_REQUESTED,
            reason="Waiting for a deterministic 3MF package.",
        ),
        slicer_validation=CapabilityState(
            status=CapabilityStatus.NOT_REQUESTED,
            reason="Waiting for a packaged 3MF.",
        ),
        download=CapabilityState(
            status=CapabilityStatus.NOT_REQUESTED,
            reason="Waiting for slicer validation.",
        ),
    )
    payload = document.model_dump(mode="python")
    payload.update({"capabilities": capabilities, "meshes": meshes, "parts": parts})
    assembled = GeometryDocument.model_validate(payload)
    return MeshAssemblyResult(
        document=assembled,
        mesh_artifact_sha256=mesh_artifact_sha256,
        artwork_extrusion_sha256=artwork.fingerprint(),
    )


def _validate_strategy_heights(base_top_z_mm: float, layer_height_mm: float) -> None:
    _canonical_mm(base_top_z_mm)
    _canonical_mm(layer_height_mm)
    if layer_height_mm <= MESH_TOLERANCE_MM:
        raise ValueError("layer_height_mm must exceed mesh tolerance")
    layers = base_top_z_mm / layer_height_mm
    if not math.isclose(layers, round(layers), rel_tol=0, abs_tol=1e-6):
        raise ValueError("base_top_z_mm must be a whole multiple of layer_height_mm")


def _validate_extrusion_height(height_mm: float) -> None:
    _canonical_mm(height_mm)
    if height_mm <= MESH_TOLERANCE_MM:
        raise ValueError("artwork height must exceed mesh tolerance")


def _canonical_mm(value: float) -> float:
    number = float(value)
    normalized = round(number, 6)
    if not math.isfinite(number) or not math.isclose(
        number,
        normalized,
        rel_tol=0,
        abs_tol=5e-13,
    ):
        raise ValueError("physical heights must align to 0.000001 mm precision")
    return 0.0 if normalized == 0 else normalized


def _layers_for_label(strategy: ArtworkZStrategy, label: SourceLabel) -> int:
    if isinstance(strategy, FlushInlayStrategy):
        return strategy.artwork_layers
    return strategy.foundation_layers + (
        strategy.raised_layers if label.classification in {"artwork", "support"} else 0
    )


def _part_name(label: SourceLabel, island: Island2D) -> str:
    suffix = f" [{island.id[-8:]}]"
    return f"{label.name[: 120 - len(suffix)]}{suffix}"


def _path_ring(path: Path2D) -> list[Point2]:
    if path.purpose != "boundary" or not path.closed:
        raise ArtworkExtrusionError("island contours require closed boundary paths")
    if any(not isinstance(segment, LineSegment) for segment in path.segments):
        raise ArtworkExtrusionError("Geometry IR v1 island boundaries must be linear")
    return [path.start, *(segment.end for segment in path.segments[:-1])]


def _split_ring(ring: list[Point2], shared_edges: list[SharedEdge]) -> list[Point2]:
    split: list[Point2] = []
    shared_points = tuple(
        {
            (point.x_mm, point.y_mm): point
            for edge in shared_edges
            for point in (edge.start, edge.end)
        }.values()
    )
    points_by_x: dict[float, list[Point2]] = {}
    points_by_y: dict[float, list[Point2]] = {}
    for point in shared_points:
        points_by_x.setdefault(point.x_mm, []).append(point)
        points_by_y.setdefault(point.y_mm, []).append(point)
    for points in points_by_x.values():
        points.sort(key=lambda point: point.y_mm)
    for points in points_by_y.values():
        points.sort(key=lambda point: point.x_mm)
    for index, start in enumerate(ring):
        end = ring[(index + 1) % len(ring)]
        dx = end.x_mm - start.x_mm
        dy = end.y_mm - start.y_mm
        denominator = dx * dx + dy * dy
        additions: list[tuple[float, Point2]] = []
        if abs(dy) <= TOPOLOGY_TOLERANCE_MM:
            line = points_by_y.get(start.y_mm, [])
            positions = [point.x_mm for point in line]
            low, high = sorted((start.x_mm, end.x_mm))
            candidates = line[
                bisect_left(positions, low - TOPOLOGY_TOLERANCE_MM) : bisect_right(
                    positions, high + TOPOLOGY_TOLERANCE_MM
                )
            ]
        elif abs(dx) <= TOPOLOGY_TOLERANCE_MM:
            line = points_by_x.get(start.x_mm, [])
            positions = [point.y_mm for point in line]
            low, high = sorted((start.y_mm, end.y_mm))
            candidates = line[
                bisect_left(positions, low - TOPOLOGY_TOLERANCE_MM) : bisect_right(
                    positions, high + TOPOLOGY_TOLERANCE_MM
                )
            ]
        else:
            candidates = shared_points
        for point in candidates:
            if point in (start, end) or not _point_on_segment(point, start, end):
                continue
            ratio = ((point.x_mm - start.x_mm) * dx + (point.y_mm - start.y_mm) * dy) / denominator
            if TOPOLOGY_TOLERANCE_MM < ratio < 1 - TOPOLOGY_TOLERANCE_MM:
                additions.append((ratio, point))
        split.append(start)
        split.extend(point for _ratio, point in sorted(set(additions), key=lambda item: item[0]))
    if len({(point.x_mm, point.y_mm) for point in split}) != len(split):
        raise ArtworkExtrusionError("shared-edge splitting produced duplicate ring vertices")
    return split


def _corner_touch_points(
    document: GeometryDocument,
    *,
    paths: dict[str, Path2D],
    contours: dict[str, Contour2D],
) -> frozenset[tuple[float, float]]:
    """Find zero-area contacts between distinct holes of one filled island."""

    touching: set[tuple[float, float]] = set()
    for island in document.islands:
        owners: dict[tuple[float, float], set[str]] = {}
        contour_ids = (island.exterior_contour_id, *island.hole_contour_ids)
        for contour_id in contour_ids:
            contour = contours[contour_id]
            for point in _path_ring(paths[contour.path_id]):
                owners.setdefault((point.x_mm, point.y_mm), set()).add(contour_id)
        touching.update(point for point, owner_ids in owners.items() if len(owner_ids) > 1)
    return frozenset(touching)


def _relieve_corner_touches(
    ring: list[Point2],
    *,
    touch_points: frozenset[tuple[float, float]],
) -> tuple[list[Point2], set[tuple[float, float]]]:
    """Chamfer mathematically touching raster corners below printable resolution."""

    relieved: list[Point2] = []
    protected: set[tuple[float, float]] = set()
    for index, current in enumerate(ring):
        if (current.x_mm, current.y_mm) not in touch_points:
            relieved.append(current)
            continue
        previous = ring[index - 1]
        following = ring[(index + 1) % len(ring)]
        previous_distance = math.sqrt(_distance_squared(previous, current))
        following_distance = math.sqrt(_distance_squared(current, following))
        relief = min(CORNER_RELIEF_MM, previous_distance / 4, following_distance / 4)
        if relief <= MESH_TOLERANCE_MM:
            raise ArtworkExtrusionError(
                "corner-touch relief is below mesh tolerance; increase source feature size"
            )
        before = Point2(
            x_mm=current.x_mm + (previous.x_mm - current.x_mm) * relief / previous_distance,
            y_mm=current.y_mm + (previous.y_mm - current.y_mm) * relief / previous_distance,
        )
        after = Point2(
            x_mm=current.x_mm + (following.x_mm - current.x_mm) * relief / following_distance,
            y_mm=current.y_mm + (following.y_mm - current.y_mm) * relief / following_distance,
        )
        relieved.extend((before, after))
        protected.update(((before.x_mm, before.y_mm), (after.x_mm, after.y_mm)))
    return relieved, protected


def _simplify_ring_for_mesh(
    ring: list[Point2],
    *,
    protected_points: set[tuple[float, float]],
) -> list[Point2]:
    """Collapse unprintable convex spikes without moving exact shared interfaces.

    Fractional mural clipping can leave a contour vertex less than one mesh tolerance from
    the line between its neighbours.  Keeping that microscopic spike forces a triangle that
    the Geometry IR contract correctly rejects.  Removing the spike changes area only below
    the declared mesh tolerance and keeps every shared-edge endpoint intact.
    """

    simplified = list(ring)
    orientation = 1 if _signed_area(simplified) > 0 else -1
    changed = True
    while changed and len(simplified) > 3:
        changed = False
        for index, current in enumerate(simplified):
            if (current.x_mm, current.y_mm) in protected_points:
                continue
            previous = simplified[index - 1]
            following = simplified[(index + 1) % len(simplified)]
            longest_edge = max(
                math.sqrt(_distance_squared(previous, current)),
                math.sqrt(_distance_squared(current, following)),
                math.sqrt(_distance_squared(previous, following)),
            )
            if longest_edge <= MESH_TOLERANCE_MM:
                continue
            signed_turn = _cross(previous, current, following) * orientation
            if signed_turn < 0 or signed_turn / longest_edge > MESH_TOLERANCE_MM:
                continue
            candidate = simplified[:index] + simplified[index + 1 :]
            candidate_area = _signed_area(candidate)
            if candidate_area * orientation <= MESH_TOLERANCE_MM**2:
                continue
            simplified = candidate
            changed = True
            break
    return simplified


def _point_on_segment(point: Point2, start: Point2, end: Point2) -> bool:
    length = math.hypot(end.x_mm - start.x_mm, end.y_mm - start.y_mm)
    if length <= TOPOLOGY_TOLERANCE_MM:
        return False
    if abs(_cross(start, end, point)) > TOPOLOGY_TOLERANCE_MM * length:
        return False
    return (
        min(start.x_mm, end.x_mm) - TOPOLOGY_TOLERANCE_MM
        <= point.x_mm
        <= max(start.x_mm, end.x_mm) + TOPOLOGY_TOLERANCE_MM
        and min(start.y_mm, end.y_mm) - TOPOLOGY_TOLERANCE_MM
        <= point.y_mm
        <= max(start.y_mm, end.y_mm) + TOPOLOGY_TOLERANCE_MM
    )


def _extrude_prism(
    outer: list[Point2],
    holes: list[list[Point2]],
    *,
    bottom_z: float,
    top_z: float,
) -> Mesh:
    _validate_extrusion_height(top_z - bottom_z)
    if _signed_area(outer) <= 0 or any(_signed_area(hole) >= 0 for hole in holes):
        raise ArtworkExtrusionError("island contour winding is invalid")
    rings = [outer, *holes]
    flat = [point for ring in rings for point in ring]
    top_faces = _triangulate_polygon(outer, holes)
    count = len(flat)
    vertices = tuple(
        [Point3(x_mm=point.x_mm, y_mm=point.y_mm, z_mm=bottom_z) for point in flat]
        + [Point3(x_mm=point.x_mm, y_mm=point.y_mm, z_mm=top_z) for point in flat]
    )
    triangles: list[tuple[int, int, int]] = []
    for a, b, c in top_faces:
        triangles.append((a, c, b))
        triangles.append((a + count, b + count, c + count))
    offset = 0
    for ring in rings:
        for index in range(len(ring)):
            a = offset + index
            b = offset + (index + 1) % len(ring)
            triangles.extend(((a, b, b + count), (a, b + count, a + count)))
        offset += len(ring)
    return Mesh.create(
        vertices=vertices,
        triangles=tuple(Triangle(vertices=item) for item in triangles),
        watertight=True,
    )


def _triangulate_polygon(
    outer: list[Point2], holes: list[list[Point2]]
) -> list[tuple[int, int, int]]:
    _validate_triangulation_input(outer, holes)
    accelerated = _triangulate_with_triangle(outer, holes)
    if accelerated is not None:
        return accelerated
    if not holes:
        return _ear_clip_indexed([(point, index) for index, point in enumerate(outer)])
    polygon: list[tuple[Point2, int]] = [(point, index) for index, point in enumerate(outer)]
    next_index = len(outer)
    indexed_holes: list[list[tuple[Point2, int]]] = []
    for hole in holes:
        indexed_holes.append([(point, next_index + index) for index, point in enumerate(hole)])
        next_index += len(hole)
    indexed_holes.sort(key=_hole_bridge_order)
    for hole_index, hole in enumerate(indexed_holes):
        hole_position = max(
            range(len(hole)),
            key=lambda index: (hole[index][0].x_mm, -hole[index][0].y_mm),
        )
        hole_vertex = hole[hole_position]
        candidates = sorted(
            _unique_polygon_positions(polygon),
            key=lambda index: (
                _distance_squared(hole_vertex[0], polygon[index][0]),
                polygon[index][0].x_mm,
                polygon[index][0].y_mm,
            ),
        )
        outer_position = next(
            (
                index
                for index in candidates
                if _bridge_visible(
                    hole_vertex[0],
                    polygon[index][0],
                    polygon,
                    indexed_holes[hole_index:],
                    outer,
                    holes,
                )
            ),
            None,
        )
        if outer_position is None:
            raise ArtworkExtrusionError(
                f"hole {hole_index + 1} at ({hole_vertex[0].x_mm:g}, "
                f"{hole_vertex[0].y_mm:g}) cannot be bridged without crossing an edge; "
                "ensure holes are strictly inside, disjoint, and separated above topology tolerance"
            )
        rotated_hole = hole[hole_position:] + hole[:hole_position]
        outer_vertex = polygon[outer_position]
        polygon = (
            polygon[: outer_position + 1]
            + [hole_vertex]
            + rotated_hole[1:]
            + [hole_vertex, outer_vertex]
            + polygon[outer_position + 1 :]
        )
    return _ear_clip_indexed(polygon)


def _triangulate_with_triangle(
    outer: list[Point2], holes: list[list[Point2]]
) -> Optional[list[tuple[int, int, int]]]:
    """Use constrained Delaunay triangulation, retaining the audited Python fallback."""

    rings = [outer, *holes]
    vertices = np.asarray(
        [(point.x_mm, point.y_mm) for ring in rings for point in ring],
        dtype=np.float64,
    )
    segments: list[tuple[int, int]] = []
    offset = 0
    for ring in rings:
        segments.extend(
            (offset + index, offset + (index + 1) % len(ring)) for index in range(len(ring))
        )
        offset += len(ring)
    payload: dict[str, np.ndarray] = {
        "vertices": vertices,
        "segments": np.asarray(segments, dtype=np.int32),
    }
    if holes:
        payload["holes"] = np.asarray(
            [_interior_point(hole) for hole in holes],
            dtype=np.float64,
        )
    try:
        result = triangle_lib.triangulate(payload, "pQ")
    except (RuntimeError, ValueError, KeyError):
        return None
    result_vertices = result.get("vertices")
    indices = result.get("triangles")
    if (
        result_vertices is None
        or indices is None
        or result_vertices.shape != vertices.shape
        or not np.array_equal(result_vertices, vertices)
    ):
        return None
    flat = [point for ring in rings for point in ring]
    triangles: list[tuple[int, int, int]] = []
    for raw in indices:
        item = tuple(int(index) for index in raw)
        if _cross(*(flat[index] for index in item)) < 0:
            item = (item[0], item[2], item[1])
        triangles.append(item)
    if not triangles or any(
        len(set(triangle)) != 3
        or not _triangle_meets_mesh_tolerance(*(flat[index] for index in triangle))
        for triangle in triangles
    ):
        return None
    edge_counts: dict[tuple[int, int], int] = {}
    for a, b, c in triangles:
        for edge in ((a, b), (b, c), (c, a)):
            key = tuple(sorted(edge))
            edge_counts[key] = edge_counts.get(key, 0) + 1
    boundary_edges = {tuple(sorted(edge)) for edge in segments}
    if any(edge_counts.get(edge) != 1 for edge in boundary_edges) or any(
        count != (1 if edge in boundary_edges else 2) for edge, count in edge_counts.items()
    ):
        return None
    expected_area = _signed_area(outer) + sum(_signed_area(hole) for hole in holes)
    triangulated_area = sum(_cross(flat[a], flat[b], flat[c]) / 2 for a, b, c in triangles)
    if not math.isclose(
        triangulated_area,
        expected_area,
        rel_tol=1e-10,
        abs_tol=MESH_TOLERANCE_MM**2,
    ):
        return None
    return triangles


def _interior_point(ring: list[Point2]) -> tuple[float, float]:
    """Find a deterministic strict interior point for a simple hole ring."""

    minimum_y = min(point.y_mm for point in ring)
    maximum_y = max(point.y_mm for point in ring)
    for fraction in (0.5, 0.499123, 0.371247, 0.618731):
        y = minimum_y + (maximum_y - minimum_y) * fraction
        crossings: list[float] = []
        for start, end in zip(ring, (*ring[1:], ring[0])):
            if (start.y_mm > y) == (end.y_mm > y):
                continue
            crossings.append(
                start.x_mm + (y - start.y_mm) * (end.x_mm - start.x_mm) / (end.y_mm - start.y_mm)
            )
        crossings.sort()
        intervals = [
            (right - left, (left + right) / 2)
            for left, right in zip(crossings[::2], crossings[1::2])
            if right - left > MESH_TOLERANCE_MM
        ]
        for _width, x in sorted(intervals, reverse=True):
            candidate = Point2(x_mm=round(x, 6), y_mm=round(y, 6))
            if _point_strictly_inside(candidate, ring):
                return (candidate.x_mm, candidate.y_mm)
    raise ArtworkExtrusionError("hole has no stable interior point for triangulation")


def _hole_bridge_order(hole: list[tuple[Point2, int]]) -> tuple[float, float, tuple]:
    """Order holes from right to left so every new bridge targets the active boundary.

    Bridging in source-ID order is unstable for multiple apertures: distant holes can all
    choose the same original exterior vertex and create overlapping zero-width cuts. The
    rightmost-first order is geometric, independent of contour IDs, and lets later holes
    bridge to already incorporated hole boundaries when that is the nearest visible route.
    """

    anchor = max((item[0] for item in hole), key=lambda point: (point.x_mm, -point.y_mm))
    signature = tuple((item[0].x_mm, item[0].y_mm) for item in hole)
    return (-anchor.x_mm, anchor.y_mm, signature)


def _unique_polygon_positions(polygon: list[tuple[Point2, int]]) -> list[int]:
    positions: list[int] = []
    seen: set[tuple[float, float]] = set()
    for index, (point, _original_index) in enumerate(polygon):
        coordinate = (point.x_mm, point.y_mm)
        if coordinate in seen:
            continue
        seen.add(coordinate)
        positions.append(index)
    return positions


def _validate_triangulation_input(outer: list[Point2], holes: list[list[Point2]]) -> None:
    if len(outer) < 3:
        raise ArtworkExtrusionError("exterior contour needs at least three distinct vertices")
    if len({(point.x_mm, point.y_mm) for point in outer}) != len(outer):
        raise ArtworkExtrusionError("exterior contour contains duplicate vertices")
    if _signed_area(outer) <= MESH_TOLERANCE_MM**2:
        raise ArtworkExtrusionError(
            "exterior contour must be counter-clockwise with area above mesh tolerance"
        )
    outer_edges = _ring_edges(outer)
    outer_queries = _RingQueries(outer)
    previous_queries: list[_RingQueries] = []
    for hole_index, hole in enumerate(holes, start=1):
        if len(hole) < 3:
            raise ArtworkExtrusionError(f"hole {hole_index} needs at least three distinct vertices")
        if len({(point.x_mm, point.y_mm) for point in hole}) != len(hole):
            raise ArtworkExtrusionError(f"hole {hole_index} contains duplicate vertices")
        if _signed_area(hole) >= -(MESH_TOLERANCE_MM**2):
            raise ArtworkExtrusionError(
                f"hole {hole_index} must be clockwise with area above mesh tolerance"
            )
        queries = _RingQueries(hole)
        hole_edges = queries.edges
        if any(
            _segments_intersect(*hole_edge, *outer_edges[index])
            for hole_edge in hole_edges
            for index in outer_queries.index.query(_edge_bounds(hole_edge))
        ) or any(not outer_queries.inside(point) for point in hole):
            raise ArtworkExtrusionError(
                f"hole {hole_index} must be strictly inside and must not touch the exterior"
            )
        for previous_index, previous in enumerate(previous_queries, start=1):
            if any(
                _segments_intersect_beyond_shared_endpoint(*hole_edge, *previous.edges[index])
                for hole_edge in hole_edges
                for index in previous.index.query(_edge_bounds(hole_edge))
            ):
                raise ArtworkExtrusionError(
                    f"holes {previous_index} and {hole_index} overlap or share an edge"
                )
            if any(previous.strictly_inside(point) for point in hole) or any(
                queries.strictly_inside(point) for point in previous.ring
            ):
                raise ArtworkExtrusionError(
                    f"holes {previous_index} and {hole_index} must be disjoint, not nested"
                )
        previous_queries.append(queries)


def _edge_bounds(edge: tuple[Point2, Point2]) -> tuple[float, float, float, float]:
    a, b = edge
    return min(a.x_mm, b.x_mm), min(a.y_mm, b.y_mm), max(a.x_mm, b.x_mm), max(a.y_mm, b.y_mm)


class _RingQueries:
    def __init__(self, ring: list[Point2]) -> None:
        self.ring = ring
        self.edges = _ring_edges(ring)
        self.index = BoundsIndex(
            tuple(_edge_bounds(edge) for edge in self.edges), tolerance=TOPOLOGY_TOLERANCE_MM
        )
        self.starts = np.asarray([(p.x_mm, p.y_mm) for p in ring])
        self.ends = np.roll(self.starts, -1, axis=0)
        self.bounds = self.index.root.bounds

    def _on_boundary(self, point: Point2) -> bool:
        bounds = point.x_mm, point.y_mm, point.x_mm, point.y_mm
        return any(_point_on_segment(point, *self.edges[i]) for i in self.index.query(bounds))

    def inside(self, point: Point2) -> bool:
        return self._on_boundary(point) or self._ray_inside(point)

    def _ray_inside(self, point: Point2) -> bool:
        if not (
            self.bounds[0] <= point.x_mm <= self.bounds[2]
            and self.bounds[1] <= point.y_mm <= self.bounds[3]
        ):
            return False
        # Same ray-crossing predicate, evaluated only on edges straddling this scanline.
        mask = (self.starts[:, 1] > point.y_mm) != (self.ends[:, 1] > point.y_mm)
        starts, ends = self.starts[mask], self.ends[mask]
        crossings = starts[:, 0] + (point.y_mm - starts[:, 1]) * (ends[:, 0] - starts[:, 0]) / (
            ends[:, 1] - starts[:, 1]
        )
        return bool(np.count_nonzero(crossings > point.x_mm) % 2)

    def strictly_inside(self, point: Point2) -> bool:
        return not self._on_boundary(point) and self._ray_inside(point)


def _ring_edges(ring: list[Point2]) -> list[tuple[Point2, Point2]]:
    return [(ring[index], ring[(index + 1) % len(ring)]) for index in range(len(ring))]


def _bridge_visible(
    start: Point2,
    end: Point2,
    polygon: list[tuple[Point2, int]],
    holes_to_bridge: list[list[tuple[Point2, int]]],
    outer: list[Point2],
    holes: list[list[Point2]],
) -> bool:
    edges = [
        (ring[index][0], ring[(index + 1) % len(ring)][0])
        for ring in (polygon, *holes_to_bridge)
        for index in range(len(ring))
    ]
    for left, right in edges:
        if start in (left, right) or end in (left, right):
            continue
        if _segments_intersect(start, end, left, right):
            return False
    midpoint = Point2(
        x_mm=round((start.x_mm + end.x_mm) / 2, 6),
        y_mm=round((start.y_mm + end.y_mm) / 2, 6),
    )
    return _point_inside(midpoint, outer) and not any(
        _point_inside(midpoint, hole) for hole in holes
    )


def _ear_clip_indexed(points: list[tuple[Point2, int]]) -> list[tuple[int, int, int]]:
    if _signed_area([item[0] for item in points]) <= 0:
        raise ArtworkExtrusionError("triangulation requires a counter-clockwise polygon")
    try:
        return _ear_clip_indexed_pass(points, prefer_well_conditioned_ears=False)
    except ArtworkExtrusionError as original_error:
        try:
            return _ear_clip_indexed_pass(points, prefer_well_conditioned_ears=True)
        except ArtworkExtrusionError:
            raise original_error from None


def _ear_clip_indexed_pass(
    points: list[tuple[Point2, int]],
    *,
    prefer_well_conditioned_ears: bool,
) -> list[tuple[int, int, int]]:
    remaining = list(range(len(points)))
    triangles: list[tuple[int, int, int]] = []
    guard = 0
    while len(remaining) > 3:
        if len(remaining) == 4:
            final_pair = _triangulate_final_quad(points, remaining)
            if final_pair is not None:
                triangles.extend(final_pair)
                return triangles
        clipped = False
        candidates: list[tuple[float, int, tuple[int, int, int]]] = []
        for position, current in enumerate(remaining):
            previous = remaining[position - 1]
            following = remaining[(position + 1) % len(remaining)]
            a, b, c = points[previous][0], points[current][0], points[following][0]
            if not _triangle_meets_mesh_tolerance(a, b, c):
                continue
            if any(
                candidate not in (previous, current, following)
                and points[candidate][0] not in (a, b, c)
                and _point_in_triangle(points[candidate][0], a, b, c)
                for candidate in remaining
            ):
                continue
            triangle = (points[previous][1], points[current][1], points[following][1])
            if not prefer_well_conditioned_ears:
                triangles.append(triangle)
                del remaining[position]
                clipped = True
                break
            candidates.append((_triangle_mesh_margin(a, b, c), position, triangle))
        if prefer_well_conditioned_ears and candidates:
            _margin, position, triangle = max(
                candidates,
                key=lambda item: (item[0], -item[1]),
            )
            triangles.append(triangle)
            del remaining[position]
            clipped = True
        guard += 1
        if not clipped or guard > len(points) * len(points):
            duplicate_count = len(points) - len(
                {(point.x_mm, point.y_mm) for point, _source_index in points}
            )
            raise ArtworkExtrusionError(
                "polygon triangulation stalled with "
                f"{len(remaining)} of {len(points)} vertices remaining and "
                f"{duplicate_count} bridge duplicates; check for touching, nested, overlapping, "
                "or sub-tolerance contours"
            )
    triangle = tuple(points[index][1] for index in remaining)
    if len(set(triangle)) != 3 or not _triangle_meets_mesh_tolerance(
        points[remaining[0]][0],
        points[remaining[1]][0],
        points[remaining[2]][0],
    ):
        raise ArtworkExtrusionError(
            "triangulation ended with a face below mesh edge or altitude tolerance"
        )
    triangles.append(triangle)  # type: ignore[arg-type]
    return triangles


def _triangle_mesh_margin(a: Point2, b: Point2, c: Point2) -> float:
    """Return the limiting edge/altitude dimension of a valid triangle."""

    edges = (
        math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm),
        math.hypot(b.x_mm - c.x_mm, b.y_mm - c.y_mm),
        math.hypot(c.x_mm - a.x_mm, c.y_mm - a.y_mm),
    )
    return min(*edges, _cross(a, b, c) / max(edges))


def _triangulate_final_quad(
    points: list[tuple[Point2, int]],
    remaining: list[int],
) -> Optional[tuple[tuple[int, int, int], tuple[int, int, int]]]:
    """Choose a final diagonal only when both resulting faces meet mesh tolerance."""

    for position, current in enumerate(remaining):
        previous = remaining[position - 1]
        following = remaining[(position + 1) % len(remaining)]
        ear = (previous, current, following)
        if not _triangle_meets_mesh_tolerance(*(points[index][0] for index in ear)):
            continue
        opposite = next(index for index in remaining if index not in ear)
        if _point_in_triangle(
            points[opposite][0],
            points[previous][0],
            points[current][0],
            points[following][0],
        ):
            continue
        final = (previous, following, opposite)
        final_points = [points[index][0] for index in final]
        if _cross(*final_points) < 0:
            final = (previous, opposite, following)
            final_points = [points[index][0] for index in final]
        if not _triangle_meets_mesh_tolerance(*final_points):
            continue
        return (
            tuple(points[index][1] for index in ear),
            tuple(points[index][1] for index in final),
        )
    return None


def _validate_shared_interface_vertices(
    shared_edges: tuple[SharedEdge, ...],
    by_island: dict[str, tuple[Island2D, SourceLabel, Mesh, Part, int, float, float]],
    *,
    corner_touch_points: frozenset[tuple[float, float]],
) -> None:
    vertices_by_island = {
        island_id: {(item.x_mm, item.y_mm, item.z_mm) for item in evidence[2].vertices}
        for island_id, evidence in by_island.items()
    }
    surface_xy_by_island = {
        island_id: {
            z: {(x, y) for x, y, vertex_z in vertices_by_island[island_id] if vertex_z == z}
            for z in (evidence[5], evidence[6])
        }
        for island_id, evidence in by_island.items()
    }
    for edge in shared_edges:
        for point in (edge.start, edge.end):
            point_key = (point.x_mm, point.y_mm)
            common_relief_vertices: Optional[set[tuple[float, float]]] = None
            for island_id in edge.adjacent_island_ids:
                bottom = by_island[island_id][5]
                top = by_island[island_id][6]
                vertices = vertices_by_island[island_id]
                if point_key in corner_touch_points:
                    bottom_xy = surface_xy_by_island[island_id][bottom]
                    top_xy = surface_xy_by_island[island_id][top]
                    nearby = {
                        xy
                        for xy in bottom_xy & top_xy
                        if math.hypot(xy[0] - point.x_mm, xy[1] - point.y_mm)
                        <= CORNER_RELIEF_MM * 1.01
                    }
                    common_relief_vertices = (
                        nearby
                        if common_relief_vertices is None
                        else common_relief_vertices & nearby
                    )
                    continue
                if (point.x_mm, point.y_mm, bottom) not in vertices or (
                    point.x_mm,
                    point.y_mm,
                    top,
                ) not in vertices:
                    raise ArtworkExtrusionError(
                        f"shared edge {edge.id} is not segmented identically in island {island_id}"
                    )
            if point_key in corner_touch_points and not common_relief_vertices:
                raise ArtworkExtrusionError(
                    f"shared edge {edge.id} corner relief differs across adjacent islands"
                )


def _signed_mesh_volume(mesh: Mesh) -> float:
    volume = 0.0
    for triangle in mesh.triangles:
        a, b, c = (mesh.vertices[index] for index in triangle.vertices)
        volume += (
            a.x_mm * (b.y_mm * c.z_mm - b.z_mm * c.y_mm)
            - a.y_mm * (b.x_mm * c.z_mm - b.z_mm * c.x_mm)
            + a.z_mm * (b.x_mm * c.y_mm - b.y_mm * c.x_mm)
        ) / 6.0
    return volume


def _signed_area(points: list[Point2]) -> float:
    return (
        sum(
            left.x_mm * right.y_mm - right.x_mm * left.y_mm
            for left, right in zip(points, (*points[1:], points[0]))
        )
        / 2.0
    )


def _cross(a: Point2, b: Point2, c: Point2) -> float:
    return (b.x_mm - a.x_mm) * (c.y_mm - a.y_mm) - (b.y_mm - a.y_mm) * (c.x_mm - a.x_mm)


def _distance_squared(a: Point2, b: Point2) -> float:
    return (a.x_mm - b.x_mm) ** 2 + (a.y_mm - b.y_mm) ** 2


def _triangle_meets_mesh_tolerance(a: Point2, b: Point2, c: Point2) -> bool:
    twice_area = _cross(a, b, c)
    if twice_area <= 0:
        return False
    edge_lengths = (
        math.sqrt(_distance_squared(a, b)),
        math.sqrt(_distance_squared(b, c)),
        math.sqrt(_distance_squared(c, a)),
    )
    return (
        min(edge_lengths) > MESH_TOLERANCE_MM and twice_area / max(edge_lengths) > MESH_TOLERANCE_MM
    )


def _point_in_triangle(point: Point2, a: Point2, b: Point2, c: Point2) -> bool:
    first = _cross(a, b, point)
    second = _cross(b, c, point)
    third = _cross(c, a, point)
    tolerance = MESH_TOLERANCE_MM**2
    return first >= -tolerance and second >= -tolerance and third >= -tolerance


def _point_inside(point: Point2, polygon: list[Point2]) -> bool:
    if any(
        _point_on_segment(point, polygon[index], polygon[(index + 1) % len(polygon)])
        for index in range(len(polygon))
    ):
        return True
    inside = False
    for start, end in zip(polygon, (*polygon[1:], polygon[0])):
        if (start.y_mm > point.y_mm) == (end.y_mm > point.y_mm):
            continue
        crossing_x = start.x_mm + (point.y_mm - start.y_mm) * (end.x_mm - start.x_mm) / (
            end.y_mm - start.y_mm
        )
        if crossing_x > point.x_mm:
            inside = not inside
    return inside


def _segments_intersect(a: Point2, b: Point2, c: Point2, d: Point2) -> bool:
    orientations = (_cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b))
    if ((orientations[0] > 0 > orientations[1]) or (orientations[0] < 0 < orientations[1])) and (
        (orientations[2] > 0 > orientations[3]) or (orientations[2] < 0 < orientations[3])
    ):
        return True
    return any(
        abs(orientation) <= TOPOLOGY_TOLERANCE_MM**2 and _point_on_segment(point, start, end)
        for orientation, point, start, end in (
            (orientations[0], c, a, b),
            (orientations[1], d, a, b),
            (orientations[2], a, c, d),
            (orientations[3], b, c, d),
        )
    )


def _segments_intersect_beyond_shared_endpoint(a: Point2, b: Point2, c: Point2, d: Point2) -> bool:
    """Reject crossings and shared spans while permitting one shared endpoint.

    Raster contours routinely contain two holes that meet at one pixel corner. That
    zero-area contact is printable and the hole-bridging triangulator handles it, while
    a crossing or positive-length shared edge remains non-manifold.
    """

    if not _segments_intersect(a, b, c, d):
        return False
    if not any(_same_point(left, right) for left in (a, b) for right in (c, d)):
        return True
    if (
        abs(_cross(a, b, c)) > TOPOLOGY_TOLERANCE_MM**2
        or abs(_cross(a, b, d)) > TOPOLOGY_TOLERANCE_MM**2
    ):
        return False
    use_x = abs(b.x_mm - a.x_mm) >= abs(b.y_mm - a.y_mm)
    first = sorted((a.x_mm, b.x_mm) if use_x else (a.y_mm, b.y_mm))
    second = sorted((c.x_mm, d.x_mm) if use_x else (c.y_mm, d.y_mm))
    return min(first[1], second[1]) - max(first[0], second[0]) > TOPOLOGY_TOLERANCE_MM


def _same_point(left: Point2, right: Point2) -> bool:
    return math.hypot(left.x_mm - right.x_mm, left.y_mm - right.y_mm) <= TOPOLOGY_TOLERANCE_MM


def _point_strictly_inside(point: Point2, polygon: list[Point2]) -> bool:
    if any(
        _point_on_segment(point, polygon[index], polygon[(index + 1) % len(polygon)])
        for index in range(len(polygon))
    ):
        return False
    return _point_inside(point, polygon)
