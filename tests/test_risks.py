from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from image23mf.engine import (
    LabelField,
    RiskActionKind,
    RiskAnalysisOptions,
    RiskCode,
    RiskFinding,
    RiskMeasurement,
    RiskMeasurementKey,
    RiskMeasurementRole,
    RiskReport,
    RiskSeverity,
    RiskSuggestion,
    RiskUnit,
    analyze_printability_risks,
    analyze_regions,
    build_risk_report,
    classify_palette,
    default_risk_suggestions,
    validate_risk_report_against_graph,
)
from image23mf.processing import _risk_severity_mask

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def _analysis(rows: list[list[int]], labels: tuple[int, ...] = (0, 1, 2)):
    array = np.asarray(rows, dtype=np.uint8)
    field = LabelField(
        width=array.shape[1],
        height=array.shape[0],
        label_values=labels,
        pixels=array.tobytes(),
    )
    return analyze_regions(
        field,
        colors={label: ("#000000", "#777777", "#FFFFFF")[label] for label in labels},
        width_mm=field.width,
        height_mm=field.height,
    )


def _measurement() -> RiskMeasurement:
    return RiskMeasurement(
        key=RiskMeasurementKey.AREA,
        role=RiskMeasurementRole.MEASURED,
        value=0.1,
        unit=RiskUnit.MM2,
    )


def _finding(code: RiskCode, region_id: str, label: int = 0) -> RiskFinding:
    return RiskFinding(
        code=code,
        feature_key=f"fixture-{code.value}",
        severity=RiskSeverity.WARNING,
        title=f"Fixture {code.value}",
        explanation=f"Measured fixture evidence for {code.value}.",
        affected_region_ids=(region_id,),
        affected_labels=(label,),
        measurements=(_measurement(),),
        suggestions=default_risk_suggestions(code),
        classifier_id=f"fixture-{code.value}",
        classifier_version="1",
    )


def test_risk_mask_unions_label_and_region_severities_without_counting_bounds_as_exact() -> None:
    analysis = _analysis([[0, 1, 0, 2]])
    regions = {region.pixel_bounds.x: region for region in analysis.graph.regions}
    base = _finding(RiskCode.SMALL_ISLAND, regions[0].id)
    findings = (
        base.model_copy(
            update={
                "feature_key": "specific-info",
                "severity": RiskSeverity.INFO,
                "affected_labels": (),
            }
        ),
        base.model_copy(
            update={
                "feature_key": "label-warning",
                "affected_region_ids": (),
            }
        ),
        base.model_copy(
            update={
                "feature_key": "mixed-error",
                "severity": RiskSeverity.ERROR,
                "affected_labels": (1,),
            }
        ),
        base.model_copy(
            update={
                "feature_key": "bounds-only",
                "affected_region_ids": (),
                "affected_labels": (),
                "affected_bounds": (regions[3].physical_bounds,),
            }
        ),
    )
    report = build_risk_report(
        analysis.graph,
        findings,
        options=RiskAnalysisOptions(nozzle_mm=0.4),
        evaluated_codes=(RiskCode.SMALL_ISLAND,),
    )
    mask, exact, approximate = _risk_severity_mask(analysis.assignment, analysis.graph, report)
    assert mask == bytes([7, 4, 2, 0])
    assert (exact, approximate) == (3, 1)


def test_graph_level_fixture_classifies_fragmentation_and_absent_color_honestly() -> None:
    graph = _analysis([[1, 0, 1, 0, 1]]).graph
    options = RiskAnalysisOptions(
        nozzle_mm=0.4,
        max_components_per_label=2,
        max_components_per_100_mm2=100,
    )

    first = analyze_printability_risks(graph, options=options)
    second = analyze_printability_risks(graph, options=options)

    assert first == second
    assert first.canonical_json() == second.canonical_json()
    assert len(first.fingerprint()) == 64
    assert first.evaluated_codes == (
        RiskCode.EXCESS_FRAGMENTATION,
        RiskCode.COLOR_ABSENT,
    )
    assert first.pending_codes == tuple(
        code for code in RiskCode if code not in first.evaluated_codes
    )
    assert not first.analysis_complete
    assert [warning.code for warning in first.warnings] == [
        RiskCode.EXCESS_FRAGMENTATION,
        RiskCode.COLOR_ABSENT,
    ]
    fragmented, absent = first.warnings
    assert fragmented.affected_labels == (1,)
    assert len(fragmented.affected_region_ids) == 3
    assert {suggestion.kind for suggestion in fragmented.suggestions} == {
        RiskActionKind.REVIEW,
        RiskActionKind.KEEP,
        RiskActionKind.MERGE_DOMINANT_NEIGHBOR,
        RiskActionKind.MERGE_PERCEPTUAL_NEIGHBOR,
        RiskActionKind.MERGE_EXPLICIT_COLOR,
        RiskActionKind.SMOOTH,
    }
    assert absent.affected_labels == (2,)
    assert absent.severity == RiskSeverity.INFO
    assert first.summary.total == 2
    assert first.summary.warning == 1
    assert first.summary.info == 1
    assert first.summary.error == 0


def test_all_taxonomy_codes_accept_measured_findings_and_only_valid_actions() -> None:
    graph = _analysis([[0, 1]], labels=(0, 1)).graph
    region = next(item for item in graph.regions if item.label == 0)
    findings = tuple(_finding(code, region.id) for code in RiskCode)
    options = RiskAnalysisOptions(nozzle_mm=0.4)

    report = build_risk_report(
        graph,
        findings,
        options=options,
        evaluated_codes=tuple(RiskCode),
    )

    assert report.analysis_complete
    assert report.pending_codes == ()
    assert {warning.code for warning in report.warnings} == set(RiskCode)
    assert report.summary.total == len(RiskCode)
    assert all(warning.id.startswith("risk_") for warning in report.warnings)
    assert len({warning.id for warning in report.warnings}) == len(RiskCode)
    assert RiskReport.model_validate_json(report.canonical_json()) == report


def test_warning_identity_survives_threshold_and_explanation_changes() -> None:
    graph = _analysis([[1, 0, 1, 0, 1]]).graph
    loose = analyze_printability_risks(
        graph,
        options=RiskAnalysisOptions(
            nozzle_mm=0.4,
            max_components_per_label=2,
            max_components_per_100_mm2=100,
        ),
    )
    strict = analyze_printability_risks(
        graph,
        options=RiskAnalysisOptions(
            nozzle_mm=0.4,
            max_components_per_label=1,
            max_components_per_100_mm2=100,
        ),
    )
    loose_warning = next(
        warning for warning in loose.warnings if warning.code == RiskCode.EXCESS_FRAGMENTATION
    )
    strict_warning = next(
        warning for warning in strict.warnings if warning.code == RiskCode.EXCESS_FRAGMENTATION
    )

    assert loose_warning.id == strict_warning.id
    assert loose.options_fingerprint != strict.options_fingerprint
    assert loose_warning.measurements != strict_warning.measurements


def test_contract_rejects_wrong_units_safety_flags_and_code_actions() -> None:
    with pytest.raises(ValidationError, match="area measurements must use mm2"):
        RiskMeasurement(
            key=RiskMeasurementKey.AREA,
            role=RiskMeasurementRole.MEASURED,
            value=1,
            unit=RiskUnit.MM,
        )
    with pytest.raises(ValidationError, match="safety flags"):
        RiskSuggestion(
            kind=RiskActionKind.FILL_HOLE,
            title="Fill",
            explanation="Fill it.",
            destructive=False,
            requires_confirmation=False,
        )
    invalid_action = default_risk_suggestions(RiskCode.SMALL_ISLAND)[2]
    with pytest.raises(ValidationError, match="invalid actions"):
        RiskFinding(
            code=RiskCode.TINY_HOLE,
            feature_key="hole",
            severity=RiskSeverity.WARNING,
            title="Hole",
            explanation="A measured hole.",
            affected_labels=(0,),
            measurements=(_measurement(),),
            suggestions=(invalid_action,),
            classifier_id="fixture",
            classifier_version="1",
        )


def test_report_rejects_unknown_graph_references_duplicates_and_inconsistent_summary() -> None:
    graph = _analysis([[0, 1]], labels=(0, 1)).graph
    region = graph.regions[0]
    options = RiskAnalysisOptions(nozzle_mm=0.4)
    unknown = _finding(RiskCode.SMALL_ISLAND, "region_000000000000000000000000")
    with pytest.raises(ValueError, match="unknown regions"):
        build_risk_report(
            graph,
            (unknown,),
            options=options,
            evaluated_codes=(RiskCode.SMALL_ISLAND,),
        )
    finding = _finding(RiskCode.SMALL_ISLAND, region.id, region.label)
    with pytest.raises(ValueError, match="duplicate risk"):
        build_risk_report(
            graph,
            (finding, finding),
            options=options,
            evaluated_codes=(RiskCode.SMALL_ISLAND,),
        )
    report = build_risk_report(
        graph,
        (finding,),
        options=options,
        evaluated_codes=(RiskCode.SMALL_ISLAND,),
    )
    invalid = report.model_dump(mode="json")
    invalid["summary"]["total"] = 99
    with pytest.raises(ValidationError, match="summary"):
        RiskReport.model_validate(invalid)
    different_graph = _analysis([[0, 0]], labels=(0, 1)).graph
    with pytest.raises(ValueError, match="fingerprint"):
        validate_risk_report_against_graph(report, different_graph)


def test_synthetic_geometry_fixture_produces_traceable_fragmentation_warnings() -> None:
    with Image.open(FIXTURES / "geometry-nozzle-040.png") as image:
        quantized = classify_palette(image.convert("RGBA"), ("#000000", "#FFFFFF"))
    analysis = analyze_regions(
        quantized.labels,
        colors=dict(enumerate(quantized.palette)),
        width_mm=51.2,
        height_mm=38.4,
        active=quantized.alpha,
    )
    report = analyze_printability_risks(
        analysis.graph,
        options=RiskAnalysisOptions(
            nozzle_mm=0.4,
            max_components_per_label=1,
            max_components_per_100_mm2=1000,
        ),
    )

    fragmentation = [
        warning for warning in report.warnings if warning.code == RiskCode.EXCESS_FRAGMENTATION
    ]
    assert fragmentation
    assert all(warning.affected_region_ids for warning in fragmentation)
    assert all(
        region_id in {region.id for region in analysis.graph.regions}
        for warning in fragmentation
        for region_id in warning.affected_region_ids
    )
