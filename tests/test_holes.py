from __future__ import annotations

import tracemalloc
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from image23mf.engine.holes import (
    HoleAnalysis,
    HoleAnalysisOptions,
    HoleCenterKind,
    HoleCorrectionPolicy,
    HoleCorrectionRequest,
    HoleDecisionStatus,
    HoleFeatureKind,
    apply_hole_policy,
    classify_holes,
    hole_risk_findings,
    validate_hole_analysis,
)
from image23mf.engine.labels import LabelField
from image23mf.engine.palette import classify_palette
from image23mf.engine.regions import analyze_regions
from image23mf.engine.risks import RiskActionKind, RiskCode

PUBLIC_FIXTURES = Path(__file__).parent / "fixtures" / "public-regression"


def _analysis(
    rows: list[list[int]],
    *,
    labels: tuple[int, ...] = (0, 1),
    width_mm: float | None = None,
    height_mm: float | None = None,
    active: bytes | None = None,
):
    array = np.asarray(rows, dtype=np.uint8)
    field = LabelField(
        width=array.shape[1],
        height=array.shape[0],
        label_values=labels,
        pixels=array.tobytes(),
    )
    colors = {0: "#FFFFFF", 1: "#000000", 2: "#FF0000"}
    analysis = analyze_regions(
        field,
        colors={label: colors[label] for label in labels},
        width_mm=width_mm or field.width,
        height_mm=height_mm or field.height,
        active=active,
    )
    return field, analysis


def _ring(*, holes: tuple[tuple[int, int], ...] = ((4, 4),)) -> list[list[int]]:
    rows = np.zeros((9, 9), dtype=np.uint8)
    rows[2:7, 2:7] = 1
    for y, x in holes:
        rows[y, x] = 0
    return rows.tolist()


def _options(**changes) -> HoleAnalysisOptions:
    values = {
        "maximum_area_mm2": 2.0,
        "maximum_equivalent_diameter_mm": 2.0,
        "minimum_surviving_ring_width_mm": 2.0,
        "maximum_ring_to_center_area_ratio": 64.0,
        "error_ratio": 0.5,
    }
    values.update(changes)
    return HoleAnalysisOptions(**values)


def test_isolated_small_center_with_surviving_local_wall_is_hollow_ring() -> None:
    _field, analysis = _analysis(_ring())
    classification = classify_holes(analysis, options=_options())

    assert classification.summary.model_dump() == {
        "feature_count": 1,
        "tiny_hole_count": 0,
        "hollow_ring_count": 1,
        "transparent_center_count": 0,
    }
    feature = classification.features[0]
    assert feature.kind == HoleFeatureKind.HOLLOW_RING
    assert feature.center_kind == HoleCenterKind.ACTIVE_REGION
    assert feature.center_area_mm2 == 1
    assert feature.ring_pixel_count == 24
    assert feature.ring_median_width_mm == 2
    assert feature.ring_to_center_area_ratio == 24
    finding = hole_risk_findings(classification, analysis.graph)[0]
    assert finding.code == RiskCode.HOLLOW_RING
    assert {suggestion.kind for suggestion in finding.suggestions} == {
        RiskActionKind.REVIEW,
        RiskActionKind.KEEP,
        RiskActionKind.FILL_HOLE,
        RiskActionKind.RECOLOR_HOLE,
        RiskActionKind.COLLAPSE_RING,
    }


def test_many_enclosed_regions_have_bounded_analysis_memory() -> None:
    rows = np.ones((161, 161), dtype=np.uint8)
    rows[2:-2:4, 2:-2:4] = 0
    _field, analysis = _analysis(rows.tolist())
    tracemalloc.start()
    try:
        classification = classify_holes(analysis, options=_options())
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(classification.features) == 40 * 40
    # One full-canvas mask per feature alone would exceed 39 MiB here.
    assert peak < 32 * 1024 * 1024


def test_shared_ring_outer_selection_excludes_each_center() -> None:
    rows = np.asarray(_ring(), dtype=np.uint8)
    rows[4, 4] = 1
    rows[4, 3] = rows[4, 5] = 2
    _field, analysis = _analysis(rows.tolist(), labels=(0, 1, 2))
    classification = classify_holes(analysis, options=_options())
    assert len(classification.features) == 2
    first, second = classification.features
    assert first.outer_region_ids == (second.center_region_id,)
    assert second.outer_region_ids == (first.center_region_id,)


def test_solid_dot_is_island_not_hole_and_open_center_is_not_enclosed() -> None:
    dot = np.zeros((7, 7), dtype=np.uint8)
    dot[3, 3] = 1
    opened = np.asarray(_ring(), dtype=np.uint8)
    opened[2:5, 4] = 0
    _dot_field, dot_analysis = _analysis(dot.tolist())
    _open_field, open_analysis = _analysis(opened.tolist())

    assert classify_holes(dot_analysis, options=_options()).features == ()
    assert classify_holes(open_analysis, options=_options()).features == ()


def test_threshold_equal_and_large_holes_are_retained() -> None:
    _field, analysis = _analysis(_ring())
    equal = classify_holes(
        analysis,
        options=_options(
            maximum_area_mm2=1,
            maximum_equivalent_diameter_mm=0,
        ),
    )
    large_center = np.asarray(_ring(), dtype=np.uint8)
    large_center[3:6, 3:6] = 0
    _large_field, large_analysis = _analysis(large_center.tolist())
    large = classify_holes(
        large_analysis,
        options=_options(maximum_area_mm2=2, maximum_equivalent_diameter_mm=2),
    )

    assert equal.features == ()
    assert large.features == ()


def test_thin_or_shared_wall_downgrades_to_tiny_hole_without_collapse_action() -> None:
    thin = np.zeros((7, 7), dtype=np.uint8)
    thin[1:6, 1:6] = 1
    thin[2:5, 2:5] = 0
    thin[3, 3] = 1
    _thin_field, thin_analysis = _analysis(thin.tolist())
    thin_result = classify_holes(
        thin_analysis,
        options=_options(
            maximum_area_mm2=10,
            maximum_equivalent_diameter_mm=5,
            minimum_surviving_ring_width_mm=2,
        ),
    )

    assert all(feature.kind == HoleFeatureKind.TINY_HOLE for feature in thin_result.features)
    assert all(
        RiskActionKind.COLLAPSE_RING not in {item.kind for item in finding.suggestions}
        for finding in hole_risk_findings(thin_result, thin_analysis.graph)
    )


def test_transparent_enclosed_center_is_classified_and_fill_activates_it() -> None:
    rows = np.asarray(_ring(), dtype=np.uint8)
    active = np.ones(rows.shape, dtype=np.uint8)
    active[4, 4] = 0
    field, analysis = _analysis(rows.tolist(), active=active.tobytes())
    classification = classify_holes(analysis, options=_options())
    feature = classification.features[0]

    assert feature.center_kind == HoleCenterKind.TRANSPARENT_VOID
    assert feature.center_region_id is None
    assert classification.summary.transparent_center_count == 1
    result = apply_hole_policy(
        field,
        analysis,
        classification,
        HoleCorrectionRequest(
            policy=HoleCorrectionPolicy.FILL_HOLE,
            feature_ids=(feature.id,),
        ),
    )
    result_active = np.frombuffer(result.active, dtype=np.uint8).reshape(rows.shape)
    result_labels = np.frombuffer(result.labels.pixels, dtype=np.uint8).reshape(rows.shape)
    assert result_active[4, 4] == 1
    assert result_labels[4, 4] == 1
    assert result.record.changed_pixel_count == 1


def test_fill_recolor_and_local_collapse_are_explicit_with_lineage() -> None:
    field, analysis = _analysis(_ring(), labels=(0, 1, 2))
    classification = classify_holes(analysis, options=_options())
    feature = classification.features[0]
    fill = apply_hole_policy(
        field,
        analysis,
        classification,
        HoleCorrectionRequest(
            policy=HoleCorrectionPolicy.FILL_HOLE,
            feature_ids=(feature.id,),
        ),
    )
    recolor = apply_hole_policy(
        field,
        analysis,
        classification,
        HoleCorrectionRequest(
            policy=HoleCorrectionPolicy.RECOLOR_CENTER,
            feature_ids=(feature.id,),
            explicit_target_label=2,
        ),
    )
    collapse = apply_hole_policy(
        field,
        analysis,
        classification,
        HoleCorrectionRequest(
            policy=HoleCorrectionPolicy.COLLAPSE_RING,
            feature_ids=(feature.id,),
        ),
    )

    assert fill.record.changed_pixel_count == 1
    assert recolor.record.changed_pixel_count == 1
    assert collapse.record.changed_pixel_count == feature.ring_pixel_count
    assert len(fill.record.lineage.events) > 0
    collapse_pixels = np.frombuffer(collapse.labels.pixels, dtype=np.uint8)
    assert np.all(collapse_pixels == 0)


def test_review_and_keep_are_non_destructive_and_invalid_requests_are_atomic() -> None:
    field, analysis = _analysis(_ring())
    classification = classify_holes(analysis, options=_options())
    feature = classification.features[0]
    for policy, status in (
        (HoleCorrectionPolicy.REVIEW, HoleDecisionStatus.REVIEW_REQUIRED),
        (HoleCorrectionPolicy.KEEP, HoleDecisionStatus.KEPT),
    ):
        result = apply_hole_policy(
            field,
            analysis,
            classification,
            HoleCorrectionRequest(policy=policy, feature_ids=(feature.id,)),
        )
        assert result.labels == field
        assert result.record.changed_pixel_count == 0
        assert result.record.decisions[0].status == status

    with pytest.raises(ValueError, match="absent"):
        apply_hole_policy(
            field,
            analysis,
            classification,
            HoleCorrectionRequest(
                policy=HoleCorrectionPolicy.RECOLOR_CENTER,
                feature_ids=(feature.id,),
                explicit_target_label=2,
            ),
        )
    with pytest.raises(ValidationError, match="requires"):
        HoleCorrectionRequest(
            policy=HoleCorrectionPolicy.RECOLOR_CENTER,
            feature_ids=(feature.id,),
        )


def test_two_local_rings_on_one_region_cannot_overlap_destructive_bulk_request() -> None:
    rows = np.zeros((9, 13), dtype=np.uint8)
    rows[2:7, 2:11] = 1
    rows[4, 4] = 0
    rows[4, 8] = 0
    field, analysis = _analysis(rows.tolist())
    classification = classify_holes(analysis, options=_options())
    rings = tuple(
        sorted(
            feature.id
            for feature in classification.features
            if feature.kind == HoleFeatureKind.HOLLOW_RING
        )
    )

    assert len(rings) == 2
    with pytest.raises(ValueError, match="overlap"):
        apply_hole_policy(
            field,
            analysis,
            classification,
            HoleCorrectionRequest(
                policy=HoleCorrectionPolicy.COLLAPSE_RING,
                feature_ids=rings,
            ),
        )


def test_analysis_is_deterministic_reconstructable_and_rejects_tampering() -> None:
    _field, analysis = _analysis(_ring())
    first = classify_holes(analysis, options=_options())
    second = classify_holes(analysis, options=_options())

    assert first == second
    assert first.canonical_json() == second.canonical_json()
    assert len(first.fingerprint()) == 64
    validate_hole_analysis(first, analysis)
    payload = first.model_dump(mode="json")
    payload["summary"]["feature_count"] += 1
    with pytest.raises(ValidationError, match="summary"):
        HoleAnalysis.model_validate(payload)


def test_synthetic_ring_grid_crop_exposes_local_ring_corrections_without_mutation() -> None:
    with Image.open(PUBLIC_FIXTURES / "geometry-stress.png") as source:
        crop = source.convert("RGBA").crop((1230, 390, 1650, 860))
    quantized = classify_palette(crop, ("#F2D6AA", "#E75B12", "#0A3A78", "#202428"))
    original = bytes(quantized.labels.pixels)
    analysis = analyze_regions(
        quantized.labels,
        colors=dict(enumerate(quantized.palette)),
        width_mm=420 * 600 / 2520,
        height_mm=470 * 400 / 1680,
        active=quantized.alpha,
    )
    classification = classify_holes(
        analysis,
        options=HoleAnalysisOptions(
            maximum_area_mm2=0.5,
            maximum_equivalent_diameter_mm=0.4,
            minimum_surviving_ring_width_mm=0.4,
        ),
    )

    assert classification.summary.feature_count > 100
    assert classification.summary.hollow_ring_count > 0
    assert all(
        feature.ring_to_center_area_ratio <= 64
        for feature in classification.features
        if feature.kind == HoleFeatureKind.HOLLOW_RING
    )
    assert quantized.labels.pixels == original
