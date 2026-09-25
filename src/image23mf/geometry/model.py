"""Canonical, renderer-independent geometry intermediate representation."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from image23mf.geometry.spatial import BoundsIndex

GEOMETRY_IR_SCHEMA_VERSION = 1
COORDINATE_PRECISION_MM = 0.000001
TOPOLOGY_TOLERANCE_MM = 0.00001
MESH_TOLERANCE_MM = 0.0001
MAX_ABS_COORDINATE_MM = 1_000_000.0
MAX_DOCUMENT_ENTITIES = 1_000_000
MAX_PATH_SEGMENTS = 2_000_000
MAX_MESH_VERTICES = 5_000_000
MAX_MESH_TRIANGLES = 10_000_000

ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*_[0-9a-f]{24}$")
SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, int):
            return str(item)
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("canonical JSON numbers must be finite")
            rendered = format(item, ".6f").rstrip("0").rstrip(".")
            return "0" if rendered in ("", "-0") else rendered
        if isinstance(item, dict):
            return (
                "{" + ",".join(f"{encode(key)}:{encode(item[key])}" for key in sorted(item)) + "}"
            )
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(element) for element in item) + "]"
        raise TypeError(f"unsupported canonical JSON value: {type(item).__name__}")

    return encode(_canonical_value(value)).encode("utf-8")


def deterministic_id(kind: str, value: Any) -> str:
    """Derive a stable content ID; callers must exclude the object's current ID."""

    if not re.fullmatch(r"[a-z][a-z0-9-]*", kind):
        raise ValueError("geometry ID kind must be lowercase kebab-case")
    digest = hashlib.sha256(
        _canonical_json_bytes(
            {"geometry_ir_schema_version": GEOMETRY_IR_SCHEMA_VERSION, "value": value}
        )
    ).hexdigest()[:24]
    return f"{kind}_{digest}"


def _canonical_mm(value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("geometry coordinates must be finite")
    if abs(number) > MAX_ABS_COORDINATE_MM:
        raise ValueError("geometry coordinate exceeds the supported millimetre range")
    normalized = round(number, 6)
    if not math.isclose(number, normalized, rel_tol=0, abs_tol=5e-13):
        raise ValueError("millimetre values must align to 0.000001 mm precision")
    return 0.0 if normalized == 0 else normalized


def _canonical_text(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("geometry text must use Unicode NFC normalization")
    return value


def _point_key(point: Point2) -> tuple[float, float]:
    return (point.x_mm, point.y_mm)


def _orientation(left: Point2, middle: Point2, right: Point2) -> float:
    return (middle.x_mm - left.x_mm) * (right.y_mm - left.y_mm) - (middle.y_mm - left.y_mm) * (
        right.x_mm - left.x_mm
    )


def _point_on_segment(point: Point2, start: Point2, end: Point2) -> bool:
    if abs(_orientation(start, end, point)) > TOPOLOGY_TOLERANCE_MM**2:
        return False
    return (
        min(start.x_mm, end.x_mm) - TOPOLOGY_TOLERANCE_MM
        <= point.x_mm
        <= max(start.x_mm, end.x_mm) + TOPOLOGY_TOLERANCE_MM
        and min(start.y_mm, end.y_mm) - TOPOLOGY_TOLERANCE_MM
        <= point.y_mm
        <= max(start.y_mm, end.y_mm) + TOPOLOGY_TOLERANCE_MM
    )


def _segments_intersect(
    first_start: Point2,
    first_end: Point2,
    second_start: Point2,
    second_end: Point2,
) -> bool:
    orientations = (
        _orientation(first_start, first_end, second_start),
        _orientation(first_start, first_end, second_end),
        _orientation(second_start, second_end, first_start),
        _orientation(second_start, second_end, first_end),
    )
    if ((orientations[0] > 0 > orientations[1]) or (orientations[0] < 0 < orientations[1])) and (
        (orientations[2] > 0 > orientations[3]) or (orientations[2] < 0 < orientations[3])
    ):
        return True
    return any(
        abs(orientation) <= TOPOLOGY_TOLERANCE_MM**2 and _point_on_segment(point, start, end)
        for orientation, point, start, end in (
            (orientations[0], second_start, first_start, first_end),
            (orientations[1], second_end, first_start, first_end),
            (orientations[2], first_start, second_start, second_end),
            (orientations[3], first_end, second_start, second_end),
        )
    )


def _proper_segments_intersect(
    first_start: Point2,
    first_end: Point2,
    second_start: Point2,
    second_end: Point2,
) -> bool:
    if not _segments_intersect(first_start, first_end, second_start, second_end):
        return False
    return not any(
        math.hypot(left.x_mm - right.x_mm, left.y_mm - right.y_mm) <= TOPOLOGY_TOLERANCE_MM
        for left in (first_start, first_end)
        for right in (second_start, second_end)
    )


def _collinear_overlap_edge(
    first_start: Point2,
    first_end: Point2,
    second_start: Point2,
    second_end: Point2,
) -> Optional[tuple[Point2, Point2]]:
    dx = first_end.x_mm - first_start.x_mm
    dy = first_end.y_mm - first_start.y_mm
    length = math.hypot(dx, dy)
    if length <= TOPOLOGY_TOLERANCE_MM:
        return None
    tolerance = TOPOLOGY_TOLERANCE_MM * length
    if (
        abs(_orientation(first_start, first_end, second_start)) > tolerance
        or abs(_orientation(first_start, first_end, second_end)) > tolerance
    ):
        return None
    use_x = abs(dx) >= abs(dy)
    if use_x:
        first = sorted((first_start.x_mm, first_end.x_mm))
        second = sorted((second_start.x_mm, second_end.x_mm))
    else:
        first = sorted((first_start.y_mm, first_end.y_mm))
        second = sorted((second_start.y_mm, second_end.y_mm))
    low = max(first[0], second[0])
    high = min(first[1], second[1])
    if high - low <= TOPOLOGY_TOLERANCE_MM:
        return None

    def interpolate(position: float) -> Point2:
        ratio = (position - first_start.x_mm) / dx if use_x else (position - first_start.y_mm) / dy
        return Point2(
            x_mm=first_start.x_mm + ratio * dx,
            y_mm=first_start.y_mm + ratio * dy,
        )

    return interpolate(low), interpolate(high)


def _path_vertices(path: Path2D) -> tuple[Point2, ...]:
    return (path.start, *(segment.end for segment in path.segments[:-1]))


def _path_edges(path: Path2D) -> tuple[tuple[Point2, Point2], ...]:
    points = (path.start, *(segment.end for segment in path.segments))
    return tuple(zip(points, points[1:]))


def _path_bounds(path: Path2D) -> tuple[float, float, float, float]:
    vertices = _path_vertices(path)
    return (
        min(point.x_mm for point in vertices),
        min(point.y_mm for point in vertices),
        max(point.x_mm for point in vertices),
        max(point.y_mm for point in vertices),
    )


def _bounds_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] < second[0] - TOPOLOGY_TOLERANCE_MM
        or second[2] < first[0] - TOPOLOGY_TOLERANCE_MM
        or first[3] < second[1] - TOPOLOGY_TOLERANCE_MM
        or second[3] < first[1] - TOPOLOGY_TOLERANCE_MM
    )


def _edge_bounds(edge: tuple[Point2, Point2]) -> tuple[float, float, float, float]:
    return (
        min(edge[0].x_mm, edge[1].x_mm),
        min(edge[0].y_mm, edge[1].y_mm),
        max(edge[0].x_mm, edge[1].x_mm),
        max(edge[0].y_mm, edge[1].y_mm),
    )


def _canonical_edge_key(start: Point2, end: Point2) -> tuple[float, float, float, float]:
    left, right = sorted((_point_key(start), _point_key(end)))
    return (*left, *right)


def _point_strictly_inside_polygon(point: Point2, polygon: Path2D) -> bool:
    edges = _path_edges(polygon)
    if any(_point_on_segment(point, start, end) for start, end in edges):
        return False
    inside = False
    for start, end in edges:
        if (start.y_mm > point.y_mm) == (end.y_mm > point.y_mm):
            continue
        crossing_x = start.x_mm + (point.y_mm - start.y_mm) * (end.x_mm - start.x_mm) / (
            end.y_mm - start.y_mm
        )
        if crossing_x > point.x_mm:
            inside = not inside
    return inside


def _polygons_have_area_overlap(first: Path2D, second: Path2D) -> bool:
    first_edges = _path_edges(first)
    second_edges = _path_edges(second)
    second_index = BoundsIndex(
        tuple(_edge_bounds(edge) for edge in second_edges), tolerance=TOPOLOGY_TOLERANCE_MM
    )
    if any(
        _proper_segments_intersect(*first_edge, *second_edges[index])
        for first_edge in first_edges
        for index in second_index.query(_edge_bounds(first_edge))
    ):
        return True
    return _point_strictly_inside_polygon(first.start, second) or _point_strictly_inside_polygon(
        second.start, first
    )


def _point_in_filled_paths(
    point: Point2,
    exterior: Path2D,
    holes: tuple[Path2D, ...],
    hole_bounds: tuple[tuple[float, float, float, float], ...],
) -> bool:
    if not _point_strictly_inside_polygon(point, exterior):
        return False
    for hole, bounds in zip(holes, hole_bounds):
        if not (
            bounds[0] - TOPOLOGY_TOLERANCE_MM <= point.x_mm <= bounds[2] + TOPOLOGY_TOLERANCE_MM
            and bounds[1] - TOPOLOGY_TOLERANCE_MM <= point.y_mm <= bounds[3] + TOPOLOGY_TOLERANCE_MM
        ):
            continue
        if _point_strictly_inside_polygon(point, hole) or any(
            _point_on_segment(point, *edge) for edge in _path_edges(hole)
        ):
            return False
    return True


def _filled_path_sample(
    exterior: Path2D,
    holes: tuple[Path2D, ...],
    hole_bounds: tuple[tuple[float, float, float, float], ...],
) -> Optional[Point2]:
    """Choose a deterministic point strictly inside a simple filled island."""

    for start, end in _path_edges(exterior):
        dx = end.x_mm - start.x_mm
        dy = end.y_mm - start.y_mm
        length = math.hypot(dx, dy)
        if length <= MESH_TOLERANCE_MM:
            continue
        inset = min(MESH_TOLERANCE_MM * 2, length / 4)
        candidate = Point2(
            x_mm=round((start.x_mm + end.x_mm) / 2 - dy * inset / length, 6),
            y_mm=round((start.y_mm + end.y_mm) / 2 + dx * inset / length, 6),
        )
        if _point_in_filled_paths(candidate, exterior, holes, hole_bounds):
            return candidate
    return None


class GeometryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class IdentifiedGeometryModel(GeometryModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*_[0-9a-f]{24}$")
    id_kind: ClassVar[str]

    @classmethod
    def create(cls, **payload: Any) -> Any:
        provisional = cls(id=f"{cls.id_kind}_{'0' * 24}", **payload)
        return provisional.model_copy(update={"id": provisional.expected_id()})

    def expected_id(self) -> str:
        return deterministic_id(self.id_kind, self.model_dump(mode="json", exclude={"id"}))


class GeometryContract(GeometryModel):
    units: Literal["millimetre"] = "millimetre"
    origin: Literal["lower_left_build_plate"] = "lower_left_build_plate"
    x_axis: Literal["right"] = "right"
    y_axis: Literal["away_from_viewer"] = "away_from_viewer"
    z_axis: Literal["up"] = "up"
    source_image_origin: Literal["upper_left"] = "upper_left"
    source_image_y_axis: Literal["down"] = "down"
    source_to_geometry_y: Literal["invert_about_canvas_height"] = "invert_about_canvas_height"
    exterior_winding: Literal["counter_clockwise"] = "counter_clockwise"
    hole_winding: Literal["clockwise"] = "clockwise"
    mesh_winding: Literal["counter_clockwise_when_viewed_from_outside"] = (
        "counter_clockwise_when_viewed_from_outside"
    )
    fill_rule: Literal["non_zero"] = "non_zero"
    coordinate_precision_mm: Literal[0.000001] = COORDINATE_PRECISION_MM
    positional_tolerance_mm: Literal[0.00001] = TOPOLOGY_TOLERANCE_MM
    altitude_tolerance_mm: Literal[0.0001] = MESH_TOLERANCE_MM
    topology_tolerance_mm: Literal[0.00001] = TOPOLOGY_TOLERANCE_MM
    mesh_tolerance_mm: Literal[0.0001] = MESH_TOLERANCE_MM


class Point2(GeometryModel):
    x_mm: float
    y_mm: float

    _normalize_x = field_validator("x_mm")(_canonical_mm)
    _normalize_y = field_validator("y_mm")(_canonical_mm)


class Point3(Point2):
    z_mm: float

    _normalize_z = field_validator("z_mm")(_canonical_mm)


class LineSegment(GeometryModel):
    kind: Literal["line"] = "line"
    end: Point2


class ArcSegment(GeometryModel):
    kind: Literal["arc"] = "arc"
    end: Point2
    center: Point2
    clockwise: bool


class CubicSegment(GeometryModel):
    kind: Literal["cubic"] = "cubic"
    control_1: Point2
    control_2: Point2
    end: Point2


PathSegment = Annotated[
    Union[LineSegment, ArcSegment, CubicSegment],
    Field(discriminator="kind"),
]


class Path2D(IdentifiedGeometryModel):
    id_kind = "path"
    purpose: Literal["boundary", "construction"]
    start: Point2
    segments: tuple[PathSegment, ...] = Field(min_length=1, max_length=MAX_PATH_SEGMENTS)
    closed: bool

    @classmethod
    def create(cls, **payload: Any) -> Path2D:
        if payload.get("purpose") == "boundary":
            start = Point2.model_validate(payload["start"])
            all_linear = all(
                (
                    item.kind
                    if isinstance(item, (LineSegment, ArcSegment, CubicSegment))
                    else item.get("kind")
                )
                == "line"
                for item in payload["segments"]
            )
            segments = (
                tuple(LineSegment.model_validate(item) for item in payload["segments"])
                if all_linear
                else ()
            )
            if all_linear and segments and segments[-1].end == start:
                vertices = [start, *(segment.end for segment in segments[:-1])]
                if vertices:
                    offset = min(
                        range(len(vertices)), key=lambda index: _point_key(vertices[index])
                    )
                    vertices = vertices[offset:] + vertices[:offset]
                    start = vertices[0]
                    payload = {
                        **payload,
                        "start": start,
                        "segments": tuple(
                            LineSegment(end=point) for point in (*vertices[1:], start)
                        ),
                    }
        return super().create(**payload)

    @model_validator(mode="after")
    def path_is_well_formed(self) -> Path2D:
        points = [self.start, *(segment.end for segment in self.segments)]
        current = self.start
        for segment in self.segments:
            if segment.end == current:
                raise ValueError("path segments must have non-zero length")
            if isinstance(segment, ArcSegment):
                start_radius = math.hypot(
                    current.x_mm - segment.center.x_mm,
                    current.y_mm - segment.center.y_mm,
                )
                end_radius = math.hypot(
                    segment.end.x_mm - segment.center.x_mm,
                    segment.end.y_mm - segment.center.y_mm,
                )
                if start_radius <= TOPOLOGY_TOLERANCE_MM:
                    raise ValueError("arc radius must exceed topology tolerance")
                if not math.isclose(
                    start_radius,
                    end_radius,
                    rel_tol=0,
                    abs_tol=TOPOLOGY_TOLERANCE_MM,
                ):
                    raise ValueError("arc start and end radii must match within tolerance")
            current = segment.end
        if self.closed and points[-1] != self.start:
            raise ValueError("closed paths must end exactly at their start point")
        if not self.closed and points[-1] == self.start:
            raise ValueError("open paths must not end at their start point")
        if self.purpose == "boundary":
            if not self.closed:
                raise ValueError("boundary paths must be closed")
            if len(self.segments) < 3:
                raise ValueError("boundary paths require at least three segments")
            if any(segment.kind != "line" for segment in self.segments):
                raise ValueError("v1 boundary paths must use linear segments")
            vertices = points[:-1]
            keys = tuple(_point_key(point) for point in vertices)
            if len(set(keys)) != len(keys) or len(keys) < 3:
                raise ValueError("boundary paths require unique vertices")
            if keys[0] != min(keys):
                raise ValueError("boundary paths must begin at their canonical minimum vertex")
            edges = _path_edges(self)
            edge_count = len(edges)
            edge_index = BoundsIndex(
                tuple(_edge_bounds(edge) for edge in edges), tolerance=TOPOLOGY_TOLERANCE_MM
            )
            for first_index, first in enumerate(edges):
                for second_index in edge_index.query(_edge_bounds(first)):
                    if second_index <= first_index:
                        continue
                    if second_index in (first_index + 1,):
                        continue
                    if first_index == 0 and second_index == edge_count - 1:
                        continue
                    if _segments_intersect(*first, *edges[second_index]):
                        raise ValueError("boundary paths must not self-intersect")
        return self

    @property
    def signed_area_mm2(self) -> float:
        if self.purpose != "boundary":
            raise ValueError("signed area is defined only for boundary paths")
        points = [self.start, *(segment.end for segment in self.segments)]
        return (
            sum(
                left.x_mm * right.y_mm - right.x_mm * left.y_mm
                for left, right in zip(points, points[1:])
            )
            / 2.0
        )


class Contour2D(IdentifiedGeometryModel):
    id_kind = "contour"
    role: Literal["exterior", "hole"]
    path_id: str = Field(pattern=r"^path_[0-9a-f]{24}$")
    parent_contour_id: Optional[str] = Field(
        default=None,
        pattern=r"^contour_[0-9a-f]{24}$",
    )

    @model_validator(mode="after")
    def parent_matches_role(self) -> Contour2D:
        if self.role == "exterior" and self.parent_contour_id is not None:
            raise ValueError("exterior contours cannot have a parent")
        if self.role == "hole" and self.parent_contour_id is None:
            raise ValueError("hole contours require an exterior parent")
        return self

    def validate_path(self, path: Path2D) -> None:
        if path.purpose != "boundary":
            raise ValueError("contours require boundary paths")
        area = path.signed_area_mm2
        if abs(area) <= TOPOLOGY_TOLERANCE_MM**2:
            raise ValueError("contour area is below topology tolerance")
        if self.role == "exterior" and area <= 0:
            raise ValueError("exterior contours must be counter-clockwise")
        if self.role == "hole" and area >= 0:
            raise ValueError("hole contours must be clockwise")


class Material(IdentifiedGeometryModel):
    id_kind = "material"
    name: str = Field(min_length=1, max_length=120)
    color_hex: str = Field(pattern=r"^#[0-9A-F]{6}$")
    palette_color_id: str = Field(min_length=1, max_length=80)
    filament_id: Optional[str] = Field(default=None, min_length=1, max_length=160)

    _canonical_name = field_validator("name")(_canonical_text)
    _canonical_palette = field_validator("palette_color_id")(_canonical_text)

    @field_validator("filament_id")
    @classmethod
    def canonical_filament_id(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _canonical_text(value)


class SourceLabel(IdentifiedGeometryModel):
    id_kind = "source-label"
    source_asset_id: str = Field(min_length=1, max_length=160)
    processed_labels_sha256: str = Field(pattern=SHA256_PATTERN)
    label_index: int = Field(ge=0, le=65535)
    name: str = Field(min_length=1, max_length=120)
    color_hex: str = Field(pattern=r"^#[0-9A-F]{6}$")
    palette_color_id: str = Field(min_length=1, max_length=80)
    material_id: str = Field(pattern=r"^material_[0-9a-f]{24}$")
    classification: Literal["artwork", "background", "support"]

    _canonical_source_asset = field_validator("source_asset_id")(_canonical_text)
    _canonical_name = field_validator("name")(_canonical_text)
    _canonical_palette = field_validator("palette_color_id")(_canonical_text)


class Island2D(IdentifiedGeometryModel):
    id_kind = "island"
    exterior_contour_id: str = Field(pattern=r"^contour_[0-9a-f]{24}$")
    hole_contour_ids: tuple[str, ...] = ()
    material_id: str = Field(pattern=r"^material_[0-9a-f]{24}$")
    source_label_id: str = Field(pattern=r"^source-label_[0-9a-f]{24}$")

    @field_validator("hole_contour_ids")
    @classmethod
    def holes_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"contour_[0-9a-f]{24}", item) for item in value):
            raise ValueError("island hole contour IDs are invalid")
        if value != tuple(sorted(set(value))):
            raise ValueError("island hole contour IDs must be unique and sorted")
        return value


class SharedEdge(IdentifiedGeometryModel):
    id_kind = "shared-edge"
    start: Point2
    end: Point2
    adjacent_island_ids: tuple[str, str]
    owner_island_id: str = Field(pattern=r"^island_[0-9a-f]{24}$")

    @model_validator(mode="after")
    def ownership_is_canonical(self) -> SharedEdge:
        start_key = (self.start.x_mm, self.start.y_mm)
        end_key = (self.end.x_mm, self.end.y_mm)
        if start_key >= end_key:
            raise ValueError("shared edges must use ascending lexicographic endpoint order")
        if self.adjacent_island_ids != tuple(sorted(set(self.adjacent_island_ids))):
            raise ValueError("shared edge island IDs must be distinct and sorted")
        if any(not re.fullmatch(r"island_[0-9a-f]{24}", item) for item in self.adjacent_island_ids):
            raise ValueError("shared edge island IDs are invalid")
        if self.owner_island_id != self.adjacent_island_ids[0]:
            raise ValueError("the lexicographically first adjacent island owns the shared edge")
        return self


class Triangle(GeometryModel):
    vertices: tuple[int, int, int]

    @model_validator(mode="after")
    def vertices_are_distinct(self) -> Triangle:
        if min(self.vertices) < 0 or len(set(self.vertices)) != 3:
            raise ValueError("triangle vertex indices must be non-negative and distinct")
        return self


class Mesh(IdentifiedGeometryModel):
    id_kind = "mesh"
    vertices: tuple[Point3, ...] = Field(min_length=3, max_length=MAX_MESH_VERTICES)
    triangles: tuple[Triangle, ...] = Field(min_length=1, max_length=MAX_MESH_TRIANGLES)
    watertight: bool

    @classmethod
    def create(cls, **payload: Any) -> Mesh:
        vertices = tuple(Point3.model_validate(item) for item in payload["vertices"])
        order = sorted(
            range(len(vertices)),
            key=lambda index: (
                vertices[index].x_mm,
                vertices[index].y_mm,
                vertices[index].z_mm,
            ),
        )
        remap = {old: new for new, old in enumerate(order)}
        canonical_vertices = tuple(vertices[index] for index in order)
        canonical_triangles = []
        for item in payload["triangles"]:
            triangle = Triangle.model_validate(item)
            if max(triangle.vertices) >= len(vertices):
                raise ValueError("triangle references a missing mesh vertex")
            mapped = tuple(remap[index] for index in triangle.vertices)
            offset = mapped.index(min(mapped))
            canonical_triangles.append(mapped[offset:] + mapped[:offset])
        payload = {
            **payload,
            "vertices": canonical_vertices,
            "triangles": tuple(Triangle(vertices=item) for item in sorted(canonical_triangles)),
        }
        return super().create(**payload)

    @model_validator(mode="after")
    def triangles_reference_real_non_degenerate_vertices(self) -> Mesh:
        vertex_keys = tuple((item.x_mm, item.y_mm, item.z_mm) for item in self.vertices)
        if vertex_keys != tuple(sorted(vertex_keys)) or len(vertex_keys) != len(set(vertex_keys)):
            raise ValueError("mesh vertices must be unique and lexicographically sorted")
        if self.triangles != tuple(sorted(self.triangles, key=lambda item: item.vertices)):
            raise ValueError("mesh triangles must use canonical index ordering")
        directed_edges: dict[tuple[int, int], int] = {}
        undirected_edges: dict[tuple[int, int], int] = {}
        for triangle in self.triangles:
            if triangle.vertices[0] != min(triangle.vertices):
                raise ValueError("mesh triangle cycles must begin with their minimum vertex")
            if max(triangle.vertices) >= len(self.vertices):
                raise ValueError("triangle references a missing mesh vertex")
            a, b, c = (self.vertices[index] for index in triangle.vertices)
            ab = (b.x_mm - a.x_mm, b.y_mm - a.y_mm, b.z_mm - a.z_mm)
            ac = (c.x_mm - a.x_mm, c.y_mm - a.y_mm, c.z_mm - a.z_mm)
            cross = (
                ab[1] * ac[2] - ab[2] * ac[1],
                ab[2] * ac[0] - ab[0] * ac[2],
                ab[0] * ac[1] - ab[1] * ac[0],
            )
            cross_magnitude = math.sqrt(sum(value * value for value in cross))
            edge_lengths = (
                math.dist((a.x_mm, a.y_mm, a.z_mm), (b.x_mm, b.y_mm, b.z_mm)),
                math.dist((b.x_mm, b.y_mm, b.z_mm), (c.x_mm, c.y_mm, c.z_mm)),
                math.dist((c.x_mm, c.y_mm, c.z_mm), (a.x_mm, a.y_mm, a.z_mm)),
            )
            minimum_altitude = cross_magnitude / max(edge_lengths)
            if min(edge_lengths) <= MESH_TOLERANCE_MM or minimum_altitude <= MESH_TOLERANCE_MM:
                raise ValueError(
                    "mesh triangle edges and minimum altitude must exceed mesh tolerance"
                )
            for start, end in zip(
                triangle.vertices,
                (*triangle.vertices[1:], triangle.vertices[0]),
            ):
                directed_edges[(start, end)] = directed_edges.get((start, end), 0) + 1
                edge = tuple(sorted((start, end)))
                undirected_edges[edge] = undirected_edges.get(edge, 0) + 1
        if any(count > 2 for count in undirected_edges.values()):
            raise ValueError("mesh edges cannot belong to more than two triangles")
        faces = [frozenset(item.vertices) for item in self.triangles]
        if len(faces) != len(set(faces)):
            raise ValueError("mesh faces must be unique regardless of winding")
        if self.watertight:
            if any(count != 2 for count in undirected_edges.values()):
                raise ValueError("watertight meshes require exactly two triangles per edge")
            if any(
                directed_edges.get((left, right), 0) != 1
                or directed_edges.get((right, left), 0) != 1
                for left, right in undirected_edges
            ):
                raise ValueError("watertight mesh faces must use consistent outward winding")
            remaining = set(range(len(self.triangles)))
            edge_faces: dict[tuple[int, int], list[int]] = {}
            for index, triangle in enumerate(self.triangles):
                for start, end in zip(
                    triangle.vertices,
                    (*triangle.vertices[1:], triangle.vertices[0]),
                ):
                    edge_faces.setdefault(tuple(sorted((start, end))), []).append(index)
            while remaining:
                pending = [remaining.pop()]
                component: set[int] = set(pending)
                while pending:
                    face_index = pending.pop()
                    triangle = self.triangles[face_index]
                    for start, end in zip(
                        triangle.vertices,
                        (*triangle.vertices[1:], triangle.vertices[0]),
                    ):
                        for neighbor in edge_faces[tuple(sorted((start, end)))]:
                            if neighbor not in component:
                                component.add(neighbor)
                                remaining.discard(neighbor)
                                pending.append(neighbor)
                signed_volume = 0.0
                for face_index in component:
                    a, b, c = (
                        self.vertices[index] for index in self.triangles[face_index].vertices
                    )
                    signed_volume += (
                        a.x_mm * (b.y_mm * c.z_mm - b.z_mm * c.y_mm)
                        - a.y_mm * (b.x_mm * c.z_mm - b.z_mm * c.x_mm)
                        + a.z_mm * (b.x_mm * c.y_mm - b.y_mm * c.x_mm)
                    ) / 6.0
                if signed_volume <= MESH_TOLERANCE_MM**3:
                    raise ValueError(
                        "each watertight mesh component must have positive outward volume"
                    )
        return self


class Part(IdentifiedGeometryModel):
    id_kind = "part"
    name: str = Field(min_length=1, max_length=120)
    role: Literal["base", "artwork", "support"]
    mesh_id: str = Field(pattern=r"^mesh_[0-9a-f]{24}$")
    material_id: str = Field(pattern=r"^material_[0-9a-f]{24}$")
    source_geometry_kind: Literal["base", "island"]
    source_geometry_id: str = Field(pattern=r"^(?:base|island)_[0-9a-f]{24}$")
    source_label_id: Optional[str] = Field(
        default=None,
        pattern=r"^source-label_[0-9a-f]{24}$",
    )

    _canonical_name = field_validator("name")(_canonical_text)

    @model_validator(mode="after")
    def source_is_present_for_non_base_parts(self) -> Part:
        if self.role == "base":
            if self.source_geometry_kind != "base" or not self.source_geometry_id.startswith(
                "base_"
            ):
                raise ValueError("base parts require base-shape provenance")
            if self.source_label_id is not None:
                raise ValueError("base parts cannot claim an artwork source label")
        elif self.source_label_id is None:
            raise ValueError("artwork and support parts require a source label")
        elif self.source_geometry_kind != "island" or not self.source_geometry_id.startswith(
            "island_"
        ):
            raise ValueError("artwork and support parts require island provenance")
        return self


class RectangleBase(IdentifiedGeometryModel):
    id_kind = "base"
    kind: Literal["rectangle"] = "rectangle"
    center: Point2
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    corner_radius_mm: float = Field(default=0, ge=0)

    _normalize_width = field_validator("width_mm")(_canonical_mm)
    _normalize_height = field_validator("height_mm")(_canonical_mm)
    _normalize_radius = field_validator("corner_radius_mm")(_canonical_mm)

    @model_validator(mode="after")
    def radius_fits(self) -> RectangleBase:
        if self.corner_radius_mm > min(self.width_mm, self.height_mm) / 2:
            raise ValueError("rectangle corner radius exceeds half its shortest side")
        return self


class CircleBase(IdentifiedGeometryModel):
    id_kind = "base"
    kind: Literal["circle"] = "circle"
    center: Point2
    radius_mm: float = Field(gt=0)

    _normalize_radius = field_validator("radius_mm")(_canonical_mm)


class CustomBase(IdentifiedGeometryModel):
    id_kind = "base"
    kind: Literal["custom"] = "custom"
    exterior_contour_id: str = Field(pattern=r"^contour_[0-9a-f]{24}$")
    hole_contour_ids: tuple[str, ...] = ()

    @field_validator("hole_contour_ids")
    @classmethod
    def holes_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("custom base hole contour IDs must be unique and sorted")
        return value


BaseShape = Annotated[
    Union[RectangleBase, CircleBase, CustomBase],
    Field(discriminator="kind"),
]


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_REQUESTED = "not_requested"
    FAILED = "failed"


class CapabilityState(GeometryModel):
    status: CapabilityStatus
    reason: Optional[str] = Field(default=None, min_length=1, max_length=500)
    artifact_sha256: Optional[str] = Field(default=None, pattern=SHA256_PATTERN)

    @field_validator("reason")
    @classmethod
    def canonical_reason(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _canonical_text(value)

    @model_validator(mode="after")
    def status_has_honest_evidence(self) -> CapabilityState:
        if self.status == CapabilityStatus.AVAILABLE and self.reason is not None:
            raise ValueError("available capabilities cannot carry an unavailable reason")
        if self.status == CapabilityStatus.AVAILABLE and self.artifact_sha256 is None:
            raise ValueError("available capabilities require an artifact fingerprint")
        if (
            self.status in (CapabilityStatus.UNAVAILABLE, CapabilityStatus.FAILED)
            and not self.reason
        ):
            raise ValueError("unavailable and failed capabilities require a reason")
        if self.status != CapabilityStatus.AVAILABLE and self.artifact_sha256 is not None:
            raise ValueError("only available capabilities may reference an artifact")
        return self


class GeometryCapabilities(GeometryModel):
    geometry_ir: CapabilityState
    vector_geometry: CapabilityState
    topology: CapabilityState
    mesh: CapabilityState
    package_3mf: CapabilityState
    slicer_validation: CapabilityState
    download: CapabilityState

    @model_validator(mode="after")
    def capabilities_follow_pipeline(self) -> GeometryCapabilities:
        ordered = (
            self.geometry_ir,
            self.vector_geometry,
            self.topology,
            self.mesh,
            self.package_3mf,
            self.slicer_validation,
            self.download,
        )
        unavailable_seen = False
        for state in ordered:
            if unavailable_seen and state.status == CapabilityStatus.AVAILABLE:
                raise ValueError("downstream capabilities cannot be available before dependencies")
            if state.status != CapabilityStatus.AVAILABLE:
                unavailable_seen = True
        return self


class GeometryDocument(GeometryModel):
    schema_version: Literal[1]
    contract: GeometryContract = GeometryContract()
    source_asset_sha256: str = Field(pattern=SHA256_PATTERN)
    processed_labels_sha256: str = Field(pattern=SHA256_PATTERN)
    source_width_px: int = Field(gt=0)
    source_height_px: int = Field(gt=0)
    canvas_width_mm: float = Field(gt=0)
    canvas_height_mm: float = Field(gt=0)
    palette_color_order: tuple[str, ...] = Field(default=(), max_length=8)
    paths: tuple[Path2D, ...] = Field(default=(), max_length=MAX_DOCUMENT_ENTITIES)
    contours: tuple[Contour2D, ...] = Field(default=(), max_length=MAX_DOCUMENT_ENTITIES)
    materials: tuple[Material, ...] = Field(default=(), max_length=MAX_DOCUMENT_ENTITIES)
    source_labels: tuple[SourceLabel, ...] = Field(default=(), max_length=MAX_DOCUMENT_ENTITIES)
    islands: tuple[Island2D, ...] = Field(default=(), max_length=MAX_DOCUMENT_ENTITIES)
    shared_edges: tuple[SharedEdge, ...] = Field(default=(), max_length=MAX_DOCUMENT_ENTITIES)
    meshes: tuple[Mesh, ...] = Field(default=(), max_length=MAX_DOCUMENT_ENTITIES)
    parts: tuple[Part, ...] = Field(default=(), max_length=MAX_DOCUMENT_ENTITIES)
    base: BaseShape
    capabilities: GeometryCapabilities

    _normalize_width = field_validator("canvas_width_mm")(_canonical_mm)
    _normalize_height = field_validator("canvas_height_mm")(_canonical_mm)

    @model_validator(mode="after")
    def graph_is_canonical_and_closed(self) -> GeometryDocument:
        collections = (
            self.paths,
            self.contours,
            self.materials,
            self.source_labels,
            self.islands,
            self.shared_edges,
            self.meshes,
            self.parts,
        )
        all_ids: list[str] = []
        for collection in collections:
            ids = tuple(item.id for item in collection)
            if ids != tuple(sorted(ids)):
                raise ValueError("geometry collections must use canonical ID ordering")
            for item in collection:
                if item.id != item.expected_id():
                    raise ValueError(f"geometry content ID mismatch for {item.id}")
            all_ids.extend(ids)
        if self.base.id != self.base.expected_id():
            raise ValueError("geometry content ID mismatch for base")
        all_ids.append(self.base.id)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("geometry IDs must be globally unique")

        paths = {item.id: item for item in self.paths}
        contours = {item.id: item for item in self.contours}
        materials = {item.id: item for item in self.materials}
        labels = {item.id: item for item in self.source_labels}
        islands = {item.id: item for item in self.islands}
        meshes = {item.id for item in self.meshes}
        label_keys = tuple((item.source_asset_id, item.label_index) for item in self.source_labels)
        if len(label_keys) != len(set(label_keys)):
            raise ValueError("source asset label indices must be unique")
        palette_materials: dict[str, str] = {}
        for material in self.materials:
            previous = palette_materials.setdefault(material.palette_color_id, material.id)
            if previous != material.id:
                raise ValueError("each palette color must map to exactly one material")
        if self.palette_color_order:
            if len(self.palette_color_order) != len(set(self.palette_color_order)):
                raise ValueError("palette color order must be unique")
            if set(self.palette_color_order) != set(palette_materials):
                raise ValueError("palette color order must exactly cover document materials")
        for label in self.source_labels:
            if label.processed_labels_sha256 != self.processed_labels_sha256:
                raise ValueError(
                    "source labels must reference the authoritative processed-label artifact"
                )
            material = materials.get(label.material_id)
            if material is None or material.palette_color_id != label.palette_color_id:
                raise ValueError("every source label must map to its palette color and material")

        used_paths: set[str] = set()
        used_contours: set[str] = set()
        for contour in self.contours:
            path = paths.get(contour.path_id)
            if path is None:
                raise ValueError("contour references a missing path")
            if contour.path_id in used_paths:
                raise ValueError("boundary paths may belong to only one contour")
            used_paths.add(contour.path_id)
            contour.validate_path(path)

        base_contours: set[str] = set()
        if isinstance(self.base, CustomBase):
            exterior = contours.get(self.base.exterior_contour_id)
            if exterior is None or exterior.role != "exterior":
                raise ValueError("custom base references a missing exterior contour")
            base_contours.add(exterior.id)
            for hole_id in self.base.hole_contour_ids:
                hole = contours.get(hole_id)
                if hole is None or hole.role != "hole" or hole.parent_contour_id != exterior.id:
                    raise ValueError("custom base references an invalid hole contour")
                base_contours.add(hole_id)

        island_paths: dict[str, tuple[Path2D, tuple[Path2D, ...]]] = {}
        for island in self.islands:
            exterior = contours.get(island.exterior_contour_id)
            if exterior is None or exterior.role != "exterior":
                raise ValueError("island references a missing exterior contour")
            if island.exterior_contour_id in used_contours:
                raise ValueError("exterior contours may belong to only one island")
            used_contours.add(island.exterior_contour_id)
            exterior_path = paths[exterior.path_id]
            exterior_vertices = {_point_key(point) for point in _path_vertices(exterior_path)}
            exterior_edges = _path_edges(exterior_path)
            exterior_edge_bounds = tuple(_edge_bounds(edge) for edge in exterior_edges)
            exterior_index = BoundsIndex(exterior_edge_bounds, tolerance=TOPOLOGY_TOLERANCE_MM)
            hole_paths: list[Path2D] = []
            hole_bounds: list[tuple[float, float, float, float]] = []
            hole_edges_by_path: list[tuple[tuple[Point2, Point2], ...]] = []
            hole_edge_bounds_by_path: list[tuple[tuple[float, float, float, float], ...]] = []
            for hole_id in island.hole_contour_ids:
                hole = contours.get(hole_id)
                if hole is None or hole.role != "hole":
                    raise ValueError("island references a missing hole contour")
                if hole.parent_contour_id != island.exterior_contour_id:
                    raise ValueError("hole contour parent does not match island exterior")
                if hole_id in used_contours:
                    raise ValueError("hole contours may belong to only one island")
                used_contours.add(hole_id)
                hole_path = paths[hole.path_id]
                vertices = _path_vertices(hole_path)
                representative = next(
                    (point for point in vertices if _point_key(point) not in exterior_vertices),
                    vertices[0],
                )
                current_edges = _path_edges(hole_path)
                current_edge_bounds = tuple(_edge_bounds(edge) for edge in current_edges)
                crosses_exterior = any(
                    _proper_segments_intersect(*hole_edge, *exterior_edges[index])
                    or _collinear_overlap_edge(*hole_edge, *exterior_edges[index]) is not None
                    for hole_edge, hole_edge_bounds in zip(current_edges, current_edge_bounds)
                    for index in exterior_index.query(hole_edge_bounds)
                )
                if (
                    not _point_strictly_inside_polygon(representative, exterior_path)
                    or crosses_exterior
                ):
                    raise ValueError(
                        "island holes must lie strictly inside their exterior except at "
                        "isolated shared vertices"
                    )
                current_bounds = _path_bounds(hole_path)
                for other, other_bounds, other_edges, other_edge_bounds in zip(
                    hole_paths,
                    hole_bounds,
                    hole_edges_by_path,
                    hole_edge_bounds_by_path,
                ):
                    if not _bounds_overlap(current_bounds, other_bounds):
                        continue
                    other_index = BoundsIndex(other_edge_bounds, tolerance=TOPOLOGY_TOLERANCE_MM)
                    if _polygons_have_area_overlap(hole_path, other) or any(
                        _proper_segments_intersect(*left, *other_edges[index])
                        or _collinear_overlap_edge(*left, *other_edges[index]) is not None
                        for left, left_bounds in zip(current_edges, current_edge_bounds)
                        for index in other_index.query(left_bounds)
                    ):
                        raise ValueError("island holes may meet only at isolated shared vertices")
                hole_paths.append(hole_path)
                hole_bounds.append(current_bounds)
                hole_edges_by_path.append(current_edges)
                hole_edge_bounds_by_path.append(current_edge_bounds)
            if island.material_id not in materials or island.source_label_id not in labels:
                raise ValueError("island references a missing material or source label")
            if labels[island.source_label_id].material_id != island.material_id:
                raise ValueError("island material must match its source-label mapping")
            island_paths[island.id] = (exterior_path, tuple(hole_paths))
        if {item.source_label_id for item in self.islands} != set(labels):
            raise ValueError("every source label must be represented by at least one island")
        if used_contours & base_contours:
            raise ValueError("custom base contours cannot also belong to an island")
        if used_contours | base_contours != set(contours):
            raise ValueError("every contour must belong to exactly one island or custom base")

        island_edges: dict[str, tuple[tuple[Point2, Point2], ...]] = {}
        island_edge_bounds: dict[str, tuple[tuple[float, float, float, float], ...]] = {}
        island_bounds: dict[str, tuple[float, float, float, float]] = {}
        island_hole_bounds: dict[str, tuple[tuple[float, float, float, float], ...]] = {}
        island_samples: dict[str, Optional[Point2]] = {}
        for island_id, (exterior_path, hole_paths) in island_paths.items():
            edges = tuple(
                edge for path in (exterior_path, *hole_paths) for edge in _path_edges(path)
            )
            island_edges[island_id] = edges
            island_edge_bounds[island_id] = tuple(_edge_bounds(edge) for edge in edges)
            island_bounds[island_id] = _path_bounds(exterior_path)
            island_hole_bounds[island_id] = tuple(_path_bounds(hole) for hole in hole_paths)
            island_samples[island_id] = _filled_path_sample(
                exterior_path,
                hole_paths,
                island_hole_bounds[island_id],
            )

        derived_shared: dict[tuple[float, float, float, float], tuple[str, str]] = {}
        island_ids = tuple(island_paths)
        active: list[str] = []
        candidate_pairs: set[tuple[str, str]] = set()
        for island_id in sorted(
            island_ids,
            key=lambda item: (
                island_bounds[item][0],
                island_bounds[item][2],
                item,
            ),
        ):
            minimum_x = island_bounds[island_id][0]
            active = [
                other
                for other in active
                if island_bounds[other][2] >= minimum_x - TOPOLOGY_TOLERANCE_MM
            ]
            candidate_pairs.update(
                tuple(sorted((other, island_id)))
                for other in active
                if _bounds_overlap(island_bounds[other], island_bounds[island_id])
            )
            active.append(island_id)
        ordered_pairs = tuple(sorted(candidate_pairs))
        for left_id, right_id in ordered_pairs:
            left_exterior, left_holes = island_paths[left_id]
            right_exterior, right_holes = island_paths[right_id]
            left_candidates = (
                (edge, bounds)
                for edge, bounds in zip(island_edges[left_id], island_edge_bounds[left_id])
                if _bounds_overlap(bounds, island_bounds[right_id])
            )
            right_candidates = tuple(
                (edge, bounds)
                for edge, bounds in zip(island_edges[right_id], island_edge_bounds[right_id])
                if _bounds_overlap(bounds, island_bounds[left_id])
            )
            right_index = BoundsIndex(
                tuple(bounds for _, bounds in right_candidates), tolerance=TOPOLOGY_TOLERANCE_MM
            )
            for left_edge, left_edge_bounds in left_candidates:
                for index in right_index.query(left_edge_bounds):
                    right_edge, _ = right_candidates[index]
                    overlap = _collinear_overlap_edge(*left_edge, *right_edge)
                    if overlap is not None:
                        key = _canonical_edge_key(*overlap)
                        adjacent = tuple(sorted((left_id, right_id)))
                        previous = derived_shared.get(key)
                        if previous is not None and previous != adjacent:
                            raise ValueError("an edge cannot be shared by more than two islands")
                        derived_shared[key] = adjacent
                        continue
                    if _proper_segments_intersect(*left_edge, *right_edge):
                        raise ValueError("island filled regions cannot overlap")

            left_test_points = (
                (island_samples[left_id],)
                if island_samples[left_id] is not None
                else _path_vertices(left_exterior)
            )
            right_test_points = (
                (island_samples[right_id],)
                if island_samples[right_id] is not None
                else _path_vertices(right_exterior)
            )
            if any(
                _point_in_filled_paths(
                    point,
                    right_exterior,
                    right_holes,
                    island_hole_bounds[right_id],
                )
                for point in left_test_points
            ) or any(
                _point_in_filled_paths(
                    point,
                    left_exterior,
                    left_holes,
                    island_hole_bounds[left_id],
                )
                for point in right_test_points
            ):
                raise ValueError("island filled regions cannot overlap")

        supplied_shared: dict[tuple[float, float, float, float], tuple[str, str]] = {}
        for edge in self.shared_edges:
            if any(item not in islands for item in edge.adjacent_island_ids):
                raise ValueError("shared edge references a missing island")
            key = _canonical_edge_key(edge.start, edge.end)
            if key in supplied_shared:
                raise ValueError("shared edge geometry must be unique")
            supplied_shared[key] = edge.adjacent_island_ids
        if supplied_shared != derived_shared:
            raise ValueError(
                "shared edge declarations must exactly cover every shared island boundary"
            )

        mesh_owners: dict[str, str] = {}
        island_part_owners: set[str] = set()
        base_part_count = 0
        for part in self.parts:
            if part.mesh_id not in meshes or part.material_id not in materials:
                raise ValueError("part references a missing mesh or material")
            if part.source_label_id is not None and part.source_label_id not in labels:
                raise ValueError("part references a missing source label")
            if part.mesh_id in mesh_owners:
                raise ValueError("each mesh must belong to exactly one part")
            mesh_owners[part.mesh_id] = part.id
            if part.role == "base":
                if part.source_geometry_id != self.base.id:
                    raise ValueError("base part provenance must reference the document base")
                base_part_count += 1
                continue
            island = islands.get(part.source_geometry_id)
            if island is None:
                raise ValueError("part provenance references a missing island")
            if part.source_geometry_id in island_part_owners:
                raise ValueError("each island must produce exactly one part")
            island_part_owners.add(part.source_geometry_id)
            if (
                part.material_id != island.material_id
                or part.source_label_id != island.source_label_id
            ):
                raise ValueError("part material and label must match its source island")
            classification = labels[island.source_label_id].classification
            expected_role = "support" if classification == "support" else "artwork"
            if part.role != expected_role:
                raise ValueError("part role must match source-label classification")

        if self.capabilities.geometry_ir.status != CapabilityStatus.AVAILABLE:
            raise ValueError(
                "a validated geometry document must advertise geometry IR availability"
            )
        vector_available = self.capabilities.vector_geometry.status == CapabilityStatus.AVAILABLE
        vector_content = bool(self.paths)
        if vector_available != vector_content:
            raise ValueError("vector-geometry capability must agree with vector path content")
        topology_available = self.capabilities.topology.status == CapabilityStatus.AVAILABLE
        topology_content = bool(self.contours or self.islands or self.shared_edges)
        if topology_available != topology_content:
            raise ValueError("topology capability must agree with contour and island content")
        mesh_available = self.capabilities.mesh.status == CapabilityStatus.AVAILABLE
        if mesh_available:
            if not self.meshes or not self.parts:
                raise ValueError("available mesh capability requires mesh and part evidence")
            if set(mesh_owners) != set(meshes):
                raise ValueError("every mesh must belong to exactly one part")
            if base_part_count != 1 or island_part_owners != set(islands):
                raise ValueError("mesh output requires one base part and one part per island")
        elif self.meshes or self.parts:
            raise ValueError("mesh entities require an available mesh capability")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    def canonical_json(self) -> str:
        return self.canonical_bytes().decode("utf-8")

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def cache_key(self, *dependency_fingerprints: str) -> str:
        return geometry_cache_key(self, dependency_fingerprints)


def geometry_cache_key(
    document: GeometryDocument,
    dependency_fingerprints: tuple[str, ...] = (),
) -> str:
    if dependency_fingerprints != tuple(sorted(set(dependency_fingerprints))):
        raise ValueError("geometry cache dependencies must be unique and sorted")
    if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in dependency_fingerprints):
        raise ValueError("geometry cache dependencies must be SHA-256 fingerprints")
    payload = {
        "dependencies": dependency_fingerprints,
        "geometry_fingerprint": document.fingerprint(),
        "namespace": "image23mf-geometry-ir-v1",
    }
    return f"image23mf-geometry-ir-v1:{hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()}"


def geometry_ir_equal(left: GeometryDocument, right: GeometryDocument) -> bool:
    return left.canonical_bytes() == right.canonical_bytes()
