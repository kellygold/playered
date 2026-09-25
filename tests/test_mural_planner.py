from typing import Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from image23mf.contracts.job import CropConfig
from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize
from image23mf.mural import (
    BedEnvelope,
    BedRectangle,
    MuralLayout,
    MuralPlan,
    MuralPlanRequest,
    MuralSourceProvenance,
    OrientationPreference,
    PlanFreshnessReason,
    assess_plan_freshness,
    build_mural_plan,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def source(
    *,
    width: int = 1200,
    height: int = 800,
    artifact_sha256: str = HASH_B,
    config_sha256: str = HASH_C,
    engine_version: str = "0.1.0",
    revision_id: Optional[str] = "revision_wager",
    draft_generation: Optional[int] = None,
) -> MuralSourceProvenance:
    size = PixelSize(width=width, height=height)
    transform = CanonicalTransform.from_crop_config(
        original_size=size,
        normalized_size=size,
        exif_orientation=1,
        crop=CropConfig(mode="stretch"),
        working_size=size,
        canvas_size=MillimetreSize(width=200, height=200),
    )
    return MuralSourceProvenance(
        source_asset_id="asset_wager",
        source_asset_sha256=HASH_A,
        processed_artifact_id="artifact_wager_master",
        processed_artifact_sha256=artifact_sha256,
        processed_size=size,
        canonical_transform=transform,
        config_sha256=config_sha256,
        engine_version=engine_version,
        revision_id=revision_id,
        draft_generation=draft_generation,
    )


def bed(
    *,
    width: float = 256,
    height: float = 256,
    clearance: float = 0,
    exclusions: tuple[BedRectangle, ...] = (),
) -> BedEnvelope:
    return BedEnvelope(
        printer_id="bambu-p2s",
        plate_id="textured-pei",
        profile_catalog_fingerprint=HASH_D,
        width_mm=width,
        height_mm=height,
        edge_clearance_mm=clearance,
        excluded_rectangles=exclusions,
    )


def request(
    layout: MuralLayout,
    *,
    master: Optional[MuralSourceProvenance] = None,
    printer_bed: Optional[BedEnvelope] = None,
    reservations: tuple[BedRectangle, ...] = (),
) -> MuralPlanRequest:
    return MuralPlanRequest(
        source=master or source(),
        layout=layout,
        bed=printer_bed or bed(),
        reserved_rectangles=reservations,
    )


def test_wager_3x2_is_one_master_with_six_unique_row_major_tiles() -> None:
    result = build_mural_plan(
        request(
            MuralLayout(
                rows=2,
                columns=3,
                panel_width_mm=200,
                panel_height_mm=200,
                horizontal_gap_mm=8,
                vertical_gap_mm=12,
            )
        )
    )

    assert result.master_size_mm == MillimetreSize(width=600, height=400)
    assert result.assembled_size_mm == MillimetreSize(width=616, height=412)
    assert result.master_transform.canvas_size == result.master_size_mm
    assert [tile.id for tile in result.tiles] == [
        "tile-r01-c01",
        "tile-r01-c02",
        "tile-r01-c03",
        "tile-r02-c01",
        "tile-r02-c02",
        "tile-r02-c03",
    ]
    assert [tile.build_plate_index for tile in result.tiles] == list(range(1, 7))
    assert len({tile.master_pixel_bounds for tile in result.tiles}) == 6
    assert result.tiles[0].master_bounds_mm.model_dump() == {
        "x": 0.0,
        "y": 0.0,
        "width": 200.0,
        "height": 200.0,
    }
    assert result.tiles[-1].master_bounds_mm.model_dump() == {
        "x": 400.0,
        "y": 200.0,
        "width": 200.0,
        "height": 200.0,
    }
    assert result.all_tiles_fit is True


def test_non_divisible_raster_uses_shared_integer_boundaries_without_loss() -> None:
    result = build_mural_plan(
        request(
            MuralLayout(rows=2, columns=3, panel_width_mm=190, panel_height_mm=170),
            master=source(width=1201, height=799),
        )
    )

    first_row = result.tiles[:3]
    assert [
        (tile.master_pixel_bounds.x_start, tile.master_pixel_bounds.x_end) for tile in first_row
    ] == [(0, 400), (400, 800), (800, 1201)]
    assert [
        (
            result.tiles[index].master_pixel_bounds.y_start,
            result.tiles[index].master_pixel_bounds.y_end,
        )
        for index in (0, 3)
    ] == [(0, 399), (399, 799)]
    assert (
        sum(
            tile.master_pixel_bounds.width * tile.master_pixel_bounds.height
            for tile in result.tiles
        )
        == 1201 * 799
    )


def test_bleed_expands_sampling_but_does_not_move_nominal_shared_edges() -> None:
    result = build_mural_plan(
        request(
            MuralLayout(
                rows=1,
                columns=2,
                panel_width_mm=100,
                panel_height_mm=80,
                bleed_mm=3,
            )
        )
    )
    left, right = result.tiles

    assert left.master_bounds_mm.right == right.master_bounds_mm.x == 100
    assert left.sampled_master_bounds_mm.model_dump() == {
        "x": 0.0,
        "y": 0.0,
        "width": 103.0,
        "height": 80.0,
    }
    assert right.sampled_master_bounds_mm.model_dump() == {
        "x": 97.0,
        "y": 0.0,
        "width": 103.0,
        "height": 80.0,
    }
    assert left.output_size_mm == right.output_size_mm == MillimetreSize(width=106, height=86)
    assert left.outside_master_padding.left_mm == 3
    assert left.outside_master_padding.top_mm == 3
    assert left.outside_master_padding.bottom_mm == 3
    assert left.outside_master_padding.right_mm == 0
    assert right.outside_master_padding.right_mm == 3


def test_auto_orientation_rotates_only_when_native_output_does_not_fit() -> None:
    result = build_mural_plan(
        request(
            MuralLayout(
                rows=1,
                columns=1,
                panel_width_mm=235,
                panel_height_mm=195,
                bleed_mm=2.5,
                orientation=OrientationPreference.AUTO,
            ),
            printer_bed=bed(width=220, height=250),
        )
    )

    assert result.all_tiles_fit is True
    assert result.tiles[0].bed_fit.rotation_degrees == 90
    assert result.tiles[0].bed_fit.placed_width_mm == 200
    assert result.tiles[0].bed_fit.placed_height_mm == 240


def test_explicit_native_orientation_fails_with_actionable_fit_reason() -> None:
    result = build_mural_plan(
        request(
            MuralLayout(
                rows=1,
                columns=1,
                panel_width_mm=235,
                panel_height_mm=195,
                bleed_mm=2.5,
                orientation=OrientationPreference.NATIVE,
            ),
            printer_bed=bed(width=220, height=250),
        )
    )

    assert result.all_tiles_fit is False
    assert result.tiles[0].bed_fit.origin_x_mm is None
    assert "reduce panel size or bleed" in result.warnings[0]


def test_fit_finds_deterministic_origin_around_reserved_bed_area() -> None:
    result = build_mural_plan(
        request(
            MuralLayout(rows=1, columns=1, panel_width_mm=120, panel_height_mm=100),
            printer_bed=bed(width=256, height=256, clearance=3),
            reservations=(BedRectangle(x_mm=0, y_mm=0, width_mm=130, height_mm=130),),
        )
    )

    assert result.all_tiles_fit is True
    assert result.tiles[0].bed_fit.origin_x_mm == 133
    assert result.tiles[0].bed_fit.origin_y_mm == 3


def test_profile_exclusions_and_user_reservations_remain_distinct_and_both_block_fit() -> None:
    profile_exclusion = BedRectangle(x_mm=0, y_mm=0, width_mm=100, height_mm=256)
    reservation = BedRectangle(x_mm=100, y_mm=0, width_mm=156, height_mm=100)
    plan_request = request(
        MuralLayout(rows=1, columns=1, panel_width_mm=120, panel_height_mm=120),
        printer_bed=bed(width=256, height=256, exclusions=(profile_exclusion,)),
        reservations=(reservation,),
    )

    result = build_mural_plan(plan_request)

    assert plan_request.bed.excluded_rectangles == (profile_exclusion,)
    assert plan_request.reserved_rectangles == (reservation,)
    assert result.all_tiles_fit is True
    assert result.tiles[0].bed_fit.origin_x_mm == 100
    assert result.tiles[0].bed_fit.origin_y_mm == 100


def test_reservation_must_stay_inside_bed() -> None:
    with pytest.raises(ValidationError, match="reserved rectangle must remain"):
        request(
            MuralLayout(rows=1, columns=1, panel_width_mm=100, panel_height_mm=100),
            reservations=(BedRectangle(x_mm=240, y_mm=0, width_mm=20, height_mm=20),),
        )


def test_freshness_explains_artifact_config_revision_and_engine_drift() -> None:
    saved = source()
    current = source(
        artifact_sha256=HASH_D,
        config_sha256=HASH_B,
        engine_version="0.2.0",
        revision_id=None,
        draft_generation=4,
    )

    freshness = assess_plan_freshness(saved, current)

    assert freshness.current is False
    assert freshness.reasons == (
        PlanFreshnessReason.PROCESSED_ARTIFACT_CHANGED,
        PlanFreshnessReason.CONFIG_CHANGED,
        PlanFreshnessReason.REVISION_CHANGED,
        PlanFreshnessReason.ENGINE_CHANGED,
    )
    assert assess_plan_freshness(saved, saved).current is True


def test_plan_round_trips_with_stable_fingerprint_and_forbidden_unknown_fields() -> None:
    planned = build_mural_plan(
        request(MuralLayout(rows=2, columns=3, panel_width_mm=200, panel_height_mm=200))
    )

    restored = MuralPlan.model_validate_json(planned.model_dump_json())

    assert restored == planned
    assert restored.request_fingerprint == planned.request_fingerprint
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MuralPlan.model_validate({**planned.model_dump(), "future_guess": True})


@settings(max_examples=80, deadline=None)
@given(
    rows=st.integers(min_value=1, max_value=12),
    columns=st.integers(min_value=1, max_value=12),
    extra_width=st.integers(min_value=0, max_value=31),
    extra_height=st.integers(min_value=0, max_value=31),
    panel_width=st.integers(min_value=10, max_value=200),
    panel_height=st.integers(min_value=10, max_value=200),
)
def test_arbitrary_grids_partition_non_square_master_exactly_once(
    rows: int,
    columns: int,
    extra_width: int,
    extra_height: int,
    panel_width: int,
    panel_height: int,
) -> None:
    width = columns + extra_width
    height = rows + extra_height
    planned = build_mural_plan(
        request(
            MuralLayout(
                rows=rows,
                columns=columns,
                panel_width_mm=panel_width,
                panel_height_mm=panel_height,
            ),
            master=source(width=width, height=height),
            printer_bed=bed(width=500, height=500),
        )
    )

    assert len(planned.tiles) == rows * columns
    assert len({tile.id for tile in planned.tiles}) == rows * columns
    assert [tile.build_plate_index for tile in planned.tiles] == list(range(1, rows * columns + 1))
    assert (
        sum(
            tile.master_pixel_bounds.width * tile.master_pixel_bounds.height
            for tile in planned.tiles
        )
        == width * height
    )
    for row in range(rows):
        row_tiles = planned.tiles[row * columns : (row + 1) * columns]
        assert row_tiles[0].master_pixel_bounds.x_start == 0
        assert row_tiles[-1].master_pixel_bounds.x_end == width
        for left, right in zip(row_tiles, row_tiles[1:]):
            assert left.master_pixel_bounds.x_end == right.master_pixel_bounds.x_start
            assert left.master_bounds_mm.right == right.master_bounds_mm.x
    for column in range(columns):
        column_tiles = planned.tiles[column::columns]
        assert column_tiles[0].master_pixel_bounds.y_start == 0
        assert column_tiles[-1].master_pixel_bounds.y_end == height
        for top, bottom in zip(column_tiles, column_tiles[1:]):
            assert top.master_pixel_bounds.y_end == bottom.master_pixel_bounds.y_start
            assert top.master_bounds_mm.bottom == bottom.master_bounds_mm.y


def test_grid_rejects_more_tiles_than_processed_pixels() -> None:
    with pytest.raises(ValidationError, match="columns cannot exceed"):
        request(
            MuralLayout(rows=1, columns=3, panel_width_mm=100, panel_height_mm=100),
            master=source(width=2, height=10),
        )
