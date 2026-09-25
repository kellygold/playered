"""Exact calibration coupon generation and human measurement records."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import html
import io
import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.calibration.models import CalibrationFeatureKind, PrintabilityProfile

CALIBRATION_ARTIFACT_SCHEMA_VERSION = 2
CALIBRATION_RECORD_SCHEMA_VERSION = 1


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CalibrationBounds(ArtifactModel):
    x_mm: float = Field(ge=0)
    y_mm: float = Field(ge=0)
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)


class CalibrationFeature(ArtifactModel):
    id: str = Field(pattern=r"^(dot|hole|line|neck|gap)-[0-9]{2}-[0-9]+p[0-9]+$")
    kind: CalibrationFeatureKind
    column: int = Field(ge=0)
    nominal_dimension_mm: float = Field(gt=0)
    rasterized_dimension_mm: float = Field(gt=0)
    rasterized_width_mm: float = Field(gt=0)
    rasterized_height_mm: float = Field(gt=0)
    dimension_name: Literal["diameter", "width", "gap"]
    center_x_mm: float = Field(ge=0)
    center_y_mm: float = Field(ge=0)
    bounds: CalibrationBounds


class CalibrationArtifactManifest(ArtifactModel):
    schema_version: Literal[2] = CALIBRATION_ARTIFACT_SCHEMA_VERSION
    profile_id: str
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    printer_id: str
    nozzle_id: str
    material_class: str
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    raster_mm_per_pixel: float = Field(gt=0, le=1)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    svg_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    png_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    features: tuple[CalibrationFeature, ...] = Field(min_length=15)

    @model_validator(mode="after")
    def features_are_canonical_and_inside_canvas(self) -> CalibrationArtifactManifest:
        if self.features != tuple(sorted(self.features, key=lambda item: item.id)):
            raise ValueError("calibration features must use canonical ID ordering")
        if len({feature.id for feature in self.features}) != len(self.features):
            raise ValueError("calibration feature IDs must be unique")
        for feature in self.features:
            if (
                feature.bounds.x_mm + feature.bounds.width_mm > self.width_mm + 1e-9
                or feature.bounds.y_mm + feature.bounds.height_mm > self.height_mm + 1e-9
            ):
                raise ValueError("calibration feature must remain inside the coupon")
        expected_width = round(self.width_mm / self.raster_mm_per_pixel)
        expected_height = round(self.height_mm / self.raster_mm_per_pixel)
        if (self.width_px, self.height_px) != (expected_width, expected_height):
            raise ValueError("calibration raster dimensions do not match its physical scale")
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


class CalibrationRunStatus(str, Enum):
    TEMPLATE = "template"
    COMPLETED = "completed"


class CalibrationOutcome(str, Enum):
    UNTESTED = "untested"
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"


class CalibrationObservation(ArtifactModel):
    feature_id: str
    outcome: CalibrationOutcome
    measured_dimension_mm: Optional[float] = Field(default=None, ge=0)
    notes: str = Field(default="", max_length=1000)


class CalibrationRunRecord(ArtifactModel):
    schema_version: Literal[1] = CALIBRATION_RECORD_SCHEMA_VERSION
    status: CalibrationRunStatus
    record_id: Optional[str] = Field(default=None, pattern=r"^calibration-run-[a-z0-9-]{4,120}$")
    profile_id: str
    artifact_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    printer_id: str
    nozzle_id: str
    material_class: str
    layer_height_mm: Optional[float] = Field(default=None, gt=0, le=2)
    plate_id: Optional[str] = None
    filament: Optional[str] = Field(default=None, max_length=200)
    operator: Optional[str] = Field(default=None, max_length=200)
    printed_on: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    slicer_profile: Optional[str] = Field(default=None, max_length=300)
    observations: tuple[CalibrationObservation, ...] = Field(min_length=1)
    notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def completed_records_are_complete(self) -> CalibrationRunRecord:
        ids = tuple(item.feature_id for item in self.observations)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("calibration observations must use unique canonical feature IDs")
        if self.status == CalibrationRunStatus.COMPLETED:
            required = (
                self.record_id,
                self.layer_height_mm,
                self.plate_id,
                self.filament,
                self.operator,
                self.printed_on,
                self.slicer_profile,
            )
            if any(value is None or value == "" for value in required):
                raise ValueError("completed calibration records require full print provenance")
            if any(item.outcome == CalibrationOutcome.UNTESTED for item in self.observations):
                raise ValueError("completed calibration records cannot contain untested features")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class GeneratedCalibrationArtifact:
    manifest: CalibrationArtifactManifest
    svg_bytes: bytes
    png_bytes: bytes
    record_template: CalibrationRunRecord

    def __post_init__(self) -> None:
        if hashlib.sha256(self.svg_bytes).hexdigest() != self.manifest.svg_sha256:
            raise ValueError("generated calibration SVG does not match its manifest")
        if hashlib.sha256(self.png_bytes).hexdigest() != self.manifest.png_sha256:
            raise ValueError("generated calibration PNG does not match its manifest")
        if self.record_template.artifact_fingerprint != self.manifest.fingerprint():
            raise ValueError("calibration record template does not match its manifest")


@dataclass(frozen=True)
class _Primitive:
    feature: CalibrationFeature
    svg: str


def generate_calibration_artifact(
    profile: PrintabilityProfile,
    *,
    raster_mm_per_pixel: float = 0.05,
) -> GeneratedCalibrationArtifact:
    """Generate an exact-mm SVG and a discrete two-color PNG for a profile sweep."""

    if not math.isfinite(raster_mm_per_pixel) or not 0.02 <= raster_mm_per_pixel <= 0.25:
        raise ValueError("calibration raster scale must be between 0.02 and 0.25 mm/pixel")
    width_mm = 160.0
    height_mm = 112.0
    x_positions = _sample_positions(profile.sweep.sample_count, width_mm)
    rows = (
        (CalibrationFeatureKind.DOT, "diameter", profile.sweep.dot_diameters_mm, 14.0),
        (CalibrationFeatureKind.HOLE, "diameter", profile.sweep.hole_diameters_mm, 35.0),
        (CalibrationFeatureKind.LINE, "width", profile.sweep.line_widths_mm, 56.0),
        (CalibrationFeatureKind.NECK, "width", profile.sweep.neck_widths_mm, 77.0),
        (CalibrationFeatureKind.GAP, "gap", profile.sweep.gap_widths_mm, 98.0),
    )
    primitives = []
    for kind, dimension_name, values, y_mm in rows:
        for column, (x_mm, value) in enumerate(zip(x_positions, values)):
            primitives.append(
                _primitive(
                    kind,
                    dimension_name,
                    column,
                    value,
                    x_mm,
                    y_mm,
                    raster_mm_per_pixel,
                )
            )
    primitives.sort(key=lambda item: item.feature.id)
    svg_bytes = _render_svg(width_mm, height_mm, profile, primitives)
    png_bytes, width_px, height_px = _render_png(
        width_mm,
        height_mm,
        primitives,
        raster_mm_per_pixel,
    )
    profile_fingerprint = hashlib.sha256(
        json.dumps(
            profile.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest = CalibrationArtifactManifest(
        profile_id=profile.id,
        profile_fingerprint=profile_fingerprint,
        printer_id=profile.printer_id,
        nozzle_id=profile.nozzle_id,
        material_class=profile.material_class,
        width_mm=width_mm,
        height_mm=height_mm,
        raster_mm_per_pixel=raster_mm_per_pixel,
        width_px=width_px,
        height_px=height_px,
        svg_sha256=hashlib.sha256(svg_bytes).hexdigest(),
        png_sha256=hashlib.sha256(png_bytes).hexdigest(),
        features=tuple(item.feature for item in primitives),
    )
    record = create_calibration_record_template(profile, manifest)
    return GeneratedCalibrationArtifact(
        manifest=manifest,
        svg_bytes=svg_bytes,
        png_bytes=png_bytes,
        record_template=record,
    )


def create_calibration_record_template(
    profile: PrintabilityProfile,
    manifest: CalibrationArtifactManifest,
) -> CalibrationRunRecord:
    return CalibrationRunRecord(
        status=CalibrationRunStatus.TEMPLATE,
        profile_id=profile.id,
        artifact_fingerprint=manifest.fingerprint(),
        printer_id=profile.printer_id,
        nozzle_id=profile.nozzle_id,
        material_class=profile.material_class,
        observations=tuple(
            CalibrationObservation(feature_id=feature.id, outcome=CalibrationOutcome.UNTESTED)
            for feature in manifest.features
        ),
    )


def validate_calibration_record(
    record: CalibrationRunRecord,
    manifest: CalibrationArtifactManifest,
) -> None:
    if record.profile_id != manifest.profile_id:
        raise ValueError("calibration record profile does not match the artifact")
    if record.artifact_fingerprint != manifest.fingerprint():
        raise ValueError("calibration record fingerprint does not match the artifact")
    if (
        record.printer_id != manifest.printer_id
        or record.nozzle_id != manifest.nozzle_id
        or record.material_class != manifest.material_class
    ):
        raise ValueError("calibration record setup does not match the artifact")
    expected = tuple(feature.id for feature in manifest.features)
    actual = tuple(item.feature_id for item in record.observations)
    if actual != expected:
        raise ValueError("calibration record must contain every artifact feature exactly once")


def write_calibration_bundle(
    artifact: GeneratedCalibrationArtifact,
    output_directory: Path,
) -> tuple[Path, Path, Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = artifact.manifest.profile_id
    svg_path = output_directory / f"{stem}-coupon.svg"
    png_path = output_directory / f"{stem}-coupon.png"
    manifest_path = output_directory / f"{stem}-manifest.json"
    record_path = output_directory / f"{stem}-record-template.json"
    svg_path.write_bytes(artifact.svg_bytes)
    png_path.write_bytes(artifact.png_bytes)
    manifest_path.write_text(artifact.manifest.canonical_json() + "\n", encoding="utf-8")
    record_path.write_text(artifact.record_template.canonical_json() + "\n", encoding="utf-8")
    return svg_path, png_path, manifest_path, record_path


def _sample_positions(count: int, width_mm: float) -> tuple[float, ...]:
    if count < 2:
        raise ValueError("calibration coupon requires at least two columns")
    margin = 16.0
    step = (width_mm - 2 * margin) / (count - 1)
    return tuple(margin + index * step for index in range(count))


def _primitive(
    kind: CalibrationFeatureKind,
    dimension_name: str,
    column: int,
    value: float,
    x_mm: float,
    y_mm: float,
    raster_mm_per_pixel: float,
) -> _Primitive:
    feature_id = f"{kind.value}-{column:02d}-{_slug(value)}"
    dimension_center = (
        x_mm
        if kind
        in (
            CalibrationFeatureKind.DOT,
            CalibrationFeatureKind.HOLE,
            CalibrationFeatureKind.GAP,
        )
        else y_mm
    )
    actual = _rasterized_dimension(value, dimension_center, raster_mm_per_pixel)
    if kind in (CalibrationFeatureKind.DOT, CalibrationFeatureKind.HOLE):
        width_dimension = height_dimension = value
    elif kind == CalibrationFeatureKind.LINE:
        width_dimension, height_dimension = 12.0, value
    elif kind == CalibrationFeatureKind.NECK:
        width_dimension, height_dimension = 6.0, value
    else:
        width_dimension, height_dimension = value, 6.0
    rasterized_width = _rasterized_dimension(width_dimension, x_mm, raster_mm_per_pixel)
    rasterized_height = _rasterized_dimension(height_dimension, y_mm, raster_mm_per_pixel)
    if kind == CalibrationFeatureKind.DOT:
        bounds = CalibrationBounds(
            x_mm=x_mm - value / 2,
            y_mm=y_mm - value / 2,
            width_mm=value,
            height_mm=value,
        )
        svg = f'<circle cx="{_fmt(x_mm)}" cy="{_fmt(y_mm)}" r="{_fmt(value / 2)}"/>'
    elif kind == CalibrationFeatureKind.HOLE:
        bounds = CalibrationBounds(x_mm=x_mm - 4, y_mm=y_mm - 4, width_mm=8, height_mm=8)
        svg = (
            f'<rect x="{_fmt(x_mm - 4)}" y="{_fmt(y_mm - 4)}" width="8" height="8"/>'
            f'<circle class="negative" cx="{_fmt(x_mm)}" cy="{_fmt(y_mm)}" '
            f'r="{_fmt(value / 2)}"/>'
        )
    elif kind == CalibrationFeatureKind.LINE:
        bounds = CalibrationBounds(
            x_mm=x_mm - 6,
            y_mm=y_mm - value / 2,
            width_mm=12,
            height_mm=value,
        )
        svg = (
            f'<rect x="{_fmt(x_mm - 6)}" y="{_fmt(y_mm - value / 2)}" '
            f'width="12" height="{_fmt(value)}"/>'
        )
    elif kind == CalibrationFeatureKind.NECK:
        bounds = CalibrationBounds(x_mm=x_mm - 7, y_mm=y_mm - 3, width_mm=14, height_mm=6)
        svg = (
            f'<rect x="{_fmt(x_mm - 7)}" y="{_fmt(y_mm - 3)}" width="4" height="6"/>'
            f'<rect x="{_fmt(x_mm + 3)}" y="{_fmt(y_mm - 3)}" width="4" height="6"/>'
            f'<rect x="{_fmt(x_mm - 3)}" y="{_fmt(y_mm - value / 2)}" '
            f'width="6" height="{_fmt(value)}"/>'
        )
    else:
        bounds = CalibrationBounds(x_mm=x_mm - 7, y_mm=y_mm - 3, width_mm=14, height_mm=6)
        left_width = 7 - value / 2
        svg = (
            f'<rect x="{_fmt(x_mm - 7)}" y="{_fmt(y_mm - 3)}" '
            f'width="{_fmt(left_width)}" height="6"/>'
            f'<rect x="{_fmt(x_mm + value / 2)}" y="{_fmt(y_mm - 3)}" '
            f'width="{_fmt(left_width)}" height="6"/>'
        )
    return _Primitive(
        feature=CalibrationFeature(
            id=feature_id,
            kind=kind,
            column=column,
            nominal_dimension_mm=value,
            rasterized_dimension_mm=actual,
            rasterized_width_mm=rasterized_width,
            rasterized_height_mm=rasterized_height,
            dimension_name=dimension_name,
            center_x_mm=x_mm,
            center_y_mm=y_mm,
            bounds=bounds,
        ),
        svg=svg,
    )


def _render_svg(
    width_mm: float,
    height_mm: float,
    profile: PrintabilityProfile,
    primitives: list[_Primitive],
) -> bytes:
    body = "".join(item.svg for item in primitives)
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width_mm)}mm" '
        f'height="{_fmt(height_mm)}mm" viewBox="0 0 {_fmt(width_mm)} {_fmt(height_mm)}">'
        f"<title>{html.escape(profile.display_name)} printability calibration</title>"
        '<rect class="background" width="100%" height="100%"/>'
        f'<g class="features">{body}</g>'
        "<style>.background,.negative{fill:#fff}.features{fill:#000;shape-rendering:geometricPrecision}"
        "</style></svg>"
    )
    return svg.encode("utf-8")


def _render_png(
    width_mm: float,
    height_mm: float,
    primitives: list[_Primitive],
    raster_mm_per_pixel: float,
) -> tuple[bytes, int, int]:
    width_px = round(width_mm / raster_mm_per_pixel)
    height_px = round(height_mm / raster_mm_per_pixel)
    image = Image.new("L", (width_px, height_px), 255)
    draw = ImageDraw.Draw(image)
    scale = 1 / raster_mm_per_pixel

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[int, int, int, int]:
        return (
            round(x0 * scale),
            round(y0 * scale),
            max(round(x0 * scale), round(x1 * scale) - 1),
            max(round(y0 * scale), round(y1 * scale) - 1),
        )

    for item in primitives:
        feature = item.feature
        x = feature.center_x_mm
        y = feature.center_y_mm
        value = feature.nominal_dimension_mm
        if feature.kind == CalibrationFeatureKind.DOT:
            draw.ellipse(box(x - value / 2, y - value / 2, x + value / 2, y + value / 2), fill=0)
        elif feature.kind == CalibrationFeatureKind.HOLE:
            draw.rectangle(box(x - 4, y - 4, x + 4, y + 4), fill=0)
            draw.ellipse(box(x - value / 2, y - value / 2, x + value / 2, y + value / 2), fill=255)
        elif feature.kind == CalibrationFeatureKind.LINE:
            draw.rectangle(box(x - 6, y - value / 2, x + 6, y + value / 2), fill=0)
        elif feature.kind == CalibrationFeatureKind.NECK:
            draw.rectangle(box(x - 7, y - 3, x - 3, y + 3), fill=0)
            draw.rectangle(box(x + 3, y - 3, x + 7, y + 3), fill=0)
            draw.rectangle(box(x - 3, y - value / 2, x + 3, y + value / 2), fill=0)
        else:
            draw.rectangle(box(x - 7, y - 3, x - value / 2, y + 3), fill=0)
            draw.rectangle(box(x + value / 2, y - 3, x + 7, y + 3), fill=0)
    output = io.BytesIO()
    image.convert("RGBA").save(
        output,
        format="PNG",
        optimize=True,
        dpi=(25.4 / raster_mm_per_pixel, 25.4 / raster_mm_per_pixel),
    )
    image.close()
    return output.getvalue(), width_px, height_px


def _slug(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    whole, _, fraction = text.partition(".")
    return f"{whole}p{fraction or '0'}"


def _rasterized_dimension(value: float, center: float, scale_mm: float) -> float:
    first = round((center - value / 2) / scale_mm)
    second = round((center + value / 2) / scale_mm)
    return round(max(1, second - first) * scale_mm, 9)


def _fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")
