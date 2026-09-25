from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from image23mf.engine.contours import (
    AppliedContourCleanup,
    ContourCleanupRecord,
    ContourCleanupRequest,
    ContourOperation,
    ContourOperationKind,
    apply_contour_cleanup,
    physical_kernel_offsets,
)
from image23mf.engine.labels import LabelField
from image23mf.engine.regions import analyze_regions


def _source(
    rows: list[list[int]],
    *,
    labels: tuple[int, ...] = (0, 1),
    width_mm: float | None = None,
    height_mm: float | None = None,
    active: bytes | None = None,
):
    pixels = np.asarray(rows, dtype=np.uint8)
    field = LabelField(
        width=pixels.shape[1],
        height=pixels.shape[0],
        label_values=labels,
        pixels=pixels.tobytes(),
    )
    palette = {0: "#FFFFFF", 1: "#000000", 2: "#FF6600"}
    analysis = analyze_regions(
        field,
        colors={label: palette[label] for label in labels},
        width_mm=width_mm or field.width,
        height_mm=height_mm or field.height,
        active=active,
    )
    return field, analysis


def _operation(kind: str, **changes) -> ContourOperation:
    values = {
        "kind": kind,
        "radius_mm": 1.0,
    }
    if kind == "open":
        values.update({"subject_label": 1, "replacement_label": 0})
    elif kind == "close":
        values.update({"subject_label": 1, "editable_labels": (0,)})
    else:
        values["editable_labels"] = (0, 1)
        values["minimum_majority_ratio"] = 0.6
    values.update(changes)
    return ContourOperation(**values)


def _apply(field, analysis, *operations: ContourOperation) -> AppliedContourCleanup:
    return apply_contour_cleanup(
        field,
        analysis,
        ContourCleanupRequest(operations=operations),
    )


def _array(result: AppliedContourCleanup) -> np.ndarray:
    return np.frombuffer(result.labels.pixels, dtype=np.uint8).reshape(
        (result.labels.height, result.labels.width)
    )


def test_physical_kernel_honors_anisotropic_pixels_and_real_cell_centres() -> None:
    offsets = physical_kernel_offsets(
        0.75,
        pixel_width_mm=0.5,
        pixel_height_mm=1.0,
    )

    assert offsets == ((0, -1), (0, 0), (0, 1))
    assert physical_kernel_offsets(
        0.49,
        pixel_width_mm=0.5,
        pixel_height_mm=1.0,
    ) == ((0, 0),)
    with pytest.raises(ValueError, match="too many raster samples"):
        physical_kernel_offsets(25, pixel_width_mm=0.01, pixel_height_mm=0.01)


def test_open_removes_a_compact_source_without_hidden_changes_to_protected_pixels() -> None:
    rows = np.zeros((7, 7), dtype=np.uint8)
    rows[3, 3] = 1
    rows[0, 0] = 2
    field, analysis = _source(rows.tolist(), labels=(0, 1, 2))
    result = _apply(
        field,
        analysis,
        _operation("open", subject_label=1, replacement_label=0),
    )

    assert _array(result)[3, 3] == 0
    assert _array(result)[0, 0] == 2
    assert result.record.changed_pixel_count == 1
    assert result.record.operation_changed_pixel_count == 1
    assert result.record.changed_area_mm2 == 1
    assert [item.model_dump() for item in result.record.transitions] == [
        {
            "source_label": 1,
            "target_label": 0,
            "pixel_count": 1,
            "area_mm2": 1.0,
        }
    ]
    assert sum(result.changed_mask) == 1
    areas = {item.label: item for item in result.record.label_areas}
    assert areas[0].delta_pixel_count == 1
    assert areas[1].delta_pixel_count == -1
    assert areas[2].delta_pixel_count == 0


def test_close_fills_only_the_explicitly_editable_hole_source() -> None:
    rows = np.zeros((9, 9), dtype=np.uint8)
    rows[2:7, 2:7] = 1
    rows[4, 4] = 0
    rows[0, 0] = 2
    field, analysis = _source(rows.tolist(), labels=(0, 1, 2))
    result = _apply(
        field,
        analysis,
        _operation("close", subject_label=1, editable_labels=(0,)),
    )

    assert _array(result)[4, 4] == 1
    assert _array(result)[0, 0] == 2
    assert result.record.changed_pixel_count == 1
    assert result.record.transitions[0].source_label == 0
    assert result.record.transitions[0].target_label == 1


def test_majority_threshold_is_explicit_inclusive_and_supports_multiple_passes() -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[2, 2] = 1
    field, analysis = _source(rows.tolist())
    equal = _apply(
        field,
        analysis,
        _operation(
            "majority",
            editable_labels=(1,),
            minimum_majority_ratio=0.8,
            iterations=2,
        ),
    )
    above = _apply(
        field,
        analysis,
        _operation(
            "majority",
            editable_labels=(1,),
            minimum_majority_ratio=0.81,
        ),
    )

    assert equal.record.changed_pixel_count == 1
    assert above.record.changed_pixel_count == 0
    assert equal.record.steps[0].operation.iterations == 2


def test_boundary_simplification_is_bounded_to_existing_boundaries() -> None:
    rows = np.zeros((9, 9), dtype=np.uint8)
    rows[2:7, 2:7] = 1
    rows[1, 1] = 1
    field, analysis = _source(rows.tolist())
    result = _apply(
        field,
        analysis,
        _operation(
            "boundary_simplify",
            radius_mm=1.5,
            editable_labels=(1,),
            minimum_majority_ratio=0.55,
        ),
    )

    assert result.record.changed_pixel_count == 1
    assert _array(result)[1, 1] == 0
    assert np.all(_array(result)[2:7, 2:7] == 1)


@pytest.mark.parametrize("kind", ["open", "close"])
def test_binary_physical_operations_are_idempotent_on_stable_fixture(kind: str) -> None:
    rows = np.zeros((11, 11), dtype=np.uint8)
    rows[2:9, 2:9] = 1
    rows[5, 5] = 0
    rows[1, 5] = 1
    field, analysis = _source(rows.tolist())
    operation = _operation(kind)
    first = _apply(field, analysis, operation)
    second = _apply(first.labels, first.analysis, operation)

    assert second.labels.pixels == first.labels.pixels
    assert second.record.changed_pixel_count == 0


@pytest.mark.parametrize("kind", ["open", "close"])
def test_binary_physical_operations_are_idempotent_across_random_label_fields(
    kind: str,
) -> None:
    random = np.random.default_rng(24)
    for _case in range(30):
        rows = (random.random((9, 9)) > 0.5).astype(np.uint8)
        field, analysis = _source(rows.tolist())
        operation = _operation(kind)
        first = _apply(field, analysis, operation)
        second = _apply(first.labels, first.analysis, operation)

        assert second.labels.pixels == first.labels.pixels
        assert second.record.changed_pixel_count == 0


def test_operation_order_is_preserved_and_can_change_the_exact_result() -> None:
    rows = [
        [1, 1, 0, 1, 0, 0, 1],
        [0, 1, 0, 1, 1, 0, 1],
        [0, 0, 0, 0, 0, 0, 1],
        [0, 0, 1, 1, 1, 1, 0],
        [0, 1, 1, 0, 1, 1, 1],
        [1, 0, 1, 0, 0, 1, 1],
        [1, 0, 1, 1, 1, 1, 0],
    ]
    field, analysis = _source(rows)
    opened_then_closed = _apply(
        field,
        analysis,
        _operation("open"),
        _operation("close"),
    )
    closed_then_opened = _apply(
        field,
        analysis,
        _operation("close"),
        _operation("open"),
    )

    assert opened_then_closed.labels.pixels != closed_then_opened.labels.pixels
    assert [step.operation.kind for step in opened_then_closed.record.steps] == [
        ContourOperationKind.OPEN,
        ContourOperationKind.CLOSE,
    ]
    assert opened_then_closed.record.operation_changed_pixel_count == sum(
        step.changed_pixel_count for step in opened_then_closed.record.steps
    )


def test_transparency_plane_and_inactive_label_bytes_are_never_modified() -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[2, 2] = 1
    rows[0, 0] = 1
    active = np.ones(rows.shape, dtype=np.uint8)
    active[0, 0] = 0
    field, analysis = _source(rows.tolist(), active=active.tobytes())
    result = _apply(
        field,
        analysis,
        _operation("open", subject_label=1, replacement_label=0),
    )

    assert result.active == active.tobytes()
    assert _array(result)[0, 0] == 1
    assert _array(result)[2, 2] == 0
    assert np.frombuffer(result.changed_mask, dtype=np.uint8)[0] == 0


def test_record_is_deterministic_reconstructable_and_carries_exact_lineage() -> None:
    rows = np.zeros((7, 7), dtype=np.uint8)
    rows[3, 3] = 1
    field, analysis = _source(rows.tolist())
    request = ContourCleanupRequest(
        operations=(_operation("open", subject_label=1, replacement_label=0),)
    )
    first = apply_contour_cleanup(field, analysis, request)
    second = apply_contour_cleanup(field, analysis, request)

    assert first.record == second.record
    assert first.record.fingerprint() == second.record.fingerprint()
    assert ContourCleanupRecord.model_validate_json(first.record.canonical_json()) == first.record
    assert first.record.before_graph_fingerprint == analysis.graph.fingerprint()
    assert first.record.after_graph_fingerprint == first.analysis.graph.fingerprint()
    assert first.record.lineage.events


def test_invalid_or_mismatched_requests_fail_before_mutating_the_source() -> None:
    field, analysis = _source([[0, 0], [0, 1]])
    original = field.pixels

    with pytest.raises(ValidationError, match="canonically sorted"):
        _operation("majority", editable_labels=(1, 0))
    with pytest.raises(ValidationError, match="requires an explicit majority ratio"):
        ContourOperation(kind="majority", radius_mm=1, editable_labels=(0,))
    with pytest.raises(ValidationError, match="does not accept a majority ratio"):
        ContourOperation(
            kind="open",
            radius_mm=1,
            subject_label=1,
            replacement_label=0,
            minimum_majority_ratio=0.75,
        )
    with pytest.raises(ValidationError, match="one physical-radius pass"):
        ContourOperation(
            kind="close",
            radius_mm=1,
            subject_label=1,
            editable_labels=(0,),
            iterations=2,
        )
    with pytest.raises(ValueError, match="absent from the palette"):
        _apply(field, analysis, _operation("open", subject_label=2, replacement_label=0))

    assert field.pixels == original
