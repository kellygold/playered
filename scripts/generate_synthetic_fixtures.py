#!/usr/bin/env python3
"""Regenerate the committed deterministic synthetic fixture corpus."""

from pathlib import Path

from image23mf.quality import generate_synthetic_fixture_suite

ROOT = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    destination = ROOT / "tests" / "fixtures" / "synthetic"
    catalog = generate_synthetic_fixture_suite(destination)
    print(f"Generated {len(catalog.fixtures)} fixtures in {destination}")
