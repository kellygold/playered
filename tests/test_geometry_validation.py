from __future__ import annotations

import math

import pytest

from image23mf.geometry import (
    BaseBuildBounds,
    Material,
    Mesh,
    MeshQualityOptions,
    MeshQualitySeverity,
    Part,
    Point3,
    Triangle,
    validate_part_meshes,
)


def cube(
    *,
    minimum_x: float = 0,
    minimum_y: float = 0,
    minimum_z: float = 0,
    width: float = 10,
    depth: float = 10,
    height: float = 1.2,
) -> Mesh:
    vertices = (
        Point3(x_mm=minimum_x, y_mm=minimum_y, z_mm=minimum_z),
        Point3(x_mm=minimum_x + width, y_mm=minimum_y, z_mm=minimum_z),
        Point3(x_mm=minimum_x + width, y_mm=minimum_y + depth, z_mm=minimum_z),
        Point3(x_mm=minimum_x, y_mm=minimum_y + depth, z_mm=minimum_z),
        Point3(x_mm=minimum_x, y_mm=minimum_y, z_mm=minimum_z + height),
        Point3(x_mm=minimum_x + width, y_mm=minimum_y, z_mm=minimum_z + height),
        Point3(
            x_mm=minimum_x + width,
            y_mm=minimum_y + depth,
            z_mm=minimum_z + height,
        ),
        Point3(x_mm=minimum_x, y_mm=minimum_y + depth, z_mm=minimum_z + height),
    )
    faces = (
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
        triangles=tuple(Triangle(vertices=item) for item in faces),
        watertight=True,
    )


def part(mesh: Mesh, *, role: str = "artwork", suffix: str = "1") -> Part:
    material = Material.create(
        name="Test",
        color_hex="#FFFFFF",
        palette_color_id=f"test-{suffix}",
    )
    if role == "base":
        return Part.create(
            name="Base",
            role="base",
            mesh_id=mesh.id,
            material_id=material.id,
            source_geometry_kind="base",
            source_geometry_id="base_" + suffix * 24,
            source_label_id=None,
        )
    return Part.create(
        name="Artwork",
        role=role,
        mesh_id=mesh.id,
        material_id=material.id,
        source_geometry_kind="island",
        source_geometry_id="island_" + suffix * 24,
        source_label_id="source-label_" + suffix * 24,
    )


def quality_options(**overrides: object) -> MeshQualityOptions:
    return MeshQualityOptions.model_validate(
        {
            "minimum_part_thickness_mm": 0.2,
            "build_bounds": BaseBuildBounds(width_mm=256, depth_mm=256),
            **overrides,
        }
    )


def finding_codes(report: object) -> set[str]:
    return {item.code for item in report.findings}  # type: ignore[attr-defined]


def test_valid_base_contact_is_safe_and_report_is_deterministic() -> None:
    base_mesh = cube(width=200, depth=133.333, height=1.2)
    artwork_mesh = cube(
        minimum_x=20,
        minimum_y=20,
        minimum_z=1.2,
        width=40,
        depth=30,
        height=0.4,
    )
    pairs = [(part(base_mesh, role="base"), base_mesh), (part(artwork_mesh), artwork_mesh)]

    first = validate_part_meshes(pairs, options=quality_options(), base_top_z_mm=1.2)
    second = validate_part_meshes(reversed(pairs), options=quality_options(), base_top_z_mm=1.2)

    assert first.safe_for_export is True
    assert first.findings == ()
    assert first.fingerprint == second.fingerprint
    assert first.total_signed_volume_mm3 == pytest.approx(200 * 133.333 * 1.2 + 40 * 30 * 0.4)
    assert first.evidence[0].connected_component_count == 1
    assert all(item.boundary_edge_count == 0 for item in first.evidence)


def test_floating_and_buried_parts_block_export_and_link_source_region() -> None:
    floating = cube(minimum_z=1.4, height=0.4)
    buried = cube(minimum_x=20, minimum_z=1.0, height=0.4)
    report = validate_part_meshes(
        [(part(floating, suffix="1"), floating), (part(buried, suffix="2"), buried)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )

    assert report.safe_for_export is False
    assert {"floating-part", "part-below-contact-plane"} <= finding_codes(report)
    assert report.affected_source_geometry_ids() == (
        "island_" + "1" * 24,
        "island_" + "2" * 24,
    )
    assert all(item.source_label_id is not None for item in report.findings)


def test_minimum_thickness_and_build_bounds_are_hard_gates() -> None:
    thin = cube(minimum_x=250, minimum_z=1.2, width=10, height=0.1)
    report = validate_part_meshes(
        [(part(thin), thin)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )
    assert report.safe_for_export is False
    assert {"part-too-thin", "outside-build-bounds"} <= finding_codes(report)


def test_open_mesh_reports_non_manifold_edges() -> None:
    valid = cube(minimum_z=1.2, height=0.4)
    open_mesh = Mesh.model_construct(
        id=valid.id,
        vertices=valid.vertices,
        triangles=valid.triangles[:-1],
        watertight=True,
    )
    report = validate_part_meshes(
        [(part(open_mesh), open_mesh)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )
    evidence = report.evidence[0]
    assert report.safe_for_export is False
    assert "non-manifold-mesh" in finding_codes(report)
    assert evidence.boundary_edge_count == 3


def test_declared_open_mesh_and_part_ownership_mismatch_fail_closed() -> None:
    valid = cube(minimum_z=1.2, height=0.4)
    declared_open = Mesh.model_construct(
        id=valid.id,
        vertices=(*valid.vertices, Point3(x_mm=99, y_mm=99, z_mm=99)),
        triangles=valid.triangles,
        watertight=False,
    )
    wrong_owner = part(valid).model_copy(update={"mesh_id": "mesh_" + "f" * 24})
    report = validate_part_meshes(
        [(wrong_owner, declared_open)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )
    assert {
        "mesh-not-declared-watertight",
        "part-mesh-mismatch",
        "unused-vertex",
    } <= finding_codes(report)


def test_invalid_indices_and_duplicate_faces_fail_closed() -> None:
    valid = cube(minimum_z=1.2, height=0.4)
    invalid = Mesh.model_construct(
        id=valid.id,
        vertices=valid.vertices,
        triangles=(
            *valid.triangles,
            valid.triangles[0],
            Triangle(vertices=(0, 1, 999)),
        ),
        watertight=True,
    )
    report = validate_part_meshes(
        [(part(invalid), invalid)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )
    assert {"duplicate-face", "invalid-triangle-index", "non-manifold-mesh"} <= finding_codes(
        report
    )


def test_non_finite_and_degenerate_geometry_fails_closed() -> None:
    valid = cube(minimum_z=1.2, height=0.4)
    non_finite_vertices = list(valid.vertices)
    non_finite_vertices[0] = Point3.model_construct(x_mm=float("nan"), y_mm=0, z_mm=1.2)
    invalid = Mesh.model_construct(
        id=valid.id,
        vertices=tuple(non_finite_vertices),
        triangles=valid.triangles,
        watertight=True,
    )
    report = validate_part_meshes(
        [(part(invalid), invalid)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )
    assert report.safe_for_export is False
    assert "non-finite-vertex" in finding_codes(report)
    assert math.isfinite(report.total_signed_volume_mm3)

    duplicate_vertices = list(valid.vertices)
    duplicate_vertices[1] = duplicate_vertices[0]
    degenerate = Mesh.model_construct(
        id=valid.id,
        vertices=tuple(duplicate_vertices),
        triangles=valid.triangles,
        watertight=True,
    )
    degenerate_report = validate_part_meshes(
        [(part(degenerate), degenerate)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )
    assert {"degenerate-triangle", "duplicate-vertex"} <= finding_codes(degenerate_report)


def test_inverted_winding_reports_conflicts_and_non_positive_volume() -> None:
    valid = cube(minimum_z=1.2, height=0.4)
    inverted = Mesh.model_construct(
        id=valid.id,
        vertices=valid.vertices,
        triangles=tuple(
            Triangle(vertices=(face.vertices[0], face.vertices[2], face.vertices[1]))
            for face in valid.triangles
        ),
        watertight=True,
    )
    report = validate_part_meshes(
        [(part(inverted), inverted)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )
    assert "non-positive-volume" in finding_codes(report)
    assert report.evidence[0].signed_volume_mm3 < 0


def test_disconnected_bodies_and_duplicate_mesh_ownership_are_blocked() -> None:
    first = cube(minimum_z=1.2, height=0.4)
    second = cube(minimum_x=20, minimum_z=1.2, height=0.4)
    offset = len(first.vertices)
    combined = Mesh.create(
        vertices=(*first.vertices, *second.vertices),
        triangles=(
            *first.triangles,
            *(
                Triangle(vertices=tuple(index + offset for index in face.vertices))
                for face in second.triangles
            ),
        ),
        watertight=True,
    )
    owner = part(combined, suffix="1")
    duplicate_owner = part(combined, role="support", suffix="2")
    report = validate_part_meshes(
        [(owner, combined), (duplicate_owner, combined)],
        options=quality_options(),
        base_top_z_mm=1.2,
    )
    assert {"disconnected-part", "mesh-owned-more-than-once"} <= finding_codes(report)
    assert all(item.connected_component_count == 2 for item in report.evidence)


def test_excluded_build_area_intersection_is_reported() -> None:
    from image23mf.geometry import ExcludedBuildRectangle

    mesh = cube(minimum_x=20, minimum_y=20, minimum_z=1.2, height=0.4)
    report = validate_part_meshes(
        [(part(mesh), mesh)],
        options=quality_options(
            build_bounds=BaseBuildBounds(
                width_mm=256,
                depth_mm=256,
                excluded_rectangles=(
                    ExcludedBuildRectangle(
                        minimum_x_mm=25,
                        minimum_y_mm=25,
                        maximum_x_mm=30,
                        maximum_y_mm=30,
                    ),
                ),
            )
        ),
        base_top_z_mm=1.2,
    )
    assert "intersects-excluded-build-area" in finding_codes(report)
    assert all(item.severity == MeshQualitySeverity.ERROR for item in report.findings)
