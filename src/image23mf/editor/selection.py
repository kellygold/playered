"""Deterministic rasterization of canonical source-space editor selections."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from image23mf.contracts.editor import (
    CanvasSelectionState,
    SelectionCombineMode,
    SourceBrushSelection,
    SourceLassoSelection,
    SourceRectangleSelection,
    SourceRegionSelection,
)
from image23mf.engine.contours import physical_kernel_offsets
from image23mf.engine.transform import CanonicalTransform, CoordinateStage, Point


@dataclass(frozen=True)
class SelectionRaster:
    """Binary authority plus optional 8-bit feather preview for one working raster."""

    width: int
    height: int
    mask: bytes
    feather: bytes

    def __post_init__(self) -> None:
        size = self.width * self.height
        if self.width < 1 or self.height < 1 or len(self.mask) != size or len(self.feather) != size:
            raise ValueError("selection raster planes must match positive dimensions")
        if any(value not in {0, 1} for value in self.mask):
            raise ValueError("selection authority mask must be binary")

    @property
    def selected_pixel_count(self) -> int:
        return sum(self.mask)

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(f"selection-raster-v1:{self.width}x{self.height}:".encode())
        digest.update(self.mask)
        digest.update(self.feather)
        return digest.hexdigest()


def rasterize_canvas_selection(
    selection: CanvasSelectionState,
    transform: CanonicalTransform,
    *,
    region_masks: Mapping[str, bytes] | None = None,
) -> SelectionRaster:
    """Rasterize ordered add/subtract primitives onto the transform's working stage.

    Geometry is authored after EXIF normalization and is therefore independent of the current
    viewport camera. Region primitives use exact graph assignment masks supplied by the caller.
    Expansion and feather radii are physical millimetres, so anisotropic pixels remain honest.
    """

    if (
        selection.source_width_px != transform.normalized_size.width
        or selection.source_height_px != transform.normalized_size.height
    ):
        raise ValueError("selection source dimensions do not match the canonical transform")
    width = transform.working_size.width
    height = transform.working_size.height
    working_x, working_y = _working_pixel_centers(width, height)
    working_to_source = transform.matrix(CoordinateStage.WORKING, CoordinateStage.NORMALIZED)
    source_x = (
        working_to_source.m00 * working_x + working_to_source.m01 * working_y + working_to_source.tx
    )
    source_y = (
        working_to_source.m10 * working_x + working_to_source.m11 * working_y + working_to_source.ty
    )
    selected = np.zeros((height, width), dtype=bool)
    exact_region_masks = region_masks or {}

    for primitive in selection.primitives:
        if isinstance(primitive, SourceRectangleSelection):
            primitive_mask = (
                (source_x >= primitive.x)
                & (source_x < primitive.x + primitive.width)
                & (source_y >= primitive.y)
                & (source_y < primitive.y + primitive.height)
            )
        elif isinstance(primitive, SourceLassoSelection):
            primitive_mask = _polygon_mask(source_x, source_y, primitive)
        elif isinstance(primitive, SourceBrushSelection):
            primitive_mask = _brush_mask(working_x, working_y, primitive, transform)
        elif isinstance(primitive, SourceRegionSelection):
            primitive_mask = np.zeros((height, width), dtype=bool)
            for region_id in primitive.region_ids:
                try:
                    encoded = exact_region_masks[region_id]
                except KeyError as error:
                    raise ValueError(f"selection region mask is missing: {region_id}") from error
                if len(encoded) != width * height:
                    raise ValueError(f"selection region mask dimensions are invalid: {region_id}")
                primitive_mask |= (
                    np.frombuffer(encoded, dtype=np.uint8).reshape((height, width)) > 0
                )
        else:  # pragma: no cover - discriminated contract is exhaustive
            raise TypeError(f"unsupported selection primitive: {type(primitive).__name__}")

        if primitive.combine == SelectionCombineMode.ADD:
            selected |= primitive_mask
        else:
            selected &= ~primitive_mask

    pixel_width_mm = transform.canvas_size.width / width
    pixel_height_mm = transform.canvas_size.height / height
    if selection.expand_mm > 0:
        selected = _dilate(
            selected,
            physical_kernel_offsets(
                selection.expand_mm,
                pixel_width_mm=pixel_width_mm,
                pixel_height_mm=pixel_height_mm,
            ),
        )
    elif selection.expand_mm < 0:
        selected = _erode(
            selected,
            physical_kernel_offsets(
                abs(selection.expand_mm),
                pixel_width_mm=pixel_width_mm,
                pixel_height_mm=pixel_height_mm,
            ),
        )

    feather = _feather_plane(
        selected,
        selection.feather_mm,
        pixel_width_mm=pixel_width_mm,
        pixel_height_mm=pixel_height_mm,
    )
    return SelectionRaster(
        width=width,
        height=height,
        mask=selected.astype(np.uint8).tobytes(),
        feather=feather.tobytes(),
    )


def _working_pixel_centers(width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    y, x = np.indices((height, width), dtype=np.float64)
    return x + 0.5, y + 0.5


def _polygon_mask(
    source_x: np.ndarray,
    source_y: np.ndarray,
    primitive: SourceLassoSelection,
) -> np.ndarray:
    inside = np.zeros(source_x.shape, dtype=bool)
    points = primitive.points
    previous = points[-1]
    for current in points:
        crosses = (current.y > source_y) != (previous.y > source_y)
        denominator = previous.y - current.y
        if denominator != 0:
            boundary_x = (previous.x - current.x) * (source_y - current.y) / denominator + current.x
            inside ^= crosses & (source_x < boundary_x)
        previous = current
    return inside


def _brush_mask(
    working_x: np.ndarray,
    working_y: np.ndarray,
    primitive: SourceBrushSelection,
    transform: CanonicalTransform,
) -> np.ndarray:
    mm_per_x = transform.canvas_size.width / transform.working_size.width
    mm_per_y = transform.canvas_size.height / transform.working_size.height
    point_x = working_x * mm_per_x
    point_y = working_y * mm_per_y
    source_to_mm = transform.matrix(CoordinateStage.NORMALIZED, CoordinateStage.MILLIMETRES)
    path = tuple(source_to_mm.map_point(Point(x=item.x, y=item.y)) for item in primitive.points)
    result = np.zeros(working_x.shape, dtype=bool)
    if len(path) == 1:
        distance_squared = (point_x - path[0].x) ** 2 + (point_y - path[0].y) ** 2
        return distance_squared <= primitive.radius_mm**2
    for start, end in zip(path, path[1:]):
        dx = end.x - start.x
        dy = end.y - start.y
        length_squared = dx * dx + dy * dy
        if length_squared == 0:
            distance_squared = (point_x - start.x) ** 2 + (point_y - start.y) ** 2
        else:
            projection = np.clip(
                ((point_x - start.x) * dx + (point_y - start.y) * dy) / length_squared,
                0,
                1,
            )
            distance_squared = (point_x - (start.x + projection * dx)) ** 2 + (
                point_y - (start.y + projection * dy)
            ) ** 2
        result |= distance_squared <= primitive.radius_mm**2
    return result


def _dilate(mask: np.ndarray, offsets: tuple[tuple[int, int], ...]) -> np.ndarray:
    result = np.zeros(mask.shape, dtype=bool)
    for dy, dx in offsets:
        result |= _shift(mask, dy, dx)
    return result


def _erode(mask: np.ndarray, offsets: tuple[tuple[int, int], ...]) -> np.ndarray:
    result = np.ones(mask.shape, dtype=bool)
    for dy, dx in offsets:
        result &= _shift(mask, dy, dx)
    return result


def _shift(mask: np.ndarray, dy: int, dx: int) -> np.ndarray:
    result = np.zeros(mask.shape, dtype=bool)
    source_y = slice(max(0, -dy), min(mask.shape[0], mask.shape[0] - dy))
    source_x = slice(max(0, -dx), min(mask.shape[1], mask.shape[1] - dx))
    target_y = slice(max(0, dy), min(mask.shape[0], mask.shape[0] + dy))
    target_x = slice(max(0, dx), min(mask.shape[1], mask.shape[1] + dx))
    result[target_y, target_x] = mask[source_y, source_x]
    return result


def _feather_plane(
    mask: np.ndarray,
    radius_mm: float,
    *,
    pixel_width_mm: float,
    pixel_height_mm: float,
) -> np.ndarray:
    result = mask.astype(np.uint8) * 255
    if radius_mm == 0 or not np.any(mask):
        return result
    offsets = physical_kernel_offsets(
        radius_mm,
        pixel_width_mm=pixel_width_mm,
        pixel_height_mm=pixel_height_mm,
    )
    for dy, dx in offsets:
        distance = math.hypot(dx * pixel_width_mm, dy * pixel_height_mm)
        if distance == 0 or distance > radius_mm:
            continue
        weight = max(1, min(254, round(255 * (1 - distance / radius_mm))))
        shifted = _shift(mask, dy, dx)
        result = np.maximum(result, shifted.astype(np.uint8) * weight)
    return result
