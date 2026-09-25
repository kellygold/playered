#!/usr/bin/env python3
"""Generate the small actual-output payload used to exercise the golden workflow."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from image23mf.quality.goldens import (
    GoldenLabel,
    GoldenTolerance,
    LabelResult,
    StatTolerance,
    write_golden_payload,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    labels = Image.new("L", (16, 12), 0)
    draw = ImageDraw.Draw(labels)
    draw.rectangle((1, 1, 5, 5), fill=1)
    draw.ellipse((7, 1, 13, 7), fill=2)
    draw.line((0, 10, 15, 10), fill=3, width=1)
    palette = (
        GoldenLabel(value=0, name="Cream", hex="#F2D6AA"),
        GoldenLabel(value=1, name="Orange", hex="#E75B12"),
        GoldenLabel(value=2, name="Blue", hex="#0A3A78"),
        GoldenLabel(value=3, name="Charcoal", hex="#202428"),
    )
    write_golden_payload(
        arguments.output,
        LabelResult(
            case_id="workflow-specimen",
            labels=labels,
            palette=palette,
            stats={"edge.length_mm": 12, "regions.count": 3},
        ),
        tolerance=GoldenTolerance(
            max_changed_pixels=1,
            max_changed_ratio=1 / (16 * 12),
            stats={"edge.length_mm": StatTolerance(absolute=0.2, relative=0.01)},
        ),
    )
    print(f"Generated golden actual-output specimen at {arguments.output}")


if __name__ == "__main__":
    main()
