from __future__ import annotations

import hashlib

import pytest

from image23mf.engine.labels import LabelField
from image23mf.geometry.extrusion import FlushInlayStrategy, extrude_artwork_regions
from image23mf.geometry.model import (
    CapabilityStatus,
    LineSegment,
    Material,
    Path2D,
    Point2,
    RectangleBase,
    SourceLabel,
)
from image23mf.geometry.topology import TopologyBuildError, build_shared_boundary_topology

SOURCE_SHA = "a" * 64
VECTOR_SHA = "b" * 64


def field(rows: list[list[int]], *, declared: tuple[int, ...] | None = None) -> LabelField:
    pixels = bytes(value for row in rows for value in row)
    return LabelField(
        width=len(rows[0]),
        height=len(rows),
        label_values=declared or tuple(sorted(set(pixels))),
        pixels=pixels,
    )


def palette(labels: LabelField, indices: tuple[int, ...] | None = None):
    colors = ("#0078BF", "#F99963", "#000000", "#CBC6B8")
    materials = tuple(
        Material.create(
            name=f"Material {index}",
            color_hex=colors[position],
            palette_color_id=f"color-{index}",
        )
        for position, index in enumerate(indices or tuple(sorted(set(labels.pixels))))
    )
    materials = tuple(sorted(materials, key=lambda item: item.id))
    by_palette = {item.palette_color_id: item for item in materials}
    digest = hashlib.sha256(labels.pixels).hexdigest()
    source_labels = tuple(
        SourceLabel.create(
            source_asset_id="source-1",
            processed_labels_sha256=digest,
            label_index=index,
            name=f"Label {index}",
            color_hex=colors[position],
            palette_color_id=f"color-{index}",
            material_id=by_palette[f"color-{index}"].id,
            classification="background" if position == 0 else "artwork",
        )
        for position, index in enumerate(indices or tuple(sorted(set(labels.pixels))))
    )
    return materials, tuple(sorted(source_labels, key=lambda item: item.id))


def construction_path(*, bow_tie: bool = False) -> Path2D:
    points = (
        ((0, 0), (4, 4), (0, 4), (4, 0), (0, 0))
        if bow_tie
        else ((0, 0), (4, 0), (4, 4), (0, 4), (0, 0))
    )
    return Path2D.create(
        purpose="construction",
        start=Point2(x_mm=points[0][0], y_mm=points[0][1]),
        segments=tuple(LineSegment(end=Point2(x_mm=x, y_mm=y)) for x, y in points[1:]),
        closed=True,
    )


def build(labels: LabelField, *, bow_tie: bool = False):
    materials, source_labels = palette(labels)
    path = construction_path(bow_tie=bow_tie)
    return build_shared_boundary_topology(
        labels,
        source_asset_sha256=SOURCE_SHA,
        source_labels=source_labels,
        materials=materials,
        base=RectangleBase.create(center=Point2(x_mm=2, y_mm=2), width_mm=4, height_mm=4),
        canvas_width_mm=4,
        canvas_height_mm=4,
        vector_paths=(path,),
        vector_artifact_sha256=VECTOR_SHA,
    )


def test_uniform_field_covers_canvas_and_retains_potrace_construction_evidence():
    labels = field([[0, 0], [0, 0]])

    result = build(labels)

    assert len(result.document.islands) == 1
    assert len(result.document.contours) == 1
    assert result.document.shared_edges == ()
    assert result.coverage.source_pixel_count == result.coverage.represented_pixel_count == 4
    assert result.coverage.source_area_mm2 == result.coverage.represented_area_mm2 == 16
    assert result.coverage.gap_area_mm2 == result.coverage.overlap_area_mm2 == 0
    assert result.document.capabilities.topology.status == CapabilityStatus.AVAILABLE
    assert result.document.capabilities.topology.artifact_sha256 == result.topology_artifact_sha256
    construction = [path for path in result.document.paths if path.purpose == "construction"]
    assert tuple(item.id for item in construction) == result.source_vector_path_ids


def test_checkerboard_keeps_diagonal_regions_distinct_and_owns_four_atomic_seams():
    labels = field([[0, 1], [1, 0]])

    first = build(labels)
    second = build(labels)

    assert first == second
    assert first.fingerprint() == second.fingerprint()
    assert len(first.document.islands) == 4
    assert len(first.components) == 4
    assert {item.pixel_count for item in first.components} == {1}
    assert len(first.document.shared_edges) == 4
    assert all(
        edge.owner_island_id == min(edge.adjacent_island_ids)
        for edge in first.document.shared_edges
    )
    endpoints = {
        (point.x_mm, point.y_mm)
        for edge in first.document.shared_edges
        for point in (edge.start, edge.end)
    }
    assert (2, 2) in endpoints


def test_many_components_do_not_accumulate_false_coverage_drift():
    labels = field([[(row + column) % 2 for column in range(21)] for row in range(21)])

    result = build(labels)

    assert result.coverage.source_pixel_count == result.coverage.represented_pixel_count == 441
    assert result.coverage.source_area_mm2 == result.coverage.represented_area_mm2 == 16


def test_nested_ring_creates_parented_holes_without_overlap_or_missing_center():
    labels = field(
        [
            [0, 0, 0, 0, 0],
            [0, 1, 1, 1, 0],
            [0, 1, 0, 1, 0],
            [0, 1, 1, 1, 0],
            [0, 0, 0, 0, 0],
        ]
    )

    result = build(labels)

    assert len(result.document.islands) == 3
    assert sorted(len(item.hole_contour_ids) for item in result.document.islands) == [0, 1, 1]
    assert sum(item.pixel_count for item in result.components) == 25
    assert result.coverage.represented_area_mm2 == 16
    contours = {item.id: item for item in result.document.contours}
    for island in result.document.islands:
        for hole_id in island.hole_contour_ids:
            assert contours[hole_id].parent_contour_id == island.exterior_contour_id


def test_corner_touching_holes_remain_separate_simple_loops():
    labels = field(
        [
            [1, 1, 1, 1],
            [1, 0, 1, 1],
            [1, 1, 0, 1],
            [1, 1, 1, 1],
        ]
    )

    result = build(labels)

    background = next(item for item in result.components if item.label_index == 1)
    island = next(item for item in result.document.islands if item.id == background.island_id)
    assert len(island.hole_contour_ids) == 2
    assert len(result.document.islands) == 3
    assert sum(item.pixel_count for item in result.components) == 16
    assert result.coverage.represented_area_mm2 == 16
    assert result.coverage.gap_area_mm2 == result.coverage.overlap_area_mm2 == 0

    extrusion = extrude_artwork_regions(
        result.document,
        FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2),
    )
    assert len(extrusion.meshes) == 3
    assert all(item.watertight for item in extrusion.meshes)


def test_hole_touching_exterior_at_pixel_corner_gets_printable_relief():
    labels = field(
        [
            [0, 1, 1],
            [1, 0, 1],
            [1, 1, 1],
        ]
    )

    result = build(labels)
    background = next(item for item in result.components if item.label_index == 1)
    island = next(item for item in result.document.islands if item.id == background.island_id)
    assert len(island.hole_contour_ids) == 1

    extrusion = extrude_artwork_regions(
        result.document,
        FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2),
    )
    assert len(extrusion.meshes) == 3
    assert all(item.watertight for item in extrusion.meshes)


def test_t_junctions_share_exact_endpoints_and_full_edge_contacts_reach_canvas_bounds():
    labels = field(
        [
            [0, 0, 1, 1],
            [0, 0, 1, 1],
            [0, 2, 2, 1],
            [0, 2, 2, 1],
        ]
    )

    result = build(labels)

    endpoints = {
        (point.x_mm, point.y_mm)
        for edge in result.document.shared_edges
        for point in (edge.start, edge.end)
    }
    assert (2, 2) in endpoints
    boundary_points = {
        (point.x_mm, point.y_mm)
        for path in result.document.paths
        if path.purpose == "boundary"
        for point in (path.start, *(segment.end for segment in path.segments))
    }
    assert {(0, 0), (0, 4), (4, 0), (4, 4)} <= boundary_points


def test_source_y_is_inverted_once_when_top_row_becomes_upper_physical_region():
    labels = field([[1, 1], [0, 0]])

    result = build(labels)

    labels_by_id = {item.id: item for item in result.document.source_labels}
    paths = {item.id: item for item in result.document.paths}
    contours = {item.id: item for item in result.document.contours}
    upper = next(
        island
        for island in result.document.islands
        if labels_by_id[island.source_label_id].label_index == 1
    )
    upper_path = paths[contours[upper.exterior_contour_id].path_id]
    assert (
        min(
            point.y_mm
            for point in (upper_path.start, *(segment.end for segment in upper_path.segments))
        )
        == 2
    )


def test_topology_document_flows_directly_into_layer_aware_extrusion():
    topology = build(field([[0, 1], [0, 1]]))

    extrusion = extrude_artwork_regions(
        topology.document,
        FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2),
    )

    assert len(extrusion.meshes) == len(topology.document.islands) == 2
    assert {item.bottom_z_mm for item in extrusion.part_evidence} == {1.2}
    assert {item.top_z_mm for item in extrusion.part_evidence} == {1.6}
    assert len(extrusion.shared_interfaces) == 1


def test_self_intersecting_construction_trace_cannot_pollute_simple_owned_boundaries():
    labels = field([[0, 0], [0, 0]])

    result = build(labels, bow_tie=True)

    construction = next(path for path in result.document.paths if path.purpose == "construction")
    boundary = next(path for path in result.document.paths if path.purpose == "boundary")
    assert construction.id in result.source_vector_path_ids
    assert boundary.signed_area_mm2 == 16


def test_mismatched_label_hash_unknown_present_label_and_unsorted_evidence_fail_closed():
    labels = field([[0, 1], [0, 1]])
    materials, source_labels = palette(labels)
    wrong = source_labels[0].model_copy(update={"processed_labels_sha256": "f" * 64})
    path = construction_path()
    common = dict(
        source_asset_sha256=SOURCE_SHA,
        materials=materials,
        base=RectangleBase.create(center=Point2(x_mm=2, y_mm=2), width_mm=4, height_mm=4),
        canvas_width_mm=4,
        canvas_height_mm=4,
        vector_paths=(path,),
        vector_artifact_sha256=VECTOR_SHA,
    )
    with pytest.raises(TopologyBuildError, match="authoritative"):
        build_shared_boundary_topology(
            labels,
            source_labels=tuple(sorted((wrong, source_labels[1]), key=lambda item: item.id)),
            **common,
        )
    with pytest.raises(TopologyBuildError, match="exactly represent"):
        build_shared_boundary_topology(
            labels,
            source_labels=(source_labels[0],),
            **common,
        )
    with pytest.raises(TopologyBuildError, match="ID-sorted"):
        build_shared_boundary_topology(
            labels,
            source_labels=source_labels,
            **{**common, "vector_paths": (construction_path(), path)},
        )


def test_declared_but_absent_palette_value_needs_no_fake_label_or_island():
    labels = field([[0, 0], [0, 0]], declared=(0, 1))
    result = build(labels)

    assert len(result.document.source_labels) == len(result.document.islands) == 1


def test_physical_pitch_below_ir_precision_is_rejected_before_vertices_collapse():
    labels = field([[0, 0], [0, 0]])
    materials, source_labels = palette(labels)
    with pytest.raises(TopologyBuildError, match="pixel pitch"):
        build_shared_boundary_topology(
            labels,
            source_asset_sha256=SOURCE_SHA,
            source_labels=source_labels,
            materials=materials,
            base=RectangleBase.create(
                center=Point2(x_mm=0.000001, y_mm=2),
                width_mm=0.000002,
                height_mm=4,
            ),
            canvas_width_mm=0.000001,
            canvas_height_mm=4,
            vector_paths=(construction_path(),),
            vector_artifact_sha256=VECTOR_SHA,
        )
