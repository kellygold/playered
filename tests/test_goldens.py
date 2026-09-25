import json
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image, ImageDraw

from image23mf.quality.goldens import (
    GoldenLabel,
    GoldenTolerance,
    InvalidGoldenError,
    LabelResult,
    MissingGoldenError,
    StaleGoldenReviewError,
    StatTolerance,
    accept_golden_update,
    compare_golden,
    load_golden_payload,
    propose_golden_update,
    write_golden_payload,
)

PALETTE = (
    GoldenLabel(value=0, name="Cream", hex="#F2D6AA"),
    GoldenLabel(value=1, name="Orange", hex="#E75B12"),
    GoldenLabel(value=2, name="Blue", hex="#0A3A78"),
    GoldenLabel(value=3, name="Charcoal", hex="#202428"),
)
COMMITTED_SPECIMEN = Path(__file__).parent / "goldens" / "workflow-specimen"


def result(
    *, changed: Optional[tuple[int, int, int]] = None, edge_length: float = 12
) -> LabelResult:
    labels = Image.new("L", (16, 12), 0)
    draw = ImageDraw.Draw(labels)
    draw.rectangle((1, 1, 5, 5), fill=1)
    draw.ellipse((7, 1, 13, 7), fill=2)
    draw.line((0, 10, 15, 10), fill=3, width=1)
    if changed:
        labels.putpixel((changed[0], changed[1]), changed[2])
    return LabelResult(
        case_id="workflow-specimen",
        labels=labels,
        palette=PALETTE,
        stats={"edge.length_mm": edge_length, "regions.count": 3},
    )


def tolerance() -> GoldenTolerance:
    return GoldenTolerance(
        max_changed_pixels=1,
        max_changed_ratio=1 / (16 * 12),
        stats={"edge.length_mm": StatTolerance(absolute=0.2, relative=0.01)},
    )


def tree_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_payload_versions_exhaustive_masks_counts_stats_and_tolerance(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    manifest = write_golden_payload(baseline, result(), tolerance=tolerance())

    loaded_manifest, loaded = load_golden_payload(baseline)

    assert loaded_manifest == manifest
    assert loaded.labels.tobytes() == result().labels.tobytes()
    assert loaded.stats == result().stats
    assert set(manifest.mask_files) == {"0", "1", "2", "3"}
    stats = json.loads((baseline / "stats.json").read_text(encoding="utf-8"))
    assert sum(stats[f"label.{value}.pixels"] for value in range(4)) == stats["pixels.total"]


def test_committed_specimen_is_versioned_and_matches_the_documented_actual() -> None:
    manifest, loaded = load_golden_payload(COMMITTED_SPECIMEN)

    assert manifest.tolerance == tolerance()
    assert loaded.labels.tobytes() == result().labels.tobytes()
    assert loaded.stats == result().stats
    assert compare_golden(COMMITTED_SPECIMEN, result()).passed


def test_comparison_is_read_only_and_enforces_pixel_and_stat_tolerances(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    write_golden_payload(baseline, result(), tolerance=tolerance())
    before = tree_bytes(baseline)

    exact = compare_golden(baseline, result())
    tolerated = compare_golden(baseline, result(changed=(0, 0, 3), edge_length=12.15))
    too_many_pixels = compare_golden(baseline, result(changed=(0, 0, 3)))
    too_many_pixels_result = result(changed=(0, 0, 3))
    too_many_pixels_result.labels.putpixel((0, 1), 3)
    failed_pixels = compare_golden(baseline, too_many_pixels_result)
    failed_stat = compare_golden(baseline, result(edge_length=12.5))

    assert exact.passed
    assert tolerated.passed
    assert too_many_pixels.passed
    assert not failed_pixels.passed
    assert failed_pixels.changed_pixels == 2
    assert not failed_stat.passed
    assert failed_stat.metric_differences[0].allowed_delta == pytest.approx(0.2)
    assert tree_bytes(baseline) == before


def test_missing_and_corrupt_goldens_fail_with_actionable_errors(tmp_path) -> None:
    with pytest.raises(MissingGoldenError, match="explicit review proposal"):
        compare_golden(tmp_path / "missing", result())

    baseline = tmp_path / "baseline"
    write_golden_payload(baseline, result())
    Image.new("L", (16, 12), 0).save(baseline / "mask-001.png")

    with pytest.raises(InvalidGoldenError, match="mask disagrees"):
        load_golden_payload(baseline)


def test_proposal_emits_review_evidence_without_mutating_baseline_then_accepts(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    review_directory = tmp_path / "review"
    write_golden_payload(baseline, result(), tolerance=tolerance())
    baseline_before = tree_bytes(baseline)
    proposed_result = result(changed=(0, 0, 3), edge_length=12.1)

    review = propose_golden_update(baseline, proposed_result, review_directory)

    assert tree_bytes(baseline) == baseline_before
    assert review.baseline_present
    assert review.comparison is not None
    assert review.comparison.passed
    assert (review_directory / "before" / "manifest.json").is_file()
    assert (review_directory / "after" / "manifest.json").is_file()
    assert (review_directory / "contact-sheet.png").is_file()
    assert review.proposal_sha256 in review.acceptance_command
    with Image.open(review_directory / "contact-sheet.png") as sheet:
        assert sheet.width > proposed_result.labels.width * 3
        assert sheet.height > proposed_result.labels.height

    with pytest.raises(InvalidGoldenError, match="digest"):
        accept_golden_update(baseline, review_directory, proposal_sha256="0" * 64)
    accepted = accept_golden_update(
        baseline,
        review_directory,
        proposal_sha256=review.proposal_sha256,
    )

    assert accepted.case_id == proposed_result.case_id
    assert compare_golden(baseline, proposed_result).passed
    assert (review_directory / "before" / "manifest.json").is_file()


def test_accept_rejects_changed_proposal_contact_sheet_and_stale_baseline(tmp_path) -> None:
    baseline = tmp_path / "baseline"
    write_golden_payload(baseline, result())

    changed_proposal = tmp_path / "changed-proposal"
    proposal_review = propose_golden_update(baseline, result(changed=(0, 0, 3)), changed_proposal)
    (changed_proposal / "after" / "stats.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(InvalidGoldenError, match="proposal bytes changed"):
        accept_golden_update(
            baseline,
            changed_proposal,
            proposal_sha256=proposal_review.proposal_sha256,
        )

    changed_sheet = tmp_path / "changed-sheet"
    sheet_review = propose_golden_update(baseline, result(changed=(0, 0, 3)), changed_sheet)
    (changed_sheet / "contact-sheet.png").write_bytes(b"changed")
    with pytest.raises(InvalidGoldenError, match="contact sheet changed"):
        accept_golden_update(
            baseline,
            changed_sheet,
            proposal_sha256=sheet_review.proposal_sha256,
        )

    stale = tmp_path / "stale"
    stale_review = propose_golden_update(baseline, result(changed=(0, 0, 3)), stale)
    (baseline / "stats.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(StaleGoldenReviewError, match="baseline changed"):
        accept_golden_update(baseline, stale, proposal_sha256=stale_review.proposal_sha256)


def test_new_case_proposal_has_explicit_no_baseline_contact_sheet(tmp_path) -> None:
    baseline = tmp_path / "new-baseline"
    review_directory = tmp_path / "review"

    review = propose_golden_update(baseline, result(), review_directory, tolerance=tolerance())

    assert not review.baseline_present
    assert review.baseline_sha256 is None
    assert review.comparison is None
    accepted = accept_golden_update(
        baseline,
        review_directory,
        proposal_sha256=review.proposal_sha256,
    )
    assert accepted.tolerance == tolerance()
