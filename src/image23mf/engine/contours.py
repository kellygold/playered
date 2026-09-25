"""Explicit physical-unit contour cleanup for exhaustive label fields."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.labels import LabelField
from image23mf.engine.regions import (
    RegionAnalysis,
    RegionLineage,
    analyze_regions,
    derive_region_lineage,
)

CONTOUR_CLEANUP_SCHEMA_VERSION = 1
MAX_CONTOUR_KERNEL_OFFSETS = 4096


class ContourOperationKind(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    MAJORITY = "majority"
    BOUNDARY_SIMPLIFY = "boundary_simplify"


class ContourModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ContourOperation(ContourModel):
    kind: ContourOperationKind
    radius_mm: float = Field(gt=0, le=25)
    editable_labels: tuple[int, ...] = ()
    subject_label: Optional[int] = Field(default=None, ge=0, le=255)
    replacement_label: Optional[int] = Field(default=None, ge=0, le=255)
    iterations: int = Field(default=1, ge=1, le=8)
    minimum_majority_ratio: Optional[float] = Field(default=None, gt=0.5, le=1)

    @model_validator(mode="after")
    def operation_is_explicit(self) -> ContourOperation:
        if self.editable_labels != tuple(sorted(set(self.editable_labels))):
            raise ValueError("editable labels must be unique and canonically sorted")
        if self.kind == ContourOperationKind.OPEN:
            if self.subject_label is None or self.replacement_label is None:
                raise ValueError("open requires explicit subject and replacement labels")
            if self.subject_label == self.replacement_label:
                raise ValueError("open subject and replacement labels must differ")
            if self.editable_labels:
                raise ValueError("open edits only its explicit subject label")
        elif self.kind == ContourOperationKind.CLOSE:
            if self.subject_label is None or not self.editable_labels:
                raise ValueError("close requires a subject and editable source labels")
            if self.subject_label in self.editable_labels:
                raise ValueError("close source labels must exclude its subject label")
            if self.replacement_label is not None:
                raise ValueError("close does not accept a replacement label")
        else:
            if not self.editable_labels:
                raise ValueError(f"{self.kind.value} requires editable source labels")
            if self.subject_label is not None or self.replacement_label is not None:
                raise ValueError(f"{self.kind.value} does not accept subject or replacement labels")
        needs_ratio = self.kind in (
            ContourOperationKind.MAJORITY,
            ContourOperationKind.BOUNDARY_SIMPLIFY,
        )
        if needs_ratio and self.minimum_majority_ratio is None:
            raise ValueError(f"{self.kind.value} requires an explicit majority ratio")
        if not needs_ratio and self.minimum_majority_ratio is not None:
            raise ValueError(f"{self.kind.value} does not accept a majority ratio")
        if (
            self.kind in (ContourOperationKind.OPEN, ContourOperationKind.CLOSE)
            and self.iterations != 1
        ):
            raise ValueError(f"{self.kind.value} uses one physical-radius pass")
        return self


class ContourCleanupRequest(ContourModel):
    schema_version: Literal[1] = CONTOUR_CLEANUP_SCHEMA_VERSION
    operations: tuple[ContourOperation, ...] = Field(min_length=1, max_length=16)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class LabelTransition(ContourModel):
    source_label: int = Field(ge=0, le=255)
    target_label: int = Field(ge=0, le=255)
    pixel_count: int = Field(gt=0)
    area_mm2: float = Field(gt=0)


class LabelAreaChange(ContourModel):
    label: int = Field(ge=0, le=255)
    before_pixel_count: int = Field(ge=0)
    after_pixel_count: int = Field(ge=0)
    delta_pixel_count: int
    before_area_mm2: float = Field(ge=0)
    after_area_mm2: float = Field(ge=0)
    delta_area_mm2: float

    @model_validator(mode="after")
    def deltas_are_exact(self) -> LabelAreaChange:
        if self.delta_pixel_count != self.after_pixel_count - self.before_pixel_count:
            raise ValueError("label pixel delta does not match before and after counts")
        expected = self.after_area_mm2 - self.before_area_mm2
        if not math.isclose(self.delta_area_mm2, expected, abs_tol=1e-12):
            raise ValueError("label area delta does not match before and after areas")
        return self


class ContourStepStatistics(ContourModel):
    index: int = Field(ge=0)
    operation: ContourOperation
    before_label_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_label_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_pixel_count: int = Field(ge=0)
    changed_area_mm2: float = Field(ge=0)
    transitions: tuple[LabelTransition, ...]
    label_areas: tuple[LabelAreaChange, ...]
    before_region_count: int = Field(ge=0)
    after_region_count: int = Field(ge=0)
    before_total_perimeter_mm: float = Field(ge=0)
    after_total_perimeter_mm: float = Field(ge=0)

    @model_validator(mode="after")
    def statistics_are_canonical(self) -> ContourStepStatistics:
        if self.transitions != tuple(
            sorted(self.transitions, key=lambda item: (item.source_label, item.target_label))
        ):
            raise ValueError("label transitions must use canonical ordering")
        if self.label_areas != tuple(sorted(self.label_areas, key=lambda item: item.label)):
            raise ValueError("label area changes must use canonical ordering")
        if self.changed_pixel_count != sum(item.pixel_count for item in self.transitions):
            raise ValueError("changed pixel total does not match label transitions")
        return self


class ContourCleanupRecord(ContourModel):
    schema_version: Literal[1] = CONTOUR_CLEANUP_SCHEMA_VERSION
    request: ContourCleanupRequest
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    steps: tuple[ContourStepStatistics, ...]
    operation_changed_pixel_count: int = Field(ge=0)
    changed_pixel_count: int = Field(ge=0)
    changed_area_mm2: float = Field(ge=0)
    transitions: tuple[LabelTransition, ...]
    label_areas: tuple[LabelAreaChange, ...]
    lineage: RegionLineage

    @model_validator(mode="after")
    def record_is_consistent(self) -> ContourCleanupRecord:
        if self.request_fingerprint != self.request.fingerprint():
            raise ValueError("contour request fingerprint does not match request")
        if len(self.steps) != len(self.request.operations):
            raise ValueError("contour statistics must cover every requested operation")
        for index, (step, operation) in enumerate(zip(self.steps, self.request.operations)):
            if step.index != index or step.operation != operation:
                raise ValueError("contour step statistics do not match request ordering")
        if self.operation_changed_pixel_count != sum(
            step.changed_pixel_count for step in self.steps
        ):
            raise ValueError("operation change total does not match contour steps")
        if self.changed_pixel_count != sum(item.pixel_count for item in self.transitions):
            raise ValueError("net changed pixel total does not match label transitions")
        if self.transitions != tuple(
            sorted(self.transitions, key=lambda item: (item.source_label, item.target_label))
        ):
            raise ValueError("net label transitions must use canonical ordering")
        if self.label_areas != tuple(sorted(self.label_areas, key=lambda item: item.label)):
            raise ValueError("net label areas must use canonical ordering")
        if (
            self.lineage.before_graph_fingerprint != self.before_graph_fingerprint
            or self.lineage.after_graph_fingerprint != self.after_graph_fingerprint
        ):
            raise ValueError("contour lineage does not match graph fingerprints")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AppliedContourCleanup:
    record: ContourCleanupRecord
    labels: LabelField
    active: bytes
    changed_mask: bytes
    analysis: RegionAnalysis

    def __post_init__(self) -> None:
        size = self.labels.width * self.labels.height
        if len(self.active) != size or len(self.changed_mask) != size:
            raise ValueError("contour active and changed planes must match the label field")
        if self.record.after_graph_fingerprint != self.analysis.graph.fingerprint():
            raise ValueError("applied contour cleanup does not match its after analysis")
        if sum(1 for value in self.changed_mask if value) != self.record.changed_pixel_count:
            raise ValueError("contour changed mask does not match its exact statistics")


def apply_contour_cleanup(
    labels: LabelField,
    analysis: RegionAnalysis,
    request: ContourCleanupRequest,
) -> AppliedContourCleanup:
    """Apply only the ordered, explicitly requested operations to editable active labels."""

    _validate_source_labels(labels, analysis)
    declared = set(labels.label_values)
    for operation in request.operations:
        referenced = set(operation.editable_labels)
        if operation.subject_label is not None:
            referenced.add(operation.subject_label)
        if operation.replacement_label is not None:
            referenced.add(operation.replacement_label)
        unknown = referenced - declared
        if unknown:
            raise ValueError(
                f"editable contour labels are absent from the palette: {sorted(unknown)}"
            )

    active_array = analysis.assignment >= 0
    active = active_array.astype(np.uint8).tobytes()
    original = np.frombuffer(labels.pixels, dtype=np.uint8).reshape((labels.height, labels.width))
    working = original.copy()
    colors = {entry.label: entry.color for entry in analysis.graph.palette}
    current_analysis = analysis
    step_statistics = []
    pixel_width_mm = analysis.graph.pixel_width_mm
    pixel_height_mm = analysis.graph.pixel_height_mm

    for index, operation in enumerate(request.operations):
        offsets = physical_kernel_offsets(
            operation.radius_mm,
            pixel_width_mm=pixel_width_mm,
            pixel_height_mm=pixel_height_mm,
        )
        before = working.copy()
        working = _apply_operation(
            working,
            active_array,
            labels.label_values,
            operation,
            offsets,
        )
        next_labels = _field_like(labels, working)
        next_analysis = analyze_regions(
            next_labels,
            colors=colors,
            width_mm=analysis.graph.width_mm,
            height_mm=analysis.graph.height_mm,
            active=active,
        )
        step_statistics.append(
            _step_statistics(
                index,
                operation,
                before,
                working,
                active_array,
                labels.label_values,
                current_analysis,
                next_analysis,
            )
        )
        current_analysis = next_analysis

    result_labels = _field_like(labels, working)
    changed = active_array & (working != original)
    pixel_area = pixel_width_mm * pixel_height_mm
    transitions = _transitions(original, working, changed, pixel_area)
    label_areas = _label_areas(
        original,
        working,
        active_array,
        labels.label_values,
        pixel_area,
    )
    lineage = derive_region_lineage(analysis, current_analysis)
    record = ContourCleanupRecord(
        request=request,
        request_fingerprint=request.fingerprint(),
        before_graph_fingerprint=analysis.graph.fingerprint(),
        after_graph_fingerprint=current_analysis.graph.fingerprint(),
        steps=tuple(step_statistics),
        operation_changed_pixel_count=sum(item.changed_pixel_count for item in step_statistics),
        changed_pixel_count=int(np.count_nonzero(changed)),
        changed_area_mm2=float(np.count_nonzero(changed)) * pixel_area,
        transitions=transitions,
        label_areas=label_areas,
        lineage=lineage,
    )
    return AppliedContourCleanup(
        record=record,
        labels=result_labels,
        active=active,
        changed_mask=changed.astype(np.uint8).tobytes(),
        analysis=current_analysis,
    )


def physical_kernel_offsets(
    radius_mm: float,
    *,
    pixel_width_mm: float,
    pixel_height_mm: float,
) -> tuple[tuple[int, int], ...]:
    """Return a deterministic cell-centre ellipse for a physical radius."""

    dimensions = (radius_mm, pixel_width_mm, pixel_height_mm)
    if not all(math.isfinite(value) and value > 0 for value in dimensions):
        raise ValueError("contour radius and pixel dimensions must be finite and positive")
    x_extent = math.floor(radius_mm / pixel_width_mm)
    y_extent = math.floor(radius_mm / pixel_height_mm)
    bounding_sample_count = (2 * x_extent + 1) * (2 * y_extent + 1)
    if bounding_sample_count > MAX_CONTOUR_KERNEL_OFFSETS * 2:
        raise ValueError(
            "physical contour radius resolves to too many raster samples; reduce the radius "
            "or preview resolution"
        )
    offsets = tuple(
        (dy, dx)
        for dy in range(-y_extent, y_extent + 1)
        for dx in range(-x_extent, x_extent + 1)
        if (dx * pixel_width_mm) ** 2 + (dy * pixel_height_mm) ** 2 <= radius_mm**2 + 1e-12
    )
    if len(offsets) > MAX_CONTOUR_KERNEL_OFFSETS:
        raise ValueError(
            "physical contour radius resolves to too many raster samples; reduce the radius "
            "or preview resolution"
        )
    return offsets


def _apply_operation(
    labels: np.ndarray,
    active: np.ndarray,
    label_values: tuple[int, ...],
    operation: ContourOperation,
    offsets: tuple[tuple[int, int], ...],
) -> np.ndarray:
    result = labels.copy()
    for _iteration in range(operation.iterations):
        if operation.kind == ContourOperationKind.OPEN:
            editable = active & (result == operation.subject_label)
        else:
            editable = active & np.isin(result, operation.editable_labels)
        if not np.any(editable):
            break
        if operation.kind == ContourOperationKind.OPEN:
            proposed = _explicit_open(result, active, operation, offsets)
            accepted = editable
        elif operation.kind == ContourOperationKind.CLOSE:
            proposed = _explicit_close(result, active, operation, offsets)
            accepted = editable
        elif operation.kind == ContourOperationKind.MAJORITY:
            proposed, winning, total = _local_mode(result, active, label_values, offsets)
            ratio = np.divide(
                winning,
                total,
                out=np.zeros_like(winning, dtype=np.float64),
                where=total > 0,
            )
            accepted = editable & (ratio >= operation.minimum_majority_ratio)
        elif operation.kind == ContourOperationKind.BOUNDARY_SIMPLIFY:
            proposed, winning, total = _morphological_proposal(
                result,
                active,
                label_values,
                offsets,
            )
            ratio = np.divide(
                winning,
                total,
                out=np.zeros_like(winning, dtype=np.float64),
                where=total > 0,
            )
            accepted = (
                editable
                & _boundary_pixels(result, active)
                & (ratio >= operation.minimum_majority_ratio)
            )
        accepted &= proposed != result
        if not np.any(accepted):
            break
        result[accepted] = proposed[accepted]
    result[~active] = labels[~active]
    return result


def _morphological_proposal(
    labels: np.ndarray,
    active: np.ndarray,
    label_values: tuple[int, ...],
    offsets: tuple[tuple[int, int], ...],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    opened = _multilabel_opening_proposal(labels, active, label_values, label_values, offsets)
    proposed = _multilabel_closing_proposal(opened, active, label_values, offsets)
    winning, total = _proposal_support(labels, proposed, active, label_values, offsets)
    return proposed, winning, total


def _explicit_open(
    labels: np.ndarray,
    active: np.ndarray,
    operation: ContourOperation,
    offsets: tuple[tuple[int, int], ...],
) -> np.ndarray:
    proposed = labels.copy()
    subject = active & (labels == operation.subject_label)
    opened = _binary_open(subject, active, offsets)
    proposed[subject & ~opened] = operation.replacement_label
    return proposed


def _explicit_close(
    labels: np.ndarray,
    active: np.ndarray,
    operation: ContourOperation,
    offsets: tuple[tuple[int, int], ...],
) -> np.ndarray:
    proposed = labels.copy()
    subject = active & (labels == operation.subject_label)
    closed = _binary_close(subject, active, offsets)
    editable = active & np.isin(labels, operation.editable_labels)
    proposed[closed & ~subject & editable] = operation.subject_label
    return proposed


def _multilabel_opening_proposal(
    labels: np.ndarray,
    active: np.ndarray,
    label_values: tuple[int, ...],
    editable_labels: tuple[int, ...],
    offsets: tuple[tuple[int, int], ...],
) -> np.ndarray:
    """Remove only pixels excluded by their source label's exact binary opening."""

    proposed = labels.copy()
    counts = {label: _neighbor_count(active & (labels == label), offsets) for label in label_values}
    for source_label in editable_labels:
        source = active & (labels == source_label)
        opened = _binary_open(source, active, offsets)
        removed = source & ~opened
        if not np.any(removed):
            continue
        best_label = np.full(labels.shape, source_label, dtype=np.uint8)
        best_score = np.full(labels.shape, -1, dtype=np.int32)
        for target_label in label_values:
            if target_label == source_label:
                continue
            score = counts[target_label]
            better = score > best_score
            best_label[better] = target_label
            best_score[better] = score[better]
        replace = removed & (best_score > 0)
        proposed[replace] = best_label[replace]
    return proposed


def _multilabel_closing_proposal(
    labels: np.ndarray,
    active: np.ndarray,
    label_values: tuple[int, ...],
    offsets: tuple[tuple[int, int], ...],
) -> np.ndarray:
    """Resolve overlapping exact binary closings by physical-neighborhood support."""

    candidate_masks = []
    counts = []
    for label in label_values:
        mask = active & (labels == label)
        candidate_masks.append(_binary_close(mask, active, offsets))
        counts.append(_neighbor_count(mask, offsets))
    proposed, _winning, _total = _resolve_candidates(
        labels, label_values, candidate_masks, counts, active
    )
    return proposed


def _proposal_support(
    labels: np.ndarray,
    proposed: np.ndarray,
    active: np.ndarray,
    label_values: tuple[int, ...],
    offsets: tuple[tuple[int, int], ...],
) -> tuple[np.ndarray, np.ndarray]:
    winning = np.zeros(labels.shape, dtype=np.int32)
    total = np.zeros(labels.shape, dtype=np.int32)
    for label in label_values:
        count = _neighbor_count(active & (labels == label), offsets)
        winning[proposed == label] = count[proposed == label]
        total += count
    return winning, total


def _local_mode(
    labels: np.ndarray,
    active: np.ndarray,
    label_values: tuple[int, ...],
    offsets: tuple[tuple[int, int], ...],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    counts = [_neighbor_count(active & (labels == label), offsets) for label in label_values]
    candidates = [active] * len(label_values)
    return _resolve_candidates(labels, label_values, candidates, counts, active)


def _resolve_candidates(
    original: np.ndarray,
    label_values: tuple[int, ...],
    candidates: list[np.ndarray],
    counts: list[np.ndarray],
    active: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    shape = original.shape
    candidate_label = np.full(shape, label_values[0], dtype=np.uint8)
    candidate_score = np.full(shape, -1, dtype=np.int32)
    fallback_label = np.full(shape, label_values[0], dtype=np.uint8)
    fallback_score = np.full(shape, -1, dtype=np.int32)
    any_candidate = np.zeros(shape, dtype=np.bool_)
    for label, candidate, count in zip(label_values, candidates, counts):
        fallback_better = (count > fallback_score) | (
            (count == fallback_score) & (original == label)
        )
        fallback_label[fallback_better] = label
        fallback_score[fallback_better] = count[fallback_better]
        eligible_score = np.where(candidate, count, -1)
        candidate_better = (eligible_score > candidate_score) | (
            (eligible_score == candidate_score) & candidate & (original == label)
        )
        candidate_label[candidate_better] = label
        candidate_score[candidate_better] = eligible_score[candidate_better]
        any_candidate |= candidate
    proposed = np.where(any_candidate, candidate_label, fallback_label).astype(np.uint8)
    winning = np.where(any_candidate, candidate_score, fallback_score)
    total = np.zeros(shape, dtype=np.int32)
    for count in counts:
        total += count
    return proposed, winning, total


def _binary_open(
    mask: np.ndarray, domain: np.ndarray, offsets: tuple[tuple[int, int], ...]
) -> np.ndarray:
    return _binary_dilate(_binary_erode(mask, domain, offsets), domain, offsets)


def _binary_close(
    mask: np.ndarray, domain: np.ndarray, offsets: tuple[tuple[int, int], ...]
) -> np.ndarray:
    return _binary_erode(_binary_dilate(mask, domain, offsets), domain, offsets)


def _binary_dilate(
    mask: np.ndarray, domain: np.ndarray, offsets: tuple[tuple[int, int], ...]
) -> np.ndarray:
    result = np.zeros(mask.shape, dtype=np.bool_)
    for dy, dx in offsets:
        destination, source = _paired_slices(mask.shape, dy, dx)
        result[destination] |= mask[source]
    result &= domain
    return result


def _binary_erode(
    mask: np.ndarray, domain: np.ndarray, offsets: tuple[tuple[int, int], ...]
) -> np.ndarray:
    result = domain.copy()
    for dy, dx in offsets:
        destination, source = _paired_slices(mask.shape, dy, dx)
        result[destination] &= ~domain[source] | mask[source]
    return result


def _neighbor_count(mask: np.ndarray, offsets: tuple[tuple[int, int], ...]) -> np.ndarray:
    result = np.zeros(mask.shape, dtype=np.int32)
    for dy, dx in offsets:
        destination, source = _paired_slices(mask.shape, dy, dx)
        result[destination] += mask[source]
    return result


def _boundary_pixels(labels: np.ndarray, active: np.ndarray) -> np.ndarray:
    boundary = np.zeros(labels.shape, dtype=np.bool_)
    for dy, dx in ((-1, 0), (0, -1), (0, 1), (1, 0)):
        destination, source = _paired_slices(labels.shape, dy, dx)
        boundary[destination] |= (
            active[destination] & active[source] & (labels[destination] != labels[source])
        )
    return boundary


def _paired_slices(
    shape: tuple[int, int], dy: int, dx: int
) -> tuple[tuple[slice, slice], tuple[slice, slice]]:
    height, width = shape
    destination_y = slice(max(0, -dy), min(height, height - dy))
    source_y = slice(max(0, dy), min(height, height + dy))
    destination_x = slice(max(0, -dx), min(width, width - dx))
    source_x = slice(max(0, dx), min(width, width + dx))
    return (destination_y, destination_x), (source_y, source_x)


def _step_statistics(
    index: int,
    operation: ContourOperation,
    before: np.ndarray,
    after: np.ndarray,
    active: np.ndarray,
    label_values: tuple[int, ...],
    before_analysis: RegionAnalysis,
    after_analysis: RegionAnalysis,
) -> ContourStepStatistics:
    pixel_area = before_analysis.graph.pixel_width_mm * before_analysis.graph.pixel_height_mm
    changed = active & (before != after)
    changed_count = int(np.count_nonzero(changed))
    return ContourStepStatistics(
        index=index,
        operation=operation,
        before_label_fingerprint=_label_fingerprint(before, label_values),
        after_label_fingerprint=_label_fingerprint(after, label_values),
        changed_pixel_count=changed_count,
        changed_area_mm2=changed_count * pixel_area,
        transitions=_transitions(before, after, changed, pixel_area),
        label_areas=_label_areas(before, after, active, label_values, pixel_area),
        before_region_count=len(before_analysis.graph.regions),
        after_region_count=len(after_analysis.graph.regions),
        before_total_perimeter_mm=sum(
            region.perimeter_mm for region in before_analysis.graph.regions
        ),
        after_total_perimeter_mm=sum(
            region.perimeter_mm for region in after_analysis.graph.regions
        ),
    )


def _transitions(
    before: np.ndarray,
    after: np.ndarray,
    changed: np.ndarray,
    pixel_area: float,
) -> tuple[LabelTransition, ...]:
    if not np.any(changed):
        return ()
    pairs, counts = np.unique(
        np.column_stack((before[changed], after[changed])),
        axis=0,
        return_counts=True,
    )
    return tuple(
        LabelTransition(
            source_label=int(pair[0]),
            target_label=int(pair[1]),
            pixel_count=int(count),
            area_mm2=int(count) * pixel_area,
        )
        for pair, count in zip(pairs, counts)
    )


def _label_areas(
    before: np.ndarray,
    after: np.ndarray,
    active: np.ndarray,
    label_values: tuple[int, ...],
    pixel_area: float,
) -> tuple[LabelAreaChange, ...]:
    result = []
    for label in label_values:
        before_count = int(np.count_nonzero(active & (before == label)))
        after_count = int(np.count_nonzero(active & (after == label)))
        result.append(
            LabelAreaChange(
                label=label,
                before_pixel_count=before_count,
                after_pixel_count=after_count,
                delta_pixel_count=after_count - before_count,
                before_area_mm2=before_count * pixel_area,
                after_area_mm2=after_count * pixel_area,
                delta_area_mm2=(after_count - before_count) * pixel_area,
            )
        )
    return tuple(result)


def _field_like(source: LabelField, pixels: np.ndarray) -> LabelField:
    return LabelField(
        width=source.width,
        height=source.height,
        label_values=source.label_values,
        pixels=pixels.astype(np.uint8, copy=False).tobytes(),
    )


def _label_fingerprint(pixels: np.ndarray, label_values: tuple[int, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(np.asarray(pixels.shape, dtype=np.int64).tobytes())
    digest.update(bytes(label_values))
    digest.update(pixels.astype(np.uint8, copy=False).tobytes())
    return digest.hexdigest()


def _validate_source_labels(labels: LabelField, analysis: RegionAnalysis) -> None:
    graph = analysis.graph
    if (labels.width, labels.height) != (graph.width_px, graph.height_px):
        raise ValueError("contour source labels do not match the region graph dimensions")
    graph_labels = tuple(entry.label for entry in graph.palette)
    if labels.label_values != graph_labels:
        raise ValueError("contour source palette does not match the region graph palette")
    label_array = np.frombuffer(labels.pixels, dtype=np.uint8).reshape(
        (labels.height, labels.width)
    )
    active = analysis.assignment >= 0
    graph_region_labels = np.asarray([region.label for region in graph.regions], dtype=np.uint8)
    if np.any(active) and not np.array_equal(
        label_array[active], graph_region_labels[analysis.assignment[active]]
    ):
        raise ValueError("contour source labels do not match the region assignment")
