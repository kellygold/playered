"""Gap-free shared-boundary topology derived from exhaustive label fields.

Potrace output is retained as construction evidence, never as independent color
ownership.  Physical boundary paths are polygonized once from the authoritative label
field, which makes every pixel exhaustive and every inter-color seam identical by
construction.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from image23mf.engine.labels import LabelField
from image23mf.geometry.model import (
    COORDINATE_PRECISION_MM,
    BaseShape,
    CapabilityState,
    CapabilityStatus,
    Contour2D,
    GeometryCapabilities,
    GeometryDocument,
    Island2D,
    LineSegment,
    Material,
    Path2D,
    Point2,
    SharedEdge,
    SourceLabel,
)

GridPoint = tuple[int, int]
GridEdge = tuple[GridPoint, GridPoint]


class TopologyBuildError(ValueError):
    """Raised when source evidence cannot produce canonical printable topology."""


@dataclass(frozen=True)
class TopologyComponentEvidence:
    component_index: int
    label_index: int
    island_id: str
    pixel_count: int
    area_mm2: float
    exterior_path_id: str
    hole_path_ids: tuple[str, ...]


@dataclass(frozen=True)
class TopologyCoverageEvidence:
    source_pixel_count: int
    represented_pixel_count: int
    source_area_mm2: float
    represented_area_mm2: float
    gap_area_mm2: float
    overlap_area_mm2: float


@dataclass(frozen=True)
class SharedBoundaryTopologyResult:
    document: GeometryDocument
    components: tuple[TopologyComponentEvidence, ...]
    coverage: TopologyCoverageEvidence
    source_vector_path_ids: tuple[str, ...]
    label_field_sha256: str
    topology_artifact_sha256: str

    def fingerprint(self) -> str:
        payload = {
            "components": [item.__dict__ for item in self.components],
            "coverage": self.coverage.__dict__,
            "document": self.document.model_dump(mode="json"),
            "label_field_sha256": self.label_field_sha256,
            "source_vector_path_ids": self.source_vector_path_ids,
            "topology_artifact_sha256": self.topology_artifact_sha256,
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True)
class _Component:
    index: int
    label: int
    cells: tuple[GridPoint, ...]
    loops: tuple[tuple[GridPoint, ...], ...]


def build_shared_boundary_topology(
    labels: LabelField,
    *,
    source_asset_sha256: str,
    source_labels: tuple[SourceLabel, ...],
    materials: tuple[Material, ...],
    base: BaseShape,
    canvas_width_mm: float,
    canvas_height_mm: float,
    vector_paths: tuple[Path2D, ...],
    vector_artifact_sha256: str,
    palette_color_order: tuple[str, ...] = (),
) -> SharedBoundaryTopologyResult:
    """Build canonical contours and shared edges from one exhaustive label field.

    ``vector_paths`` are the ID-sorted construction paths produced by the Potrace
    adapter. They remain in the Geometry IR as source evidence, but they cannot claim
    pixel ownership. This deliberately prevents per-color trace tolerances from making
    adjacent colors overlap or leave a hairline gap.
    """

    _validate_source_contract(
        labels,
        source_asset_sha256=source_asset_sha256,
        source_labels=source_labels,
        materials=materials,
        canvas_width_mm=canvas_width_mm,
        canvas_height_mm=canvas_height_mm,
        vector_paths=vector_paths,
        vector_artifact_sha256=vector_artifact_sha256,
    )
    label_sha256 = hashlib.sha256(labels.pixels).hexdigest()
    assignment, components = _connected_components(labels)
    label_by_index = {item.label_index: item for item in source_labels}

    paths: list[Path2D] = list(vector_paths)
    contours: list[Contour2D] = []
    islands: list[Island2D] = []
    component_records: list[
        tuple[_Component, Path2D, tuple[Path2D, ...], Contour2D, tuple[Contour2D, ...], Island2D]
    ] = []
    pixel_area = canvas_width_mm * canvas_height_mm / (labels.width * labels.height)

    for component in components:
        exterior_loops = [loop for loop in component.loops if _signed_grid_area(loop) > 0]
        hole_loops = [loop for loop in component.loops if _signed_grid_area(loop) < 0]
        if len(exterior_loops) != 1:
            raise TopologyBuildError(
                f"label {component.label} component {component.index} has non-manifold "
                f"boundary topology ({len(exterior_loops)} exteriors)"
            )
        exterior_path = _boundary_path(
            exterior_loops[0],
            width_px=labels.width,
            height_px=labels.height,
            width_mm=canvas_width_mm,
            height_mm=canvas_height_mm,
        )
        hole_paths = tuple(
            _boundary_path(
                loop,
                width_px=labels.width,
                height_px=labels.height,
                width_mm=canvas_width_mm,
                height_mm=canvas_height_mm,
            )
            for loop in hole_loops
        )
        exterior_contour = Contour2D.create(role="exterior", path_id=exterior_path.id)
        hole_contours = tuple(
            Contour2D.create(
                role="hole",
                path_id=path.id,
                parent_contour_id=exterior_contour.id,
            )
            for path in hole_paths
        )
        source_label = label_by_index[component.label]
        island = Island2D.create(
            exterior_contour_id=exterior_contour.id,
            hole_contour_ids=tuple(sorted(item.id for item in hole_contours)),
            material_id=source_label.material_id,
            source_label_id=source_label.id,
        )
        expected_area = len(component.cells) * pixel_area
        actual_area = exterior_path.signed_area_mm2 + sum(
            path.signed_area_mm2 for path in hole_paths
        )
        area_tolerance = max(
            COORDINATE_PRECISION_MM**2,
            _physical_perimeter(exterior_path, hole_paths) * COORDINATE_PRECISION_MM,
        )
        if not math.isclose(actual_area, expected_area, rel_tol=0, abs_tol=area_tolerance):
            raise TopologyBuildError(
                f"label {component.label} component {component.index} physical area changed "
                "during boundary quantization"
            )
        paths.extend((exterior_path, *hole_paths))
        contours.extend((exterior_contour, *hole_contours))
        islands.append(island)
        component_records.append(
            (
                component,
                exterior_path,
                hole_paths,
                exterior_contour,
                hole_contours,
                island,
            )
        )

    island_by_component = {item[0].index: item[5].id for item in component_records}
    shared_edges = _derive_shared_edges(
        assignment,
        island_by_component,
        width_mm=canvas_width_mm,
        height_mm=canvas_height_mm,
    )
    component_evidence = tuple(
        TopologyComponentEvidence(
            component_index=component.index,
            label_index=component.label,
            island_id=island.id,
            pixel_count=len(component.cells),
            area_mm2=round(len(component.cells) * pixel_area, 12),
            exterior_path_id=exterior_path.id,
            hole_path_ids=tuple(sorted(path.id for path in hole_paths)),
        )
        for component, exterior_path, hole_paths, _exterior, _holes, island in component_records
    )
    source_area = canvas_width_mm * canvas_height_mm
    represented_pixel_count = sum(item.pixel_count for item in component_evidence)
    coverage = TopologyCoverageEvidence(
        source_pixel_count=labels.width * labels.height,
        represented_pixel_count=represented_pixel_count,
        source_area_mm2=round(source_area, 12),
        represented_area_mm2=round(represented_pixel_count * pixel_area, 12),
        gap_area_mm2=0.0,
        overlap_area_mm2=0.0,
    )
    if coverage.source_pixel_count != coverage.represented_pixel_count or not math.isclose(
        coverage.source_area_mm2,
        coverage.represented_area_mm2,
        rel_tol=0,
        abs_tol=1e-9,
    ):
        raise TopologyBuildError("topology does not exhaustively cover the source label field")

    canonical_paths = tuple(sorted(paths, key=lambda item: item.id))
    canonical_contours = tuple(sorted(contours, key=lambda item: item.id))
    canonical_islands = tuple(sorted(islands, key=lambda item: item.id))
    canonical_shared = tuple(sorted(shared_edges, key=lambda item: item.id))
    topology_payload = {
        "contours": [item.model_dump(mode="json") for item in canonical_contours],
        "islands": [item.model_dump(mode="json") for item in canonical_islands],
        "label_field_sha256": label_sha256,
        "shared_edges": [item.model_dump(mode="json") for item in canonical_shared],
        "source_vector_path_ids": tuple(item.id for item in vector_paths),
    }
    topology_sha256 = hashlib.sha256(_canonical_json(topology_payload)).hexdigest()
    geometry_ir_sha256 = _source_contract_sha256(
        labels,
        source_asset_sha256=source_asset_sha256,
        source_labels=source_labels,
        materials=materials,
        base=base,
        canvas_width_mm=canvas_width_mm,
        canvas_height_mm=canvas_height_mm,
    )
    capabilities = GeometryCapabilities(
        geometry_ir=CapabilityState(
            status=CapabilityStatus.AVAILABLE,
            artifact_sha256=geometry_ir_sha256,
        ),
        vector_geometry=CapabilityState(
            status=CapabilityStatus.AVAILABLE,
            artifact_sha256=vector_artifact_sha256,
        ),
        topology=CapabilityState(
            status=CapabilityStatus.AVAILABLE,
            artifact_sha256=topology_sha256,
        ),
        mesh=CapabilityState(
            status=CapabilityStatus.NOT_REQUESTED,
            reason="Waiting for layer-aware mesh generation.",
        ),
        package_3mf=CapabilityState(
            status=CapabilityStatus.NOT_REQUESTED,
            reason="Waiting for printable meshes.",
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
    try:
        document = GeometryDocument(
            schema_version=1,
            source_asset_sha256=source_asset_sha256,
            processed_labels_sha256=label_sha256,
            source_width_px=labels.width,
            source_height_px=labels.height,
            canvas_width_mm=canvas_width_mm,
            canvas_height_mm=canvas_height_mm,
            palette_color_order=palette_color_order,
            paths=canonical_paths,
            contours=canonical_contours,
            materials=materials,
            source_labels=source_labels,
            islands=canonical_islands,
            shared_edges=canonical_shared,
            base=base,
            capabilities=capabilities,
        )
    except ValueError as error:
        raise TopologyBuildError(f"generated topology violates Geometry IR: {error}") from error
    return SharedBoundaryTopologyResult(
        document=document,
        components=component_evidence,
        coverage=coverage,
        source_vector_path_ids=tuple(item.id for item in vector_paths),
        label_field_sha256=label_sha256,
        topology_artifact_sha256=topology_sha256,
    )


def _validate_source_contract(
    labels: LabelField,
    *,
    source_asset_sha256: str,
    source_labels: tuple[SourceLabel, ...],
    materials: tuple[Material, ...],
    canvas_width_mm: float,
    canvas_height_mm: float,
    vector_paths: tuple[Path2D, ...],
    vector_artifact_sha256: str,
) -> None:
    if len(source_asset_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_asset_sha256
    ):
        raise TopologyBuildError("source_asset_sha256 must be a lowercase SHA-256")
    if len(vector_artifact_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in vector_artifact_sha256
    ):
        raise TopologyBuildError("vector_artifact_sha256 must be a lowercase SHA-256")
    if not math.isfinite(canvas_width_mm) or not math.isfinite(canvas_height_mm):
        raise TopologyBuildError("canvas dimensions must be finite")
    if canvas_width_mm <= 0 or canvas_height_mm <= 0:
        raise TopologyBuildError("canvas dimensions must be positive")
    if canvas_width_mm / labels.width < COORDINATE_PRECISION_MM or (
        canvas_height_mm / labels.height < COORDINATE_PRECISION_MM
    ):
        raise TopologyBuildError("physical pixel pitch is below Geometry IR coordinate precision")
    if not vector_paths or tuple(item.id for item in vector_paths) != tuple(
        sorted({item.id for item in vector_paths})
    ):
        raise TopologyBuildError("Potrace construction paths must be present and ID-sorted")
    if any(item.purpose != "construction" for item in vector_paths):
        raise TopologyBuildError("topology accepts only Potrace construction paths as evidence")
    if tuple(item.id for item in source_labels) != tuple(sorted(item.id for item in source_labels)):
        raise TopologyBuildError("source labels must be ID-sorted")
    if tuple(item.id for item in materials) != tuple(sorted(item.id for item in materials)):
        raise TopologyBuildError("materials must be ID-sorted")
    label_sha256 = hashlib.sha256(labels.pixels).hexdigest()
    if any(item.processed_labels_sha256 != label_sha256 for item in source_labels):
        raise TopologyBuildError(
            "source labels do not reference the authoritative label-field bytes"
        )
    label_by_index = {item.label_index: item for item in source_labels}
    if len(label_by_index) != len(source_labels):
        raise TopologyBuildError("source label indices must be unique")
    present = set(labels.pixels)
    if set(label_by_index) != present:
        raise TopologyBuildError(
            "source labels must exactly represent every label present in the exhaustive field"
        )
    material_by_id = {item.id: item for item in materials}
    if len(material_by_id) != len(materials):
        raise TopologyBuildError("material IDs must be unique")
    for source_label in source_labels:
        material = material_by_id.get(source_label.material_id)
        if material is None or material.palette_color_id != source_label.palette_color_id:
            raise TopologyBuildError("source label material mapping is missing or inconsistent")


def _connected_components(labels: LabelField) -> tuple[np.ndarray, tuple[_Component, ...]]:
    pixels = np.frombuffer(labels.pixels, dtype=np.uint8).reshape((labels.height, labels.width))
    assignment = np.full(pixels.shape, -1, dtype=np.int64)
    raw_components: list[tuple[int, int, tuple[GridPoint, ...]]] = []
    for row in range(labels.height):
        for column in range(labels.width):
            if assignment[row, column] >= 0:
                continue
            index = len(raw_components)
            label = int(pixels[row, column])
            pending = [(row, column)]
            assignment[row, column] = index
            cells: list[GridPoint] = []
            while pending:
                current_row, current_column = pending.pop()
                cells.append((current_column, labels.height - 1 - current_row))
                for next_row, next_column in (
                    (current_row - 1, current_column),
                    (current_row, current_column - 1),
                    (current_row, current_column + 1),
                    (current_row + 1, current_column),
                ):
                    if not (0 <= next_row < labels.height and 0 <= next_column < labels.width):
                        continue
                    if assignment[next_row, next_column] >= 0:
                        continue
                    if int(pixels[next_row, next_column]) != label:
                        continue
                    assignment[next_row, next_column] = index
                    pending.append((next_row, next_column))
            canonical_cells = tuple(sorted(cells))
            raw_components.append((index, label, canonical_cells))
    protected = _junction_points(assignment)
    components = tuple(
        _Component(
            index=index,
            label=label,
            cells=cells,
            loops=_component_loops(cells, protected=protected),
        )
        for index, label, cells in raw_components
    )
    return assignment, components


def _junction_points(assignment_source_y: np.ndarray) -> frozenset[GridPoint]:
    assignment = np.flipud(assignment_source_y)
    height, width = assignment.shape
    protected: set[GridPoint] = set()
    for y in range(height + 1):
        for x in range(width + 1):
            touching = {
                int(assignment[cell_y, cell_x])
                for cell_y, cell_x in (
                    (y - 1, x - 1),
                    (y - 1, x),
                    (y, x - 1),
                    (y, x),
                )
                if 0 <= cell_y < height and 0 <= cell_x < width
            }
            if len(touching) >= 3:
                protected.add((x, y))
    return frozenset(protected)


def _component_loops(
    cells: tuple[GridPoint, ...], *, protected: frozenset[GridPoint]
) -> tuple[tuple[GridPoint, ...], ...]:
    occupied = set(cells)
    edges: set[GridEdge] = set()
    for x, y in cells:
        if (x, y - 1) not in occupied:
            edges.add(((x, y), (x + 1, y)))
        if (x + 1, y) not in occupied:
            edges.add(((x + 1, y), (x + 1, y + 1)))
        if (x, y + 1) not in occupied:
            edges.add(((x + 1, y + 1), (x, y + 1)))
        if (x - 1, y) not in occupied:
            edges.add(((x, y + 1), (x, y)))
    outgoing: dict[GridPoint, list[GridPoint]] = defaultdict(list)
    for start, end in edges:
        outgoing[start].append(end)
    unused = set(edges)
    loops: list[tuple[GridPoint, ...]] = []
    while unused:
        start_edge = min(unused)
        start, current = start_edge
        unused.remove(start_edge)
        loop = [start]
        visited = {start}
        previous = start
        while current != start:
            if current in visited:
                raise TopologyBuildError("component boundary self-intersects at a repeated vertex")
            loop.append(current)
            visited.add(current)
            candidates = [end for end in outgoing[current] if (current, end) in unused]
            if not candidates:
                raise TopologyBuildError("component boundary is open")
            following = min(
                candidates,
                key=lambda end: (
                    _turn_preference(previous, current, end),
                    end,
                ),
            )
            unused.remove((current, following))
            previous, current = current, following
            if len(loop) > len(edges):
                raise TopologyBuildError("component boundary traversal did not terminate")
        simplified = _simplify_grid_loop(loop, protected=protected)
        if len(simplified) < 3 or _signed_grid_area(simplified) == 0:
            raise TopologyBuildError("component boundary collapsed during simplification")
        loops.append(simplified)
    return tuple(sorted(loops, key=lambda item: (_loop_sort_key(item), len(item))))


def _turn_preference(previous: GridPoint, current: GridPoint, following: GridPoint) -> int:
    incoming = _direction(previous, current)
    outgoing = _direction(current, following)
    turn = (outgoing - incoming) % 4
    # At a four-way vertex, keep each occupied 4-connected region on the right
    # side of its directed boundary.  Taking the right-hand continuation first
    # separates corner-touching holes into simple loops instead of stitching
    # them into a self-intersecting figure eight.
    return {3: 0, 0: 1, 1: 2, 2: 3}[turn]


def _direction(start: GridPoint, end: GridPoint) -> int:
    delta = (end[0] - start[0], end[1] - start[1])
    directions = {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}
    try:
        return directions[delta]
    except KeyError as error:  # pragma: no cover - edges originate on the unit pixel grid
        raise TopologyBuildError("boundary contains a non-orthogonal grid edge") from error


def _simplify_grid_loop(
    points: list[GridPoint], *, protected: frozenset[GridPoint]
) -> tuple[GridPoint, ...]:
    simplified = list(points)
    changed = True
    while changed and len(simplified) >= 3:
        changed = False
        result = []
        for index, current in enumerate(simplified):
            if current in protected:
                result.append(current)
                continue
            previous = simplified[index - 1]
            following = simplified[(index + 1) % len(simplified)]
            cross = (current[0] - previous[0]) * (following[1] - current[1]) - (
                current[1] - previous[1]
            ) * (following[0] - current[0])
            if cross == 0:
                changed = True
                continue
            result.append(current)
        simplified = result
    if len(set(simplified)) != len(simplified):
        raise TopologyBuildError("component boundary is non-manifold after simplification")
    return tuple(simplified)


def _boundary_path(
    loop: tuple[GridPoint, ...],
    *,
    width_px: int,
    height_px: int,
    width_mm: float,
    height_mm: float,
) -> Path2D:
    points = tuple(
        Point2(
            x_mm=round(x * width_mm / width_px, 6),
            y_mm=round(y * height_mm / height_px, 6),
        )
        for x, y in loop
    )
    if len(set(points)) != len(points):
        raise TopologyBuildError("physical quantization collapsed boundary vertices")
    try:
        return Path2D.create(
            purpose="boundary",
            start=points[0],
            segments=tuple(LineSegment(end=point) for point in (*points[1:], points[0])),
            closed=True,
        )
    except ValueError as error:
        raise TopologyBuildError(f"boundary is not a simple physical polygon: {error}") from error


def _derive_shared_edges(
    assignment_source_y: np.ndarray,
    island_by_component: dict[int, str],
    *,
    width_mm: float,
    height_mm: float,
) -> tuple[SharedEdge, ...]:
    assignment = np.flipud(assignment_source_y)
    height_px, width_px = assignment.shape
    unit_intervals: dict[tuple[tuple[str, str], str, int], list[int]] = defaultdict(list)
    for y in range(height_px):
        for x in range(width_px):
            component = int(assignment[y, x])
            if x + 1 < width_px and int(assignment[y, x + 1]) != component:
                adjacent = tuple(
                    sorted(
                        (
                            island_by_component[component],
                            island_by_component[int(assignment[y, x + 1])],
                        )
                    )
                )
                unit_intervals[(adjacent, "vertical", x + 1)].append(y)
            if y + 1 < height_px and int(assignment[y + 1, x]) != component:
                adjacent = tuple(
                    sorted(
                        (
                            island_by_component[component],
                            island_by_component[int(assignment[y + 1, x])],
                        )
                    )
                )
                unit_intervals[(adjacent, "horizontal", y + 1)].append(x)

    result: list[SharedEdge] = []
    for (adjacent, orientation, fixed), offsets in sorted(unit_intervals.items()):
        for start, end in _merge_unit_intervals(offsets):
            if orientation == "vertical":
                first = Point2(
                    x_mm=round(fixed * width_mm / width_px, 6),
                    y_mm=round(start * height_mm / height_px, 6),
                )
                second = Point2(
                    x_mm=round(fixed * width_mm / width_px, 6),
                    y_mm=round(end * height_mm / height_px, 6),
                )
            else:
                first = Point2(
                    x_mm=round(start * width_mm / width_px, 6),
                    y_mm=round(fixed * height_mm / height_px, 6),
                )
                second = Point2(
                    x_mm=round(end * width_mm / width_px, 6),
                    y_mm=round(fixed * height_mm / height_px, 6),
                )
            result.append(
                SharedEdge.create(
                    start=first,
                    end=second,
                    adjacent_island_ids=adjacent,
                    owner_island_id=adjacent[0],
                )
            )
    return tuple(result)


def _merge_unit_intervals(offsets: Iterable[int]) -> tuple[tuple[int, int], ...]:
    ordered = sorted(set(offsets))
    if not ordered:
        return ()
    result = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value != previous + 1:
            result.append((start, previous + 1))
            start = value
        previous = value
    result.append((start, previous + 1))
    return tuple(result)


def _signed_grid_area(loop: tuple[GridPoint, ...]) -> int:
    return sum(
        left[0] * right[1] - right[0] * left[1] for left, right in zip(loop, (*loop[1:], loop[0]))
    )


def _loop_sort_key(loop: tuple[GridPoint, ...]) -> tuple[int, int, int]:
    return (*min(loop), 0 if _signed_grid_area(loop) > 0 else 1)


def _physical_perimeter(exterior: Path2D, holes: tuple[Path2D, ...]) -> float:
    total = 0.0
    for path in (exterior, *holes):
        points = (path.start, *(segment.end for segment in path.segments))
        total += sum(
            math.hypot(right.x_mm - left.x_mm, right.y_mm - left.y_mm)
            for left, right in zip(points, points[1:])
        )
    return total


def _source_contract_sha256(
    labels: LabelField,
    *,
    source_asset_sha256: str,
    source_labels: tuple[SourceLabel, ...],
    materials: tuple[Material, ...],
    base: BaseShape,
    canvas_width_mm: float,
    canvas_height_mm: float,
) -> str:
    payload = {
        "base": base.model_dump(mode="json"),
        "canvas_height_mm": canvas_height_mm,
        "canvas_width_mm": canvas_width_mm,
        "label_field_sha256": hashlib.sha256(labels.pixels).hexdigest(),
        "materials": [item.model_dump(mode="json") for item in materials],
        "source_asset_sha256": source_asset_sha256,
        "source_height_px": labels.height,
        "source_labels": [item.model_dump(mode="json") for item in source_labels],
        "source_width_px": labels.width,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
