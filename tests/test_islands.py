from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from image23mf.engine import (
    IslandAnalysisOptions,
    IslandCandidateStatus,
    IslandDecisionStatus,
    IslandMergePolicy,
    IslandMergeRequest,
    LabelField,
    RiskActionKind,
    RiskCode,
    analyze_regions,
    apply_island_policy,
    classify_palette,
    classify_small_islands,
    island_risk_findings,
)

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def _analyze(
    rows: list[list[int]],
    *,
    colors: dict[int, str] | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
    active: bytes | None = None,
):
    array = np.asarray(rows, dtype=np.uint8)
    labels = tuple(sorted(set(array.ravel()) | set((colors or {}).keys())))
    field = LabelField(
        width=array.shape[1],
        height=array.shape[0],
        label_values=labels,
        pixels=array.tobytes(),
    )
    palette = colors or {
        label: ("#000000", "#777777", "#FFFFFF", "#FF0000")[label] for label in labels
    }
    analysis = analyze_regions(
        field,
        colors=palette,
        width_mm=width_mm or field.width,
        height_mm=height_mm or field.height,
        active=active,
    )
    return field, analysis


def _options(**changes) -> IslandAnalysisOptions:
    values = {
        "minimum_area_mm2": 2.0,
        "minimum_equivalent_diameter_mm": 1.5,
        "preserve_long_lines": True,
        "long_line_minimum_aspect_ratio": 6.0,
        "long_line_minimum_length_mm": 4.0,
    }
    values.update(changes)
    return IslandAnalysisOptions(**values)


def _center_fixture():
    return _analyze(
        [
            [0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0],
            [0, 0, 1, 0, 0],
            [0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0],
        ]
    )


def test_classification_uses_physical_area_and_diameter_with_traceable_neighbors() -> None:
    _field, analysis = _center_fixture()
    classification = classify_small_islands(analysis, options=_options())

    assert classification.summary.candidate_count == 1
    assert classification.summary.risk_count == 1
    assert classification.summary.long_line_exempt_count == 0
    candidate = classification.candidates[0]
    assert candidate.label == 1
    assert candidate.area_mm2 == 1
    assert candidate.equivalent_diameter_mm == pytest.approx(2 / np.sqrt(np.pi))
    assert [trigger.value for trigger in candidate.triggers] == [
        "area",
        "equivalent_diameter",
    ]
    assert not candidate.touches_canvas_border
    assert len(candidate.neighbors) == 1
    assert candidate.neighbors[0].label == 0
    finding = island_risk_findings(classification, analysis.graph)[0]
    assert finding.code == RiskCode.SMALL_ISLAND
    assert finding.affected_region_ids == (candidate.region_id,)
    assert {suggestion.kind for suggestion in finding.suggestions} == {
        RiskActionKind.REVIEW,
        RiskActionKind.KEEP,
        RiskActionKind.MERGE_DOMINANT_NEIGHBOR,
        RiskActionKind.MERGE_PERCEPTUAL_NEIGHBOR,
        RiskActionKind.MERGE_EXPLICIT_COLOR,
    }
    assert len(classification.fingerprint()) == 64


def test_diameter_threshold_can_classify_when_area_threshold_is_disabled() -> None:
    _field, analysis = _center_fixture()
    classification = classify_small_islands(
        analysis,
        options=_options(minimum_area_mm2=0, minimum_equivalent_diameter_mm=1.2),
    )

    assert classification.summary.risk_count == 1
    assert [trigger.value for trigger in classification.candidates[0].triggers] == [
        "equivalent_diameter"
    ]


def test_long_thin_candidate_is_exempt_unless_preservation_is_disabled() -> None:
    _field, analysis = _analyze(
        [
            [0, 0, 0, 0, 0, 0, 0, 0],
            [0, 1, 1, 1, 1, 1, 1, 0],
            [0, 0, 0, 0, 0, 0, 0, 0],
        ]
    )
    preserved = classify_small_islands(
        analysis,
        options=_options(
            minimum_area_mm2=7,
            minimum_equivalent_diameter_mm=3,
            preserve_long_lines=True,
        ),
    )
    unprotected = classify_small_islands(
        analysis,
        options=_options(
            minimum_area_mm2=7,
            minimum_equivalent_diameter_mm=3,
            preserve_long_lines=False,
        ),
    )
    line = next(candidate for candidate in preserved.candidates if candidate.label == 1)

    assert line.bbox_aspect_ratio == 6
    assert line.status == IslandCandidateStatus.LONG_LINE_EXEMPT
    assert preserved.summary.long_line_exempt_count == 1
    assert island_risk_findings(preserved, analysis.graph) == ()
    assert unprotected.summary.risk_count == 1
    assert island_risk_findings(unprotected, analysis.graph)[0].affected_region_ids == (
        line.region_id,
    )


def test_dominant_tie_and_perceptual_neighbor_are_deterministic() -> None:
    colors = {0: "#000000", 1: "#EEEEEE", 2: "#FFFFFF"}
    field, analysis = _analyze(
        [[0, 0, 2], [0, 1, 2], [0, 2, 2]],
        colors=colors,
    )
    classification = classify_small_islands(analysis, options=_options())
    island = next(candidate for candidate in classification.candidates if candidate.label == 1)

    dominant_request = IslandMergeRequest(
        policy=IslandMergePolicy.DOMINANT_NEIGHBOR,
        region_ids=(island.region_id,),
    )
    dominant = apply_island_policy(
        field,
        analysis,
        classification,
        dominant_request,
    )
    perceptual = apply_island_policy(
        field,
        analysis,
        classification,
        IslandMergeRequest(
            policy=IslandMergePolicy.PERCEPTUAL_NEIGHBOR,
            region_ids=(island.region_id,),
        ),
    )

    assert dominant.record.decisions[0].target_label == 0
    assert perceptual.record.decisions[0].target_label == 2
    assert dominant.record.changed_pixel_count == 1
    assert perceptual.record.changed_pixel_count == 1
    assert dominant.labels.pixels != field.pixels
    assert any(event.kind == "merged" for event in dominant.record.lineage.events)
    repeat = apply_island_policy(
        field,
        analysis,
        classification,
        dominant_request,
    )
    assert repeat.record == dominant.record
    assert repeat.labels == dominant.labels


def test_explicit_review_keep_border_and_transparent_no_neighbor_modes() -> None:
    border_field, border_analysis = _analyze([[1, 0], [0, 0]])
    border_classification = classify_small_islands(border_analysis, options=_options())
    border = next(
        candidate for candidate in border_classification.candidates if candidate.label == 1
    )
    assert border.touches_canvas_border

    review = apply_island_policy(
        border_field,
        border_analysis,
        border_classification,
        IslandMergeRequest(policy=IslandMergePolicy.REVIEW, region_ids=(border.region_id,)),
    )
    keep = apply_island_policy(
        border_field,
        border_analysis,
        border_classification,
        IslandMergeRequest(policy=IslandMergePolicy.KEEP, region_ids=(border.region_id,)),
    )
    explicit = apply_island_policy(
        border_field,
        border_analysis,
        border_classification,
        IslandMergeRequest(
            policy=IslandMergePolicy.EXPLICIT_COLOR,
            region_ids=(border.region_id,),
            explicit_target_label=0,
        ),
    )
    assert review.record.decisions[0].status == IslandDecisionStatus.REVIEW_REQUIRED
    assert keep.record.decisions[0].status == IslandDecisionStatus.KEPT
    assert review.record.changed_pixel_count == keep.record.changed_pixel_count == 0
    assert review.labels == keep.labels == border_field
    assert explicit.record.decisions[0].status == IslandDecisionStatus.MERGED
    assert explicit.record.changed_pixel_count == 1

    hidden_field, hidden_analysis = _analyze(
        [[0, 0, 0], [0, 1, 0], [0, 0, 0]],
        colors={0: "#000000", 1: "#FFFFFF"},
        active=bytes((0, 0, 0, 0, 1, 0, 0, 0, 0)),
    )
    hidden_classification = classify_small_islands(hidden_analysis, options=_options())
    isolated = hidden_classification.candidates[0]
    no_neighbor = apply_island_policy(
        hidden_field,
        hidden_analysis,
        hidden_classification,
        IslandMergeRequest(
            policy=IslandMergePolicy.DOMINANT_NEIGHBOR,
            region_ids=(isolated.region_id,),
        ),
    )
    assert isolated.neighbors == ()
    assert no_neighbor.record.decisions[0].status == IslandDecisionStatus.NO_ELIGIBLE_NEIGHBOR
    assert no_neighbor.labels == hidden_field


def test_selected_adjacent_dust_cannot_target_each_other_or_cascade() -> None:
    field, analysis = _analyze([[1, 2]], colors={1: "#777777", 2: "#FFFFFF"})
    classification = classify_small_islands(analysis, options=_options())
    region_ids = tuple(sorted(candidate.region_id for candidate in classification.candidates))
    result = apply_island_policy(
        field,
        analysis,
        classification,
        IslandMergeRequest(
            policy=IslandMergePolicy.DOMINANT_NEIGHBOR,
            region_ids=region_ids,
        ),
    )

    assert result.record.changed_pixel_count == 0
    assert result.labels == field
    assert {decision.status for decision in result.record.decisions} == {
        IslandDecisionStatus.NO_ELIGIBLE_NEIGHBOR
    }


def test_invalid_requests_are_atomic_and_long_line_exempt_regions_cannot_merge() -> None:
    field, analysis = _center_fixture()
    classification = classify_small_islands(analysis, options=_options())
    island = classification.candidates[0]
    with pytest.raises(ValidationError, match="target label"):
        IslandMergeRequest(
            policy=IslandMergePolicy.EXPLICIT_COLOR,
            region_ids=(island.region_id,),
        )
    with pytest.raises(ValueError, match="differ"):
        apply_island_policy(
            field,
            analysis,
            classification,
            IslandMergeRequest(
                policy=IslandMergePolicy.EXPLICIT_COLOR,
                region_ids=(island.region_id,),
                explicit_target_label=island.label,
            ),
        )
    with pytest.raises(ValueError, match="unclassified"):
        apply_island_policy(
            field,
            analysis,
            classification,
            IslandMergeRequest(
                policy=IslandMergePolicy.KEEP,
                region_ids=("region_000000000000000000000000",),
            ),
        )

    line_field, line_analysis = _analyze([[0, 1, 1, 1, 1, 1, 1, 0]])
    line_classification = classify_small_islands(
        line_analysis,
        options=_options(minimum_area_mm2=7, minimum_equivalent_diameter_mm=3),
    )
    line = next(
        candidate
        for candidate in line_classification.candidates
        if candidate.status == IslandCandidateStatus.LONG_LINE_EXEMPT
    )
    with pytest.raises(ValueError, match="long-line-exempt"):
        apply_island_policy(
            line_field,
            line_analysis,
            line_classification,
            IslandMergeRequest(policy=IslandMergePolicy.KEEP, region_ids=(line.region_id,)),
        )


def test_synthetic_geometry_fixture_classifies_without_modifying_source_labels() -> None:
    with Image.open(FIXTURES / "geometry-nozzle-040.png") as image:
        quantized = classify_palette(image.convert("RGBA"), ("#000000", "#FFFFFF"))
    analysis = analyze_regions(
        quantized.labels,
        colors=dict(enumerate(quantized.palette)),
        width_mm=51.2,
        height_mm=38.4,
        active=quantized.alpha,
    )
    classification = classify_small_islands(
        analysis,
        options=IslandAnalysisOptions(
            minimum_area_mm2=0.3,
            minimum_equivalent_diameter_mm=0.4,
            preserve_long_lines=True,
        ),
    )

    assert classification.summary.candidate_count > 0
    findings = island_risk_findings(classification, analysis.graph)
    assert classification.summary.risk_count == len(findings)
    assert all(finding.code == RiskCode.SMALL_ISLAND for finding in findings)
    region_ids = {region.id for region in analysis.graph.regions}
    assert all(finding.affected_region_ids[0] in region_ids for finding in findings)
    assert quantized.labels.pixels == bytes(quantized.labels.pixels)


def test_color_distance_work_scales_with_palette_pairs_not_boundary_count(monkeypatch):
    import image23mf.engine.islands as islands

    _field, analysis = _analyze((np.indices((20, 20)).sum(axis=0) % 2).tolist())
    original = islands.rgb_to_lab
    calls = []

    def counted(rgb):
        calls.append(rgb)
        return original(rgb)

    monkeypatch.setattr(islands, "rgb_to_lab", counted)
    result = classify_small_islands(analysis, options=_options())
    assert result.summary.candidate_count == 400
    assert len(calls) == 4  # Two directed pairs; each converts both colors once.
    expected = islands.delta_e_76(original((0, 0, 0)), original((119, 119, 119)))
    assert all(n.delta_e == expected for r in result.candidates for n in r.neighbors)
