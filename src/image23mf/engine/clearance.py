"""Physical clearance-ridge analysis for thin lines, necks, and narrow gaps."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.regions import (
    PhysicalBounds,
    PixelBounds,
    RegionAnalysis,
    RegionGraph,
    RegionNode,
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
from image23mf.parallel import process_map

CLEARANCE_ANALYSIS_SCHEMA_VERSION = 1


class ClearanceFeatureKind(str, Enum):
    THIN_LINE = "thin_line"
    NARROW_NECK = "narrow_neck"
    NARROW_GAP = "narrow_gap"


class ClearanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ClearanceAnalysisSettings(ClearanceModel):
    nozzle_mm: float = Field(gt=0, le=2)
    minimum_width_mm: float = Field(gt=0, le=10)
    minimum_line_width_mm: float = Field(gt=0, le=10)
    minimum_neck_width_mm: float = Field(gt=0, le=10)
    minimum_gap_width_mm: float = Field(gt=0, le=10)
    minimum_line_length_mm: float = Field(gt=0, le=1000)
    minimum_line_aspect_ratio: float = Field(ge=1, le=1000)
    wide_support_ratio: float = Field(gt=1, le=20)
    error_ratio: float = Field(gt=0, le=1)
    distance_method: Literal["euclidean-center-distance-v1"] = "euclidean-center-distance-v1"
    width_method: Literal["orthogonal-run-at-clearance-ridge-v1"] = (
        "orthogonal-run-at-clearance-ridge-v1"
    )

    @model_validator(mode="before")
    @classmethod
    def enrich_legacy_widths(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        enriched = dict(value)
        shared = enriched.get("minimum_width_mm")
        if shared is not None:
            enriched.setdefault("minimum_line_width_mm", shared)
            enriched.setdefault("minimum_neck_width_mm", shared)
            enriched.setdefault("minimum_gap_width_mm", shared)
        return enriched

    def canonical_json(self) -> str:
        payload = self.model_dump(mode="json")
        if (
            self.minimum_line_width_mm
            == self.minimum_neck_width_mm
            == self.minimum_gap_width_mm
            == self.minimum_width_mm
        ):
            payload.pop("minimum_line_width_mm")
            payload.pop("minimum_neck_width_mm")
            payload.pop("minimum_gap_width_mm")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ClearanceFeature(ClearanceModel):
    id: str = Field(pattern=r"^feature_[0-9a-f]{24}$")
    kind: ClearanceFeatureKind
    region_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    labels: tuple[int, ...] = Field(min_length=1, max_length=2)
    pixel_bounds: PixelBounds
    bounds: PhysicalBounds
    minimum_width_mm: float = Field(ge=0)
    median_width_mm: float = Field(ge=0)
    maximum_width_mm: float = Field(ge=0)
    length_mm: float = Field(gt=0)
    evidence_sample_count: int = Field(gt=0)
    touches_canvas_border: bool

    @model_validator(mode="after")
    def feature_is_canonical(self) -> ClearanceFeature:
        if self.region_ids != tuple(sorted(set(self.region_ids))):
            raise ValueError("clearance feature region IDs must be unique and canonical")
        if self.labels != tuple(sorted(set(self.labels))):
            raise ValueError("clearance feature labels must be unique and canonical")
        if not (self.minimum_width_mm <= self.median_width_mm <= self.maximum_width_mm):
            raise ValueError("clearance feature widths must be ordered")
        if self.id != _feature_id(self.kind, self.region_ids, self.pixel_bounds):
            raise ValueError("clearance feature ID does not match its stable identity")
        return self


class ClearanceAnalysisSummary(ClearanceModel):
    feature_count: int = Field(ge=0)
    thin_line_count: int = Field(ge=0)
    narrow_neck_count: int = Field(ge=0)
    narrow_gap_count: int = Field(ge=0)


class ClearanceAnalysis(ClearanceModel):
    schema_version: Literal[1] = CLEARANCE_ANALYSIS_SCHEMA_VERSION
    graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    options: ClearanceAnalysisSettings
    options_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    features: tuple[ClearanceFeature, ...]
    summary: ClearanceAnalysisSummary

    @model_validator(mode="after")
    def analysis_is_canonical(self) -> ClearanceAnalysis:
        if self.options_fingerprint != self.options.fingerprint():
            raise ValueError("clearance options fingerprint does not match settings")
        if self.features != tuple(sorted(self.features, key=lambda item: item.id)):
            raise ValueError("clearance features must use canonical ordering")
        if len({feature.id for feature in self.features}) != len(self.features):
            raise ValueError("clearance feature IDs must be unique")
        if self.summary != _summary(self.features):
            raise ValueError("clearance summary does not match features")
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
class ClearanceAnalysisOptions:
    nozzle_mm: float
    minimum_width_mm: float | None = None
    minimum_line_width_mm: float | None = None
    minimum_neck_width_mm: float | None = None
    minimum_gap_width_mm: float | None = None
    minimum_line_length_mm: float | None = None
    minimum_line_aspect_ratio: float = 6.0
    wide_support_ratio: float = 1.5
    error_ratio: float = 0.5

    def __post_init__(self) -> None:
        values = (
            self.nozzle_mm,
            self.minimum_width_mm,
            self.minimum_line_width_mm,
            self.minimum_neck_width_mm,
            self.minimum_gap_width_mm,
            self.minimum_line_length_mm,
            self.minimum_line_aspect_ratio,
            self.wide_support_ratio,
            self.error_ratio,
        )
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("clearance analysis options must be finite")
        self.contract()

    def contract(self) -> ClearanceAnalysisSettings:
        shared = self.minimum_width_mm if self.minimum_width_mm is not None else self.nozzle_mm
        return ClearanceAnalysisSettings(
            nozzle_mm=self.nozzle_mm,
            minimum_width_mm=shared,
            minimum_line_width_mm=(
                self.minimum_line_width_mm if self.minimum_line_width_mm is not None else shared
            ),
            minimum_neck_width_mm=(
                self.minimum_neck_width_mm if self.minimum_neck_width_mm is not None else shared
            ),
            minimum_gap_width_mm=(
                self.minimum_gap_width_mm if self.minimum_gap_width_mm is not None else shared
            ),
            minimum_line_length_mm=(
                self.minimum_line_length_mm
                if self.minimum_line_length_mm is not None
                else self.nozzle_mm * 4
            ),
            minimum_line_aspect_ratio=self.minimum_line_aspect_ratio,
            wide_support_ratio=self.wide_support_ratio,
            error_ratio=self.error_ratio,
        )


@dataclass(frozen=True)
class _CanvasMetrics:
    width_px: int
    height_px: int
    pixel_width_mm: float
    pixel_height_mm: float


@dataclass(frozen=True)
class _RegionBatch:
    canvas: _CanvasMetrics
    settings: ClearanceAnalysisSettings
    regions: tuple[tuple[RegionNode, bytes], ...]


def clearance_worker_count(analysis: RegionAnalysis) -> int:
    """Bound startup/IPC costs; tiny previews and low-core machines stay serial."""

    regions = analysis.graph.regions
    packed_size = sum((r.pixel_bounds.width * r.pixel_bounds.height + 7) // 8 for r in regions)
    estimated_work = sum(r.pixel_bounds.width * r.pixel_bounds.height + 256 for r in regions)
    if len(regions) < 4 or estimated_work < 2_000_000 or packed_size > 64 * 1024 * 1024:
        return 1
    return min(8, len(regions), max(1, (os.cpu_count() or 1) - 2))


def _region_batch_features(batch: _RegionBatch) -> list[ClearanceFeature]:
    features: list[ClearanceFeature] = []
    for region, packed in batch.regions:
        bounds = region.pixel_bounds
        mask = np.unpackbits(
            np.frombuffer(packed, dtype=np.uint8), count=bounds.width * bounds.height
        )
        mask = mask.reshape(bounds.height, bounds.width).astype(bool)
        features.extend(_region_features(batch.canvas, region, mask, batch.settings))
    return features


def analyze_clearance_features(
    analysis: RegionAnalysis,
    *,
    options: ClearanceAnalysisOptions,
    max_workers: int = 1,
    check_canceled: Optional[Callable[[], None]] = None,
) -> ClearanceAnalysis:
    """Measure thin lines, constricted necks, and same-label physical gaps."""

    settings = options.contract()
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    graph = analysis.graph
    canvas = _CanvasMetrics(
        graph.width_px, graph.height_px, graph.pixel_width_mm, graph.pixel_height_mm
    )
    features: list[ClearanceFeature] = []
    # Balance both large crops and the per-region overhead across bounded batches.
    batches: list[list[tuple[RegionNode, bytes]]] = [
        [] for _ in range(min(max_workers * 4, len(graph.regions)))
    ]
    costs = [0] * len(batches)
    ordered_regions = (
        sorted(
            enumerate(graph.regions),
            key=lambda pair: (
                -(pair[1].pixel_bounds.width * pair[1].pixel_bounds.height + 256),
                pair[0],
            ),
        )
        if max_workers > 1
        else enumerate(graph.regions)
    )
    for index, region in ordered_regions:
        if check_canceled:
            check_canceled()
        bounds = region.pixel_bounds
        crop = analysis.assignment[
            bounds.y : bounds.y + bounds.height,
            bounds.x : bounds.x + bounds.width,
        ]
        mask = crop == index
        if not np.any(mask):  # pragma: no cover - RegionAnalysis invariant
            raise ValueError(f"region assignment is missing region {region.id}")
        if max_workers == 1:
            features.extend(_region_features(canvas, region, mask, settings))
        else:
            target = min(range(len(batches)), key=costs.__getitem__)
            batches[target].append((region, np.packbits(mask).tobytes()))
            costs[target] += mask.size + 256
    if max_workers > 1:
        results = process_map(
            _region_batch_features,
            [_RegionBatch(canvas, settings, tuple(batch)) for batch in batches if batch],
            max_workers=max_workers,
            check_canceled=check_canceled,
        )
        for result in results:
            features.extend(result)
    if check_canceled:
        check_canceled()
    features.extend(_gap_features(analysis, settings))
    ordered = tuple(sorted(features, key=lambda item: item.id))
    return ClearanceAnalysis(
        graph_fingerprint=analysis.graph.fingerprint(),
        options=settings,
        options_fingerprint=settings.fingerprint(),
        features=ordered,
        summary=_summary(ordered),
    )


def validate_clearance_analysis(
    classification: ClearanceAnalysis, analysis: RegionAnalysis
) -> None:
    validate_clearance_analysis_against_graph(classification, analysis.graph)
    options = ClearanceAnalysisOptions(
        nozzle_mm=classification.options.nozzle_mm,
        minimum_width_mm=classification.options.minimum_width_mm,
        minimum_line_width_mm=classification.options.minimum_line_width_mm,
        minimum_neck_width_mm=classification.options.minimum_neck_width_mm,
        minimum_gap_width_mm=classification.options.minimum_gap_width_mm,
        minimum_line_length_mm=classification.options.minimum_line_length_mm,
        minimum_line_aspect_ratio=classification.options.minimum_line_aspect_ratio,
        wide_support_ratio=classification.options.wide_support_ratio,
        error_ratio=classification.options.error_ratio,
    )
    if classification != analyze_clearance_features(analysis, options=options):
        raise ValueError("clearance analysis content does not match its assignment and settings")


def validate_clearance_analysis_against_graph(
    classification: ClearanceAnalysis, graph: RegionGraph
) -> None:
    """Validate serialized feature references without requiring the private assignment raster."""

    if classification.graph_fingerprint != graph.fingerprint():
        raise ValueError("clearance analysis does not match the region graph fingerprint")
    regions = {region.id: region for region in graph.regions}
    for feature in classification.features:
        unknown = {region_id for region_id in feature.region_ids if region_id not in regions}
        if unknown:
            raise ValueError(f"clearance feature references unknown regions: {sorted(unknown)}")
        expected_labels = tuple(
            sorted({regions[region_id].label for region_id in feature.region_ids})
        )
        if feature.labels != expected_labels:
            raise ValueError("clearance feature labels do not match its referenced regions")
        bounds = feature.pixel_bounds
        if bounds.x + bounds.width > graph.width_px or bounds.y + bounds.height > graph.height_px:
            raise ValueError("clearance feature bounds exceed the graph canvas")
        if feature.bounds != _physical_bounds(bounds, graph.pixel_width_mm, graph.pixel_height_mm):
            raise ValueError("clearance feature physical bounds do not match its pixel bounds")


def clearance_risk_findings(
    classification: ClearanceAnalysis, analysis: RegionAnalysis
) -> tuple[RiskFinding, ...]:
    """Materialize canonical risk findings from verified clearance features."""

    validate_clearance_analysis_against_graph(classification, analysis.graph)
    return tuple(
        _feature_finding(feature, classification.options) for feature in classification.features
    )


def _region_features(
    graph: _CanvasMetrics,
    region: RegionNode,
    mask: np.ndarray,
    settings: ClearanceAnalysisSettings,
) -> list[ClearanceFeature]:
    pixel_width = graph.pixel_width_mm
    pixel_height = graph.pixel_height_mm
    clearance = _euclidean_distance_to_exterior(mask, pixel_width, pixel_height)
    ridge = _clearance_ridge(mask, clearance)
    widths = _orthogonal_widths(mask, pixel_width, pixel_height)
    ridge_values = widths[ridge]
    if not ridge_values.size:  # pragma: no cover - every nonempty region has a ridge
        return []
    features: list[ClearanceFeature] = []
    physical_length = math.hypot(
        region.physical_bounds.width_mm,
        region.physical_bounds.height_mm,
    )
    median_width = float(np.median(ridge_values))
    p75_width = float(np.percentile(ridge_values, 75))
    aspect_ratio = physical_length / max(median_width, min(pixel_width, pixel_height))
    if (
        physical_length >= settings.minimum_line_length_mm
        and aspect_ratio >= settings.minimum_line_aspect_ratio
        and _below(p75_width, settings.minimum_line_width_mm)
        and _below(
            float(np.max(ridge_values)),
            settings.minimum_line_width_mm * settings.wide_support_ratio,
        )
    ):
        features.append(
            _feature(
                ClearanceFeatureKind.THIN_LINE,
                (region.id,),
                (region.label,),
                region.pixel_bounds,
                graph,
                ridge_values,
                physical_length,
                int(np.count_nonzero(ridge)),
                region.border_contact.touches_canvas_border,
            )
        )

    tolerance = max(1e-12, settings.minimum_neck_width_mm * 1e-9)
    narrow = ridge & (widths < settings.minimum_neck_width_mm - tolerance)
    printable = ridge & ~narrow
    printable_components, printable_count = _components8(printable)
    if printable_count < 2 or not np.any(narrow):
        return features
    narrow_components, narrow_count = _components8(narrow)
    for component in range(narrow_count):
        component_mask = narrow_components == component
        touching = set(
            int(value)
            for value in _neighbor_values(printable_components, component_mask)
            if value >= 0
        )
        supported = [
            item
            for item in sorted(touching)
            if float(np.max(widths[printable_components == item]))
            >= settings.minimum_neck_width_mm * settings.wide_support_ratio
        ]
        if len(supported) < 2:
            continue
        local_bounds = _mask_bounds(component_mask)
        global_bounds = PixelBounds(
            x=region.pixel_bounds.x + local_bounds.x,
            y=region.pixel_bounds.y + local_bounds.y,
            width=local_bounds.width,
            height=local_bounds.height,
        )
        values = widths[component_mask]
        length = max(
            global_bounds.width * pixel_width,
            global_bounds.height * pixel_height,
            min(pixel_width, pixel_height),
        )
        features.append(
            _feature(
                ClearanceFeatureKind.NARROW_NECK,
                (region.id,),
                (region.label,),
                global_bounds,
                graph,
                values,
                length,
                int(values.size),
                _touches_canvas(global_bounds, graph.width_px, graph.height_px),
            )
        )
    return features


def _gap_features(
    analysis: RegionAnalysis, settings: ClearanceAnalysisSettings
) -> list[ClearanceFeature]:
    graph = analysis.graph
    assignment = analysis.assignment
    labels_by_region = np.asarray([region.label for region in graph.regions], dtype=np.int16)
    maximum_dx = min(
        graph.width_px - 1,
        math.ceil(settings.minimum_gap_width_mm / graph.pixel_width_mm) + 1,
    )
    maximum_dy = min(
        graph.height_px - 1,
        math.ceil(settings.minimum_gap_width_mm / graph.pixel_height_mm) + 1,
    )
    evidence: dict[tuple[int, int], list[tuple[float, int, int, int, int]]] = {}
    offsets = []
    for dy in range(maximum_dy + 1):
        for dx in range(-maximum_dx, maximum_dx + 1):
            if dy == 0 and dx <= 0:
                continue
            gap_x = max(abs(dx) - 1, 0) * graph.pixel_width_mm
            gap_y = max(dy - 1, 0) * graph.pixel_height_mm
            gap = math.hypot(gap_x, gap_y)
            if gap > max(1e-12, settings.minimum_gap_width_mm * 1e-9) and _below(
                gap, settings.minimum_gap_width_mm
            ):
                offsets.append((gap, dy, dx))
    if len(offsets) > 128:
        evidence = _voronoi_gap_evidence(analysis, settings)
        return _gap_features_from_evidence(analysis, settings, evidence)
    for gap, dy, dx in sorted(offsets):
        first_y, second_y = _paired_slices(graph.height_px, dy)
        first_x, second_x = _paired_slices(graph.width_px, dx)
        first = assignment[first_y, first_x]
        second = assignment[second_y, second_x]
        valid = (first >= 0) & (second >= 0) & (first != second)
        if not np.any(valid):
            continue
        first_labels = np.full(first.shape, -1, dtype=np.int16)
        second_labels = np.full(second.shape, -2, dtype=np.int16)
        first_labels[valid] = labels_by_region[first[valid]]
        second_labels[valid] = labels_by_region[second[valid]]
        same_label = valid & (first_labels == second_labels)
        ys, xs = np.nonzero(same_label)
        for y, x in zip(ys.tolist(), xs.tolist()):
            first_region = int(first[y, x])
            second_region = int(second[y, x])
            pair = tuple(sorted((first_region, second_region)))
            y1 = y + first_y.start
            x1 = x + first_x.start
            y2 = y + second_y.start
            x2 = x + second_x.start
            evidence.setdefault(pair, []).append((gap, y1, x1, y2, x2))

    return _gap_features_from_evidence(analysis, settings, evidence)


def _voronoi_gap_evidence(
    analysis: RegionAnalysis, settings: ClearanceAnalysisSettings
) -> dict[tuple[int, int], list[tuple[float, int, int, int, int]]]:
    """Find physically neighboring same-label regions without radius-squared offset work."""

    graph = analysis.graph
    assignment = analysis.assignment
    evidence: dict[tuple[int, int], list[tuple[float, int, int, int, int]]] = {}
    region_labels = np.asarray([region.label for region in graph.regions], dtype=np.int16)
    for palette in graph.palette:
        region_indices = np.flatnonzero(region_labels == palette.label)
        if region_indices.size < 2:
            continue
        sources = np.isin(assignment, region_indices)
        source_y, source_x = _nearest_sources(sources, graph.pixel_width_mm, graph.pixel_height_mm)
        nearest_region = assignment[source_y, source_x]
        for dy, dx in ((0, 1), (1, -1), (1, 0), (1, 1)):
            first_y, second_y = _paired_slices(graph.height_px, dy)
            first_x, second_x = _paired_slices(graph.width_px, dx)
            first_region = nearest_region[first_y, first_x]
            second_region = nearest_region[second_y, second_x]
            different = first_region != second_region
            if not np.any(different):
                continue
            first_source_y = source_y[first_y, first_x]
            first_source_x = source_x[first_y, first_x]
            second_source_y = source_y[second_y, second_x]
            second_source_x = source_x[second_y, second_x]
            gap_x = np.maximum(np.abs(first_source_x - second_source_x) - 1, 0)
            gap_y = np.maximum(np.abs(first_source_y - second_source_y) - 1, 0)
            gaps = np.hypot(
                gap_x * graph.pixel_width_mm,
                gap_y * graph.pixel_height_mm,
            )
            tolerance = max(1e-12, settings.minimum_gap_width_mm * 1e-9)
            valid = (
                different & (gaps > tolerance) & (gaps < settings.minimum_gap_width_mm - tolerance)
            )
            ys, xs = np.nonzero(valid)
            for y, x in zip(ys.tolist(), xs.tolist()):
                first_index = int(first_region[y, x])
                second_index = int(second_region[y, x])
                pair = tuple(sorted((first_index, second_index)))
                y1 = int(first_source_y[y, x])
                x1 = int(first_source_x[y, x])
                y2 = int(second_source_y[y, x])
                x2 = int(second_source_x[y, x])
                evidence.setdefault(pair, []).append((float(gaps[y, x]), y1, x1, y2, x2))
    return evidence


def _gap_features_from_evidence(
    analysis: RegionAnalysis,
    settings: ClearanceAnalysisSettings,
    evidence: dict[tuple[int, int], list[tuple[float, int, int, int, int]]],
) -> list[ClearanceFeature]:
    graph = analysis.graph

    features = []
    for (first_index, second_index), samples in sorted(evidence.items()):
        samples.sort()
        gaps = np.asarray([sample[0] for sample in samples], dtype=np.float64)
        coordinates = [(y1, x1) for _gap, y1, x1, _y2, _x2 in samples]
        coordinates.extend((y2, x2) for _gap, _y1, _x1, y2, x2 in samples)
        ys = [point[0] for point in coordinates]
        xs = [point[1] for point in coordinates]
        bounds = PixelBounds(
            x=min(xs),
            y=min(ys),
            width=max(xs) - min(xs) + 1,
            height=max(ys) - min(ys) + 1,
        )
        regions = (graph.regions[first_index], graph.regions[second_index])
        length = max(
            bounds.width * graph.pixel_width_mm,
            bounds.height * graph.pixel_height_mm,
            min(graph.pixel_width_mm, graph.pixel_height_mm),
        )
        if length < settings.minimum_line_length_mm:
            continue
        features.append(
            _feature(
                ClearanceFeatureKind.NARROW_GAP,
                tuple(sorted(region.id for region in regions)),
                (regions[0].label,),
                bounds,
                graph,
                gaps,
                length,
                len(samples),
                _touches_canvas(bounds, graph.width_px, graph.height_px),
            )
        )
    return features


def _euclidean_distance_to_exterior(
    mask: np.ndarray, pixel_width_mm: float, pixel_height_mm: float
) -> np.ndarray:
    padded = np.pad(mask, 1, constant_values=False)
    values = np.where(padded, math.inf, 0.0)
    horizontal = np.empty_like(values, dtype=np.float64)
    for y in range(values.shape[0]):
        horizontal[y] = _distance_transform_1d(values[y], pixel_width_mm)
    squared = np.empty_like(horizontal)
    for x in range(horizontal.shape[1]):
        squared[:, x] = _distance_transform_1d(horizontal[:, x], pixel_height_mm)
    return np.sqrt(squared[1:-1, 1:-1])


def _distance_transform_1d(values: np.ndarray, spacing: float) -> np.ndarray:
    """Felzenszwalb/Huttenlocher lower-envelope squared distance transform."""

    return _distance_transform_1d_with_indices(values, spacing)[0]


def _distance_transform_1d_with_indices(
    values: np.ndarray, spacing: float
) -> tuple[np.ndarray, np.ndarray]:
    """Squared distance plus the deterministic nearest source index."""

    sites = np.flatnonzero(np.isfinite(values))
    if not sites.size:
        return (
            np.full(values.shape, math.inf, dtype=np.float64),
            np.full(values.shape, -1, dtype=np.int32),
        )
    count = values.size
    vertices = np.empty(sites.size, dtype=np.int32)
    boundaries = np.empty(sites.size + 1, dtype=np.float64)
    k = 0
    vertices[0] = int(sites[0])
    boundaries[0] = -math.inf
    boundaries[1] = math.inf
    coefficient = spacing * spacing
    for raw_site in sites[1:]:
        site = int(raw_site)
        while True:
            previous = int(vertices[k])
            crossing = (
                (values[site] + coefficient * site * site)
                - (values[previous] + coefficient * previous * previous)
            ) / (2 * coefficient * (site - previous))
            if crossing > boundaries[k] or k == 0:
                break
            k -= 1
        if crossing <= boundaries[k]:
            k = 0
        else:
            k += 1
        vertices[k] = site
        boundaries[k] = crossing
        boundaries[k + 1] = math.inf
    output = np.empty(count, dtype=np.float64)
    nearest = np.empty(count, dtype=np.int32)
    k = 0
    for index in range(count):
        while boundaries[k + 1] < index:
            k += 1
        site = int(vertices[k])
        output[index] = coefficient * (index - site) ** 2 + values[site]
        nearest[index] = site
    return output, nearest


def _nearest_sources(
    sources: np.ndarray, pixel_width_mm: float, pixel_height_mm: float
) -> tuple[np.ndarray, np.ndarray]:
    values = np.where(sources, 0.0, math.inf)
    horizontal = np.empty_like(values, dtype=np.float64)
    horizontal_source_x = np.full(values.shape, -1, dtype=np.int32)
    for y in range(values.shape[0]):
        horizontal[y], horizontal_source_x[y] = _distance_transform_1d_with_indices(
            values[y], pixel_width_mm
        )
    source_y = np.empty(values.shape, dtype=np.int32)
    for x in range(values.shape[1]):
        _distance, source_y[:, x] = _distance_transform_1d_with_indices(
            horizontal[:, x], pixel_height_mm
        )
    columns = np.arange(values.shape[1])[None, :]
    if np.any(source_y < 0):  # pragma: no cover - callers require at least one source
        raise ValueError("nearest-source transform requires at least one source")
    source_x = horizontal_source_x[source_y, columns]
    if np.any(source_x < 0):  # pragma: no cover - second pass selects finite source rows
        raise ValueError("nearest-source transform produced an invalid source coordinate")
    return source_y, source_x


def _clearance_ridge(mask: np.ndarray, distance: np.ndarray) -> np.ndarray:
    padded = np.pad(distance, 1, constant_values=0)
    center = padded[1:-1, 1:-1]
    pairs = (
        (padded[1:-1, :-2], padded[1:-1, 2:]),
        (padded[:-2, 1:-1], padded[2:, 1:-1]),
        (padded[:-2, :-2], padded[2:, 2:]),
        (padded[:-2, 2:], padded[2:, :-2]),
    )
    ridge = np.zeros(mask.shape, dtype=bool)
    for first, second in pairs:
        ridge |= (center >= first) & (center >= second) & ((center > first) | (center > second))
    return mask & ridge


def _orthogonal_widths(
    mask: np.ndarray, pixel_width_mm: float, pixel_height_mm: float
) -> np.ndarray:
    horizontal = np.zeros(mask.shape, dtype=np.float64)
    vertical = np.zeros(mask.shape, dtype=np.float64)
    for y in range(mask.shape[0]):
        for start, end in _true_runs(mask[y]):
            horizontal[y, start:end] = (end - start) * pixel_width_mm
    for x in range(mask.shape[1]):
        for start, end in _true_runs(mask[:, x]):
            vertical[start:end, x] = (end - start) * pixel_height_mm
    return np.minimum(horizontal, vertical)


def _true_runs(values: np.ndarray) -> tuple[tuple[int, int], ...]:
    padded = np.pad(values.astype(np.int8), 1)
    transitions = np.diff(padded)
    starts = np.flatnonzero(transitions == 1)
    ends = np.flatnonzero(transitions == -1)
    return tuple((int(start), int(end)) for start, end in zip(starts, ends))


def _components8(mask: np.ndarray) -> tuple[np.ndarray, int]:
    labels = np.full(mask.shape, -1, dtype=np.int32)
    component = 0
    for y, x in zip(*np.nonzero(mask)):
        if labels[y, x] >= 0:
            continue
        labels[y, x] = component
        stack = [(int(y), int(x))]
        while stack:
            current_y, current_x = stack.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy == dx == 0:
                        continue
                    neighbor_y = current_y + dy
                    neighbor_x = current_x + dx
                    if (
                        0 <= neighbor_y < mask.shape[0]
                        and 0 <= neighbor_x < mask.shape[1]
                        and mask[neighbor_y, neighbor_x]
                        and labels[neighbor_y, neighbor_x] < 0
                    ):
                        labels[neighbor_y, neighbor_x] = component
                        stack.append((neighbor_y, neighbor_x))
        component += 1
    return labels, component


def _neighbor_values(labels: np.ndarray, mask: np.ndarray) -> np.ndarray:
    padded = np.pad(labels, 1, constant_values=-1)
    values = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == dx == 0:
                continue
            values.append(
                padded[1 + dy : 1 + dy + mask.shape[0], 1 + dx : 1 + dx + mask.shape[1]][mask]
            )
    return np.concatenate(values) if values else np.empty(0, dtype=np.int32)


def _paired_slices(length: int, delta: int) -> tuple[slice, slice]:
    if delta >= 0:
        return slice(0, length - delta), slice(delta, length)
    return slice(-delta, length), slice(0, length + delta)


def _feature(
    kind: ClearanceFeatureKind,
    region_ids: tuple[str, ...],
    labels: tuple[int, ...],
    pixel_bounds: PixelBounds,
    graph,
    widths: np.ndarray,
    length_mm: float,
    sample_count: int,
    touches_canvas_border: bool,
) -> ClearanceFeature:
    ordered_regions = tuple(sorted(set(region_ids)))
    ordered_labels = tuple(sorted(set(labels)))
    return ClearanceFeature(
        id=_feature_id(kind, ordered_regions, pixel_bounds),
        kind=kind,
        region_ids=ordered_regions,
        labels=ordered_labels,
        pixel_bounds=pixel_bounds,
        bounds=_physical_bounds(pixel_bounds, graph.pixel_width_mm, graph.pixel_height_mm),
        minimum_width_mm=float(np.min(widths)),
        median_width_mm=float(np.median(widths)),
        maximum_width_mm=float(np.max(widths)),
        length_mm=length_mm,
        evidence_sample_count=sample_count,
        touches_canvas_border=touches_canvas_border,
    )


def _feature_id(
    kind: ClearanceFeatureKind, region_ids: tuple[str, ...], bounds: PixelBounds
) -> str:
    payload = json.dumps(
        {
            "bounds": bounds.model_dump(mode="json"),
            "kind": kind.value,
            "region_ids": region_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"feature_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _mask_bounds(mask: np.ndarray) -> PixelBounds:
    ys, xs = np.nonzero(mask)
    return PixelBounds(
        x=int(xs.min()),
        y=int(ys.min()),
        width=int(xs.max() - xs.min() + 1),
        height=int(ys.max() - ys.min() + 1),
    )


def _physical_bounds(
    bounds: PixelBounds, pixel_width: float, pixel_height: float
) -> PhysicalBounds:
    return PhysicalBounds(
        x_mm=bounds.x * pixel_width,
        y_mm=bounds.y * pixel_height,
        width_mm=bounds.width * pixel_width,
        height_mm=bounds.height * pixel_height,
    )


def _touches_canvas(bounds: PixelBounds, width: int, height: int) -> bool:
    return (
        bounds.x == 0
        or bounds.y == 0
        or bounds.x + bounds.width == width
        or bounds.y + bounds.height == height
    )


def _feature_finding(feature: ClearanceFeature, settings: ClearanceAnalysisSettings) -> RiskFinding:
    code = RiskCode(feature.kind.value)
    threshold = {
        RiskCode.THIN_LINE: settings.minimum_line_width_mm,
        RiskCode.NARROW_NECK: settings.minimum_neck_width_mm,
        RiskCode.NARROW_GAP: settings.minimum_gap_width_mm,
    }[code]
    measurements = [
        RiskMeasurement(
            key=(
                RiskMeasurementKey.GAP_WIDTH
                if code == RiskCode.NARROW_GAP
                else RiskMeasurementKey.MINIMUM_WIDTH
            ),
            role=RiskMeasurementRole.MEASURED,
            value=feature.minimum_width_mm,
            unit=RiskUnit.MM,
        ),
        RiskMeasurement(
            key=RiskMeasurementKey.LENGTH,
            role=RiskMeasurementRole.MEASURED,
            value=feature.length_mm,
            unit=RiskUnit.MM,
        ),
        RiskMeasurement(
            key=(
                RiskMeasurementKey.GAP_WIDTH
                if code == RiskCode.NARROW_GAP
                else RiskMeasurementKey.MINIMUM_WIDTH
            ),
            role=RiskMeasurementRole.THRESHOLD,
            value=threshold,
            unit=RiskUnit.MM,
        ),
        RiskMeasurement(
            key=RiskMeasurementKey.NOZZLE_DIAMETER,
            role=RiskMeasurementRole.CONTEXT,
            value=settings.nozzle_mm,
            unit=RiskUnit.MM,
        ),
    ]
    if code != RiskCode.NARROW_GAP:
        measurements.extend(
            (
                RiskMeasurement(
                    key=RiskMeasurementKey.MEDIAN_WIDTH,
                    role=RiskMeasurementRole.MEASURED,
                    value=feature.median_width_mm,
                    unit=RiskUnit.MM,
                ),
                RiskMeasurement(
                    key=RiskMeasurementKey.MAXIMUM_WIDTH,
                    role=RiskMeasurementRole.MEASURED,
                    value=feature.maximum_width_mm,
                    unit=RiskUnit.MM,
                ),
            )
        )
    titles = {
        RiskCode.THIN_LINE: "Thin intentional-or-accidental line",
        RiskCode.NARROW_NECK: "Narrow connection",
        RiskCode.NARROW_GAP: "Narrow same-color gap",
    }
    explanations = {
        RiskCode.THIN_LINE: (
            "This elongated region is narrower than the configured printable width. Preserve it "
            "as intentional or widen it; do not treat it as compact island dust."
        ),
        RiskCode.NARROW_NECK: (
            "This local clearance ridge connects two wider supported portions through a width "
            "below the configured printable threshold."
        ),
        RiskCode.NARROW_GAP: (
            "These separate same-color regions approach more closely than the configured width, "
            "so slicing may close the measured gap."
        ),
    }
    return RiskFinding(
        code=code,
        feature_key=feature.id,
        severity=(
            RiskSeverity.ERROR
            if feature.minimum_width_mm / threshold <= settings.error_ratio
            else RiskSeverity.WARNING
        ),
        title=titles[code],
        explanation=explanations[code],
        affected_region_ids=feature.region_ids,
        affected_labels=feature.labels,
        affected_bounds=(feature.bounds,),
        measurements=tuple(measurements),
        suggestions=default_risk_suggestions(code),
        classifier_id="physical-clearance-ridge",
        classifier_version="1",
    )


def _summary(features: tuple[ClearanceFeature, ...]) -> ClearanceAnalysisSummary:
    counts = {kind: 0 for kind in ClearanceFeatureKind}
    for feature in features:
        counts[feature.kind] += 1
    return ClearanceAnalysisSummary(
        feature_count=len(features),
        thin_line_count=counts[ClearanceFeatureKind.THIN_LINE],
        narrow_neck_count=counts[ClearanceFeatureKind.NARROW_NECK],
        narrow_gap_count=counts[ClearanceFeatureKind.NARROW_GAP],
    )


def _below(value: float, threshold: float) -> bool:
    return value < threshold - max(1e-12, threshold * 1e-9)
