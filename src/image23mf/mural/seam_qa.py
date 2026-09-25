"""Deterministic seam and assembly evidence for mural review surfaces.

This module describes diagnostic overlays; it never renders them into artwork.
The lossless recomposition hash is the proof that numbered tiles and exaggerated
seam guides remain review metadata rather than printable pixels.
"""

# ruff: noqa: UP045 -- Python 3.9 is supported and Pydantic evaluates these annotations.

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.transform import MillimetreSize, Rect
from image23mf.mural.partition import (
    MuralLabelPartition,
    SeamOrientation,
    TileSide,
    recompose_visible_tiles,
)
from image23mf.mural.planner import MuralPlan, PixelBoundaryRect
from image23mf.mural.topology_partition import MuralTopologyPartition

MURAL_SEAM_QA_SCHEMA_VERSION = 1


class MuralSeamQaError(ValueError):
    """The plan and partition cannot produce trustworthy seam evidence."""


class SeamQaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RiskSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class QaStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class TileRiskFinding(SeamQaModel):
    tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    severity: RiskSeverity
    message: str = Field(min_length=1, max_length=600)


class TileRiskSummary(SeamQaModel):
    status: QaStatus
    finding_count: int = Field(ge=0)
    info_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    findings: tuple[TileRiskFinding, ...] = ()

    @model_validator(mode="after")
    def counts_match_findings(self) -> TileRiskSummary:
        counts = {
            RiskSeverity.INFO: self.info_count,
            RiskSeverity.WARNING: self.warning_count,
            RiskSeverity.ERROR: self.error_count,
        }
        if self.finding_count != len(self.findings):
            raise ValueError("risk finding count must match the finding collection")
        if any(
            expected != sum(item.severity == severity for item in self.findings)
            for severity, expected in counts.items()
        ):
            raise ValueError("risk severity counts must match the finding collection")
        expected_status = (
            QaStatus.FAIL
            if self.error_count
            else QaStatus.WARNING
            if self.warning_count
            else QaStatus.PASS
        )
        if self.status != expected_status:
            raise ValueError("risk status must match its highest finding severity")
        return self


class SharedEdgeEvidence(SeamQaModel):
    seam_id: str = Field(pattern=r"^seam-[vh]-[0-9]+-[0-9]+-[0-9]+$")
    orientation: SeamOrientation
    coordinate_px: int = Field(ge=1)
    span_start_px: int = Field(ge=0)
    span_end_px: int = Field(ge=1)
    first_tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    second_tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    first_side: TileSide
    second_side: TileSide
    first_boundary_px: int = Field(ge=1)
    second_boundary_px: int = Field(ge=1)
    first_guard_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    second_guard_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_guard_role: Literal["first_tile_inner_edge_strip"] = "first_tile_inner_edge_strip"
    second_guard_role: Literal["second_tile_inner_edge_strip"] = "second_tile_inner_edge_strip"
    guard_hashes_expected_to_match: Literal[False] = False
    exact_coordinate_match: bool
    exact_span_match: bool
    no_gap_no_overlap: bool
    evidence_kind: Literal["raster_partition_boundary"] = "raster_partition_boundary"
    overlay_source: Literal["diagnostic_metadata_only"] = "diagnostic_metadata_only"

    @model_validator(mode="after")
    def exact_evidence_is_internally_consistent(self) -> SharedEdgeEvidence:
        if self.span_end_px <= self.span_start_px:
            raise ValueError("shared edge span must have positive length")
        if self.exact_coordinate_match != (self.first_boundary_px == self.second_boundary_px):
            raise ValueError("coordinate verdict must match the recorded boundaries")
        if self.no_gap_no_overlap != (self.exact_coordinate_match and self.exact_span_match):
            raise ValueError("gap/overlap verdict must match exact boundary evidence")
        return self


class TileSeamEvidence(SeamQaModel):
    tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    plate_number: int = Field(ge=1, le=2_500)
    row: int = Field(ge=1, le=50)
    column: int = Field(ge=1, le=50)
    rotation_degrees: Literal[0, 90]
    master_pixel_bounds: PixelBoundaryRect
    shared_edge_count: int = Field(ge=0, le=4)
    protected_sides: tuple[TileSide, ...] = ()
    visible_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector_clip_bounds_master_mm: Rect
    tile_topology_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tile_geometry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    topology_source_mode: Literal["master_topology_exact_label_clip"] = (
        "master_topology_exact_label_clip"
    )
    fits_build_plate: bool
    risk: TileRiskSummary


class ArtworkPurityEvidence(SeamQaModel):
    authoritative_master_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recomposed_visible_art_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visible_art_unchanged: bool
    overlay_storage: Literal["metadata_only"] = "metadata_only"
    overlay_pixels_written: Literal[0] = 0
    tile_number_pixels_written: Literal[0] = 0
    orientation_marker_pixels_written: Literal[0] = 0
    proof: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def unchanged_verdict_matches_hashes(self) -> ArtworkPurityEvidence:
        if self.visible_art_unchanged != (
            self.authoritative_master_sha256 == self.recomposed_visible_art_sha256
        ):
            raise ValueError("artwork purity verdict must match the recorded hashes")
        return self


class TopologyQaEvidence(SeamQaModel):
    evidence_kind: Literal["exact_master_topology_clip"] = "exact_master_topology_clip"
    label_partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    topology_partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seam_topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_topology_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_topology_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recomposed_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pixel_count: int = Field(gt=0)
    represented_pixel_count: int = Field(gt=0)
    every_master_pixel_represented: bool

    @model_validator(mode="after")
    def represented_pixels_are_closed(self) -> TopologyQaEvidence:
        if self.every_master_pixel_represented != (
            self.source_pixel_count == self.represented_pixel_count
        ):
            raise ValueError("topology coverage verdict must match represented pixel totals")
        return self


class MuralSeamQaReport(SeamQaModel):
    schema_version: Literal[1] = MURAL_SEAM_QA_SCHEMA_VERSION
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: int = Field(ge=1, le=50)
    columns: int = Field(ge=1, le=50)
    panel_size_mm: MillimetreSize
    master_size_mm: MillimetreSize
    assembled_size_mm: MillimetreSize
    horizontal_gap_mm: float = Field(ge=0, le=500)
    vertical_gap_mm: float = Field(ge=0, le=500)
    expected_seam_count: int = Field(ge=0)
    exact_shared_edge_count: int = Field(ge=0)
    failed_shared_edge_count: int = Field(ge=0)
    status: QaStatus
    artwork: ArtworkPurityEvidence
    topology: TopologyQaEvidence
    seams: tuple[SharedEdgeEvidence, ...] = ()
    tiles: tuple[TileSeamEvidence, ...] = Field(min_length=1, max_length=2_500)

    @model_validator(mode="after")
    def totals_and_status_are_consistent(self) -> MuralSeamQaReport:
        if len(self.seams) != self.expected_seam_count:
            raise ValueError("seam collection must match the expected grid seam count")
        if self.exact_shared_edge_count + self.failed_shared_edge_count != len(self.seams):
            raise ValueError("shared-edge totals must account for every seam")
        if len(self.tiles) != self.rows * self.columns:
            raise ValueError("tile evidence count must match the mural grid")
        has_tile_error = any(item.risk.error_count for item in self.tiles)
        has_tile_warning = any(item.risk.warning_count for item in self.tiles)
        expected_status = (
            QaStatus.FAIL
            if (
                self.failed_shared_edge_count
                or not self.artwork.visible_art_unchanged
                or not self.topology.every_master_pixel_represented
                or has_tile_error
            )
            else QaStatus.WARNING
            if has_tile_warning
            else QaStatus.PASS
        )
        if self.status != expected_status:
            raise ValueError("report status must match seam, artwork, and tile evidence")
        return self


def build_mural_seam_qa(
    partition: MuralLabelPartition,
    topology: MuralTopologyPartition,
    plan: MuralPlan,
    *,
    findings: tuple[TileRiskFinding, ...] = (),
) -> MuralSeamQaReport:
    """Summarize exact shared edges without modifying the partitioned artwork."""

    manifest = partition.manifest
    if manifest.request_fingerprint != plan.request_fingerprint:
        raise MuralSeamQaError("seam partition belongs to a different mural request")
    if manifest.source_fingerprint != plan.source_fingerprint:
        raise MuralSeamQaError("seam partition belongs to a different processed master")
    topology_manifest = topology.manifest
    if topology_manifest.label_partition_sha256 != manifest.partition_sha256:
        raise MuralSeamQaError("topology evidence belongs to a different label partition")
    if topology.labels.manifest != manifest:
        raise MuralSeamQaError("runtime topology is bound to different mural labels")
    if topology_manifest.master_labels_sha256 != manifest.master_labels.pixels_sha256:
        raise MuralSeamQaError("topology evidence uses a different authoritative label field")
    if topology_manifest.recomposed_labels_sha256 != manifest.master_labels.pixels_sha256:
        raise MuralSeamQaError("topology tiles do not recompose the authoritative label field")
    plan_tiles = {item.id: item for item in plan.tiles}
    partition_tiles = {item.tile_id: item for item in manifest.tiles}
    if set(plan_tiles) != set(partition_tiles):
        raise MuralSeamQaError("plan and seam partition contain different tiles")
    topology_tiles = {item.tile_id: item for item in topology_manifest.tiles}
    if set(topology_tiles) != set(plan_tiles):
        raise MuralSeamQaError("topology and mural plan contain different tiles")
    for tile_id, topology_tile in topology_tiles.items():
        # Planner pixel rows are top-down; physical topology clips are Cartesian
        # bottom-up. The partition recipe is the authoritative coordinate bridge.
        if (
            topology_tile.vector_clip_bounds_master_mm
            != partition_tiles[tile_id].topology.master_bounds_mm
        ):
            raise MuralSeamQaError("topology tile uses a different exact physical clip window")
        if (
            topology_tile.visible_labels_sha256
            != partition_tiles[tile_id].visible_labels.pixels_sha256
        ):
            raise MuralSeamQaError("topology tile uses different nominal visible labels")
    finding_tile_ids = {item.tile_id for item in findings}
    if not finding_tile_ids <= set(plan_tiles):
        raise MuralSeamQaError("tile risk evidence references an unknown mural tile")

    seams = tuple(_shared_edge_evidence(item, plan_tiles) for item in manifest.seams)
    seam_counts = {tile_id: 0 for tile_id in plan_tiles}
    for seam in seams:
        seam_counts[seam.first_tile_id] += 1
        seam_counts[seam.second_tile_id] += 1

    tiles = tuple(
        TileSeamEvidence(
            tile_id=tile.id,
            plate_number=tile.build_plate_index,
            row=tile.row,
            column=tile.column,
            rotation_degrees=tile.bed_fit.rotation_degrees,
            master_pixel_bounds=tile.master_pixel_bounds,
            shared_edge_count=seam_counts[tile.id],
            protected_sides=partition_tiles[tile.id].topology.protected_sides,
            visible_labels_sha256=partition_tiles[tile.id].visible_labels.pixels_sha256,
            vector_clip_bounds_master_mm=topology_tiles[tile.id].vector_clip_bounds_master_mm,
            tile_topology_artifact_sha256=(topology_tiles[tile.id].tile_topology_artifact_sha256),
            tile_geometry_fingerprint=topology_tiles[tile.id].tile_geometry_fingerprint,
            fits_build_plate=tile.bed_fit.fits,
            risk=_risk_summary(
                tile.id,
                tile.bed_fit.fits,
                tuple(item for item in findings if item.tile_id == tile.id),
            ),
        )
        for tile in plan.tiles
    )
    recomposed = recompose_visible_tiles(partition)
    recomposed_sha256 = hashlib.sha256(recomposed.pixels).hexdigest()
    artwork = ArtworkPurityEvidence(
        authoritative_master_sha256=manifest.master_labels.pixels_sha256,
        recomposed_visible_art_sha256=recomposed_sha256,
        visible_art_unchanged=recomposed_sha256 == manifest.master_labels.pixels_sha256,
        proof=(
            "Every nominal tile was recomposed into the authoritative master byte-for-byte. "
            "Seam guides, plate numbers, and orientation markers exist only as diagnostic "
            "metadata and write zero pixels into visible art."
        ),
    )
    topology_evidence = TopologyQaEvidence(
        label_partition_sha256=topology_manifest.label_partition_sha256,
        topology_partition_sha256=topology_manifest.partition_sha256,
        seam_topology_sha256=topology_manifest.seam_topology_sha256,
        master_topology_fingerprint=topology_manifest.master_topology_fingerprint,
        master_topology_artifact_sha256=topology_manifest.master_topology_artifact_sha256,
        recomposed_labels_sha256=topology_manifest.recomposed_labels_sha256,
        source_pixel_count=topology_manifest.source_pixel_count,
        represented_pixel_count=topology_manifest.represented_pixel_count,
        every_master_pixel_represented=(
            topology_manifest.source_pixel_count == topology_manifest.represented_pixel_count
        ),
    )
    exact_count = sum(item.no_gap_no_overlap for item in seams)
    failed_count = len(seams) - exact_count
    status = (
        QaStatus.FAIL
        if (
            failed_count
            or not artwork.visible_art_unchanged
            or not topology_evidence.every_master_pixel_represented
            or any(item.risk.error_count for item in tiles)
        )
        else QaStatus.WARNING
        if any(item.risk.warning_count for item in tiles)
        else QaStatus.PASS
    )
    return MuralSeamQaReport(
        request_fingerprint=plan.request_fingerprint,
        partition_sha256=manifest.partition_sha256,
        rows=manifest.layout.rows,
        columns=manifest.layout.columns,
        panel_size_mm=MillimetreSize(
            width=manifest.layout.panel_width_mm,
            height=manifest.layout.panel_height_mm,
        ),
        master_size_mm=manifest.master_size_mm,
        assembled_size_mm=manifest.assembled_size_mm,
        horizontal_gap_mm=manifest.layout.horizontal_gap_mm,
        vertical_gap_mm=manifest.layout.vertical_gap_mm,
        expected_seam_count=(
            manifest.layout.rows * (manifest.layout.columns - 1)
            + manifest.layout.columns * (manifest.layout.rows - 1)
        ),
        exact_shared_edge_count=exact_count,
        failed_shared_edge_count=failed_count,
        status=status,
        artwork=artwork,
        topology=topology_evidence,
        seams=seams,
        tiles=tiles,
    )


def _shared_edge_evidence(item, plan_tiles) -> SharedEdgeEvidence:
    first = plan_tiles[item.first_tile_id].master_pixel_bounds
    second = plan_tiles[item.second_tile_id].master_pixel_bounds
    if item.orientation == SeamOrientation.VERTICAL:
        first_boundary = first.x_end
        second_boundary = second.x_start
        span_match = (
            first.y_start == second.y_start == item.span_start_px
            and first.y_end == second.y_end == item.span_end_px
        )
    else:
        first_boundary = first.y_end
        second_boundary = second.y_start
        span_match = (
            first.x_start == second.x_start == item.span_start_px
            and first.x_end == second.x_end == item.span_end_px
        )
    coordinate_match = first_boundary == second_boundary == item.coordinate_px
    return SharedEdgeEvidence(
        seam_id=item.id,
        orientation=item.orientation,
        coordinate_px=item.coordinate_px,
        span_start_px=item.span_start_px,
        span_end_px=item.span_end_px,
        first_tile_id=item.first_tile_id,
        second_tile_id=item.second_tile_id,
        first_side=item.first_side,
        second_side=item.second_side,
        first_boundary_px=first_boundary,
        second_boundary_px=second_boundary,
        first_guard_sha256=item.first_guard_sha256,
        second_guard_sha256=item.second_guard_sha256,
        exact_coordinate_match=coordinate_match,
        exact_span_match=span_match,
        no_gap_no_overlap=coordinate_match and span_match,
    )


def _risk_summary(
    tile_id: str,
    fits_build_plate: bool,
    supplied: tuple[TileRiskFinding, ...],
) -> TileRiskSummary:
    findings = supplied
    if not fits_build_plate:
        findings = (
            *findings,
            TileRiskFinding(
                tile_id=tile_id,
                code="build_plate_fit",
                severity=RiskSeverity.ERROR,
                message="The tile does not fit the selected printable bed envelope.",
            ),
        )
    info = sum(item.severity == RiskSeverity.INFO for item in findings)
    warnings = sum(item.severity == RiskSeverity.WARNING for item in findings)
    errors = sum(item.severity == RiskSeverity.ERROR for item in findings)
    status = QaStatus.FAIL if errors else QaStatus.WARNING if warnings else QaStatus.PASS
    return TileRiskSummary(
        status=status,
        finding_count=len(findings),
        info_count=info,
        warning_count=warnings,
        error_count=errors,
        findings=findings,
    )
