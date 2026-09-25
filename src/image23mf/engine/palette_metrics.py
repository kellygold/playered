"""Exact, deterministic palette coverage, error, adjacency, and fragmentation metrics."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from image23mf.engine.palette import QuantizationResult, delta_e_76, rgb_to_lab, srgb8_to_lab

PALETTE_METRICS_SCHEMA_VERSION = 1


class PaletteMetricModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PaletteColorMetrics(PaletteMetricModel):
    index: int = Field(ge=0, le=7)
    color: str = Field(pattern=r"^#[0-9A-F]{6}$")
    pixel_count: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)
    mean_delta_e: Optional[float] = Field(default=None, ge=0)
    p95_delta_e: Optional[float] = Field(default=None, ge=0)
    component_count: int = Field(ge=0)
    largest_component_share: float = Field(ge=0, le=1)
    smallest_component_pixels: Optional[int] = Field(default=None, ge=1)
    smallest_component_mm2: Optional[float] = Field(default=None, gt=0)


class PaletteAdjacencyMetrics(PaletteMetricModel):
    first_index: int = Field(ge=0, le=7)
    second_index: int = Field(ge=0, le=7)
    delta_e: float = Field(ge=0)
    boundary_edge_count: int = Field(gt=0)


class PaletteReconstructionMetrics(PaletteMetricModel):
    alpha_weighted_mean_delta_e: float = Field(ge=0)
    alpha_weighted_p95_delta_e: float = Field(ge=0)


class PaletteFragmentationMetrics(PaletteMetricModel):
    component_count: int = Field(ge=0)
    excess_component_count: int = Field(ge=0)
    single_pixel_component_count: int = Field(ge=0)
    components_per_100_mm2: float = Field(ge=0)


class PaletteMetrics(PaletteMetricModel):
    schema_version: Literal[1] = PALETTE_METRICS_SCHEMA_VERSION
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quantization_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    options_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    palette: tuple[str, ...] = Field(min_length=2, max_length=8)
    visible_pixel_count: int = Field(ge=1)
    physical_area_mm2: float = Field(gt=0)
    colors: tuple[PaletteColorMetrics, ...] = Field(min_length=2, max_length=8)
    reconstruction: PaletteReconstructionMetrics
    adjacency: tuple[PaletteAdjacencyMetrics, ...]
    minimum_adjacent_delta_e: Optional[float] = Field(default=None, ge=0)
    fragmentation: PaletteFragmentationMetrics

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
class PaletteMetricsOptions:
    chunk_pixels: int = 65_536
    alpha_threshold: int = 0

    def __post_init__(self) -> None:
        if self.chunk_pixels < 256 or self.chunk_pixels > 1_048_576:
            raise ValueError("chunk_pixels must be between 256 and 1,048,576")
        if self.alpha_threshold < 0 or self.alpha_threshold > 254:
            raise ValueError("alpha_threshold must be between 0 and 254")

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "alpha_threshold": self.alpha_threshold,
                "chunk_pixels": self.chunk_pixels,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


DEFAULT_PALETTE_METRICS_OPTIONS = PaletteMetricsOptions()


def compute_palette_metrics(
    source: Image.Image,
    result: QuantizationResult,
    *,
    config_sha256: str,
    width_mm: float,
    height_mm: float,
    options: PaletteMetricsOptions = DEFAULT_PALETTE_METRICS_OPTIONS,
) -> PaletteMetrics:
    """Measure exact preview assignments without turning indicators into a pass/fail score."""

    invalid_hash = len(config_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in config_sha256
    )
    if invalid_hash:
        raise ValueError("config_sha256 must be a lowercase SHA-256 digest")
    if not np.isfinite((width_mm, height_mm)).all() or width_mm <= 0 or height_mm <= 0:
        raise ValueError("physical dimensions must be finite and positive")
    rgba = _rgba_array(source)
    if (rgba.shape[1], rgba.shape[0]) != (result.labels.width, result.labels.height):
        raise ValueError("metric source dimensions must match the quantized label field")
    flat = rgba.reshape((-1, 4))
    labels = np.frombuffer(result.labels.pixels, dtype=np.uint8)
    alpha = np.frombuffer(result.alpha, dtype=np.uint8)
    if not np.array_equal(alpha, flat[:, 3]):
        raise ValueError("metric source alpha must match the quantization result")
    visible = alpha > options.alpha_threshold
    visible_count = int(np.count_nonzero(visible))
    if visible_count == 0:
        raise ValueError("palette metrics require at least one visible pixel")

    palette_lab = np.asarray(
        [rgb_to_lab(_hex_to_rgb(color)) for color in result.palette], dtype=np.float64
    )
    errors = _reconstruction_errors(flat, labels, visible, palette_lab, options.chunk_pixels)
    weights = alpha.astype(np.float64) / 255.0
    counts = np.bincount(labels[visible], minlength=len(result.palette))
    component_sizes = _component_sizes(
        labels.reshape((result.labels.height, result.labels.width)),
        visible.reshape((result.labels.height, result.labels.width)),
        len(result.palette),
    )
    pixel_area_mm2 = width_mm * height_mm / (result.labels.width * result.labels.height)
    colors = tuple(
        _color_metrics(
            index,
            result.palette[index],
            counts,
            errors,
            weights,
            labels,
            visible,
            component_sizes[index],
            pixel_area_mm2,
            visible_count,
        )
        for index in range(len(result.palette))
    )
    adjacency = _adjacency_metrics(
        labels.reshape((result.labels.height, result.labels.width)),
        visible.reshape((result.labels.height, result.labels.width)),
        result.palette,
    )
    all_sizes = tuple(size for sizes in component_sizes for size in sizes)
    assigned_colors = sum(count > 0 for count in counts)
    physical_area = width_mm * height_mm
    return PaletteMetrics(
        config_sha256=config_sha256,
        quantization_fingerprint=result.fingerprint(),
        options_fingerprint=options.fingerprint(),
        palette=result.palette,
        visible_pixel_count=visible_count,
        physical_area_mm2=physical_area,
        colors=colors,
        reconstruction=PaletteReconstructionMetrics(
            alpha_weighted_mean_delta_e=_weighted_mean(errors[visible], weights[visible]),
            alpha_weighted_p95_delta_e=_weighted_percentile(
                errors[visible], weights[visible], 0.95
            ),
        ),
        adjacency=adjacency,
        minimum_adjacent_delta_e=(min(item.delta_e for item in adjacency) if adjacency else None),
        fragmentation=PaletteFragmentationMetrics(
            component_count=len(all_sizes),
            excess_component_count=max(0, len(all_sizes) - assigned_colors),
            single_pixel_component_count=sum(size == 1 for size in all_sizes),
            components_per_100_mm2=len(all_sizes) / physical_area * 100,
        ),
    )


def _reconstruction_errors(
    flat: np.ndarray,
    labels: np.ndarray,
    visible: np.ndarray,
    palette_lab: np.ndarray,
    chunk_pixels: int,
) -> np.ndarray:
    errors = np.zeros(len(flat), dtype=np.float64)
    for start in range(0, len(flat), chunk_pixels):
        stop = min(len(flat), start + chunk_pixels)
        chunk_visible = visible[start:stop]
        if not np.any(chunk_visible):
            continue
        source_lab = srgb8_to_lab(flat[start:stop][chunk_visible, :3])
        assigned_lab = palette_lab[labels[start:stop][chunk_visible]]
        errors[start:stop][chunk_visible] = np.sqrt(
            np.sum((source_lab - assigned_lab) ** 2, axis=1)
        )
    return errors


def _color_metrics(
    index: int,
    color: str,
    counts: np.ndarray,
    errors: np.ndarray,
    weights: np.ndarray,
    labels: np.ndarray,
    visible: np.ndarray,
    component_sizes: tuple[int, ...],
    pixel_area_mm2: float,
    visible_count: int,
) -> PaletteColorMetrics:
    selected = visible & (labels == index)
    pixel_count = int(counts[index])
    return PaletteColorMetrics(
        index=index,
        color=color,
        pixel_count=pixel_count,
        coverage_ratio=pixel_count / visible_count,
        mean_delta_e=(_weighted_mean(errors[selected], weights[selected]) if pixel_count else None),
        p95_delta_e=(
            _weighted_percentile(errors[selected], weights[selected], 0.95) if pixel_count else None
        ),
        component_count=len(component_sizes),
        largest_component_share=(max(component_sizes) / pixel_count if pixel_count else 0),
        smallest_component_pixels=(min(component_sizes) if component_sizes else None),
        smallest_component_mm2=(min(component_sizes) * pixel_area_mm2 if component_sizes else None),
    )


def _adjacency_metrics(
    labels: np.ndarray, visible: np.ndarray, palette: tuple[str, ...]
) -> tuple[PaletteAdjacencyMetrics, ...]:
    edge_counts: defaultdict[tuple[int, int], int] = defaultdict(int)
    for first, second, active in (
        (labels[:, :-1], labels[:, 1:], visible[:, :-1] & visible[:, 1:]),
        (labels[:-1, :], labels[1:, :], visible[:-1, :] & visible[1:, :]),
    ):
        changed = active & (first != second)
        left = first[changed]
        right = second[changed]
        pairs, counts = np.unique(
            np.sort(np.column_stack((left, right)), axis=1),
            axis=0,
            return_counts=True,
        )
        for pair, count in zip(pairs, counts):
            edge_counts[(int(pair[0]), int(pair[1]))] += int(count)
    labs = tuple(rgb_to_lab(_hex_to_rgb(color)) for color in palette)
    return tuple(
        PaletteAdjacencyMetrics(
            first_index=first,
            second_index=second,
            delta_e=delta_e_76(labs[first], labs[second]),
            boundary_edge_count=edge_counts[(first, second)],
        )
        for first, second in sorted(edge_counts)
    )


def _component_sizes(
    labels: np.ndarray, visible: np.ndarray, color_count: int
) -> tuple[tuple[int, ...], ...]:
    parents: list[int] = []
    run_sizes: list[int] = []
    run_labels: list[int] = []
    previous: list[tuple[int, int, int, int]] = []

    def make_node(size: int, label: int) -> int:
        node = len(parents)
        parents.append(node)
        run_sizes.append(size)
        run_labels.append(label)
        return node

    def root(node: int) -> int:
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    def union(first: int, second: int) -> None:
        first_root = root(first)
        second_root = root(second)
        if first_root != second_root:
            parents[max(first_root, second_root)] = min(first_root, second_root)

    for row_labels, row_visible in zip(labels, visible):
        starts = np.flatnonzero(
            row_visible
            & np.concatenate(([True], (~row_visible[:-1]) | (row_labels[:-1] != row_labels[1:])))
        )
        ends = (
            np.flatnonzero(
                row_visible
                & np.concatenate(((~row_visible[1:]) | (row_labels[:-1] != row_labels[1:]), [True]))
            )
            + 1
        )
        current = [
            (
                int(start),
                int(end),
                int(row_labels[start]),
                make_node(int(end - start), int(row_labels[start])),
            )
            for start, end in zip(starts, ends)
        ]
        previous_at = 0
        for start, end, label, node in current:
            while previous_at < len(previous) and previous[previous_at][1] <= start:
                previous_at += 1
            candidate = previous_at
            while candidate < len(previous) and previous[candidate][0] < end:
                prior_start, prior_end, prior_label, prior_node = previous[candidate]
                if prior_end > start and prior_start < end and prior_label == label:
                    union(node, prior_node)
                candidate += 1
        previous = current

    sizes_by_root: defaultdict[int, int] = defaultdict(int)
    root_labels: dict[int, int] = {}
    for node, size in enumerate(run_sizes):
        component = root(node)
        sizes_by_root[component] += size
        root_labels.setdefault(component, run_labels[node])
    result: list[list[int]] = [[] for _ in range(color_count)]
    for component, size in sizes_by_root.items():
        result[root_labels[component]].append(size)
    return tuple(tuple(sorted(sizes, reverse=True)) for sizes in result)


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    return float(np.average(values, weights=weights))


def _weighted_percentile(values: np.ndarray, weights: np.ndarray, fraction: float) -> float:
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    cumulative = np.cumsum(weights[order])
    index = int(np.searchsorted(cumulative, cumulative[-1] * fraction, side="left"))
    return float(sorted_values[min(index, len(sorted_values) - 1)])


def _rgba_array(image: Image.Image) -> np.ndarray:
    converted = image.convert("RGBA")
    try:
        return np.array(converted, dtype=np.uint8, copy=True)
    finally:
        converted.close()


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
