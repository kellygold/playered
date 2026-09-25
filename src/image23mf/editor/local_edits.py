"""Deterministic, selection-bounded edits over an exhaustive label raster."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from image23mf.contracts.editor import (
    LocalAffineEdit,
    LocalCloneEdit,
    LocalColorCleanupEdit,
    LocalFillEdit,
    LocalMorphologyEdit,
    LocalMorphologyKind,
    LocalRasterEdit,
)
from image23mf.editor.selection import SelectionRaster
from image23mf.engine.contours import physical_kernel_offsets
from image23mf.engine.labels import LabelField
from image23mf.engine.transform import CanonicalTransform


class LocalEditError(ValueError):
    """A deterministic local edit cannot be applied without guessing."""


class LocalEditBoundsError(LocalEditError):
    """A clone or affine destination exceeds the canvas without explicit clipping."""


@dataclass(frozen=True)
class LocalEditResult:
    labels: LabelField
    active: bytes
    selected_pixel_count: int
    destination_pixel_count: int

    def __post_init__(self) -> None:
        size = self.labels.width * self.labels.height
        if len(self.active) != size:
            raise ValueError("local-edit active plane must match the label field")
        if self.selected_pixel_count < 1:
            raise ValueError("local edits require a non-empty source selection")
        if self.destination_pixel_count < 0:
            raise ValueError("local-edit destination count cannot be negative")


def apply_local_raster_edit(
    labels: LabelField,
    *,
    active: bytes,
    selection: SelectionRaster,
    transform: CanonicalTransform,
    edit: LocalRasterEdit,
) -> LocalEditResult:
    """Apply one typed edit using only exact label, active, selection, and transform planes."""

    _validate_planes(labels, active, selection, transform)
    label_array = np.frombuffer(labels.pixels, dtype=np.uint8).reshape(
        (labels.height, labels.width)
    )
    active_array = (
        np.frombuffer(active, dtype=np.uint8).reshape((labels.height, labels.width)).astype(bool)
    )
    selected = (
        np.frombuffer(selection.mask, dtype=np.uint8)
        .reshape((labels.height, labels.width))
        .astype(bool)
    )
    selected_count = int(np.count_nonzero(selected))
    if selected_count == 0:
        raise LocalEditError("the saved selection does not cover any working pixel")

    _validate_declared_labels(labels, edit)
    if isinstance(edit, LocalFillEdit):
        updated_labels, updated_active, destination_count = _fill(
            label_array, active_array, selected, edit
        )
    elif isinstance(edit, LocalColorCleanupEdit):
        updated_labels, updated_active, destination_count = _color_cleanup(
            label_array, active_array, selected, edit
        )
    elif isinstance(edit, LocalMorphologyEdit):
        updated_labels, updated_active, destination_count = _morphology(
            label_array,
            active_array,
            selected,
            edit,
            pixel_width_mm=transform.canvas_size.width / labels.width,
            pixel_height_mm=transform.canvas_size.height / labels.height,
        )
    elif isinstance(edit, LocalCloneEdit):
        updated_labels, updated_active, destination_count = _clone(
            label_array,
            active_array,
            selected,
            edit,
            pixel_width_mm=transform.canvas_size.width / labels.width,
            pixel_height_mm=transform.canvas_size.height / labels.height,
        )
    elif isinstance(edit, LocalAffineEdit):
        updated_labels, updated_active, destination_count = _affine(
            label_array,
            active_array,
            selected,
            edit,
            pixel_width_mm=transform.canvas_size.width / labels.width,
            pixel_height_mm=transform.canvas_size.height / labels.height,
        )
    else:  # pragma: no cover - the discriminated contract is exhaustive
        raise TypeError(f"unsupported local raster edit: {type(edit).__name__}")

    return LocalEditResult(
        labels=LabelField(
            width=labels.width,
            height=labels.height,
            label_values=labels.label_values,
            pixels=updated_labels.astype(np.uint8).tobytes(),
        ),
        active=updated_active.astype(np.uint8).tobytes(),
        selected_pixel_count=selected_count,
        destination_pixel_count=destination_count,
    )


def _validate_planes(
    labels: LabelField,
    active: bytes,
    selection: SelectionRaster,
    transform: CanonicalTransform,
) -> None:
    size = labels.width * labels.height
    if len(active) != size:
        raise ValueError("local-edit active plane must match the label field")
    if selection.width != labels.width or selection.height != labels.height:
        raise ValueError("local-edit selection must match the label field")
    if (
        transform.working_size.width != labels.width
        or transform.working_size.height != labels.height
    ):
        raise ValueError("local-edit transform must match the label field")


def _validate_declared_labels(labels: LabelField, edit: LocalRasterEdit) -> None:
    declared = set(labels.label_values)
    referenced: set[int]
    if isinstance(edit, LocalFillEdit):
        referenced = {edit.target_label}
    elif isinstance(edit, LocalColorCleanupEdit):
        referenced = {*edit.source_labels, edit.target_label}
    elif isinstance(edit, LocalMorphologyEdit):
        referenced = {edit.source_label, *edit.editable_labels}
        if edit.replacement_label is not None:
            referenced.add(edit.replacement_label)
    elif isinstance(edit, LocalCloneEdit):
        referenced = set(edit.overwrite_labels)
    else:
        referenced = set(edit.overwrite_labels)
        if edit.source_background_label is not None:
            referenced.add(edit.source_background_label)
    unknown = referenced - declared
    if unknown:
        raise LocalEditError(
            f"local edit references labels absent from the palette: {sorted(unknown)}"
        )


def _fill(
    labels: np.ndarray,
    active: np.ndarray,
    selected: np.ndarray,
    edit: LocalFillEdit,
) -> tuple[np.ndarray, np.ndarray, int]:
    destination = selected & (active | edit.activate_transparent)
    updated_labels = labels.copy()
    updated_active = active.copy()
    updated_labels[destination] = edit.target_label
    if edit.activate_transparent:
        updated_active[destination] = True
    return updated_labels, updated_active, int(np.count_nonzero(destination))


def _color_cleanup(
    labels: np.ndarray,
    active: np.ndarray,
    selected: np.ndarray,
    edit: LocalColorCleanupEdit,
) -> tuple[np.ndarray, np.ndarray, int]:
    destination = selected & active & np.isin(labels, edit.source_labels)
    updated = labels.copy()
    updated[destination] = edit.target_label
    return updated, active.copy(), int(np.count_nonzero(destination))


def _morphology(
    labels: np.ndarray,
    active: np.ndarray,
    selected: np.ndarray,
    edit: LocalMorphologyEdit,
    *,
    pixel_width_mm: float,
    pixel_height_mm: float,
) -> tuple[np.ndarray, np.ndarray, int]:
    source = selected & active & (labels == edit.source_label)
    if not np.any(source):
        raise LocalEditError("the selection contains no active pixel with the morphology label")
    offsets = physical_kernel_offsets(
        edit.radius_mm,
        pixel_width_mm=pixel_width_mm,
        pixel_height_mm=pixel_height_mm,
    )
    if edit.operation == LocalMorphologyKind.DILATE:
        proposed = _dilate(source, offsets)
    elif edit.operation == LocalMorphologyKind.ERODE:
        proposed = _erode(source, offsets)
    elif edit.operation == LocalMorphologyKind.OPEN:
        proposed = _dilate(_erode(source, offsets), offsets)
    else:
        proposed = _erode(_dilate(source, offsets), offsets)

    updated = labels.copy()
    if edit.operation in {LocalMorphologyKind.DILATE, LocalMorphologyKind.CLOSE}:
        destination = proposed & ~source & active & np.isin(labels, edit.editable_labels)
        updated[destination] = edit.source_label
    else:
        destination = source & ~proposed
        assert edit.replacement_label is not None
        updated[destination] = edit.replacement_label
    return updated, active.copy(), int(np.count_nonzero(destination))


def _clone(
    labels: np.ndarray,
    active: np.ndarray,
    selected: np.ndarray,
    edit: LocalCloneEdit,
    *,
    pixel_width_mm: float,
    pixel_height_mm: float,
) -> tuple[np.ndarray, np.ndarray, int]:
    dx = _round_half_away_from_zero(edit.offset_x_mm / pixel_width_mm)
    dy = _round_half_away_from_zero(edit.offset_y_mm / pixel_height_mm)
    if dx == 0 and dy == 0:
        raise LocalEditError("clone offset resolves to zero working pixels")
    source = selected & (active | edit.include_transparent)
    source_y, source_x = np.nonzero(source)
    destination_x = source_x + dx
    destination_y = source_y + dy
    in_bounds = (
        (destination_x >= 0)
        & (destination_x < labels.shape[1])
        & (destination_y >= 0)
        & (destination_y < labels.shape[0])
    )
    if not edit.clip_to_canvas and not np.all(in_bounds):
        raise LocalEditBoundsError("clone destination exceeds the working canvas")
    source_x = source_x[in_bounds]
    source_y = source_y[in_bounds]
    destination_x = destination_x[in_bounds]
    destination_y = destination_y[in_bounds]
    allowed = (~active[destination_y, destination_x]) | np.isin(
        labels[destination_y, destination_x], edit.overwrite_labels
    )
    source_x = source_x[allowed]
    source_y = source_y[allowed]
    destination_x = destination_x[allowed]
    destination_y = destination_y[allowed]
    updated_labels = labels.copy()
    updated_active = active.copy()
    updated_labels[destination_y, destination_x] = labels[source_y, source_x]
    updated_active[destination_y, destination_x] = active[source_y, source_x]
    return updated_labels, updated_active, len(destination_x)


def _affine(
    labels: np.ndarray,
    active: np.ndarray,
    selected: np.ndarray,
    edit: LocalAffineEdit,
    *,
    pixel_width_mm: float,
    pixel_height_mm: float,
) -> tuple[np.ndarray, np.ndarray, int]:
    source_y, source_x = np.nonzero(selected)
    left = float(source_x.min()) * pixel_width_mm
    right = float(source_x.max() + 1) * pixel_width_mm
    top = float(source_y.min()) * pixel_height_mm
    bottom = float(source_y.max() + 1) * pixel_height_mm
    origin_x = left + (right - left) * edit.origin_x
    origin_y = top + (bottom - top) * edit.origin_y
    matrix = np.asarray(
        [[edit.scale_x, edit.shear_x], [edit.shear_y, edit.scale_y]], dtype=np.float64
    )
    inverse = np.linalg.inv(matrix)

    corners = np.asarray(
        [[left, top], [right, top], [left, bottom], [right, bottom]], dtype=np.float64
    )
    transformed_corners = (corners - (origin_x, origin_y)) @ matrix.T + (
        origin_x + edit.translate_x_mm,
        origin_y + edit.translate_y_mm,
    )
    canvas_width = labels.shape[1] * pixel_width_mm
    canvas_height = labels.shape[0] * pixel_height_mm
    outside = (
        np.any(transformed_corners[:, 0] < 0)
        or np.any(transformed_corners[:, 0] > canvas_width)
        or np.any(transformed_corners[:, 1] < 0)
        or np.any(transformed_corners[:, 1] > canvas_height)
    )
    if outside and not edit.clip_to_canvas:
        raise LocalEditBoundsError("affine destination exceeds the working canvas")

    grid_y, grid_x = np.indices(labels.shape, dtype=np.float64)
    destination_points = np.stack(
        ((grid_x + 0.5) * pixel_width_mm, (grid_y + 0.5) * pixel_height_mm), axis=-1
    )
    source_points = (
        destination_points - (origin_x + edit.translate_x_mm, origin_y + edit.translate_y_mm)
    ) @ inverse.T + (origin_x, origin_y)
    mapped_x = np.floor(source_points[..., 0] / pixel_width_mm).astype(np.int64)
    mapped_y = np.floor(source_points[..., 1] / pixel_height_mm).astype(np.int64)
    valid = (
        (mapped_x >= 0)
        & (mapped_x < labels.shape[1])
        & (mapped_y >= 0)
        & (mapped_y < labels.shape[0])
    )
    clipped_x = np.clip(mapped_x, 0, labels.shape[1] - 1)
    clipped_y = np.clip(mapped_y, 0, labels.shape[0] - 1)
    sampled = valid & selected[clipped_y, clipped_x]
    sampled &= active[clipped_y, clipped_x] | edit.include_transparent
    overwrite = (~active) | np.isin(labels, edit.overwrite_labels) | selected
    destination = sampled & overwrite

    updated_labels = labels.copy()
    updated_active = active.copy()
    if edit.clear_source:
        clear = selected & (active | edit.include_transparent)
        if edit.source_background_label is None:
            updated_active[clear] = False
        else:
            updated_labels[clear] = edit.source_background_label
            updated_active[clear] = True
    updated_labels[destination] = labels[clipped_y[destination], clipped_x[destination]]
    updated_active[destination] = active[clipped_y[destination], clipped_x[destination]]
    return updated_labels, updated_active, int(np.count_nonzero(destination))


def _dilate(mask: np.ndarray, offsets: tuple[tuple[int, int], ...]) -> np.ndarray:
    result = np.zeros(mask.shape, dtype=bool)
    for dy, dx in offsets:
        result |= _shift(mask, dy, dx)
    return result


def _erode(mask: np.ndarray, offsets: tuple[tuple[int, int], ...]) -> np.ndarray:
    result = np.ones(mask.shape, dtype=bool)
    for dy, dx in offsets:
        result &= _shift(mask, dy, dx)
    return result


def _shift(mask: np.ndarray, dy: int, dx: int) -> np.ndarray:
    result = np.zeros(mask.shape, dtype=bool)
    source_y = slice(max(0, -dy), min(mask.shape[0], mask.shape[0] - dy))
    source_x = slice(max(0, -dx), min(mask.shape[1], mask.shape[1] - dx))
    target_y = slice(max(0, dy), min(mask.shape[0], mask.shape[0] + dy))
    target_x = slice(max(0, dx), min(mask.shape[1], mask.shape[1] + dx))
    result[target_y, target_x] = mask[source_y, source_x]
    return result


def _round_half_away_from_zero(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)
