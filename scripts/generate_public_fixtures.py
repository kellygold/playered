"""Generate original, redistributable raster regression inputs without private artwork."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "public-regression"
PALETTE = ("#F2D6AA", "#E75B12", "#0A3A78", "#202428")


def generate() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # Full-resolution gradients and seeded texture exercise the classifier rather
    # than benchmarking a tiny, constant-color input. Shapes below pin exact risks.
    y, x = np.indices((1680, 2520))
    rng = np.random.default_rng(2306)
    tone = ((x // 80 + y // 65) % 4).astype(np.uint8)
    colors = np.array([tuple(bytes.fromhex(c[1:])) for c in PALETTE], dtype=np.int16)
    pixels = np.clip(colors[tone] + rng.integers(-12, 13, (1680, 2520, 1)), 0, 255)
    stress = Image.fromarray(pixels.astype(np.uint8))
    draw = ImageDraw.Draw(stress)
    draw.rectangle((180, 300, 1499, 679), fill=PALETTE[0])
    for row in range(320, 660, 20):
        draw.line((200, row, 1200, row), fill=PALETTE[3], width=1)
    draw.rectangle((1230, 390, 1649, 859), fill=PALETTE[0])
    for row in range(400, 850, 12):
        for column in range(1240, 1640, 12):
            draw.rectangle((column, row, column + 4, row + 4), fill=PALETTE[3])
            draw.point((column + 2, row + 2), fill=PALETTE[0])
    stress.save(OUTPUT / "geometry-stress.png")

    detail = Image.new("RGB", (1408, 768), "white")
    draw = ImageDraw.Draw(detail)
    for row in range(12, 740, 19):
        for column in range(12, 1380, 29):
            draw.rectangle((column, row, column + 19, row + 9), outline="black", width=1)
    # Intentional extension/signature mismatch; no photograph or personal data.
    detail.save(OUTPUT / "signature-detail.png", format="JPEG", quality=92)

    glyphs = Image.new("RGB", (1760, 800), "white")
    draw = ImageDraw.Draw(glyphs)
    for row in range(30, 740, 45):
        for column in range(20, 1720, 28):
            draw.ellipse((column, row, column + 16, row + 24), outline="black", width=2)
            draw.line((column + 20, row, column + 20, row + 18), fill="black", width=1)
    glyphs.save(OUTPUT / "glyph-strokes.png")

    definitions = [
        (
            "geometry-stress.png",
            "png",
            2520,
            1680,
            600,
            400,
            [
                (
                    "enclosed-ring-grid",
                    1230,
                    390,
                    420,
                    470,
                    "Enclosed one-pixel centers and local ring walls.",
                ),
                (
                    "long-thin-lines",
                    180,
                    300,
                    1320,
                    380,
                    "Sub-nozzle lines retain measured clearance evidence.",
                ),
            ],
        ),
        (
            "signature-detail.png",
            "jpeg",
            1408,
            768,
            78,
            42.55,
            [
                ("dense-line-detail", 0, 0, 1408, 768, "Fine edges and repeated narrow gaps."),
                (
                    "extension-signature-mismatch",
                    0,
                    0,
                    1408,
                    768,
                    "JPEG decoded by bytes despite PNG extension.",
                ),
            ],
        ),
        (
            "glyph-strokes.png",
            "png",
            1760,
            800,
            88,
            40,
            [
                (
                    "small-stroke-counters",
                    0,
                    0,
                    1760,
                    800,
                    "Thin strokes and closed counters without font assets.",
                ),
            ],
        ),
    ]
    fixtures = []
    for filename, encoding, width, height, width_mm, height_mm, regions in definitions:
        payload = (OUTPUT / filename).read_bytes()
        fixtures.append(
            {
                "id": Path(filename).stem,
                "file": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload),
                "encoded_format": encoding,
                "width_px": width,
                "height_px": height,
                "canvas_width_mm": width_mm,
                "canvas_height_mm": height_mm,
                "source_path": "scripts/generate_public_fixtures.py",
                "provenance": "Original procedural shapes and seeded texture; no external artwork.",
                "rights": "MIT; see repository LICENSE.",
                "redistributable": True,
                "regions": [
                    {
                        "id": key,
                        "x_px": x,
                        "y_px": y,
                        "width_px": w,
                        "height_px": h,
                        "protects": purpose,
                    }
                    for key, x, y, w, h, purpose in regions
                ],
            }
        )
    (OUTPUT / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "collection": "public-procedural-regressions",
                "distribution": "redistributable",
                "fixtures": fixtures,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    generate()
