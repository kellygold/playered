from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from image23mf.contracts.editor import (
    CanvasSelectionCommand,
    CanvasSelectionSelector,
    CanvasSelectionState,
    SelectionCombineMode,
    SourceBrushSelection,
    SourceLassoSelection,
    SourceRectangleSelection,
    SourceRegionSelection,
    SourceSelectionPoint,
)
from image23mf.contracts.job import CropConfig, CropMode
from image23mf.editor.replay import command_to_storage_payload, replay_editor_commands
from image23mf.editor.selection import rasterize_canvas_selection
from image23mf.engine.labels import LabelField
from image23mf.engine.regions import analyze_regions
from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize

SHA = "a" * 64
REGION = "region_" + "1" * 24


def _transform(
    *,
    crop: CropConfig | None = None,
    working: PixelSize | None = None,
    canvas: MillimetreSize | None = None,
) -> CanonicalTransform:
    source = PixelSize(width=10, height=10)
    return CanonicalTransform.from_crop_config(
        original_size=source,
        normalized_size=source,
        exif_orientation=1,
        crop=crop or CropConfig(mode=CropMode.STRETCH),
        working_size=working or PixelSize(width=10, height=10),
        canvas_size=canvas or MillimetreSize(width=10, height=10),
    )


def _selection(*primitives, expand_mm: float = 0, feather_mm: float = 0):
    return CanvasSelectionState(
        source_width_px=10,
        source_height_px=10,
        primitives=primitives,
        expand_mm=expand_mm,
        feather_mm=feather_mm,
    )


def test_selection_contract_rejects_out_of_bounds_and_noncanonical_regions() -> None:
    with pytest.raises(ValidationError, match="normalized source bounds"):
        _selection(
            SourceRectangleSelection(
                primitive_id="selection_rectangle1",
                x=9,
                y=0,
                width=2,
                height=1,
            )
        )
    with pytest.raises(ValidationError, match="canonically sorted"):
        SourceRegionSelection(
            primitive_id="selection_regions1",
            region_ids=("region_" + "2" * 24, REGION),
        )


def test_rectangle_add_subtract_and_boundaries_are_deterministic() -> None:
    selection = _selection(
        SourceRectangleSelection(
            primitive_id="selection_rectangle1",
            x=2,
            y=2,
            width=6,
            height=6,
        ),
        SourceRectangleSelection(
            primitive_id="selection_rectangle2",
            combine=SelectionCombineMode.SUBTRACT,
            x=4,
            y=4,
            width=2,
            height=2,
        ),
    )
    first = rasterize_canvas_selection(selection, _transform())
    second = rasterize_canvas_selection(selection, _transform())

    assert first == second
    assert first.selected_pixel_count == 32
    assert first.fingerprint() == second.fingerprint()
    mask = [first.mask[index * 10 : (index + 1) * 10] for index in range(10)]
    assert mask[2][2] == 1
    assert mask[3][7] == 1
    assert mask[4][4] == 0
    assert mask[8][8] == 0


def test_canonical_source_selection_maps_after_crop_and_resize() -> None:
    selection = _selection(
        SourceRectangleSelection(
            primitive_id="selection_rectangle1",
            x=2,
            y=2,
            width=2,
            height=2,
        )
    )
    cropped = _transform(
        crop=CropConfig(mode=CropMode.STRETCH, x=0.2, y=0.2, width=0.5, height=0.5),
        working=PixelSize(width=20, height=20),
        canvas=MillimetreSize(width=100, height=50),
    )
    result = rasterize_canvas_selection(selection, cropped)

    assert result.width == 20
    assert result.height == 20
    assert result.selected_pixel_count == 64


def test_lasso_brush_expand_and_feather_cover_distinct_selection_paths() -> None:
    lasso = SourceLassoSelection(
        primitive_id="selection_lasso001",
        points=(
            SourceSelectionPoint(x=1, y=1),
            SourceSelectionPoint(x=5, y=1),
            SourceSelectionPoint(x=1, y=5),
        ),
    )
    brush = SourceBrushSelection(
        primitive_id="selection_brush001",
        points=(SourceSelectionPoint(x=7, y=2), SourceSelectionPoint(x=7, y=7)),
        radius_mm=0.75,
    )
    plain = rasterize_canvas_selection(_selection(lasso, brush), _transform())
    expanded = rasterize_canvas_selection(
        _selection(lasso, brush, expand_mm=1.1, feather_mm=2.1),
        _transform(),
    )

    assert plain.selected_pixel_count > 0
    assert expanded.selected_pixel_count > plain.selected_pixel_count
    assert any(0 < value < 255 for value in expanded.feather)


def test_exact_region_selection_requires_and_uses_bound_mask() -> None:
    selection = _selection(
        SourceRegionSelection(
            primitive_id="selection_regions1",
            region_ids=(REGION,),
        )
    )
    with pytest.raises(ValueError, match="region mask is missing"):
        rasterize_canvas_selection(selection, _transform())

    region_mask = bytes([1 if index in {0, 99} else 0 for index in range(100)])
    result = rasterize_canvas_selection(selection, _transform(), region_masks={REGION: region_mask})
    assert result.selected_pixel_count == 2


def test_selection_snapshot_round_trips_as_a_no_op_revision_command() -> None:
    labels = LabelField(width=2, height=2, label_values=(0,), pixels=bytes([0, 0, 0, 0]))
    active = bytes([1, 1, 1, 1])
    analysis = analyze_regions(labels, colors={0: "#000000"}, width_mm=2, height_mm=2)
    command = CanvasSelectionCommand(
        command_id="cmd_selection01",
        created_at=datetime.now(timezone.utc),
        selector=CanvasSelectionSelector(
            graph_fingerprint=analysis.graph.fingerprint(),
            config_fingerprint=SHA,
            selection=CanvasSelectionState(
                source_width_px=2,
                source_height_px=2,
                primitives=(
                    SourceRectangleSelection(
                        primitive_id="selection_rectangle1",
                        x=0,
                        y=0,
                        width=1,
                        height=1,
                    ),
                ),
            ),
        ),
    )
    payload = command_to_storage_payload(command)
    replay = replay_editor_commands(
        labels,
        active=active,
        colors={0: "#000000"},
        width_mm=2,
        height_mm=2,
        config_fingerprint=SHA,
        commands=(command,),
    )

    assert payload["operation_type"] == "canvas_selection_v1"
    assert payload["parameters"]["command"]["command_type"] == "canvas_selection"
    assert replay.labels == labels
    assert replay.record.command_count == 1
    assert replay.record.steps[0].changed_pixel_count == 0
