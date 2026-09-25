import hashlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from image23mf.contracts.job import CropConfig
from image23mf.engine.labels import LabelField
from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize
from image23mf.geometry.model import (
    LineSegment,
    Material,
    Path2D,
    Point2,
    RectangleBase,
    SourceLabel,
)
from image23mf.geometry.topology import build_shared_boundary_topology
from image23mf.mural.partition import partition_master_labels
from image23mf.mural.planner import (
    BedEnvelope,
    MuralLayout,
    MuralPlanRequest,
    MuralSourceProvenance,
    build_mural_plan,
)
from image23mf.mural.seam_qa import (
    MuralSeamQaError,
    QaStatus,
    RiskSeverity,
    TileRiskFinding,
    build_mural_seam_qa,
)
from image23mf.mural.topology_partition import partition_master_topology

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def request(width: int, height: int, rows: int, columns: int) -> MuralPlanRequest:
    size = PixelSize(width=width, height=height)
    transform = CanonicalTransform.from_crop_config(
        original_size=size,
        normalized_size=size,
        exif_orientation=1,
        crop=CropConfig(mode="stretch"),
        working_size=size,
        canvas_size=MillimetreSize(width=200, height=200),
    )
    return MuralPlanRequest(
        source=MuralSourceProvenance(
            source_asset_id="asset_wager",
            source_asset_sha256=HASH_A,
            processed_artifact_id="artifact_wager",
            processed_artifact_sha256=HASH_B,
            processed_size=size,
            canonical_transform=transform,
            config_sha256=HASH_C,
            engine_version="0.1.0",
            revision_id="revision_wager",
        ),
        layout=MuralLayout(
            rows=rows,
            columns=columns,
            panel_width_mm=180,
            panel_height_mm=140,
        ),
        bed=BedEnvelope(
            printer_id="bambu-p2s",
            plate_id="textured-pei",
            profile_catalog_fingerprint=HASH_D,
            width_mm=256,
            height_mm=256,
        ),
    )


def master(width: int, height: int) -> LabelField:
    return LabelField(
        width=width,
        height=height,
        label_values=(0, 1, 2, 3),
        pixels=bytes(
            (
                (x * 4 // width)
                + (y * 3 // height)
                + (1 if x == width // 2 and y > height // 3 else 0)
            )
            % 4
            for y in range(height)
            for x in range(width)
        ),
    )


def construction_rectangle(width_mm: float, height_mm: float) -> Path2D:
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


def authoritative_topology(labels: LabelField, plan_request: MuralPlanRequest):
    plan = build_mural_plan(plan_request)
    colors = ("#111111", "#F99963", "#0078BF", "#CBC6B8")
    materials = tuple(
        sorted(
            (
                Material.create(
                    name=f"Color {index}",
                    color_hex=colors[index],
                    palette_color_id=f"color-{index}",
                    filament_id=f"filament-{index}",
                )
                for index in sorted(set(labels.pixels))
            ),
            key=lambda item: item.id,
        )
    )
    material_by_palette = {item.palette_color_id: item for item in materials}
    labels_sha256 = hashlib.sha256(labels.pixels).hexdigest()
    source_labels = tuple(
        sorted(
            (
                SourceLabel.create(
                    source_asset_id=plan_request.source.source_asset_id,
                    processed_labels_sha256=labels_sha256,
                    label_index=index,
                    name=f"Color {index}",
                    color_hex=colors[index],
                    palette_color_id=f"color-{index}",
                    material_id=material_by_palette[f"color-{index}"].id,
                    classification="background" if index == 0 else "artwork",
                )
                for index in sorted(set(labels.pixels))
            ),
            key=lambda item: item.id,
        )
    )
    return build_shared_boundary_topology(
        labels,
        source_asset_sha256=plan_request.source.source_asset_sha256,
        source_labels=source_labels,
        materials=materials,
        base=RectangleBase.create(
            center=Point2(
                x_mm=plan.master_size_mm.width / 2,
                y_mm=plan.master_size_mm.height / 2,
            ),
            width_mm=plan.master_size_mm.width,
            height_mm=plan.master_size_mm.height,
        ),
        canvas_width_mm=plan.master_size_mm.width,
        canvas_height_mm=plan.master_size_mm.height,
        vector_paths=(
            construction_rectangle(plan.master_size_mm.width, plan.master_size_mm.height),
        ),
        vector_artifact_sha256=HASH_E,
        palette_color_order=tuple(f"color-{index}" for index in sorted(set(labels.pixels))),
    )


def report(
    *,
    width: int = 13,
    height: int = 9,
    rows: int = 2,
    columns: int = 3,
    findings: tuple[TileRiskFinding, ...] = (),
):
    plan_request = request(width, height, rows, columns)
    plan = build_mural_plan(plan_request)
    labels = master(width, height)
    authoritative = authoritative_topology(labels, plan_request)
    partition = partition_master_labels(
        labels,
        plan_request,
        plan,
        authoritative_vector_sha256=HASH_E,
        authoritative_topology_sha256=authoritative.topology_artifact_sha256,
    )
    topology = partition_master_topology(partition, authoritative)
    return (
        build_mural_seam_qa(partition, topology, plan, findings=findings),
        partition,
        topology,
        plan,
    )


def test_wager_report_proves_exact_edges_numbering_orientation_and_overlay_purity() -> None:
    qa, partition, topology, _plan = report()

    assert qa.status == QaStatus.PASS
    assert qa.expected_seam_count == qa.exact_shared_edge_count == 7
    assert qa.failed_shared_edge_count == 0
    assert qa.panel_size_mm == MillimetreSize(width=180, height=140)
    assert qa.master_size_mm == MillimetreSize(width=540, height=280)
    assert qa.assembled_size_mm == MillimetreSize(width=540, height=280)
    assert all(item.no_gap_no_overlap for item in qa.seams)
    assert [item.plate_number for item in qa.tiles] == [1, 2, 3, 4, 5, 6]
    assert [item.rotation_degrees for item in qa.tiles] == [0] * 6
    assert qa.tiles[0].shared_edge_count == 2
    assert qa.tiles[1].shared_edge_count == 3
    assert qa.artwork.visible_art_unchanged is True
    assert qa.artwork.authoritative_master_sha256 == (
        partition.manifest.master_labels.pixels_sha256
    )
    assert qa.artwork.overlay_storage == "metadata_only"
    assert qa.artwork.overlay_pixels_written == 0
    assert qa.artwork.tile_number_pixels_written == 0
    assert qa.artwork.orientation_marker_pixels_written == 0
    assert "write zero pixels" in qa.artwork.proof
    assert qa.topology.evidence_kind == "exact_master_topology_clip"
    assert qa.topology.label_partition_sha256 == partition.manifest.partition_sha256
    assert qa.topology.topology_partition_sha256 == topology.manifest.partition_sha256
    assert qa.topology.every_master_pixel_represented is True
    assert all(item.topology_source_mode == "master_topology_exact_label_clip" for item in qa.tiles)
    assert all(item.evidence_kind == "raster_partition_boundary" for item in qa.seams)
    assert all(item.guard_hashes_expected_to_match is False for item in qa.seams)


def test_per_tile_risk_summary_keeps_external_findings_scoped() -> None:
    qa, _partition, _topology, _plan = report(
        findings=(
            TileRiskFinding(
                tile_id="tile-r01-c02",
                code="tiny_feature",
                severity=RiskSeverity.WARNING,
                message="A small isolated feature approaches the nozzle threshold.",
            ),
            TileRiskFinding(
                tile_id="tile-r01-c02",
                code="manual_review",
                severity=RiskSeverity.INFO,
                message="Review the tile at full scale before packaging.",
            ),
        )
    )

    assert qa.status == QaStatus.WARNING
    by_id = {item.tile_id: item for item in qa.tiles}
    assert by_id["tile-r01-c02"].risk.warning_count == 1
    assert by_id["tile-r01-c02"].risk.info_count == 1
    assert by_id["tile-r01-c02"].risk.status == QaStatus.WARNING
    assert by_id["tile-r02-c03"].risk.finding_count == 0


def test_report_rejects_mismatched_plan_and_unknown_tile_risk() -> None:
    _qa, partition, topology, plan = report()
    changed_request = request(13, 9, 2, 3).model_copy(
        update={"layout": request(13, 9, 2, 3).layout.model_copy(update={"panel_width_mm": 179})}
    )
    with pytest.raises(MuralSeamQaError, match="different mural request"):
        build_mural_seam_qa(partition, topology, build_mural_plan(changed_request))
    with pytest.raises(MuralSeamQaError, match="unknown mural tile"):
        build_mural_seam_qa(
            partition,
            topology,
            plan,
            findings=(
                TileRiskFinding(
                    tile_id="tile-r09-c09",
                    code="unknown",
                    severity=RiskSeverity.ERROR,
                    message="Unknown tile.",
                ),
            ),
        )


@settings(max_examples=6, deadline=None)
@given(
    rows=st.integers(min_value=1, max_value=6),
    columns=st.integers(min_value=1, max_value=6),
    extra_width=st.integers(min_value=0, max_value=19),
    extra_height=st.integers(min_value=0, max_value=19),
)
def test_arbitrary_non_square_grids_account_for_every_exact_shared_edge(
    rows: int,
    columns: int,
    extra_width: int,
    extra_height: int,
) -> None:
    width = columns + extra_width
    height = rows + extra_height
    qa, _partition, _topology, _plan = report(
        width=width,
        height=height,
        rows=rows,
        columns=columns,
    )

    expected = rows * (columns - 1) + columns * (rows - 1)
    assert qa.expected_seam_count == expected
    assert qa.exact_shared_edge_count == expected
    assert qa.failed_shared_edge_count == 0
    assert qa.artwork.visible_art_unchanged is True
    assert qa.topology.every_master_pixel_represented is True
    assert len(qa.tiles) == rows * columns


def test_fractional_non_divisible_grid_keeps_side_specific_seam_truth() -> None:
    qa, partition, topology, plan = report(width=13, height=9, rows=2, columns=3)

    assert [tile.master_pixel_bounds.x_start for tile in plan.tiles[:3]] == [0, 4, 8]
    assert [tile.master_pixel_bounds.x_end for tile in plan.tiles[:3]] == [4, 8, 13]
    assert qa.expected_seam_count == qa.exact_shared_edge_count == 7
    assert qa.topology.recomposed_labels_sha256 == (partition.manifest.master_labels.pixels_sha256)
    assert qa.topology.topology_partition_sha256 == topology.manifest.partition_sha256
    by_id = {item.tile_id: item for item in qa.tiles}
    partition_by_id = {item.tile_id: item for item in partition.manifest.tiles}
    for tile in plan.tiles:
        assert by_id[tile.id].vector_clip_bounds_master_mm == (
            partition_by_id[tile.id].topology.master_bounds_mm
        )
    assert by_id["tile-r01-c01"].vector_clip_bounds_master_mm.y == 140
    assert by_id["tile-r02-c01"].vector_clip_bounds_master_mm.y == 0
    horizontal = [item for item in qa.seams if item.orientation.value == "horizontal"]
    assert len(horizontal) == 3
    assert all(item.guard_hashes_expected_to_match is False for item in horizontal)
    assert all(item.first_guard_role != item.second_guard_role for item in horizontal)
