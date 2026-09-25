"""Deterministic geometry-to-label rasterization and pre-export difference evidence.

The authoritative processed label field uses an upper-left pixel origin. Geometry IR uses
millimetres from the lower-left build-plate origin. This module is the explicit audit seam
between those contracts: it rasterizes either canonical vector islands or the top faces of
their generated meshes at source resolution, then reports every geometric or material-color
difference without relying on a renderer or slicer preview.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.labels import LabelField
from image23mf.engine.regions import PixelBounds, RegionAnalysis
from image23mf.geometry.model import GeometryDocument, LineSegment, Path2D, SourceLabel

GEOMETRY_DIFFERENCE_SCHEMA_VERSION = 1
MAX_RASTER_PIXELS = 16_777_216
MAX_RASTER_LABELS = 64
UNASSIGNED_LABEL = -1
OVERLAP_LABEL = -2

DifferenceStatus = Literal["match", "warning", "error"]
RasterSource = Literal["vector", "mesh"]


class GeometryRasterizationError(ValueError):
    """Raised when geometry cannot be compared honestly with its source labels."""


class DifferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class GeometryDifferenceThresholds(DifferenceModel):
    """Materiality gates; warning values are inclusive lower bounds above exact zero."""

    warning_mismatch_ratio: float = Field(default=0.0, ge=0, le=1)
    error_mismatch_ratio: float = Field(default=0.005, gt=0, le=1)
    warning_area_delta_ratio: float = Field(default=0.0, ge=0, le=1)
    error_area_delta_ratio: float = Field(default=0.005, gt=0, le=1)
    warning_boundary_error_px: float = Field(default=0.0, ge=0)
    error_boundary_error_px: float = Field(default=1.0, gt=0)
    material_color_change_is_error: bool = True

    @model_validator(mode="after")
    def error_thresholds_exceed_warning_thresholds(self) -> GeometryDifferenceThresholds:
        pairs = (
            (self.warning_mismatch_ratio, self.error_mismatch_ratio, "mismatch ratio"),
            (self.warning_area_delta_ratio, self.error_area_delta_ratio, "area delta ratio"),
            (
                self.warning_boundary_error_px,
                self.error_boundary_error_px,
                "boundary error",
            ),
        )
        for warning, error, name in pairs:
            if error <= warning:
                raise ValueError(f"error {name} threshold must exceed its warning threshold")
        return self


class GeometryLabelDifference(DifferenceModel):
    label_index: int = Field(ge=0, le=255)
    source_label_id: str = Field(pattern=r"^source-label_[0-9a-f]{24}$")
    material_id: str = Field(pattern=r"^material_[0-9a-f]{24}$")
    expected_color_hex: str = Field(pattern=r"^#[0-9A-F]{6}$")
    actual_color_hex: str = Field(pattern=r"^#[0-9A-F]{6}$")
    color_matches: bool
    expected_pixel_count: int = Field(ge=0)
    actual_pixel_count: int = Field(ge=0)
    matched_pixel_count: int = Field(ge=0)
    false_positive_pixel_count: int = Field(ge=0)
    false_negative_pixel_count: int = Field(ge=0)
    expected_area_mm2: float = Field(ge=0)
    actual_area_mm2: float = Field(ge=0)
    signed_area_delta_mm2: float
    area_delta_ratio: float = Field(ge=0)
    intersection_over_union: float = Field(ge=0, le=1)
    boundary_max_error_px: float = Field(ge=0)
    status: DifferenceStatus


class GeometryRasterLabelCount(DifferenceModel):
    label_index: int = Field(ge=0, le=255)
    pixel_count: int = Field(gt=0)


class GeometryRegionDifference(DifferenceModel):
    region_id: str = Field(pattern=r"^region_[0-9a-f]{24}$")
    source_label_id: str = Field(pattern=r"^source-label_[0-9a-f]{24}$")
    expected_label_index: int = Field(ge=0, le=255)
    expected_pixel_count: int = Field(gt=0)
    mismatch_pixel_count: int = Field(gt=0)
    mismatch_ratio: float = Field(gt=0, le=1)
    wrong_label_pixel_count: int = Field(ge=0)
    unassigned_pixel_count: int = Field(ge=0)
    overlap_pixel_count: int = Field(ge=0)
    material_color_change_pixel_count: int = Field(ge=0)
    rasterized_label_counts: tuple[GeometryRasterLabelCount, ...]
    mismatch_bounds: PixelBounds
    status: DifferenceStatus


class GeometryDifferenceSummary(DifferenceModel):
    total_pixel_count: int = Field(gt=0)
    matched_pixel_count: int = Field(ge=0)
    mismatch_pixel_count: int = Field(ge=0)
    wrong_label_pixel_count: int = Field(ge=0)
    unassigned_pixel_count: int = Field(ge=0)
    overlap_pixel_count: int = Field(ge=0)
    material_color_change_pixel_count: int = Field(ge=0)
    mismatch_ratio: float = Field(ge=0, le=1)
    maximum_label_area_delta_ratio: float = Field(ge=0)
    maximum_boundary_error_px: float = Field(ge=0)
    affected_region_count: int = Field(ge=0)
    status: DifferenceStatus


class GeometryRasterTransform(DifferenceModel):
    """Exact affine from Geometry IR millimetres to source pixel-edge coordinates."""

    x_scale_px_per_mm: float = Field(gt=0)
    y_scale_px_per_mm: float = Field(lt=0)
    translate_x_px: Literal[0.0] = 0.0
    translate_y_px: float = Field(gt=0)


class GeometryDifferenceReport(DifferenceModel):
    schema_version: Literal[1] = GEOMETRY_DIFFERENCE_SCHEMA_VERSION
    raster_source: RasterSource
    source_width_px: int = Field(gt=0)
    source_height_px: int = Field(gt=0)
    canvas_width_mm: float = Field(gt=0)
    canvas_height_mm: float = Field(gt=0)
    source_image_origin: Literal["upper_left"] = "upper_left"
    geometry_origin: Literal["lower_left_build_plate"] = "lower_left_build_plate"
    source_to_geometry_y: Literal["invert_about_canvas_height"] = "invert_about_canvas_height"
    sampling: Literal["source_pixel_centers"] = "source_pixel_centers"
    geometry_to_source_pixel_edges: GeometryRasterTransform
    boundary_error_method: Literal["symmetric_chebyshev_hausdorff_pixel_centers_v1"] = (
        "symmetric_chebyshev_hausdorff_pixel_centers_v1"
    )
    geometry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    processed_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry_raster_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    difference_mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    region_graph_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    thresholds: GeometryDifferenceThresholds
    summary: GeometryDifferenceSummary
    labels: tuple[GeometryLabelDifference, ...]
    affected_regions: tuple[GeometryRegionDifference, ...]

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GeometryRaster:
    """Canonical source-resolution assignment; -1 is absent and -2 is overlap."""

    source: RasterSource
    width: int
    height: int
    geometry_fingerprint: str
    assignment: np.ndarray

    def __post_init__(self) -> None:
        if self.assignment.shape != (self.height, self.width):
            raise ValueError("geometry raster dimensions do not match its assignment")
        if self.assignment.dtype != np.int16:
            raise ValueError("geometry raster assignment must use signed 16-bit labels")
        if self.assignment.flags.writeable:
            raise ValueError("geometry raster assignment must be read-only")
        values = np.unique(self.assignment)
        if np.any(values < OVERLAP_LABEL) or np.any(values > 255):
            raise ValueError("geometry raster contains an invalid label value")

    def encoded_assignment(self) -> bytes:
        """0=unassigned, 1..256=label+1, 65535=overlap, little-endian uint16."""

        encoded = self.assignment.astype(np.int32) + 1
        encoded[self.assignment == OVERLAP_LABEL] = 65535
        return encoded.astype("<u2", copy=False).tobytes(order="C")

    def fingerprint(self) -> str:
        header = json.dumps(
            {
                "encoding": "uint16-label-plus-one-row-major-le;65535=overlap",
                "geometry_fingerprint": self.geometry_fingerprint,
                "height": self.height,
                "source": self.source,
                "width": self.width,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(header + b"\n" + self.encoded_assignment()).hexdigest()


@dataclass(frozen=True)
class GeometryDifferenceResult:
    """Report plus one-byte visualization evidence for the Geometry view.

    Difference mask codes: 0 exact, 1 wrong label, 2 unassigned, 3 overlap,
    4 material color changed. The report fingerprints the mask, so a UI cannot
    accidentally display difference pixels from another geometry revision.
    """

    report: GeometryDifferenceReport
    raster: GeometryRaster
    difference_mask: bytes

    def __post_init__(self) -> None:
        if len(self.difference_mask) != self.raster.width * self.raster.height:
            raise ValueError("difference mask dimensions do not match geometry raster")
        if set(self.difference_mask) - {0, 1, 2, 3, 4}:
            raise ValueError("difference mask uses an unknown evidence code")
        if hashlib.sha256(self.difference_mask).hexdigest() != self.report.difference_mask_sha256:
            raise ValueError("difference mask fingerprint does not match report")
        if self.raster.fingerprint() != self.report.geometry_raster_sha256:
            raise ValueError("geometry raster fingerprint does not match report")


def rasterize_geometry(
    document: GeometryDocument,
    *,
    source: RasterSource,
) -> GeometryRaster:
    """Rasterize canonical vector islands or generated mesh top faces at source resolution."""

    _validate_raster_bounds(document)
    assignment = np.full(
        (document.source_height_px, document.source_width_px),
        UNASSIGNED_LABEL,
        dtype=np.int16,
    )
    labels = {item.id: item.label_index for item in document.source_labels}
    if source == "vector":
        _rasterize_vector(document, labels, assignment)
    elif source == "mesh":
        _rasterize_mesh(document, labels, assignment)
    else:  # pragma: no cover - Literal contract protects typed callers
        raise GeometryRasterizationError(f"unsupported geometry raster source: {source}")
    assignment.flags.writeable = False
    return GeometryRaster(
        source=source,
        width=document.source_width_px,
        height=document.source_height_px,
        geometry_fingerprint=document.fingerprint(),
        assignment=assignment,
    )


def compare_geometry_to_labels(
    document: GeometryDocument,
    authoritative: LabelField,
    *,
    source: RasterSource,
    thresholds: Optional[GeometryDifferenceThresholds] = None,
    regions: Optional[RegionAnalysis] = None,
) -> GeometryDifferenceResult:
    """Create deterministic, source-linked difference evidence before package export."""

    _validate_comparison_contract(document, authoritative, regions)
    thresholds = thresholds or GeometryDifferenceThresholds()
    raster = rasterize_geometry(document, source=source)
    expected = np.frombuffer(authoritative.pixels, dtype=np.uint8).reshape(
        authoritative.height, authoritative.width
    )
    actual = raster.assignment
    wrong = (actual >= 0) & (actual != expected)
    unassigned = actual == UNASSIGNED_LABEL
    overlap = actual == OVERLAP_LABEL

    labels_by_index = {item.label_index: item for item in document.source_labels}
    materials = {item.id: item for item in document.materials}
    material_changed = np.zeros(expected.shape, dtype=bool)
    for label_index, source_label in labels_by_index.items():
        if materials[source_label.material_id].color_hex != source_label.color_hex:
            material_changed |= (expected == label_index) & (actual == label_index)

    mismatch = wrong | unassigned | overlap | material_changed
    difference = np.zeros(expected.shape, dtype=np.uint8)
    difference[wrong] = 1
    difference[unassigned] = 2
    difference[overlap] = 3
    difference[material_changed] = 4
    difference_bytes = difference.tobytes(order="C")
    pixel_area = document.canvas_width_mm * document.canvas_height_mm / expected.size

    label_reports = tuple(
        _label_difference(
            label_index,
            labels_by_index[label_index],
            materials[labels_by_index[label_index].material_id].color_hex,
            expected,
            actual,
            pixel_area=pixel_area,
            thresholds=thresholds,
        )
        for label_index in sorted(labels_by_index)
    )
    region_reports = _region_differences(
        regions,
        labels_by_index,
        expected,
        actual,
        difference,
        thresholds,
    )
    mismatch_count = int(np.count_nonzero(mismatch))
    summary_status = _combine_statuses(
        *[item.status for item in label_reports],
        _metric_status(
            mismatch_count / expected.size,
            thresholds.warning_mismatch_ratio,
            thresholds.error_mismatch_ratio,
        ),
    )
    summary = GeometryDifferenceSummary(
        total_pixel_count=expected.size,
        matched_pixel_count=expected.size - mismatch_count,
        mismatch_pixel_count=mismatch_count,
        wrong_label_pixel_count=int(np.count_nonzero(wrong)),
        unassigned_pixel_count=int(np.count_nonzero(unassigned)),
        overlap_pixel_count=int(np.count_nonzero(overlap)),
        material_color_change_pixel_count=int(np.count_nonzero(material_changed)),
        mismatch_ratio=_metric(mismatch_count / expected.size),
        maximum_label_area_delta_ratio=max(
            (item.area_delta_ratio for item in label_reports), default=0.0
        ),
        maximum_boundary_error_px=max(
            (item.boundary_max_error_px for item in label_reports), default=0.0
        ),
        affected_region_count=len(region_reports),
        status=summary_status,
    )
    report = GeometryDifferenceReport(
        raster_source=source,
        source_width_px=document.source_width_px,
        source_height_px=document.source_height_px,
        canvas_width_mm=document.canvas_width_mm,
        canvas_height_mm=document.canvas_height_mm,
        geometry_to_source_pixel_edges=GeometryRasterTransform(
            x_scale_px_per_mm=document.source_width_px / document.canvas_width_mm,
            y_scale_px_per_mm=-document.source_height_px / document.canvas_height_mm,
            translate_y_px=document.source_height_px,
        ),
        geometry_fingerprint=document.fingerprint(),
        processed_labels_sha256=document.processed_labels_sha256,
        geometry_raster_sha256=raster.fingerprint(),
        difference_mask_sha256=hashlib.sha256(difference_bytes).hexdigest(),
        region_graph_fingerprint=None if regions is None else regions.graph.fingerprint(),
        thresholds=thresholds,
        summary=summary,
        labels=label_reports,
        affected_regions=region_reports,
    )
    return GeometryDifferenceResult(
        report=report,
        raster=raster,
        difference_mask=difference_bytes,
    )


def _validate_raster_bounds(document: GeometryDocument) -> None:
    pixels = document.source_width_px * document.source_height_px
    if pixels > MAX_RASTER_PIXELS:
        raise GeometryRasterizationError(
            f"geometry audit raster exceeds the {MAX_RASTER_PIXELS:,}-pixel safety limit"
        )
    if len(document.source_labels) > MAX_RASTER_LABELS:
        raise GeometryRasterizationError(
            f"geometry audit exceeds the {MAX_RASTER_LABELS}-label safety limit"
        )


def _validate_comparison_contract(
    document: GeometryDocument,
    authoritative: LabelField,
    regions: Optional[RegionAnalysis],
) -> None:
    if (authoritative.width, authoritative.height) != (
        document.source_width_px,
        document.source_height_px,
    ):
        raise GeometryRasterizationError(
            "processed labels must match the Geometry IR source raster dimensions"
        )
    actual_sha = hashlib.sha256(authoritative.pixels).hexdigest()
    if actual_sha != document.processed_labels_sha256:
        raise GeometryRasterizationError(
            "processed labels do not match the Geometry IR authoritative fingerprint"
        )
    expected_labels = tuple(sorted(item.label_index for item in document.source_labels))
    if tuple(sorted(authoritative.label_values)) != expected_labels:
        raise GeometryRasterizationError(
            "processed-label palette does not match Geometry IR source labels"
        )
    if regions is None:
        return
    if regions.assignment.shape != (authoritative.height, authoritative.width):
        raise GeometryRasterizationError("region assignment does not match processed labels")
    graph = regions.graph
    if (graph.width_px, graph.height_px) != (authoritative.width, authoritative.height):
        raise GeometryRasterizationError("region graph does not match processed labels")
    width_matches = math.isclose(graph.width_mm, document.canvas_width_mm, rel_tol=0, abs_tol=1e-9)
    height_matches = math.isclose(
        graph.height_mm, document.canvas_height_mm, rel_tol=0, abs_tol=1e-9
    )
    if not width_matches or not height_matches:
        raise GeometryRasterizationError("region graph physical canvas does not match Geometry IR")


def _rasterize_vector(
    document: GeometryDocument,
    labels: dict[str, int],
    assignment: np.ndarray,
) -> None:
    if not document.islands:
        raise GeometryRasterizationError("vector rasterization requires canonical islands")
    paths = {item.id: item for item in document.paths}
    contours = {item.id: item for item in document.contours}
    for island in document.islands:
        exterior = contours[island.exterior_contour_id]
        exterior_points = _path_pixel_points(document, paths[exterior.path_id])
        x0, y0, mask = _polygon_mask(
            exterior_points,
            width=document.source_width_px,
            height=document.source_height_px,
        )
        for hole_id in island.hole_contour_ids:
            hole = contours[hole_id]
            _erase_polygon(mask, _path_pixel_points(document, paths[hole.path_id]), x0=x0, y0=y0)
        _claim(assignment, mask, x0=x0, y0=y0, label=labels[island.source_label_id])


def _rasterize_mesh(
    document: GeometryDocument,
    labels: dict[str, int],
    assignment: np.ndarray,
) -> None:
    if not document.meshes or not document.parts:
        raise GeometryRasterizationError("mesh rasterization requires generated parts and meshes")
    meshes = {item.id: item for item in document.meshes}
    artwork = [item for item in document.parts if item.role != "base"]
    if not artwork:
        raise GeometryRasterizationError("mesh rasterization requires artwork or support parts")
    for part in sorted(artwork, key=lambda item: item.id):
        if part.source_label_id is None:  # pragma: no cover - Geometry IR enforces provenance
            raise GeometryRasterizationError("artwork mesh is missing source-label provenance")
        mesh = meshes[part.mesh_id]
        top_z = max(item.z_mm for item in mesh.vertices)
        label = labels[part.source_label_id]
        for triangle in mesh.triangles:
            vertices = tuple(mesh.vertices[index] for index in triangle.vertices)
            if not all(
                math.isclose(item.z_mm, top_z, rel_tol=0, abs_tol=1e-9) for item in vertices
            ):
                continue
            points = tuple(
                _geometry_pixel_point(document, item.x_mm, item.y_mm) for item in vertices
            )
            if abs(_signed_area(points)) <= 1e-12:
                continue
            x0, y0, mask = _triangle_mask(
                points,
                width=document.source_width_px,
                height=document.source_height_px,
            )
            _claim(assignment, mask, x0=x0, y0=y0, label=label)


def _path_pixel_points(
    document: GeometryDocument,
    path: Path2D,
) -> tuple[tuple[float, float], ...]:
    if any(not isinstance(segment, LineSegment) for segment in path.segments):
        raise GeometryRasterizationError("v1 raster audit supports linear boundary paths only")
    points = (path.start, *(segment.end for segment in path.segments[:-1]))
    return tuple(_geometry_pixel_point(document, item.x_mm, item.y_mm) for item in points)


def _geometry_pixel_point(
    document: GeometryDocument,
    x_mm: float,
    y_mm: float,
) -> tuple[float, float]:
    """Map lower-left geometry millimetres to upper-left continuous pixel edges."""

    return (
        x_mm * document.source_width_px / document.canvas_width_mm,
        document.source_height_px - y_mm * document.source_height_px / document.canvas_height_mm,
    )


def _polygon_mask(
    points: tuple[tuple[float, float], ...],
    *,
    width: int,
    height: int,
) -> tuple[int, int, np.ndarray]:
    x0, y0, x1, y1 = _pixel_center_bounds(points, width=width, height=height)
    mask = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    _fill_polygon(mask, points, x0=x0, y0=y0, value=True)
    return x0, y0, mask


def _erase_polygon(
    target: np.ndarray,
    points: tuple[tuple[float, float], ...],
    *,
    x0: int,
    y0: int,
) -> None:
    hole = np.zeros(target.shape, dtype=bool)
    _fill_polygon(hole, points, x0=x0, y0=y0, value=True)
    target &= ~hole


def _fill_polygon(
    target: np.ndarray,
    points: tuple[tuple[float, float], ...],
    *,
    x0: int,
    y0: int,
    value: bool,
) -> None:
    edges = tuple(zip(points, (*points[1:], points[0])))
    for local_y in range(target.shape[0]):
        sample_y = y0 + local_y + 0.5
        crossings = sorted(
            left[0] + (sample_y - left[1]) * (right[0] - left[0]) / (right[1] - left[1])
            for left, right in edges
            if (left[1] > sample_y) != (right[1] > sample_y)
        )
        if len(crossings) % 2:
            raise GeometryRasterizationError("polygon scanline produced an odd crossing count")
        for left, right in zip(crossings[::2], crossings[1::2]):
            start = max(0, math.ceil(left - 0.5) - x0)
            stop = min(target.shape[1], math.ceil(right - 0.5) - x0)
            if stop > start:
                target[local_y, start:stop] = value


def _triangle_mask(
    points: tuple[tuple[float, float], ...],
    *,
    width: int,
    height: int,
) -> tuple[int, int, np.ndarray]:
    x0, y0, x1, y1 = _pixel_center_bounds(points, width=width, height=height)
    xs = np.arange(x0, x1, dtype=np.float64) + 0.5
    ys = np.arange(y0, y1, dtype=np.float64) + 0.5
    grid_x, grid_y = np.meshgrid(xs, ys)
    signs = []
    for left, right in zip(points, (*points[1:], points[0])):
        signs.append(
            (grid_x - left[0]) * (right[1] - left[1]) - (grid_y - left[1]) * (right[0] - left[0])
        )
    if _signed_area(points) > 0:
        mask = np.logical_and.reduce(tuple(item <= 1e-9 for item in signs))
    else:
        mask = np.logical_and.reduce(tuple(item >= -1e-9 for item in signs))
    return x0, y0, mask


def _pixel_center_bounds(
    points: tuple[tuple[float, float], ...],
    *,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x0 = max(0, math.ceil(min(item[0] for item in points) - 0.5))
    y0 = max(0, math.ceil(min(item[1] for item in points) - 0.5))
    x1 = min(width, math.ceil(max(item[0] for item in points) - 0.5))
    y1 = min(height, math.ceil(max(item[1] for item in points) - 0.5))
    if x1 <= x0 or y1 <= y0:
        return x0, y0, x0, y0
    return x0, y0, x1, y1


def _signed_area(points: tuple[tuple[float, float], ...]) -> float:
    return (
        sum(
            left[0] * right[1] - right[0] * left[1]
            for left, right in zip(points, (*points[1:], points[0]))
        )
        / 2
    )


def _claim(
    assignment: np.ndarray,
    mask: np.ndarray,
    *,
    x0: int,
    y0: int,
    label: int,
) -> None:
    target = assignment[y0 : y0 + mask.shape[0], x0 : x0 + mask.shape[1]]
    collision = mask & (target >= 0) & (target != label)
    target[mask & (target == UNASSIGNED_LABEL)] = label
    target[collision] = OVERLAP_LABEL


def _label_difference(
    label_index: int,
    source_label: SourceLabel,
    material_color: str,
    expected: np.ndarray,
    actual: np.ndarray,
    *,
    pixel_area: float,
    thresholds: GeometryDifferenceThresholds,
) -> GeometryLabelDifference:
    expected_mask = expected == label_index
    actual_mask = actual == label_index
    intersection = int(np.count_nonzero(expected_mask & actual_mask))
    expected_count = int(np.count_nonzero(expected_mask))
    actual_count = int(np.count_nonzero(actual_mask))
    false_positive = actual_count - intersection
    false_negative = expected_count - intersection
    union = expected_count + actual_count - intersection
    area_delta = (actual_count - expected_count) * pixel_area
    area_delta_ratio = abs(actual_count - expected_count) / expected_count
    boundary_error = _symmetric_boundary_error(expected_mask, actual_mask)
    color_matches = material_color == source_label.color_hex
    mismatch_ratio = (false_positive + false_negative) / expected.size
    status = _combine_statuses(
        _metric_status(
            mismatch_ratio,
            thresholds.warning_mismatch_ratio,
            thresholds.error_mismatch_ratio,
        ),
        _metric_status(
            area_delta_ratio,
            thresholds.warning_area_delta_ratio,
            thresholds.error_area_delta_ratio,
        ),
        _metric_status(
            boundary_error,
            thresholds.warning_boundary_error_px,
            thresholds.error_boundary_error_px,
        ),
        (
            "match"
            if color_matches
            else ("error" if thresholds.material_color_change_is_error else "warning")
        ),
    )
    return GeometryLabelDifference(
        label_index=label_index,
        source_label_id=source_label.id,
        material_id=source_label.material_id,
        expected_color_hex=source_label.color_hex,
        actual_color_hex=material_color,
        color_matches=color_matches,
        expected_pixel_count=expected_count,
        actual_pixel_count=actual_count,
        matched_pixel_count=intersection if color_matches else 0,
        false_positive_pixel_count=false_positive,
        false_negative_pixel_count=false_negative,
        expected_area_mm2=_metric(expected_count * pixel_area),
        actual_area_mm2=_metric(actual_count * pixel_area),
        signed_area_delta_mm2=_metric(area_delta),
        area_delta_ratio=_metric(area_delta_ratio),
        intersection_over_union=_metric(intersection / union if union else 1.0),
        boundary_max_error_px=_metric(boundary_error),
        status=status,
    )


def _region_differences(
    regions: Optional[RegionAnalysis],
    labels_by_index: dict[int, SourceLabel],
    expected: np.ndarray,
    actual: np.ndarray,
    difference: np.ndarray,
    thresholds: GeometryDifferenceThresholds,
) -> tuple[GeometryRegionDifference, ...]:
    if regions is None:
        return ()
    output = []
    for index, region in enumerate(regions.graph.regions):
        region_mask = regions.assignment == index
        affected = region_mask & (difference != 0)
        count = int(np.count_nonzero(affected))
        if not count:
            continue
        ys, xs = np.nonzero(affected)
        values, counts = np.unique(actual[region_mask & (actual >= 0)], return_counts=True)
        codes = difference[affected]
        ratio = count / region.pixel_count
        output.append(
            GeometryRegionDifference(
                region_id=region.id,
                source_label_id=labels_by_index[region.label].id,
                expected_label_index=region.label,
                expected_pixel_count=region.pixel_count,
                mismatch_pixel_count=count,
                mismatch_ratio=_metric(ratio),
                wrong_label_pixel_count=int(np.count_nonzero(codes == 1)),
                unassigned_pixel_count=int(np.count_nonzero(codes == 2)),
                overlap_pixel_count=int(np.count_nonzero(codes == 3)),
                material_color_change_pixel_count=int(np.count_nonzero(codes == 4)),
                rasterized_label_counts=tuple(
                    GeometryRasterLabelCount(label_index=int(value), pixel_count=int(pixel_count))
                    for value, pixel_count in zip(values, counts)
                ),
                mismatch_bounds=PixelBounds(
                    x=int(xs.min()),
                    y=int(ys.min()),
                    width=int(xs.max() - xs.min() + 1),
                    height=int(ys.max() - ys.min() + 1),
                ),
                status=_metric_status(
                    ratio,
                    thresholds.warning_mismatch_ratio,
                    thresholds.error_mismatch_ratio,
                ),
            )
        )
    return tuple(sorted(output, key=lambda item: item.region_id))


def _boundary(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask, 1, constant_values=False)
    interior = (
        padded[1:-1, 1:-1]
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    return mask & ~interior


def _symmetric_boundary_error(expected: np.ndarray, actual: np.ndarray) -> float:
    expected_boundary = _boundary(expected)
    actual_boundary = _boundary(actual)
    if not np.any(expected_boundary) and not np.any(actual_boundary):
        return 0.0
    if not np.any(expected_boundary) or not np.any(actual_boundary):
        return float(max(expected.shape))
    return float(
        max(
            _directed_chebyshev_distance(expected_boundary, actual_boundary),
            _directed_chebyshev_distance(actual_boundary, expected_boundary),
        )
    )


def _directed_chebyshev_distance(source: np.ndarray, target: np.ndarray) -> int:
    if np.all(target[source]):
        return 0
    low = 0
    high = 1
    limit = max(source.shape)
    while high < limit and not np.all(_square_dilation(target, high)[source]):
        low = high
        high = min(limit, high * 2)
    while low + 1 < high:
        middle = (low + high) // 2
        if np.all(_square_dilation(target, middle)[source]):
            high = middle
        else:
            low = middle
    return high


def _square_dilation(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius == 0:
        return mask
    padded = np.pad(mask.astype(np.uint8), radius, constant_values=0)
    integral = np.pad(
        padded.astype(np.uint32).cumsum(axis=0).cumsum(axis=1),
        ((1, 0), (1, 0)),
        constant_values=0,
    )
    size = radius * 2 + 1
    windows = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    )
    return windows > 0


def _metric_status(value: float, warning: float, error: float) -> DifferenceStatus:
    if value >= error:
        return "error"
    if value > warning:
        return "warning"
    return "match"


def _combine_statuses(*statuses: DifferenceStatus) -> DifferenceStatus:
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "match"


def _metric(value: float) -> float:
    rounded = round(float(value), 9)
    return 0.0 if rounded == 0 else rounded
