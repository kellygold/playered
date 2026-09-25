"""Topology partition adapter derived from one authoritative mural master.

Geometry IR intentionally treats vector paths as construction evidence only; pixel
ownership comes from the exhaustive label field.  This adapter therefore binds every
tile to the master vector/topology fingerprints, applies the exact tile clip window,
and rebuilds canonical boundary graphs from immutable nominal label crops.  It never
invokes Potrace and never runs a tile-local cleanup policy.
"""

# ruff: noqa: UP045 -- Python 3.9 is supported and Pydantic evaluates these annotations.

from __future__ import annotations

import hashlib
import json
import math
import warnings
from collections import Counter, deque
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, Optional

import numpy as np
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.labels import LabelField
from image23mf.engine.transform import Rect
from image23mf.geometry import topology as topology_core
from image23mf.geometry.model import (
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
    RectangleBase,
    SharedEdge,
    SourceLabel,
)
from image23mf.geometry.topology import (
    SharedBoundaryTopologyResult,
    TopologyComponentEvidence,
    TopologyCoverageEvidence,
)
from image23mf.mural.partition import (
    MuralLabelPartition,
    MuralPartitionError,
    recompose_visible_tiles,
)
from image23mf.mural.planner import PixelBoundaryRect

MURAL_TOPOLOGY_PARTITION_SCHEMA_VERSION = 1


class MuralTopologyPartitionError(MuralPartitionError):
    """Master topology or tile topology evidence violates the mural contract."""


@dataclass(frozen=True)
class _PhysicalClipGrid:
    bounds: PixelBoundaryRect
    labels: LabelField
    x_coordinates_mm: tuple[Fraction, ...]
    y_coordinates_mm: tuple[Fraction, ...]


class TopologyPartitionModel(BaseModel):
    # Persisted v1 manifests used ``tile_raster_sha256``.  New code uses the
    # accurate nominal-label name, while canonical serialization deliberately
    # retains the legacy alias so existing manifest fingerprints stay valid.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        serialize_by_alias=True,
    )


class MasterComponentSlice(TopologyPartitionModel):
    component_index: int = Field(ge=0)
    master_island_id: str = Field(pattern=r"^island_[0-9a-f]{24}$")
    label_index: int = Field(ge=0, le=255)
    pixel_count: int = Field(ge=0)
    clipped_area_mm2: float = Field(gt=0)
    tile_island_ids: tuple[str, ...] = Field(min_length=1)


class SourceLabelLineage(TopologyPartitionModel):
    label_index: int = Field(ge=0, le=255)
    master_source_label_id: str = Field(pattern=r"^source-label_[0-9a-f]{24}$")
    tile_source_label_id: str = Field(pattern=r"^source-label_[0-9a-f]{24}$")


class MuralTileTopologyEvidence(TopologyPartitionModel):
    tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    source_mode: Literal["master_topology_exact_label_clip"] = "master_topology_exact_label_clip"
    vector_policy: Literal["master_construction_coordinates_exact_clip_window"] = (
        "master_construction_coordinates_exact_clip_window"
    )
    cleanup_policy: Literal["no_tile_local_cleanup"] = "no_tile_local_cleanup"
    master_topology_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_topology_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_vector_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_vector_path_ids: tuple[str, ...] = Field(min_length=1)
    master_vector_paths_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_vector_artifact_verification: Literal[
        "opaque_sha256_unverified", "supplied_bytes_sha256_verified"
    ]
    vector_clip_bounds_master_mm: Rect
    clipped_vector_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tile_topology_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tile_geometry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    nominal_label_crop_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        validation_alias=AliasChoices(
            "nominal_label_crop_sha256",
            "tile_raster_sha256",
        ),
        serialization_alias="tile_raster_sha256",
        description=(
            "SHA-256 of the immutable nominal visible-label crop. This is label "
            "recomposition evidence, not rasterized physical-topology evidence. "
            "Canonical v1 serialization retains the legacy tile_raster_sha256 key."
        ),
    )
    raster_evidence_policy: Literal["separate_nominal_label_recomposition"] = (
        "separate_nominal_label_recomposition"
    )
    visible_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    represented_pixel_count: int = Field(gt=0)
    source_label_lineage: tuple[SourceLabelLineage, ...] = Field(min_length=1)
    master_components: tuple[MasterComponentSlice, ...] = Field(min_length=1)

    @property
    def tile_raster_sha256(self) -> str:
        """Deprecated compatibility accessor for the nominal label-crop hash."""

        warnings.warn(
            "tile_raster_sha256 is deprecated; use nominal_label_crop_sha256",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.nominal_label_crop_sha256


class MuralTopologyPartitionManifest(TopologyPartitionModel):
    schema_version: Literal[1] = MURAL_TOPOLOGY_PARTITION_SCHEMA_VERSION
    label_partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_topology_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_topology_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_vector_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pixel_count: int = Field(gt=0)
    represented_pixel_count: int = Field(gt=0)
    recomposed_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tiles: tuple[MuralTileTopologyEvidence, ...] = Field(min_length=1, max_length=2_500)
    seam_topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def evidence_is_closed(self) -> MuralTopologyPartitionManifest:
        if self.source_pixel_count != self.represented_pixel_count:
            raise ValueError("tile topology evidence must represent every master pixel exactly")
        if len({item.tile_id for item in self.tiles}) != len(self.tiles):
            raise ValueError("tile topology identifiers must be unique")
        expected = _fingerprint(self.model_dump(mode="json", exclude={"partition_sha256"}))
        if self.partition_sha256 != expected:
            raise ValueError("topology partition fingerprint is not canonical")
        return self


@dataclass(frozen=True)
class MuralTileTopology:
    evidence: MuralTileTopologyEvidence
    topology: SharedBoundaryTopologyResult

    def __post_init__(self) -> None:
        if self.topology.topology_artifact_sha256 != self.evidence.tile_topology_artifact_sha256:
            raise MuralTopologyPartitionError("tile topology result does not match its evidence")
        if self.topology.document.fingerprint() != self.evidence.tile_geometry_fingerprint:
            raise MuralTopologyPartitionError("tile geometry document does not match its evidence")


@dataclass(frozen=True)
class MuralTopologyPartition:
    labels: MuralLabelPartition
    master: SharedBoundaryTopologyResult
    manifest: MuralTopologyPartitionManifest
    tiles: tuple[MuralTileTopology, ...]
    vector_artifact_bytes: Optional[bytes] = None

    def __post_init__(self) -> None:
        expected = _derive_topology_partition(
            self.labels, self.master, vector_artifact_bytes=self.vector_artifact_bytes
        )
        if self.tiles != expected[0] or self.manifest != expected[1]:
            raise MuralTopologyPartitionError(
                "runtime topology partition is not the canonical clip of its master topology"
            )
        self._validate_runtime_binding()

    def _validate_runtime_binding(self) -> None:
        if self.manifest.label_partition_sha256 != self.labels.manifest.partition_sha256:
            raise MuralTopologyPartitionError("topology uses a different label partition")
        if tuple(item.evidence for item in self.tiles) != self.manifest.tiles:
            raise MuralTopologyPartitionError("runtime topology tiles do not match their manifest")


def partition_master_topology(
    labels: MuralLabelPartition,
    master: SharedBoundaryTopologyResult,
    *,
    vector_artifact_bytes: Optional[bytes] = None,
) -> MuralTopologyPartition:
    """Clip authoritative master topology into exact, cleanup-free tile documents."""

    runtime_tiles, manifest = _derive_topology_partition(
        labels, master, vector_artifact_bytes=vector_artifact_bytes
    )
    return _from_canonical_derivation(
        labels,
        master,
        manifest,
        runtime_tiles,
        vector_artifact_bytes=vector_artifact_bytes,
    )


def _from_canonical_derivation(
    labels: MuralLabelPartition,
    master: SharedBoundaryTopologyResult,
    manifest: MuralTopologyPartitionManifest,
    tiles: tuple[MuralTileTopology, ...],
    *,
    vector_artifact_bytes: Optional[bytes],
) -> MuralTopologyPartition:
    """Build the frozen runtime from the canonical payload derived immediately above.

    The public factory has already recomputed every cell clip and canonical identifier, so
    invoking the dataclass constructor would repeat the full pixel-to-geometry derivation.
    Direct dataclass construction still runs ``__post_init__`` and independently derives the
    canonical payload, preserving fail-closed validation for deserialized or rewritten input.
    """
    partition = object.__new__(MuralTopologyPartition)
    object.__setattr__(partition, "labels", labels)
    object.__setattr__(partition, "master", master)
    object.__setattr__(partition, "manifest", manifest)
    object.__setattr__(partition, "tiles", tiles)
    object.__setattr__(partition, "vector_artifact_bytes", vector_artifact_bytes)
    partition._validate_runtime_binding()
    return partition


def _derive_topology_partition(
    labels: MuralLabelPartition,
    master: SharedBoundaryTopologyResult,
    *,
    vector_artifact_bytes: Optional[bytes],
) -> tuple[tuple[MuralTileTopology, ...], MuralTopologyPartitionManifest]:
    master_labels = recompose_visible_tiles(labels)
    document = master.document
    _validate_canonical_geometry_ids(document)
    vector_sha256 = document.capabilities.vector_geometry.artifact_sha256
    topology_sha256 = document.capabilities.topology.artifact_sha256
    if (
        document.capabilities.vector_geometry.status != CapabilityStatus.AVAILABLE
        or vector_sha256 is None
    ):
        raise MuralTopologyPartitionError("master vector construction evidence is unavailable")
    if (
        document.capabilities.topology.status != CapabilityStatus.AVAILABLE
        or topology_sha256 is None
    ):
        raise MuralTopologyPartitionError("master shared topology evidence is unavailable")
    if topology_sha256 != master.topology_artifact_sha256:
        raise MuralTopologyPartitionError("master topology capability fingerprint is inconsistent")
    recomputed_master_topology_sha256 = hashlib.sha256(
        topology_core._canonical_json(  # noqa: SLF001
            {
                "contours": [item.model_dump(mode="json") for item in document.contours],
                "islands": [item.model_dump(mode="json") for item in document.islands],
                "label_field_sha256": master.label_field_sha256,
                "shared_edges": [item.model_dump(mode="json") for item in document.shared_edges],
                "source_vector_path_ids": master.source_vector_path_ids,
            }
        )
    ).hexdigest()
    if recomputed_master_topology_sha256 != topology_sha256:
        raise MuralTopologyPartitionError(
            "master topology artifact does not match its canonical document graph"
        )
    vector_verification: Literal["opaque_sha256_unverified", "supplied_bytes_sha256_verified"] = (
        "opaque_sha256_unverified"
    )
    if vector_artifact_bytes is not None:
        if hashlib.sha256(vector_artifact_bytes).hexdigest() != vector_sha256:
            raise MuralTopologyPartitionError(
                "supplied vector artifact bytes do not match external SHA-256 provenance"
            )
        vector_verification = "supplied_bytes_sha256_verified"
    if labels.manifest.authoritative_vector_sha256 != vector_sha256:
        raise MuralTopologyPartitionError(
            "label partition is not bound to the authoritative master vector artifact"
        )
    if labels.manifest.authoritative_topology_sha256 != topology_sha256:
        raise MuralTopologyPartitionError(
            "label partition is not bound to the authoritative master topology artifact"
        )
    if labels.manifest.source_asset_sha256 != document.source_asset_sha256:
        raise MuralTopologyPartitionError("master topology uses a different source asset")
    if (
        master.label_field_sha256 != labels.manifest.master_labels.pixels_sha256
        or document.processed_labels_sha256 != labels.manifest.master_labels.pixels_sha256
    ):
        raise MuralTopologyPartitionError("master topology does not use the mural master labels")
    if (document.source_width_px, document.source_height_px) != (
        master_labels.width,
        master_labels.height,
    ):
        raise MuralTopologyPartitionError("master topology label-field dimensions are stale")
    if (
        document.canvas_width_mm != labels.manifest.master_size_mm.width
        or document.canvas_height_mm != labels.manifest.master_size_mm.height
    ):
        raise MuralTopologyPartitionError("master topology physical canvas is stale")
    if master.coverage.gap_area_mm2 != 0 or master.coverage.overlap_area_mm2 != 0:
        raise MuralTopologyPartitionError("master topology is not exhaustive and gap-free")

    path_by_id = {item.id: item for item in document.paths}
    try:
        vector_paths = tuple(path_by_id[item] for item in master.source_vector_path_ids)
    except KeyError as error:
        raise MuralTopologyPartitionError(
            "master topology references missing vector construction evidence"
        ) from error
    if not vector_paths or any(item.purpose != "construction" for item in vector_paths):
        raise MuralTopologyPartitionError(
            "master vector evidence must contain construction paths only"
        )

    component_assignment = _master_component_assignment(master_labels)
    component_by_index = {item.component_index: item for item in master.components}
    component_pixel_counts = Counter(component_assignment)
    _validate_master_components(
        master,
        master_labels,
        component_assignment,
        component_by_index,
        component_pixel_counts,
    )

    runtime_tiles = tuple(
        _partition_tile_topology(
            label_tile,
            master,
            component_assignment,
            component_by_index,
            vector_paths,
            labels,
            vector_verification,
        )
        for label_tile in labels.tiles
    )
    recomposed = recompose_topology_labels(labels, runtime_tiles)
    if recomposed != master_labels:
        raise MuralTopologyPartitionError(
            "nominal label crops did not losslessly recompose the master label field"
        )
    represented = sum(item.evidence.represented_pixel_count for item in runtime_tiles)
    seam_sha256 = _fingerprint(
        {
            "label_seams": [item.model_dump(mode="json") for item in labels.manifest.seams],
            "tile_topologies": [
                {
                    "tile_id": item.evidence.tile_id,
                    # ``raster`` is retained only as the canonical v1 fingerprint
                    # token. Its value is explicitly nominal label-crop evidence.
                    "raster": item.evidence.nominal_label_crop_sha256,
                    "topology": item.evidence.tile_topology_artifact_sha256,
                }
                for item in runtime_tiles
            ],
        }
    )
    manifest_payload = {
        "schema_version": MURAL_TOPOLOGY_PARTITION_SCHEMA_VERSION,
        "label_partition_sha256": labels.manifest.partition_sha256,
        "master_labels_sha256": labels.manifest.master_labels.pixels_sha256,
        "master_topology_fingerprint": master.fingerprint(),
        "master_topology_artifact_sha256": topology_sha256,
        "master_vector_artifact_sha256": vector_sha256,
        "source_pixel_count": master_labels.width * master_labels.height,
        "represented_pixel_count": represented,
        "recomposed_labels_sha256": hashlib.sha256(recomposed.pixels).hexdigest(),
        "tiles": tuple(item.evidence for item in runtime_tiles),
        "seam_topology_sha256": seam_sha256,
    }
    manifest = MuralTopologyPartitionManifest(
        **manifest_payload,
        partition_sha256=_fingerprint(_json_value(manifest_payload)),
    )
    return runtime_tiles, manifest


def recompose_topology_labels(
    labels: MuralLabelPartition,
    tiles: tuple[MuralTileTopology, ...],
) -> LabelField:
    """Recompose immutable nominal label crops in master pixel space.

    This verifies only the label evidence carried under
    ``raster_evidence_policy=separate_nominal_label_recomposition``. Physical
    geometry, area, and seam continuity are proved independently by the topology
    partition and seam-QA contracts; this function never rasterizes geometry.
    """

    if len(tiles) != len(labels.tiles):
        raise MuralTopologyPartitionError("topology recomposition requires every tile")
    by_id = {item.evidence.tile_id: item for item in tiles}
    if len(by_id) != len(tiles):
        raise MuralTopologyPartitionError("topology recomposition contains duplicate tiles")
    for label_tile in labels.tiles:
        tile = by_id.get(label_tile.manifest.tile_id)
        if tile is None:
            raise MuralTopologyPartitionError("topology recomposition is missing a tile")
        expected_sha = hashlib.sha256(label_tile.visible_labels.pixels).hexdigest()
        if (
            tile.evidence.visible_labels_sha256 != expected_sha
            or tile.evidence.raster_evidence_policy != "separate_nominal_label_recomposition"
        ):
            raise MuralTopologyPartitionError("tile nominal label evidence is stale")
    return recompose_visible_tiles(labels)


def _partition_tile_topology(
    label_tile,
    master: SharedBoundaryTopologyResult,
    component_assignment: list[int],
    component_by_index: dict,
    vector_paths: tuple[Path2D, ...],
    labels: MuralLabelPartition,
    vector_verification: Literal["opaque_sha256_unverified", "supplied_bytes_sha256_verified"],
) -> MuralTileTopology:
    document = master.document
    recipe = label_tile.manifest.topology
    clip_grid = _physical_clip_grid(
        labels.master_labels,
        master_width_mm=document.canvas_width_mm,
        master_height_mm=document.canvas_height_mm,
        clip=recipe.master_bounds_mm,
    )
    topology_labels = clip_grid.labels
    present_labels = topology_labels.label_values
    tile_labels_sha256 = hashlib.sha256(topology_labels.pixels).hexdigest()
    master_label_by_index = {item.label_index: item for item in document.source_labels}
    source_labels = tuple(
        sorted(
            (
                SourceLabel.create(
                    source_asset_id=master_label_by_index[index].source_asset_id,
                    processed_labels_sha256=tile_labels_sha256,
                    label_index=index,
                    name=master_label_by_index[index].name,
                    color_hex=master_label_by_index[index].color_hex,
                    palette_color_id=master_label_by_index[index].palette_color_id,
                    material_id=master_label_by_index[index].material_id,
                    classification=master_label_by_index[index].classification,
                )
                for index in present_labels
            ),
            key=lambda item: item.id,
        )
    )
    needed_materials = {item.material_id for item in source_labels}
    materials = tuple(item for item in document.materials if item.id in needed_materials)
    palette_ids = {material.palette_color_id for material in materials}
    palette_order = tuple(item for item in document.palette_color_order if item in palette_ids)
    clip_path = _clip_window_path(
        recipe.local_canvas_size_mm.width,
        recipe.local_canvas_size_mm.height,
    )
    master_vector_sha256 = document.capabilities.vector_geometry.artifact_sha256
    assert master_vector_sha256 is not None  # validated by the public entry point
    master_vector_paths_sha256 = _fingerprint(
        [item.model_dump(mode="json") for item in vector_paths]
    )
    clipped_vector_sha256 = _fingerprint(
        {
            "clip_bounds_master_mm": recipe.master_bounds_mm.model_dump(mode="json"),
            "clipped_path": clip_path.model_dump(mode="json"),
            "master_vector_artifact_sha256": master_vector_sha256,
            "master_vector_paths": [item.model_dump(mode="json") for item in vector_paths],
            "master_vector_paths_sha256": master_vector_paths_sha256,
            "policy": "master-construction-coordinates-exact-clip-window-v2",
        }
    )
    topology, master_component_islands, clipped_component_areas = _clip_master_topology(
        topology_labels,
        master=master,
        component_assignment=component_assignment,
        component_by_index=component_by_index,
        bounds=clip_grid.bounds,
        clip_bounds_master_mm=recipe.master_bounds_mm,
        x_coordinates_mm=clip_grid.x_coordinates_mm,
        y_coordinates_mm=clip_grid.y_coordinates_mm,
        source_asset_sha256=document.source_asset_sha256,
        source_labels=source_labels,
        materials=materials,
        base=RectangleBase.create(
            center=Point2(
                x_mm=recipe.local_canvas_size_mm.width / 2,
                y_mm=recipe.local_canvas_size_mm.height / 2,
            ),
            width_mm=recipe.local_canvas_size_mm.width,
            height_mm=recipe.local_canvas_size_mm.height,
        ),
        canvas_width_mm=recipe.local_canvas_size_mm.width,
        canvas_height_mm=recipe.local_canvas_size_mm.height,
        construction_path=clip_path,
        vector_artifact_sha256=clipped_vector_sha256,
        palette_color_order=palette_order,
    )
    bounds = label_tile.manifest.visible_master_bounds
    component_counts = Counter(
        component_assignment[y * labels.manifest.master_labels.width + x]
        for y in range(bounds.y_start, bounds.y_end)
        for x in range(bounds.x_start, bounds.x_end)
    )
    components = tuple(
        MasterComponentSlice(
            component_index=index,
            master_island_id=component_by_index[index].island_id,
            label_index=component_by_index[index].label_index,
            pixel_count=component_counts.get(index, 0),
            clipped_area_mm2=clipped_component_areas[index],
            tile_island_ids=master_component_islands[index],
        )
        for index in sorted(master_component_islands)
    )
    tile_label_by_index = {item.label_index: item for item in source_labels}
    lineage = tuple(
        SourceLabelLineage(
            label_index=index,
            master_source_label_id=master_label_by_index[index].id,
            tile_source_label_id=tile_label_by_index[index].id,
        )
        for index in present_labels
    )
    evidence = MuralTileTopologyEvidence(
        tile_id=label_tile.manifest.tile_id,
        master_topology_fingerprint=master.fingerprint(),
        master_topology_artifact_sha256=master.topology_artifact_sha256,
        master_vector_artifact_sha256=master_vector_sha256,
        master_vector_path_ids=tuple(item.id for item in vector_paths),
        master_vector_paths_sha256=master_vector_paths_sha256,
        external_vector_artifact_verification=vector_verification,
        vector_clip_bounds_master_mm=recipe.master_bounds_mm,
        clipped_vector_artifact_sha256=clipped_vector_sha256,
        tile_topology_artifact_sha256=topology.topology_artifact_sha256,
        tile_geometry_fingerprint=topology.document.fingerprint(),
        nominal_label_crop_sha256=hashlib.sha256(label_tile.visible_labels.pixels).hexdigest(),
        visible_labels_sha256=hashlib.sha256(label_tile.visible_labels.pixels).hexdigest(),
        represented_pixel_count=len(label_tile.visible_labels.pixels),
        source_label_lineage=lineage,
        master_components=components,
    )
    return MuralTileTopology(evidence=evidence, topology=topology)


def _physical_clip_grid(
    master: LabelField,
    *,
    master_width_mm: float,
    master_height_mm: float,
    clip: Rect,
) -> _PhysicalClipGrid:
    width_mm = Fraction(str(master_width_mm))
    height_mm = Fraction(str(master_height_mm))
    left = Fraction(str(clip.x))
    right = Fraction(str(clip.right))
    bottom = Fraction(str(clip.y))
    top = Fraction(str(clip.bottom))
    columns = [
        column
        for column in range(master.width)
        if Fraction((column + 1), master.width) * width_mm > left
        and Fraction(column, master.width) * width_mm < right
    ]
    geometry_rows = [
        row
        for row in range(master.height)
        if Fraction((row + 1), master.height) * height_mm > bottom
        and Fraction(row, master.height) * height_mm < top
    ]
    if not columns or not geometry_rows:
        raise MuralTopologyPartitionError("physical tile clip contains no master cells")
    x_start, x_end = min(columns), max(columns) + 1
    geometry_y_start, geometry_y_end = min(geometry_rows), max(geometry_rows) + 1
    source_y_start = master.height - geometry_y_end
    source_y_end = master.height - geometry_y_start
    pixels = b"".join(
        master.pixels[row * master.width + x_start : row * master.width + x_end]
        for row in range(source_y_start, source_y_end)
    )
    x_global = (
        left,
        *(
            Fraction(column, master.width) * width_mm
            for column in range(x_start + 1, x_end)
            if left < Fraction(column, master.width) * width_mm < right
        ),
        right,
    )
    y_global = (
        bottom,
        *(
            Fraction(row, master.height) * height_mm
            for row in range(geometry_y_start + 1, geometry_y_end)
            if bottom < Fraction(row, master.height) * height_mm < top
        ),
        top,
    )
    return _PhysicalClipGrid(
        bounds=PixelBoundaryRect(
            x_start=x_start,
            x_end=x_end,
            y_start=source_y_start,
            y_end=source_y_end,
        ),
        labels=LabelField(
            width=x_end - x_start,
            height=source_y_end - source_y_start,
            label_values=tuple(sorted(set(pixels))),
            pixels=pixels,
        ),
        x_coordinates_mm=tuple(value - left for value in x_global),
        y_coordinates_mm=tuple(value - bottom for value in y_global),
    )


def _physical_boundary_path(
    loop: tuple[tuple[int, int], ...],
    x_coordinates_mm: tuple[Fraction, ...],
    y_coordinates_mm: tuple[Fraction, ...],
) -> Path2D:
    points = tuple(
        Point2(
            x_mm=round(float(x_coordinates_mm[x]), 6),
            y_mm=round(float(y_coordinates_mm[y]), 6),
        )
        for x, y in loop
    )
    return Path2D.create(
        purpose="boundary",
        start=points[0],
        segments=tuple(LineSegment(end=point) for point in (*points[1:], points[0])),
        closed=True,
    )


def _derive_physical_shared_edges(
    assignment_source_y: np.ndarray,
    island_by_component: dict[int, str],
    x_coordinates_mm: tuple[Fraction, ...],
    y_coordinates_mm: tuple[Fraction, ...],
) -> tuple[SharedEdge, ...]:
    assignment = np.flipud(assignment_source_y)
    height, width = assignment.shape
    intervals: dict[tuple[tuple[str, str], str, Fraction], list[tuple[Fraction, Fraction]]] = {}
    for y in range(height):
        for x in range(width):
            component = int(assignment[y, x])
            if x + 1 < width and int(assignment[y, x + 1]) != component:
                adjacent = tuple(
                    sorted(
                        (
                            island_by_component[component],
                            island_by_component[int(assignment[y, x + 1])],
                        )
                    )
                )
                intervals.setdefault((adjacent, "vertical", x_coordinates_mm[x + 1]), []).append(
                    (y_coordinates_mm[y], y_coordinates_mm[y + 1])
                )
            if y + 1 < height and int(assignment[y + 1, x]) != component:
                adjacent = tuple(
                    sorted(
                        (
                            island_by_component[component],
                            island_by_component[int(assignment[y + 1, x])],
                        )
                    )
                )
                intervals.setdefault((adjacent, "horizontal", y_coordinates_mm[y + 1]), []).append(
                    (x_coordinates_mm[x], x_coordinates_mm[x + 1])
                )
    result: list[SharedEdge] = []
    for (adjacent, orientation, fixed), values in sorted(intervals.items()):
        for start, end in _merge_fraction_intervals(values):
            if orientation == "vertical":
                first = Point2(x_mm=round(float(fixed), 6), y_mm=round(float(start), 6))
                second = Point2(x_mm=round(float(fixed), 6), y_mm=round(float(end), 6))
            else:
                first = Point2(x_mm=round(float(start), 6), y_mm=round(float(fixed), 6))
                second = Point2(x_mm=round(float(end), 6), y_mm=round(float(fixed), 6))
            result.append(
                SharedEdge.create(
                    start=first,
                    end=second,
                    adjacent_island_ids=adjacent,
                    owner_island_id=adjacent[0],
                )
            )
    return tuple(result)


def _merge_fraction_intervals(
    values: list[tuple[Fraction, Fraction]],
) -> tuple[tuple[Fraction, Fraction], ...]:
    ordered = sorted(values)
    result: list[tuple[Fraction, Fraction]] = []
    for start, end in ordered:
        if result and result[-1][1] == start:
            result[-1] = (result[-1][0], end)
        else:
            result.append((start, end))
    return tuple(result)


def _validate_clip_window_paths(
    document: GeometryDocument,
    *,
    clip_bounds_master_mm: Rect,
    master_width_mm: float,
    master_height_mm: float,
    master_width_px: int,
    master_height_px: int,
) -> None:
    x_pitch = Fraction(str(master_width_mm)) / master_width_px
    y_pitch = Fraction(str(master_height_mm)) / master_height_px
    clip_x = {Fraction(str(clip_bounds_master_mm.x)), Fraction(str(clip_bounds_master_mm.right))}
    clip_y = {Fraction(str(clip_bounds_master_mm.y)), Fraction(str(clip_bounds_master_mm.bottom))}
    path_by_id = {item.id: item for item in document.paths}
    for contour in document.contours:
        path = path_by_id[contour.path_id]
        for point in (path.start, *(segment.end for segment in path.segments)):
            master_x = Fraction(str(point.x_mm)) + Fraction(str(clip_bounds_master_mm.x))
            master_y = Fraction(str(point.y_mm)) + Fraction(str(clip_bounds_master_mm.y))
            x_on_grid = abs(float(master_x / x_pitch) - round(float(master_x / x_pitch))) < 1e-5
            y_on_grid = abs(float(master_y / y_pitch) - round(float(master_y / y_pitch))) < 1e-5
            if not (x_on_grid or master_x in clip_x) or not (y_on_grid or master_y in clip_y):
                raise MuralTopologyPartitionError(
                    "tile ownership vertex is not on a master grid edge or exact panel seam"
                )


def _validate_canonical_geometry_ids(document: GeometryDocument) -> None:
    for collection in (
        document.paths,
        document.contours,
        document.materials,
        document.source_labels,
        document.islands,
        document.shared_edges,
    ):
        for item in collection:
            if item.id != item.expected_id():
                raise MuralTopologyPartitionError(
                    f"geometry entity {item.id} has stale content-derived identity"
                )
    if document.base.id != document.base.expected_id():
        raise MuralTopologyPartitionError("geometry base has stale content-derived identity")


def _clip_master_topology(
    topology_labels: LabelField,
    *,
    master: SharedBoundaryTopologyResult,
    component_assignment: list[int],
    component_by_index: dict[int, TopologyComponentEvidence],
    bounds: PixelBoundaryRect,
    clip_bounds_master_mm: Rect,
    x_coordinates_mm: tuple[Fraction, ...],
    y_coordinates_mm: tuple[Fraction, ...],
    source_asset_sha256: str,
    source_labels: tuple[SourceLabel, ...],
    materials: tuple[Material, ...],
    base: RectangleBase,
    canvas_width_mm: float,
    canvas_height_mm: float,
    construction_path: Path2D,
    vector_artifact_sha256: str,
    palette_color_order: tuple[str, ...],
) -> tuple[
    SharedBoundaryTopologyResult,
    dict[int, tuple[str, ...]],
    dict[int, float],
]:
    """Intersect the verified master component cell complex with one tile rectangle."""

    width = topology_labels.width
    height = topology_labels.height
    master_width = master.document.source_width_px
    master_components = np.empty((height, width), dtype=np.int64)
    for local_row in range(height):
        start = (bounds.y_start + local_row) * master_width + bounds.x_start
        master_components[local_row, :] = component_assignment[start : start + width]

    fragment_assignment = np.full((height, width), -1, dtype=np.int64)
    fragments: list[tuple[int, int, tuple[tuple[int, int], ...]]] = []
    for row in range(height):
        for column in range(width):
            if fragment_assignment[row, column] >= 0:
                continue
            fragment_index = len(fragments)
            master_index = int(master_components[row, column])
            label = topology_labels.pixels[row * width + column]
            pending = [(row, column)]
            fragment_assignment[row, column] = fragment_index
            cells: list[tuple[int, int]] = []
            while pending:
                current_row, current_column = pending.pop()
                cells.append((current_column, height - 1 - current_row))
                for next_row, next_column in (
                    (current_row - 1, current_column),
                    (current_row, current_column - 1),
                    (current_row, current_column + 1),
                    (current_row + 1, current_column),
                ):
                    if not (0 <= next_row < height and 0 <= next_column < width):
                        continue
                    if fragment_assignment[next_row, next_column] >= 0:
                        continue
                    if int(master_components[next_row, next_column]) != master_index:
                        continue
                    fragment_assignment[next_row, next_column] = fragment_index
                    pending.append((next_row, next_column))
            fragments.append((master_index, label, tuple(sorted(cells))))

    protected = topology_core._junction_points(fragment_assignment)  # noqa: SLF001
    label_by_index = {item.label_index: item for item in source_labels}
    paths: list[Path2D] = [construction_path]
    contours: list[Contour2D] = []
    islands: list[Island2D] = []
    component_records: list[TopologyComponentEvidence] = []
    island_by_fragment: dict[int, str] = {}
    master_component_islands: dict[int, list[str]] = {}
    clipped_component_areas: dict[int, float] = {}
    x_area_indices, y_area_indices, cell_areas = _cell_area_lookup(
        x_coordinates_mm,
        y_coordinates_mm,
    )
    for fragment_index, (master_index, label, cells) in enumerate(fragments):
        if component_by_index[master_index].label_index != label:
            raise MuralTopologyPartitionError("clipped master component changed label ownership")
        loops = topology_core._component_loops(cells, protected=protected)  # noqa: SLF001
        exterior_loops = [item for item in loops if topology_core._signed_grid_area(item) > 0]  # noqa: SLF001
        hole_loops = [item for item in loops if topology_core._signed_grid_area(item) < 0]  # noqa: SLF001
        if len(exterior_loops) != 1:
            raise MuralTopologyPartitionError(
                "master topology clip produced a non-manifold fragment"
            )
        exterior_path = _physical_boundary_path(
            exterior_loops[0], x_coordinates_mm, y_coordinates_mm
        )
        hole_paths = tuple(
            _physical_boundary_path(loop, x_coordinates_mm, y_coordinates_mm) for loop in hole_loops
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
        source_label = label_by_index[label]
        island = Island2D.create(
            exterior_contour_id=exterior_contour.id,
            hole_contour_ids=tuple(sorted(item.id for item in hole_contours)),
            material_id=source_label.material_id,
            source_label_id=source_label.id,
        )
        paths.extend((exterior_path, *hole_paths))
        contours.extend((exterior_contour, *hole_contours))
        islands.append(island)
        island_by_fragment[fragment_index] = island.id
        master_component_islands.setdefault(master_index, []).append(island.id)
        # Preserve the sequential IEEE-754 accumulation used by existing artifacts.
        # Python 3.12 changed sum(float) to compensated summation, changing hashes.
        fragment_area = 0.0
        for x, y in cells:
            fragment_area += cell_areas[y_area_indices[y]][x_area_indices[x]]
        clipped_component_areas[master_index] = (
            clipped_component_areas.get(master_index, 0.0) + fragment_area
        )
        component_records.append(
            TopologyComponentEvidence(
                component_index=fragment_index,
                label_index=label,
                island_id=island.id,
                pixel_count=len(cells),
                area_mm2=round(fragment_area, 12),
                exterior_path_id=exterior_path.id,
                hole_path_ids=tuple(sorted(item.id for item in hole_paths)),
            )
        )

    shared_edges = _derive_physical_shared_edges(
        fragment_assignment,
        island_by_fragment,
        x_coordinates_mm,
        y_coordinates_mm,
    )
    canonical_paths = tuple(sorted(paths, key=lambda item: item.id))
    canonical_contours = tuple(sorted(contours, key=lambda item: item.id))
    canonical_islands = tuple(sorted(islands, key=lambda item: item.id))
    canonical_shared = tuple(sorted(shared_edges, key=lambda item: item.id))
    label_sha256 = hashlib.sha256(topology_labels.pixels).hexdigest()
    topology_payload = {
        "contours": [item.model_dump(mode="json") for item in canonical_contours],
        "islands": [item.model_dump(mode="json") for item in canonical_islands],
        "label_field_sha256": label_sha256,
        "shared_edges": [item.model_dump(mode="json") for item in canonical_shared],
        "source_vector_path_ids": (construction_path.id,),
    }
    topology_sha256 = hashlib.sha256(
        topology_core._canonical_json(topology_payload)  # noqa: SLF001
    ).hexdigest()
    geometry_sha256 = topology_core._source_contract_sha256(  # noqa: SLF001
        topology_labels,
        source_asset_sha256=source_asset_sha256,
        source_labels=source_labels,
        materials=materials,
        base=base,
        canvas_width_mm=canvas_width_mm,
        canvas_height_mm=canvas_height_mm,
    )
    capabilities = GeometryCapabilities(
        geometry_ir=CapabilityState(
            status=CapabilityStatus.AVAILABLE, artifact_sha256=geometry_sha256
        ),
        vector_geometry=CapabilityState(
            status=CapabilityStatus.AVAILABLE, artifact_sha256=vector_artifact_sha256
        ),
        topology=CapabilityState(
            status=CapabilityStatus.AVAILABLE, artifact_sha256=topology_sha256
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
    document = GeometryDocument(
        schema_version=1,
        source_asset_sha256=source_asset_sha256,
        processed_labels_sha256=label_sha256,
        source_width_px=width,
        source_height_px=height,
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
    coverage = TopologyCoverageEvidence(
        source_pixel_count=width * height,
        represented_pixel_count=width * height,
        source_area_mm2=round(canvas_width_mm * canvas_height_mm, 12),
        represented_area_mm2=round(canvas_width_mm * canvas_height_mm, 12),
        gap_area_mm2=0.0,
        overlap_area_mm2=0.0,
    )
    result = SharedBoundaryTopologyResult(
        document=document,
        components=tuple(component_records),
        coverage=coverage,
        source_vector_path_ids=(construction_path.id,),
        label_field_sha256=label_sha256,
        topology_artifact_sha256=topology_sha256,
    )
    _validate_canonical_geometry_ids(result.document)
    _validate_clip_window_paths(
        result.document,
        clip_bounds_master_mm=clip_bounds_master_mm,
        master_width_mm=master.document.canvas_width_mm,
        master_height_mm=master.document.canvas_height_mm,
        master_width_px=master.document.source_width_px,
        master_height_px=master.document.source_height_px,
    )
    return (
        result,
        {index: tuple(sorted(ids)) for index, ids in master_component_islands.items()},
        {index: round(area, 12) for index, area in clipped_component_areas.items()},
    )


def _cell_area_lookup(
    x_coordinates_mm: tuple[Fraction, ...],
    y_coordinates_mm: tuple[Fraction, ...],
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[tuple[float, ...], ...]]:
    """Cache exact rational cell areas by unique width/height dimensions.

    Fraction multiplication is intentionally retained once per unique dimension pair. Raster
    clips normally have one interior pitch plus at most two fractional boundary widths, so this
    replaces roughly one exact multiplication per represented pixel with integer tuple lookups.
    The cached float for every cell is byte-for-byte the same conversion used by the original
    per-cell expression.
    """

    x_widths = tuple(
        x_coordinates_mm[index + 1] - x_coordinates_mm[index]
        for index in range(len(x_coordinates_mm) - 1)
    )
    y_heights = tuple(
        y_coordinates_mm[index + 1] - y_coordinates_mm[index]
        for index in range(len(y_coordinates_mm) - 1)
    )
    unique_x = tuple(sorted(set(x_widths)))
    unique_y = tuple(sorted(set(y_heights)))
    x_index = {value: index for index, value in enumerate(unique_x)}
    y_index = {value: index for index, value in enumerate(unique_y)}
    areas = tuple(tuple(float(width * height) for width in unique_x) for height in unique_y)
    return (
        tuple(x_index[value] for value in x_widths),
        tuple(y_index[value] for value in y_heights),
        areas,
    )


def _master_component_assignment(labels: LabelField) -> list[int]:
    """Recreate topology's deterministic source-row component numbering without cleanup."""

    assignment = [-1] * (labels.width * labels.height)
    component_index = 0
    for row in range(labels.height):
        for column in range(labels.width):
            start = row * labels.width + column
            if assignment[start] >= 0:
                continue
            label = labels.pixels[start]
            assignment[start] = component_index
            pending = deque(((row, column),))
            while pending:
                current_row, current_column = pending.pop()
                for next_row, next_column in (
                    (current_row - 1, current_column),
                    (current_row, current_column - 1),
                    (current_row, current_column + 1),
                    (current_row + 1, current_column),
                ):
                    if not (0 <= next_row < labels.height and 0 <= next_column < labels.width):
                        continue
                    index = next_row * labels.width + next_column
                    if assignment[index] >= 0 or labels.pixels[index] != label:
                        continue
                    assignment[index] = component_index
                    pending.append((next_row, next_column))
            component_index += 1
    return assignment


def _validate_master_components(
    master: SharedBoundaryTopologyResult,
    labels: LabelField,
    assignment: list[int],
    component_by_index: dict[int, TopologyComponentEvidence],
    component_pixel_counts: Counter,
) -> None:
    expected_indices = set(assignment)
    if set(component_by_index) != expected_indices or len(component_by_index) != len(
        master.components
    ):
        raise MuralTopologyPartitionError(
            "master component evidence does not match authoritative label connectivity"
        )
    island_by_id = {item.id: item for item in master.document.islands}
    contour_by_id = {item.id: item for item in master.document.contours}
    source_label_by_id = {item.id: item for item in master.document.source_labels}
    if len(island_by_id) != len(master.document.islands):
        raise MuralTopologyPartitionError("master topology contains duplicate islands")
    label_counts: Counter[tuple[int, int]] = Counter(
        (component_index, labels.pixels[pixel_index])
        for pixel_index, component_index in enumerate(assignment)
    )
    pixel_area = (
        master.document.canvas_width_mm
        * master.document.canvas_height_mm
        / (labels.width * labels.height)
    )
    seen_islands: set[str] = set()
    for index, component in component_by_index.items():
        labels_for_component = {
            label
            for (component_index, label), count in label_counts.items()
            if component_index == index and count
        }
        if labels_for_component != {component.label_index}:
            raise MuralTopologyPartitionError(
                "master component label ownership is stale or swapped"
            )
        if component.pixel_count != component_pixel_counts[index]:
            raise MuralTopologyPartitionError("master component pixel evidence is stale")
        if not math.isclose(
            component.area_mm2,
            component.pixel_count * pixel_area,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise MuralTopologyPartitionError("master component area evidence is stale")
        island = island_by_id.get(component.island_id)
        if island is None or island.id in seen_islands:
            raise MuralTopologyPartitionError(
                "master component island lineage is missing or reused"
            )
        seen_islands.add(island.id)
        source_label = source_label_by_id.get(island.source_label_id)
        if source_label is None or source_label.label_index != component.label_index:
            raise MuralTopologyPartitionError(
                "master component island/source-label lineage is stale or swapped"
            )
        exterior = contour_by_id.get(island.exterior_contour_id)
        if exterior is None or exterior.path_id != component.exterior_path_id:
            raise MuralTopologyPartitionError("master component exterior lineage is stale")
        try:
            hole_path_ids = tuple(
                sorted(contour_by_id[item].path_id for item in island.hole_contour_ids)
            )
        except KeyError as error:
            raise MuralTopologyPartitionError(
                "master component hole lineage references a missing contour"
            ) from error
        if hole_path_ids != component.hole_path_ids:
            raise MuralTopologyPartitionError("master component hole lineage is stale")
    if seen_islands != set(island_by_id):
        raise MuralTopologyPartitionError(
            "master component evidence does not account for every topology island"
        )


def _clip_window_path(width_mm: float, height_mm: float) -> Path2D:
    origin = Point2(x_mm=0, y_mm=0)
    return Path2D.create(
        purpose="construction",
        start=origin,
        segments=(
            LineSegment(end=Point2(x_mm=width_mm, y_mm=0)),
            LineSegment(end=Point2(x_mm=width_mm, y_mm=height_mm)),
            LineSegment(end=Point2(x_mm=0, y_mm=height_mm)),
            LineSegment(end=origin),
        ),
        closed=True,
    )


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
    return json.loads(
        json.dumps(
            value,
            default=lambda item: (
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            ),
        )
    )
