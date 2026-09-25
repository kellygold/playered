"""Pure master-canvas division and physical build-plate planning.

The mural planner never reprocesses individual tiles. It partitions one processed
master with shared mathematical boundaries, then layers physical panel, bleed,
assembly-gap, and printer-placement concerns on top of those immutable crops.
"""

# ruff: noqa: UP045 -- Python 3.9 is supported and Pydantic evaluates these annotations.

from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize, Rect

MURAL_PLAN_SCHEMA_VERSION = 1
GEOMETRY_EPSILON_MM = 1e-9


class MuralModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class OrientationPreference(str, Enum):
    AUTO = "auto"
    NATIVE = "native"
    ROTATE_90 = "rotate_90"


class MuralSourceProvenance(MuralModel):
    """Exact identity of the processed master used to derive every tile."""

    source_asset_id: str = Field(min_length=1, max_length=160)
    source_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    processed_artifact_id: str = Field(min_length=1, max_length=160)
    processed_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    processed_size: PixelSize
    canonical_transform: CanonicalTransform
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_version: str = Field(min_length=1, max_length=120)
    revision_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    draft_generation: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def processed_master_matches_transform(self) -> MuralSourceProvenance:
        if self.processed_size != self.canonical_transform.working_size:
            raise ValueError("processed master dimensions must match the canonical transform")
        if (self.revision_id is None) == (self.draft_generation is None):
            raise ValueError("provenance must identify exactly one revision or draft generation")
        return self

    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class MuralLayout(MuralModel):
    rows: int = Field(ge=1, le=50)
    columns: int = Field(ge=1, le=50)
    panel_width_mm: float = Field(gt=0, le=2_000)
    panel_height_mm: float = Field(gt=0, le=2_000)
    horizontal_gap_mm: float = Field(default=0, ge=0, le=500)
    vertical_gap_mm: float = Field(default=0, ge=0, le=500)
    bleed_mm: float = Field(default=0, ge=0, le=100)
    orientation: OrientationPreference = OrientationPreference.AUTO

    @model_validator(mode="after")
    def output_dimensions_are_supported(self) -> MuralLayout:
        if self.panel_width_mm + 2 * self.bleed_mm > 2_000:
            raise ValueError("panel width plus bleed cannot exceed 2000 mm")
        if self.panel_height_mm + 2 * self.bleed_mm > 2_000:
            raise ValueError("panel height plus bleed cannot exceed 2000 mm")
        return self

    @property
    def tile_count(self) -> int:
        return self.rows * self.columns

    @property
    def master_size(self) -> MillimetreSize:
        return MillimetreSize(
            width=_mm(self.columns * self.panel_width_mm),
            height=_mm(self.rows * self.panel_height_mm),
        )

    @property
    def assembled_size(self) -> MillimetreSize:
        return MillimetreSize(
            width=_mm(self.master_size.width + (self.columns - 1) * self.horizontal_gap_mm),
            height=_mm(self.master_size.height + (self.rows - 1) * self.vertical_gap_mm),
        )

    @property
    def output_tile_size(self) -> MillimetreSize:
        return MillimetreSize(
            width=_mm(self.panel_width_mm + 2 * self.bleed_mm),
            height=_mm(self.panel_height_mm + 2 * self.bleed_mm),
        )


class BedRectangle(MuralModel):
    x_mm: float = Field(ge=0)
    y_mm: float = Field(ge=0)
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)

    @property
    def right_mm(self) -> float:
        return self.x_mm + self.width_mm

    @property
    def bottom_mm(self) -> float:
        return self.y_mm + self.height_mm


class BedEnvelope(MuralModel):
    printer_id: str = Field(min_length=1, max_length=120)
    plate_id: str = Field(min_length=1, max_length=120)
    profile_catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    width_mm: float = Field(gt=0, le=2_000)
    height_mm: float = Field(gt=0, le=2_000)
    edge_clearance_mm: float = Field(default=0, ge=0, le=100)
    excluded_rectangles: tuple[BedRectangle, ...] = ()

    @model_validator(mode="after")
    def exclusions_and_clearance_stay_inside_bed(self) -> BedEnvelope:
        if 2 * self.edge_clearance_mm >= min(self.width_mm, self.height_mm):
            raise ValueError("edge clearance must leave a positive printable rectangle")
        for rectangle in self.excluded_rectangles:
            if (
                rectangle.right_mm > self.width_mm + GEOMETRY_EPSILON_MM
                or rectangle.bottom_mm > self.height_mm + GEOMETRY_EPSILON_MM
            ):
                raise ValueError("excluded rectangle must remain inside the printable bed")
        return self


class PixelBoundaryRect(MuralModel):
    x_start: int = Field(ge=0)
    y_start: int = Field(ge=0)
    x_end: int = Field(ge=1)
    y_end: int = Field(ge=1)

    @model_validator(mode="after")
    def has_positive_area(self) -> PixelBoundaryRect:
        if self.x_end <= self.x_start or self.y_end <= self.y_start:
            raise ValueError("pixel boundary rectangle must have positive area")
        return self

    @property
    def width(self) -> int:
        return self.x_end - self.x_start

    @property
    def height(self) -> int:
        return self.y_end - self.y_start


class EdgeInsets(MuralModel):
    top_mm: float = Field(ge=0)
    right_mm: float = Field(ge=0)
    bottom_mm: float = Field(ge=0)
    left_mm: float = Field(ge=0)


class BedFit(MuralModel):
    fits: bool
    rotation_degrees: Literal[0, 90]
    placed_width_mm: float = Field(gt=0)
    placed_height_mm: float = Field(gt=0)
    origin_x_mm: Optional[float] = Field(default=None, ge=0)
    origin_y_mm: Optional[float] = Field(default=None, ge=0)
    reason: Optional[str] = None

    @model_validator(mode="after")
    def placement_matches_fit_state(self) -> BedFit:
        has_origin = self.origin_x_mm is not None and self.origin_y_mm is not None
        if self.fits != has_origin:
            raise ValueError("a fitting tile must have a bed origin and a failed fit must not")
        if self.fits and self.reason is not None:
            raise ValueError("a fitting tile cannot include a failure reason")
        if not self.fits and not self.reason:
            raise ValueError("a failed tile fit must explain why")
        return self


class TilePlan(MuralModel):
    id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    row: int = Field(ge=1, le=50)
    column: int = Field(ge=1, le=50)
    build_plate_index: int = Field(ge=1, le=2_500)
    normalized_master_bounds: Rect
    master_bounds_mm: Rect
    master_pixel_bounds: PixelBoundaryRect
    sampled_master_bounds_mm: Rect
    sampled_master_pixel_bounds: PixelBoundaryRect
    output_size_mm: MillimetreSize
    outside_master_padding: EdgeInsets
    bed_fit: BedFit


class MuralPlanRequest(MuralModel):
    schema_version: Literal[1] = MURAL_PLAN_SCHEMA_VERSION
    source: MuralSourceProvenance
    layout: MuralLayout
    bed: BedEnvelope
    reserved_rectangles: tuple[BedRectangle, ...] = ()

    @model_validator(mode="after")
    def grid_does_not_exceed_raster_resolution(self) -> MuralPlanRequest:
        if self.layout.columns > self.source.processed_size.width:
            raise ValueError("grid columns cannot exceed processed master pixel width")
        if self.layout.rows > self.source.processed_size.height:
            raise ValueError("grid rows cannot exceed processed master pixel height")
        for rectangle in self.reserved_rectangles:
            if (
                rectangle.right_mm > self.bed.width_mm + GEOMETRY_EPSILON_MM
                or rectangle.bottom_mm > self.bed.height_mm + GEOMETRY_EPSILON_MM
            ):
                raise ValueError("reserved rectangle must remain inside the printable bed")
        return self


class MuralPlan(MuralModel):
    schema_version: Literal[1] = MURAL_PLAN_SCHEMA_VERSION
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_transform: CanonicalTransform
    master_size_mm: MillimetreSize
    assembled_size_mm: MillimetreSize
    tiles: tuple[TilePlan, ...] = Field(min_length=1, max_length=2_500)
    all_tiles_fit: bool
    warnings: tuple[str, ...] = ()


class PlanFreshnessReason(str, Enum):
    SOURCE_ASSET_CHANGED = "source_asset_changed"
    PROCESSED_ARTIFACT_CHANGED = "processed_artifact_changed"
    CANONICAL_TRANSFORM_CHANGED = "canonical_transform_changed"
    CONFIG_CHANGED = "config_changed"
    REVISION_CHANGED = "revision_changed"
    ENGINE_CHANGED = "engine_changed"


class PlanFreshness(MuralModel):
    current: bool
    reasons: tuple[PlanFreshnessReason, ...] = ()

    @model_validator(mode="after")
    def current_state_matches_reasons(self) -> PlanFreshness:
        if self.current == bool(self.reasons):
            raise ValueError("current plans have no stale reasons and stale plans have reasons")
        return self


def build_mural_plan(request: MuralPlanRequest) -> MuralPlan:
    """Build a deterministic row-major plan from one canonical processed master."""

    layout = request.layout
    source = request.source
    master_size = layout.master_size
    master_transform = source.canonical_transform.model_copy(update={"canvas_size": master_size})
    effective_bed = request.bed.model_copy(
        update={
            "excluded_rectangles": (
                *request.bed.excluded_rectangles,
                *request.reserved_rectangles,
            )
        }
    )
    bed_fit = _resolve_bed_fit(layout.output_tile_size, layout.orientation, effective_bed)
    tiles: list[TilePlan] = []
    for row_index in range(layout.rows):
        for column_index in range(layout.columns):
            pixel_bounds = _pixel_partition(
                source.processed_size,
                row_index,
                column_index,
                layout.rows,
                layout.columns,
            )
            nominal = Rect(
                x=_mm(column_index * layout.panel_width_mm),
                y=_mm(row_index * layout.panel_height_mm),
                width=_mm(layout.panel_width_mm),
                height=_mm(layout.panel_height_mm),
            )
            sampled, padding = _sampled_bounds(nominal, master_size, layout.bleed_mm)
            sampled_pixels = _physical_bounds_to_pixels(
                sampled,
                master_size,
                source.processed_size,
            )
            plate_index = row_index * layout.columns + column_index + 1
            tiles.append(
                TilePlan(
                    id=f"tile-r{row_index + 1:02d}-c{column_index + 1:02d}",
                    row=row_index + 1,
                    column=column_index + 1,
                    build_plate_index=plate_index,
                    normalized_master_bounds=Rect(
                        x=column_index / layout.columns,
                        y=row_index / layout.rows,
                        width=1 / layout.columns,
                        height=1 / layout.rows,
                    ),
                    master_bounds_mm=nominal,
                    master_pixel_bounds=pixel_bounds,
                    sampled_master_bounds_mm=sampled,
                    sampled_master_pixel_bounds=sampled_pixels,
                    output_size_mm=layout.output_tile_size,
                    outside_master_padding=padding,
                    bed_fit=bed_fit,
                )
            )

    warnings = ()
    if not bed_fit.fits:
        warnings = (bed_fit.reason or "Tile does not fit the selected printer plate.",)
    return MuralPlan(
        request_fingerprint=_fingerprint(request.model_dump(mode="json")),
        source_fingerprint=source.fingerprint(),
        master_transform=master_transform,
        master_size_mm=master_size,
        assembled_size_mm=layout.assembled_size,
        tiles=tuple(tiles),
        all_tiles_fit=bed_fit.fits,
        warnings=warnings,
    )


def assess_plan_freshness(
    saved_source: MuralSourceProvenance,
    current_source: MuralSourceProvenance,
) -> PlanFreshness:
    """Explain why a persisted plan no longer describes the current master."""

    reasons: list[PlanFreshnessReason] = []
    if (
        saved_source.source_asset_id != current_source.source_asset_id
        or saved_source.source_asset_sha256 != current_source.source_asset_sha256
    ):
        reasons.append(PlanFreshnessReason.SOURCE_ASSET_CHANGED)
    if (
        saved_source.processed_artifact_id != current_source.processed_artifact_id
        or saved_source.processed_artifact_sha256 != current_source.processed_artifact_sha256
        or saved_source.processed_size != current_source.processed_size
    ):
        reasons.append(PlanFreshnessReason.PROCESSED_ARTIFACT_CHANGED)
    if saved_source.canonical_transform != current_source.canonical_transform:
        reasons.append(PlanFreshnessReason.CANONICAL_TRANSFORM_CHANGED)
    if saved_source.config_sha256 != current_source.config_sha256:
        reasons.append(PlanFreshnessReason.CONFIG_CHANGED)
    if (
        saved_source.revision_id != current_source.revision_id
        or saved_source.draft_generation != current_source.draft_generation
    ):
        reasons.append(PlanFreshnessReason.REVISION_CHANGED)
    if saved_source.engine_version != current_source.engine_version:
        reasons.append(PlanFreshnessReason.ENGINE_CHANGED)
    return PlanFreshness(current=not reasons, reasons=tuple(reasons))


def _pixel_partition(
    size: PixelSize,
    row_index: int,
    column_index: int,
    rows: int,
    columns: int,
) -> PixelBoundaryRect:
    return PixelBoundaryRect(
        x_start=column_index * size.width // columns,
        x_end=(column_index + 1) * size.width // columns,
        y_start=row_index * size.height // rows,
        y_end=(row_index + 1) * size.height // rows,
    )


def _sampled_bounds(
    nominal: Rect,
    master: MillimetreSize,
    bleed_mm: float,
) -> tuple[Rect, EdgeInsets]:
    requested_left = nominal.x - bleed_mm
    requested_top = nominal.y - bleed_mm
    requested_right = nominal.right + bleed_mm
    requested_bottom = nominal.bottom + bleed_mm
    left = max(0.0, requested_left)
    top = max(0.0, requested_top)
    right = min(master.width, requested_right)
    bottom = min(master.height, requested_bottom)
    return (
        Rect(
            x=_mm(left),
            y=_mm(top),
            width=_mm(right - left),
            height=_mm(bottom - top),
        ),
        EdgeInsets(
            top_mm=_mm(top - requested_top),
            right_mm=_mm(requested_right - right),
            bottom_mm=_mm(requested_bottom - bottom),
            left_mm=_mm(left - requested_left),
        ),
    )


def _physical_bounds_to_pixels(
    bounds: Rect,
    master: MillimetreSize,
    raster: PixelSize,
) -> PixelBoundaryRect:
    x_start = math.floor(bounds.x * raster.width / master.width + GEOMETRY_EPSILON_MM)
    y_start = math.floor(bounds.y * raster.height / master.height + GEOMETRY_EPSILON_MM)
    x_end = math.ceil(bounds.right * raster.width / master.width - GEOMETRY_EPSILON_MM)
    y_end = math.ceil(bounds.bottom * raster.height / master.height - GEOMETRY_EPSILON_MM)
    return PixelBoundaryRect(
        x_start=max(0, x_start),
        y_start=max(0, y_start),
        x_end=min(raster.width, x_end),
        y_end=min(raster.height, y_end),
    )


def _resolve_bed_fit(
    output: MillimetreSize,
    preference: OrientationPreference,
    bed: BedEnvelope,
) -> BedFit:
    candidates = (
        ((0, output.width, output.height), (90, output.height, output.width))
        if preference == OrientationPreference.AUTO
        else ((0, output.width, output.height),)
        if preference == OrientationPreference.NATIVE
        else ((90, output.height, output.width),)
    )
    for rotation, width, height in candidates:
        origin = _find_bed_origin(width, height, bed)
        if origin is not None:
            return BedFit(
                fits=True,
                rotation_degrees=rotation,
                placed_width_mm=_mm(width),
                placed_height_mm=_mm(height),
                origin_x_mm=_mm(origin[0]),
                origin_y_mm=_mm(origin[1]),
            )
    rotation, width, height = candidates[0]
    available_width = bed.width_mm - 2 * bed.edge_clearance_mm
    available_height = bed.height_mm - 2 * bed.edge_clearance_mm
    return BedFit(
        fits=False,
        rotation_degrees=rotation,
        placed_width_mm=_mm(width),
        placed_height_mm=_mm(height),
        reason=(
            f"Tile {width:g}×{height:g} mm does not fit the usable "
            f"{available_width:g}×{available_height:g} mm area on {bed.printer_id}; "
            "reduce panel size or bleed, or allow the other orientation."
        ),
    )


def _find_bed_origin(
    width: float,
    height: float,
    bed: BedEnvelope,
) -> Optional[tuple[float, float]]:
    clearance = bed.edge_clearance_mm
    maximum_x = bed.width_mm - clearance - width
    maximum_y = bed.height_mm - clearance - height
    if maximum_x < clearance - GEOMETRY_EPSILON_MM:
        return None
    if maximum_y < clearance - GEOMETRY_EPSILON_MM:
        return None

    x_candidates = {clearance, maximum_x}
    y_candidates = {clearance, maximum_y}
    for excluded in bed.excluded_rectangles:
        x_candidates.update(
            {
                excluded.x_mm - clearance - width,
                excluded.right_mm + clearance,
            }
        )
        y_candidates.update(
            {
                excluded.y_mm - clearance - height,
                excluded.bottom_mm + clearance,
            }
        )
    for y in sorted(value for value in y_candidates if clearance <= value <= maximum_y):
        for x in sorted(value for value in x_candidates if clearance <= value <= maximum_x):
            if not any(
                _rectangles_overlap(
                    x,
                    y,
                    width,
                    height,
                    excluded.x_mm - clearance,
                    excluded.y_mm - clearance,
                    excluded.width_mm + 2 * clearance,
                    excluded.height_mm + 2 * clearance,
                )
                for excluded in bed.excluded_rectangles
            ):
                return x, y
    return None


def _rectangles_overlap(
    ax: float,
    ay: float,
    aw: float,
    ah: float,
    bx: float,
    by: float,
    bw: float,
    bh: float,
) -> bool:
    return (
        ax < bx + bw - GEOMETRY_EPSILON_MM
        and ax + aw > bx + GEOMETRY_EPSILON_MM
        and ay < by + bh - GEOMETRY_EPSILON_MM
        and ay + ah > by + GEOMETRY_EPSILON_MM
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mm(value: float) -> float:
    return round(value, 9)
