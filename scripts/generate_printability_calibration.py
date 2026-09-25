"""Generate exact SVG/PNG coupons, manifests, and blank print records."""

from __future__ import annotations

import argparse
from pathlib import Path

from image23mf.calibration import (
    generate_calibration_artifact,
    load_bundled_printability_catalog,
    write_calibration_bundle,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="Profile ID to generate; repeat as needed. Defaults to every bundled profile.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("workspace/calibration"),
        help="Output directory (default: workspace/calibration).",
    )
    parser.add_argument(
        "--raster-mm-per-pixel",
        type=float,
        default=0.05,
        help="PNG physical pixel scale from 0.02 to 0.25 mm (default: 0.05).",
    )
    arguments = parser.parse_args()
    catalog = load_bundled_printability_catalog()
    requested = set(arguments.profile)
    unknown = requested - {profile.id for profile in catalog.profiles}
    if unknown:
        parser.error(f"unknown profile IDs: {', '.join(sorted(unknown))}")
    profiles = tuple(
        profile for profile in catalog.profiles if not requested or profile.id in requested
    )
    for profile in profiles:
        artifact = generate_calibration_artifact(
            profile,
            raster_mm_per_pixel=arguments.raster_mm_per_pixel,
        )
        paths = write_calibration_bundle(artifact, arguments.output)
        print(f"{profile.id}: {', '.join(str(path) for path in paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
