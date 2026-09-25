import numpy as np
import pytest
from PIL import Image

from image23mf.engine import (
    PaletteMetricsOptions,
    classify_palette,
    compute_palette_metrics,
    delta_e_76,
    rgb_to_lab,
)

CONFIG_HASH = "a" * 64


def test_controlled_split_has_exact_coverage_error_adjacency_and_components() -> None:
    pixels = np.zeros((2, 4, 3), dtype=np.uint8)
    pixels[:, 2:, :] = 255
    image = Image.fromarray(pixels)
    try:
        quantized = classify_palette(image, ("#000000", "#FFFFFF"))
        metrics = compute_palette_metrics(
            image,
            quantized,
            config_sha256=CONFIG_HASH,
            width_mm=40,
            height_mm=20,
        )
    finally:
        image.close()

    assert metrics.visible_pixel_count == 8
    assert [color.pixel_count for color in metrics.colors] == [4, 4]
    assert [color.coverage_ratio for color in metrics.colors] == [0.5, 0.5]
    assert metrics.reconstruction.alpha_weighted_mean_delta_e == pytest.approx(0)
    assert metrics.reconstruction.alpha_weighted_p95_delta_e == pytest.approx(0)
    assert [color.component_count for color in metrics.colors] == [1, 1]
    assert [color.smallest_component_mm2 for color in metrics.colors] == [400, 400]
    assert metrics.fragmentation.component_count == 2
    assert metrics.fragmentation.excess_component_count == 0
    assert len(metrics.adjacency) == 1
    assert metrics.adjacency[0].boundary_edge_count == 2
    assert metrics.adjacency[0].delta_e == pytest.approx(100)
    assert metrics.minimum_adjacent_delta_e == pytest.approx(100)
    assert len(metrics.fingerprint()) == 64


def test_reconstruction_error_matches_known_delta_e_without_false_score() -> None:
    image = Image.new("RGB", (3, 1), "#808080")
    try:
        quantized = classify_palette(image, ("#FFFFFF", "#000000"))
        metrics = compute_palette_metrics(
            image,
            quantized,
            config_sha256=CONFIG_HASH,
            width_mm=30,
            height_mm=10,
        )
    finally:
        image.close()

    expected = delta_e_76(rgb_to_lab((128, 128, 128)), rgb_to_lab((255, 255, 255)))
    assert metrics.reconstruction.alpha_weighted_mean_delta_e == pytest.approx(expected)
    assert metrics.reconstruction.alpha_weighted_p95_delta_e == pytest.approx(expected)
    assert metrics.colors[0].mean_delta_e == pytest.approx(expected)
    assert metrics.colors[1].mean_delta_e is None
    assert metrics.minimum_adjacent_delta_e is None


def test_checkerboard_reports_four_connected_fragmentation_and_all_boundary_edges() -> None:
    pixels = np.indices((3, 3)).sum(axis=0) % 2 * 255
    image = Image.fromarray(pixels.astype(np.uint8)).convert("RGB")
    try:
        quantized = classify_palette(image, ("#000000", "#FFFFFF"))
        metrics = compute_palette_metrics(
            image,
            quantized,
            config_sha256=CONFIG_HASH,
            width_mm=30,
            height_mm=30,
        )
    finally:
        image.close()

    assert [color.component_count for color in metrics.colors] == [5, 4]
    assert [color.largest_component_share for color in metrics.colors] == [0.2, 0.25]
    assert metrics.fragmentation.component_count == 9
    assert metrics.fragmentation.excess_component_count == 7
    assert metrics.fragmentation.single_pixel_component_count == 9
    assert metrics.fragmentation.components_per_100_mm2 == pytest.approx(1)
    assert metrics.adjacency[0].boundary_edge_count == 12


def test_transparency_excludes_hidden_pixels_and_keeps_physical_pixel_area() -> None:
    image = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    image.putpixel((0, 0), (255, 0, 0, 255))
    image.putpixel((1, 0), (0, 0, 255, 128))
    try:
        quantized = classify_palette(image, ("#FF0000", "#0000FF"))
        metrics = compute_palette_metrics(
            image,
            quantized,
            config_sha256=CONFIG_HASH,
            width_mm=20,
            height_mm=20,
        )
    finally:
        image.close()

    assert metrics.visible_pixel_count == 2
    assert [color.pixel_count for color in metrics.colors] == [1, 1]
    assert [color.smallest_component_mm2 for color in metrics.colors] == [100, 100]
    assert metrics.reconstruction.alpha_weighted_mean_delta_e == pytest.approx(0)


def test_metrics_are_chunk_independent_and_serialization_is_reproducible() -> None:
    pixels = np.random.default_rng(24).integers(0, 256, size=(31, 43, 3), dtype=np.uint8)
    image = Image.fromarray(pixels)
    try:
        quantized = classify_palette(image, ("#000000", "#777777", "#FFFFFF"))
        small = compute_palette_metrics(
            image,
            quantized,
            config_sha256=CONFIG_HASH,
            width_mm=86,
            height_mm=62,
            options=PaletteMetricsOptions(chunk_pixels=256),
        )
        large = compute_palette_metrics(
            image,
            quantized,
            config_sha256=CONFIG_HASH,
            width_mm=86,
            height_mm=62,
            options=PaletteMetricsOptions(chunk_pixels=4096),
        )
    finally:
        image.close()

    assert small.colors == large.colors
    assert small.reconstruction == large.reconstruction
    assert small.adjacency == large.adjacency
    assert small.fragmentation == large.fragmentation
    assert small.options_fingerprint != large.options_fingerprint
    assert small.canonical_json() == small.canonical_json()


def test_invalid_dimensions_alpha_and_empty_visibility_are_rejected() -> None:
    image = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    other = Image.new("RGB", (3, 2), "black")
    try:
        quantized = classify_palette(image, ("#000000", "#FFFFFF"))
        with pytest.raises(ValueError, match="visible pixel"):
            compute_palette_metrics(
                image,
                quantized,
                config_sha256=CONFIG_HASH,
                width_mm=20,
                height_mm=20,
            )
        with pytest.raises(ValueError, match="dimensions"):
            compute_palette_metrics(
                other,
                quantized,
                config_sha256=CONFIG_HASH,
                width_mm=20,
                height_mm=20,
            )
    finally:
        image.close()
        other.close()
