import io

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image, ImageOps
from pydantic import ValidationError

from image23mf.contracts.job import CropConfig, CropMode
from image23mf.engine import (
    Affine2D,
    CanonicalTransform,
    CoordinateStage,
    DimensionAxis,
    MillimetreSize,
    PixelSize,
    Point,
    Rect,
    ingest_image,
    normalized_size_for_orientation,
    orientation_matrix,
    resize_canvas,
)

DEFAULT_WORKING = PixelSize(width=100, height=100)
# The supported aspect-ratio extremes can compose a roughly 1e4:1 source through an
# inverse roughly 1:2e3 working transform. IEEE-754 cancellation in that full chain has
# a measured worst case just under 2e-9 source pixels, so keep the proof strict while
# leaving a small deterministic margin above the numeric floor.
POINT_ABSOLUTE_TOLERANCE = 5e-9


def assert_point(point: Point, x: float, y: float) -> None:
    # A full source -> physical -> source chain composes and inverts four affine matrices.
    # Near zero, pytest's default 1e-12 absolute tolerance is tighter than the engine's
    # coordinate epsilon and ordinary double-precision cancellation at the supported extremes.
    assert point.x == pytest.approx(x, abs=POINT_ABSOLUTE_TOLERANCE)
    assert point.y == pytest.approx(y, abs=POINT_ABSOLUTE_TOLERANCE)


def assert_rect(rect: Rect, x: float, y: float, width: float, height: float) -> None:
    assert rect.x == pytest.approx(x)
    assert rect.y == pytest.approx(y)
    assert rect.width == pytest.approx(width)
    assert rect.height == pytest.approx(height)


def transform(mode: CropMode, *, working: PixelSize = DEFAULT_WORKING) -> CanonicalTransform:
    return CanonicalTransform.from_crop_config(
        original_size=PixelSize(width=100, height=50),
        normalized_size=PixelSize(width=100, height=50),
        exif_orientation=1,
        crop=CropConfig(mode=mode),
        working_size=working,
        canvas_size=MillimetreSize(width=200, height=100),
    )


@pytest.mark.parametrize(
    ("mode", "content_bounds", "source_at_top_center", "uses_extension"),
    [
        (CropMode.CONTAIN, (0, 25, 100, 50), False, False),
        (CropMode.COVER, (-50, 0, 200, 100), True, False),
        (CropMode.STRETCH, (0, 0, 100, 100), True, False),
        (CropMode.EXTEND, (0, 25, 100, 50), False, True),
    ],
)
def test_all_fit_modes_have_explicit_content_and_extension_semantics(
    mode: CropMode,
    content_bounds: tuple[float, float, float, float],
    source_at_top_center: bool,
    uses_extension: bool,
) -> None:
    mapping = transform(mode)

    assert_rect(mapping.content_bounds_working, *content_bounds)
    assert mapping.working_point_has_source(Point(x=50, y=0)) is source_at_top_center
    assert mapping.uses_canvas_extension is uses_extension
    if mode in {CropMode.CONTAIN, CropMode.EXTEND}:
        assert_rect(mapping.visible_content_bounds_working, 0, 25, 100, 50)
    else:
        assert_rect(mapping.visible_content_bounds_working, 0, 0, 100, 100)


@pytest.mark.parametrize(
    ("orientation", "normalized", "corner", "mapped"),
    [
        (1, (9, 5), (0, 0), (0, 0)),
        (2, (9, 5), (0, 0), (9, 0)),
        (3, (9, 5), (0, 0), (9, 5)),
        (4, (9, 5), (0, 0), (0, 5)),
        (5, (5, 9), (9, 5), (5, 9)),
        (6, (5, 9), (0, 0), (5, 0)),
        (7, (5, 9), (0, 0), (5, 9)),
        (8, (5, 9), (0, 0), (0, 9)),
    ],
)
def test_all_exif_orientation_matrices_match_normalized_boundaries(
    orientation: int,
    normalized: tuple[int, int],
    corner: tuple[int, int],
    mapped: tuple[int, int],
) -> None:
    original = PixelSize(width=9, height=5)
    size = normalized_size_for_orientation(original, orientation)
    assert (size.width, size.height) == normalized
    assert_point(
        orientation_matrix(original, orientation).map_point(Point(x=corner[0], y=corner[1])),
        *mapped,
    )


@pytest.mark.parametrize("orientation", range(1, 9))
def test_orientation_matrix_matches_pillow_exif_transpose_for_every_pixel(
    orientation: int,
) -> None:
    source = Image.new("L", (3, 2))
    source.putdata(range(6))
    exif = source.getexif()
    exif[274] = orientation
    source.info["exif"] = exif.tobytes()
    normalized = ImageOps.exif_transpose(source)
    matrix = orientation_matrix(PixelSize(width=3, height=2), orientation)

    for y in range(source.height):
        for x in range(source.width):
            mapped = matrix.map_point(Point(x=x + 0.5, y=y + 0.5))
            assert normalized.getpixel((int(mapped.x), int(mapped.y))) == source.getpixel((x, y))


def test_fractional_crop_becomes_normalized_edge_coordinates() -> None:
    mapping = CanonicalTransform.from_crop_config(
        original_size=PixelSize(width=200, height=100),
        normalized_size=PixelSize(width=200, height=100),
        exif_orientation=1,
        crop=CropConfig(mode="stretch", x=0.25, y=0.1, width=0.5, height=0.8),
        working_size=PixelSize(width=1000, height=400),
        canvas_size=MillimetreSize(width=250, height=100),
    )

    assert_rect(mapping.crop_rect, 50, 10, 100, 80)
    assert_point(
        mapping.map_point(
            Point(x=50, y=10),
            source=CoordinateStage.NORMALIZED,
            target=CoordinateStage.CROP,
        ),
        0,
        0,
    )
    assert_point(
        mapping.map_point(
            Point(x=150, y=90),
            source=CoordinateStage.NORMALIZED,
            target=CoordinateStage.WORKING,
        ),
        1000,
        400,
    )


@pytest.mark.parametrize("mode", tuple(CropMode))
def test_every_stage_round_trips_through_composed_inverse(mode: CropMode) -> None:
    mapping = transform(mode, working=PixelSize(width=731, height=413))
    stages = tuple(CoordinateStage)
    points = {
        CoordinateStage.ORIGINAL: Point(x=31.25, y=17.75),
        CoordinateStage.NORMALIZED: Point(x=31.25, y=17.75),
        CoordinateStage.CROP: Point(x=31.25, y=17.75),
        CoordinateStage.WORKING: Point(x=231.5, y=117.5),
        CoordinateStage.MILLIMETRES: Point(x=83.2, y=41.7),
    }
    for source in stages:
        for target in stages:
            forward = mapping.map_point(points[source], source=source, target=target)
            restored = mapping.map_point(forward, source=target, target=source)
            assert_point(restored, points[source].x, points[source].y)


def test_non_square_working_output_maps_edges_and_pixel_centers_to_millimetres() -> None:
    mapping = CanonicalTransform.from_crop_config(
        original_size=PixelSize(width=1200, height=600),
        normalized_size=PixelSize(width=1200, height=600),
        exif_orientation=1,
        crop=CropConfig(mode="stretch"),
        working_size=PixelSize(width=800, height=400),
        canvas_size=MillimetreSize(width=200, height=100),
    )

    assert_point(
        mapping.map_point(
            Point(x=0, y=0),
            source=CoordinateStage.WORKING,
            target=CoordinateStage.MILLIMETRES,
        ),
        0,
        0,
    )
    assert_point(
        mapping.map_point(
            Point(x=800, y=400),
            source=CoordinateStage.WORKING,
            target=CoordinateStage.MILLIMETRES,
        ),
        200,
        100,
    )
    assert_point(
        mapping.map_pixel_center(
            0,
            0,
            source=CoordinateStage.WORKING,
            target=CoordinateStage.MILLIMETRES,
        ),
        0.125,
        0.125,
    )
    assert_rect(mapping.working_pixel_bounds_mm(799, 399), 199.75, 99.75, 0.25, 0.25)


def test_original_orientation_crop_working_and_mm_share_one_composed_mapping() -> None:
    mapping = CanonicalTransform.from_crop_config(
        original_size=PixelSize(width=9, height=5),
        normalized_size=PixelSize(width=5, height=9),
        exif_orientation=6,
        crop=CropConfig(mode="stretch", x=0, y=0, width=1, height=1),
        working_size=PixelSize(width=500, height=900),
        canvas_size=MillimetreSize(width=50, height=90),
    )

    assert_point(
        mapping.map_point(
            Point(x=0, y=0),
            source=CoordinateStage.ORIGINAL,
            target=CoordinateStage.MILLIMETRES,
        ),
        50,
        0,
    )
    assert_point(
        mapping.map_point(
            Point(x=9, y=5),
            source=CoordinateStage.ORIGINAL,
            target=CoordinateStage.MILLIMETRES,
        ),
        0,
        90,
    )


def test_transform_builds_directly_from_ingestion_metadata() -> None:
    source = Image.new("RGB", (9, 5), (20, 40, 60))
    exif = Image.Exif()
    exif[274] = 6
    encoded = io.BytesIO()
    source.save(encoded, format="JPEG", exif=exif)
    ingested = ingest_image(encoded.getvalue(), filename="rotated.jpg")

    mapping = CanonicalTransform.from_source_metadata(
        source=ingested.metadata,
        crop=CropConfig(mode="stretch"),
        working_size=PixelSize(width=500, height=900),
        canvas_size=MillimetreSize(width=50, height=90),
    )

    assert mapping.original_size == PixelSize(width=9, height=5)
    assert mapping.normalized_size == PixelSize(width=5, height=9)
    assert mapping.exif_orientation == 6


def test_aspect_lock_updates_the_other_dimension_or_leaves_it_fixed() -> None:
    canvas = MillimetreSize(width=200, height=100)

    assert resize_canvas(
        canvas, axis=DimensionAxis.WIDTH, value_mm=300, lock_aspect=True
    ) == MillimetreSize(width=300, height=150)
    assert resize_canvas(
        canvas, axis=DimensionAxis.HEIGHT, value_mm=80, lock_aspect=True
    ) == MillimetreSize(width=160, height=80)
    assert resize_canvas(
        canvas, axis=DimensionAxis.WIDTH, value_mm=300, lock_aspect=False
    ) == MillimetreSize(width=300, height=100)
    assert resize_canvas(
        canvas, axis=DimensionAxis.HEIGHT, value_mm=80, lock_aspect=False
    ) == MillimetreSize(width=200, height=80)
    with pytest.raises(ValueError, match="positive finite"):
        resize_canvas(canvas, axis=DimensionAxis.WIDTH, value_mm=float("nan"), lock_aspect=True)


def test_transform_contract_serializes_without_derived_matrix_drift() -> None:
    mapping = transform(CropMode.COVER)
    restored = CanonicalTransform.model_validate_json(mapping.model_dump_json())

    assert restored == mapping
    assert restored.schema_version == 1
    assert restored.matrix(CoordinateStage.ORIGINAL, CoordinateStage.MILLIMETRES) == mapping.matrix(
        CoordinateStage.ORIGINAL, CoordinateStage.MILLIMETRES
    )


def test_transform_rejects_inconsistent_dimensions_crop_and_invalid_pixel_access() -> None:
    with pytest.raises(ValidationError, match="normalized dimensions"):
        CanonicalTransform(
            original_size=PixelSize(width=9, height=5),
            normalized_size=PixelSize(width=9, height=5),
            exif_orientation=6,
            crop_rect=Rect(x=0, y=0, width=9, height=5),
            fit_mode="contain",
            working_size=PixelSize(width=90, height=50),
            canvas_size=MillimetreSize(width=90, height=50),
        )

    with pytest.raises(ValidationError, match="crop rectangle"):
        CanonicalTransform(
            original_size=PixelSize(width=9, height=5),
            normalized_size=PixelSize(width=9, height=5),
            exif_orientation=1,
            crop_rect=Rect(x=8, y=0, width=2, height=5),
            fit_mode="contain",
            working_size=PixelSize(width=90, height=50),
            canvas_size=MillimetreSize(width=90, height=50),
        )

    mapping = transform(CropMode.CONTAIN)
    with pytest.raises(ValueError, match="outside"):
        mapping.map_pixel_center(
            100,
            0,
            source=CoordinateStage.WORKING,
            target=CoordinateStage.MILLIMETRES,
        )
    with pytest.raises(ValueError, match="raster stages"):
        mapping.map_pixel_center(
            0,
            0,
            source=CoordinateStage.CROP,
            target=CoordinateStage.WORKING,
        )
    with pytest.raises(ValueError, match="integers"):
        mapping.map_pixel_center(
            0.5,  # type: ignore[arg-type]
            0,
            source=CoordinateStage.WORKING,
            target=CoordinateStage.MILLIMETRES,
        )


def test_affine_composition_and_inverse_are_explicit() -> None:
    composed = Affine2D.translate(10, -4).then(Affine2D.scale(2, 3))
    mapped = composed.map_point(Point(x=2, y=5))
    assert_point(mapped, 24, 3)
    assert_point(composed.inverse().map_point(mapped), 2, 5)
    with pytest.raises(ValueError, match="singular"):
        Affine2D(m00=1, m01=2, m10=2, m11=4).inverse()


@settings(max_examples=120, deadline=None)
@given(
    width=st.integers(min_value=1, max_value=10_000),
    height=st.integers(min_value=1, max_value=10_000),
    orientation=st.integers(min_value=1, max_value=8),
    mode=st.sampled_from(tuple(CropMode)),
    working_width=st.integers(min_value=1, max_value=2_000),
    working_height=st.integers(min_value=1, max_value=2_000),
    canvas_width=st.integers(min_value=1, max_value=1_000),
    canvas_height=st.integers(min_value=1, max_value=1_000),
    x_fraction=st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
    y_fraction=st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
)
def test_generated_source_points_round_trip_across_the_full_physical_chain(
    width: int,
    height: int,
    orientation: int,
    mode: CropMode,
    working_width: int,
    working_height: int,
    canvas_width: int,
    canvas_height: int,
    x_fraction: float,
    y_fraction: float,
) -> None:
    original = PixelSize(width=width, height=height)
    mapping = CanonicalTransform.from_crop_config(
        original_size=original,
        normalized_size=normalized_size_for_orientation(original, orientation),
        exif_orientation=orientation,
        crop=CropConfig(mode=mode),
        working_size=PixelSize(width=working_width, height=working_height),
        canvas_size=MillimetreSize(width=canvas_width, height=canvas_height),
    )
    point = Point(x=x_fraction * width, y=y_fraction * height)

    physical = mapping.map_point(
        point,
        source=CoordinateStage.ORIGINAL,
        target=CoordinateStage.MILLIMETRES,
    )
    restored = mapping.map_point(
        physical,
        source=CoordinateStage.MILLIMETRES,
        target=CoordinateStage.ORIGINAL,
    )

    assert_point(restored, point.x, point.y)
