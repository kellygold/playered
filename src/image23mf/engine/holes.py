"""Enclosed tiny-hole and hollow-ring classification with explicit correction policies."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import heapq
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.labels import LabelField
from image23mf.engine.regions import (
    PhysicalBounds,
    PixelBounds,
    RegionAnalysis,
    RegionGraph,
    RegionLineage,
    analyze_regions,
    derive_region_lineage,
    first_mismatched_region,
)
from image23mf.engine.risks import (
    RiskCode,
    RiskFinding,
    RiskMeasurement,
    RiskMeasurementKey,
    RiskMeasurementRole,
    RiskSeverity,
    RiskUnit,
    default_risk_suggestions,
)

HOLE_ANALYSIS_SCHEMA_VERSION = 1
HOLE_POLICY_SCHEMA_VERSION = 1


class HoleFeatureKind(str, Enum):
    TINY_HOLE = "tiny_hole"
    HOLLOW_RING = "hollow_ring"


class HoleCenterKind(str, Enum):
    ACTIVE_REGION = "active_region"
    TRANSPARENT_VOID = "transparent_void"


class HoleTrigger(str, Enum):
    AREA = "area"
    EQUIVALENT_DIAMETER = "equivalent_diameter"


class HoleCorrectionPolicy(str, Enum):
    REVIEW = "review"
    KEEP = "keep"
    FILL_HOLE = "fill_hole"
    COLLAPSE_RING = "collapse_ring"
    RECOLOR_CENTER = "recolor_center"


class HoleDecisionStatus(str, Enum):
    REVIEW_REQUIRED = "review_required"
    KEPT = "kept"
    CHANGED = "changed"


class HoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class HoleAnalysisSettings(HoleModel):
    maximum_area_mm2: float = Field(ge=0, le=25)
    maximum_equivalent_diameter_mm: float = Field(ge=0, le=10)
    minimum_surviving_ring_width_mm: float = Field(gt=0, le=10)
    maximum_ring_to_center_area_ratio: float = Field(gt=1, le=1000)
    error_ratio: float = Field(gt=0, le=1)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class HoleFeature(HoleModel):
    id: str = Field(pattern=r"^feature_[0-9a-f]{24}$")
    kind: HoleFeatureKind
    center_kind: HoleCenterKind
    center_component_id: str = Field(pattern=r"^(region|void)_[0-9a-f]{24}$")
    center_region_id: Optional[str] = Field(default=None, pattern=r"^region_[0-9a-f]{24}$")
    center_label: Optional[int] = Field(default=None, ge=0, le=255)
    ring_region_id: str = Field(pattern=r"^region_[0-9a-f]{24}$")
    ring_label: int = Field(ge=0, le=255)
    ring_pixel_count: int = Field(gt=0)
    ring_pixel_bounds: PixelBounds
    ring_bounds: PhysicalBounds
    outer_region_ids: tuple[str, ...]
    outer_labels: tuple[int, ...]
    triggers: tuple[HoleTrigger, ...] = Field(min_length=1, max_length=2)
    center_pixel_count: int = Field(gt=0)
    center_area_mm2: float = Field(gt=0)
    center_equivalent_diameter_mm: float = Field(gt=0)
    center_perimeter_mm: float = Field(gt=0)
    center_pixel_bounds: PixelBounds
    center_bounds: PhysicalBounds
    ring_minimum_width_mm: float = Field(gt=0)
    ring_median_width_mm: float = Field(gt=0)
    ring_maximum_width_mm: float = Field(gt=0)
    ring_area_mm2: float = Field(gt=0)
    ring_to_center_area_ratio: float = Field(gt=0)
    ring_perimeter_mm: float = Field(gt=0)

    @model_validator(mode="after")
    def feature_is_canonical(self) -> HoleFeature:
        if self.center_kind == HoleCenterKind.ACTIVE_REGION:
            if self.center_region_id != self.center_component_id or self.center_label is None:
                raise ValueError("active hole centers require matching region identity and label")
        elif self.center_region_id is not None or self.center_label is not None:
            raise ValueError("transparent hole centers cannot carry an active region or label")
        if self.outer_region_ids != tuple(sorted(set(self.outer_region_ids))):
            raise ValueError("hole outer region IDs must be unique and canonical")
        if self.outer_labels != tuple(sorted(set(self.outer_labels))):
            raise ValueError("hole outer labels must be unique and canonical")
        if self.id != _feature_id(self.kind, self.center_component_id, self.ring_region_id):
            raise ValueError("hole feature ID does not match its stable identity")
        return self


class HoleAnalysisSummary(HoleModel):
    feature_count: int = Field(ge=0)
    tiny_hole_count: int = Field(ge=0)
    hollow_ring_count: int = Field(ge=0)
    transparent_center_count: int = Field(ge=0)


class HoleAnalysis(HoleModel):
    schema_version: Literal[1] = HOLE_ANALYSIS_SCHEMA_VERSION
    graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    options: HoleAnalysisSettings
    options_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    features: tuple[HoleFeature, ...]
    summary: HoleAnalysisSummary

    @model_validator(mode="after")
    def analysis_is_canonical(self) -> HoleAnalysis:
        if self.options_fingerprint != self.options.fingerprint():
            raise ValueError("hole options fingerprint does not match settings")
        if self.features != tuple(sorted(self.features, key=lambda item: item.id)):
            raise ValueError("hole features must use canonical ordering")
        if len({feature.id for feature in self.features}) != len(self.features):
            raise ValueError("hole feature IDs must be unique")
        if self.summary != _summary(self.features):
            raise ValueError("hole summary does not match features")
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


class HoleCorrectionRequest(HoleModel):
    schema_version: Literal[1] = HOLE_POLICY_SCHEMA_VERSION
    policy: HoleCorrectionPolicy
    feature_ids: tuple[str, ...] = Field(min_length=1)
    explicit_target_label: Optional[int] = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def request_is_deterministic(self) -> HoleCorrectionRequest:
        if self.feature_ids != tuple(sorted(set(self.feature_ids))):
            raise ValueError("hole feature IDs must be unique and canonical")
        if self.policy == HoleCorrectionPolicy.RECOLOR_CENTER:
            if self.explicit_target_label is None:
                raise ValueError("recolor-center policy requires a target label")
        elif self.explicit_target_label is not None:
            raise ValueError("only recolor-center policy accepts a target label")
        return self


class HoleCorrectionDecision(HoleModel):
    feature_id: str = Field(pattern=r"^feature_[0-9a-f]{24}$")
    policy: HoleCorrectionPolicy
    status: HoleDecisionStatus
    target_label: Optional[int] = Field(default=None, ge=0, le=255)
    target_active: bool
    changed_pixel_count: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class HolePolicyRecord(HoleModel):
    schema_version: Literal[1] = HOLE_POLICY_SCHEMA_VERSION
    before_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: HoleCorrectionRequest
    decisions: tuple[HoleCorrectionDecision, ...]
    changed_pixel_count: int = Field(ge=0)
    lineage: RegionLineage

    @model_validator(mode="after")
    def record_is_consistent(self) -> HolePolicyRecord:
        if self.decisions != tuple(sorted(self.decisions, key=lambda item: item.feature_id)):
            raise ValueError("hole decisions must use canonical feature ordering")
        if tuple(item.feature_id for item in self.decisions) != self.request.feature_ids:
            raise ValueError("hole decisions must cover every requested feature exactly once")
        if self.changed_pixel_count != sum(item.changed_pixel_count for item in self.decisions):
            raise ValueError("hole changed-pixel total does not match decisions")
        if (
            self.lineage.before_graph_fingerprint != self.before_graph_fingerprint
            or self.lineage.after_graph_fingerprint != self.after_graph_fingerprint
        ):
            raise ValueError("hole lineage does not match policy graph fingerprints")
        return self


@dataclass(frozen=True)
class HoleAnalysisOptions:
    maximum_area_mm2: float
    maximum_equivalent_diameter_mm: float
    minimum_surviving_ring_width_mm: float
    maximum_ring_to_center_area_ratio: float = 64.0
    error_ratio: float = 0.5

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.maximum_area_mm2,
                self.maximum_equivalent_diameter_mm,
                self.minimum_surviving_ring_width_mm,
                self.maximum_ring_to_center_area_ratio,
                self.error_ratio,
            )
        ):
            raise ValueError("hole analysis options must be finite")
        self.contract()

    def contract(self) -> HoleAnalysisSettings:
        return HoleAnalysisSettings(
            maximum_area_mm2=self.maximum_area_mm2,
            maximum_equivalent_diameter_mm=self.maximum_equivalent_diameter_mm,
            minimum_surviving_ring_width_mm=self.minimum_surviving_ring_width_mm,
            maximum_ring_to_center_area_ratio=self.maximum_ring_to_center_area_ratio,
            error_ratio=self.error_ratio,
        )


@dataclass(frozen=True)
class AppliedHolePolicy:
    record: HolePolicyRecord
    labels: LabelField
    active: bytes
    analysis: RegionAnalysis

    def __post_init__(self) -> None:
        if self.record.after_graph_fingerprint != self.analysis.graph.fingerprint():
            raise ValueError("applied hole policy does not match its after analysis")
        if len(self.active) != self.labels.width * self.labels.height:
            raise ValueError("applied hole policy active plane has the wrong size")


@dataclass(frozen=True)
class _HoleEvidence:
    feature: HoleFeature
    # Own only the bounded center crop, never a view retaining a full canvas per hole.
    center_crop: np.ndarray
    ring_index: int


def classify_holes(analysis: RegionAnalysis, *, options: HoleAnalysisOptions) -> HoleAnalysis:
    settings = options.contract()
    evidence = _classify_evidence(analysis, settings)
    features = tuple(sorted((item.feature for item in evidence), key=lambda item: item.id))
    return HoleAnalysis(
        graph_fingerprint=analysis.graph.fingerprint(),
        options=settings,
        options_fingerprint=settings.fingerprint(),
        features=features,
        summary=_summary(features),
    )


def validate_hole_analysis(classification: HoleAnalysis, analysis: RegionAnalysis) -> None:
    validate_hole_analysis_against_graph(classification, analysis.graph)
    expected = classify_holes(
        analysis,
        options=HoleAnalysisOptions(**classification.options.model_dump()),
    )
    if classification != expected:
        raise ValueError("hole analysis content does not match its assignment and settings")


def validate_hole_analysis_against_graph(classification: HoleAnalysis, graph: RegionGraph) -> None:
    if classification.graph_fingerprint != graph.fingerprint():
        raise ValueError("hole analysis does not match the region graph fingerprint")
    regions = {region.id: region for region in graph.regions}
    for feature in classification.features:
        ring = regions.get(feature.ring_region_id)
        if ring is None or ring.label != feature.ring_label:
            raise ValueError("hole feature ring reference does not match the graph")
        if feature.center_region_id is not None:
            center = regions.get(feature.center_region_id)
            if center is None or center.label != feature.center_label:
                raise ValueError("hole feature center reference does not match the graph")
        if any(region_id not in regions for region_id in feature.outer_region_ids):
            raise ValueError("hole feature references an unknown outer region")
        expected_outer_labels = tuple(
            sorted({regions[region_id].label for region_id in feature.outer_region_ids})
        )
        if feature.outer_labels != expected_outer_labels:
            raise ValueError("hole feature outer labels do not match the graph")
        bounds = feature.center_pixel_bounds
        if bounds.x + bounds.width > graph.width_px or bounds.y + bounds.height > graph.height_px:
            raise ValueError("hole feature bounds exceed the graph canvas")
        if feature.center_bounds != _physical_bounds(bounds, graph):
            raise ValueError("hole feature physical bounds do not match its pixel bounds")
        ring_bounds = feature.ring_pixel_bounds
        if (
            ring_bounds.x + ring_bounds.width > graph.width_px
            or ring_bounds.y + ring_bounds.height > graph.height_px
        ):
            raise ValueError("hole ring bounds exceed the graph canvas")
        if feature.ring_bounds != _physical_bounds(ring_bounds, graph):
            raise ValueError("hole ring physical bounds do not match its pixel bounds")


def hole_risk_findings(classification: HoleAnalysis, graph: RegionGraph) -> tuple[RiskFinding, ...]:
    validate_hole_analysis_against_graph(classification, graph)
    return tuple(_finding(feature, classification.options) for feature in classification.features)


def apply_hole_policy(
    labels: LabelField,
    analysis: RegionAnalysis,
    classification: HoleAnalysis,
    request: HoleCorrectionRequest,
) -> AppliedHolePolicy:
    validate_hole_analysis(classification, analysis)
    _validate_source_labels(labels, analysis)
    if (
        request.explicit_target_label is not None
        and request.explicit_target_label not in labels.label_values
    ):
        raise ValueError("explicit hole target label is absent from the palette")
    evidence = {
        item.feature.id: item for item in _classify_evidence(analysis, classification.options)
    }
    selected = []
    for feature_id in request.feature_ids:
        item = evidence.get(feature_id)
        if item is None:
            raise ValueError(f"hole request references an unknown feature: {feature_id}")
        if (
            request.policy == HoleCorrectionPolicy.COLLAPSE_RING
            and item.feature.kind != HoleFeatureKind.HOLLOW_RING
        ):
            raise ValueError("collapse-ring policy requires a hollow-ring feature")
        if (
            request.policy == HoleCorrectionPolicy.RECOLOR_CENTER
            and item.feature.center_label == request.explicit_target_label
        ):
            raise ValueError("explicit hole target must differ from the current center label")
        selected.append(item)

    masks = [_policy_mask(item, analysis, request.policy) for item in selected]
    if request.policy not in {HoleCorrectionPolicy.REVIEW, HoleCorrectionPolicy.KEEP}:
        occupied = np.zeros(analysis.assignment.shape, dtype=bool)
        for mask in masks:
            if np.any(occupied & mask):
                raise ValueError("destructive hole corrections cannot overlap in one request")
            occupied |= mask

    current_labels = (
        np.frombuffer(labels.pixels, dtype=np.uint8).copy().reshape((labels.height, labels.width))
    )
    current_active = analysis.assignment >= 0
    updated_labels = current_labels.copy()
    updated_active = current_active.copy()
    decisions = []
    for item, mask in zip(selected, masks):
        feature = item.feature
        if request.policy == HoleCorrectionPolicy.REVIEW:
            decisions.append(
                _decision(
                    feature,
                    request.policy,
                    HoleDecisionStatus.REVIEW_REQUIRED,
                    None,
                    feature.center_kind == HoleCenterKind.ACTIVE_REGION,
                    0,
                    "The enclosed feature remains unchanged until a correction is selected.",
                )
            )
            continue
        if request.policy == HoleCorrectionPolicy.KEEP:
            decisions.append(
                _decision(
                    feature,
                    request.policy,
                    HoleDecisionStatus.KEPT,
                    feature.center_label,
                    feature.center_kind == HoleCenterKind.ACTIVE_REGION,
                    0,
                    "The enclosed feature was explicitly accepted without changing pixels.",
                )
            )
            continue
        target_label, target_active = _policy_target(feature, request, labels.label_values)
        before_labels = updated_labels[mask].copy()
        before_active = updated_active[mask].copy()
        if target_label is not None:
            updated_labels[mask] = target_label
        updated_active[mask] = target_active
        changed = int(
            np.count_nonzero(
                (before_active != target_active)
                | (target_active & (before_labels != updated_labels[mask]))
            )
        )
        decisions.append(
            _decision(
                feature,
                request.policy,
                HoleDecisionStatus.CHANGED,
                target_label,
                target_active,
                changed,
                f"Changed {changed} pixels using the explicit {request.policy.value} policy.",
            )
        )

    result_labels = LabelField(
        width=labels.width,
        height=labels.height,
        label_values=labels.label_values,
        pixels=updated_labels.tobytes(),
    )
    active_bytes = updated_active.astype(np.uint8).tobytes()
    colors = {entry.label: entry.color for entry in analysis.graph.palette}
    after = analyze_regions(
        result_labels,
        colors=colors,
        width_mm=analysis.graph.width_mm,
        height_mm=analysis.graph.height_mm,
        active=active_bytes,
    )
    lineage = derive_region_lineage(analysis, after)
    ordered = tuple(sorted(decisions, key=lambda item: item.feature_id))
    record = HolePolicyRecord(
        before_graph_fingerprint=analysis.graph.fingerprint(),
        after_graph_fingerprint=after.graph.fingerprint(),
        classification_fingerprint=classification.fingerprint(),
        request=request,
        decisions=ordered,
        changed_pixel_count=sum(item.changed_pixel_count for item in ordered),
        lineage=lineage,
    )
    return AppliedHolePolicy(
        record=record,
        labels=result_labels,
        active=active_bytes,
        analysis=after,
    )


def _classify_evidence(
    analysis: RegionAnalysis, settings: HoleAnalysisSettings
) -> tuple[_HoleEvidence, ...]:
    graph = analysis.graph
    region_index = {region.id: index for index, region in enumerate(graph.regions)}
    neighbors = {
        region.id: tuple(region_index[item] for item in region.neighbor_region_ids)
        for region in graph.regions
    }
    void_assignment, voids = _void_components(analysis.assignment)
    ring_void_neighbors = _ring_void_neighbors(analysis.assignment, void_assignment)
    evidence = []
    outer_candidates: dict[tuple[int, int], tuple[int, ...]] = {}

    for center_index, center in enumerate(graph.regions):
        if center.border_contact.touches_canvas_border:
            continue
        adjacent = neighbors[center.id]
        if len(adjacent) != 1:
            continue
        ring_index = adjacent[0]
        ring = graph.regions[ring_index]
        # Many photo speckles share one surrounding region. Rank its neighbors once
        # per label; two candidates suffice because only this center is excluded.
        candidate_key = (ring_index, center.label)
        if candidate_key not in outer_candidates:
            outer_candidates[candidate_key] = tuple(
                heapq.nsmallest(
                    2,
                    (
                        item
                        for item in neighbors[ring.id]
                        if graph.regions[item].label == center.label
                    ),
                    key=lambda index: _outer_region_order(index, graph),
                )
            )
        outer_index = next(
            (item for item in outer_candidates[candidate_key] if item != center_index), None
        )
        if outer_index is None:
            continue
        mask = analysis.assignment == center_index
        full_ring_mask = analysis.assignment == ring_index
        ring_wall_widths = _ring_wall_widths(
            full_ring_mask,
            mask,
            center.pixel_bounds,
            graph.pixel_width_mm,
            graph.pixel_height_mm,
        )
        local_ring_bounds, local_ring_pixel_count = _local_ring_geometry(
            full_ring_mask, center.pixel_bounds, ring_wall_widths, graph
        )
        feature = _hole_feature(
            graph,
            settings,
            center_kind=HoleCenterKind.ACTIVE_REGION,
            center_component_id=center.id,
            center_region_id=center.id,
            center_label=center.label,
            center_mask=mask,
            center_pixel_count=center.pixel_count,
            center_perimeter_mm=center.perimeter_mm,
            center_bounds=center.pixel_bounds,
            ring_index=ring_index,
            ring_wall_widths=ring_wall_widths,
            local_ring_bounds=local_ring_bounds,
            local_ring_pixel_count=local_ring_pixel_count,
            outer_indices=(outer_index,),
            has_outer_void=bool(ring_void_neighbors.get(ring_index)),
        )
        if feature is not None:
            evidence.append(_hole_evidence(feature, mask, ring_index))

    for void_index, void in enumerate(voids):
        if void.touches_canvas_border or len(void.adjacent_ring_indices) != 1:
            continue
        ring_index = void.adjacent_ring_indices[0]
        ring = graph.regions[ring_index]
        all_outer_indices = neighbors[ring.id]
        outer_indices = (
            (_representative_outer(all_outer_indices, graph),) if all_outer_indices else ()
        )
        other_voids = set(ring_void_neighbors.get(ring_index, ())) - {void_index}
        mask = void_assignment == void_index
        full_ring_mask = analysis.assignment == ring_index
        ring_wall_widths = _ring_wall_widths(
            full_ring_mask,
            mask,
            void.pixel_bounds,
            graph.pixel_width_mm,
            graph.pixel_height_mm,
        )
        local_ring_bounds, local_ring_pixel_count = _local_ring_geometry(
            full_ring_mask, void.pixel_bounds, ring_wall_widths, graph
        )
        feature = _hole_feature(
            graph,
            settings,
            center_kind=HoleCenterKind.TRANSPARENT_VOID,
            center_component_id=void.id,
            center_region_id=None,
            center_label=None,
            center_mask=mask,
            center_pixel_count=void.pixel_count,
            center_perimeter_mm=void.perimeter_mm,
            center_bounds=void.pixel_bounds,
            ring_index=ring_index,
            ring_wall_widths=ring_wall_widths,
            local_ring_bounds=local_ring_bounds,
            local_ring_pixel_count=local_ring_pixel_count,
            outer_indices=outer_indices,
            has_outer_void=bool(other_voids),
        )
        if feature is not None:
            evidence.append(_hole_evidence(feature, mask, ring_index))
    normalized = tuple(
        _as_tiny_hole(item)
        if item.feature.kind == HoleFeatureKind.HOLLOW_RING
        and any(
            other is not item
            and _bounds_overlap(
                item.feature.ring_pixel_bounds,
                other.feature.center_pixel_bounds,
            )
            for other in evidence
        )
        else item
        for item in evidence
    )
    return tuple(sorted(normalized, key=lambda item: item.feature.id))


def _hole_evidence(feature: HoleFeature, mask: np.ndarray, ring_index: int) -> _HoleEvidence:
    bounds = feature.center_pixel_bounds
    crop = mask[
        bounds.y : bounds.y + bounds.height,
        bounds.x : bounds.x + bounds.width,
    ].copy()
    return _HoleEvidence(feature, crop, ring_index)


@dataclass(frozen=True)
class _VoidComponent:
    id: str
    pixel_count: int
    perimeter_mm: float
    pixel_bounds: PixelBounds
    touches_canvas_border: bool
    adjacent_ring_indices: tuple[int, ...]


def _void_components(assignment: np.ndarray) -> tuple[np.ndarray, tuple[_VoidComponent, ...]]:
    inactive = assignment < 0
    labels = np.full(assignment.shape, -1, dtype=np.int32)
    if not np.any(inactive):
        return labels, ()
    voids = []
    component = 0
    height, width = assignment.shape
    for start_y, start_x in zip(*np.nonzero(inactive)):
        if labels[start_y, start_x] >= 0:
            continue
        labels[start_y, start_x] = component
        stack = [(int(start_y), int(start_x))]
        pixels = []
        adjacent = set()
        perimeter_edges = 0
        while stack:
            y, x = stack.pop()
            pixels.append((y, x))
            for dy, dx in ((-1, 0), (0, 1), (1, 0), (0, -1)):
                ny, nx = y + dy, x + dx
                if not (0 <= ny < height and 0 <= nx < width):
                    perimeter_edges += 1
                elif inactive[ny, nx]:
                    if labels[ny, nx] < 0:
                        labels[ny, nx] = component
                        stack.append((ny, nx))
                else:
                    perimeter_edges += 1
                    adjacent.add(int(assignment[ny, nx]))
        ys = [item[0] for item in pixels]
        xs = [item[1] for item in pixels]
        bounds = PixelBounds(
            x=min(xs),
            y=min(ys),
            width=max(xs) - min(xs) + 1,
            height=max(ys) - min(ys) + 1,
        )
        touches = min(xs) == 0 or min(ys) == 0 or max(xs) == width - 1 or max(ys) == height - 1
        component_mask = labels == component
        voids.append(
            _VoidComponent(
                id=_void_id(component_mask),
                pixel_count=len(pixels),
                perimeter_mm=_mask_perimeter(component_mask, 1.0, 1.0),
                pixel_bounds=bounds,
                touches_canvas_border=touches,
                adjacent_ring_indices=tuple(sorted(adjacent)),
            )
        )
        component += 1
    return labels, tuple(voids)


def _ring_void_neighbors(
    assignment: np.ndarray, void_assignment: np.ndarray
) -> dict[int, tuple[int, ...]]:
    result: dict[int, set[int]] = {}
    for first, second in (
        (assignment[:, :-1], void_assignment[:, 1:]),
        (assignment[:, 1:], void_assignment[:, :-1]),
        (assignment[:-1, :], void_assignment[1:, :]),
        (assignment[1:, :], void_assignment[:-1, :]),
    ):
        valid = (first >= 0) & (second >= 0)
        for ring, void in zip(first[valid].tolist(), second[valid].tolist()):
            result.setdefault(int(ring), set()).add(int(void))
    return {ring: tuple(sorted(voids)) for ring, voids in result.items()}


def _hole_feature(
    graph: RegionGraph,
    settings: HoleAnalysisSettings,
    *,
    center_kind: HoleCenterKind,
    center_component_id: str,
    center_region_id: Optional[str],
    center_label: Optional[int],
    center_mask: np.ndarray,
    center_pixel_count: int,
    center_perimeter_mm: float,
    center_bounds: PixelBounds,
    ring_index: int,
    ring_wall_widths: np.ndarray,
    local_ring_bounds: PixelBounds,
    local_ring_pixel_count: int,
    outer_indices: tuple[int, ...],
    has_outer_void: bool,
) -> Optional[HoleFeature]:
    pixel_area = graph.pixel_width_mm * graph.pixel_height_mm
    area = center_pixel_count * pixel_area
    diameter = 2 * math.sqrt(area / math.pi)
    triggers = []
    if settings.maximum_area_mm2 > 0 and _below(area, settings.maximum_area_mm2):
        triggers.append(HoleTrigger.AREA)
    if settings.maximum_equivalent_diameter_mm > 0 and _below(
        diameter, settings.maximum_equivalent_diameter_mm
    ):
        triggers.append(HoleTrigger.EQUIVALENT_DIAMETER)
    if not triggers:
        return None
    ring = graph.regions[ring_index]
    ring_area = local_ring_pixel_count * pixel_area
    ring_ratio = ring_area / area
    has_outer = bool(outer_indices) or has_outer_void
    kind = (
        HoleFeatureKind.HOLLOW_RING
        if (
            not ring.border_contact.touches_canvas_border
            and has_outer
            and float(np.median(ring_wall_widths)) >= settings.minimum_surviving_ring_width_mm
            and ring_ratio <= settings.maximum_ring_to_center_area_ratio
        )
        else HoleFeatureKind.TINY_HOLE
    )
    outer_regions = tuple(sorted(graph.regions[index].id for index in outer_indices))
    outer_labels = tuple(sorted({graph.regions[index].label for index in outer_indices}))
    return HoleFeature(
        id=_feature_id(kind, center_component_id, ring.id),
        kind=kind,
        center_kind=center_kind,
        center_component_id=center_component_id,
        center_region_id=center_region_id,
        center_label=center_label,
        ring_region_id=ring.id,
        ring_label=ring.label,
        ring_pixel_count=local_ring_pixel_count,
        ring_pixel_bounds=local_ring_bounds,
        ring_bounds=_physical_bounds(local_ring_bounds, graph),
        outer_region_ids=outer_regions,
        outer_labels=outer_labels,
        triggers=tuple(triggers),
        center_pixel_count=center_pixel_count,
        center_area_mm2=area,
        center_equivalent_diameter_mm=diameter,
        center_perimeter_mm=(
            _mask_perimeter(center_mask, graph.pixel_width_mm, graph.pixel_height_mm)
            if center_kind == HoleCenterKind.TRANSPARENT_VOID
            else center_perimeter_mm
        ),
        center_pixel_bounds=center_bounds,
        center_bounds=_physical_bounds(center_bounds, graph),
        ring_minimum_width_mm=float(np.min(ring_wall_widths)),
        ring_median_width_mm=float(np.median(ring_wall_widths)),
        ring_maximum_width_mm=float(np.max(ring_wall_widths)),
        ring_area_mm2=ring_area,
        ring_to_center_area_ratio=ring_ratio,
        ring_perimeter_mm=ring.perimeter_mm,
    )


def _as_tiny_hole(evidence: _HoleEvidence) -> _HoleEvidence:
    payload = evidence.feature.model_dump(mode="json")
    payload["kind"] = HoleFeatureKind.TINY_HOLE.value
    payload["id"] = _feature_id(
        HoleFeatureKind.TINY_HOLE,
        evidence.feature.center_component_id,
        evidence.feature.ring_region_id,
    )
    return _HoleEvidence(
        feature=HoleFeature.model_validate(payload),
        center_crop=evidence.center_crop,
        ring_index=evidence.ring_index,
    )


def _finding(feature: HoleFeature, settings: HoleAnalysisSettings) -> RiskFinding:
    code = RiskCode(feature.kind.value)
    ratios = []
    measurements = [
        RiskMeasurement(
            key=RiskMeasurementKey.HOLE_AREA,
            role=RiskMeasurementRole.MEASURED,
            value=feature.center_area_mm2,
            unit=RiskUnit.MM2,
        ),
        RiskMeasurement(
            key=RiskMeasurementKey.EQUIVALENT_DIAMETER,
            role=RiskMeasurementRole.MEASURED,
            value=feature.center_equivalent_diameter_mm,
            unit=RiskUnit.MM,
        ),
        RiskMeasurement(
            key=RiskMeasurementKey.PERIMETER,
            role=RiskMeasurementRole.MEASURED,
            value=feature.center_perimeter_mm,
            unit=RiskUnit.MM,
        ),
        RiskMeasurement(
            key=RiskMeasurementKey.MINIMUM_WIDTH,
            role=RiskMeasurementRole.CONTEXT,
            value=feature.ring_minimum_width_mm,
            unit=RiskUnit.MM,
        ),
        RiskMeasurement(
            key=RiskMeasurementKey.MEDIAN_WIDTH,
            role=RiskMeasurementRole.CONTEXT,
            value=feature.ring_median_width_mm,
            unit=RiskUnit.MM,
        ),
        RiskMeasurement(
            key=RiskMeasurementKey.MAXIMUM_WIDTH,
            role=RiskMeasurementRole.CONTEXT,
            value=feature.ring_maximum_width_mm,
            unit=RiskUnit.MM,
        ),
    ]
    if settings.maximum_area_mm2 > 0:
        ratios.append(feature.center_area_mm2 / settings.maximum_area_mm2)
        measurements.append(
            RiskMeasurement(
                key=RiskMeasurementKey.HOLE_AREA,
                role=RiskMeasurementRole.THRESHOLD,
                value=settings.maximum_area_mm2,
                unit=RiskUnit.MM2,
            )
        )
    if settings.maximum_equivalent_diameter_mm > 0:
        ratios.append(
            feature.center_equivalent_diameter_mm / settings.maximum_equivalent_diameter_mm
        )
        measurements.append(
            RiskMeasurement(
                key=RiskMeasurementKey.EQUIVALENT_DIAMETER,
                role=RiskMeasurementRole.THRESHOLD,
                value=settings.maximum_equivalent_diameter_mm,
                unit=RiskUnit.MM,
            )
        )
    titles = {
        RiskCode.TINY_HOLE: "Tiny enclosed hole",
        RiskCode.HOLLOW_RING: "Hollow ring may remain after its center vanishes",
    }
    explanations = {
        RiskCode.TINY_HOLE: (
            "This enclosed center falls below the configured physical hole threshold. Review, "
            "fill, or explicitly recolor it; threshold-equal and larger holes are retained."
        ),
        RiskCode.HOLLOW_RING: (
            "This small enclosed center is surrounded by a ring wide enough to remain printable, "
            "which can leave an awkward empty outline if the center disappears."
        ),
    }
    affected_regions = tuple(
        sorted(
            {
                feature.ring_region_id,
                *(() if feature.center_region_id is None else (feature.center_region_id,)),
                *feature.outer_region_ids,
            }
        )
    )
    affected_labels = tuple(
        sorted(
            {
                feature.ring_label,
                *(() if feature.center_label is None else (feature.center_label,)),
                *feature.outer_labels,
            }
        )
    )
    return RiskFinding(
        code=code,
        feature_key=feature.id,
        severity=(
            RiskSeverity.ERROR
            if ratios and min(ratios) <= settings.error_ratio
            else RiskSeverity.WARNING
        ),
        title=titles[code],
        explanation=explanations[code],
        affected_region_ids=affected_regions,
        affected_labels=affected_labels,
        affected_bounds=(feature.center_bounds,),
        measurements=tuple(measurements),
        suggestions=default_risk_suggestions(code),
        classifier_id="enclosed-hole-topology",
        classifier_version="1",
    )


def _policy_mask(
    evidence: _HoleEvidence,
    analysis: RegionAnalysis,
    policy: HoleCorrectionPolicy,
) -> np.ndarray:
    if policy == HoleCorrectionPolicy.COLLAPSE_RING:
        bounds = evidence.feature.ring_pixel_bounds
        mask = np.zeros(analysis.assignment.shape, dtype=bool)
        crop = analysis.assignment[
            bounds.y : bounds.y + bounds.height,
            bounds.x : bounds.x + bounds.width,
        ]
        mask[
            bounds.y : bounds.y + bounds.height,
            bounds.x : bounds.x + bounds.width,
        ] = crop == evidence.ring_index
        return mask
    if policy in {HoleCorrectionPolicy.FILL_HOLE, HoleCorrectionPolicy.RECOLOR_CENTER}:
        bounds = evidence.feature.center_pixel_bounds
        mask = np.zeros(analysis.assignment.shape, dtype=bool)
        mask[
            bounds.y : bounds.y + bounds.height,
            bounds.x : bounds.x + bounds.width,
        ] = evidence.center_crop
        return mask
    return np.zeros(analysis.assignment.shape, dtype=bool)


def _policy_target(
    feature: HoleFeature,
    request: HoleCorrectionRequest,
    label_values: tuple[int, ...],
) -> tuple[Optional[int], bool]:
    if request.policy == HoleCorrectionPolicy.FILL_HOLE:
        return feature.ring_label, True
    if request.policy == HoleCorrectionPolicy.RECOLOR_CENTER:
        return request.explicit_target_label, True
    if request.policy == HoleCorrectionPolicy.COLLAPSE_RING:
        if feature.center_kind == HoleCenterKind.TRANSPARENT_VOID:
            return min(label_values), False
        return feature.center_label, True
    raise ValueError("non-destructive hole policies do not have a pixel target")


def _decision(
    feature: HoleFeature,
    policy: HoleCorrectionPolicy,
    status: HoleDecisionStatus,
    target_label: Optional[int],
    target_active: bool,
    changed_pixel_count: int,
    reason: str,
) -> HoleCorrectionDecision:
    return HoleCorrectionDecision(
        feature_id=feature.id,
        policy=policy,
        status=status,
        target_label=target_label,
        target_active=target_active,
        changed_pixel_count=changed_pixel_count,
        reason=reason,
    )


def _feature_id(kind: HoleFeatureKind, center_id: str, ring_id: str) -> str:
    payload = json.dumps(
        {"center": center_id, "kind": kind.value, "ring": ring_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"feature_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _void_id(mask: np.ndarray) -> str:
    rows = []
    for y in range(mask.shape[0]):
        padded = np.pad(mask[y].astype(np.int8), 1)
        transitions = np.diff(padded)
        starts = np.flatnonzero(transitions == 1)
        ends = np.flatnonzero(transitions == -1)
        rows.extend((y, int(start), int(end)) for start, end in zip(starts, ends))
    payload = json.dumps(rows, separators=(",", ":"))
    return f"void_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _local_ring_geometry(
    full_ring_mask: np.ndarray,
    center_bounds: PixelBounds,
    ring_wall_widths: np.ndarray,
    graph: RegionGraph,
) -> tuple[PixelBounds, int]:
    wall = float(np.median(ring_wall_widths))
    margin_x = max(1, math.ceil(wall / graph.pixel_width_mm))
    margin_y = max(1, math.ceil(wall / graph.pixel_height_mm))
    x0 = max(0, center_bounds.x - margin_x)
    y0 = max(0, center_bounds.y - margin_y)
    x1 = min(graph.width_px, center_bounds.x + center_bounds.width + margin_x)
    y1 = min(graph.height_px, center_bounds.y + center_bounds.height + margin_y)
    crop = full_ring_mask[y0:y1, x0:x1]
    ys, xs = np.nonzero(crop)
    bounds = PixelBounds(
        x=x0 + int(xs.min()),
        y=y0 + int(ys.min()),
        width=int(xs.max() - xs.min() + 1),
        height=int(ys.max() - ys.min() + 1),
    )
    return bounds, int(xs.size)


def _representative_outer(indices: tuple[int, ...], graph: RegionGraph) -> int:
    return min(indices, key=lambda index: _outer_region_order(index, graph))


def _outer_region_order(index: int, graph: RegionGraph) -> tuple[bool, int, str]:
    region = graph.regions[index]
    return not region.border_contact.touches_canvas_border, -region.pixel_count, region.id


def _bounds_overlap(first: PixelBounds, second: PixelBounds) -> bool:
    return not (
        first.x + first.width <= second.x
        or second.x + second.width <= first.x
        or first.y + first.height <= second.y
        or second.y + second.height <= first.y
    )


def _ring_wall_widths(
    ring_mask: np.ndarray,
    center_mask: np.ndarray,
    center_bounds: PixelBounds,
    pixel_width: float,
    pixel_height: float,
) -> np.ndarray:
    crop = center_mask[
        center_bounds.y : center_bounds.y + center_bounds.height,
        center_bounds.x : center_bounds.x + center_bounds.width,
    ]
    adjacent: set[tuple[int, int]] = set()
    for local_y, local_x in zip(*np.nonzero(crop)):
        y = center_bounds.y + int(local_y)
        x = center_bounds.x + int(local_x)
        for dy, dx in ((-1, 0), (0, 1), (1, 0), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < ring_mask.shape[0] and 0 <= nx < ring_mask.shape[1] and ring_mask[ny, nx]:
                adjacent.add((ny, nx))
    if not adjacent:  # pragma: no cover - enclosure topology requires contact
        raise ValueError("hole center does not touch its surrounding ring")
    widths = []
    for y, x in sorted(adjacent):
        left = x
        while left > 0 and ring_mask[y, left - 1]:
            left -= 1
        right = x
        while right + 1 < ring_mask.shape[1] and ring_mask[y, right + 1]:
            right += 1
        top = y
        while top > 0 and ring_mask[top - 1, x]:
            top -= 1
        bottom = y
        while bottom + 1 < ring_mask.shape[0] and ring_mask[bottom + 1, x]:
            bottom += 1
        widths.append(min((right - left + 1) * pixel_width, (bottom - top + 1) * pixel_height))
    return np.asarray(widths, dtype=np.float64)


def _mask_perimeter(mask: np.ndarray, pixel_width: float, pixel_height: float) -> float:
    horizontal = int(np.count_nonzero(mask[:, :-1] != mask[:, 1:])) * pixel_height
    vertical = int(np.count_nonzero(mask[:-1, :] != mask[1:, :])) * pixel_width
    border = (
        int(np.count_nonzero(mask[:, 0])) * pixel_height
        + int(np.count_nonzero(mask[:, -1])) * pixel_height
        + int(np.count_nonzero(mask[0, :])) * pixel_width
        + int(np.count_nonzero(mask[-1, :])) * pixel_width
    )
    return horizontal + vertical + border


def _physical_bounds(bounds: PixelBounds, graph: RegionGraph) -> PhysicalBounds:
    return PhysicalBounds(
        x_mm=bounds.x * graph.pixel_width_mm,
        y_mm=bounds.y * graph.pixel_height_mm,
        width_mm=bounds.width * graph.pixel_width_mm,
        height_mm=bounds.height * graph.pixel_height_mm,
    )


def _validate_source_labels(labels: LabelField, analysis: RegionAnalysis) -> None:
    graph = analysis.graph
    if (labels.width, labels.height) != (graph.width_px, graph.height_px):
        raise ValueError("hole source labels must match the region graph dimensions")
    if set(labels.label_values) != {entry.label for entry in graph.palette}:
        raise ValueError("hole source labels must match the region graph palette")
    mismatch = first_mismatched_region(labels, analysis)
    if mismatch is not None:
        raise ValueError(f"hole source labels do not match region: {mismatch.id}")


def _summary(features: tuple[HoleFeature, ...]) -> HoleAnalysisSummary:
    return HoleAnalysisSummary(
        feature_count=len(features),
        tiny_hole_count=sum(item.kind == HoleFeatureKind.TINY_HOLE for item in features),
        hollow_ring_count=sum(item.kind == HoleFeatureKind.HOLLOW_RING for item in features),
        transparent_center_count=sum(
            item.center_kind == HoleCenterKind.TRANSPARENT_VOID for item in features
        ),
    )


def _below(value: float, threshold: float) -> bool:
    return value < threshold - max(1e-12, threshold * 1e-9)
