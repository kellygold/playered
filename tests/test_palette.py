import hashlib
import time

import numpy as np
import pytest
from PIL import Image

from image23mf.engine import (
    PaletteErrorCode,
    PaletteQuantizationError,
    QuantizationOptions,
    classify_palette,
    delta_e_76,
    fit_auto_palette,
    lab_to_rgb,
    nearest_palette_index,
    quantize_auto_palette,
    rgb_to_lab,
)


def gradient_image(width: int = 512, height: int = 128) -> Image.Image:
    values = np.linspace(0, 255, width, dtype=np.uint8)
    rgb = np.repeat(values[None, :, None], height, axis=0)
    rgb = np.repeat(rgb, 3, axis=2)
    return Image.fromarray(rgb)


def test_standard_d65_lab_references_and_round_trip() -> None:
    assert rgb_to_lab((0, 0, 0)) == pytest.approx((0, 0, 0), abs=0.001)
    assert rgb_to_lab((255, 255, 255)) == pytest.approx((100, 0, 0), abs=0.01)
    assert rgb_to_lab((255, 0, 0)) == pytest.approx((53.2408, 80.0925, 67.2032), abs=0.01)
    assert lab_to_rgb(rgb_to_lab((20, 80, 140))) == (20, 80, 140)
    assert delta_e_76((50, 10, -20), (50, 13, -16)) == pytest.approx(5)


def test_known_nearest_color_uses_lab_and_stable_palette_order() -> None:
    # Mid-sRGB gray has L* > 50 and is perceptually closer to white than black.
    assert nearest_palette_index("#808080", ("#000000", "#FFFFFF")) == 1
    assert nearest_palette_index("#102030", ("#102030", "#102030", "#FFFFFF")) == 0


def test_auto_palette_is_reproducible_and_classifies_every_gradient_pixel() -> None:
    image = gradient_image()
    try:
        first = quantize_auto_palette(image, 4)
        second = quantize_auto_palette(image, 4)
    finally:
        image.close()

    assert first == second
    assert first.fit is not None and first.fit.converged
    assert first.fit.sample_size == 512 * 128
    assert first.labels.width == 512
    assert first.labels.height == 128
    assert first.labels.label_values == (0, 1, 2, 3)
    assert set(first.labels.pixels) == {0, 1, 2, 3}
    assert len(first.fingerprint()) == 64
    assert first.fingerprint() == second.fingerprint()


def test_flat_input_reports_deterministic_palette_collapse_without_invalid_labels() -> None:
    image = Image.new("RGB", (64, 64), "#456789")
    try:
        result = quantize_auto_palette(image, 4)
    finally:
        image.close()

    assert result.palette == ("#456789",) * 4
    assert result.unique_palette_count == 1
    assert result.effective_label_count == 1
    assert set(result.labels.pixels) == {0}


def test_locked_positions_stay_exact_while_unlocked_colors_fit() -> None:
    image = gradient_image(256, 32)
    try:
        fit = fit_auto_palette(
            image,
            4,
            locked_colors={1: "#e75b12", 3: "#0a3a78"},
        )
    finally:
        image.close()

    assert fit.colors[1] == "#E75B12"
    assert fit.colors[3] == "#0A3A78"
    assert len(fit.colors) == 4


def test_transparent_hidden_rgb_does_not_change_fit_and_alpha_round_trips() -> None:
    first = Image.new("RGBA", (4, 1), (0, 255, 0, 0))
    second = Image.new("RGBA", (4, 1), (0, 0, 255, 0))
    for image in (first, second):
        image.putpixel((0, 0), (255, 0, 0, 255))
        image.putpixel((1, 0), (255, 255, 255, 128))
    try:
        first_result = quantize_auto_palette(first, 2)
        second_result = quantize_auto_palette(second, 2)
        rendered = first_result.to_image()
        try:
            assert rendered.getchannel("A").tobytes() == first.getchannel("A").tobytes()
        finally:
            rendered.close()
    finally:
        first.close()
        second.close()

    assert first_result.palette == second_result.palette
    assert first_result.labels.pixels == second_result.labels.pixels
    assert first_result.labels.pixels[2:] == b"\x00\x00"
    assert first_result.png_bytes() == first_result.png_bytes()
    assert hashlib.sha256(first_result.png_bytes()).hexdigest()


def test_saved_palette_classification_is_chunk_size_independent_and_ties_choose_first() -> None:
    pixels = np.zeros((57, 83, 4), dtype=np.uint8)
    pixels[:, :, :3] = np.random.default_rng(20260716).integers(
        0, 256, size=(57, 83, 3), dtype=np.uint8
    )
    pixels[:, :, 3] = 255
    image = Image.fromarray(pixels)
    palette = ("#202428", "#202428", "#F2D6AA", "#E75B12")
    try:
        small = classify_palette(
            image,
            palette,
            options=QuantizationOptions(sample_pixels=256, chunk_pixels=256),
        )
        large = classify_palette(
            image,
            palette,
            options=QuantizationOptions(sample_pixels=256, chunk_pixels=8_192),
        )
    finally:
        image.close()

    assert small.palette == large.palette
    assert small.labels.pixels == large.labels.pixels
    assert 1 not in set(small.labels.pixels)


def test_seeded_noise_fit_is_reproducible_bounded_and_fast() -> None:
    pixels = np.random.default_rng(24).integers(0, 256, size=(768, 1024, 3), dtype=np.uint8)
    image = Image.fromarray(pixels)
    options = QuantizationOptions(sample_pixels=16_384, chunk_pixels=32_768, max_iterations=16)
    started = time.perf_counter()
    try:
        first = quantize_auto_palette(image, 8, options=options)
        second = quantize_auto_palette(image, 8, options=options)
    finally:
        image.close()
    elapsed = time.perf_counter() - started

    assert first == second
    assert first.fit is not None
    assert first.fit.sample_size == options.sample_pixels
    assert first.fit.visible_pixel_count == 768 * 1024
    assert len(first.labels.pixels) == 768 * 1024
    assert elapsed < 5


@pytest.mark.parametrize("count", [0, 1, 9, True, 4.0])
def test_invalid_auto_palette_counts_fail_with_stable_code(count) -> None:
    image = Image.new("RGB", (2, 2), "white")
    try:
        with pytest.raises(PaletteQuantizationError) as raised:
            fit_auto_palette(image, count)
    finally:
        image.close()
    assert raised.value.code == PaletteErrorCode.INVALID_COLOR_COUNT


def test_invalid_color_lock_and_empty_transparency_have_actionable_codes() -> None:
    image = Image.new("RGBA", (2, 2), (10, 20, 30, 0))
    try:
        with pytest.raises(PaletteQuantizationError) as color:
            classify_palette(image, ("#000000", "nope"))
        with pytest.raises(PaletteQuantizationError) as lock:
            fit_auto_palette(image, 2, locked_colors={2: "#FFFFFF"})
        with pytest.raises(PaletteQuantizationError) as empty:
            fit_auto_palette(image, 2)
    finally:
        image.close()

    assert color.value.code == PaletteErrorCode.INVALID_COLOR
    assert lock.value.code == PaletteErrorCode.INVALID_LOCK
    assert empty.value.code == PaletteErrorCode.NO_VISIBLE_PIXELS


def test_quantization_option_contract_is_versionable_and_validated() -> None:
    first = QuantizationOptions()
    second = QuantizationOptions()
    assert first.fingerprint() == second.fingerprint()
    assert len(first.fingerprint()) == 64
    with pytest.raises(ValueError, match="sample_pixels"):
        QuantizationOptions(sample_pixels=1)
    with pytest.raises(ValueError, match="alpha_threshold"):
        QuantizationOptions(alpha_threshold=255)
