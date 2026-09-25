from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import numpy as np
import pytest

from image23mf.engine.labels import LabelField
from image23mf.engine.regions import analyze_regions
from image23mf.geometry.base import (
    BaseBuildBounds,
    BaseMeshOptions,
    generate_structural_base,
)
from image23mf.geometry.extrusion import (
    FlushInlayStrategy,
    assemble_mesh_document,
    extrude_artwork_regions,
)
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
from image23mf.geometry.raster_diff import (
    GeometryDifferenceThresholds,
    GeometryRasterizationError,
    compare_geometry_to_labels,
    rasterize_geometry,
)


@dataclass(frozen=True)
class Shape:
    label: int
    exterior: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...] = ()


def available(digit: str) -> CapabilityState:
    return CapabilityState(status=CapabilityStatus.AVAILABLE, artifact_sha256=digit * 64)


def pending(reason: str) -> CapabilityState:
    return CapabilityState(status=CapabilityStatus.NOT_REQUESTED, reason=reason)


def topology_capabilities() -> GeometryCapabilities:
    return GeometryCapabilities(
        geometry_ir=available("1"),
        vector_geometry=available("2"),
        topology=available("3"),
        mesh=pending("Waiting for extrusion."),
        package_3mf=pending("Waiting for packaging."),
        slicer_validation=pending("Waiting for package validation."),
        download=pending("Waiting for validation."),
    )


def boundary(points: tuple[tuple[float, float], ...]) -> Path2D:
    start = Point2(x_mm=points[0][0], y_mm=points[0][1])
    return Path2D.create(
        purpose="boundary",
        start=start,
        segments=tuple(
            LineSegment(end=Point2(x_mm=x, y_mm=y)) for x, y in (*points[1:], points[0])
        ),
        closed=True,
    )


def label_field(width: int, height: int, rows: tuple[tuple[int, ...], ...]) -> LabelField:
    assert len(rows) == height
    assert all(len(row) == width for row in rows)
    pixels = bytes(value for row in rows for value in row)
    return LabelField(
        width=width,
        height=height,
        label_values=tuple(sorted(set(pixels))),
        pixels=pixels,
    )


def geometry_document(
    labels: LabelField,
    *,
    width_mm: float,
    height_mm: float,
    shapes: tuple[Shape, ...],
    shared: tuple[tuple[int, int, tuple[float, float], tuple[float, float]], ...] = (),
    material_colors: dict[int, str] | None = None,
) -> GeometryDocument:
    palette = {
        0: "#000000",
        1: "#0078BF",
        2: "#F99963",
        3: "#CBC6B8",
    }
    material_colors = material_colors or palette
    digest = hashlib.sha256(labels.pixels).hexdigest()
    materials_by_label = {
        value: Material.create(
            name=f"Material {value}",
            color_hex=material_colors[value],
            palette_color_id=f"palette-{value}",
        )
        for value in labels.label_values
    }
    source_labels_by_label = {
        value: SourceLabel.create(
            source_asset_id="source-geometry-raster-test",
            processed_labels_sha256=digest,
            label_index=value,
            name=f"Label {value}",
            color_hex=palette[value],
            palette_color_id=f"palette-{value}",
            material_id=materials_by_label[value].id,
            classification="background" if value == 0 else "artwork",
        )
        for value in labels.label_values
    }
    paths: list[Path2D] = []
    contours: list[Contour2D] = []
    islands: list[Island2D] = []
    for shape in shapes:
        exterior_path = boundary(shape.exterior)
        exterior = Contour2D.create(role="exterior", path_id=exterior_path.id)
        hole_paths = tuple(boundary(points) for points in shape.holes)
        holes = tuple(
            Contour2D.create(
                role="hole",
                path_id=path.id,
                parent_contour_id=exterior.id,
            )
            for path in hole_paths
        )
        source_label = source_labels_by_label[shape.label]
        island = Island2D.create(
            exterior_contour_id=exterior.id,
            hole_contour_ids=tuple(sorted(item.id for item in holes)),
            material_id=source_label.material_id,
            source_label_id=source_label.id,
        )
        paths.extend((exterior_path, *hole_paths))
        contours.extend((exterior, *holes))
        islands.append(island)
    shared_edges = []
    for left, right, start, end in shared:
        adjacent = tuple(sorted((islands[left].id, islands[right].id)))
        ordered = sorted((start, end))
        shared_edges.append(
            SharedEdge.create(
                start=Point2(x_mm=ordered[0][0], y_mm=ordered[0][1]),
                end=Point2(x_mm=ordered[1][0], y_mm=ordered[1][1]),
                adjacent_island_ids=adjacent,
                owner_island_id=adjacent[0],
            )
        )
    return GeometryDocument(
        schema_version=1,
        source_asset_sha256="b" * 64,
        processed_labels_sha256=digest,
        source_width_px=labels.width,
        source_height_px=labels.height,
        canvas_width_mm=width_mm,
        canvas_height_mm=height_mm,
        paths=tuple(sorted(paths, key=lambda item: item.id)),
        contours=tuple(sorted(contours, key=lambda item: item.id)),
        materials=tuple(sorted(materials_by_label.values(), key=lambda item: item.id)),
        source_labels=tuple(sorted(source_labels_by_label.values(), key=lambda item: item.id)),
        islands=tuple(sorted(islands, key=lambda item: item.id)),
        shared_edges=tuple(sorted(shared_edges, key=lambda item: item.id)),
        base=RectangleBase.create(
            center=Point2(x_mm=width_mm / 2, y_mm=height_mm / 2),
            width_mm=width_mm,
            height_mm=height_mm,
        ),
        capabilities=topology_capabilities(),
    )


def horizontal_document(
    labels: LabelField,
    *,
    boundary_y: float,
    material_colors: dict[int, str] | None = None,
) -> GeometryDocument:
    return geometry_document(
        labels,
        width_mm=float(labels.width),
        height_mm=float(labels.height),
        shapes=(
            Shape(
                1,
                (
                    (0, boundary_y),
                    (labels.width, boundary_y),
                    (labels.width, labels.height),
                    (0, labels.height),
                ),
            ),
            Shape(
                2,
                ((0, 0), (labels.width, 0), (labels.width, boundary_y), (0, boundary_y)),
            ),
        ),
        shared=((0, 1, (0, boundary_y), (labels.width, boundary_y)),),
        material_colors=material_colors,
    )


def test_vector_raster_exactly_inverts_y_for_non_square_source_and_is_deterministic() -> None:
    labels = label_field(4, 2, ((1, 1, 1, 1), (2, 2, 2, 2)))
    document = horizontal_document(labels, boundary_y=1)

    first = compare_geometry_to_labels(document, labels, source="vector")
    second = compare_geometry_to_labels(document, labels, source="vector")

    assert first.raster.assignment.tolist() == [[1, 1, 1, 1], [2, 2, 2, 2]]
    assert first.report.summary.status == "match"
    assert first.report.summary.mismatch_pixel_count == 0
    assert first.report.source_image_origin == "upper_left"
    assert first.report.geometry_origin == "lower_left_build_plate"
    assert first.report.source_to_geometry_y == "invert_about_canvas_height"
    assert first.report.geometry_to_source_pixel_edges.model_dump() == {
        "x_scale_px_per_mm": 1.0,
        "y_scale_px_per_mm": -1.0,
        "translate_x_px": 0.0,
        "translate_y_px": 2.0,
    }
    assert first.report.canonical_json() == second.report.canonical_json()
    assert first.report.fingerprint() == second.report.fingerprint()
    assert first.raster.encoded_assignment() == second.raster.encoded_assignment()


def test_mesh_top_faces_rasterize_to_same_authoritative_labels_as_vector_islands() -> None:
    labels = label_field(4, 2, ((1, 1, 1, 1), (2, 2, 2, 2)))
    topology = horizontal_document(labels, boundary_y=1)
    material = topology.materials[0]
    base = generate_structural_base(
        topology.base,
        material=material,
        options=BaseMeshOptions(thickness_mm=0.4, layer_height_mm=0.2),
        build_bounds=BaseBuildBounds(width_mm=10, depth_mm=10),
    )
    artwork = extrude_artwork_regions(
        topology,
        FlushInlayStrategy(base_top_z_mm=0.4, layer_height_mm=0.2, artwork_layers=2),
    )
    meshed = assemble_mesh_document(
        topology,
        base_mesh=base.mesh,
        base_part=base.part,
        artwork=artwork,
    ).document

    vector = rasterize_geometry(meshed, source="vector")
    mesh = compare_geometry_to_labels(meshed, labels, source="mesh")

    assert np.array_equal(mesh.raster.assignment, vector.assignment)
    assert mesh.report.summary.status == "match"
    assert mesh.report.summary.mismatch_pixel_count == 0


def test_hole_and_enclosed_island_preserve_pixel_ownership() -> None:
    labels = label_field(
        5,
        5,
        (
            (1, 1, 1, 1, 1),
            (1, 2, 2, 2, 1),
            (1, 2, 2, 2, 1),
            (1, 2, 2, 2, 1),
            (1, 1, 1, 1, 1),
        ),
    )
    document = geometry_document(
        labels,
        width_mm=5,
        height_mm=5,
        shapes=(
            Shape(
                1,
                ((0, 0), (5, 0), (5, 5), (0, 5)),
                holes=(((1, 1), (1, 4), (4, 4), (4, 1)),),
            ),
            Shape(2, ((1, 1), (4, 1), (4, 4), (1, 4))),
        ),
        shared=(
            (0, 1, (1, 1), (1, 4)),
            (0, 1, (1, 1), (4, 1)),
            (0, 1, (1, 4), (4, 4)),
            (0, 1, (4, 1), (4, 4)),
        ),
    )

    result = compare_geometry_to_labels(document, labels, source="vector")

    assert result.report.summary.status == "match"
    assert result.raster.assignment.tolist() == [
        list(row)
        for row in (
            (1, 1, 1, 1, 1),
            (1, 2, 2, 2, 1),
            (1, 2, 2, 2, 1),
            (1, 2, 2, 2, 1),
            (1, 1, 1, 1, 1),
        )
    ]


def test_four_color_junction_has_no_gap_or_overlap() -> None:
    labels = label_field(
        4,
        4,
        (
            (0, 0, 1, 1),
            (0, 0, 1, 1),
            (2, 2, 3, 3),
            (2, 2, 3, 3),
        ),
    )
    document = geometry_document(
        labels,
        width_mm=4,
        height_mm=4,
        shapes=(
            Shape(0, ((0, 2), (2, 2), (2, 4), (0, 4))),
            Shape(1, ((2, 2), (4, 2), (4, 4), (2, 4))),
            Shape(2, ((0, 0), (2, 0), (2, 2), (0, 2))),
            Shape(3, ((2, 0), (4, 0), (4, 2), (2, 2))),
        ),
        shared=(
            (0, 1, (2, 2), (2, 4)),
            (0, 2, (0, 2), (2, 2)),
            (1, 3, (2, 2), (4, 2)),
            (2, 3, (2, 0), (2, 2)),
        ),
    )

    result = compare_geometry_to_labels(document, labels, source="vector")

    assert result.report.summary.status == "match"
    assert result.report.summary.unassigned_pixel_count == 0
    assert result.report.summary.overlap_pixel_count == 0


def test_mismatch_thresholds_boundary_error_and_source_region_links_are_explicit() -> None:
    labels = label_field(
        4,
        4,
        (
            (1, 1, 1, 1),
            (1, 1, 1, 1),
            (2, 2, 2, 2),
            (2, 2, 2, 2),
        ),
    )
    document = horizontal_document(labels, boundary_y=3)
    regions = analyze_regions(
        labels,
        colors={1: "#0078BF", 2: "#F99963"},
        width_mm=4,
        height_mm=4,
    )
    thresholds = GeometryDifferenceThresholds(
        warning_mismatch_ratio=0.1,
        error_mismatch_ratio=0.4,
        warning_area_delta_ratio=0.1,
        error_area_delta_ratio=0.4,
        warning_boundary_error_px=0.25,
        error_boundary_error_px=1.5,
    )

    result = compare_geometry_to_labels(
        document,
        labels,
        source="vector",
        thresholds=thresholds,
        regions=regions,
    )

    assert result.report.summary.status == "error"
    assert result.report.summary.mismatch_pixel_count == 4
    assert result.report.summary.maximum_boundary_error_px == 1
    assert result.report.region_graph_fingerprint == regions.graph.fingerprint()
    assert len(result.report.affected_regions) == 1
    affected = result.report.affected_regions[0]
    assert affected.region_id in {item.id for item in regions.graph.regions}
    assert affected.source_label_id in {item.id for item in document.source_labels}
    assert affected.mismatch_bounds.model_dump() == {"x": 0, "y": 1, "width": 4, "height": 1}
    assert set(result.difference_mask) == {0, 1}


def test_material_color_change_is_visible_even_when_geometry_is_pixel_exact() -> None:
    labels = label_field(4, 2, ((1, 1, 1, 1), (2, 2, 2, 2)))
    document = horizontal_document(
        labels,
        boundary_y=1,
        material_colors={1: "#CBC6B8", 2: "#F99963"},
    )

    result = compare_geometry_to_labels(document, labels, source="vector")

    changed = next(item for item in result.report.labels if item.label_index == 1)
    assert changed.color_matches is False
    assert changed.expected_color_hex == "#0078BF"
    assert changed.actual_color_hex == "#CBC6B8"
    assert changed.status == "error"
    assert result.report.summary.material_color_change_pixel_count == 4
    assert result.report.summary.status == "error"
    assert set(result.difference_mask) == {0, 4}


def test_comparison_rejects_wrong_authoritative_artifact_and_is_bounded() -> None:
    labels = label_field(4, 2, ((1, 1, 1, 1), (2, 2, 2, 2)))
    document = horizontal_document(labels, boundary_y=1)
    changed = LabelField(
        width=4,
        height=2,
        label_values=(1, 2),
        pixels=bytes((2, 2, 2, 2, 1, 1, 1, 1)),
    )

    with pytest.raises(GeometryRasterizationError, match="authoritative fingerprint"):
        compare_geometry_to_labels(document, changed, source="vector")


def test_source_resolution_audit_has_bounded_runtime_for_large_non_square_artwork() -> None:
    width = 512
    height = 256
    labels = label_field(
        width,
        height,
        tuple(tuple([1 if y < height // 2 else 2] * width) for y in range(height)),
    )
    document = horizontal_document(labels, boundary_y=height / 2)

    started = time.perf_counter()
    result = compare_geometry_to_labels(document, labels, source="vector")
    elapsed = time.perf_counter() - started

    assert result.report.summary.status == "match"
    assert elapsed < 2.0
