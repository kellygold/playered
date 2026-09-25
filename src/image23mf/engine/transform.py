"""Versioned canonical mapping from source pixel boundaries to physical output."""

import math
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.contracts.job import CropConfig, CropMode
from image23mf.engine.ingestion import SourceImageMetadata

TRANSFORM_SCHEMA_VERSION = 1
COORDINATE_EPSILON = 1e-9


class TransformModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CoordinateStage(str, Enum):
    ORIGINAL = "original"
    NORMALIZED = "normalized"
    CROP = "crop"
    WORKING = "working"
    MILLIMETRES = "millimetres"


class DimensionAxis(str, Enum):
    WIDTH = "width"
    HEIGHT = "height"


class PixelSize(TransformModel):
    width: int = Field(ge=1, le=250_000)
    height: int = Field(ge=1, le=250_000)

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height


class MillimetreSize(TransformModel):
    width: float = Field(gt=0, le=10_000)
    height: float = Field(gt=0, le=10_000)

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height


class Point(TransformModel):
    x: float
    y: float


class Rect(TransformModel):
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def contains(self, point: Point, *, epsilon: float = COORDINATE_EPSILON) -> bool:
        return (
            self.x - epsilon <= point.x <= self.right + epsilon
            and self.y - epsilon <= point.y <= self.bottom + epsilon
        )

    def intersection(self, other: "Rect") -> "Rect":
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right - left <= COORDINATE_EPSILON or bottom - top <= COORDINATE_EPSILON:
            raise ValueError("rectangles do not have a positive-area intersection")
        return Rect(x=left, y=top, width=right - left, height=bottom - top)


class Affine2D(TransformModel):
    """2D affine matrix where x'=m00*x+m01*y+tx and y'=m10*x+m11*y+ty."""

    m00: float = 1
    m01: float = 0
    m10: float = 0
    m11: float = 1
    tx: float = 0
    ty: float = 0

    @classmethod
    def identity(cls) -> "Affine2D":
        return cls()

    @classmethod
    def scale(cls, x: float, y: float) -> "Affine2D":
        if x == 0 or y == 0:
            raise ValueError("affine scale must be nonzero")
        return cls(m00=x, m11=y)

    @classmethod
    def translate(cls, x: float, y: float) -> "Affine2D":
        return cls(tx=x, ty=y)

    def map_point(self, point: Point) -> Point:
        return Point(
            x=self.m00 * point.x + self.m01 * point.y + self.tx,
            y=self.m10 * point.x + self.m11 * point.y + self.ty,
        )

    def map_rect(self, rect: Rect) -> Rect:
        points = (
            self.map_point(Point(x=rect.x, y=rect.y)),
            self.map_point(Point(x=rect.right, y=rect.y)),
            self.map_point(Point(x=rect.x, y=rect.bottom)),
            self.map_point(Point(x=rect.right, y=rect.bottom)),
        )
        left = min(point.x for point in points)
        right = max(point.x for point in points)
        top = min(point.y for point in points)
        bottom = max(point.y for point in points)
        return Rect(x=left, y=top, width=right - left, height=bottom - top)

    def then(self, following: "Affine2D") -> "Affine2D":
        """Return the matrix that applies this transform, then ``following``."""
        return Affine2D(
            m00=following.m00 * self.m00 + following.m01 * self.m10,
            m01=following.m00 * self.m01 + following.m01 * self.m11,
            m10=following.m10 * self.m00 + following.m11 * self.m10,
            m11=following.m10 * self.m01 + following.m11 * self.m11,
            tx=following.m00 * self.tx + following.m01 * self.ty + following.tx,
            ty=following.m10 * self.tx + following.m11 * self.ty + following.ty,
        )

    def inverse(self) -> "Affine2D":
        determinant = self.m00 * self.m11 - self.m01 * self.m10
        if determinant == 0:
            raise ValueError("affine transform is singular")
        m00 = self.m11 / determinant
        m01 = -self.m01 / determinant
        m10 = -self.m10 / determinant
        m11 = self.m00 / determinant
        return Affine2D(
            m00=m00,
            m01=m01,
            m10=m10,
            m11=m11,
            tx=-(m00 * self.tx + m01 * self.ty),
            ty=-(m10 * self.tx + m11 * self.ty),
        )


class CanonicalTransform(TransformModel):
    """Serializable source-to-physical transform using continuous pixel-edge coordinates."""

    schema_version: Literal[1] = TRANSFORM_SCHEMA_VERSION
    original_size: PixelSize
    normalized_size: PixelSize
    exif_orientation: int = Field(default=1, ge=1, le=8)
    crop_rect: Rect
    fit_mode: CropMode
    working_size: PixelSize
    canvas_size: MillimetreSize

    @model_validator(mode="after")
    def validate_stage_geometry(self) -> "CanonicalTransform":
        expected = normalized_size_for_orientation(self.original_size, self.exif_orientation)
        if self.normalized_size != expected:
            raise ValueError(
                "normalized dimensions do not match original dimensions and EXIF orientation"
            )
        bounds = Rect(
            x=0,
            y=0,
            width=self.normalized_size.width,
            height=self.normalized_size.height,
        )
        if (
            self.crop_rect.x < -COORDINATE_EPSILON
            or self.crop_rect.y < -COORDINATE_EPSILON
            or self.crop_rect.right > bounds.right + COORDINATE_EPSILON
            or self.crop_rect.bottom > bounds.bottom + COORDINATE_EPSILON
        ):
            raise ValueError("crop rectangle must remain inside normalized pixel boundaries")
        return self

    @classmethod
    def from_crop_config(
        cls,
        *,
        original_size: PixelSize,
        normalized_size: PixelSize,
        exif_orientation: int,
        crop: CropConfig,
        working_size: PixelSize,
        canvas_size: MillimetreSize,
    ) -> "CanonicalTransform":
        return cls(
            original_size=original_size,
            normalized_size=normalized_size,
            exif_orientation=exif_orientation,
            crop_rect=Rect(
                x=crop.x * normalized_size.width,
                y=crop.y * normalized_size.height,
                width=crop.width * normalized_size.width,
                height=crop.height * normalized_size.height,
            ),
            fit_mode=crop.mode,
            working_size=working_size,
            canvas_size=canvas_size,
        )

    @classmethod
    def from_source_metadata(
        cls,
        *,
        source: SourceImageMetadata,
        crop: CropConfig,
        working_size: PixelSize,
        canvas_size: MillimetreSize,
    ) -> "CanonicalTransform":
        return cls.from_crop_config(
            original_size=PixelSize(
                width=source.source_width_px,
                height=source.source_height_px,
            ),
            normalized_size=PixelSize(
                width=source.normalized_width_px,
                height=source.normalized_height_px,
            ),
            exif_orientation=source.exif_orientation,
            crop=crop,
            working_size=working_size,
            canvas_size=canvas_size,
        )

    @property
    def uses_canvas_extension(self) -> bool:
        return self.fit_mode == CropMode.EXTEND

    @property
    def content_bounds_working(self) -> Rect:
        crop_bounds = self.stage_bounds(CoordinateStage.CROP)
        return self.matrix(CoordinateStage.CROP, CoordinateStage.WORKING).map_rect(crop_bounds)

    @property
    def visible_content_bounds_working(self) -> Rect:
        return self.content_bounds_working.intersection(self.stage_bounds(CoordinateStage.WORKING))

    def stage_bounds(self, stage: CoordinateStage) -> Rect:
        if stage == CoordinateStage.ORIGINAL:
            return Rect(x=0, y=0, width=self.original_size.width, height=self.original_size.height)
        if stage == CoordinateStage.NORMALIZED:
            return Rect(
                x=0,
                y=0,
                width=self.normalized_size.width,
                height=self.normalized_size.height,
            )
        if stage == CoordinateStage.CROP:
            return Rect(x=0, y=0, width=self.crop_rect.width, height=self.crop_rect.height)
        if stage == CoordinateStage.WORKING:
            return Rect(x=0, y=0, width=self.working_size.width, height=self.working_size.height)
        return Rect(x=0, y=0, width=self.canvas_size.width, height=self.canvas_size.height)

    def matrix(self, source: CoordinateStage, target: CoordinateStage) -> Affine2D:
        stages = tuple(CoordinateStage)
        source_index = stages.index(source)
        target_index = stages.index(target)
        if source_index == target_index:
            return Affine2D.identity()
        if source_index > target_index:
            return self.matrix(target, source).inverse()
        result = Affine2D.identity()
        adjacent = self._adjacent_matrices()
        for index in range(source_index, target_index):
            result = result.then(adjacent[index])
        return result

    def map_point(self, point: Point, *, source: CoordinateStage, target: CoordinateStage) -> Point:
        return self.matrix(source, target).map_point(point)

    def map_rect(self, rect: Rect, *, source: CoordinateStage, target: CoordinateStage) -> Rect:
        return self.matrix(source, target).map_rect(rect)

    def map_pixel_center(
        self,
        x: int,
        y: int,
        *,
        source: CoordinateStage,
        target: CoordinateStage,
    ) -> Point:
        if source not in {
            CoordinateStage.ORIGINAL,
            CoordinateStage.NORMALIZED,
            CoordinateStage.WORKING,
        }:
            raise ValueError("pixel centers are defined only for raster stages")
        _validate_pixel_index_type(x, y)
        bounds = self.stage_bounds(source)
        if x < 0 or y < 0 or x >= bounds.width or y >= bounds.height:
            raise ValueError("pixel index is outside its raster stage")
        return self.map_point(Point(x=x + 0.5, y=y + 0.5), source=source, target=target)

    def working_pixel_bounds_mm(self, x: int, y: int) -> Rect:
        _validate_pixel_index_type(x, y)
        if x < 0 or y < 0 or x >= self.working_size.width or y >= self.working_size.height:
            raise ValueError("working pixel index is outside the raster")
        return self.map_rect(
            Rect(x=x, y=y, width=1, height=1),
            source=CoordinateStage.WORKING,
            target=CoordinateStage.MILLIMETRES,
        )

    def working_point_has_source(self, point: Point) -> bool:
        crop_point = self.map_point(
            point,
            source=CoordinateStage.WORKING,
            target=CoordinateStage.CROP,
        )
        return self.stage_bounds(CoordinateStage.CROP).contains(crop_point)

    def _adjacent_matrices(self) -> tuple[Affine2D, Affine2D, Affine2D, Affine2D]:
        return (
            orientation_matrix(self.original_size, self.exif_orientation),
            Affine2D.translate(-self.crop_rect.x, -self.crop_rect.y),
            _fit_matrix(
                self.crop_rect.width,
                self.crop_rect.height,
                self.working_size.width,
                self.working_size.height,
                self.fit_mode,
            ),
            Affine2D.scale(
                self.canvas_size.width / self.working_size.width,
                self.canvas_size.height / self.working_size.height,
            ),
        )


def normalized_size_for_orientation(original: PixelSize, orientation: int) -> PixelSize:
    if orientation not in range(1, 9):
        raise ValueError("EXIF orientation must be between 1 and 8")
    if orientation in {5, 6, 7, 8}:
        return PixelSize(width=original.height, height=original.width)
    return original


def orientation_matrix(original: PixelSize, orientation: int) -> Affine2D:
    width = original.width
    height = original.height
    matrices = {
        1: Affine2D.identity(),
        2: Affine2D(m00=-1, m11=1, tx=width),
        3: Affine2D(m00=-1, m11=-1, tx=width, ty=height),
        4: Affine2D(m00=1, m11=-1, ty=height),
        5: Affine2D(m00=0, m01=1, m10=1, m11=0),
        6: Affine2D(m00=0, m01=-1, m10=1, m11=0, tx=height),
        7: Affine2D(m00=0, m01=-1, m10=-1, m11=0, tx=height, ty=width),
        8: Affine2D(m00=0, m01=1, m10=-1, m11=0, ty=width),
    }
    try:
        return matrices[orientation]
    except KeyError as error:
        raise ValueError("EXIF orientation must be between 1 and 8") from error


def resize_canvas(
    canvas: MillimetreSize,
    *,
    axis: DimensionAxis,
    value_mm: float,
    lock_aspect: bool,
) -> MillimetreSize:
    if not math.isfinite(value_mm) or value_mm <= 0:
        raise ValueError("canvas dimension must be a positive finite number")
    if axis == DimensionAxis.WIDTH:
        height = value_mm / canvas.aspect_ratio if lock_aspect else canvas.height
        return MillimetreSize(width=value_mm, height=height)
    if axis == DimensionAxis.HEIGHT:
        width = value_mm * canvas.aspect_ratio if lock_aspect else canvas.width
        return MillimetreSize(width=width, height=value_mm)
    raise ValueError(f"unsupported dimension axis: {axis}")


def _fit_matrix(
    source_width: float,
    source_height: float,
    target_width: float,
    target_height: float,
    mode: CropMode,
) -> Affine2D:
    scale_x = target_width / source_width
    scale_y = target_height / source_height
    if mode == CropMode.STRETCH:
        return Affine2D.scale(scale_x, scale_y)
    if mode == CropMode.COVER:
        scale = max(scale_x, scale_y)
    elif mode in {CropMode.CONTAIN, CropMode.EXTEND}:
        scale = min(scale_x, scale_y)
    else:  # pragma: no cover - enum contract prevents unknown modes
        raise ValueError(f"unsupported crop mode: {mode}")
    offset_x = (target_width - source_width * scale) / 2
    offset_y = (target_height - source_height * scale) / 2
    return Affine2D(m00=scale, m11=scale, tx=offset_x, ty=offset_y)


def _validate_pixel_index_type(x: int, y: int) -> None:
    if type(x) is not int or type(y) is not int:
        raise ValueError("pixel indices must be integers")
