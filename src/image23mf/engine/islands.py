"""Physical small-island classification and deterministic merge policies."""

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
from image23mf.engine.palette import delta_e_76, rgb_to_lab
from image23mf.engine.regions import (
    PhysicalBounds,
    RegionAnalysis,
    RegionGraph,
    RegionLineage,
    analyze_regions,
    derive_region_lineage,
    first_mismatched_region,
)
from image23mf.engine.risks import (
    RiskActionKind,
    RiskCode,
    RiskFinding,
    RiskMeasurement,
    RiskMeasurementKey,
    RiskMeasurementRole,
    RiskSeverity,
    RiskUnit,
    default_risk_suggestions,
)

ISLAND_ANALYSIS_SCHEMA_VERSION = 1
ISLAND_POLICY_SCHEMA_VERSION = 1


class IslandCandidateStatus(str, Enum):
    RISK = "risk"
    LONG_LINE_EXEMPT = "long_line_exempt"


class IslandTrigger(str, Enum):
    AREA = "area"
    EQUIVALENT_DIAMETER = "equivalent_diameter"


class IslandMergePolicy(str, Enum):
    REVIEW = "review"
    KEEP = "keep"
    DOMINANT_NEIGHBOR = "dominant_neighbor"
    PERCEPTUAL_NEIGHBOR = "perceptual_neighbor"
    EXPLICIT_COLOR = "explicit_color"


class IslandDecisionStatus(str, Enum):
    REVIEW_REQUIRED = "review_required"
    KEPT = "kept"
    MERGED = "merged"
    NO_ELIGIBLE_NEIGHBOR = "no_eligible_neighbor"


class IslandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class IslandAnalysisSettings(IslandModel):
    minimum_area_mm2: float = Field(ge=0, le=25)
    minimum_equivalent_diameter_mm: float = Field(ge=0, le=10)
    preserve_long_lines: bool
    long_line_minimum_aspect_ratio: float = Field(ge=1, le=1000)
    long_line_minimum_length_mm: float = Field(ge=0, le=1000)
    error_ratio: float = Field(gt=0, le=1)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class IslandNeighbor(IslandModel):
    region_id: str = Field(pattern=r"^region_[0-9a-f]{24}$")
    label: int = Field(ge=0, le=255)
    color: str = Field(pattern=r"^#[0-9A-F]{6}$")
    boundary_edge_count: int = Field(gt=0)
    boundary_length_mm: float = Field(gt=0)
    delta_e: float = Field(ge=0)


class IslandCandidate(IslandModel):
    region_id: str = Field(pattern=r"^region_[0-9a-f]{24}$")
    label: int = Field(ge=0, le=255)
    color: str = Field(pattern=r"^#[0-9A-F]{6}$")
    status: IslandCandidateStatus
    triggers: tuple[IslandTrigger, ...] = Field(min_length=1, max_length=2)
    area_mm2: float = Field(gt=0)
    equivalent_diameter_mm: float = Field(gt=0)
    perimeter_mm: float = Field(gt=0)
    compactness: float = Field(ge=0)
    bounds: PhysicalBounds
    bbox_length_mm: float = Field(gt=0)
    bbox_width_mm: float = Field(gt=0)
    bbox_aspect_ratio: float = Field(ge=1)
    touches_canvas_border: bool
    neighbors: tuple[IslandNeighbor, ...]


class IslandAnalysisSummary(IslandModel):
    candidate_count: int = Field(ge=0)
    risk_count: int = Field(ge=0)
    long_line_exempt_count: int = Field(ge=0)


class IslandAnalysis(IslandModel):
    schema_version: Literal[1] = ISLAND_ANALYSIS_SCHEMA_VERSION
    graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    options: IslandAnalysisSettings
    options_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: tuple[IslandCandidate, ...]
    summary: IslandAnalysisSummary

    @model_validator(mode="after")
    def analysis_is_canonical(self) -> IslandAnalysis:
        if self.options_fingerprint != self.options.fingerprint():
            raise ValueError("island options fingerprint does not match settings")
        if self.candidates != tuple(sorted(self.candidates, key=lambda item: item.region_id)):
            raise ValueError("island candidates must use canonical region ordering")
        if len({candidate.region_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("island candidate region IDs must be unique")
        expected_summary = _island_summary(self.candidates)
        if self.summary != expected_summary:
            raise ValueError("island summary does not match candidates")
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


class IslandMergeRequest(IslandModel):
    schema_version: Literal[1] = ISLAND_POLICY_SCHEMA_VERSION
    policy: IslandMergePolicy
    region_ids: tuple[str, ...] = Field(min_length=1)
    explicit_target_label: Optional[int] = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def request_is_deterministic(self) -> IslandMergeRequest:
        if self.region_ids != tuple(sorted(set(self.region_ids))):
            raise ValueError("island request region IDs must be unique and canonically sorted")
        if self.policy == IslandMergePolicy.EXPLICIT_COLOR:
            if self.explicit_target_label is None:
                raise ValueError("explicit-color policy requires a target label")
        elif self.explicit_target_label is not None:
            raise ValueError("only explicit-color policy accepts a target label")
        return self


class IslandMergeDecision(IslandModel):
    region_id: str = Field(pattern=r"^region_[0-9a-f]{24}$")
    policy: IslandMergePolicy
    status: IslandDecisionStatus
    source_label: int = Field(ge=0, le=255)
    target_label: Optional[int] = Field(default=None, ge=0, le=255)
    target_region_id: Optional[str] = Field(default=None, pattern=r"^region_[0-9a-f]{24}$")
    changed_pixel_count: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class IslandPolicyRecord(IslandModel):
    schema_version: Literal[1] = ISLAND_POLICY_SCHEMA_VERSION
    before_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: IslandMergeRequest
    decisions: tuple[IslandMergeDecision, ...]
    changed_pixel_count: int = Field(ge=0)
    lineage: RegionLineage

    @model_validator(mode="after")
    def record_is_consistent(self) -> IslandPolicyRecord:
        if self.decisions != tuple(sorted(self.decisions, key=lambda item: item.region_id)):
            raise ValueError("island decisions must use canonical region ordering")
        if tuple(decision.region_id for decision in self.decisions) != self.request.region_ids:
            raise ValueError("island decisions must cover every requested region exactly once")
        if self.changed_pixel_count != sum(
            decision.changed_pixel_count for decision in self.decisions
        ):
            raise ValueError("island changed-pixel total does not match decisions")
        if (
            self.lineage.before_graph_fingerprint != self.before_graph_fingerprint
            or self.lineage.after_graph_fingerprint != self.after_graph_fingerprint
        ):
            raise ValueError("island lineage does not match policy graph fingerprints")
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
class IslandAnalysisOptions:
    minimum_area_mm2: float
    minimum_equivalent_diameter_mm: float
    preserve_long_lines: bool = True
    long_line_minimum_aspect_ratio: float = 6.0
    long_line_minimum_length_mm: float = 1.6
    error_ratio: float = 0.25

    def __post_init__(self) -> None:
        values = (
            self.minimum_area_mm2,
            self.minimum_equivalent_diameter_mm,
            self.long_line_minimum_aspect_ratio,
            self.long_line_minimum_length_mm,
            self.error_ratio,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("island analysis options must be finite")
        self.contract()

    def contract(self) -> IslandAnalysisSettings:
        return IslandAnalysisSettings(
            minimum_area_mm2=self.minimum_area_mm2,
            minimum_equivalent_diameter_mm=self.minimum_equivalent_diameter_mm,
            preserve_long_lines=self.preserve_long_lines,
            long_line_minimum_aspect_ratio=self.long_line_minimum_aspect_ratio,
            long_line_minimum_length_mm=self.long_line_minimum_length_mm,
            error_ratio=self.error_ratio,
        )

    def fingerprint(self) -> str:
        return self.contract().fingerprint()


@dataclass(frozen=True)
class AppliedIslandPolicy:
    record: IslandPolicyRecord
    labels: LabelField
    analysis: RegionAnalysis

    def __post_init__(self) -> None:
        if self.record.after_graph_fingerprint != self.analysis.graph.fingerprint():
            raise ValueError("applied island policy does not match its after analysis")


def classify_small_islands(
    analysis: RegionAnalysis, *, options: IslandAnalysisOptions
) -> IslandAnalysis:
    """Classify physically small components without changing any label pixel."""

    return _classify_small_islands_graph(analysis.graph, options)


def _classify_small_islands_graph(
    graph: RegionGraph, options: IslandAnalysisOptions
) -> IslandAnalysis:
    neighbor_map = _neighbor_map(graph)
    candidates = []
    for region in graph.regions:
        equivalent_diameter = 2 * math.sqrt(region.area_mm2 / math.pi)
        triggers = []
        if options.minimum_area_mm2 > 0 and region.area_mm2 < options.minimum_area_mm2:
            triggers.append(IslandTrigger.AREA)
        if (
            options.minimum_equivalent_diameter_mm > 0
            and equivalent_diameter < options.minimum_equivalent_diameter_mm
        ):
            triggers.append(IslandTrigger.EQUIVALENT_DIAMETER)
        if not triggers:
            continue
        bbox_length = max(region.physical_bounds.width_mm, region.physical_bounds.height_mm)
        bbox_width = min(region.physical_bounds.width_mm, region.physical_bounds.height_mm)
        bbox_aspect = bbox_length / bbox_width
        exempt = (
            options.preserve_long_lines
            and bbox_aspect >= options.long_line_minimum_aspect_ratio
            and bbox_length >= options.long_line_minimum_length_mm
        )
        neighbors = neighbor_map.get(region.id, ())
        candidate = IslandCandidate(
            region_id=region.id,
            label=region.label,
            color=region.color,
            status=(
                IslandCandidateStatus.LONG_LINE_EXEMPT if exempt else IslandCandidateStatus.RISK
            ),
            triggers=tuple(triggers),
            area_mm2=region.area_mm2,
            equivalent_diameter_mm=equivalent_diameter,
            perimeter_mm=region.perimeter_mm,
            compactness=region.compactness,
            bounds=region.physical_bounds,
            bbox_length_mm=bbox_length,
            bbox_width_mm=bbox_width,
            bbox_aspect_ratio=bbox_aspect,
            touches_canvas_border=region.border_contact.touches_canvas_border,
            neighbors=neighbors,
        )
        candidates.append(candidate)
    ordered = tuple(sorted(candidates, key=lambda item: item.region_id))
    settings = options.contract()
    return IslandAnalysis(
        graph_fingerprint=graph.fingerprint(),
        options=settings,
        options_fingerprint=settings.fingerprint(),
        candidates=ordered,
        summary=_island_summary(ordered),
    )


def island_risk_findings(
    classification: IslandAnalysis, graph: RegionGraph
) -> tuple[RiskFinding, ...]:
    """Materialize risk findings only when composing a risk report."""

    validate_island_analysis_against_graph(classification, graph)
    options = IslandAnalysisOptions(**classification.options.model_dump())
    return tuple(
        _island_finding(candidate, options, len(graph.palette))
        for candidate in classification.candidates
        if candidate.status == IslandCandidateStatus.RISK
    )


def validate_island_analysis_against_graph(
    classification: IslandAnalysis, graph: RegionGraph
) -> None:
    if classification.graph_fingerprint != graph.fingerprint():
        raise ValueError("island analysis does not match the region graph fingerprint")
    expected = _classify_small_islands_graph(
        graph,
        IslandAnalysisOptions(**classification.options.model_dump()),
    )
    if classification != expected:
        raise ValueError("island analysis content does not match the region graph and settings")


def apply_island_policy(
    labels: LabelField,
    analysis: RegionAnalysis,
    classification: IslandAnalysis,
    request: IslandMergeRequest,
) -> AppliedIslandPolicy:
    """Apply selected island decisions simultaneously on the exact source assignment."""

    validate_island_analysis_against_graph(classification, analysis.graph)
    _validate_source_labels(labels, analysis)
    candidates = {candidate.region_id: candidate for candidate in classification.candidates}
    selected = set(request.region_ids)
    for region_id in request.region_ids:
        candidate = candidates.get(region_id)
        if candidate is None:
            raise ValueError(f"island request references an unclassified region: {region_id}")
        if candidate.status != IslandCandidateStatus.RISK:
            raise ValueError(f"long-line-exempt region cannot be merged as an island: {region_id}")
    if request.explicit_target_label is not None:
        if request.explicit_target_label not in labels.label_values:
            raise ValueError("explicit island target label is absent from the palette")
        if any(
            candidates[region_id].label == request.explicit_target_label
            for region_id in request.region_ids
        ):
            raise ValueError("explicit island target must differ from every source label")

    graph_index = {region.id: index for index, region in enumerate(analysis.graph.regions)}
    updated = (
        np.frombuffer(labels.pixels, dtype=np.uint8).copy().reshape((labels.height, labels.width))
    )
    decisions = []
    for region_id in request.region_ids:
        candidate = candidates[region_id]
        target = _policy_target(candidate, request, selected)
        if request.policy == IslandMergePolicy.REVIEW:
            decisions.append(
                _decision(
                    candidate,
                    request.policy,
                    IslandDecisionStatus.REVIEW_REQUIRED,
                    "The region remains unchanged until a merge policy is selected.",
                )
            )
            continue
        if request.policy == IslandMergePolicy.KEEP:
            decisions.append(
                _decision(
                    candidate,
                    request.policy,
                    IslandDecisionStatus.KEPT,
                    "The measured island was explicitly accepted without changing labels.",
                )
            )
            continue
        if target is None:
            decisions.append(
                _decision(
                    candidate,
                    request.policy,
                    IslandDecisionStatus.NO_ELIGIBLE_NEIGHBOR,
                    "No touching neighbor outside the selected island set can receive this region.",
                )
            )
            continue
        target_label, target_region_id = target
        mask = analysis.assignment == graph_index[region_id]
        changed = int(np.count_nonzero(mask & (updated != target_label)))
        updated[mask] = target_label
        decisions.append(
            _decision(
                candidate,
                request.policy,
                IslandDecisionStatus.MERGED,
                f"Changed {changed} active pixels from label {candidate.label} to {target_label}.",
                target_label=target_label,
                target_region_id=target_region_id,
                changed_pixel_count=changed,
            )
        )

    result_labels = LabelField(
        width=labels.width,
        height=labels.height,
        label_values=labels.label_values,
        pixels=updated.tobytes(),
    )
    active = (analysis.assignment >= 0).astype(np.uint8).tobytes()
    colors = {entry.label: entry.color for entry in analysis.graph.palette}
    after = analyze_regions(
        result_labels,
        colors=colors,
        width_mm=analysis.graph.width_mm,
        height_mm=analysis.graph.height_mm,
        active=active,
    )
    lineage = derive_region_lineage(analysis, after)
    ordered_decisions = tuple(sorted(decisions, key=lambda item: item.region_id))
    record = IslandPolicyRecord(
        before_graph_fingerprint=analysis.graph.fingerprint(),
        after_graph_fingerprint=after.graph.fingerprint(),
        classification_fingerprint=classification.fingerprint(),
        request=request,
        decisions=ordered_decisions,
        changed_pixel_count=sum(item.changed_pixel_count for item in ordered_decisions),
        lineage=lineage,
    )
    return AppliedIslandPolicy(record=record, labels=result_labels, analysis=after)


def _neighbor_map(graph: RegionGraph) -> dict[str, tuple[IslandNeighbor, ...]]:
    regions = {region.id: region for region in graph.regions}
    neighbors: dict[str, list[IslandNeighbor]] = {region.id: [] for region in graph.regions}
    distances: dict[tuple[str, str], float] = {}
    for edge in graph.adjacency:
        for source_id, target_id in (
            (edge.first_region_id, edge.second_region_id),
            (edge.second_region_id, edge.first_region_id),
        ):
            source = regions[source_id]
            target = regions[target_id]
            color_pair = (source.color, target.color)
            if color_pair not in distances:
                distances[color_pair] = delta_e_76(
                    rgb_to_lab(_hex_rgb(source.color)), rgb_to_lab(_hex_rgb(target.color))
                )
            neighbors[source_id].append(
                IslandNeighbor(
                    region_id=target.id,
                    label=target.label,
                    color=target.color,
                    boundary_edge_count=edge.boundary_edge_count,
                    boundary_length_mm=edge.boundary_length_mm,
                    delta_e=distances[color_pair],
                )
            )
    return {
        region_id: tuple(sorted(items, key=lambda item: (item.label, item.region_id)))
        for region_id, items in neighbors.items()
    }


def _island_finding(
    candidate: IslandCandidate, options: IslandAnalysisOptions, palette_count: int
) -> RiskFinding:
    ratios = []
    measurements = [
        _measurement(
            RiskMeasurementKey.AREA,
            RiskMeasurementRole.MEASURED,
            candidate.area_mm2,
            RiskUnit.MM2,
        ),
        _measurement(
            RiskMeasurementKey.EQUIVALENT_DIAMETER,
            RiskMeasurementRole.MEASURED,
            candidate.equivalent_diameter_mm,
            RiskUnit.MM,
        ),
        _measurement(
            RiskMeasurementKey.PERIMETER,
            RiskMeasurementRole.CONTEXT,
            candidate.perimeter_mm,
            RiskUnit.MM,
        ),
        _measurement(
            RiskMeasurementKey.COMPACTNESS,
            RiskMeasurementRole.CONTEXT,
            candidate.compactness,
            RiskUnit.UNITLESS,
        ),
        _measurement(
            RiskMeasurementKey.LENGTH,
            RiskMeasurementRole.CONTEXT,
            candidate.bbox_length_mm,
            RiskUnit.MM,
        ),
        _measurement(
            RiskMeasurementKey.MINIMUM_WIDTH,
            RiskMeasurementRole.CONTEXT,
            candidate.bbox_width_mm,
            RiskUnit.MM,
        ),
    ]
    if options.minimum_area_mm2 > 0:
        ratios.append(candidate.area_mm2 / options.minimum_area_mm2)
        measurements.append(
            _measurement(
                RiskMeasurementKey.AREA,
                RiskMeasurementRole.THRESHOLD,
                options.minimum_area_mm2,
                RiskUnit.MM2,
            )
        )
    if options.minimum_equivalent_diameter_mm > 0:
        ratios.append(candidate.equivalent_diameter_mm / options.minimum_equivalent_diameter_mm)
        measurements.append(
            _measurement(
                RiskMeasurementKey.EQUIVALENT_DIAMETER,
                RiskMeasurementRole.THRESHOLD,
                options.minimum_equivalent_diameter_mm,
                RiskUnit.MM,
            )
        )
    suggestions = list(default_risk_suggestions(RiskCode.SMALL_ISLAND))
    if not candidate.neighbors:
        suggestions = [
            suggestion
            for suggestion in suggestions
            if suggestion.kind
            not in {
                RiskActionKind.MERGE_DOMINANT_NEIGHBOR,
                RiskActionKind.MERGE_PERCEPTUAL_NEIGHBOR,
            }
        ]
    if palette_count <= 1:
        suggestions = [
            suggestion
            for suggestion in suggestions
            if suggestion.kind != RiskActionKind.MERGE_EXPLICIT_COLOR
        ]
    trigger_text = " and ".join(trigger.value.replace("_", " ") for trigger in candidate.triggers)
    return RiskFinding(
        code=RiskCode.SMALL_ISLAND,
        feature_key=f"island-region-{candidate.region_id}",
        severity=(
            RiskSeverity.ERROR
            if ratios and min(ratios) <= options.error_ratio
            else RiskSeverity.WARNING
        ),
        title=f"Small {candidate.color} island",
        explanation=(
            f"This {candidate.area_mm2:.4g} mm² region falls below the configured "
            f"{trigger_text} threshold. It touches {len(candidate.neighbors)} neighboring "
            "region(s); review the exact boundary evidence before merging it."
        ),
        affected_region_ids=(candidate.region_id,),
        affected_labels=(candidate.label,),
        affected_bounds=(candidate.bounds,),
        measurements=tuple(measurements),
        suggestions=tuple(suggestions),
        classifier_id="physical-small-island",
        classifier_version="1",
    )


def _measurement(
    key: RiskMeasurementKey,
    role: RiskMeasurementRole,
    value: float,
    unit: RiskUnit,
) -> RiskMeasurement:
    return RiskMeasurement(key=key, role=role, value=value, unit=unit)


def _policy_target(
    candidate: IslandCandidate,
    request: IslandMergeRequest,
    selected: set[str],
) -> Optional[tuple[int, Optional[str]]]:
    if request.policy == IslandMergePolicy.EXPLICIT_COLOR:
        assert request.explicit_target_label is not None
        return request.explicit_target_label, None
    eligible = tuple(
        neighbor for neighbor in candidate.neighbors if neighbor.region_id not in selected
    )
    if not eligible:
        return None
    if request.policy == IslandMergePolicy.DOMINANT_NEIGHBOR:
        target_label = _dominant_label(eligible)
    elif request.policy == IslandMergePolicy.PERCEPTUAL_NEIGHBOR:
        target_label = _perceptual_label(eligible)
    else:
        return None
    label_neighbors = tuple(neighbor for neighbor in eligible if neighbor.label == target_label)
    target_region = sorted(
        label_neighbors, key=lambda item: (-item.boundary_length_mm, item.region_id)
    )[0]
    return target_label, target_region.region_id


def _dominant_label(neighbors: tuple[IslandNeighbor, ...]) -> int:
    totals: dict[int, float] = {}
    for neighbor in neighbors:
        totals[neighbor.label] = totals.get(neighbor.label, 0.0) + neighbor.boundary_length_mm
    return sorted(totals, key=lambda label: (-totals[label], label))[0]


def _perceptual_label(neighbors: tuple[IslandNeighbor, ...]) -> int:
    distances: dict[int, float] = {}
    totals: dict[int, float] = {}
    for neighbor in neighbors:
        distances[neighbor.label] = min(distances.get(neighbor.label, math.inf), neighbor.delta_e)
        totals[neighbor.label] = totals.get(neighbor.label, 0.0) + neighbor.boundary_length_mm
    return sorted(distances, key=lambda label: (distances[label], -totals[label], label))[0]


def _decision(
    candidate: IslandCandidate,
    policy: IslandMergePolicy,
    status: IslandDecisionStatus,
    reason: str,
    *,
    target_label: Optional[int] = None,
    target_region_id: Optional[str] = None,
    changed_pixel_count: int = 0,
) -> IslandMergeDecision:
    return IslandMergeDecision(
        region_id=candidate.region_id,
        policy=policy,
        status=status,
        source_label=candidate.label,
        target_label=target_label,
        target_region_id=target_region_id,
        changed_pixel_count=changed_pixel_count,
        reason=reason,
    )


def _validate_source_labels(labels: LabelField, analysis: RegionAnalysis) -> None:
    graph = analysis.graph
    if (labels.width, labels.height) != (graph.width_px, graph.height_px):
        raise ValueError("island source labels must match the region graph dimensions")
    if set(labels.label_values) != {entry.label for entry in graph.palette}:
        raise ValueError("island source labels must match the region graph palette")
    mismatch = first_mismatched_region(labels, analysis)
    if mismatch is not None:
        raise ValueError(f"island source labels do not match region: {mismatch.id}")


def _island_summary(candidates: tuple[IslandCandidate, ...]) -> IslandAnalysisSummary:
    risk_count = sum(item.status == IslandCandidateStatus.RISK for item in candidates)
    return IslandAnalysisSummary(
        candidate_count=len(candidates),
        risk_count=risk_count,
        long_line_exempt_count=len(candidates) - risk_count,
    )


def _hex_rgb(color: str) -> tuple[int, int, int]:
    return tuple(bytes.fromhex(color[1:]))  # type: ignore[return-value]
