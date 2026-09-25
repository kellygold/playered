from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from image23mf.engine.clearance import (
    ClearanceAnalysis,
    ClearanceAnalysisOptions,
    ClearanceFeatureKind,
    _distance_transform_1d,
    analyze_clearance_features,
    clearance_risk_findings,
    validate_clearance_analysis,
)
from image23mf.engine.islands import (
    IslandAnalysisOptions,
    IslandCandidateStatus,
    classify_small_islands,
)
from image23mf.engine.labels import LabelField
from image23mf.engine.palette import classify_palette
from image23mf.engine.regions import analyze_regions
from image23mf.engine.risks import RiskActionKind, RiskCode, RiskMeasurementRole

PUBLIC_FIXTURES = Path(__file__).parent / "fixtures" / "public-regression"


def _analysis(
    rows: list[list[int]],
    *,
    width_mm: float | None = None,
    height_mm: float | None = None,
    active: bytes | None = None,
):
    array = np.asarray(rows, dtype=np.uint8)
    labels = tuple(sorted(set(array.ravel()) | {0, 1}))
    field = LabelField(
        width=array.shape[1],
        height=array.shape[0],
        label_values=labels,
        pixels=array.tobytes(),
    )
    return analyze_regions(
        field,
        colors={label: ("#FFFFFF", "#000000")[label] for label in labels},
        width_mm=width_mm or field.width,
        height_mm=height_mm or field.height,
        active=active,
    )


def _options(**changes) -> ClearanceAnalysisOptions:
    values = {
        "nozzle_mm": 2.0,
        "minimum_width_mm": 2.0,
        "minimum_line_length_mm": 6.0,
        "minimum_line_aspect_ratio": 4.0,
        "wide_support_ratio": 1.5,
        "error_ratio": 0.5,
    }
    values.update(changes)
    return ClearanceAnalysisOptions(**values)


def test_squared_distance_transform_is_exact_and_anisotropic() -> None:
    values = np.asarray([0, np.inf, np.inf, 0], dtype=np.float64)
    assert _distance_transform_1d(values, 2).tolist() == [0, 4, 4, 0]
    assert _distance_transform_1d(values, 0.5).tolist() == [0, 0.25, 0.25, 0]


def test_long_thin_line_is_distinct_from_compact_dust_and_has_protection_action() -> None:
    rows = np.zeros((5, 12), dtype=np.uint8)
    rows[2, 2:10] = 1
    rows[0, 0] = 1
    analysis = _analysis(rows.tolist(), width_mm=1.2, height_mm=0.5)
    classification = analyze_clearance_features(
        analysis,
        options=_options(
            nozzle_mm=0.2,
            minimum_width_mm=0.2,
            minimum_line_length_mm=0.6,
            minimum_line_aspect_ratio=6,
        ),
    )
    lines = [
        feature
        for feature in classification.features
        if feature.kind == ClearanceFeatureKind.THIN_LINE and feature.labels == (1,)
    ]

    assert len(lines) == 1
    assert lines[0].minimum_width_mm == pytest.approx(0.1)
    assert lines[0].length_mm > 0.8
    assert all(
        feature.pixel_bounds.model_dump() != {"x": 0, "y": 0, "width": 1, "height": 1}
        for feature in lines
    )
    finding = next(
        item
        for item in clearance_risk_findings(classification, analysis)
        if item.code == RiskCode.THIN_LINE and item.affected_labels == (1,)
    )
    assert {suggestion.kind for suggestion in finding.suggestions} == {
        RiskActionKind.REVIEW,
        RiskActionKind.PRESERVE_LINE,
        RiskActionKind.WIDEN,
    }


def test_exact_thin_line_evidence_and_island_safeguard_agree_on_protection() -> None:
    rows = np.zeros((5, 12), dtype=np.uint8)
    rows[2, 2:10] = 1
    analysis = _analysis(rows.tolist(), width_mm=1.2, height_mm=0.5)
    islands = classify_small_islands(
        analysis,
        options=IslandAnalysisOptions(
            minimum_area_mm2=0.1,
            minimum_equivalent_diameter_mm=0.4,
            preserve_long_lines=True,
            long_line_minimum_aspect_ratio=6,
            long_line_minimum_length_mm=0.4,
        ),
    )
    clearance = analyze_clearance_features(
        analysis,
        options=ClearanceAnalysisOptions(
            nozzle_mm=0.2,
            minimum_line_length_mm=0.6,
            minimum_line_aspect_ratio=6,
        ),
    )
    line_region = next(region for region in analysis.graph.regions if region.label == 1)
    island = next(item for item in islands.candidates if item.region_id == line_region.id)
    line = next(
        item
        for item in clearance.features
        if item.kind == ClearanceFeatureKind.THIN_LINE and item.region_ids == (line_region.id,)
    )

    assert island.status == IslandCandidateStatus.LONG_LINE_EXEMPT
    assert line.minimum_width_mm == pytest.approx(0.1)


def test_width_equal_to_threshold_is_not_a_thin_line() -> None:
    rows = np.zeros((6, 12), dtype=np.uint8)
    rows[2:4, 2:10] = 1
    analysis = _analysis(rows.tolist(), width_mm=1.2, height_mm=0.6)
    classification = analyze_clearance_features(
        analysis,
        options=_options(
            nozzle_mm=0.2,
            minimum_width_mm=0.2,
            minimum_line_length_mm=0.6,
            minimum_line_aspect_ratio=4,
        ),
    )

    assert not any(
        feature.kind == ClearanceFeatureKind.THIN_LINE and feature.labels == (1,)
        for feature in classification.features
    )


def test_dumbbell_has_one_local_neck_with_two_wide_supported_sides() -> None:
    rows = np.zeros((7, 15), dtype=np.uint8)
    rows[1:6, 1:6] = 1
    rows[1:6, 9:14] = 1
    rows[3, 6:9] = 1
    analysis = _analysis(rows.tolist())
    classification = analyze_clearance_features(analysis, options=_options())
    necks = [
        feature
        for feature in classification.features
        if feature.kind == ClearanceFeatureKind.NARROW_NECK and feature.labels == (1,)
    ]

    assert len(necks) == 1
    assert necks[0].pixel_bounds.model_dump() == {"x": 6, "y": 3, "width": 3, "height": 1}
    assert necks[0].minimum_width_mm == 1
    finding = next(
        item
        for item in clearance_risk_findings(classification, analysis)
        if item.feature_key == necks[0].id
    )
    assert finding.code == RiskCode.NARROW_NECK
    assert {suggestion.kind for suggestion in finding.suggestions} == {
        RiskActionKind.REVIEW,
        RiskActionKind.KEEP,
        RiskActionKind.WIDEN,
    }


def test_thin_branch_with_only_one_wide_side_is_not_mislabeled_as_a_neck() -> None:
    rows = np.zeros((9, 13), dtype=np.uint8)
    rows[2:7, 2:7] = 1
    rows[4, 7:12] = 1
    analysis = _analysis(rows.tolist())
    classification = analyze_clearance_features(analysis, options=_options())

    assert not any(
        feature.kind == ClearanceFeatureKind.NARROW_NECK and feature.labels == (1,)
        for feature in classification.features
    )


def test_separate_same_label_regions_form_exact_physical_gap() -> None:
    analysis = _analysis([[1, 0, 1]], width_mm=0.3, height_mm=0.1)
    classification = analyze_clearance_features(
        analysis,
        options=_options(
            nozzle_mm=0.2,
            minimum_width_mm=0.2,
            minimum_line_length_mm=0.1,
        ),
    )
    gaps = [
        feature
        for feature in classification.features
        if feature.kind == ClearanceFeatureKind.NARROW_GAP
    ]

    assert len(gaps) == 1
    assert len(gaps[0].region_ids) == 2
    assert gaps[0].labels == (1,)
    assert gaps[0].minimum_width_mm == pytest.approx(0.1)
    finding = next(
        item
        for item in clearance_risk_findings(classification, analysis)
        if item.feature_key == gaps[0].id
    )
    assert finding.code == RiskCode.NARROW_GAP
    assert {suggestion.kind for suggestion in finding.suggestions} == {
        RiskActionKind.REVIEW,
        RiskActionKind.KEEP,
        RiskActionKind.CLOSE_GAP,
    }


def test_line_neck_and_gap_use_independent_profile_thresholds() -> None:
    line_rows = np.zeros((5, 12), dtype=np.uint8)
    line_rows[2, 2:10] = 1
    line_analysis = _analysis(line_rows.tolist(), width_mm=1.2, height_mm=0.5)
    line_result = analyze_clearance_features(
        line_analysis,
        options=_options(
            nozzle_mm=0.2,
            minimum_width_mm=0.05,
            minimum_line_width_mm=0.2,
            minimum_neck_width_mm=0.05,
            minimum_gap_width_mm=0.05,
            minimum_line_length_mm=0.6,
            minimum_line_aspect_ratio=6,
        ),
    )

    neck_rows = np.zeros((7, 15), dtype=np.uint8)
    neck_rows[1:6, 1:6] = 1
    neck_rows[1:6, 9:14] = 1
    neck_rows[3, 6:9] = 1
    neck_analysis = _analysis(neck_rows.tolist())
    neck_result = analyze_clearance_features(
        neck_analysis,
        options=_options(
            minimum_width_mm=0.5,
            minimum_line_width_mm=0.5,
            minimum_neck_width_mm=2,
            minimum_gap_width_mm=0.5,
        ),
    )

    gap_analysis = _analysis([[1, 0, 1]], width_mm=0.3, height_mm=0.1)
    gap_result = analyze_clearance_features(
        gap_analysis,
        options=_options(
            nozzle_mm=0.2,
            minimum_width_mm=0.05,
            minimum_line_width_mm=0.05,
            minimum_neck_width_mm=0.05,
            minimum_gap_width_mm=0.2,
            minimum_line_length_mm=0.1,
        ),
    )

    assert line_result.summary.thin_line_count >= 1
    assert neck_result.summary.narrow_neck_count >= 1
    assert gap_result.summary.narrow_gap_count == 1
    cases = (
        (line_result, line_analysis, RiskCode.THIN_LINE, 0.2),
        (neck_result, neck_analysis, RiskCode.NARROW_NECK, 2.0),
        (gap_result, gap_analysis, RiskCode.NARROW_GAP, 0.2),
    )
    for result, analysis, code, expected in cases:
        finding = next(
            item for item in clearance_risk_findings(result, analysis) if item.code == code
        )
        threshold = next(
            item for item in finding.measurements if item.role == RiskMeasurementRole.THRESHOLD
        )
        assert threshold.value == expected


def test_gap_equal_to_threshold_and_corner_touch_are_kept() -> None:
    equal = _analysis([[1, 0, 1]], width_mm=0.6, height_mm=0.2)
    corner = _analysis([[1, 0], [0, 1]], width_mm=0.4, height_mm=0.4)
    equal_result = analyze_clearance_features(
        equal, options=_options(nozzle_mm=0.2, minimum_width_mm=0.2)
    )
    corner_result = analyze_clearance_features(
        corner, options=_options(nozzle_mm=0.2, minimum_width_mm=0.2)
    )

    assert equal_result.summary.narrow_gap_count == 0
    assert corner_result.summary.narrow_gap_count == 0


def test_anisotropic_pixels_use_physical_cross_section_width() -> None:
    rows = np.zeros((5, 10), dtype=np.uint8)
    rows[2, 1:9] = 1
    analysis = _analysis(rows.tolist(), width_mm=5, height_mm=0.5)
    classification = analyze_clearance_features(
        analysis,
        options=_options(
            nozzle_mm=0.2,
            minimum_width_mm=0.2,
            minimum_line_length_mm=3,
            minimum_line_aspect_ratio=6,
        ),
    )
    line = next(
        feature
        for feature in classification.features
        if feature.kind == ClearanceFeatureKind.THIN_LINE and feature.labels == (1,)
    )

    assert line.minimum_width_mm == pytest.approx(0.1)
    assert line.bounds.width_mm == 4


def test_transparent_pixels_do_not_join_regions_or_hide_gap_evidence() -> None:
    active = bytes((1, 0, 1))
    analysis = _analysis([[1, 1, 1]], width_mm=0.3, height_mm=0.1, active=active)
    classification = analyze_clearance_features(
        analysis,
        options=_options(nozzle_mm=0.2, minimum_width_mm=0.2, minimum_line_length_mm=0.1),
    )

    gap = next(
        feature
        for feature in classification.features
        if feature.kind == ClearanceFeatureKind.NARROW_GAP
    )
    assert gap.labels == (1,)
    assert gap.minimum_width_mm == pytest.approx(0.1)


def test_high_resolution_gap_search_uses_bounded_voronoi_path() -> None:
    rows = np.zeros((300, 300), dtype=np.uint8)
    rows[:, 100] = 1
    rows[:, 102] = 1
    analysis = _analysis(rows.tolist(), width_mm=20, height_mm=20)
    classification = analyze_clearance_features(
        analysis,
        options=ClearanceAnalysisOptions(nozzle_mm=0.4),
    )
    dark_gaps = [
        feature
        for feature in classification.features
        if feature.kind == ClearanceFeatureKind.NARROW_GAP and feature.labels == (1,)
    ]

    assert len(dark_gaps) == 1
    assert dark_gaps[0].minimum_width_mm == pytest.approx(20 / 300)
    assert dark_gaps[0].length_mm == 20


def test_analysis_is_deterministic_reconstructable_and_rejects_tampering() -> None:
    rows = np.zeros((5, 12), dtype=np.uint8)
    rows[2, 2:10] = 1
    analysis = _analysis(rows.tolist(), width_mm=1.2, height_mm=0.5)
    options = _options(
        nozzle_mm=0.2,
        minimum_width_mm=0.2,
        minimum_line_length_mm=0.6,
        minimum_line_aspect_ratio=6,
    )
    first = analyze_clearance_features(analysis, options=options)
    second = analyze_clearance_features(analysis, options=options)

    assert first == second
    assert first.canonical_json() == second.canonical_json()
    assert len(first.fingerprint()) == 64
    validate_clearance_analysis(first, analysis)
    payload = first.model_dump(mode="json")
    payload["summary"]["feature_count"] += 1
    with pytest.raises(ValidationError, match="summary"):
        ClearanceAnalysis.model_validate(payload)


def test_legacy_shared_width_analysis_keeps_its_original_fingerprint() -> None:
    analysis = _analysis([[1, 0, 1]], width_mm=0.3, height_mm=0.1)
    original = analyze_clearance_features(
        analysis,
        options=_options(
            nozzle_mm=0.2,
            minimum_width_mm=0.2,
            minimum_line_length_mm=0.1,
        ),
    )
    payload = original.model_dump(mode="json")
    payload["options"].pop("minimum_line_width_mm")
    payload["options"].pop("minimum_neck_width_mm")
    payload["options"].pop("minimum_gap_width_mm")

    restored = ClearanceAnalysis.model_validate(payload)

    assert restored == original
    assert restored.options_fingerprint == original.options_fingerprint
    validate_clearance_analysis(restored, analysis)


def test_synthetic_thin_line_crop_retains_long_line_evidence_without_mutating_labels() -> None:
    with Image.open(PUBLIC_FIXTURES / "geometry-stress.png") as source:
        crop = source.convert("RGBA").crop((180, 300, 1500, 680))
    quantized = classify_palette(crop, ("#F2D6AA", "#E75B12", "#0A3A78", "#202428"))
    original_labels = bytes(quantized.labels.pixels)
    analysis = analyze_regions(
        quantized.labels,
        colors=dict(enumerate(quantized.palette)),
        width_mm=1320 * 600 / 2520,
        height_mm=380 * 400 / 1680,
        active=quantized.alpha,
    )
    classification = analyze_clearance_features(
        analysis,
        options=ClearanceAnalysisOptions(nozzle_mm=0.4),
    )
    dark_lines = [
        feature
        for feature in classification.features
        if feature.kind == ClearanceFeatureKind.THIN_LINE and feature.labels == (3,)
    ]

    assert dark_lines
    assert all(feature.minimum_width_mm < 0.4 for feature in dark_lines)
    assert all(feature.length_mm >= 1.6 for feature in dark_lines)
    assert quantized.labels.pixels == original_labels


def test_invalid_options_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ClearanceAnalysisOptions(nozzle_mm=0).contract()
    with pytest.raises(ValidationError):
        ClearanceAnalysisOptions(nozzle_mm=0.4, wide_support_ratio=1).contract()


def test_parallel_clearance_preserves_exact_anisotropic_features_and_cancellation():
    rng = np.random.default_rng(41)
    pixels = rng.integers(0, 2, size=(32, 33), dtype=np.uint8)
    analysis = _analysis(pixels.tolist(), width_mm=9.9, height_mm=6.4)
    options = _options(nozzle_mm=0.4, minimum_width_mm=0.6)
    serial = analyze_clearance_features(analysis, options=options)
    parallel = analyze_clearance_features(analysis, options=options, max_workers=2)
    assert serial.canonical_json() == parallel.canonical_json()

    def canceled():
        raise RuntimeError("preview canceled")

    with pytest.raises(RuntimeError, match="preview canceled"):
        analyze_clearance_features(
            analysis, options=options, max_workers=2, check_canceled=canceled
        )


def test_clearance_worker_budget_respects_core_count_and_small_artwork(monkeypatch):
    from image23mf.engine.clearance import clearance_worker_count

    small = _analysis([[0, 1], [1, 0]])
    fragmented = _analysis((np.indices((100, 100)).sum(axis=0) % 2).tolist())
    monkeypatch.setattr("image23mf.engine.clearance.os.cpu_count", lambda: 18)
    assert clearance_worker_count(small) == 1
    assert clearance_worker_count(fragmented) == 8
    monkeypatch.setattr("image23mf.engine.clearance.os.cpu_count", lambda: 4)
    assert clearance_worker_count(fragmented) == 2
    monkeypatch.setattr("image23mf.engine.clearance.os.cpu_count", lambda: 1)
    assert clearance_worker_count(fragmented) == 1
