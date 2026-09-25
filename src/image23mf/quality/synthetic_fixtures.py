"""Deterministic physical-scale torture artwork for print-processing regressions."""

import hashlib
import io
import json
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

PALETTE = ("#F2D6AA", "#E75B12", "#0A3A78", "#202428")
SUPERSAMPLE = 4


class FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhysicalCanvas(FixtureModel):
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    pixels_per_mm: float = Field(gt=0)
    nozzle_diameter_mm: Optional[float] = Field(default=None, gt=0)


class FixtureAnnotation(FixtureModel):
    id: str
    protects: str
    parameters_mm: tuple[float, ...] = ()


class FixtureCatalogEntry(FixtureModel):
    id: str
    file: str
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    encoded_format: str
    color_mode: str
    has_transparency: bool
    png_sha256: str
    pixel_sha256: str
    canvas: PhysicalCanvas
    annotations: tuple[FixtureAnnotation, ...]


class SyntheticCatalog(FixtureModel):
    schema_version: int
    generator_version: int
    palette: tuple[str, ...]
    fixtures: tuple[FixtureCatalogEntry, ...]
    crop_cases: tuple[dict, ...]
    palette_cases: tuple[dict, ...]


def generate_synthetic_fixture_suite(output_directory: Path) -> SyntheticCatalog:
    """Render the complete suite and a deterministic catalog into ``output_directory``."""
    output_directory.mkdir(parents=True, exist_ok=True)
    rendered = (
        _render_geometry_fixture(
            fixture_id="geometry-nozzle-020",
            width_px=1024,
            height_px=768,
            width_mm=51.2,
            height_mm=38.4,
            nozzle_diameter_mm=0.2,
        ),
        _render_geometry_fixture(
            fixture_id="geometry-nozzle-040",
            width_px=1024,
            height_px=768,
            width_mm=102.4,
            height_mm=76.8,
            nozzle_diameter_mm=0.4,
        ),
        _render_boundary_transparency_fixture(),
        _render_palette_fixture(),
    )
    entries = tuple(_write_fixture(output_directory, item) for item in rendered)
    catalog = SyntheticCatalog(
        schema_version=1,
        generator_version=1,
        palette=PALETTE,
        fixtures=entries,
        crop_cases=(
            {"id": "full-canvas", "normalized_box": [0.0, 0.0, 1.0, 1.0]},
            {"id": "single-pixel", "pixel_box": [511, 511, 512, 512]},
            {"id": "one-pixel-edge-strip", "pixel_box": [0, 0, 1, 512]},
            {"id": "far-corner", "pixel_box": [448, 448, 512, 512]},
            {"id": "transparent-only", "pixel_box": [210, 210, 302, 302]},
            {"id": "negative-origin-clamp", "pixel_box": [-64, -32, 128, 160]},
            {"id": "outside-extent-clamp", "pixel_box": [420, 420, 640, 620]},
        ),
        palette_cases=(
            {
                "id": "single-color",
                "colors": ["#F2D6AA"],
                "protects": "A valid one-label field remains exhaustive.",
            },
            {
                "id": "exact-duplicate",
                "colors": ["#202428", "#202428", "#F2D6AA"],
                "protects": "Duplicate swatches are rejected or merged deterministically.",
            },
            {
                "id": "near-black-tie",
                "colors": ["#000000", "#010101", "#020202", "#F2D6AA"],
                "protects": "Perceptual ties use stable palette order.",
            },
            {
                "id": "near-white-tie",
                "colors": ["#FDFDFD", "#FEFEFE", "#FFFFFF", "#E75B12"],
                "protects": "Near-identical light filaments do not create nondeterministic labels.",
            },
            {
                "id": "over-color-limit",
                "colors": ["#202428", "#0A3A78", "#E75B12", "#F2D6AA", "#25A45A"],
                "protects": "A palette above the four-color project limit fails explicitly.",
            },
        ),
    )
    (output_directory / "catalog.json").write_text(
        json.dumps(catalog.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return catalog


class _RenderedFixture:
    def __init__(
        self,
        *,
        fixture_id: str,
        image: Image.Image,
        canvas: PhysicalCanvas,
        annotations: tuple[FixtureAnnotation, ...],
    ) -> None:
        self.fixture_id = fixture_id
        self.image = image
        self.canvas = canvas
        self.annotations = annotations


def _render_geometry_fixture(
    *,
    fixture_id: str,
    width_px: int,
    height_px: int,
    width_mm: float,
    height_mm: float,
    nozzle_diameter_mm: float,
) -> _RenderedFixture:
    scale = width_px / width_mm
    image = Image.new("RGBA", (width_px * SUPERSAMPLE, height_px * SUPERSAMPLE), PALETTE[0])
    draw = ImageDraw.Draw(image)
    px = scale * SUPERSAMPLE
    width = width_px * SUPERSAMPLE
    height = height_px * SUPERSAMPLE
    diameters = (0.10, 0.20, 0.30, 0.40, 0.60, 0.80, 1.20)
    line_widths = (0.10, 0.20, 0.30, 0.40, 0.60)

    def mm(value: float) -> int:
        return max(1, round(value * px))

    def row(fraction: float) -> int:
        return round(height * fraction)

    def centers(count: int) -> tuple[int, ...]:
        margin = mm(4)
        span = width - margin * 2
        return tuple(round(margin + span * index / (count - 1)) for index in range(count))

    for center, diameter in zip(centers(len(diameters)), diameters):
        radius = mm(diameter) / 2
        draw.ellipse(
            (center - radius, row(0.10) - radius, center + radius, row(0.10) + radius),
            fill=PALETTE[3],
        )

    for center, diameter in zip(centers(len(diameters)), diameters):
        outer_radius = mm(max(0.35, diameter + 0.35)) / 2
        wall = mm(diameter) / 2
        inner_radius = max(0, outer_radius - wall)
        draw.ellipse(
            (
                center - outer_radius,
                row(0.23) - outer_radius,
                center + outer_radius,
                row(0.23) + outer_radius,
            ),
            fill=PALETTE[1],
        )
        if inner_radius:
            draw.ellipse(
                (
                    center - inner_radius,
                    row(0.23) - inner_radius,
                    center + inner_radius,
                    row(0.23) + inner_radius,
                ),
                fill=PALETTE[0],
            )

    hole_y = row(0.36)
    draw.rectangle((mm(2), hole_y - mm(2), width - mm(2), hole_y + mm(2)), fill=PALETTE[3])
    for center, diameter in zip(centers(len(diameters)), diameters):
        radius = mm(diameter) / 2
        draw.ellipse(
            (center - radius, hole_y - radius, center + radius, hole_y + radius),
            fill=PALETTE[0],
        )

    for center, neck_width in zip(centers(len(diameters)), diameters):
        y = row(0.50)
        lobe_radius = mm(1.35)
        half_gap = mm(1.0)
        draw.ellipse(
            (
                center - half_gap - lobe_radius,
                y - lobe_radius,
                center - half_gap + lobe_radius,
                y + lobe_radius,
            ),
            fill=PALETTE[2],
        )
        draw.ellipse(
            (
                center + half_gap - lobe_radius,
                y - lobe_radius,
                center + half_gap + lobe_radius,
                y + lobe_radius,
            ),
            fill=PALETTE[2],
        )
        half_neck = mm(neck_width) / 2
        draw.rectangle(
            (center - half_gap, y - half_neck, center + half_gap, y + half_neck),
            fill=PALETTE[2],
        )

    line_x0 = mm(3)
    line_x1 = width - mm(3)
    for index, line_width in enumerate(line_widths):
        y = row(0.62) + mm(index * 1.1)
        draw.line((line_x0, y, line_x1, y), fill=PALETTE[3], width=mm(line_width))
    draw.line(
        (line_x0, row(0.78), line_x1, row(0.67)),
        fill=PALETTE[1],
        width=mm(0.25),
    )

    boundary_y = row(0.88)
    boundary_radius = mm(1.5)
    for center_x in (0, width, width // 2):
        draw.ellipse(
            (
                center_x - boundary_radius,
                boundary_y - boundary_radius,
                center_x + boundary_radius,
                boundary_y + boundary_radius,
            ),
            fill=PALETTE[2],
        )
    draw.polygon(
        ((mm(4), height), (width // 3, row(0.82)), (width * 2 // 3, height)),
        fill=PALETTE[1],
    )
    final = image.resize((width_px, height_px), Image.Resampling.LANCZOS)
    return _RenderedFixture(
        fixture_id=fixture_id,
        image=final,
        canvas=PhysicalCanvas(
            width_mm=width_mm,
            height_mm=height_mm,
            pixels_per_mm=scale,
            nozzle_diameter_mm=nozzle_diameter_mm,
        ),
        annotations=(
            FixtureAnnotation(
                id="dots",
                protects="Islands transition across sub-nozzle and printable diameters.",
                parameters_mm=diameters,
            ),
            FixtureAnnotation(
                id="rings",
                protects="Thin walls and enclosed centers do not become false perimeter artifacts.",
                parameters_mm=diameters,
            ),
            FixtureAnnotation(
                id="holes",
                protects="Enclosed voids are measured independently from foreground islands.",
                parameters_mm=diameters,
            ),
            FixtureAnnotation(
                id="necks",
                protects="Narrow connections are reported or repaired without deleting both lobes.",
                parameters_mm=diameters,
            ),
            FixtureAnnotation(
                id="long-lines",
                protects=(
                    "Area cleanup preserves long printable strokes and flags sub-nozzle widths."
                ),
                parameters_mm=line_widths,
            ),
            FixtureAnnotation(
                id="anti-alias-edges",
                protects="Sloped supersampled boundaries quantize deterministically.",
                parameters_mm=(0.25,),
            ),
            FixtureAnnotation(
                id="canvas-boundaries",
                protects="Clipped components touching each boundary remain valid regions.",
                parameters_mm=(1.5,),
            ),
        ),
    )


def _render_boundary_transparency_fixture() -> _RenderedFixture:
    size = 512
    image = Image.new("RGBA", (size * SUPERSAMPLE, size * SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = SUPERSAMPLE
    draw.rectangle((0, 0, 112 * scale, 150 * scale), fill=(10, 58, 120, 255))
    draw.ellipse(
        (410 * scale, -45 * scale, 555 * scale, 100 * scale),
        fill=(231, 91, 18, 180),
    )
    draw.polygon(
        ((0, 470 * scale), (210 * scale, 512 * scale), (0, 512 * scale)),
        fill=(32, 36, 40, 96),
    )
    draw.line(
        (-40 * scale, 390 * scale, 550 * scale, 115 * scale),
        fill=(32, 36, 40, 255),
        width=2 * scale,
    )
    for alpha, inset in ((32, 0), (64, 12), (128, 24), (192, 36), (255, 48)):
        draw.rectangle(
            (
                (160 + inset) * scale,
                (160 + inset) * scale,
                (352 - inset) * scale,
                (352 - inset) * scale,
            ),
            outline=(242, 214, 170, alpha),
            width=8 * scale,
        )
    final = image.resize((size, size), Image.Resampling.LANCZOS)
    return _RenderedFixture(
        fixture_id="boundary-transparency",
        image=final,
        canvas=PhysicalCanvas(width_mm=64, height_mm=64, pixels_per_mm=8),
        annotations=(
            FixtureAnnotation(
                id="transparent-input",
                protects="Transparent pixels remain distinct from opaque background colors.",
            ),
            FixtureAnnotation(
                id="partial-alpha",
                protects="Partial alpha compositing is deterministic and does not invent labels.",
            ),
            FixtureAnnotation(
                id="crop-extremes",
                protects="Shapes crossing every edge exercise clamped and minimal crop behavior.",
            ),
            FixtureAnnotation(
                id="anti-alias-edges",
                protects="Diagonal and curved alpha edges retain deterministic coverage.",
            ),
        ),
    )


def _render_palette_fixture() -> _RenderedFixture:
    width, height = 512, 256
    image = Image.new("RGBA", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    colors = (
        "#000000",
        "#010101",
        "#020202",
        "#FDFDFD",
        "#FEFEFE",
        "#FFFFFF",
        "#E75B12",
        "#E65C13",
    )
    stripe = width // len(colors)
    for index, color in enumerate(colors):
        draw.rectangle((index * stripe, 0, (index + 1) * stripe, height // 2), fill=color)
    for y in range(height // 2, height):
        value = round(255 * (y - height // 2) / (height // 2 - 1))
        for x in range(width):
            channel = (value + (x % 4)) % 256
            image.putpixel((x, y), (channel, channel, channel, 255))
    return _RenderedFixture(
        fixture_id="palette-degeneracy",
        image=image,
        canvas=PhysicalCanvas(width_mm=64, height_mm=32, pixels_per_mm=8),
        annotations=(
            FixtureAnnotation(
                id="near-color-ties",
                protects=(
                    "Near-identical dark, light, and orange inputs expose unstable tie-breaking."
                ),
            ),
            FixtureAnnotation(
                id="dense-gradient",
                protects=(
                    "Every grayscale boundary exercises deterministic nearest-color assignment."
                ),
            ),
        ),
    )


def _write_fixture(directory: Path, rendered: _RenderedFixture) -> FixtureCatalogEntry:
    payload = _png_bytes(rendered.image)
    filename = f"{rendered.fixture_id}.png"
    (directory / filename).write_bytes(payload)
    pixels = rendered.image.tobytes()
    return FixtureCatalogEntry(
        id=rendered.fixture_id,
        file=filename,
        width_px=rendered.image.width,
        height_px=rendered.image.height,
        encoded_format="png",
        color_mode=rendered.image.mode,
        has_transparency=rendered.image.getextrema()[3] != (255, 255),
        png_sha256=hashlib.sha256(payload).hexdigest(),
        pixel_sha256=hashlib.sha256(pixels).hexdigest(),
        canvas=rendered.canvas,
        annotations=rendered.annotations,
    )


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=9, optimize=False)
    return output.getvalue()
