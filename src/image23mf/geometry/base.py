# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
"""Deterministic structural plate-base meshing.

The generator owns the printable substrate only. Artwork islands are extruded by the
downstream part generator, so this module never guesses color-region geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.geometry.model import (
    MESH_TOLERANCE_MM,
    CircleBase,
    CustomBase,
    LineSegment,
    Material,
    Mesh,
    Part,
    Path2D,
    Point2,
    Point3,
    RectangleBase,
    Triangle,
)

LAYER_ALIGNMENT_TOLERANCE_MM = 0.0001
DEFAULT_CURVE_TOLERANCE_MM = 0.05
MAX_CURVE_SEGMENTS = 4096


class BaseGenerationError(ValueError):
    """Raised when requested base geometry cannot produce a safe structural mesh."""


class ExcludedBuildRectangle(BaseModel):
    """Reserved rectangular area that the base may not intersect."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    minimum_x_mm: float = Field(ge=0)
    minimum_y_mm: float = Field(ge=0)
    maximum_x_mm: float = Field(gt=0)
    maximum_y_mm: float = Field(gt=0)

    @model_validator(mode="after")
    def bounds_are_ordered(self) -> ExcludedBuildRectangle:
        if self.maximum_x_mm <= self.minimum_x_mm or self.maximum_y_mm <= self.minimum_y_mm:
            raise ValueError("excluded build rectangle maximums must exceed minimums")
        return self


class BaseBuildBounds(BaseModel):
    """Axis-aligned printable build area in the canonical lower-left coordinate frame."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    origin_x_mm: float = Field(default=0, ge=0)
    origin_y_mm: float = Field(default=0, ge=0)
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)
    clearance_mm: float = Field(default=0, ge=0)
    excluded_rectangles: tuple[ExcludedBuildRectangle, ...] = ()


class BaseMeshOptions(BaseModel):
    """Layer-aware structural settings shared by every supported base shape."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    thickness_mm: float = Field(gt=0)
    layer_height_mm: float = Field(gt=0)
    minimum_layers: int = Field(default=2, ge=1, le=1000)
    rim_width_mm: float = Field(default=0, ge=0)
    rim_height_mm: float = Field(default=0, ge=0)
    curve_tolerance_mm: float = Field(default=DEFAULT_CURVE_TOLERANCE_MM, gt=0)

    @model_validator(mode="after")
    def rim_is_coherent(self) -> BaseMeshOptions:
        if (self.rim_width_mm == 0) != (self.rim_height_mm == 0):
            raise ValueError("rim width and height must both be zero or both be positive")
        return self


@dataclass(frozen=True)
class BaseMeshResult:
    """Validated structural mesh plus exact physical evidence used downstream."""

    mesh: Mesh
    part: Part
    layer_count: int
    thickness_mm: float
    total_height_mm: float
    bounds_min: Point2
    bounds_max: Point2
    curve_segments: int
    rim_enabled: bool


BaseShape = Union[RectangleBase, CircleBase, CustomBase]


def generate_structural_base(
    shape: BaseShape,
    *,
    material: Material,
    options: BaseMeshOptions,
    build_bounds: BaseBuildBounds,
    custom_paths: Optional[dict[str, Path2D]] = None,
    name: str = "Structural base",
) -> BaseMeshResult:
    """Generate one canonical watertight base mesh and its exact-provenance Part.

    Rectangle and circle rims are integral stepped solids. Custom contours support holes;
    custom rims are rejected because a deterministic physical inset is not part of IR v1.
    """

    layers, thickness = _aligned_thickness(options)
    custom_paths = custom_paths or {}
    outer, holes, curve_segments = _shape_rings(
        shape,
        custom_paths=custom_paths,
        curve_tolerance_mm=options.curve_tolerance_mm,
    )
    _validate_build_bounds(outer, holes, build_bounds)
    if options.rim_width_mm > 0:
        if holes:
            raise BaseGenerationError("integral rims do not support base holes")
        if isinstance(shape, CustomBase):
            raise BaseGenerationError(
                "custom-contour rims require an explicit inset contour, which IR v1 does not define"
            )
        inner, inner_segments = _rim_inner_ring(
            shape,
            options.rim_width_mm,
            curve_tolerance_mm=options.curve_tolerance_mm,
        )
        mesh = _stepped_rim_mesh(
            outer,
            inner,
            base_height=thickness,
            rim_height=options.rim_height_mm,
        )
        curve_segments = max(curve_segments, inner_segments)
    else:
        mesh = _prism_mesh(outer, holes, height=thickness)

    part = Part.create(
        name=name,
        role="base",
        mesh_id=mesh.id,
        material_id=material.id,
        source_geometry_kind="base",
        source_geometry_id=shape.id,
        source_label_id=None,
    )
    minimum_x, minimum_y, maximum_x, maximum_y = _ring_bounds(outer)
    return BaseMeshResult(
        mesh=mesh,
        part=part,
        layer_count=layers,
        thickness_mm=thickness,
        total_height_mm=thickness + options.rim_height_mm,
        bounds_min=Point2(x_mm=minimum_x, y_mm=minimum_y),
        bounds_max=Point2(x_mm=maximum_x, y_mm=maximum_y),
        curve_segments=curve_segments,
        rim_enabled=options.rim_width_mm > 0,
    )


def _aligned_thickness(options: BaseMeshOptions) -> tuple[int, float]:
    steps = options.thickness_mm / options.layer_height_mm
    layer_count = round(steps)
    if layer_count < options.minimum_layers:
        minimum = options.minimum_layers * options.layer_height_mm
        raise BaseGenerationError(
            f"base thickness requires at least {options.minimum_layers} layers ({minimum:g} mm)"
        )
    aligned = layer_count * options.layer_height_mm
    if not math.isclose(
        aligned,
        options.thickness_mm,
        rel_tol=0,
        abs_tol=LAYER_ALIGNMENT_TOLERANCE_MM,
    ):
        raise BaseGenerationError(
            f"base thickness {options.thickness_mm:g} mm is not a whole multiple of "
            f"layer height {options.layer_height_mm:g} mm; nearest is {aligned:g} mm"
        )
    if options.rim_height_mm:
        rim_steps = options.rim_height_mm / options.layer_height_mm
        aligned_rim = round(rim_steps) * options.layer_height_mm
        if not math.isclose(
            aligned_rim,
            options.rim_height_mm,
            rel_tol=0,
            abs_tol=LAYER_ALIGNMENT_TOLERANCE_MM,
        ):
            raise BaseGenerationError(
                f"rim height {options.rim_height_mm:g} mm is not a whole multiple of "
                f"layer height {options.layer_height_mm:g} mm; nearest is {aligned_rim:g} mm"
            )
    return layer_count, round(aligned, 6)


def _shape_rings(
    shape: BaseShape,
    *,
    custom_paths: dict[str, Path2D],
    curve_tolerance_mm: float,
) -> tuple[list[Point2], list[list[Point2]], int]:
    if isinstance(shape, RectangleBase):
        outer, segments = _rectangle_ring(shape, curve_tolerance_mm)
        return outer, [], segments
    if isinstance(shape, CircleBase):
        segments = _curve_segment_count(shape.radius_mm, curve_tolerance_mm, minimum=24)
        return _circle_ring(shape.center, shape.radius_mm, segments), [], segments
    exterior = custom_paths.get(shape.exterior_contour_id)
    if exterior is None:
        raise BaseGenerationError("custom base exterior path is missing")
    outer = _path_ring(exterior, expected_winding="ccw")
    holes = []
    for hole_id in shape.hole_contour_ids:
        path = custom_paths.get(hole_id)
        if path is None:
            raise BaseGenerationError(f"custom base hole path is missing: {hole_id}")
        holes.append(_path_ring(path, expected_winding="cw"))
    return outer, holes, 0


def _rectangle_ring(
    shape: RectangleBase,
    curve_tolerance_mm: float,
) -> tuple[list[Point2], int]:
    half_width = shape.width_mm / 2
    half_height = shape.height_mm / 2
    radius = shape.corner_radius_mm
    if radius == 0:
        return (
            [
                Point2(x_mm=shape.center.x_mm - half_width, y_mm=shape.center.y_mm - half_height),
                Point2(x_mm=shape.center.x_mm + half_width, y_mm=shape.center.y_mm - half_height),
                Point2(x_mm=shape.center.x_mm + half_width, y_mm=shape.center.y_mm + half_height),
                Point2(x_mm=shape.center.x_mm - half_width, y_mm=shape.center.y_mm + half_height),
            ],
            4,
        )
    per_corner = _rectangle_segment_count(shape, curve_tolerance_mm)
    points = _rectangle_ring_with_segments(shape, per_corner)
    return points, len(points)


def _rectangle_segment_count(shape: RectangleBase, curve_tolerance_mm: float) -> int:
    if shape.corner_radius_mm == 0:
        return 1
    return max(
        2,
        math.ceil(
            _curve_segment_count(
                shape.corner_radius_mm,
                curve_tolerance_mm,
                minimum=8,
            )
            / 4
        ),
    )


def _rectangle_ring_with_segments(
    shape: RectangleBase,
    per_corner: int,
) -> list[Point2]:
    half_width = shape.width_mm / 2
    half_height = shape.height_mm / 2
    radius = shape.corner_radius_mm
    if radius == 0:
        return [
            Point2(x_mm=shape.center.x_mm - half_width, y_mm=shape.center.y_mm - half_height),
            Point2(x_mm=shape.center.x_mm + half_width, y_mm=shape.center.y_mm - half_height),
            Point2(x_mm=shape.center.x_mm + half_width, y_mm=shape.center.y_mm + half_height),
            Point2(x_mm=shape.center.x_mm - half_width, y_mm=shape.center.y_mm + half_height),
        ]
    centers = (
        (shape.center.x_mm + half_width - radius, shape.center.y_mm - half_height + radius, -90),
        (shape.center.x_mm + half_width - radius, shape.center.y_mm + half_height - radius, 0),
        (shape.center.x_mm - half_width + radius, shape.center.y_mm + half_height - radius, 90),
        (shape.center.x_mm - half_width + radius, shape.center.y_mm - half_height + radius, 180),
    )
    points: list[Point2] = []
    for center_x, center_y, start_degrees in centers:
        for index in range(per_corner + 1):
            angle = math.radians(start_degrees + 90 * index / per_corner)
            point = Point2(
                x_mm=round(center_x + radius * math.cos(angle), 6),
                y_mm=round(center_y + radius * math.sin(angle), 6),
            )
            if not points or point != points[-1]:
                points.append(point)
    if points[0] == points[-1]:
        points.pop()
    return points


def _circle_ring(center: Point2, radius: float, segments: int) -> list[Point2]:
    points = [
        Point2(
            x_mm=round(center.x_mm + radius * math.cos(2 * math.pi * index / segments), 6),
            y_mm=round(center.y_mm + radius * math.sin(2 * math.pi * index / segments), 6),
        )
        for index in range(segments)
    ]
    if len(set((item.x_mm, item.y_mm) for item in points)) != len(points):
        raise BaseGenerationError("curve tolerance produced duplicate circle vertices")
    return points


def _curve_segment_count(radius: float, tolerance: float, *, minimum: int) -> int:
    if tolerance >= radius:
        return minimum
    angle = math.acos(max(-1.0, min(1.0, 1 - tolerance / radius)))
    segments = max(minimum, math.ceil(math.pi / angle))
    if segments > MAX_CURVE_SEGMENTS:
        raise BaseGenerationError(
            f"curve tolerance requires {segments} segments, above the {MAX_CURVE_SEGMENTS} limit"
        )
    return segments


def _path_ring(path: Path2D, *, expected_winding: Literal["ccw", "cw"]) -> list[Point2]:
    if path.purpose != "boundary" or not path.closed:
        raise BaseGenerationError("custom base paths must be closed boundary paths")
    if any(not isinstance(segment, LineSegment) for segment in path.segments):
        raise BaseGenerationError("custom base paths must use linear segments in IR v1")
    points = [path.start, *(segment.end for segment in path.segments[:-1])]
    area = _signed_area(points)
    if (expected_winding == "ccw" and area <= 0) or (expected_winding == "cw" and area >= 0):
        raise BaseGenerationError(f"custom base {expected_winding} winding is invalid")
    return points


def _rim_inner_ring(
    shape: Union[RectangleBase, CircleBase],  # noqa: UP007
    rim_width_mm: float,
    *,
    curve_tolerance_mm: float,
) -> tuple[list[Point2], int]:
    if isinstance(shape, CircleBase):
        radius = shape.radius_mm - rim_width_mm
        if radius <= MESH_TOLERANCE_MM:
            raise BaseGenerationError("rim width consumes the circle interior")
        segments = _curve_segment_count(shape.radius_mm, curve_tolerance_mm, minimum=24)
        return _circle_ring(shape.center, radius, segments), segments
    width = shape.width_mm - 2 * rim_width_mm
    height = shape.height_mm - 2 * rim_width_mm
    if min(width, height) <= 2 * MESH_TOLERANCE_MM:
        raise BaseGenerationError("rim width consumes the rectangle interior")
    radius = max(0.0, shape.corner_radius_mm - rim_width_mm)
    if shape.corner_radius_mm > 0 and radius <= MESH_TOLERANCE_MM:
        raise BaseGenerationError(
            "rim width consumes the rounded-corner radius; "
            "reduce the rim width or use square corners"
        )
    inner = RectangleBase.create(
        center=shape.center,
        width_mm=width,
        height_mm=height,
        corner_radius_mm=radius,
    )
    if shape.corner_radius_mm == 0:
        return _rectangle_ring(inner, curve_tolerance_mm)
    per_corner = _rectangle_segment_count(shape, curve_tolerance_mm)
    points = _rectangle_ring_with_segments(inner, per_corner)
    return points, len(points)


def _validate_build_bounds(
    outer: list[Point2],
    holes: list[list[Point2]],
    build: BaseBuildBounds,
) -> None:
    minimum_x, minimum_y, maximum_x, maximum_y = _ring_bounds(outer)
    allowed_min_x = build.origin_x_mm + build.clearance_mm
    allowed_min_y = build.origin_y_mm + build.clearance_mm
    allowed_max_x = build.origin_x_mm + build.width_mm - build.clearance_mm
    allowed_max_y = build.origin_y_mm + build.depth_mm - build.clearance_mm
    if (
        minimum_x < allowed_min_x - LAYER_ALIGNMENT_TOLERANCE_MM
        or minimum_y < allowed_min_y - LAYER_ALIGNMENT_TOLERANCE_MM
        or maximum_x > allowed_max_x + LAYER_ALIGNMENT_TOLERANCE_MM
        or maximum_y > allowed_max_y + LAYER_ALIGNMENT_TOLERANCE_MM
    ):
        raise BaseGenerationError(
            "base bounds exceed the printable area after configured clearance: "
            f"actual ({minimum_x:g}, {minimum_y:g})–({maximum_x:g}, {maximum_y:g}), "
            f"allowed ({allowed_min_x:g}, {allowed_min_y:g})–({allowed_max_x:g}, {allowed_max_y:g})"
        )
    outer_edges = _ring_edges(outer)
    for hole_index, hole in enumerate(holes):
        if not all(_point_inside(point, outer) for point in hole):
            raise BaseGenerationError("custom base holes must remain inside the exterior")
        if any(
            _segments_intersect_or_touch(*hole_edge, *outer_edge)
            for hole_edge in _ring_edges(hole)
            for outer_edge in outer_edges
        ):
            raise BaseGenerationError("custom base holes must not touch the exterior")
        for other in holes[:hole_index]:
            if (
                any(
                    _segments_intersect_or_touch(*hole_edge, *other_edge)
                    for hole_edge in _ring_edges(hole)
                    for other_edge in _ring_edges(other)
                )
                or _point_inside(hole[0], other)
                or _point_inside(other[0], hole)
            ):
                raise BaseGenerationError("custom base holes must be disjoint and non-nested")
    for excluded in build.excluded_rectangles:
        if _polygon_intersects_rectangle(outer, holes, excluded):
            raise BaseGenerationError(
                "base intersects an excluded build-plate area: "
                f"({excluded.minimum_x_mm:g}, {excluded.minimum_y_mm:g})–"
                f"({excluded.maximum_x_mm:g}, {excluded.maximum_y_mm:g})"
            )


def _prism_mesh(outer: list[Point2], holes: list[list[Point2]], *, height: float) -> Mesh:
    rings = [outer, *holes]
    flat = [point for ring in rings for point in ring]
    top_faces = _triangulate_polygon(outer, holes)
    count = len(flat)
    vertices = tuple(
        [Point3(x_mm=point.x_mm, y_mm=point.y_mm, z_mm=0) for point in flat]
        + [Point3(x_mm=point.x_mm, y_mm=point.y_mm, z_mm=height) for point in flat]
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


def _stepped_rim_mesh(
    outer: list[Point2],
    inner: list[Point2],
    *,
    base_height: float,
    rim_height: float,
) -> Mesh:
    if len(outer) != len(inner):
        raise BaseGenerationError("rim outer and inner rings must have identical segmentation")
    count = len(outer)
    total_height = round(base_height + rim_height, 6)
    vertices = tuple(
        [Point3(x_mm=p.x_mm, y_mm=p.y_mm, z_mm=0) for p in outer]
        + [Point3(x_mm=p.x_mm, y_mm=p.y_mm, z_mm=total_height) for p in outer]
        + [Point3(x_mm=p.x_mm, y_mm=p.y_mm, z_mm=total_height) for p in inner]
        + [Point3(x_mm=p.x_mm, y_mm=p.y_mm, z_mm=base_height) for p in inner]
    )
    triangles: list[tuple[int, int, int]] = []
    for a, b, c in _ear_clip(outer):
        triangles.append((a, c, b))
    for a, b, c in _ear_clip(inner):
        triangles.append((3 * count + a, 3 * count + b, 3 * count + c))
    for index in range(count):
        nxt = (index + 1) % count
        outer_bottom_a = index
        outer_bottom_b = nxt
        outer_top_a = count + index
        outer_top_b = count + nxt
        inner_top_a = 2 * count + index
        inner_top_b = 2 * count + nxt
        inner_base_a = 3 * count + index
        inner_base_b = 3 * count + nxt
        triangles.extend(
            (
                (outer_bottom_a, outer_bottom_b, outer_top_b),
                (outer_bottom_a, outer_top_b, outer_top_a),
                (outer_top_a, outer_top_b, inner_top_b),
                (outer_top_a, inner_top_b, inner_top_a),
                (inner_base_a, inner_top_b, inner_base_b),
                (inner_base_a, inner_top_a, inner_top_b),
            )
        )
    return Mesh.create(
        vertices=vertices,
        triangles=tuple(Triangle(vertices=item) for item in triangles),
        watertight=True,
    )


def _triangulate_polygon(
    outer: list[Point2], holes: list[list[Point2]]
) -> list[tuple[int, int, int]]:
    if not holes:
        return _ear_clip(outer)
    # Bridge each clockwise hole to a visible exterior vertex. The resulting weakly-simple
    # polygon duplicates bridge endpoints; the ear clipper deliberately ignores equal-coordinate
    # non-adjacent vertices on an ear boundary.
    polygon: list[tuple[Point2, int]] = [(point, index) for index, point in enumerate(outer)]
    next_index = len(outer)
    indexed_holes: list[list[tuple[Point2, int]]] = []
    for hole in holes:
        indexed_holes.append([(point, next_index + index) for index, point in enumerate(hole)])
        next_index += len(hole)
    all_edges = [
        (ring[index][0], ring[(index + 1) % len(ring)][0])
        for ring in (polygon, *indexed_holes)
        for index in range(len(ring))
    ]
    for hole in indexed_holes:
        hole_position = max(
            range(len(hole)),
            key=lambda index: (hole[index][0].x_mm, -hole[index][0].y_mm),
        )
        hole_vertex = hole[hole_position]
        candidates = sorted(
            range(len(polygon)),
            key=lambda index: _distance_squared(hole_vertex[0], polygon[index][0]),
        )
        outer_position = next(
            (
                index
                for index in candidates
                if _bridge_visible(
                    hole_vertex[0],
                    polygon[index][0],
                    outer,
                    holes,
                    all_edges,
                )
            ),
            None,
        )
        if outer_position is None:
            raise BaseGenerationError("custom base hole cannot be bridged without crossing an edge")
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


def _ear_clip(points: list[Point2]) -> list[tuple[int, int, int]]:
    return _ear_clip_indexed([(point, index) for index, point in enumerate(points)])


def _ear_clip_indexed(points: list[tuple[Point2, int]]) -> list[tuple[int, int, int]]:
    if _signed_area([item[0] for item in points]) <= 0:
        raise BaseGenerationError("triangulation requires a counter-clockwise polygon")
    remaining = list(range(len(points)))
    triangles: list[tuple[int, int, int]] = []
    guard = 0
    while len(remaining) > 3:
        clipped = False
        for position, current in enumerate(remaining):
            previous = remaining[position - 1]
            following = remaining[(position + 1) % len(remaining)]
            a, b, c = points[previous][0], points[current][0], points[following][0]
            if _cross(a, b, c) <= MESH_TOLERANCE_MM**2:
                continue
            blocked = False
            for candidate in remaining:
                if candidate in (previous, current, following):
                    continue
                point = points[candidate][0]
                if point in (a, b, c):
                    continue
                if _point_in_triangle(point, a, b, c):
                    blocked = True
                    break
            if blocked:
                continue
            triangles.append((points[previous][1], points[current][1], points[following][1]))
            del remaining[position]
            clipped = True
            break
        guard += 1
        if not clipped or guard > len(points) * len(points):
            raise BaseGenerationError("custom base polygon could not be triangulated safely")
    a, b, c = (points[index][1] for index in remaining)
    triangles.append((a, b, c))
    return triangles


def _bridge_visible(
    start: Point2,
    end: Point2,
    outer: list[Point2],
    holes: list[list[Point2]],
    edges: list[tuple[Point2, Point2]],
) -> bool:
    if start == end:
        return False
    for left, right in edges:
        if start in (left, right) or end in (left, right):
            continue
        if _segments_intersect(start, end, left, right):
            return False
    midpoint = Point2(x_mm=(start.x_mm + end.x_mm) / 2, y_mm=(start.y_mm + end.y_mm) / 2)
    return _point_inside(midpoint, outer) and not any(
        _point_inside(midpoint, hole) for hole in holes
    )


def _segments_intersect(a: Point2, b: Point2, c: Point2, d: Point2) -> bool:
    def orientation(p: Point2, q: Point2, r: Point2) -> float:
        return _cross(p, q, r)

    values = (
        orientation(a, b, c),
        orientation(a, b, d),
        orientation(c, d, a),
        orientation(c, d, b),
    )
    return ((values[0] > 0 > values[1]) or (values[0] < 0 < values[1])) and (
        (values[2] > 0 > values[3]) or (values[2] < 0 < values[3])
    )


def _point_inside(point: Point2, polygon: list[Point2]) -> bool:
    inside = False
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        if (start.y_mm > point.y_mm) == (end.y_mm > point.y_mm):
            continue
        crossing = start.x_mm + (point.y_mm - start.y_mm) * (end.x_mm - start.x_mm) / (
            end.y_mm - start.y_mm
        )
        if crossing > point.x_mm:
            inside = not inside
    return inside


def _polygon_intersects_rectangle(
    polygon: list[Point2],
    holes: list[list[Point2]],
    rectangle: ExcludedBuildRectangle,
) -> bool:
    corners = [
        Point2(x_mm=rectangle.minimum_x_mm, y_mm=rectangle.minimum_y_mm),
        Point2(x_mm=rectangle.maximum_x_mm, y_mm=rectangle.minimum_y_mm),
        Point2(x_mm=rectangle.maximum_x_mm, y_mm=rectangle.maximum_y_mm),
        Point2(x_mm=rectangle.minimum_x_mm, y_mm=rectangle.maximum_y_mm),
    ]

    def in_or_on_rectangle(point: Point2) -> bool:
        return (
            rectangle.minimum_x_mm <= point.x_mm <= rectangle.maximum_x_mm
            and rectangle.minimum_y_mm <= point.y_mm <= rectangle.maximum_y_mm
        )

    if any(in_or_on_rectangle(point) for point in polygon):
        return True
    if any(in_or_on_rectangle(point) for hole in holes for point in hole):
        return True
    if any(
        _point_inside(corner, polygon) and not any(_point_inside(corner, hole) for hole in holes)
        for corner in corners
    ):
        return True
    polygon_edges = [
        *_ring_edges(polygon),
        *(edge for hole in holes for edge in _ring_edges(hole)),
    ]
    rectangle_edges = [
        (point, corners[(index + 1) % len(corners)]) for index, point in enumerate(corners)
    ]
    return any(
        _segments_intersect_or_touch(*polygon_edge, *rectangle_edge)
        for polygon_edge in polygon_edges
        for rectangle_edge in rectangle_edges
    )


def _ring_edges(points: list[Point2]) -> list[tuple[Point2, Point2]]:
    return [(point, points[(index + 1) % len(points)]) for index, point in enumerate(points)]


def _segments_intersect_or_touch(a: Point2, b: Point2, c: Point2, d: Point2) -> bool:
    def on_segment(point: Point2, start: Point2, end: Point2) -> bool:
        return (
            abs(_cross(start, end, point)) <= LAYER_ALIGNMENT_TOLERANCE_MM
            and min(start.x_mm, end.x_mm) - LAYER_ALIGNMENT_TOLERANCE_MM
            <= point.x_mm
            <= max(start.x_mm, end.x_mm) + LAYER_ALIGNMENT_TOLERANCE_MM
            and min(start.y_mm, end.y_mm) - LAYER_ALIGNMENT_TOLERANCE_MM
            <= point.y_mm
            <= max(start.y_mm, end.y_mm) + LAYER_ALIGNMENT_TOLERANCE_MM
        )

    return _segments_intersect(a, b, c, d) or any(
        (
            on_segment(a, c, d),
            on_segment(b, c, d),
            on_segment(c, a, b),
            on_segment(d, a, b),
        )
    )


def _point_in_triangle(point: Point2, a: Point2, b: Point2, c: Point2) -> bool:
    values = (_cross(a, b, point), _cross(b, c, point), _cross(c, a, point))
    return all(value >= -(MESH_TOLERANCE_MM**2) for value in values)


def _cross(a: Point2, b: Point2, c: Point2) -> float:
    return (b.x_mm - a.x_mm) * (c.y_mm - a.y_mm) - (b.y_mm - a.y_mm) * (c.x_mm - a.x_mm)


def _signed_area(points: list[Point2]) -> float:
    return (
        sum(
            point.x_mm * points[(index + 1) % len(points)].y_mm
            - points[(index + 1) % len(points)].x_mm * point.y_mm
            for index, point in enumerate(points)
        )
        / 2
    )


def _ring_bounds(points: list[Point2]) -> tuple[float, float, float, float]:
    return (
        min(point.x_mm for point in points),
        min(point.y_mm for point in points),
        max(point.x_mm for point in points),
        max(point.y_mm for point in points),
    )


def _distance_squared(first: Point2, second: Point2) -> float:
    return (first.x_mm - second.x_mm) ** 2 + (first.y_mm - second.y_mm) ** 2
