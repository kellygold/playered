"""Stable printability risk taxonomy, evidence, suggestions, and report contracts."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.regions import PhysicalBounds, RegionGraph

RISK_REPORT_SCHEMA_VERSION = 1


class RiskCode(str, Enum):
    SMALL_ISLAND = "small_island"
    TINY_HOLE = "tiny_hole"
    HOLLOW_RING = "hollow_ring"
    NARROW_NECK = "narrow_neck"
    THIN_LINE = "thin_line"
    NARROW_GAP = "narrow_gap"
    EXCESS_FRAGMENTATION = "excess_fragmentation"
    COLOR_ABSENT = "color_absent"


class RiskSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RiskActionKind(str, Enum):
    REVIEW = "review"
    KEEP = "keep"
    MERGE_DOMINANT_NEIGHBOR = "merge_dominant_neighbor"
    MERGE_PERCEPTUAL_NEIGHBOR = "merge_perceptual_neighbor"
    MERGE_EXPLICIT_COLOR = "merge_explicit_color"
    FILL_HOLE = "fill_hole"
    RECOLOR_HOLE = "recolor_hole"
    COLLAPSE_RING = "collapse_ring"
    WIDEN = "widen"
    PRESERVE_LINE = "preserve_line"
    CLOSE_GAP = "close_gap"
    SMOOTH = "smooth"
    REMOVE_COLOR = "remove_color"
    REPLACE_COLOR = "replace_color"


class RiskMeasurementKey(str, Enum):
    AREA = "area"
    HOLE_AREA = "hole_area"
    PERIMETER = "perimeter"
    COMPACTNESS = "compactness"
    EQUIVALENT_DIAMETER = "equivalent_diameter"
    MINIMUM_WIDTH = "minimum_width"
    MEDIAN_WIDTH = "median_width"
    MAXIMUM_WIDTH = "maximum_width"
    LENGTH = "length"
    GAP_WIDTH = "gap_width"
    COMPONENT_COUNT = "component_count"
    COMPONENTS_PER_100_MM2 = "components_per_100_mm2"
    PIXEL_COUNT = "pixel_count"
    COVERAGE_RATIO = "coverage_ratio"
    NOZZLE_DIAMETER = "nozzle_diameter"


class RiskUnit(str, Enum):
    MM = "mm"
    MM2 = "mm2"
    COUNT = "count"
    COUNT_PER_100_MM2 = "count_per_100_mm2"
    RATIO = "ratio"
    UNITLESS = "unitless"


class RiskMeasurementRole(str, Enum):
    MEASURED = "measured"
    THRESHOLD = "threshold"
    CONTEXT = "context"


_MEASUREMENT_UNITS: dict[RiskMeasurementKey, RiskUnit] = {
    RiskMeasurementKey.AREA: RiskUnit.MM2,
    RiskMeasurementKey.HOLE_AREA: RiskUnit.MM2,
    RiskMeasurementKey.PERIMETER: RiskUnit.MM,
    RiskMeasurementKey.COMPACTNESS: RiskUnit.UNITLESS,
    RiskMeasurementKey.EQUIVALENT_DIAMETER: RiskUnit.MM,
    RiskMeasurementKey.MINIMUM_WIDTH: RiskUnit.MM,
    RiskMeasurementKey.MEDIAN_WIDTH: RiskUnit.MM,
    RiskMeasurementKey.MAXIMUM_WIDTH: RiskUnit.MM,
    RiskMeasurementKey.LENGTH: RiskUnit.MM,
    RiskMeasurementKey.GAP_WIDTH: RiskUnit.MM,
    RiskMeasurementKey.COMPONENT_COUNT: RiskUnit.COUNT,
    RiskMeasurementKey.COMPONENTS_PER_100_MM2: RiskUnit.COUNT_PER_100_MM2,
    RiskMeasurementKey.PIXEL_COUNT: RiskUnit.COUNT,
    RiskMeasurementKey.COVERAGE_RATIO: RiskUnit.RATIO,
    RiskMeasurementKey.NOZZLE_DIAMETER: RiskUnit.MM,
}


_ACTION_PROPERTIES: dict[RiskActionKind, tuple[bool, bool, str, str]] = {
    RiskActionKind.REVIEW: (
        False,
        False,
        "Review region",
        "Inspect the highlighted feature before changing its labels.",
    ),
    RiskActionKind.KEEP: (
        False,
        False,
        "Keep as-is",
        "Accept this measured risk without modifying the artwork.",
    ),
    RiskActionKind.MERGE_DOMINANT_NEIGHBOR: (
        True,
        True,
        "Merge into dominant neighbor",
        "Replace the feature with the color sharing its longest measured boundary.",
    ),
    RiskActionKind.MERGE_PERCEPTUAL_NEIGHBOR: (
        True,
        True,
        "Merge into closest color",
        "Replace the feature with its nearest eligible neighboring palette color.",
    ),
    RiskActionKind.MERGE_EXPLICIT_COLOR: (
        True,
        True,
        "Merge into chosen color",
        "Replace the feature with an explicitly selected palette label.",
    ),
    RiskActionKind.FILL_HOLE: (
        True,
        True,
        "Fill hole",
        "Replace the enclosed hole with its surrounding label.",
    ),
    RiskActionKind.RECOLOR_HOLE: (
        True,
        True,
        "Recolor hole center",
        "Replace the enclosed center with an explicitly selected palette label.",
    ),
    RiskActionKind.COLLAPSE_RING: (
        True,
        True,
        "Collapse ring",
        "Resolve the hollow ring using its measured wall and neighboring labels.",
    ),
    RiskActionKind.WIDEN: (
        True,
        True,
        "Widen feature",
        "Expand the feature in physical space until it reaches a selected width.",
    ),
    RiskActionKind.PRESERVE_LINE: (
        False,
        False,
        "Protect intentional line",
        "Mark this long thin feature as intentional so area cleanup cannot erase it.",
    ),
    RiskActionKind.CLOSE_GAP: (
        True,
        True,
        "Close gap",
        "Bridge the measured gap using an explicitly selected surrounding label.",
    ),
    RiskActionKind.SMOOTH: (
        True,
        True,
        "Smooth fragmentation",
        "Apply a physical-radius contour operation and preview every changed pixel.",
    ),
    RiskActionKind.REMOVE_COLOR: (
        True,
        True,
        "Remove unused color",
        "Remove this unassigned palette slot without changing visible labels.",
    ),
    RiskActionKind.REPLACE_COLOR: (
        True,
        True,
        "Replace color",
        "Choose another filament color and re-run classification before export.",
    ),
}


_VALID_ACTIONS: dict[RiskCode, frozenset[RiskActionKind]] = {
    RiskCode.SMALL_ISLAND: frozenset(
        {
            RiskActionKind.REVIEW,
            RiskActionKind.KEEP,
            RiskActionKind.MERGE_DOMINANT_NEIGHBOR,
            RiskActionKind.MERGE_PERCEPTUAL_NEIGHBOR,
            RiskActionKind.MERGE_EXPLICIT_COLOR,
        }
    ),
    RiskCode.TINY_HOLE: frozenset(
        {
            RiskActionKind.REVIEW,
            RiskActionKind.KEEP,
            RiskActionKind.FILL_HOLE,
            RiskActionKind.RECOLOR_HOLE,
        }
    ),
    RiskCode.HOLLOW_RING: frozenset(
        {
            RiskActionKind.REVIEW,
            RiskActionKind.KEEP,
            RiskActionKind.FILL_HOLE,
            RiskActionKind.RECOLOR_HOLE,
            RiskActionKind.COLLAPSE_RING,
        }
    ),
    RiskCode.NARROW_NECK: frozenset(
        {RiskActionKind.REVIEW, RiskActionKind.KEEP, RiskActionKind.WIDEN}
    ),
    RiskCode.THIN_LINE: frozenset(
        {RiskActionKind.REVIEW, RiskActionKind.PRESERVE_LINE, RiskActionKind.WIDEN}
    ),
    RiskCode.NARROW_GAP: frozenset(
        {RiskActionKind.REVIEW, RiskActionKind.KEEP, RiskActionKind.CLOSE_GAP}
    ),
    RiskCode.EXCESS_FRAGMENTATION: frozenset(
        {
            RiskActionKind.REVIEW,
            RiskActionKind.KEEP,
            RiskActionKind.SMOOTH,
            RiskActionKind.MERGE_DOMINANT_NEIGHBOR,
            RiskActionKind.MERGE_PERCEPTUAL_NEIGHBOR,
            RiskActionKind.MERGE_EXPLICIT_COLOR,
        }
    ),
    RiskCode.COLOR_ABSENT: frozenset(
        {RiskActionKind.KEEP, RiskActionKind.REMOVE_COLOR, RiskActionKind.REPLACE_COLOR}
    ),
}


_DEFAULT_ACTIONS: dict[RiskCode, tuple[RiskActionKind, ...]] = {
    code: tuple(action for action in RiskActionKind if action in allowed)
    for code, allowed in _VALID_ACTIONS.items()
}


class RiskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RiskMeasurement(RiskModel):
    key: RiskMeasurementKey
    role: RiskMeasurementRole
    value: float = Field(ge=0)
    unit: RiskUnit

    @model_validator(mode="after")
    def unit_matches_measurement(self) -> RiskMeasurement:
        expected = _MEASUREMENT_UNITS[self.key]
        if self.unit != expected:
            raise ValueError(f"{self.key.value} measurements must use {expected.value}")
        if self.key == RiskMeasurementKey.COVERAGE_RATIO and self.value > 1:
            raise ValueError("coverage_ratio measurements cannot exceed 1")
        if self.unit == RiskUnit.COUNT and not self.value.is_integer():
            raise ValueError("count measurements must be whole numbers")
        return self


class RiskSuggestion(RiskModel):
    kind: RiskActionKind
    title: str = Field(min_length=1, max_length=120)
    explanation: str = Field(min_length=1, max_length=500)
    destructive: bool
    requires_confirmation: bool

    @model_validator(mode="after")
    def safety_flags_match_action(self) -> RiskSuggestion:
        expected_destructive, expected_confirmation, _title, _explanation = _ACTION_PROPERTIES[
            self.kind
        ]
        if (
            self.destructive != expected_destructive
            or self.requires_confirmation != expected_confirmation
        ):
            raise ValueError(f"safety flags do not match action {self.kind.value}")
        return self


class RiskFinding(RiskModel):
    code: RiskCode
    feature_key: str = Field(min_length=1, max_length=200)
    severity: RiskSeverity
    title: str = Field(min_length=1, max_length=160)
    explanation: str = Field(min_length=1, max_length=1000)
    affected_region_ids: tuple[str, ...] = ()
    affected_labels: tuple[int, ...] = ()
    affected_bounds: tuple[PhysicalBounds, ...] = ()
    measurements: tuple[RiskMeasurement, ...] = Field(min_length=1)
    suggestions: tuple[RiskSuggestion, ...] = Field(min_length=1)
    classifier_id: str = Field(min_length=1, max_length=120)
    classifier_version: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def finding_is_unambiguous_and_actionable(self) -> RiskFinding:
        if not (self.affected_region_ids or self.affected_labels or self.affected_bounds):
            raise ValueError("a risk finding must identify a region, label, or physical bound")
        if len(self.affected_region_ids) != len(set(self.affected_region_ids)):
            raise ValueError("affected region IDs must be unique")
        if len(self.affected_labels) != len(set(self.affected_labels)):
            raise ValueError("affected labels must be unique")
        measurement_keys = [(item.key, item.role) for item in self.measurements]
        if len(measurement_keys) != len(set(measurement_keys)):
            raise ValueError("measurement key/role pairs must be unique")
        actions = [suggestion.kind for suggestion in self.suggestions]
        if len(actions) != len(set(actions)):
            raise ValueError("suggested actions must be unique")
        invalid = set(actions) - _VALID_ACTIONS[self.code]
        if invalid:
            names = ", ".join(sorted(action.value for action in invalid))
            raise ValueError(f"invalid actions for {self.code.value}: {names}")
        return self


class RiskWarning(RiskModel):
    id: str = Field(pattern=r"^risk_[0-9a-f]{24}$")
    code: RiskCode
    feature_key: str = Field(min_length=1, max_length=200)
    severity: RiskSeverity
    title: str = Field(min_length=1, max_length=160)
    explanation: str = Field(min_length=1, max_length=1000)
    affected_region_ids: tuple[str, ...]
    affected_labels: tuple[int, ...]
    affected_bounds: tuple[PhysicalBounds, ...]
    measurements: tuple[RiskMeasurement, ...] = Field(min_length=1)
    suggestions: tuple[RiskSuggestion, ...] = Field(min_length=1)
    classifier_id: str = Field(min_length=1, max_length=120)
    classifier_version: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def warning_remains_actionable_after_deserialization(self) -> RiskWarning:
        if not (self.affected_region_ids or self.affected_labels or self.affected_bounds):
            raise ValueError("a risk warning must identify a region, label, or physical bound")
        if len(self.affected_region_ids) != len(set(self.affected_region_ids)):
            raise ValueError("affected region IDs must be unique")
        if len(self.affected_labels) != len(set(self.affected_labels)):
            raise ValueError("affected labels must be unique")
        measurement_keys = [(item.key, item.role) for item in self.measurements]
        if len(measurement_keys) != len(set(measurement_keys)):
            raise ValueError("measurement key/role pairs must be unique")
        actions = [suggestion.kind for suggestion in self.suggestions]
        if len(actions) != len(set(actions)):
            raise ValueError("suggested actions must be unique")
        invalid = set(actions) - _VALID_ACTIONS[self.code]
        if invalid:
            names = ", ".join(sorted(action.value for action in invalid))
            raise ValueError(f"invalid actions for {self.code.value}: {names}")
        expected_suggestions = tuple(sorted(self.suggestions, key=_suggestion_sort_key))
        if self.suggestions != expected_suggestions:
            raise ValueError("risk suggestions must use canonical ordering")
        expected_measurements = tuple(
            sorted(self.measurements, key=lambda item: (item.key.value, item.role.value))
        )
        if self.measurements != expected_measurements:
            raise ValueError("risk measurements must use canonical ordering")
        return self


class RiskCodeCount(RiskModel):
    code: RiskCode
    count: int = Field(ge=0)


class RiskSummary(RiskModel):
    total: int = Field(ge=0)
    info: int = Field(ge=0)
    warning: int = Field(ge=0)
    error: int = Field(ge=0)
    by_code: tuple[RiskCodeCount, ...] = Field(min_length=len(RiskCode), max_length=len(RiskCode))


class RiskReport(RiskModel):
    schema_version: Literal[1] = RISK_REPORT_SCHEMA_VERSION
    graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    options_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    nozzle_mm: float = Field(gt=0, le=2)
    evaluated_codes: tuple[RiskCode, ...]
    pending_codes: tuple[RiskCode, ...]
    warnings: tuple[RiskWarning, ...]
    summary: RiskSummary

    @model_validator(mode="after")
    def report_is_complete_and_canonical(self) -> RiskReport:
        expected_evaluated = tuple(code for code in RiskCode if code in self.evaluated_codes)
        expected_pending = tuple(code for code in RiskCode if code not in self.evaluated_codes)
        if self.evaluated_codes != expected_evaluated or self.pending_codes != expected_pending:
            raise ValueError("evaluated and pending codes must be a canonical taxonomy partition")
        if len(self.evaluated_codes) != len(set(self.evaluated_codes)):
            raise ValueError("evaluated risk codes must be unique")
        if any(warning.code not in self.evaluated_codes for warning in self.warnings):
            raise ValueError("warnings may only use evaluated risk codes")
        if len({warning.id for warning in self.warnings}) != len(self.warnings):
            raise ValueError("risk warning IDs must be unique")
        if any(
            warning.id
            != _risk_warning_id(
                warning.code,
                warning.feature_key,
                warning.affected_region_ids,
                warning.affected_labels,
            )
            for warning in self.warnings
        ):
            raise ValueError("risk warning ID does not match its stable identity")
        if self.warnings != tuple(sorted(self.warnings, key=_warning_sort_key)):
            raise ValueError("risk warnings must use canonical ordering")
        if self.summary != _risk_summary(self.warnings):
            raise ValueError("risk summary does not match warnings")
        return self

    @property
    def analysis_complete(self) -> bool:
        return not self.pending_codes

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
class RiskAnalysisOptions:
    nozzle_mm: float
    max_components_per_label: int = 64
    max_components_per_100_mm2: float = 20.0
    error_multiplier: float = 4.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.nozzle_mm) or not 0 < self.nozzle_mm <= 2:
            raise ValueError("nozzle_mm must be finite, positive, and at most 2")
        if self.max_components_per_label < 1:
            raise ValueError("max_components_per_label must be positive")
        if (
            not math.isfinite(self.max_components_per_100_mm2)
            or self.max_components_per_100_mm2 <= 0
        ):
            raise ValueError("max_components_per_100_mm2 must be finite and positive")
        if not math.isfinite(self.error_multiplier) or self.error_multiplier < 1:
            raise ValueError("error_multiplier must be finite and at least 1")

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "error_multiplier": self.error_multiplier,
                "max_components_per_100_mm2": self.max_components_per_100_mm2,
                "max_components_per_label": self.max_components_per_label,
                "nozzle_mm": self.nozzle_mm,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


GRAPH_LEVEL_RISK_CODES = (RiskCode.EXCESS_FRAGMENTATION, RiskCode.COLOR_ABSENT)


def default_risk_suggestions(code: RiskCode) -> tuple[RiskSuggestion, ...]:
    return tuple(_suggestion(kind) for kind in _DEFAULT_ACTIONS[code])


def build_risk_report(
    graph: RegionGraph,
    findings: tuple[RiskFinding, ...],
    *,
    options: RiskAnalysisOptions,
    evaluated_codes: tuple[RiskCode, ...],
) -> RiskReport:
    """Validate classifier findings against the graph and create stable warnings."""

    evaluated = tuple(code for code in RiskCode if code in set(evaluated_codes))
    region_ids = {region.id for region in graph.regions}
    labels = {entry.label for entry in graph.palette}
    warnings = []
    identities: set[tuple[object, ...]] = set()
    for finding in findings:
        if finding.code not in evaluated:
            raise ValueError(f"finding code was not evaluated: {finding.code.value}")
        unknown_regions = set(finding.affected_region_ids) - region_ids
        if unknown_regions:
            raise ValueError(f"finding references unknown regions: {sorted(unknown_regions)}")
        unknown_labels = set(finding.affected_labels) - labels
        if unknown_labels:
            raise ValueError(f"finding references unknown labels: {sorted(unknown_labels)}")
        if any(
            bound.x_mm + bound.width_mm > graph.width_mm + 1e-9
            or bound.y_mm + bound.height_mm > graph.height_mm + 1e-9
            for bound in finding.affected_bounds
        ):
            raise ValueError("finding physical bounds must remain inside the graph canvas")
        identity = _finding_identity(finding)
        if identity in identities:
            raise ValueError("duplicate risk finding identity")
        identities.add(identity)
        warnings.append(_warning(finding))
    ordered = tuple(sorted(warnings, key=_warning_sort_key))
    return RiskReport(
        graph_fingerprint=graph.fingerprint(),
        options_fingerprint=options.fingerprint(),
        nozzle_mm=options.nozzle_mm,
        evaluated_codes=evaluated,
        pending_codes=tuple(code for code in RiskCode if code not in evaluated),
        warnings=ordered,
        summary=_risk_summary(ordered),
    )


def analyze_printability_risks(
    graph: RegionGraph,
    *,
    options: RiskAnalysisOptions,
    findings: tuple[RiskFinding, ...] = (),
    evaluated_codes: tuple[RiskCode, ...] = GRAPH_LEVEL_RISK_CODES,
) -> RiskReport:
    """Add graph-level risks, preserving explicit coverage for pending classifiers."""

    evaluated = tuple(dict.fromkeys((*GRAPH_LEVEL_RISK_CODES, *evaluated_codes)))
    graph_findings = (*_fragmentation_findings(graph, options), *_absence_findings(graph))
    return build_risk_report(
        graph,
        tuple((*graph_findings, *findings)),
        options=options,
        evaluated_codes=evaluated,
    )


def validate_risk_report_against_graph(report: RiskReport, graph: RegionGraph) -> None:
    """Reject a well-formed report that does not reference its exact source graph."""

    if report.graph_fingerprint != graph.fingerprint():
        raise ValueError("risk report does not match the region graph fingerprint")
    region_ids = {region.id for region in graph.regions}
    labels = {entry.label for entry in graph.palette}
    for warning in report.warnings:
        unknown_regions = set(warning.affected_region_ids) - region_ids
        if unknown_regions:
            raise ValueError(f"risk warning references unknown regions: {sorted(unknown_regions)}")
        unknown_labels = set(warning.affected_labels) - labels
        if unknown_labels:
            raise ValueError(f"risk warning references unknown labels: {sorted(unknown_labels)}")
        if any(
            bound.x_mm + bound.width_mm > graph.width_mm + 1e-9
            or bound.y_mm + bound.height_mm > graph.height_mm + 1e-9
            for bound in warning.affected_bounds
        ):
            raise ValueError("risk warning physical bounds exceed the graph canvas")


def _fragmentation_findings(
    graph: RegionGraph, options: RiskAnalysisOptions
) -> tuple[RiskFinding, ...]:
    canvas_area = graph.width_mm * graph.height_mm
    findings = []
    for palette in graph.palette:
        regions = tuple(region for region in graph.regions if region.label == palette.label)
        count = len(regions)
        density = count / canvas_area * 100
        if (
            count <= options.max_components_per_label
            and density <= options.max_components_per_100_mm2
        ):
            continue
        severity = (
            RiskSeverity.ERROR
            if count > options.max_components_per_label * options.error_multiplier
            or density > options.max_components_per_100_mm2 * options.error_multiplier
            else RiskSeverity.WARNING
        )
        bounds = _combined_bounds(tuple(region.physical_bounds for region in regions))
        findings.append(
            RiskFinding(
                code=RiskCode.EXCESS_FRAGMENTATION,
                feature_key=f"palette-label-{palette.label}",
                severity=severity,
                title=f"{count} separate {palette.color} regions",
                explanation=(
                    f"Palette label {palette.label} is split into {count} four-connected regions "
                    f"({density:.3g} per 100 mm²). Many isolated paths can make the artwork "
                    "fragile or noisy; inspect the exact regions before applying cleanup."
                ),
                affected_region_ids=tuple(sorted(region.id for region in regions)),
                affected_labels=(palette.label,),
                affected_bounds=(bounds,),
                measurements=(
                    _measurement(
                        RiskMeasurementKey.COMPONENT_COUNT,
                        RiskMeasurementRole.MEASURED,
                        count,
                    ),
                    _measurement(
                        RiskMeasurementKey.COMPONENT_COUNT,
                        RiskMeasurementRole.THRESHOLD,
                        options.max_components_per_label,
                    ),
                    _measurement(
                        RiskMeasurementKey.COMPONENTS_PER_100_MM2,
                        RiskMeasurementRole.MEASURED,
                        density,
                    ),
                    _measurement(
                        RiskMeasurementKey.COMPONENTS_PER_100_MM2,
                        RiskMeasurementRole.THRESHOLD,
                        options.max_components_per_100_mm2,
                    ),
                ),
                suggestions=default_risk_suggestions(RiskCode.EXCESS_FRAGMENTATION),
                classifier_id="graph-fragmentation",
                classifier_version="1",
            )
        )
    return tuple(findings)


def _absence_findings(graph: RegionGraph) -> tuple[RiskFinding, ...]:
    used_labels = {region.label for region in graph.regions}
    findings = []
    for palette in graph.palette:
        if palette.label in used_labels:
            continue
        findings.append(
            RiskFinding(
                code=RiskCode.COLOR_ABSENT,
                feature_key=f"palette-label-{palette.label}",
                severity=RiskSeverity.INFO,
                title=f"{palette.color} has no visible assignments",
                explanation=(
                    f"Palette label {palette.label} has no active pixels. It adds no visible "
                    "geometry and may become an unnecessary material slot at export."
                ),
                affected_labels=(palette.label,),
                measurements=(
                    _measurement(RiskMeasurementKey.PIXEL_COUNT, RiskMeasurementRole.MEASURED, 0),
                    _measurement(
                        RiskMeasurementKey.COVERAGE_RATIO, RiskMeasurementRole.MEASURED, 0
                    ),
                ),
                suggestions=default_risk_suggestions(RiskCode.COLOR_ABSENT),
                classifier_id="graph-color-coverage",
                classifier_version="1",
            )
        )
    return tuple(findings)


def _measurement(
    key: RiskMeasurementKey, role: RiskMeasurementRole, value: float
) -> RiskMeasurement:
    return RiskMeasurement(key=key, role=role, value=value, unit=_MEASUREMENT_UNITS[key])


def _combined_bounds(bounds: tuple[PhysicalBounds, ...]) -> PhysicalBounds:
    minimum_x = min(item.x_mm for item in bounds)
    minimum_y = min(item.y_mm for item in bounds)
    maximum_x = max(item.x_mm + item.width_mm for item in bounds)
    maximum_y = max(item.y_mm + item.height_mm for item in bounds)
    return PhysicalBounds(
        x_mm=minimum_x,
        y_mm=minimum_y,
        width_mm=maximum_x - minimum_x,
        height_mm=maximum_y - minimum_y,
    )


def _suggestion(kind: RiskActionKind) -> RiskSuggestion:
    destructive, confirmation, title, explanation = _ACTION_PROPERTIES[kind]
    return RiskSuggestion(
        kind=kind,
        title=title,
        explanation=explanation,
        destructive=destructive,
        requires_confirmation=confirmation,
    )


def _finding_identity(finding: RiskFinding) -> tuple[object, ...]:
    return (
        finding.code.value,
        finding.feature_key,
        tuple(sorted(finding.affected_region_ids)),
        tuple(sorted(finding.affected_labels)),
    )


def _warning(finding: RiskFinding) -> RiskWarning:
    return RiskWarning(
        id=_risk_warning_id(
            finding.code,
            finding.feature_key,
            finding.affected_region_ids,
            finding.affected_labels,
        ),
        code=finding.code,
        feature_key=finding.feature_key,
        severity=finding.severity,
        title=finding.title,
        explanation=finding.explanation,
        affected_region_ids=tuple(sorted(finding.affected_region_ids)),
        affected_labels=tuple(sorted(finding.affected_labels)),
        affected_bounds=finding.affected_bounds,
        measurements=tuple(
            sorted(finding.measurements, key=lambda item: (item.key.value, item.role.value))
        ),
        suggestions=tuple(sorted(finding.suggestions, key=_suggestion_sort_key)),
        classifier_id=finding.classifier_id,
        classifier_version=finding.classifier_version,
    )


def _risk_warning_id(
    code: RiskCode,
    feature_key: str,
    affected_region_ids: tuple[str, ...],
    affected_labels: tuple[int, ...],
) -> str:
    identity = (
        code.value,
        feature_key,
        tuple(sorted(affected_region_ids)),
        tuple(sorted(affected_labels)),
    )
    payload = json.dumps(identity, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(b"image23mf-risk-v1\0" + payload).hexdigest()
    return f"risk_{digest[:24]}"


def _warning_sort_key(warning: RiskWarning) -> tuple[int, str, str]:
    severity_order = {RiskSeverity.ERROR: 0, RiskSeverity.WARNING: 1, RiskSeverity.INFO: 2}
    return severity_order[warning.severity], warning.code.value, warning.id


def _suggestion_sort_key(suggestion: RiskSuggestion) -> int:
    return tuple(RiskActionKind).index(suggestion.kind)


def _risk_summary(warnings: tuple[RiskWarning, ...]) -> RiskSummary:
    severities = Counter(warning.severity for warning in warnings)
    codes = Counter(warning.code for warning in warnings)
    return RiskSummary(
        total=len(warnings),
        info=severities[RiskSeverity.INFO],
        warning=severities[RiskSeverity.WARNING],
        error=severities[RiskSeverity.ERROR],
        by_code=tuple(RiskCodeCount(code=code, count=codes[code]) for code in RiskCode),
    )
