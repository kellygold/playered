import hashlib
from pathlib import Path

from PIL import Image

from image23mf.quality.synthetic_fixtures import (
    SyntheticCatalog,
    generate_synthetic_fixture_suite,
)

COMMITTED_ROOT = Path(__file__).parent / "fixtures" / "synthetic"
REQUIRED_ANNOTATIONS = {
    "dots",
    "rings",
    "holes",
    "necks",
    "long-lines",
    "anti-alias-edges",
    "canvas-boundaries",
    "transparent-input",
    "partial-alpha",
    "crop-extremes",
    "near-color-ties",
    "dense-gradient",
}


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_generator_is_byte_deterministic_and_matches_committed_outputs(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_catalog = generate_synthetic_fixture_suite(first)
    second_catalog = generate_synthetic_fixture_suite(second)

    assert first_catalog == second_catalog
    assert _tree(first) == _tree(second)
    committed = _tree(COMMITTED_ROOT)
    committed.pop("README.md")
    assert _tree(first) == committed


def test_catalog_dimensions_physical_scales_hashes_and_alpha_are_exact() -> None:
    catalog = SyntheticCatalog.model_validate_json(
        (COMMITTED_ROOT / "catalog.json").read_text(encoding="utf-8")
    )

    assert catalog.schema_version == 1
    assert catalog.generator_version == 1
    assert len(catalog.palette) == 4
    assert {item.id for item in catalog.fixtures} == {
        "geometry-nozzle-020",
        "geometry-nozzle-040",
        "boundary-transparency",
        "palette-degeneracy",
    }
    annotations = {annotation.id for item in catalog.fixtures for annotation in item.annotations}
    assert annotations == REQUIRED_ANNOTATIONS

    by_id = {item.id: item for item in catalog.fixtures}
    assert by_id["geometry-nozzle-020"].canvas.nozzle_diameter_mm == 0.2
    assert by_id["geometry-nozzle-020"].canvas.pixels_per_mm == 20
    assert by_id["geometry-nozzle-040"].canvas.nozzle_diameter_mm == 0.4
    assert by_id["geometry-nozzle-040"].canvas.pixels_per_mm == 10

    for item in catalog.fixtures:
        path = COMMITTED_ROOT / item.file
        payload = path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == item.png_sha256
        with Image.open(path) as image:
            rgba = image.convert("RGBA")
            assert image.format == "PNG"
            assert image.size == (item.width_px, item.height_px)
            assert hashlib.sha256(rgba.tobytes()).hexdigest() == item.pixel_sha256
            has_transparency = rgba.getextrema()[3] != (255, 255)
            assert has_transparency is item.has_transparency
        assert item.width_px / item.canvas.width_mm == item.canvas.pixels_per_mm
        assert item.height_px / item.canvas.height_mm == item.canvas.pixels_per_mm


def test_crop_and_palette_edge_case_contracts_are_complete() -> None:
    catalog = SyntheticCatalog.model_validate_json(
        (COMMITTED_ROOT / "catalog.json").read_text(encoding="utf-8")
    )

    assert {case["id"] for case in catalog.crop_cases} == {
        "full-canvas",
        "single-pixel",
        "one-pixel-edge-strip",
        "far-corner",
        "transparent-only",
        "negative-origin-clamp",
        "outside-extent-clamp",
    }
    assert {case["id"] for case in catalog.palette_cases} == {
        "single-color",
        "exact-duplicate",
        "near-black-tie",
        "near-white-tie",
        "over-color-limit",
    }
    assert all(case["protects"] for case in catalog.palette_cases)
