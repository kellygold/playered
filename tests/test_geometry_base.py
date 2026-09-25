from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from image23mf.geometry import (
    BaseBuildBounds,
    BaseGenerationError,
    BaseMeshOptions,
    CircleBase,
    CustomBase,
    ExcludedBuildRectangle,
    LineSegment,
    Material,
    Path2D,
    Point2,
    RectangleBase,
    generate_structural_base,
)


def material() -> Material:
    return Material.create(
        name="Bone White",
        color_hex="#CBC6B8",
        palette_color_id="bone-white",
        filament_id="bambu-matte-bone-white",
    )


def bounds(**overrides: object) -> BaseBuildBounds:
    return BaseBuildBounds.model_validate({"width_mm": 256, "depth_mm": 256, **overrides})


def options(**overrides: object) -> BaseMeshOptions:
    return BaseMeshOptions.model_validate(
        {"thickness_mm": 1.2, "layer_height_mm": 0.2, **overrides}
    )


def boundary(points: list[tuple[float, float]]) -> Path2D:
    return Path2D.create(
        purpose="boundary",
        start=Point2(x_mm=points[0][0], y_mm=points[0][1]),
        segments=tuple(LineSegment(end=Point2(x_mm=x, y_mm=y)) for x, y in points[1:] + points[:1]),
        closed=True,
    )


def signed_volume(result: object) -> float:
    mesh = result.mesh  # type: ignore[attr-defined]
    volume = 0.0
    for triangle in mesh.triangles:
        a, b, c = (mesh.vertices[index] for index in triangle.vertices)
        volume += (
            a.x_mm * (b.y_mm * c.z_mm - b.z_mm * c.y_mm)
            - a.y_mm * (b.x_mm * c.z_mm - b.z_mm * c.x_mm)
            + a.z_mm * (b.x_mm * c.y_mm - b.y_mm * c.x_mm)
        ) / 6
    return volume


def test_non_square_base_has_exact_stable_bounds_and_layer_evidence() -> None:
    shape = RectangleBase.create(
        center=Point2(x_mm=100, y_mm=75),
        width_mm=200,
        height_mm=133.333,
        corner_radius_mm=0,
    )

    result = generate_structural_base(
        shape,
        material=material(),
        options=options(),
        build_bounds=bounds(),
    )

    assert result.bounds_min == Point2(x_mm=0, y_mm=8.3335)
    assert result.bounds_max == Point2(x_mm=200, y_mm=141.6665)
    assert result.layer_count == 6
    assert result.thickness_mm == 1.2
    assert result.total_height_mm == 1.2
    assert result.mesh.watertight is True
    assert result.part.mesh_id == result.mesh.id
    assert result.part.source_geometry_id == shape.id
    assert signed_volume(result) == pytest.approx(200 * 133.333 * 1.2)


@pytest.mark.parametrize(
    ("layer_height", "thickness", "expected_layers"),
    [(0.2, 1.2, 6), (0.1, 0.8, 8), (0.08, 0.48, 6)],
)
def test_representative_layer_heights_are_quantized_exactly(
    layer_height: float,
    thickness: float,
    expected_layers: int,
) -> None:
    shape = RectangleBase.create(
        center=Point2(x_mm=50, y_mm=50),
        width_mm=100,
        height_mm=100,
    )
    result = generate_structural_base(
        shape,
        material=material(),
        options=options(layer_height_mm=layer_height, thickness_mm=thickness),
        build_bounds=bounds(),
    )
    assert result.layer_count == expected_layers
    assert result.thickness_mm == thickness


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"thickness_mm": 0.2}, "at least 2 layers"),
        ({"thickness_mm": 1.1}, "not a whole multiple"),
        ({"rim_width_mm": 1, "rim_height_mm": 0.3}, "rim height"),
    ],
)
def test_structural_height_must_be_layer_compatible(
    overrides: dict[str, float], message: str
) -> None:
    shape = RectangleBase.create(center=Point2(x_mm=50, y_mm=50), width_mm=100, height_mm=100)
    with pytest.raises(BaseGenerationError, match=message):
        generate_structural_base(
            shape,
            material=material(),
            options=options(**overrides),
            build_bounds=bounds(),
        )


def test_circle_uses_chord_tolerance_and_integral_layer_aligned_rim() -> None:
    shape = CircleBase.create(center=Point2(x_mm=90, y_mm=90), radius_mm=50)
    result = generate_structural_base(
        shape,
        material=material(),
        options=options(rim_width_mm=2, rim_height_mm=0.4, curve_tolerance_mm=0.03),
        build_bounds=bounds(),
    )

    assert result.rim_enabled is True
    assert result.total_height_mm == 1.6
    assert result.curve_segments >= 24
    assert result.bounds_min.x_mm == pytest.approx(40, abs=0.03)
    assert result.bounds_max.x_mm == pytest.approx(140, abs=0.03)
    assert signed_volume(result) == pytest.approx(
        math.pi * 50**2 * 1.2 + math.pi * (50**2 - 48**2) * 0.4,
        rel=0.002,
    )


def test_rounded_rectangle_rim_is_deterministic_and_watertight() -> None:
    shape = RectangleBase.create(
        center=Point2(x_mm=100, y_mm=100),
        width_mm=120,
        height_mm=80,
        corner_radius_mm=10,
    )
    first = generate_structural_base(
        shape,
        material=material(),
        options=options(rim_width_mm=2, rim_height_mm=0.4),
        build_bounds=bounds(),
    )
    second = generate_structural_base(
        shape,
        material=material(),
        options=options(rim_width_mm=2, rim_height_mm=0.4),
        build_bounds=bounds(),
    )
    assert first.mesh == second.mesh
    assert first.part == second.part
    assert first.mesh.watertight is True
    assert signed_volume(first) > 0


def test_custom_concave_base_with_hole_has_correct_net_volume() -> None:
    exterior = boundary([(10, 10), (110, 10), (110, 110), (70, 110), (70, 60), (10, 60)])
    hole = boundary([(30, 30), (30, 45), (50, 45), (50, 30)])
    exterior_contour_id = "contour_" + "1" * 24
    hole_contour_id = "contour_" + "2" * 24
    shape = CustomBase.create(
        exterior_contour_id=exterior_contour_id,
        hole_contour_ids=(hole_contour_id,),
    )
    result = generate_structural_base(
        shape,
        material=material(),
        options=options(),
        build_bounds=bounds(),
        custom_paths={exterior_contour_id: exterior, hole_contour_id: hole},
    )

    # Exterior is 7,000 mm² and the hole is 300 mm².
    assert signed_volume(result) == pytest.approx(6700 * 1.2)
    assert result.mesh.watertight is True


def test_bounds_clearance_and_excluded_areas_fail_closed() -> None:
    shape = RectangleBase.create(center=Point2(x_mm=50, y_mm=50), width_mm=100, height_mm=100)
    with pytest.raises(BaseGenerationError, match="printable area"):
        generate_structural_base(
            shape,
            material=material(),
            options=options(),
            build_bounds=bounds(clearance_mm=1),
        )

    with pytest.raises(BaseGenerationError, match="excluded build-plate area"):
        generate_structural_base(
            shape,
            material=material(),
            options=options(),
            build_bounds=bounds(
                excluded_rectangles=(
                    ExcludedBuildRectangle(
                        minimum_x_mm=90,
                        minimum_y_mm=90,
                        maximum_x_mm=120,
                        maximum_y_mm=120,
                    ),
                )
            ),
        )


def test_invalid_custom_paths_and_unsupported_rims_are_explicit() -> None:
    contour_id = "contour_" + "1" * 24
    shape = CustomBase.create(exterior_contour_id=contour_id)
    with pytest.raises(BaseGenerationError, match="exterior path is missing"):
        generate_structural_base(
            shape,
            material=material(),
            options=options(),
            build_bounds=bounds(),
        )

    with pytest.raises(BaseGenerationError, match="custom-contour rims"):
        generate_structural_base(
            shape,
            material=material(),
            options=options(rim_width_mm=1, rim_height_mm=0.2),
            build_bounds=bounds(),
            custom_paths={contour_id: boundary([(10, 10), (50, 10), (50, 50), (10, 50)])},
        )


def test_excluded_area_wholly_inside_a_custom_void_is_allowed() -> None:
    exterior_id = "contour_" + "1" * 24
    hole_id = "contour_" + "2" * 24
    shape = CustomBase.create(
        exterior_contour_id=exterior_id,
        hole_contour_ids=(hole_id,),
    )
    result = generate_structural_base(
        shape,
        material=material(),
        options=options(),
        build_bounds=bounds(
            excluded_rectangles=(
                ExcludedBuildRectangle(
                    minimum_x_mm=42,
                    minimum_y_mm=42,
                    maximum_x_mm=48,
                    maximum_y_mm=48,
                ),
            )
        ),
        custom_paths={
            exterior_id: boundary([(10, 10), (100, 10), (100, 100), (10, 100)]),
            hole_id: boundary([(35, 35), (35, 55), (55, 55), (55, 35)]),
        },
    )
    assert result.mesh.watertight is True


def test_custom_holes_must_not_touch_or_nest() -> None:
    exterior_id = "contour_" + "1" * 24
    first_hole_id = "contour_" + "2" * 24
    second_hole_id = "contour_" + "3" * 24
    shape = CustomBase.create(
        exterior_contour_id=exterior_id,
        hole_contour_ids=(first_hole_id, second_hole_id),
    )
    with pytest.raises(BaseGenerationError, match="disjoint and non-nested"):
        generate_structural_base(
            shape,
            material=material(),
            options=options(),
            build_bounds=bounds(),
            custom_paths={
                exterior_id: boundary([(10, 10), (100, 10), (100, 100), (10, 100)]),
                first_hole_id: boundary([(30, 30), (30, 60), (60, 60), (60, 30)]),
                second_hole_id: boundary([(40, 40), (40, 50), (50, 50), (50, 40)]),
            },
        )


def test_rim_configuration_and_excluded_rectangles_validate_at_boundary() -> None:
    with pytest.raises(ValidationError, match="rim width and height"):
        options(rim_width_mm=1)
    with pytest.raises(ValidationError, match="maximums must exceed minimums"):
        ExcludedBuildRectangle(
            minimum_x_mm=10,
            minimum_y_mm=10,
            maximum_x_mm=10,
            maximum_y_mm=20,
        )


def test_rounded_corner_rim_rejects_degenerate_inner_radius() -> None:
    shape = RectangleBase.create(
        center=Point2(x_mm=50, y_mm=50),
        width_mm=100,
        height_mm=100,
        corner_radius_mm=4,
    )
    with pytest.raises(BaseGenerationError, match="consumes the rounded-corner radius"):
        generate_structural_base(
            shape,
            material=material(),
            options=options(rim_width_mm=4, rim_height_mm=0.2),
            build_bounds=bounds(),
        )
