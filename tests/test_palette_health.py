import pytest
from PIL import Image

from image23mf.engine import (
    PaletteHealthCode,
    PaletteHealthOptions,
    PaletteResolutionKind,
    PaletteResolutionRequest,
    analyze_palette_health,
    classify_palette,
    quantize_auto_palette,
    resolve_palette,
)


def test_monochrome_auto_fit_reports_collapse_duplicates_and_absent_slots() -> None:
    image = Image.new("RGB", (20, 10), "#456789")
    try:
        result = quantize_auto_palette(image, 4)
        first = analyze_palette_health(result)
        second = analyze_palette_health(result)
    finally:
        image.close()

    assert first == second
    assert first.fingerprint() == second.fingerprint()
    assert first.issue_codes() == (
        PaletteHealthCode.AUTO_PALETTE_COLLAPSE,
        PaletteHealthCode.DUPLICATE_COLORS,
        PaletteHealthCode.ABSENT_COLORS,
    )
    assert first.visible_pixel_count == 200
    assert first.assigned_color_count == 1
    assert first.unique_color_count == 1
    assert [color.pixel_count for color in first.colors] == [200, 0, 0, 0]
    assert first.issues[1].indices == (0, 1, 2, 3)
    assert {action.kind for action in first.issues[1].actions} == set(PaletteResolutionKind)


def test_repeated_saved_hex_uses_first_label_and_offers_enabled_resolution() -> None:
    image = Image.new("RGB", (4, 1), "#102030")
    try:
        result = classify_palette(image, ("#102030", "#102030", "#FFFFFF"))
        report = analyze_palette_health(result)
    finally:
        image.close()

    assert report.issue_codes() == (
        PaletteHealthCode.DUPLICATE_COLORS,
        PaletteHealthCode.ABSENT_COLORS,
    )
    duplicate = report.issues[0]
    merge, replace, keep = duplicate.actions
    assert duplicate.indices == (0, 1)
    assert (merge.enabled, merge.source_index, merge.target_index) == (True, 1, 0)
    assert replace.enabled and replace.requires_replacement_color
    assert keep.enabled and keep.kind == PaletteResolutionKind.CONTINUE


def test_near_equal_lab_colors_are_grouped_but_distinct_colors_are_ready() -> None:
    near_image = Image.new("RGB", (2, 1))
    near_image.putdata(((20, 40, 60), (21, 41, 61)))
    distinct_image = Image.new("RGB", (3, 1))
    distinct_image.putdata(((0, 0, 0), (255, 255, 255), (255, 0, 0)))
    try:
        near = analyze_palette_health(
            classify_palette(near_image, ("#14283C", "#15293D")),
            options=PaletteHealthOptions(minimum_delta_e=3),
        )
        distinct = analyze_palette_health(
            classify_palette(distinct_image, ("#000000", "#FFFFFF", "#FF0000"))
        )
    finally:
        near_image.close()
        distinct_image.close()

    assert PaletteHealthCode.NEAR_IDENTICAL_COLORS in near.issue_codes()
    issue = next(
        item for item in near.issues if item.code == PaletteHealthCode.NEAR_IDENTICAL_COLORS
    )
    assert issue.indices == (0, 1)
    assert issue.minimum_delta_e is not None and 0 < issue.minimum_delta_e < 3
    assert distinct.needs_review is False
    assert distinct.assigned_color_count == 3


def test_all_locked_duplicate_colors_report_impossible_resolution_without_crashing() -> None:
    image = Image.new("RGB", (2, 1), "#111111")
    try:
        result = quantize_auto_palette(
            image,
            3,
            locked_colors={0: "#111111", 1: "#111111", 2: "#EEEEEE"},
        )
        report = analyze_palette_health(result, locked_indices=(0, 1, 2))
    finally:
        image.close()

    assert PaletteHealthCode.LOCKED_CONFLICT in report.issue_codes()
    duplicate = next(
        issue for issue in report.issues if issue.code == PaletteHealthCode.DUPLICATE_COLORS
    )
    assert duplicate.locked_indices == (0, 1)
    assert duplicate.actions[0].enabled is False
    assert duplicate.actions[1].enabled is False
    assert duplicate.actions[2].enabled is True


def test_alpha_threshold_excludes_hidden_and_faint_assignments() -> None:
    image = Image.new("RGBA", (3, 1))
    image.putdata(((255, 0, 0, 255), (0, 0, 255, 10), (0, 255, 0, 0)))
    try:
        result = classify_palette(image, ("#FF0000", "#0000FF", "#00FF00"))
        report = analyze_palette_health(
            result,
            options=PaletteHealthOptions(alpha_threshold=10),
        )
    finally:
        image.close()

    assert report.visible_pixel_count == 1
    assert [color.pixel_count for color in report.colors] == [1, 0, 0]
    absent = next(issue for issue in report.issues if issue.code == PaletteHealthCode.ABSENT_COLORS)
    assert absent.indices == (1, 2)


def test_merge_returns_complete_index_map_and_remaps_locks() -> None:
    resolved = resolve_palette(
        ("#000000", "#111111", "#222222", "#FFFFFF"),
        PaletteResolutionRequest(
            kind=PaletteResolutionKind.MERGE,
            source_index=1,
            target_index=2,
        ),
        locked_indices=(0, 2),
    )

    assert resolved.palette == ("#000000", "#222222", "#FFFFFF")
    assert resolved.old_to_new_indices == (0, 1, 1, 2)
    assert resolved.locked_indices == (0, 1)


def test_replace_and_continue_are_explicit_and_leave_input_immutable() -> None:
    palette = ("#000000", "#111111", "#FFFFFF")
    replaced = resolve_palette(
        palette,
        PaletteResolutionRequest(
            kind=PaletteResolutionKind.REPLACE,
            source_index=1,
            replacement_color="#f99963",
        ),
    )
    continued = resolve_palette(
        palette,
        PaletteResolutionRequest(kind=PaletteResolutionKind.CONTINUE),
        locked_indices=(0,),
    )

    assert palette == ("#000000", "#111111", "#FFFFFF")
    assert replaced.palette == ("#000000", "#F99963", "#FFFFFF")
    assert replaced.old_to_new_indices == (0, 1, 2)
    assert continued.palette == palette
    assert continued.locked_indices == (0,)


@pytest.mark.parametrize(
    "resolution, locked, message",
    [
        (
            PaletteResolutionRequest(
                kind=PaletteResolutionKind.MERGE,
                source_index=0,
                target_index=1,
            ),
            (0,),
            "locked",
        ),
        (
            PaletteResolutionRequest(
                kind=PaletteResolutionKind.MERGE,
                source_index=0,
                target_index=1,
            ),
            (),
            "below two",
        ),
        (
            PaletteResolutionRequest(
                kind=PaletteResolutionKind.REPLACE,
                source_index=0,
            ),
            (),
            "replacement",
        ),
        (
            PaletteResolutionRequest(
                kind=PaletteResolutionKind.CONTINUE,
                source_index=0,
            ),
            (),
            "does not accept",
        ),
    ],
)
def test_invalid_resolutions_are_rejected(resolution, locked, message) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_palette(("#000000", "#FFFFFF"), resolution, locked_indices=locked)
