from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from image23mf.geometry.extrusion import (
    ArtworkExtrusionError,
    FlushInlayStrategy,
    ShallowRaisedStrategy,
    _simplify_ring_for_mesh,
    _triangulate_polygon,
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
    Mesh,
    Part,
    Path2D,
    Point2,
    Point3,
    RectangleBase,
    SharedEdge,
    SourceLabel,
    Triangle,
)

LABEL_SHA = "a" * 64


def test_mesh_ring_simplification_collapses_sub_tolerance_spike() -> None:
    ring = [
        Point2(x_mm=3.164062, y_mm=57.421875),
        Point2(x_mm=1.054688, y_mm=58.242188),
        Point2(x_mm=1.054688, y_mm=58.652344),
        Point2(x_mm=0, y_mm=58.652344),
    ]

    simplified = _simplify_ring_for_mesh(ring, protected_points=set())

    assert simplified == ring[1:]
    assert len(_triangulate_polygon(simplified, [])) == 1


def test_mesh_ring_simplification_preserves_shared_endpoint() -> None:
    ring = [
        Point2(x_mm=3.164062, y_mm=57.421875),
        Point2(x_mm=1.054688, y_mm=58.242188),
        Point2(x_mm=1.054688, y_mm=58.652344),
        Point2(x_mm=0, y_mm=58.652344),
    ]

    simplified = _simplify_ring_for_mesh(
        ring,
        protected_points={(ring[0].x_mm, ring[0].y_mm)},
    )

    assert ring[0] in simplified


def test_triangulation_retries_with_well_conditioned_ears_for_stair_step_region() -> None:
    coordinates = (
        (0, 42.246094),
        (1.757812, 42.246094),
        (1.757812, 42.65625),
        (2.8125, 42.65625),
        (2.8125, 43.066406),
        (3.515625, 43.066406),
        (3.515625, 43.476562),
        (3.867188, 43.476562),
        (3.867188, 43.886719),
        (4.570312, 43.886719),
        (4.570312, 44.296875),
        (4.921875, 44.296875),
        (4.921875, 44.707031),
        (5.273438, 44.707031),
        (5.273438, 45.117188),
        (5.625, 45.117188),
        (5.625, 45.527344),
        (5.976562, 45.527344),
        (5.976562, 46.347656),
        (6.328125, 46.347656),
        (6.328125, 47.167969),
        (6.679688, 47.167969),
        (6.679688, 48.398438),
        (7.03125, 48.398438),
        (7.03125, 52.089844),
        (6.679688, 52.089844),
        (6.679688, 53.320312),
        (6.328125, 53.320312),
        (6.328125, 54.550781),
        (5.976562, 54.550781),
        (5.976562, 54.960938),
        (5.625, 54.960938),
        (5.625, 55.371094),
        (5.273438, 55.371094),
        (5.273438, 56.191406),
        (4.921875, 56.191406),
        (4.921875, 56.601562),
        (4.21875, 56.601562),
        (4.21875, 57.011719),
        (3.867188, 57.011719),
        (3.867188, 57.421875),
        (3.164062, 57.421875),
        (3.164062, 57.832031),
        (2.460938, 57.832031),
        (2.460938, 58.242188),
        (1.054688, 58.242188),
        (1.054688, 58.652344),
        (0, 58.652344),
    )
    ring = [Point2(x_mm=x, y_mm=y) for x, y in coordinates]

    triangles = _triangulate_polygon(ring, [])

    assert len(triangles) == len(ring) - 2


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
        package_3mf=pending("Waiting for meshes."),
        slicer_validation=pending("Waiting for a package."),
        download=pending("Waiting for validation."),
    )


def boundary(points: list[tuple[float, float]]) -> Path2D:
    start = Point2(x_mm=points[0][0], y_mm=points[0][1])
    return Path2D.create(
        purpose="boundary",
        start=start,
        segments=tuple(
            LineSegment(end=Point2(x_mm=x, y_mm=y)) for x, y in (*points[1:], points[0])
        ),
        closed=True,
    )


def material(name: str, color: str, palette: str) -> Material:
    return Material.create(name=name, color_hex=color, palette_color_id=palette)


def label(
    name: str,
    color: str,
    palette: str,
    assigned_material: Material,
    index: int,
    classification: str,
) -> SourceLabel:
    return SourceLabel.create(
        source_asset_id="source-1",
        processed_labels_sha256=LABEL_SHA,
        label_index=index,
        name=name,
        color_hex=color,
        palette_color_id=palette,
        material_id=assigned_material.id,
        classification=classification,
    )


def two_region_document(*, subdivided: bool = False) -> GeometryDocument:
    blue = material("Marine Blue", "#0078BF", "blue")
    orange = material("Mandarin Orange", "#F99963", "orange")
    absent = material("Bone White", "#CBC6B8", "bone")
    blue_label = label("Ocean", "#0078BF", "blue", blue, 1, "background")
    orange_label = label("Sun", "#F99963", "orange", orange, 2, "artwork")
    left_path = boundary([(0, 0), (4, 0), (4, 4), (0, 4)])
    right_points = (
        [(4, 0), (8, 0), (8, 4), (4, 4), (4, 2)] if subdivided else [(4, 0), (8, 0), (8, 4), (4, 4)]
    )
    right_path = boundary(right_points)
    left_contour = Contour2D.create(role="exterior", path_id=left_path.id)
    right_contour = Contour2D.create(role="exterior", path_id=right_path.id)
    left_island = Island2D.create(
        exterior_contour_id=left_contour.id,
        material_id=blue.id,
        source_label_id=blue_label.id,
    )
    right_island = Island2D.create(
        exterior_contour_id=right_contour.id,
        material_id=orange.id,
        source_label_id=orange_label.id,
    )
    adjacent = tuple(sorted((left_island.id, right_island.id)))
    spans = [((4, 0), (4, 2)), ((4, 2), (4, 4))] if subdivided else [((4, 0), (4, 4))]
    shared = tuple(
        SharedEdge.create(
            start=Point2(x_mm=start[0], y_mm=start[1]),
            end=Point2(x_mm=end[0], y_mm=end[1]),
            adjacent_island_ids=adjacent,
            owner_island_id=adjacent[0],
        )
        for start, end in spans
    )
    return GeometryDocument(
        schema_version=1,
        source_asset_sha256="b" * 64,
        processed_labels_sha256=LABEL_SHA,
        source_width_px=800,
        source_height_px=400,
        canvas_width_mm=8,
        canvas_height_mm=4,
        paths=tuple(sorted((left_path, right_path), key=lambda item: item.id)),
        contours=tuple(sorted((left_contour, right_contour), key=lambda item: item.id)),
        materials=tuple(sorted((blue, orange, absent), key=lambda item: item.id)),
        source_labels=tuple(sorted((blue_label, orange_label), key=lambda item: item.id)),
        islands=tuple(sorted((left_island, right_island), key=lambda item: item.id)),
        shared_edges=tuple(sorted(shared, key=lambda item: item.id)),
        base=RectangleBase.create(
            center=Point2(x_mm=4, y_mm=2),
            width_mm=8,
            height_mm=4,
        ),
        capabilities=topology_capabilities(),
    )


def holed_concave_document() -> GeometryDocument:
    charcoal = material("Charcoal", "#000000", "charcoal")
    charcoal_label = label("Frame", "#000000", "charcoal", charcoal, 1, "artwork")
    outer = boundary([(0, 0), (8, 0), (8, 3), (4, 3), (4, 8), (0, 8)])
    hole = boundary([(1, 4), (1, 5), (2, 5), (2, 4)])
    exterior_contour = Contour2D.create(role="exterior", path_id=outer.id)
    hole_contour = Contour2D.create(
        role="hole",
        path_id=hole.id,
        parent_contour_id=exterior_contour.id,
    )
    island = Island2D.create(
        exterior_contour_id=exterior_contour.id,
        hole_contour_ids=(hole_contour.id,),
        material_id=charcoal.id,
        source_label_id=charcoal_label.id,
    )
    return GeometryDocument(
        schema_version=1,
        source_asset_sha256="b" * 64,
        processed_labels_sha256=LABEL_SHA,
        source_width_px=800,
        source_height_px=800,
        canvas_width_mm=8,
        canvas_height_mm=8,
        paths=tuple(sorted((outer, hole), key=lambda item: item.id)),
        contours=tuple(sorted((exterior_contour, hole_contour), key=lambda item: item.id)),
        materials=(charcoal,),
        source_labels=(charcoal_label,),
        islands=(island,),
        base=RectangleBase.create(center=Point2(x_mm=4, y_mm=4), width_mm=8, height_mm=8),
        capabilities=topology_capabilities(),
    )


def multiple_hole_document(
    *,
    width: float = 12,
    height: float = 8,
    hole_boxes: tuple[tuple[float, float, float, float], ...] = (
        (2, 2, 4, 4),
        (7, 3, 10, 6),
    ),
) -> GeometryDocument:
    ink = material("Ink", "#000000", "ink")
    ink_label = label("Apertures", "#000000", "ink", ink, 1, "artwork")
    outer = boundary([(0, 0), (width, 0), (width, height), (0, height)])
    holes = tuple(
        boundary([(x0, y0), (x0, y1), (x1, y1), (x1, y0)]) for x0, y0, x1, y1 in hole_boxes
    )
    exterior = Contour2D.create(role="exterior", path_id=outer.id)
    hole_contours = tuple(
        Contour2D.create(
            role="hole",
            path_id=path.id,
            parent_contour_id=exterior.id,
        )
        for path in holes
    )
    island = Island2D.create(
        exterior_contour_id=exterior.id,
        hole_contour_ids=tuple(sorted(item.id for item in hole_contours)),
        material_id=ink.id,
        source_label_id=ink_label.id,
    )
    return GeometryDocument(
        schema_version=1,
        source_asset_sha256="b" * 64,
        processed_labels_sha256=LABEL_SHA,
        source_width_px=round(width * 100),
        source_height_px=round(height * 100),
        canvas_width_mm=width,
        canvas_height_mm=height,
        paths=tuple(sorted((outer, *holes), key=lambda item: item.id)),
        contours=tuple(sorted((exterior, *hole_contours), key=lambda item: item.id)),
        materials=(ink,),
        source_labels=(ink_label,),
        islands=(island,),
        base=RectangleBase.create(
            center=Point2(x_mm=width / 2, y_mm=height / 2),
            width_mm=width,
            height_mm=height,
        ),
        capabilities=topology_capabilities(),
    )


def mesh_volume(mesh: Mesh) -> float:
    volume = 0.0
    for triangle in mesh.triangles:
        a, b, c = (mesh.vertices[index] for index in triangle.vertices)
        volume += (
            a.x_mm * (b.y_mm * c.z_mm - b.z_mm * c.y_mm)
            - a.y_mm * (b.x_mm * c.z_mm - b.z_mm * c.x_mm)
            + a.z_mm * (b.x_mm * c.y_mm - b.y_mm * c.x_mm)
        ) / 6
    return volume


def box_mesh(width: float, depth: float, height: float) -> Mesh:
    vertices = (
        Point3(x_mm=0, y_mm=0, z_mm=0),
        Point3(x_mm=width, y_mm=0, z_mm=0),
        Point3(x_mm=width, y_mm=depth, z_mm=0),
        Point3(x_mm=0, y_mm=depth, z_mm=0),
        Point3(x_mm=0, y_mm=0, z_mm=height),
        Point3(x_mm=width, y_mm=0, z_mm=height),
        Point3(x_mm=width, y_mm=depth, z_mm=height),
        Point3(x_mm=0, y_mm=depth, z_mm=height),
    )
    triangles = (
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    )
    return Mesh.create(
        vertices=vertices,
        triangles=tuple(Triangle(vertices=item) for item in triangles),
        watertight=True,
    )


def test_flush_inlay_generates_every_present_color_and_explicitly_records_absent_materials():
    document = two_region_document()
    strategy = FlushInlayStrategy(
        base_top_z_mm=1.2,
        layer_height_mm=0.2,
        artwork_layers=2,
    )

    first = extrude_artwork_regions(document, strategy)
    second = extrude_artwork_regions(document, strategy)

    assert first == second
    assert first.fingerprint() == second.fingerprint()
    assert len(first.meshes) == len(first.parts) == len(document.islands) == 2
    assert all(mesh.watertight and mesh_volume(mesh) > 0 for mesh in first.meshes)
    assert {
        (min(vertex.z_mm for vertex in mesh.vertices), max(vertex.z_mm for vertex in mesh.vertices))
        for mesh in first.meshes
    } == {(1.2, 1.6)}
    coverage = {item.palette_color_id: item for item in first.material_coverage}
    assert coverage["blue"].present and coverage["orange"].present
    assert coverage["bone"].present is False
    assert coverage["bone"].island_ids == ()
    assert coverage["bone"].mesh_ids == ()
    assert coverage["bone"].part_ids == ()
    assert len(first.shared_interfaces) == 1
    assert first.shared_interfaces[0].bottom_z_mm == 1.2
    assert first.shared_interfaces[0].common_top_z_mm == 1.6


def test_shallow_raised_adds_only_whole_layers_to_non_background_labels():
    document = two_region_document()
    result = extrude_artwork_regions(
        document,
        ShallowRaisedStrategy(
            base_top_z_mm=1.2,
            layer_height_mm=0.2,
            foundation_layers=1,
            raised_layers=2,
        ),
    )

    labels = {item.id: item for item in document.source_labels}
    heights = {
        labels[item.source_label_id].classification: (item.layer_count, item.top_z_mm)
        for item in result.part_evidence
    }
    assert heights == {"background": (1, 1.4), "artwork": (3, 1.8)}
    assert result.shared_interfaces[0].common_top_z_mm == 1.4


def test_concavity_and_holes_preserve_exact_filled_volume_and_open_hole():
    document = holed_concave_document()
    result = extrude_artwork_regions(
        document,
        FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2, artwork_layers=2),
    )
    mesh = result.meshes[0]

    assert mesh.watertight
    assert mesh_volume(mesh) == pytest.approx(43 * 0.4, rel=1e-9, abs=1e-9)
    top_triangles = [
        triangle
        for triangle in mesh.triangles
        if all(mesh.vertices[index].z_mm == 1.6 for index in triangle.vertices)
    ]
    assert top_triangles
    for triangle in top_triangles:
        points = [mesh.vertices[index] for index in triangle.vertices]
        centroid = (sum(item.x_mm for item in points) / 3, sum(item.y_mm for item in points) / 3)
        assert not (1 < centroid[0] < 2 and 4 < centroid[1] < 5)


@pytest.mark.parametrize(
    ("width", "height", "hole_boxes"),
    (
        (12, 8, ((2, 2, 4, 4), (7, 3, 10, 6))),
        (16, 10, ((1, 1, 3, 4), (6, 5, 8, 8), (11, 2, 14, 6))),
        (
            30,
            20,
            tuple(
                (2 + column * 5, 2 + row * 5, 4 + column * 5, 4 + row * 5)
                for row in range(3)
                for column in range(5)
            ),
        ),
    ),
    ids=("two-holes", "three-holes", "fifteen-holes"),
)
def test_multiple_holes_are_bridged_deterministically_without_filling_apertures(
    width, height, hole_boxes
):
    document = multiple_hole_document(width=width, height=height, hole_boxes=hole_boxes)
    strategy = FlushInlayStrategy(
        base_top_z_mm=1.2,
        layer_height_mm=0.2,
        artwork_layers=2,
    )

    first = extrude_artwork_regions(document, strategy)
    second = extrude_artwork_regions(document, strategy)

    assert first == second
    removed_area = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in hole_boxes)
    assert first.meshes[0].watertight
    assert mesh_volume(first.meshes[0]) == pytest.approx((width * height - removed_area) * 0.4)
    top_triangles = [
        triangle
        for triangle in first.meshes[0].triangles
        if all(first.meshes[0].vertices[index].z_mm == 1.6 for index in triangle.vertices)
    ]
    for triangle in top_triangles:
        vertices = [first.meshes[0].vertices[index] for index in triangle.vertices]
        centroid = (
            sum(vertex.x_mm for vertex in vertices) / 3,
            sum(vertex.y_mm for vertex in vertices) / 3,
        )
        assert not any(
            x0 < centroid[0] < x1 and y0 < centroid[1] < y1 for x0, y0, x1, y1 in hole_boxes
        )


def test_triangulation_rejects_outside_and_touching_holes_with_actionable_errors():
    outer = [Point2(x_mm=x, y_mm=y) for x, y in ((0, 0), (10, 0), (10, 10), (0, 10))]
    outside = [Point2(x_mm=x, y_mm=y) for x, y in ((9, 2), (9, 4), (11, 4), (11, 2))]
    touching = [Point2(x_mm=x, y_mm=y) for x, y in ((0, 2), (0, 4), (2, 4), (2, 2))]

    with pytest.raises(ArtworkExtrusionError, match="hole 1 must be strictly inside"):
        _triangulate_polygon(outer, [outside])
    with pytest.raises(ArtworkExtrusionError, match="hole 1 must be strictly inside"):
        _triangulate_polygon(outer, [touching])


def test_atomic_shared_edges_split_an_unsubdivided_neighbor_at_the_same_junctions():
    document = two_region_document(subdivided=True)
    result = extrude_artwork_regions(
        document,
        FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2),
    )

    assert len(result.shared_interfaces) == 2
    island_mesh = {
        part.source_geometry_id: next(mesh for mesh in result.meshes if mesh.id == part.mesh_id)
        for part in result.parts
    }
    for island_id in (item.id for item in document.islands):
        vertices = {
            (vertex.x_mm, vertex.y_mm, vertex.z_mm) for vertex in island_mesh[island_id].vertices
        }
        assert (4, 2, 1.2) in vertices
        assert (4, 2, 1.6) in vertices


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: FlushInlayStrategy(base_top_z_mm=1.1, layer_height_mm=0.2),
        lambda: FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.0001),
        lambda: ShallowRaisedStrategy(
            base_top_z_mm=1.2,
            layer_height_mm=0.2,
            foundation_layers=0,
        ),
    ],
)
def test_z_strategies_reject_non_layer_aligned_or_non_printable_heights(constructor):
    with pytest.raises((ValidationError, ValueError)):
        constructor()


def test_assembly_proves_exact_base_contact_and_validates_complete_mesh_provenance():
    document = two_region_document()
    artwork = extrude_artwork_regions(
        document,
        FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2),
    )
    base_mesh = box_mesh(8, 4, 1.2)
    background_material = next(
        item for item in document.materials if item.palette_color_id == "blue"
    )
    base_part = Part.create(
        name="Structural base",
        role="base",
        mesh_id=base_mesh.id,
        material_id=background_material.id,
        source_geometry_kind="base",
        source_geometry_id=document.base.id,
    )

    assembled = assemble_mesh_document(
        document,
        base_mesh=base_mesh,
        base_part=base_part,
        artwork=artwork,
    )

    assert assembled.document.capabilities.mesh.status == CapabilityStatus.AVAILABLE
    assert assembled.document.capabilities.mesh.artifact_sha256 == assembled.mesh_artifact_sha256
    assert len(assembled.document.meshes) == 3
    assert len(assembled.document.parts) == 3
    assert sum(mesh_volume(mesh) for mesh in assembled.document.meshes) == pytest.approx(
        8 * 4 * 1.2 + 8 * 4 * 0.4
    )
    with pytest.raises(ArtworkExtrusionError, match="exactly contact"):
        assemble_mesh_document(
            document,
            base_mesh=box_mesh(8, 4, 1.0),
            base_part=Part.create(
                name="Wrong base",
                role="base",
                mesh_id=box_mesh(8, 4, 1.0).id,
                material_id=background_material.id,
                source_geometry_kind="base",
                source_geometry_id=document.base.id,
            ),
            artwork=artwork,
        )


def test_sliver_that_cannot_meet_mesh_tolerance_fails_instead_of_emitting_bad_geometry():
    ink = material("Ink", "#000000", "ink")
    ink_label = label("Ink", "#000000", "ink", ink, 1, "artwork")
    path = boundary([(0, 0), (0.00005, 0), (0, 10)])
    contour = Contour2D.create(role="exterior", path_id=path.id)
    island = Island2D.create(
        exterior_contour_id=contour.id,
        material_id=ink.id,
        source_label_id=ink_label.id,
    )
    document = GeometryDocument(
        schema_version=1,
        source_asset_sha256="b" * 64,
        processed_labels_sha256=LABEL_SHA,
        source_width_px=100,
        source_height_px=100,
        canvas_width_mm=10,
        canvas_height_mm=10,
        paths=(path,),
        contours=(contour,),
        materials=(ink,),
        source_labels=(ink_label,),
        islands=(island,),
        base=RectangleBase.create(center=Point2(x_mm=5, y_mm=5), width_mm=10, height_mm=10),
        capabilities=topology_capabilities(),
    )

    with pytest.raises(ArtworkExtrusionError, match="could not be extruded"):
        extrude_artwork_regions(
            document,
            FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2),
        )


def test_mesh_volumes_are_finite_and_layer_heights_are_exact_decimals():
    result = extrude_artwork_regions(
        two_region_document(),
        FlushInlayStrategy(base_top_z_mm=0.48, layer_height_mm=0.08, artwork_layers=3),
    )
    assert all(math.isfinite(mesh_volume(mesh)) for mesh in result.meshes)
    assert {item.top_z_mm for item in result.part_evidence} == {0.72}


def test_parallel_extrusion_matches_serial_geometry_exactly():
    document = two_region_document()
    strategy = FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2, artwork_layers=2)
    serial = extrude_artwork_regions(document, strategy, max_workers=1)
    parallel = extrude_artwork_regions(document, strategy, max_workers=2)
    assert parallel == serial
    assert parallel.fingerprint() == serial.fingerprint()


def test_indexed_ring_queries_match_scalar_predicates():
    from image23mf.geometry.extrusion import _point_inside, _point_strictly_inside, _RingQueries

    ring = [Point2(x_mm=x, y_mm=y) for x, y in ((0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6))]
    queries = _RingQueries(ring)
    for x in (-0.00001, 0, 0.00001, 1, 2, 2.00001, 4, 6, 6.00001):
        for y in (-0.00001, 0, 0.00001, 1, 2, 2.00001, 4, 6, 6.00001):
            point = Point2(x_mm=x, y_mm=y)
            assert queries.inside(point) == _point_inside(point, ring)
            assert queries.strictly_inside(point) == _point_strictly_inside(point, ring)
