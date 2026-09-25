import hashlib

from image23mf.engine.automatic_cleanup import apply_automatic_cleanup
from image23mf.engine.islands import IslandAnalysisOptions, IslandMergePolicy
from image23mf.engine.labels import LabelField


def _dot_field() -> LabelField:
    return LabelField(
        width=5,
        height=5,
        label_values=(0, 1),
        pixels=bytes(
            [
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ]
        ),
    )


def _options(*, preserve_long_lines: bool = True) -> IslandAnalysisOptions:
    return IslandAnalysisOptions(
        minimum_area_mm2=5,
        minimum_equivalent_diameter_mm=0,
        preserve_long_lines=preserve_long_lines,
        long_line_minimum_length_mm=1.6,
    )


def _apply(policy: IslandMergePolicy, *, smoothing_radius_mm: float = 0):
    labels = _dot_field()
    return apply_automatic_cleanup(
        labels,
        active=bytes([1] * 25),
        colors={0: "#000000", 1: "#FFFFFF"},
        width_mm=5,
        height_mm=5,
        island_options=_options(),
        island_policy=policy,
        smoothing_radius_mm=smoothing_radius_mm,
    )


def test_review_is_byte_identical_and_records_the_decision() -> None:
    result = _apply(IslandMergePolicy.REVIEW)

    assert result.labels == _dot_field()
    assert result.record.changed_pixel_count == 0
    assert result.record.before_label_sha256 == result.record.after_label_sha256
    assert result.record.island_record is not None
    assert {item.status.value for item in result.record.island_record.decisions} == {
        "review_required"
    }


def test_dominant_policy_removes_the_isolated_dot_with_exact_mask() -> None:
    first = _apply(IslandMergePolicy.DOMINANT_NEIGHBOR)
    second = _apply(IslandMergePolicy.DOMINANT_NEIGHBOR)

    assert first.labels.pixels == bytes([0] * 25)
    assert first.record.changed_pixel_count == 1
    assert first.changed_mask[12] == 1
    assert sum(first.changed_mask) == 1
    assert first.record.changed_mask_sha256 == hashlib.sha256(first.changed_mask).hexdigest()
    assert first.record == second.record
    assert first.changed_mask == second.changed_mask


def test_nonzero_smoothing_is_deterministic_and_bounded() -> None:
    first = _apply(IslandMergePolicy.KEEP, smoothing_radius_mm=1)
    second = _apply(IslandMergePolicy.KEEP, smoothing_radius_mm=1)

    assert first.record.contour_record is not None
    assert first.record == second.record
    assert first.labels == second.labels
    assert first.record.changed_pixel_count <= 25


def test_anisotropic_pixels_preserve_a_long_line_exemption() -> None:
    labels = LabelField(
        width=8,
        height=3,
        label_values=(0, 1),
        pixels=bytes([0] * 8 + [0, 1, 1, 1, 1, 1, 1, 0] + [0] * 8),
    )
    result = apply_automatic_cleanup(
        labels,
        active=bytes([1] * 24),
        colors={0: "#000000", 1: "#FFFFFF"},
        width_mm=8,
        height_mm=1.5,
        island_options=IslandAnalysisOptions(
            minimum_area_mm2=10,
            minimum_equivalent_diameter_mm=0,
            preserve_long_lines=True,
            long_line_minimum_length_mm=4,
        ),
        island_policy=IslandMergePolicy.DOMINANT_NEIGHBOR,
        smoothing_radius_mm=0,
    )

    assert result.labels == labels
    assert result.initial_island_analysis.summary.long_line_exempt_count == 1
