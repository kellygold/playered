#!/usr/bin/env python3
"""Generate the exact 0.2/0.4 mm 3MF specimens and an unsigned observation template."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from image23mf.quality.release_fixture import (
    build_physical_release_3mf,
    build_physical_release_geometry,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "workspace" / "qa" / "physical-specimens"
PROFILES = ((0.2, 0.1), (0.4, 0.2))
FEATURES = (
    ("isolated-dot", "0.4 mm isolated dot remains attached and recognizable"),
    ("enclosed-hole", "2 mm square hole remains open inside the 4 mm ring"),
    ("extended-line", "0.2 × 6 mm line remains continuous"),
    ("supported-neck", "0.2 mm bridge remains continuous between both pads"),
    ("face-quality", "visible face has no unexplained void, ring, or floating-region artifact"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)

    specimens = []
    for nozzle_mm, layer_height_mm in PROFILES:
        labels, topology, document, quality = build_physical_release_geometry(
            layer_height_mm=layer_height_mm
        )
        package = build_physical_release_3mf(
            nozzle_mm=nozzle_mm,
            layer_height_mm=layer_height_mm,
        )
        filename = f"image23mf-physical-release-{nozzle_mm:.1f}mm.3mf"
        path = arguments.output / filename
        path.write_bytes(package)
        specimens.append(
            {
                "nozzle_mm": nozzle_mm,
                "layer_height_mm": layer_height_mm,
                "filename": filename,
                "sha256": hashlib.sha256(package).hexdigest(),
                "bytes": len(package),
                "label_sha256": hashlib.sha256(labels.pixels).hexdigest(),
                "geometry_fingerprint": document.fingerprint(),
                "quality_fingerprint": quality.fingerprint,
                "gap_area_mm2": topology.coverage.gap_area_mm2,
                "overlap_area_mm2": topology.coverage.overlap_area_mm2,
                "observations": [
                    {"feature": feature, "criterion": criterion, "outcome": "not_observed"}
                    for feature, criterion in FEATURES
                ],
            }
        )

    manifest = {
        "schema_version": 1,
        "status": "not_observed",
        "fixture": {
            "width_mm": 20,
            "height_mm": 20,
            "base_thickness_mm": 1.2,
            "artwork_thickness_mm": 0.4,
            "colors": ["#CBC6B8", "#000000"],
        },
        "print_provenance": {
            "printer": None,
            "plate": None,
            "filaments": [],
            "orientation": "face_up",
            "slicer_version": None,
            "operator": None,
            "printed_on": None,
            "photo_paths": [],
            "notes": "",
        },
        "specimens": specimens,
    }
    manifest_path = arguments.output / "physical-observations.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Generated {len(specimens)} specimens and {manifest_path}")
    for item in specimens:
        print(f"- {arguments.output / item['filename']} ({item['sha256']})")


if __name__ == "__main__":
    main()
