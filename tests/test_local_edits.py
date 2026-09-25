from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import numpy as np
import pytest
from pydantic import ValidationError

from image23mf.contracts.editor import (
    CanvasSelectionSelector,
    CanvasSelectionState,
    LocalAffineEdit,
    LocalCloneEdit,
    LocalColorCleanupEdit,
    LocalFillEdit,
    LocalMorphologyEdit,
    LocalMorphologyKind,
    LocalRasterEditCommand,
    SourceRectangleSelection,
)
from image23mf.contracts.job import CropConfig, CropMode
from image23mf.editor.local_edits import LocalEditBoundsError
from image23mf.editor.replay import EditorReplayError, replay_editor_commands
from image23mf.engine.labels import LabelField
from image23mf.engine.regions import analyze_regions
from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize

CONFIG = hashlib.sha256(b"local-edit-config").hexdigest()
COLORS = {0: "#F5E6C8", 1: "#111111", 2: "#FF6600"}


def _fixture(
    rows: list[list[int]],
    *,
    labels: tuple[int, ...] = (0, 1, 2),
    active: bytes | None = None,
):
    array = np.asarray(rows, dtype=np.uint8)
    field = LabelField(
        width=array.shape[1],
        height=array.shape[0],
        label_values=labels,
        pixels=array.tobytes(),
    )
    active = active or bytes([1]) * array.size
    analysis = analyze_regions(
        field,
        colors={label: COLORS[label] for label in labels},
        width_mm=field.width,
        height_mm=field.height,
        active=active,
    )
    size = PixelSize(width=field.width, height=field.height)
    transform = CanonicalTransform.from_crop_config(
        original_size=size,
        normalized_size=size,
        exif_orientation=1,
        crop=CropConfig(mode=CropMode.STRETCH),
        working_size=size,
        canvas_size=MillimetreSize(width=field.width, height=field.height),
    )
    return field, active, analysis, transform


def _command(analysis, transform, edit, *, x: float, y: float, width: float, height: float):
    return LocalRasterEditCommand(
        command_id="cmd_localedit01",
        created_at=datetime.now(timezone.utc),
        provenance={"ui": "test-local-edit", "selection_snapshot": True},
        selector=CanvasSelectionSelector(
            graph_fingerprint=analysis.graph.fingerprint(),
            config_fingerprint=CONFIG,
            selection=CanvasSelectionState(
                source_width_px=transform.normalized_size.width,
                source_height_px=transform.normalized_size.height,
                primitives=(
                    SourceRectangleSelection(
                        primitive_id="selection_local001",
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                    ),
                ),
            ),
        ),
        edit=edit,
    )


def _replay(field, active, transform, command):
    return replay_editor_commands(
        field,
        active=active,
        colors={label: COLORS[label] for label in field.label_values},
        width_mm=field.width,
        height_mm=field.height,
        config_fingerprint=CONFIG,
        commands=(command,),
        transform=transform,
    )


def _array(result) -> np.ndarray:
    return np.frombuffer(result.labels.pixels, dtype=np.uint8).reshape(
        (result.labels.height, result.labels.width)
    )


def test_fill_activates_only_the_exact_selected_transparent_area() -> None:
    active_array = np.ones((4, 4), dtype=np.uint8)
    active_array[1, 1] = 0
    field, active, analysis, transform = _fixture(
        np.zeros((4, 4), dtype=np.uint8).tolist(), active=active_array.tobytes()
    )
    command = _command(
        analysis,
        transform,
        LocalFillEdit(target_label=2, activate_transparent=True),
        x=1,
        y=1,
        width=1,
        height=1,
    )

    result = _replay(field, active, transform, command)

    assert result.active[5] == 1
    assert _array(result)[1, 1] == 2
    assert result.record.changed_pixel_count == 1
    assert result.record.steps[0].command_type == "local_raster_edit"
    assert result.record.steps[0].engine_record_fingerprint is not None


def test_color_cleanup_replaces_only_declared_source_labels_inside_selection() -> None:
    rows = [[0, 1, 2], [1, 1, 2], [0, 2, 1]]
    field, active, analysis, transform = _fixture(rows)
    command = _command(
        analysis,
        transform,
        LocalColorCleanupEdit(source_labels=(1,), target_label=0),
        x=0,
        y=0,
        width=2,
        height=2,
    )

    result = _replay(field, active, transform, command)

    assert _array(result).tolist() == [[0, 0, 2], [0, 0, 2], [0, 2, 1]]
    assert result.record.changed_pixel_count == 3


def test_morphology_thickens_in_physical_units_without_overwriting_protected_colors() -> None:
    rows = np.zeros((7, 7), dtype=np.uint8)
    rows[3, 3] = 1
    rows[3, 4] = 2
    field, active, analysis, transform = _fixture(rows.tolist())
    command = _command(
        analysis,
        transform,
        LocalMorphologyEdit(
            operation=LocalMorphologyKind.DILATE,
            source_label=1,
            radius_mm=1,
            editable_labels=(0,),
        ),
        x=3,
        y=3,
        width=1,
        height=1,
    )

    result = _replay(field, active, transform, command)
    output = _array(result)

    assert output[3, 4] == 2
    assert output[3, 3] == 1
    assert output[2, 3] == output[4, 3] == output[3, 2] == 1
    assert result.record.changed_pixel_count == 3


@pytest.mark.parametrize(
    ("operation", "expected_changed"),
    [(LocalMorphologyKind.ERODE, 8), (LocalMorphologyKind.OPEN, 4)],
)
def test_erode_and_open_use_an_explicit_replacement_label(
    operation: LocalMorphologyKind,
    expected_changed: int,
) -> None:
    rows = np.zeros((7, 7), dtype=np.uint8)
    rows[2:5, 2:5] = 1
    field, active, analysis, transform = _fixture(rows.tolist())
    command = _command(
        analysis,
        transform,
        LocalMorphologyEdit(
            operation=operation,
            source_label=1,
            radius_mm=1,
            replacement_label=0,
        ),
        x=2,
        y=2,
        width=3,
        height=3,
    )

    result = _replay(field, active, transform, command)

    assert result.record.changed_pixel_count == expected_changed


def test_close_fills_a_selected_feature_gap_only_through_explicit_editable_color() -> None:
    rows = np.zeros((7, 7), dtype=np.uint8)
    rows[2:5, 2:5] = 1
    rows[3, 3] = 0
    field, active, analysis, transform = _fixture(rows.tolist())
    command = _command(
        analysis,
        transform,
        LocalMorphologyEdit(
            operation=LocalMorphologyKind.CLOSE,
            source_label=1,
            radius_mm=1,
            editable_labels=(0,),
        ),
        x=2,
        y=2,
        width=3,
        height=3,
    )

    result = _replay(field, active, transform, command)

    assert _array(result)[3, 3] == 1
    assert result.record.changed_pixel_count == 1


def test_clone_is_overlap_safe_and_requires_explicit_canvas_clipping() -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[1, 1:3] = (1, 2)
    field, active, analysis, transform = _fixture(rows.tolist())
    command = _command(
        analysis,
        transform,
        LocalCloneEdit(offset_x_mm=0, offset_y_mm=1, overwrite_labels=(0,)),
        x=1,
        y=1,
        width=2,
        height=1,
    )

    first = _replay(field, active, transform, command)
    second = _replay(field, active, transform, command)

    assert _array(first)[2, 1:3].tolist() == [1, 2]
    assert first.fingerprint() == second.fingerprint()

    out_of_bounds = _command(
        analysis,
        transform,
        LocalCloneEdit(offset_x_mm=-2, offset_y_mm=0, overwrite_labels=(0,)),
        x=1,
        y=1,
        width=1,
        height=1,
    )
    with pytest.raises(LocalEditBoundsError, match="exceeds"):
        _replay(field, active, transform, out_of_bounds)


def test_affine_resize_uses_nearest_neighbor_and_records_exact_label_reprocessing() -> None:
    rows = np.zeros((7, 7), dtype=np.uint8)
    rows[1:3, 1:3] = 1
    field, active, analysis, transform = _fixture(rows.tolist())
    command = _command(
        analysis,
        transform,
        LocalAffineEdit(
            scale_x=2,
            scale_y=2,
            origin_x=0,
            origin_y=0,
            overwrite_labels=(0,),
            source_background_label=0,
        ),
        x=1,
        y=1,
        width=2,
        height=2,
    )

    result = _replay(field, active, transform, command)
    output = _array(result)

    assert np.all(output[1:5, 1:5] == 1)
    assert np.count_nonzero(output == 1) == 16
    assert result.analysis.graph.fingerprint() == result.record.after_graph_fingerprint
    assert result.record.changed_pixel_count == 12


def test_affine_rejects_out_of_bounds_destination_and_singular_contracts() -> None:
    rows = np.zeros((4, 4), dtype=np.uint8)
    rows[2:4, 2:4] = 1
    field, active, analysis, transform = _fixture(rows.tolist())
    command = _command(
        analysis,
        transform,
        LocalAffineEdit(
            scale_x=2,
            scale_y=2,
            origin_x=0,
            origin_y=0,
            overwrite_labels=(0,),
        ),
        x=2,
        y=2,
        width=2,
        height=2,
    )
    with pytest.raises(LocalEditBoundsError, match="exceeds"):
        _replay(field, active, transform, command)

    with pytest.raises(ValidationError, match="invertible"):
        LocalAffineEdit(
            scale_x=1,
            scale_y=1,
            shear_x=1,
            shear_y=1,
            overwrite_labels=(0,),
        )


def test_local_edit_requires_transform_and_rejects_undeclared_labels() -> None:
    rows = np.zeros((3, 3), dtype=np.uint8)
    field, active, analysis, transform = _fixture(rows.tolist())
    command = _command(
        analysis,
        transform,
        LocalFillEdit(target_label=1),
        x=0,
        y=0,
        width=1,
        height=1,
    )
    with pytest.raises(EditorReplayError, match="canonical source transform"):
        replay_editor_commands(
            field,
            active=active,
            colors=COLORS,
            width_mm=field.width,
            height_mm=field.height,
            config_fingerprint=CONFIG,
            commands=(command,),
        )

    unknown = command.model_copy(update={"edit": LocalFillEdit(target_label=99)})
    with pytest.raises(ValueError, match="absent from the palette"):
        _replay(field, active, transform, unknown)
