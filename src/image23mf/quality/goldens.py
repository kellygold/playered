"""Versioned label/mask/stat goldens with explicit review and acceptance."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

GOLDEN_SCHEMA_VERSION = 1
SAFE_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class GoldenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldenLabel(GoldenModel):
    value: int = Field(ge=0, le=255)
    name: str = Field(min_length=1, max_length=128)
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class StatTolerance(GoldenModel):
    absolute: float = Field(default=0, ge=0)
    relative: float = Field(default=0, ge=0)


class GoldenTolerance(GoldenModel):
    max_changed_pixels: int = Field(default=0, ge=0)
    max_changed_ratio: float = Field(default=0, ge=0, le=1)
    stats: dict[str, StatTolerance] = Field(default_factory=dict)


class GoldenManifest(GoldenModel):
    schema_version: int
    case_id: str
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    labels: tuple[GoldenLabel, ...]
    tolerance: GoldenTolerance
    labels_file: str
    stats_file: str
    mask_files: dict[str, str]


class MetricDifference(GoldenModel):
    name: str
    expected: Optional[float] = None
    actual: Optional[float] = None
    absolute_delta: Optional[float] = None
    allowed_delta: Optional[float] = None
    passed: bool
    explanation: str


class GoldenComparison(GoldenModel):
    case_id: str
    passed: bool
    changed_pixels: int = Field(ge=0)
    changed_ratio: float = Field(ge=0, le=1)
    total_pixels: int = Field(ge=0)
    metric_differences: tuple[MetricDifference, ...]
    problems: tuple[str, ...]


class GoldenReview(GoldenModel):
    schema_version: int
    case_id: str
    baseline_sha256: Optional[str] = None
    proposal_sha256: str
    contact_sheet_sha256: str
    comparison: Optional[GoldenComparison] = None
    baseline_present: bool
    acceptance_command: str


@dataclass(frozen=True)
class LabelResult:
    case_id: str
    labels: Image.Image
    palette: tuple[GoldenLabel, ...]
    stats: dict[str, float]

    def validated(self) -> "LabelResult":
        if not SAFE_CASE_ID.fullmatch(self.case_id):
            raise ValueError("case_id must be a portable lowercase identifier")
        if self.labels.mode != "L":
            raise ValueError("label image must use 8-bit L mode")
        values = [item.value for item in self.palette]
        if not values or len(values) != len(set(values)):
            raise ValueError("palette label values must be present and unique")
        unknown = set(self.labels.getdata()) - set(values)
        if unknown:
            raise ValueError(
                f"label image contains values absent from the palette: {sorted(unknown)}"
            )
        if not all(_is_finite_number(value) for value in self.stats.values()):
            raise ValueError("all golden statistics must be finite numbers")
        return self


class MissingGoldenError(FileNotFoundError):
    pass


class InvalidGoldenError(ValueError):
    pass


class StaleGoldenReviewError(RuntimeError):
    pass


def write_golden_payload(
    destination: Path,
    result: LabelResult,
    *,
    tolerance: Optional[GoldenTolerance] = None,
) -> GoldenManifest:
    """Write a complete self-checking payload; callers choose the destination explicitly."""
    result.validated()
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"golden payload destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    labels_path = destination / "labels.png"
    _save_png(result.labels, labels_path)
    mask_files = {}
    counts = {}
    for label in result.palette:
        mask = result.labels.point(
            lambda value, expected=label.value: 255 if value == expected else 0
        )
        filename = f"mask-{label.value:03d}.png"
        _save_png(mask, destination / filename)
        mask_files[str(label.value)] = filename
        counts[f"label.{label.value}.pixels"] = float(mask.histogram()[255])
    stats = {
        "pixels.total": float(result.labels.width * result.labels.height),
        **counts,
        **result.stats,
    }
    (destination / "stats.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = GoldenManifest(
        schema_version=GOLDEN_SCHEMA_VERSION,
        case_id=result.case_id,
        width_px=result.labels.width,
        height_px=result.labels.height,
        labels=result.palette,
        tolerance=tolerance or GoldenTolerance(),
        labels_file="labels.png",
        stats_file="stats.json",
        mask_files=mask_files,
    )
    (destination / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_golden_payload(directory: Path) -> tuple[GoldenManifest, LabelResult]:
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise MissingGoldenError(
            f"No golden exists at {directory}. Generate an explicit review proposal first."
        )
    try:
        manifest = GoldenManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InvalidGoldenError(f"invalid golden manifest: {error}") from error
    if manifest.schema_version != GOLDEN_SCHEMA_VERSION:
        raise InvalidGoldenError(
            f"golden schema {manifest.schema_version} is unsupported; "
            f"expected {GOLDEN_SCHEMA_VERSION}"
        )
    _validate_payload_names(manifest)
    labels = _load_image(directory / manifest.labels_file, "L")
    if labels.size != (manifest.width_px, manifest.height_px):
        raise InvalidGoldenError("golden label dimensions do not match its manifest")
    try:
        stats_payload = json.loads((directory / manifest.stats_file).read_text(encoding="utf-8"))
        stats = {str(key): float(value) for key, value in stats_payload.items()}
    except (OSError, ValueError, TypeError) as error:
        raise InvalidGoldenError(f"invalid golden statistics: {error}") from error
    result = LabelResult(
        case_id=manifest.case_id,
        labels=labels,
        palette=manifest.labels,
        stats={
            key: value
            for key, value in stats.items()
            if key != "pixels.total" and not key.startswith("label.")
        },
    ).validated()
    _validate_masks_and_stats(directory, manifest, result, stats)
    return manifest, result


def compare_golden(directory: Path, actual: LabelResult) -> GoldenComparison:
    manifest, expected = load_golden_payload(directory)
    actual.validated()
    problems = []
    if actual.case_id != manifest.case_id:
        problems.append(f"case id changed from {manifest.case_id} to {actual.case_id}")
    if actual.labels.size != expected.labels.size:
        problems.append(f"dimensions changed from {expected.labels.size} to {actual.labels.size}")
    if actual.palette != expected.palette:
        problems.append("label palette/value mapping changed")
    if problems:
        return GoldenComparison(
            case_id=actual.case_id,
            passed=False,
            changed_pixels=0,
            changed_ratio=0,
            total_pixels=0,
            metric_differences=(),
            problems=tuple(problems),
        )
    expected_bytes = expected.labels.tobytes()
    actual_bytes = actual.labels.tobytes()
    changed = sum(left != right for left, right in zip(expected_bytes, actual_bytes))
    total = len(expected_bytes)
    ratio = changed / total if total else 0
    pixel_passed = (
        changed <= manifest.tolerance.max_changed_pixels
        and ratio <= manifest.tolerance.max_changed_ratio
    )
    if not pixel_passed:
        problems.append(
            f"label field changed by {changed} pixel(s) ({ratio:.6%}); allowed "
            f"{manifest.tolerance.max_changed_pixels} and "
            f"{manifest.tolerance.max_changed_ratio:.6%}"
        )
    metric_differences = _compare_stats(expected.stats, actual.stats, manifest.tolerance)
    return GoldenComparison(
        case_id=actual.case_id,
        passed=pixel_passed and all(item.passed for item in metric_differences),
        changed_pixels=changed,
        changed_ratio=ratio,
        total_pixels=total,
        metric_differences=metric_differences,
        problems=tuple(problems),
    )


def propose_golden_update(
    baseline_directory: Path,
    actual: LabelResult,
    review_directory: Path,
    *,
    tolerance: Optional[GoldenTolerance] = None,
) -> GoldenReview:
    """Stage a review bundle without mutating the baseline."""
    actual.validated()
    if review_directory.exists() and any(review_directory.iterdir()):
        raise FileExistsError(f"review destination is not empty: {review_directory}")
    review_directory.mkdir(parents=True, exist_ok=True)
    baseline_present = (baseline_directory / "manifest.json").is_file()
    baseline_sha = _tree_sha256(baseline_directory) if baseline_present else None
    before = None
    comparison = None
    if baseline_present:
        _, before = load_golden_payload(baseline_directory)
        comparison = compare_golden(baseline_directory, actual)
        shutil.copytree(baseline_directory, review_directory / "before")
    write_golden_payload(
        review_directory / "after",
        actual,
        tolerance=tolerance
        or (load_golden_payload(baseline_directory)[0].tolerance if baseline_present else None),
    )
    proposal_sha = _tree_sha256(review_directory / "after")
    contact_path = review_directory / "contact-sheet.png"
    _write_contact_sheet(contact_path, before, actual)
    contact_sha = _file_sha256(contact_path)
    review = GoldenReview(
        schema_version=GOLDEN_SCHEMA_VERSION,
        case_id=actual.case_id,
        baseline_sha256=baseline_sha,
        proposal_sha256=proposal_sha,
        contact_sheet_sha256=contact_sha,
        comparison=comparison,
        baseline_present=baseline_present,
        acceptance_command=(
            "python scripts/golden_review.py accept "
            f"{baseline_directory} {review_directory} --sha256 {proposal_sha}"
        ),
    )
    (review_directory / "review.json").write_text(
        json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return review


def accept_golden_update(
    baseline_directory: Path,
    review_directory: Path,
    *,
    proposal_sha256: str,
) -> GoldenManifest:
    """Atomically accept the exact reviewed payload if its baseline is still current."""
    try:
        review = GoldenReview.model_validate_json(
            (review_directory / "review.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise InvalidGoldenError(f"invalid golden review: {error}") from error
    if proposal_sha256 != review.proposal_sha256:
        raise InvalidGoldenError("provided proposal digest does not match the review")
    if _tree_sha256(review_directory / "after") != proposal_sha256:
        raise InvalidGoldenError("reviewed proposal bytes changed after contact-sheet generation")
    if _file_sha256(review_directory / "contact-sheet.png") != review.contact_sheet_sha256:
        raise InvalidGoldenError("review contact sheet changed after proposal generation")
    current_sha = (
        _tree_sha256(baseline_directory)
        if (baseline_directory / "manifest.json").is_file()
        else None
    )
    if current_sha != review.baseline_sha256:
        raise StaleGoldenReviewError(
            "the golden baseline changed after this proposal; generate a fresh review"
        )
    manifest, _ = load_golden_payload(review_directory / "after")
    _atomic_replace_directory(review_directory / "after", baseline_directory)
    return manifest


def _compare_stats(
    expected: dict[str, float], actual: dict[str, float], tolerance: GoldenTolerance
) -> tuple[MetricDifference, ...]:
    results = []
    for name in sorted(set(expected) | set(actual)):
        expected_value = expected.get(name)
        actual_value = actual.get(name)
        if expected_value is None or actual_value is None:
            results.append(
                MetricDifference(
                    name=name,
                    expected=expected_value,
                    actual=actual_value,
                    passed=False,
                    explanation="metric is missing from one result",
                )
            )
            continue
        rule = tolerance.stats.get(name, StatTolerance())
        delta = abs(actual_value - expected_value)
        allowed = max(rule.absolute, abs(expected_value) * rule.relative)
        results.append(
            MetricDifference(
                name=name,
                expected=expected_value,
                actual=actual_value,
                absolute_delta=delta,
                allowed_delta=allowed,
                passed=delta <= allowed,
                explanation=(
                    f"absolute delta {delta:g}; allowed {allowed:g} "
                    f"(abs={rule.absolute:g}, rel={rule.relative:g})"
                ),
            )
        )
    return tuple(results)


def _validate_payload_names(manifest: GoldenManifest) -> None:
    names = [manifest.labels_file, manifest.stats_file, *manifest.mask_files.values()]
    if len(names) != len(set(names)):
        raise InvalidGoldenError("golden payload file names must be unique")
    for name in names:
        path = Path(name)
        if path.is_absolute() or len(path.parts) != 1 or ".." in path.parts:
            raise InvalidGoldenError("golden payload paths must be safe basenames")
    expected_values = {str(item.value) for item in manifest.labels}
    if set(manifest.mask_files) != expected_values:
        raise InvalidGoldenError("golden mask mapping does not cover every label exactly once")


def _validate_masks_and_stats(
    directory: Path,
    manifest: GoldenManifest,
    result: LabelResult,
    stored_stats: dict[str, float],
) -> None:
    total = manifest.width_px * manifest.height_px
    if stored_stats.get("pixels.total") != total:
        raise InvalidGoldenError("golden total-pixel statistic is inconsistent")
    coverage = bytearray(total)
    for label in manifest.labels:
        mask = _load_image(directory / manifest.mask_files[str(label.value)], "L")
        if mask.size != result.labels.size or set(mask.getdata()) - {0, 255}:
            raise InvalidGoldenError("golden mask is non-binary or has incorrect dimensions")
        expected_mask = result.labels.point(
            lambda value, expected=label.value: 255 if value == expected else 0
        )
        if mask.tobytes() != expected_mask.tobytes():
            raise InvalidGoldenError("golden mask disagrees with the label field")
        count = mask.histogram()[255]
        if stored_stats.get(f"label.{label.value}.pixels") != count:
            raise InvalidGoldenError("golden label-count statistic is inconsistent")
        for index, value in enumerate(mask.tobytes()):
            coverage[index] += value == 255
    if set(coverage) != {1}:
        raise InvalidGoldenError("golden masks are not exhaustive and non-overlapping")


def _write_contact_sheet(
    destination: Path, before: Optional[LabelResult], after: LabelResult
) -> None:
    max_width, max_height = 320, 320
    scale = min(max_width / after.labels.width, max_height / after.labels.height)
    size = (
        max(1, round(after.labels.width * scale)),
        max(1, round(after.labels.height * scale)),
    )
    after_panel = _colorize(after).resize(size, Image.Resampling.NEAREST)
    if before is None or before.labels.size != after.labels.size:
        before_panel = Image.new("RGB", size, "#D1D5DB")
        difference_panel = Image.new("RGB", size, "#FF00AA")
    else:
        before_panel = _colorize(before).resize(size, Image.Resampling.NEAREST)
        difference = Image.new("RGB", after.labels.size, "#30343B")
        difference.putdata(
            [
                (48, 52, 59) if old == new else (255, 0, 170)
                for old, new in zip(before.labels.getdata(), after.labels.getdata())
            ]
        )
        difference_panel = difference.resize(size, Image.Resampling.NEAREST)
    padding, header = 16, 32
    sheet = Image.new("RGB", (size[0] * 3 + padding * 4, size[1] + header + padding * 2), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (title, panel) in enumerate(
        (("BEFORE", before_panel), ("AFTER", after_panel), ("DIFFERENCE", difference_panel))
    ):
        x = padding + index * (size[0] + padding)
        draw.text((x, padding), title, fill="#111827", font=font)
        y = padding + header
        sheet.paste(panel, (x, y))
        draw.rectangle((x - 1, y - 1, x + size[0], y + size[1]), outline="#6B7280")
    _save_png(sheet, destination)


def _colorize(result: LabelResult) -> Image.Image:
    lookup = {item.value: _hex_rgb(item.hex) for item in result.palette}
    image = Image.new("RGB", result.labels.size)
    image.putdata([lookup[value] for value in result.labels.getdata()])
    return image


def _atomic_replace_directory(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="golden-stage-", dir=destination.parent) as temp_root:
        stage = Path(temp_root) / "payload"
        shutil.copytree(source, stage)
        backup = Path(temp_root) / "previous"
        had_destination = destination.exists()
        try:
            if had_destination:
                os.replace(destination, backup)
            os.replace(stage, destination)
        except BaseException:
            if had_destination and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise


def _load_image(path: Path, mode: str) -> Image.Image:
    try:
        with Image.open(path) as source:
            source.load()
            if source.format != "PNG" or source.mode != mode:
                raise InvalidGoldenError(f"golden image {path.name} must be PNG {mode}")
            return source.copy()
    except OSError as error:
        raise InvalidGoldenError(f"invalid golden image {path.name}: {error}") from error


def _save_png(image: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", compress_level=9, optimize=False)


def _tree_sha256(directory: Path) -> str:
    if not directory.is_dir():
        raise InvalidGoldenError(f"golden directory does not exist: {directory}")
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hex_rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def _is_finite_number(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number not in {float("inf"), float("-inf")} and number == number
