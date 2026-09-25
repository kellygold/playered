from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import image23mf.geometry.serialization as geometry_serialization
from image23mf.geometry import (
    COORDINATE_PRECISION_MM,
    MESH_TOLERANCE_MM,
    TOPOLOGY_TOLERANCE_MM,
    ArcSegment,
    CapabilityState,
    CircleBase,
    Contour2D,
    CubicSegment,
    CustomBase,
    GeometryCapabilities,
    GeometryDocument,
    GeometryIRDecodeError,
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
    deterministic_id,
    dump_geometry_ir,
    geometry_cache_key,
    geometry_ir_equal,
    geometry_ir_json_schema,
    load_geometry_ir,
)

GOLDEN = Path(__file__).parent / "goldens" / "geometry_ir_v1.json"
INVALID_CASES = Path(__file__).parent / "goldens" / "geometry_ir_invalid_cases.json"
INVALID_WIRE_FIXTURES = Path(__file__).parent / "goldens" / "geometry_ir_invalid"
PROCESSED_LABELS_SHA256 = "b" * 64


def _golden_bytes() -> bytes:
    return GOLDEN.read_bytes().rstrip(b"\n")


def _boundary(points: tuple[tuple[float, float], ...]) -> Path2D:
    start = Point2(x_mm=points[0][0], y_mm=points[0][1])
    return Path2D.create(
        purpose="boundary",
        start=start,
        segments=tuple(
            LineSegment(end=Point2(x_mm=x, y_mm=y)) for x, y in (*points[1:], points[0])
        ),
        closed=True,
    )


def _capabilities() -> GeometryCapabilities:
    return GeometryCapabilities(
        geometry_ir=CapabilityState(status="available", artifact_sha256="1" * 64),
        vector_geometry=CapabilityState(status="available", artifact_sha256="2" * 64),
        topology=CapabilityState(status="available", artifact_sha256="3" * 64),
        mesh=CapabilityState(status="available", artifact_sha256="4" * 64),
        package_3mf=CapabilityState(
            status="unavailable",
            reason="3MF packaging is not implemented in this build.",
        ),
        slicer_validation=CapabilityState(
            status="not_requested",
            reason="Waiting for a 3MF package.",
        ),
        download=CapabilityState(
            status="not_requested",
            reason="No validated package exists.",
        ),
    )


def _geometry_only_capabilities() -> GeometryCapabilities:
    return GeometryCapabilities(
        geometry_ir=CapabilityState(status="available", artifact_sha256="1" * 64),
        vector_geometry=CapabilityState(status="available", artifact_sha256="2" * 64),
        topology=CapabilityState(status="available", artifact_sha256="3" * 64),
        mesh=CapabilityState(status="unavailable", reason="mesh not requested"),
        package_3mf=CapabilityState(status="not_requested", reason="mesh unavailable"),
        slicer_validation=CapabilityState(status="not_requested", reason="package unavailable"),
        download=CapabilityState(status="not_requested", reason="validation unavailable"),
    )


def _vector_only_capabilities() -> GeometryCapabilities:
    return GeometryCapabilities(
        geometry_ir=CapabilityState(status="available", artifact_sha256="1" * 64),
        vector_geometry=CapabilityState(status="available", artifact_sha256="2" * 64),
        topology=CapabilityState(status="unavailable", reason="topology not generated"),
        mesh=CapabilityState(status="not_requested", reason="topology unavailable"),
        package_3mf=CapabilityState(status="not_requested", reason="mesh unavailable"),
        slicer_validation=CapabilityState(status="not_requested", reason="package unavailable"),
        download=CapabilityState(status="not_requested", reason="validation unavailable"),
    )


def _tetra_mesh(x_offset: float) -> Mesh:
    return Mesh.create(
        vertices=(
            Point3(x_mm=x_offset, y_mm=0, z_mm=0),
            Point3(x_mm=x_offset + 1, y_mm=0, z_mm=0),
            Point3(x_mm=x_offset, y_mm=1, z_mm=0),
            Point3(x_mm=x_offset, y_mm=0, z_mm=1),
        ),
        triangles=tuple(
            Triangle(vertices=item) for item in ((0, 1, 3), (0, 2, 1), (0, 3, 2), (1, 2, 3))
        ),
        watertight=True,
    )


def _two_island_geometry_document(
    left_path: Path2D,
    right_path: Path2D,
    *,
    left_hole_paths: tuple[Path2D, ...] = (),
    document_processed_labels_sha256: str = PROCESSED_LABELS_SHA256,
    left_label_palette_color_id: str = "blue",
) -> GeometryDocument:
    left_contour = Contour2D.create(role="exterior", path_id=left_path.id)
    right_contour = Contour2D.create(role="exterior", path_id=right_path.id)
    hole_contours = tuple(
        Contour2D.create(role="hole", path_id=path.id, parent_contour_id=left_contour.id)
        for path in left_hole_paths
    )
    materials = (
        Material.create(name="Blue", color_hex="#0078BF", palette_color_id="blue"),
        Material.create(name="Orange", color_hex="#F99963", palette_color_id="orange"),
    )
    labels = tuple(
        SourceLabel.create(
            source_asset_id="asset",
            processed_labels_sha256=PROCESSED_LABELS_SHA256,
            label_index=index,
            name=name,
            color_hex=color,
            palette_color_id=(left_label_palette_color_id if index == 0 else name.lower()),
            material_id=materials[index].id,
            classification="artwork",
        )
        for index, (name, color) in enumerate((("Blue", "#0078BF"), ("Orange", "#F99963")))
    )
    left_island = Island2D.create(
        exterior_contour_id=left_contour.id,
        hole_contour_ids=tuple(sorted(item.id for item in hole_contours)),
        material_id=materials[0].id,
        source_label_id=labels[0].id,
    )
    right_island = Island2D.create(
        exterior_contour_id=right_contour.id,
        material_id=materials[1].id,
        source_label_id=labels[1].id,
    )
    adjacent = tuple(sorted((left_island.id, right_island.id)))

    def edges(
        path: Path2D,
    ) -> dict[tuple[tuple[float, float], tuple[float, float]], tuple[Point2, Point2]]:
        points = (path.start, *(segment.end for segment in path.segments))
        return {
            tuple(sorted(((start.x_mm, start.y_mm), (end.x_mm, end.y_mm)))): (start, end)
            for start, end in zip(points, points[1:])
        }

    left_edges = {}
    for path in (left_path, *left_hole_paths):
        left_edges.update(edges(path))
    right_edges = edges(right_path)
    shared_keys = set(left_edges) & set(right_edges)
    for left_start, left_end in left_edges.values():
        for right_start, right_end in right_edges.values():
            x_values = (
                left_start.x_mm,
                left_end.x_mm,
                right_start.x_mm,
                right_end.x_mm,
            )
            if max(x_values) - min(x_values) <= TOPOLOGY_TOLERANCE_MM:
                low = max(
                    min(left_start.y_mm, left_end.y_mm),
                    min(right_start.y_mm, right_end.y_mm),
                )
                high = min(
                    max(left_start.y_mm, left_end.y_mm),
                    max(right_start.y_mm, right_end.y_mm),
                )
                if high > low:
                    x_mm = left_start.x_mm if adjacent[0] == left_island.id else right_start.x_mm
                    shared_keys.add(((x_mm, low), (x_mm, high)))
            y_values = (
                left_start.y_mm,
                left_end.y_mm,
                right_start.y_mm,
                right_end.y_mm,
            )
            if max(y_values) - min(y_values) <= TOPOLOGY_TOLERANCE_MM:
                low = max(
                    min(left_start.x_mm, left_end.x_mm),
                    min(right_start.x_mm, right_end.x_mm),
                )
                high = min(
                    max(left_start.x_mm, left_end.x_mm),
                    max(right_start.x_mm, right_end.x_mm),
                )
                if high > low:
                    y_mm = left_start.y_mm if adjacent[0] == left_island.id else right_start.y_mm
                    shared_keys.add(((low, y_mm), (high, y_mm)))
    shared_edges = tuple(
        SharedEdge.create(
            start=Point2(x_mm=key[0][0], y_mm=key[0][1]),
            end=Point2(x_mm=key[1][0], y_mm=key[1][1]),
            adjacent_island_ids=adjacent,
            owner_island_id=adjacent[0],
        )
        for key in sorted(shared_keys)
    )
    return GeometryDocument(
        schema_version=1,
        source_asset_sha256="a" * 64,
        processed_labels_sha256=document_processed_labels_sha256,
        source_width_px=100,
        source_height_px=100,
        canvas_width_mm=20,
        canvas_height_mm=20,
        paths=tuple(sorted((left_path, right_path, *left_hole_paths), key=lambda item: item.id)),
        contours=tuple(
            sorted((left_contour, right_contour, *hole_contours), key=lambda item: item.id)
        ),
        materials=tuple(sorted(materials, key=lambda item: item.id)),
        source_labels=tuple(sorted(labels, key=lambda item: item.id)),
        islands=tuple(sorted((left_island, right_island), key=lambda item: item.id)),
        shared_edges=tuple(sorted(shared_edges, key=lambda item: item.id)),
        base=RectangleBase.create(center=Point2(x_mm=10, y_mm=10), width_mm=20, height_mm=20),
        capabilities=_geometry_only_capabilities(),
    )


def valid_document(*, base_kind: str = "rectangle") -> GeometryDocument:
    left_path = _boundary(((0, 0), (10, 0), (10, 10), (0, 10)))
    right_path = _boundary(((10, 0), (20, 0), (20, 10), (10, 10)))
    hole_path = _boundary(((2, 2), (2, 4), (4, 4), (4, 2)))
    construction = Path2D.create(
        purpose="construction",
        start=Point2(x_mm=1, y_mm=1),
        segments=(
            ArcSegment(
                end=Point2(x_mm=3, y_mm=1),
                center=Point2(x_mm=2, y_mm=1),
                clockwise=False,
            ),
            CubicSegment(
                control_1=Point2(x_mm=4, y_mm=2),
                control_2=Point2(x_mm=5, y_mm=3),
                end=Point2(x_mm=6, y_mm=1),
            ),
        ),
        closed=False,
    )
    left_contour = Contour2D.create(role="exterior", path_id=left_path.id)
    right_contour = Contour2D.create(role="exterior", path_id=right_path.id)
    hole_contour = Contour2D.create(
        role="hole",
        path_id=hole_path.id,
        parent_contour_id=left_contour.id,
    )
    blue = Material.create(
        name="Marine Blue",
        color_hex="#0078BF",
        palette_color_id="blue",
        filament_id="bambu-blue",
    )
    orange = Material.create(
        name="Mandarin Orange",
        color_hex="#F99963",
        palette_color_id="orange",
        filament_id="bambu-orange",
    )
    left_label = SourceLabel.create(
        source_asset_id="source-1",
        processed_labels_sha256=PROCESSED_LABELS_SHA256,
        label_index=1,
        name="Ocean",
        color_hex="#0078BF",
        palette_color_id="blue",
        material_id=blue.id,
        classification="artwork",
    )
    right_label = SourceLabel.create(
        source_asset_id="source-1",
        processed_labels_sha256=PROCESSED_LABELS_SHA256,
        label_index=2,
        name="Sun",
        color_hex="#F99963",
        palette_color_id="orange",
        material_id=orange.id,
        classification="artwork",
    )
    left_island = Island2D.create(
        exterior_contour_id=left_contour.id,
        hole_contour_ids=(hole_contour.id,),
        material_id=blue.id,
        source_label_id=left_label.id,
    )
    right_island = Island2D.create(
        exterior_contour_id=right_contour.id,
        material_id=orange.id,
        source_label_id=right_label.id,
    )
    adjacent = tuple(sorted((left_island.id, right_island.id)))
    shared_edge = SharedEdge.create(
        start=Point2(x_mm=10, y_mm=0),
        end=Point2(x_mm=10, y_mm=10),
        adjacent_island_ids=adjacent,
        owner_island_id=adjacent[0],
    )
    extra_paths: tuple[Path2D, ...] = ()
    extra_contours: tuple[Contour2D, ...] = ()
    if base_kind == "rectangle":
        base = RectangleBase.create(
            center=Point2(x_mm=10, y_mm=5),
            width_mm=20,
            height_mm=10,
            corner_radius_mm=1,
        )
    elif base_kind == "circle":
        base = CircleBase.create(center=Point2(x_mm=10, y_mm=5), radius_mm=5)
    else:
        base_path = _boundary(((-1, -1), (21, -1), (21, 11), (-1, 11)))
        base_contour = Contour2D.create(role="exterior", path_id=base_path.id)
        base = CustomBase.create(
            exterior_contour_id=base_contour.id,
        )
        extra_paths = (base_path,)
        extra_contours = (base_contour,)
    base_mesh = _tetra_mesh(0)
    left_mesh = _tetra_mesh(2)
    right_mesh = _tetra_mesh(4)
    parts = (
        Part.create(
            name="Base",
            role="base",
            mesh_id=base_mesh.id,
            material_id=blue.id,
            source_geometry_kind="base",
            source_geometry_id=base.id,
        ),
        Part.create(
            name="Blue artwork",
            role="artwork",
            mesh_id=left_mesh.id,
            material_id=blue.id,
            source_geometry_kind="island",
            source_geometry_id=left_island.id,
            source_label_id=left_label.id,
        ),
        Part.create(
            name="Orange artwork",
            role="artwork",
            mesh_id=right_mesh.id,
            material_id=orange.id,
            source_geometry_kind="island",
            source_geometry_id=right_island.id,
            source_label_id=right_label.id,
        ),
    )
    return GeometryDocument(
        schema_version=1,
        source_asset_sha256="a" * 64,
        processed_labels_sha256=PROCESSED_LABELS_SHA256,
        source_width_px=2000,
        source_height_px=1000,
        canvas_width_mm=20,
        canvas_height_mm=10,
        paths=tuple(
            sorted(
                (left_path, right_path, hole_path, construction, *extra_paths),
                key=lambda item: item.id,
            )
        ),
        contours=tuple(
            sorted(
                (left_contour, right_contour, hole_contour, *extra_contours),
                key=lambda item: item.id,
            )
        ),
        materials=tuple(sorted((blue, orange), key=lambda item: item.id)),
        source_labels=tuple(sorted((left_label, right_label), key=lambda item: item.id)),
        islands=tuple(sorted((left_island, right_island), key=lambda item: item.id)),
        shared_edges=(shared_edge,),
        meshes=tuple(sorted((base_mesh, left_mesh, right_mesh), key=lambda item: item.id)),
        parts=tuple(sorted(parts, key=lambda item: item.id)),
        base=base,
        capabilities=_capabilities(),
    )


def _replace(payload: dict, path: str, value) -> dict:
    current = payload
    pieces = path.split(".")
    for piece in pieces[:-1]:
        current = current[int(piece)] if isinstance(current, list) else current[piece]
    final = pieces[-1]
    if value == "__delete__":
        del current[final]
    elif isinstance(current, list):
        current[int(final)] = value
    else:
        current[final] = value
    return payload


def test_contract_fixes_units_axes_y_conversion_winding_fill_and_precision() -> None:
    contract = valid_document().contract

    assert contract.units == "millimetre"
    assert contract.origin == "lower_left_build_plate"
    assert (contract.x_axis, contract.y_axis, contract.z_axis) == (
        "right",
        "away_from_viewer",
        "up",
    )
    assert contract.source_image_origin == "upper_left"
    assert contract.source_image_y_axis == "down"
    assert contract.source_to_geometry_y == "invert_about_canvas_height"
    assert contract.exterior_winding == "counter_clockwise"
    assert contract.hole_winding == "clockwise"
    assert contract.fill_rule == "non_zero"
    assert contract.coordinate_precision_mm == COORDINATE_PRECISION_MM
    assert contract.topology_tolerance_mm == TOPOLOGY_TOLERANCE_MM
    assert contract.mesh_tolerance_mm == MESH_TOLERANCE_MM


def test_every_entity_has_a_deterministic_content_id_and_document_fingerprint() -> None:
    first = valid_document()
    second = valid_document()

    assert first == second
    assert first.fingerprint() == second.fingerprint()
    assert first.canonical_bytes() == second.canonical_bytes()
    for collection in (
        first.paths,
        first.contours,
        first.materials,
        first.source_labels,
        first.islands,
        first.shared_edges,
        first.meshes,
        first.parts,
    ):
        assert all(item.id == item.expected_id() for item in collection)
    assert deterministic_id("test", {"b": 2, "a": 1}) == deterministic_id("test", {"a": 1, "b": 2})


def test_canonical_round_trip_is_byte_deterministic_and_defines_equality() -> None:
    document = valid_document()
    encoded = dump_geometry_ir(document)
    decoded = load_geometry_ir(encoded)

    assert dump_geometry_ir(decoded) == encoded
    assert geometry_ir_equal(document, decoded)
    assert document.fingerprint() == decoded.fingerprint()
    noncanonical = json.dumps(document.model_dump(mode="json"), indent=2).encode()
    with pytest.raises(GeometryIRDecodeError, match="not canonical"):
        load_geometry_ir(noncanonical)
    assert load_geometry_ir(noncanonical, require_canonical=False) == document


def test_committed_golden_is_the_exact_canonical_wire_format() -> None:
    document = valid_document()

    assert _golden_bytes() == document.canonical_bytes()
    assert load_geometry_ir(_golden_bytes()) == document


def test_rectangle_circle_and_custom_bases_are_explicit_schema_variants() -> None:
    rectangle = valid_document(base_kind="rectangle")
    circle = valid_document(base_kind="circle")
    custom = valid_document(base_kind="custom")

    assert rectangle.base.kind == "rectangle"
    assert circle.base.kind == "circle"
    assert custom.base.kind == "custom"
    schema = geometry_ir_json_schema()
    assert schema["properties"]["base"]["discriminator"]["propertyName"] == "kind"


def test_shared_edge_has_one_deterministic_owner_and_unique_geometry() -> None:
    document = valid_document()
    edge = document.shared_edges[0]

    assert edge.owner_island_id == min(edge.adjacent_island_ids)
    payload = document.model_dump(mode="json")
    payload["shared_edges"] = [payload["shared_edges"][0], payload["shared_edges"][0]]
    with pytest.raises(ValidationError, match="canonical ID ordering|globally unique|unique"):
        GeometryDocument.model_validate(payload)

    edge_payload = edge.model_dump(mode="json", exclude={"id"})
    edge_payload["owner_island_id"] = max(edge.adjacent_island_ids)
    with pytest.raises(ValidationError, match="lexicographically first"):
        SharedEdge.create(**edge_payload)

    off_boundary = SharedEdge.create(
        start=Point2(x_mm=9, y_mm=0),
        end=Point2(x_mm=9, y_mm=10),
        adjacent_island_ids=edge.adjacent_island_ids,
        owner_island_id=edge.owner_island_id,
    )
    payload = document.model_dump(mode="json")
    payload["shared_edges"] = [off_boundary.model_dump(mode="json")]
    with pytest.raises(ValidationError, match="exactly cover"):
        GeometryDocument.model_validate(payload)


def test_winding_hole_ownership_mesh_references_and_content_ids_are_enforced() -> None:
    original = valid_document().model_dump(mode="json")
    exterior_index = next(
        index for index, contour in enumerate(original["contours"]) if contour["role"] == "exterior"
    )
    exterior_path_id = original["contours"][exterior_index]["path_id"]
    path_index = next(
        index for index, path in enumerate(original["paths"]) if path["id"] == exterior_path_id
    )
    path = original["paths"][path_index]
    vertices = [path["start"], *(segment["end"] for segment in path["segments"][:-1])]
    reversed_vertices = tuple((point["x_mm"], point["y_mm"]) for point in reversed(vertices))
    wrong_path = _boundary(reversed_vertices)
    exterior = Contour2D.model_validate(original["contours"][exterior_index])
    with pytest.raises(ValueError, match="counter-clockwise"):
        exterior.validate_path(wrong_path)

    bad_mesh = valid_document().model_dump(mode="json")
    bad_mesh["parts"][0]["mesh_id"] = "mesh_000000000000000000000000"
    part_payload = dict(bad_mesh["parts"][0])
    part_payload.pop("id")
    bad_mesh["parts"][0] = Part.create(**part_payload).model_dump(mode="json")
    bad_mesh["parts"].sort(key=lambda item: item["id"])
    with pytest.raises(ValidationError, match="missing mesh"):
        GeometryDocument.model_validate(bad_mesh)

    bad_id = valid_document().model_dump(mode="json")
    bad_id["materials"][0]["name"] = "Changed without updating ID"
    with pytest.raises(ValidationError, match="content ID mismatch"):
        GeometryDocument.model_validate(bad_id)


def test_capability_states_are_honest_and_cannot_skip_dependencies() -> None:
    with pytest.raises(ValidationError, match="require a reason"):
        CapabilityState(status="unavailable")
    with pytest.raises(ValidationError, match="only available"):
        CapabilityState(status="failed", reason="worker failed", artifact_sha256="f" * 64)
    with pytest.raises(ValidationError, match="dependencies"):
        GeometryCapabilities(
            geometry_ir=CapabilityState(status="available", artifact_sha256="1" * 64),
            vector_geometry=CapabilityState(status="unavailable", reason="not generated"),
            topology=CapabilityState(status="unavailable", reason="not generated"),
            mesh=CapabilityState(status="unavailable", reason="not generated"),
            package_3mf=CapabilityState(status="available", artifact_sha256="2" * 64),
            slicer_validation=CapabilityState(status="not_requested"),
            download=CapabilityState(status="not_requested"),
        )


def test_vector_only_and_topology_complete_capabilities_match_document_content() -> None:
    construction = Path2D.create(
        purpose="construction",
        start=Point2(x_mm=0, y_mm=0),
        segments=(LineSegment(end=Point2(x_mm=1, y_mm=1)),),
        closed=False,
    )
    vector_only = GeometryDocument(
        schema_version=1,
        source_asset_sha256="a" * 64,
        processed_labels_sha256=PROCESSED_LABELS_SHA256,
        source_width_px=10,
        source_height_px=10,
        canvas_width_mm=10,
        canvas_height_mm=10,
        paths=(construction,),
        base=RectangleBase.create(center=Point2(x_mm=5, y_mm=5), width_mm=10, height_mm=10),
        capabilities=_vector_only_capabilities(),
    )
    assert vector_only.capabilities.vector_geometry.status.value == "available"
    assert vector_only.capabilities.topology.status.value == "unavailable"

    topology_complete = _two_island_geometry_document(
        _boundary(((0, 0), (5, 0), (5, 5), (0, 5))),
        _boundary(((5, 0), (10, 0), (10, 5), (5, 5))),
    )
    assert topology_complete.capabilities.topology.status.value == "available"
    assert topology_complete.capabilities.mesh.status.value == "unavailable"

    payload = topology_complete.model_dump(mode="json")
    payload["capabilities"]["topology"] = CapabilityState(
        status="unavailable", reason="incorrectly hidden"
    ).model_dump(mode="json")
    payload["capabilities"]["mesh"] = CapabilityState(
        status="not_requested", reason="topology unavailable"
    ).model_dump(mode="json")
    with pytest.raises(ValidationError, match="topology capability must agree"):
        GeometryDocument.model_validate(payload)


def test_processed_label_provenance_and_palette_material_mapping_are_immutable() -> None:
    left = _boundary(((0, 0), (5, 0), (5, 5), (0, 5)))
    right = _boundary(((5, 0), (10, 0), (10, 5), (5, 5)))
    with pytest.raises(ValidationError, match="authoritative processed-label artifact"):
        _two_island_geometry_document(
            left,
            right,
            document_processed_labels_sha256="c" * 64,
        )
    with pytest.raises(ValidationError, match="palette color and material"):
        _two_island_geometry_document(
            left,
            right,
            left_label_palette_color_id="orange",
        )

    material = Material.create(name="Base", color_hex="#FFFFFF", palette_color_id="base")
    with pytest.raises(ValidationError, match="artwork.*background.*support"):
        SourceLabel.create(
            source_asset_id="source-1",
            processed_labels_sha256=PROCESSED_LABELS_SHA256,
            label_index=4,
            name="Impossible base label",
            color_hex="#FFFFFF",
            palette_color_id="base",
            material_id=material.id,
            classification="base",
        )

    document = valid_document()
    extra = SourceLabel.create(
        source_asset_id="source-1",
        processed_labels_sha256=PROCESSED_LABELS_SHA256,
        label_index=3,
        name="Unused",
        color_hex=document.materials[0].color_hex,
        palette_color_id=document.materials[0].palette_color_id,
        material_id=document.materials[0].id,
        classification="artwork",
    )
    payload = document.model_dump(mode="json")
    payload["source_labels"] = sorted(
        (*payload["source_labels"], extra.model_dump(mode="json")),
        key=lambda item: item["id"],
    )
    with pytest.raises(ValidationError, match="every source label must be represented"):
        GeometryDocument.model_validate(payload)


def test_mesh_watertightness_winding_and_source_label_identity_are_invariants() -> None:
    vertices = (
        Point3(x_mm=0, y_mm=0, z_mm=0),
        Point3(x_mm=10, y_mm=0, z_mm=0),
        Point3(x_mm=0, y_mm=10, z_mm=0),
        Point3(x_mm=0, y_mm=0, z_mm=1),
    )
    with pytest.raises(ValidationError, match="consistent outward winding"):
        Mesh.create(
            vertices=vertices,
            triangles=tuple(
                Triangle(vertices=item) for item in ((0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3))
            ),
            watertight=True,
        )

    document = valid_document()
    duplicate = SourceLabel.create(
        source_asset_id=document.source_labels[0].source_asset_id,
        processed_labels_sha256=document.processed_labels_sha256,
        label_index=document.source_labels[0].label_index,
        name="Conflicting duplicate",
        color_hex="#FFFFFF",
        palette_color_id="duplicate",
        material_id=document.materials[0].id,
        classification="artwork",
    )
    payload = document.model_dump(mode="json")
    payload["source_labels"] = sorted(
        [*payload["source_labels"], duplicate.model_dump(mode="json")],
        key=lambda item: item["id"],
    )
    with pytest.raises(ValidationError, match="label indices must be unique"):
        GeometryDocument.model_validate(payload)


def test_mesh_degeneracy_uses_minimum_altitude_independent_of_edge_scale() -> None:
    def skinny_triangle(altitude_mm: float) -> Mesh:
        return Mesh.create(
            vertices=(
                Point3(x_mm=0, y_mm=0, z_mm=0),
                Point3(x_mm=500_000, y_mm=altitude_mm, z_mm=0),
                Point3(x_mm=1_000_000, y_mm=0, z_mm=0),
            ),
            triangles=(Triangle(vertices=(0, 1, 2)),),
            watertight=False,
        )

    with pytest.raises(ValidationError, match="minimum altitude"):
        skinny_triangle(0.000099)
    assert skinny_triangle(0.000101).triangles


def test_schema_rejects_unknown_versions_fields_nonfinite_numbers_and_bad_precision() -> None:
    payload = valid_document().model_dump(mode="json")
    payload["schema_version"] = 2
    with pytest.raises(ValidationError, match="Input should be 1"):
        GeometryDocument.model_validate(payload)
    payload = valid_document().model_dump(mode="json")
    payload["surprise"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GeometryDocument.model_validate(payload)
    with pytest.raises(GeometryIRDecodeError, match="non-finite"):
        load_geometry_ir(b'{"schema_version":NaN}')
    with pytest.raises(GeometryIRDecodeError, match="duplicate JSON key"):
        load_geometry_ir(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValidationError, match="0.000001 mm precision"):
        Point2(x_mm=0.0000004, y_mm=0)


def test_invalid_fixture_manifest_exercises_schema_and_invariant_rejections() -> None:
    base = json.loads(GOLDEN.read_text())
    cases = json.loads(INVALID_CASES.read_text())

    for case in cases:
        payload = json.loads(json.dumps(base))
        _replace(payload, case["path"], case["value"])
        with pytest.raises((ValidationError, GeometryIRDecodeError), match=case["match"]):
            GeometryDocument.model_validate(payload)


@pytest.mark.parametrize(
    ("name", "match"),
    (
        ("duplicate-key.json", "duplicate JSON key"),
        ("noncanonical.json", "schema validation failed"),
        ("unknown-version.json", "schema validation failed"),
    ),
)
def test_invalid_wire_fixtures_are_rejected_by_the_real_parser(name: str, match: str) -> None:
    with pytest.raises(GeometryIRDecodeError, match=match):
        load_geometry_ir((INVALID_WIRE_FIXTURES / name).read_bytes().rstrip(b"\n"))


def test_cache_keys_are_namespaced_deterministic_and_dependency_order_is_canonical() -> None:
    document = valid_document()
    dependencies = ("1" * 64, "2" * 64)

    key = geometry_cache_key(document, dependencies)
    assert key == document.cache_key(*dependencies)
    assert key.startswith("image23mf-geometry-ir-v1:")
    assert len(key.rsplit(":", 1)[1]) == 64
    with pytest.raises(ValueError, match="unique and sorted"):
        geometry_cache_key(document, tuple(reversed(dependencies)))
    with pytest.raises(ValueError, match="SHA-256"):
        geometry_cache_key(document, ("not-a-hash",))


def test_triangle_and_path_primitives_reject_degenerate_or_ambiguous_data() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        Triangle(vertices=(0, 0, 1))
    with pytest.raises(ValidationError, match="end exactly"):
        Path2D.create(
            purpose="boundary",
            start=Point2(x_mm=0, y_mm=0),
            segments=(
                LineSegment(end=Point2(x_mm=1, y_mm=0)),
                LineSegment(end=Point2(x_mm=1, y_mm=1)),
                LineSegment(end=Point2(x_mm=0, y_mm=1)),
            ),
            closed=True,
        )


def test_boundary_identity_is_invariant_to_cyclic_start_and_rejects_crossings() -> None:
    points = ((0, 0), (4, 0), (4, 3), (0, 3))
    paths = tuple(_boundary(points[offset:] + points[:offset]) for offset in range(4))

    assert len({path.id for path in paths}) == 1
    assert (
        len(
            {
                path.canonical_json() if hasattr(path, "canonical_json") else path.expected_id()
                for path in paths
            }
        )
        == 1
    )
    with pytest.raises(ValidationError, match="self-intersect"):
        _boundary(((0, 0), (4, 4), (0, 4), (4, 0)))


@given(st.integers())
def test_boundary_identity_property_is_invariant_to_any_cyclic_offset(offset: int) -> None:
    points = ((0, 0), (7, 0), (9, 4), (3, 8), (0, 5))
    normalized = offset % len(points)
    assert _boundary(points).id == _boundary(points[normalized:] + points[:normalized]).id


def test_arc_radius_and_mesh_canonical_identity_and_outward_volume_are_enforced() -> None:
    with pytest.raises(ValidationError, match="radii must match"):
        Path2D.create(
            purpose="construction",
            start=Point2(x_mm=0, y_mm=0),
            segments=(
                ArcSegment(
                    end=Point2(x_mm=2, y_mm=0),
                    center=Point2(x_mm=0, y_mm=1),
                    clockwise=False,
                ),
            ),
            closed=False,
        )

    original_vertices = (
        Point3(x_mm=0, y_mm=0, z_mm=0),
        Point3(x_mm=1, y_mm=0, z_mm=0),
        Point3(x_mm=0, y_mm=1, z_mm=0),
        Point3(x_mm=0, y_mm=0, z_mm=1),
    )
    original_triangles = ((0, 1, 3), (0, 2, 1), (0, 3, 2), (1, 2, 3))
    first = Mesh.create(
        vertices=original_vertices,
        triangles=tuple(Triangle(vertices=item) for item in original_triangles),
        watertight=True,
    )
    permutation = (2, 0, 3, 1)
    new_index = {old: new for new, old in enumerate(permutation)}
    second = Mesh.create(
        vertices=tuple(original_vertices[index] for index in permutation),
        triangles=tuple(
            Triangle(vertices=tuple(new_index[index] for index in triangle))
            for triangle in reversed(original_triangles)
        ),
        watertight=True,
    )
    assert first.id == second.id
    assert first == second

    with pytest.raises(ValidationError, match="positive outward volume"):
        Mesh.create(
            vertices=original_vertices,
            triangles=tuple(
                Triangle(vertices=(triangle[0], triangle[2], triangle[1]))
                for triangle in original_triangles
            ),
            watertight=True,
        )


def test_document_requires_exhaustive_shared_edges_and_capability_evidence() -> None:
    payload = valid_document().model_dump(mode="json")
    payload["shared_edges"] = []
    with pytest.raises(ValidationError, match="exactly cover"):
        GeometryDocument.model_validate(payload)

    with pytest.raises(ValidationError, match="artifact fingerprint"):
        CapabilityState(status="available")

    with pytest.raises(ValidationError, match="vector-geometry capability must agree"):
        GeometryDocument(
            schema_version=1,
            source_asset_sha256="a" * 64,
            processed_labels_sha256=PROCESSED_LABELS_SHA256,
            source_width_px=1,
            source_height_px=1,
            canvas_width_mm=1,
            canvas_height_mm=1,
            base=RectangleBase.create(center=Point2(x_mm=0.5, y_mm=0.5), width_mm=1, height_mm=1),
            capabilities=_capabilities(),
        )


def test_part_lineage_mesh_ownership_and_orphan_rejection_are_exhaustive() -> None:
    document = valid_document()
    payload = document.model_dump(mode="json")
    artwork_index = next(
        index for index, part in enumerate(payload["parts"]) if part["role"] == "artwork"
    )
    part_payload = dict(payload["parts"][artwork_index])
    part_payload.pop("id")
    current_material = part_payload["material_id"]
    part_payload["material_id"] = next(
        material.id for material in document.materials if material.id != current_material
    )
    payload["parts"][artwork_index] = Part.create(**part_payload).model_dump(mode="json")
    payload["parts"].sort(key=lambda item: item["id"])
    with pytest.raises(ValidationError, match="material and label must match"):
        GeometryDocument.model_validate(payload)

    orphaned = document.model_dump(mode="json")
    orphaned["parts"] = orphaned["parts"][1:]
    with pytest.raises(ValidationError, match="every mesh must belong"):
        GeometryDocument.model_validate(orphaned)


def test_holes_must_be_strictly_contained_and_custom_base_owns_its_contours() -> None:
    exterior_path = _boundary(((0, 0), (10, 0), (10, 10), (0, 10)))
    outside_hole_path = _boundary(((20, 20), (20, 22), (22, 22), (22, 20)))
    exterior = Contour2D.create(role="exterior", path_id=exterior_path.id)
    hole = Contour2D.create(
        role="hole", path_id=outside_hole_path.id, parent_contour_id=exterior.id
    )
    material = Material.create(name="Blue", color_hex="#0078BF", palette_color_id="blue")
    label = SourceLabel.create(
        source_asset_id="asset",
        processed_labels_sha256=PROCESSED_LABELS_SHA256,
        label_index=0,
        name="Blue",
        color_hex="#0078BF",
        palette_color_id="blue",
        material_id=material.id,
        classification="artwork",
    )
    island = Island2D.create(
        exterior_contour_id=exterior.id,
        hole_contour_ids=(hole.id,),
        material_id=material.id,
        source_label_id=label.id,
    )
    with pytest.raises(ValidationError, match="strictly inside"):
        GeometryDocument(
            schema_version=1,
            source_asset_sha256="a" * 64,
            processed_labels_sha256=PROCESSED_LABELS_SHA256,
            source_width_px=100,
            source_height_px=100,
            canvas_width_mm=25,
            canvas_height_mm=25,
            paths=tuple(sorted((exterior_path, outside_hole_path), key=lambda item: item.id)),
            contours=tuple(sorted((exterior, hole), key=lambda item: item.id)),
            materials=(material,),
            source_labels=(label,),
            islands=(island,),
            base=RectangleBase.create(
                center=Point2(x_mm=12.5, y_mm=12.5), width_mm=25, height_mm=25
            ),
            capabilities=_geometry_only_capabilities(),
        )

    custom = valid_document(base_kind="custom")
    custom_contours = {custom.base.exterior_contour_id, *custom.base.hole_contour_ids}
    island_contours = {
        contour_id
        for item in custom.islands
        for contour_id in (item.exterior_contour_id, *item.hole_contour_ids)
    }
    assert custom_contours.isdisjoint(island_contours)


def test_island_overlap_subdivided_edges_and_exterior_to_hole_adjacency() -> None:
    with pytest.raises(ValidationError, match="filled regions cannot overlap"):
        _two_island_geometry_document(
            _boundary(((0, 0), (10, 0), (10, 10), (0, 10))),
            _boundary(((5, 2), (15, 2), (15, 12), (5, 12))),
        )

    subdivided = _two_island_geometry_document(
        _boundary(((0, 0), (10, 0), (10, 10), (0, 10))),
        _boundary(((10, 0), (20, 0), (20, 10), (10, 10), (10, 5))),
    )
    assert len(subdivided.shared_edges) == 2

    near_equal = _two_island_geometry_document(
        _boundary(((0, 0), (10, 0), (10, 10), (0, 10))),
        _boundary(((10.000005, 0), (20, 0), (20, 10), (10.000005, 10))),
    )
    assert len(near_equal.shared_edges) == 1

    outer = _boundary(((0, 0), (10, 0), (10, 10), (0, 10)))
    hole = _boundary(((2, 2), (2, 8), (8, 8), (8, 2)))
    inner = _boundary(((2, 2), (8, 2), (8, 8), (2, 8)))
    document = _two_island_geometry_document(outer, inner, left_hole_paths=(hole,))
    assert len(document.shared_edges) == 4


def test_parser_limits_duplicate_keys_and_canonical_bytes_are_enforced(monkeypatch) -> None:
    with pytest.raises(GeometryIRDecodeError, match="duplicate JSON key"):
        load_geometry_ir(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(GeometryIRDecodeError, match="nesting limit"):
        load_geometry_ir(("[" * 129 + "]" * 129).encode())
    monkeypatch.setattr(geometry_serialization, "MAX_GEOMETRY_IR_BYTES", 8)
    with pytest.raises(GeometryIRDecodeError, match="wire-size limit"):
        load_geometry_ir(b'{"schema_version":1}')


def test_text_and_version_domain_are_part_of_canonical_identity() -> None:
    with pytest.raises(ValidationError, match="Unicode NFC"):
        Material.create(name="Cafe\u0301", color_hex="#FFFFFF", palette_color_id="white")
    entity = Material.create(name="Café", color_hex="#FFFFFF", palette_color_id="white")
    payload = entity.model_dump(mode="json", exclude={"id"})
    legacy_unversioned = (
        "material_"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    assert entity.id == deterministic_id("material", payload)
    assert entity.id != legacy_unversioned
    with pytest.raises(ValidationError, match="linear segments"):
        Path2D.create(
            purpose="boundary",
            start=Point2(x_mm=0, y_mm=0),
            segments=(
                ArcSegment(
                    end=Point2(x_mm=1, y_mm=0),
                    center=Point2(x_mm=0.5, y_mm=0),
                    clockwise=False,
                ),
                LineSegment(end=Point2(x_mm=0, y_mm=1)),
                LineSegment(end=Point2(x_mm=0, y_mm=0)),
            ),
            closed=True,
        )


def test_large_boundary_checks_only_spatially_near_edges(monkeypatch):
    import image23mf.geometry.model as model

    original = model._segments_intersect
    calls = 0

    def measured(*args):
        nonlocal calls
        calls += 1
        assert calls < 10000, "Boundary validation regressed to exhaustive edge comparisons"
        return original(*args)

    monkeypatch.setattr(model, "_segments_intersect", measured)
    points = [(0, 0), (1000, 0), (1000, 10)]
    for x in range(999, -1, -1):
        points.extend(((x + 0.5, 11), (x, 10)))
    path = _boundary(tuple(points))
    assert path.signed_area_mm2 > 0
