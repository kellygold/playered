"""Seam-safe mural partitioning from one authoritative processed label field.

The nominal tile crops in this module are the topology contract.  Every source
pixel belongs to exactly one nominal tile, internal boundaries are shared integer
coordinates from :class:`MuralPlan`, and downstream geometry must vectorize these
immutable crops without running tile-local cleanup across protected seams.

Bleed is deliberately represented as a separate sampled crop.  Assembly gaps
remain physical layout metadata.  Neither can change the visible label field.
"""

# ruff: noqa: UP045 -- Python 3.9 is supported and Pydantic evaluates these annotations.

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.labels import LabelField, PixelRegion
from image23mf.engine.transform import MillimetreSize, Rect
from image23mf.mural.planner import (
    EdgeInsets,
    MuralLayout,
    MuralPlan,
    MuralPlanRequest,
    PixelBoundaryRect,
    TilePlan,
    build_mural_plan,
)

MURAL_LABEL_PARTITION_SCHEMA_VERSION = 1


class MuralPartitionError(ValueError):
    """The master, plan, or supplied tile set violates the partition contract."""


class IndependentSeamMutationError(MuralPartitionError):
    """Tile-local processing attempted to change a protected internal seam."""


class PartitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TileSide(str, Enum):
    TOP = "top"
    RIGHT = "right"
    BOTTOM = "bottom"
    LEFT = "left"


class SeamOrientation(str, Enum):
    VERTICAL = "vertical"
    HORIZONTAL = "horizontal"


class PartitionWarningCode(str, Enum):
    INDEPENDENT_SEAM_CLEANUP_FORBIDDEN = "independent_seam_cleanup_forbidden"
    BLEED_SAMPLING_ONLY = "bleed_sampling_only"
    ASSEMBLY_GAP_LAYOUT_ONLY = "assembly_gap_layout_only"
    PROTECTED_SEAM_CHANGED = "protected_seam_changed"


class SeamMutationPolicy(str, Enum):
    FORBID = "forbid"
    WARN = "warn"


class LabelFieldIdentity(PartitionModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    label_values: tuple[int, ...] = Field(min_length=1)
    pixels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_field(cls, labels: LabelField) -> LabelFieldIdentity:
        return cls(
            width=labels.width,
            height=labels.height,
            label_values=labels.label_values,
            pixels_sha256=hashlib.sha256(labels.pixels).hexdigest(),
        )

    def validate_field(self, labels: LabelField, *, description: str) -> None:
        if self != LabelFieldIdentity.from_field(labels):
            raise MuralPartitionError(f"{description} does not match its persisted identity")


class PartitionWarning(PartitionModel):
    code: PartitionWarningCode
    message: str = Field(min_length=1, max_length=600)
    tile_id: Optional[str] = Field(default=None, min_length=1, max_length=64)


class TileTopologyRecipe(PartitionModel):
    """Exact downstream topology input, without a second cleanup decision."""

    source_mode: Literal["authoritative_master_label_crop"] = "authoritative_master_label_crop"
    cleanup_policy: Literal["master_only"] = "master_only"
    master_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visible_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_pixel_bounds: PixelBoundaryRect
    master_bounds_mm: Rect
    local_canvas_size_mm: MillimetreSize
    visible_output_bounds_mm: Rect
    protected_sides: tuple[TileSide, ...] = ()


class MuralLabelTileManifest(PartitionModel):
    tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    row: int = Field(ge=1, le=50)
    column: int = Field(ge=1, le=50)
    build_plate_index: int = Field(ge=1, le=2_500)
    visible_master_bounds: PixelBoundaryRect
    sampled_master_bounds: PixelBoundaryRect
    visible_region_in_sample: PixelRegion
    outside_master_padding: EdgeInsets
    visible_labels: LabelFieldIdentity
    sampled_labels: LabelFieldIdentity
    topology: TileTopologyRecipe

    @model_validator(mode="after")
    def fields_agree_with_bounds(self) -> MuralLabelTileManifest:
        if (
            self.visible_labels.width != self.visible_master_bounds.width
            or self.visible_labels.height != self.visible_master_bounds.height
        ):
            raise ValueError("visible label dimensions must match nominal master bounds")
        if (
            self.sampled_labels.width != self.sampled_master_bounds.width
            or self.sampled_labels.height != self.sampled_master_bounds.height
        ):
            raise ValueError("sampled label dimensions must match sampled master bounds")
        region = self.visible_region_in_sample
        if region.right > self.sampled_labels.width or region.bottom > self.sampled_labels.height:
            raise ValueError("visible labels must remain inside the sampled crop")
        if region.width != self.visible_labels.width or region.height != self.visible_labels.height:
            raise ValueError("visible sample region must match visible label dimensions")
        if self.topology.master_pixel_bounds != self.visible_master_bounds:
            raise ValueError("topology recipe must use the nominal visible bounds")
        if self.topology.visible_labels_sha256 != self.visible_labels.pixels_sha256:
            raise ValueError("topology recipe must use the nominal visible label bytes")
        return self


class SharedTileSeam(PartitionModel):
    id: str = Field(pattern=r"^seam-[vh]-[0-9]+-[0-9]+-[0-9]+$")
    orientation: SeamOrientation
    coordinate_px: int = Field(ge=1)
    span_start_px: int = Field(ge=0)
    span_end_px: int = Field(ge=1)
    first_tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    second_tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    first_side: TileSide
    second_side: TileSide
    first_guard_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    second_guard_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def orientation_matches_sides(self) -> SharedTileSeam:
        expected = (
            (TileSide.RIGHT, TileSide.LEFT)
            if self.orientation == SeamOrientation.VERTICAL
            else (TileSide.BOTTOM, TileSide.TOP)
        )
        if (self.first_side, self.second_side) != expected:
            raise ValueError("seam orientation must agree with its ordered tile sides")
        if self.span_end_px <= self.span_start_px:
            raise ValueError("seam span must have positive length")
        return self


class MuralLabelPartitionManifest(PartitionModel):
    schema_version: Literal[1] = MURAL_LABEL_PARTITION_SCHEMA_VERSION
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_asset_id: str = Field(min_length=1, max_length=160)
    source_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    processed_artifact_id: str = Field(min_length=1, max_length=160)
    processed_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_vector_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authoritative_topology_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    layout: MuralLayout
    master_size_mm: MillimetreSize
    assembled_size_mm: MillimetreSize
    master_labels: LabelFieldIdentity
    tiles: tuple[MuralLabelTileManifest, ...] = Field(min_length=1, max_length=2_500)
    seams: tuple[SharedTileSeam, ...] = ()
    warnings: tuple[PartitionWarning, ...] = ()
    partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def manifest_is_self_consistent(self) -> MuralLabelPartitionManifest:
        expected_tiles = self.layout.rows * self.layout.columns
        if len(self.tiles) != expected_tiles:
            raise ValueError("partition tile count must match its layout")
        if len({item.tile_id for item in self.tiles}) != expected_tiles:
            raise ValueError("partition tile identifiers must be unique")
        expected_sha = _fingerprint(self.model_dump(mode="json", exclude={"partition_sha256"}))
        if self.partition_sha256 != expected_sha:
            raise ValueError("partition fingerprint does not match its canonical manifest")
        return self


class TileMutationAssessment(PartitionModel):
    tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    accepted: bool
    changed_pixel_count: int = Field(ge=0)
    protected_changed_pixel_count: int = Field(ge=0)
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warnings: tuple[PartitionWarning, ...] = ()


@dataclass(frozen=True)
class MuralLabelTile:
    manifest: MuralLabelTileManifest
    visible_labels: LabelField
    sampled_labels: LabelField

    def validate(self) -> None:
        self.manifest.visible_labels.validate_field(
            self.visible_labels, description=f"{self.manifest.tile_id} visible labels"
        )
        self.manifest.sampled_labels.validate_field(
            self.sampled_labels, description=f"{self.manifest.tile_id} sampled labels"
        )


@dataclass(frozen=True)
class MuralLabelPartition:
    master_labels: LabelField
    request: MuralPlanRequest
    plan: MuralPlan
    manifest: MuralLabelPartitionManifest
    tiles: tuple[MuralLabelTile, ...]

    def __post_init__(self) -> None:
        canonical_plan = build_mural_plan(self.request)
        if self.plan != canonical_plan:
            raise MuralPartitionError("runtime partition plan is not canonical")
        if (self.master_labels.width, self.master_labels.height) != (
            self.request.source.processed_size.width,
            self.request.source.processed_size.height,
        ):
            raise MuralPartitionError("runtime master label dimensions are stale")
        master_identity = LabelFieldIdentity.from_field(self.master_labels)
        expected_tiles = tuple(
            _tile_from_plan(self.master_labels, self.request, tile_plan, master_identity)
            for tile_plan in self.plan.tiles
        )
        expected_seams = _build_seams(expected_tiles, self.request.layout)
        expected_warnings = _partition_warnings(self.request.layout)
        expected_payload = {
            "schema_version": MURAL_LABEL_PARTITION_SCHEMA_VERSION,
            "request_fingerprint": self.plan.request_fingerprint,
            "source_fingerprint": self.plan.source_fingerprint,
            "source_asset_id": self.request.source.source_asset_id,
            "source_asset_sha256": self.request.source.source_asset_sha256,
            "processed_artifact_id": self.request.source.processed_artifact_id,
            "processed_artifact_sha256": self.request.source.processed_artifact_sha256,
            "authoritative_vector_sha256": self.manifest.authoritative_vector_sha256,
            "authoritative_topology_sha256": self.manifest.authoritative_topology_sha256,
            "layout": self.request.layout,
            "master_size_mm": self.plan.master_size_mm,
            "assembled_size_mm": self.plan.assembled_size_mm,
            "master_labels": master_identity,
            "tiles": tuple(item.manifest for item in expected_tiles),
            "seams": expected_seams,
            "warnings": expected_warnings,
        }
        expected_manifest = MuralLabelPartitionManifest(
            **expected_payload,
            partition_sha256=_fingerprint(_json_value(expected_payload)),
        )
        if self.manifest != expected_manifest:
            raise MuralPartitionError(
                "partition manifest is not the canonical derivation of its master labels and plan"
            )
        if self.tiles != expected_tiles:
            raise MuralPartitionError(
                "runtime partition tiles are not canonical master-label crops"
            )
        if tuple(item.manifest for item in self.tiles) != self.manifest.tiles:
            raise MuralPartitionError("runtime tiles do not match the partition manifest")
        for tile in self.tiles:
            tile.validate()

    def tile(self, tile_id: str) -> MuralLabelTile:
        matches = tuple(item for item in self.tiles if item.manifest.tile_id == tile_id)
        if len(matches) != 1:
            raise MuralPartitionError(f"partition does not contain exactly one {tile_id}")
        return matches[0]


def partition_master_labels(
    master: LabelField,
    request: MuralPlanRequest,
    plan: MuralPlan,
    *,
    authoritative_vector_sha256: Optional[str] = None,
    authoritative_topology_sha256: Optional[str] = None,
) -> MuralLabelPartition:
    """Derive immutable nominal and sampled tile fields from one verified master."""

    if (master.width, master.height) != (
        request.source.processed_size.width,
        request.source.processed_size.height,
    ):
        raise MuralPartitionError(
            "authoritative label dimensions do not match mural source provenance"
        )
    expected_plan = build_mural_plan(request)
    if plan != expected_plan:
        raise MuralPartitionError("mural plan is not the canonical plan for its persisted request")

    master_identity = LabelFieldIdentity.from_field(master)
    runtime_tiles = tuple(
        _tile_from_plan(master, request, tile_plan, master_identity) for tile_plan in plan.tiles
    )

    seams = _build_seams(runtime_tiles, request.layout)
    warnings = _partition_warnings(request.layout)
    manifest_payload = {
        "schema_version": MURAL_LABEL_PARTITION_SCHEMA_VERSION,
        "request_fingerprint": plan.request_fingerprint,
        "source_fingerprint": plan.source_fingerprint,
        "source_asset_id": request.source.source_asset_id,
        "source_asset_sha256": request.source.source_asset_sha256,
        "processed_artifact_id": request.source.processed_artifact_id,
        "processed_artifact_sha256": request.source.processed_artifact_sha256,
        "authoritative_vector_sha256": authoritative_vector_sha256,
        "authoritative_topology_sha256": authoritative_topology_sha256,
        "layout": request.layout,
        "master_size_mm": plan.master_size_mm,
        "assembled_size_mm": plan.assembled_size_mm,
        "master_labels": master_identity,
        "tiles": tuple(item.manifest for item in runtime_tiles),
        "seams": seams,
        "warnings": warnings,
    }
    manifest = MuralLabelPartitionManifest(
        **manifest_payload,
        partition_sha256=_fingerprint(_json_value(manifest_payload)),
    )
    partition = MuralLabelPartition(
        master_labels=master,
        request=request,
        plan=plan,
        manifest=manifest,
        tiles=runtime_tiles,
    )
    if recompose_visible_tiles(partition) != master:
        raise MuralPartitionError("nominal mural tiles did not losslessly recompose the master")
    return partition


def recompose_visible_tiles(
    partition: MuralLabelPartition,
    tiles: Optional[Iterable[MuralLabelTile]] = None,
) -> LabelField:
    """Rebuild the master and reject missing, duplicate, overlapping, or altered tiles."""

    supplied = tuple(partition.tiles if tiles is None else tiles)
    expected = {item.tile_id: item for item in partition.manifest.tiles}
    if len(supplied) != len(expected):
        raise MuralPartitionError("recomposition requires every nominal tile exactly once")
    if len({item.manifest.tile_id for item in supplied}) != len(supplied):
        raise MuralPartitionError("recomposition contains duplicate tile identifiers")

    master = partition.manifest.master_labels
    pixels = bytearray(master.width * master.height)
    occupied = bytearray(master.width * master.height)
    for tile in supplied:
        manifest = expected.get(tile.manifest.tile_id)
        if manifest is None or manifest != tile.manifest:
            raise MuralPartitionError("recomposition tile metadata is not from this partition")
        tile.validate()
        bounds = manifest.visible_master_bounds
        for local_y in range(tile.visible_labels.height):
            master_start = (bounds.y_start + local_y) * master.width + bounds.x_start
            master_end = master_start + tile.visible_labels.width
            if any(occupied[master_start:master_end]):
                raise MuralPartitionError("nominal tile bounds overlap during recomposition")
            tile_start = local_y * tile.visible_labels.width
            tile_end = tile_start + tile.visible_labels.width
            pixels[master_start:master_end] = tile.visible_labels.pixels[tile_start:tile_end]
            occupied[master_start:master_end] = b"\x01" * tile.visible_labels.width
    if not all(occupied):
        raise MuralPartitionError("nominal tile bounds leave a gap during recomposition")
    return LabelField(
        width=master.width,
        height=master.height,
        label_values=master.label_values,
        pixels=bytes(pixels),
    )


def assess_independent_tile_candidate(
    partition: MuralLabelPartition,
    tile_id: str,
    candidate: LabelField,
    *,
    policy: SeamMutationPolicy = SeamMutationPolicy.FORBID,
) -> TileMutationAssessment:
    """Reject or explicitly warn when tile-local work changes a protected seam strip."""

    tile = partition.tile(tile_id)
    original = tile.visible_labels
    if (
        candidate.width != original.width
        or candidate.height != original.height
        or candidate.label_values != original.label_values
    ):
        raise MuralPartitionError(
            "independent tile candidate dimensions and palette must match the nominal crop"
        )
    changed = tuple(
        index
        for index, (before, after) in enumerate(zip(original.pixels, candidate.pixels))
        if before != after
    )
    protected = _protected_indices(
        original.width,
        original.height,
        tile.manifest.topology.protected_sides,
    )
    protected_changed = sum(index in protected for index in changed)
    warnings: tuple[PartitionWarning, ...] = ()
    if protected_changed:
        warnings = (
            PartitionWarning(
                code=PartitionWarningCode.PROTECTED_SEAM_CHANGED,
                tile_id=tile_id,
                message=(
                    f"Tile-local processing changed {protected_changed} protected seam pixel(s). "
                    "Apply cleanup to the authoritative master, then repartition all tiles."
                ),
            ),
        )
        if policy == SeamMutationPolicy.FORBID:
            raise IndependentSeamMutationError(warnings[0].message)
    return TileMutationAssessment(
        tile_id=tile_id,
        accepted=not protected_changed or policy == SeamMutationPolicy.WARN,
        changed_pixel_count=len(changed),
        protected_changed_pixel_count=protected_changed,
        original_sha256=hashlib.sha256(original.pixels).hexdigest(),
        candidate_sha256=hashlib.sha256(candidate.pixels).hexdigest(),
        warnings=warnings,
    )


def _tile_from_plan(
    master: LabelField,
    request: MuralPlanRequest,
    tile_plan: TilePlan,
    master_identity: LabelFieldIdentity,
) -> MuralLabelTile:
    visible = _crop(master, tile_plan.master_pixel_bounds)
    sampled = _crop(master, tile_plan.sampled_master_pixel_bounds)
    protected_sides = _protected_sides(tile_plan, request.layout)
    visible_region = PixelRegion(
        x=tile_plan.master_pixel_bounds.x_start - tile_plan.sampled_master_pixel_bounds.x_start,
        y=tile_plan.master_pixel_bounds.y_start - tile_plan.sampled_master_pixel_bounds.y_start,
        width=tile_plan.master_pixel_bounds.width,
        height=tile_plan.master_pixel_bounds.height,
    )
    visible_identity = LabelFieldIdentity.from_field(visible)
    sampled_identity = LabelFieldIdentity.from_field(sampled)
    # Planner rows use source-image top-down Y. Geometry IR uses lower-left Y.
    master_geometry_bounds = Rect(
        x=tile_plan.master_bounds_mm.x,
        y=request.layout.master_size.height - tile_plan.master_bounds_mm.bottom,
        width=tile_plan.master_bounds_mm.width,
        height=tile_plan.master_bounds_mm.height,
    )
    topology = TileTopologyRecipe(
        master_labels_sha256=master_identity.pixels_sha256,
        visible_labels_sha256=visible_identity.pixels_sha256,
        master_pixel_bounds=tile_plan.master_pixel_bounds,
        master_bounds_mm=master_geometry_bounds,
        local_canvas_size_mm=MillimetreSize(
            width=request.layout.panel_width_mm,
            height=request.layout.panel_height_mm,
        ),
        visible_output_bounds_mm=Rect(
            x=request.layout.bleed_mm,
            y=request.layout.bleed_mm,
            width=request.layout.panel_width_mm,
            height=request.layout.panel_height_mm,
        ),
        protected_sides=protected_sides,
    )
    manifest = MuralLabelTileManifest(
        tile_id=tile_plan.id,
        row=tile_plan.row,
        column=tile_plan.column,
        build_plate_index=tile_plan.build_plate_index,
        visible_master_bounds=tile_plan.master_pixel_bounds,
        sampled_master_bounds=tile_plan.sampled_master_pixel_bounds,
        visible_region_in_sample=visible_region,
        outside_master_padding=tile_plan.outside_master_padding,
        visible_labels=visible_identity,
        sampled_labels=sampled_identity,
        topology=topology,
    )
    return MuralLabelTile(
        manifest=manifest,
        visible_labels=visible,
        sampled_labels=sampled,
    )


def _crop(labels: LabelField, bounds: PixelBoundaryRect) -> LabelField:
    if bounds.x_end > labels.width or bounds.y_end > labels.height:
        raise MuralPartitionError("tile crop escapes the authoritative label field")
    pixels = b"".join(
        labels.pixels[y * labels.width + bounds.x_start : y * labels.width + bounds.x_end]
        for y in range(bounds.y_start, bounds.y_end)
    )
    return LabelField(
        width=bounds.width,
        height=bounds.height,
        label_values=labels.label_values,
        pixels=pixels,
    )


def _protected_sides(tile: TilePlan, layout: MuralLayout) -> tuple[TileSide, ...]:
    return tuple(
        side
        for condition, side in (
            (tile.row > 1, TileSide.TOP),
            (tile.column < layout.columns, TileSide.RIGHT),
            (tile.row < layout.rows, TileSide.BOTTOM),
            (tile.column > 1, TileSide.LEFT),
        )
        if condition
    )


def _build_seams(
    tiles: tuple[MuralLabelTile, ...],
    layout: MuralLayout,
) -> tuple[SharedTileSeam, ...]:
    by_grid = {(item.manifest.row, item.manifest.column): item for item in tiles}
    seams: list[SharedTileSeam] = []
    for row in range(1, layout.rows + 1):
        for column in range(1, layout.columns):
            left = by_grid[(row, column)]
            right = by_grid[(row, column + 1)]
            first_bounds = left.manifest.visible_master_bounds
            second_bounds = right.manifest.visible_master_bounds
            if (
                first_bounds.x_end != second_bounds.x_start
                or first_bounds.y_start != second_bounds.y_start
                or first_bounds.y_end != second_bounds.y_end
            ):
                raise MuralPartitionError("vertical neighbors do not share one integer boundary")
            seams.append(
                SharedTileSeam(
                    id=(f"seam-v-{first_bounds.x_end}-{first_bounds.y_start}-{first_bounds.y_end}"),
                    orientation=SeamOrientation.VERTICAL,
                    coordinate_px=first_bounds.x_end,
                    span_start_px=first_bounds.y_start,
                    span_end_px=first_bounds.y_end,
                    first_tile_id=left.manifest.tile_id,
                    second_tile_id=right.manifest.tile_id,
                    first_side=TileSide.RIGHT,
                    second_side=TileSide.LEFT,
                    first_guard_sha256=_guard_sha256(left.visible_labels, TileSide.RIGHT),
                    second_guard_sha256=_guard_sha256(right.visible_labels, TileSide.LEFT),
                )
            )
    for row in range(1, layout.rows):
        for column in range(1, layout.columns + 1):
            top = by_grid[(row, column)]
            bottom = by_grid[(row + 1, column)]
            first_bounds = top.manifest.visible_master_bounds
            second_bounds = bottom.manifest.visible_master_bounds
            if (
                first_bounds.y_end != second_bounds.y_start
                or first_bounds.x_start != second_bounds.x_start
                or first_bounds.x_end != second_bounds.x_end
            ):
                raise MuralPartitionError("horizontal neighbors do not share one integer boundary")
            seams.append(
                SharedTileSeam(
                    id=(f"seam-h-{first_bounds.y_end}-{first_bounds.x_start}-{first_bounds.x_end}"),
                    orientation=SeamOrientation.HORIZONTAL,
                    coordinate_px=first_bounds.y_end,
                    span_start_px=first_bounds.x_start,
                    span_end_px=first_bounds.x_end,
                    first_tile_id=top.manifest.tile_id,
                    second_tile_id=bottom.manifest.tile_id,
                    first_side=TileSide.BOTTOM,
                    second_side=TileSide.TOP,
                    first_guard_sha256=_guard_sha256(top.visible_labels, TileSide.BOTTOM),
                    second_guard_sha256=_guard_sha256(bottom.visible_labels, TileSide.TOP),
                )
            )
    return tuple(seams)


def _guard_sha256(labels: LabelField, side: TileSide) -> str:
    if side == TileSide.TOP:
        guard = labels.pixels[: labels.width]
    elif side == TileSide.BOTTOM:
        guard = labels.pixels[-labels.width :]
    elif side == TileSide.LEFT:
        guard = bytes(labels.pixels[y * labels.width] for y in range(labels.height))
    else:
        guard = bytes(labels.pixels[(y + 1) * labels.width - 1] for y in range(labels.height))
    return hashlib.sha256(guard).hexdigest()


def _protected_indices(
    width: int,
    height: int,
    sides: tuple[TileSide, ...],
) -> set[int]:
    indices: set[int] = set()
    if TileSide.TOP in sides:
        indices.update(range(width))
    if TileSide.BOTTOM in sides:
        indices.update(range((height - 1) * width, height * width))
    if TileSide.LEFT in sides:
        indices.update(y * width for y in range(height))
    if TileSide.RIGHT in sides:
        indices.update((y + 1) * width - 1 for y in range(height))
    return indices


def _partition_warnings(layout: MuralLayout) -> tuple[PartitionWarning, ...]:
    warnings: list[PartitionWarning] = []
    if layout.rows > 1 or layout.columns > 1:
        warnings.append(
            PartitionWarning(
                code=PartitionWarningCode.INDEPENDENT_SEAM_CLEANUP_FORBIDDEN,
                message=(
                    "Internal seam strips are immutable. Apply smoothing, island removal, and "
                    "other topology-affecting cleanup to the authoritative master before "
                    "partitioning."
                ),
            )
        )
    if layout.bleed_mm > 0:
        warnings.append(
            PartitionWarning(
                code=PartitionWarningCode.BLEED_SAMPLING_ONLY,
                message=(
                    "Bleed pixels are a separate sampling field and must not replace or mutate "
                    "the nominal visible tile labels."
                ),
            )
        )
    if layout.horizontal_gap_mm > 0 or layout.vertical_gap_mm > 0:
        warnings.append(
            PartitionWarning(
                code=PartitionWarningCode.ASSEMBLY_GAP_LAYOUT_ONLY,
                message=(
                    "Assembly gaps affect placement only and are not rasterized into the master "
                    "or any visible tile."
                ),
            )
        )
    return tuple(warnings)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _json_value(value: object) -> object:
    """Normalize nested Pydantic values before canonical fingerprinting."""

    return json.loads(
        json.dumps(
            value,
            default=lambda item: (
                item.model_dump(mode="json")
                if isinstance(item, BaseModel)
                else item.value
                if isinstance(item, Enum)
                else item
            ),
        )
    )
