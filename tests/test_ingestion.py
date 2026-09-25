import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image, ImageCms
from pydantic import ValidationError

from image23mf.engine import (
    ImageIngestionError,
    ImageIngestionErrorCode,
    ImageIngestionLimits,
    RasterFormat,
    SourceImageMetadata,
    ingest_image,
)

ROOT = Path(__file__).resolve().parents[1]


def encoded(image: Image.Image, image_format: str, **options: object) -> bytes:
    output = io.BytesIO()
    image.save(output, format=image_format, **options)
    return output.getvalue()


def decoded_normalized(payload: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(payload))
    image.load()
    return image


@pytest.mark.parametrize(
    ("image_format", "detected", "mode"),
    [
        ("PNG", RasterFormat.PNG, "RGBA"),
        ("JPEG", RasterFormat.JPEG, "RGB"),
        ("WEBP", RasterFormat.WEBP, "RGBA"),
    ],
)
def test_valid_formats_normalize_deterministically_with_complete_metadata(
    image_format: str, detected: RasterFormat, mode: str
) -> None:
    source = Image.new(mode, (17, 11), (20, 80, 140, 255) if mode == "RGBA" else (20, 80, 140))
    payload = (
        encoded(source, image_format, lossless=True)
        if image_format == "WEBP"
        else encoded(source, image_format)
    )

    first = ingest_image(payload, filename="C:\\private\\art.WRONG")
    second = ingest_image(payload, filename="C:\\private\\art.WRONG")

    assert first == second
    assert first.metadata.original_filename == "art.WRONG"
    assert first.metadata.declared_extension == ".wrong"
    assert first.metadata.detected_format == detected
    assert first.metadata.source_media_type == detected.media_type
    assert first.metadata.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert first.metadata.normalized_sha256 == hashlib.sha256(first.png_bytes).hexdigest()
    assert first.metadata.normalized_mode == "RGBA"
    assert first.metadata.normalized_color_space == "srgb"
    assert first.metadata.normalized_media_type == "image/png"
    assert (
        SourceImageMetadata.model_validate_json(first.metadata.model_dump_json()) == first.metadata
    )
    with decoded_normalized(first.png_bytes) as normalized:
        assert normalized.mode == "RGBA"
        assert normalized.size == (17, 11)
        assert "icc_profile" not in normalized.info
        assert normalized.getchannel("A").getextrema() == (255, 255)


def test_signature_not_extension_selects_decoder_for_synthetic_detail_fixture() -> None:
    path = ROOT / "tests/fixtures/public-regression/signature-detail.png"
    result = ingest_image(path.read_bytes(), filename=path.name)

    assert result.metadata.declared_extension == ".png"
    assert result.metadata.detected_format == RasterFormat.JPEG
    assert result.metadata.source_mode == "RGB"
    assert (result.metadata.normalized_width_px, result.metadata.normalized_height_px) == (
        1408,
        768,
    )


def test_exif_orientation_is_applied_and_removed_from_normalized_pixels() -> None:
    source = Image.new("RGB", (9, 5), (240, 20, 10))
    exif = Image.Exif()
    exif[274] = 6
    result = ingest_image(encoded(source, "JPEG", exif=exif), filename="rotated.jpg")

    assert result.metadata.exif_orientation == 6
    assert result.metadata.orientation_applied
    assert (result.metadata.source_width_px, result.metadata.source_height_px) == (9, 5)
    assert (result.metadata.normalized_width_px, result.metadata.normalized_height_px) == (5, 9)
    with decoded_normalized(result.png_bytes) as normalized:
        assert normalized.size == (5, 9)
        assert normalized.getexif().get(274) is None


def test_transparency_is_preserved_and_distinguished_from_opaque_alpha() -> None:
    transparent = Image.new("RGBA", (4, 3), (10, 20, 30, 255))
    transparent.putpixel((1, 1), (100, 110, 120, 0))
    result = ingest_image(encoded(transparent, "PNG"), filename="alpha.png")

    assert result.metadata.source_had_alpha
    assert result.metadata.source_had_transparency
    with decoded_normalized(result.png_bytes) as normalized:
        assert normalized.getpixel((1, 1))[3] == 0
        assert normalized.getpixel((0, 0))[3] == 255

    opaque_alpha = ingest_image(
        encoded(Image.new("RGBA", (2, 2), (1, 2, 3, 255)), "PNG"), filename="opaque.png"
    )
    assert opaque_alpha.metadata.source_had_alpha
    assert not opaque_alpha.metadata.source_had_transparency


def test_embedded_profile_and_unprofiled_cmyk_are_recorded_and_normalized() -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    profiled = ingest_image(
        encoded(Image.new("RGB", (3, 2), (22, 91, 173)), "PNG", icc_profile=profile),
        filename="profiled.png",
    )
    assert profiled.metadata.embedded_icc_profile
    assert profiled.metadata.source_icc_sha256 == hashlib.sha256(profile).hexdigest()
    assert profiled.metadata.color_conversion == "embedded-icc-to-srgb"

    cmyk = ingest_image(
        encoded(Image.new("CMYK", (3, 2), (10, 80, 120, 0)), "JPEG"),
        filename="press.jpg",
    )
    assert cmyk.metadata.source_mode == "CMYK"
    assert not cmyk.metadata.embedded_icc_profile
    assert cmyk.metadata.color_conversion == "pillow-cmyk-to-srgb-without-profile"
    with decoded_normalized(cmyk.png_bytes) as normalized:
        assert normalized.mode == "RGBA"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", ImageIngestionErrorCode.EMPTY_INPUT),
        (b"GIF89a" + b"x" * 20, ImageIngestionErrorCode.UNSUPPORTED_FORMAT),
        (b"\x89PNG\r\n\x1a\nnot-a-png", ImageIngestionErrorCode.CORRUPT_IMAGE),
    ],
)
def test_empty_unsupported_and_corrupt_inputs_have_actionable_errors(
    payload: bytes, expected: ImageIngestionErrorCode
) -> None:
    with pytest.raises(ImageIngestionError) as raised:
        ingest_image(payload, filename="broken.png")

    assert raised.value.code == expected
    assert raised.value.message
    assert raised.value.suggestion


def test_truncated_stream_fails_full_verification() -> None:
    valid = encoded(Image.new("RGB", (80, 60), (1, 2, 3)), "PNG")
    with pytest.raises(ImageIngestionError) as raised:
        ingest_image(valid[:-12], filename="truncated.png")
    assert raised.value.code == ImageIngestionErrorCode.CORRUPT_IMAGE


def test_byte_dimension_and_pixel_limits_fail_before_unbounded_decode() -> None:
    payload = encoded(Image.new("RGB", (11, 9), (1, 2, 3)), "PNG")

    with pytest.raises(ImageIngestionError) as byte_error:
        ingest_image(
            payload,
            limits=ImageIngestionLimits(max_input_bytes=len(payload) - 1),
        )
    assert byte_error.value.code == ImageIngestionErrorCode.INPUT_TOO_LARGE
    assert byte_error.value.details["actual_bytes"] == len(payload)

    with pytest.raises(ImageIngestionError) as dimension_error:
        ingest_image(payload, limits=ImageIngestionLimits(max_width_px=10))
    assert dimension_error.value.code == ImageIngestionErrorCode.DIMENSIONS_TOO_LARGE
    assert dimension_error.value.details["actual_width_px"] == 11

    with pytest.raises(ImageIngestionError) as pixel_error:
        ingest_image(payload, limits=ImageIngestionLimits(max_pixels=98))
    assert pixel_error.value.code == ImageIngestionErrorCode.PIXEL_LIMIT_EXCEEDED
    assert pixel_error.value.details == {"actual_pixels": 99, "maximum_pixels": 98}

    with pytest.raises(ImageIngestionError) as precedence_error:
        ingest_image(payload[:-12], limits=ImageIngestionLimits(max_pixels=98))
    assert precedence_error.value.code == ImageIngestionErrorCode.PIXEL_LIMIT_EXCEEDED


def test_animation_is_rejected_with_frame_count() -> None:
    first = Image.new("RGBA", (3, 3), (255, 0, 0, 255))
    second = Image.new("RGBA", (3, 3), (0, 0, 255, 255))
    payload = encoded(first, "PNG", save_all=True, append_images=[second], duration=100, loop=0)

    with pytest.raises(ImageIngestionError) as raised:
        ingest_image(payload, filename="animated.png")
    assert raised.value.code == ImageIngestionErrorCode.ANIMATION_UNSUPPORTED
    assert raised.value.details["frame_count"] == 2


def test_invalid_and_oversized_icc_profiles_fail_explicitly() -> None:
    source = Image.new("RGB", (3, 2), (10, 20, 30))
    invalid_payload = encoded(source, "PNG", icc_profile=b"not-an-icc-profile")
    with pytest.raises(ImageIngestionError) as invalid:
        ingest_image(invalid_payload)
    assert invalid.value.code == ImageIngestionErrorCode.COLOR_PROFILE_INVALID

    large_profile = b"x" * 1024
    oversized_payload = encoded(source, "PNG", icc_profile=large_profile)
    with pytest.raises(ImageIngestionError) as oversized:
        ingest_image(
            oversized_payload,
            limits=ImageIngestionLimits(max_icc_profile_bytes=100),
        )
    assert oversized.value.code == ImageIngestionErrorCode.COLOR_PROFILE_TOO_LARGE
    assert oversized.value.details == {"actual_bytes": 1024, "maximum_bytes": 100}


def test_limit_contract_is_frozen_and_rejects_unknown_or_unbounded_values() -> None:
    with pytest.raises(ValidationError):
        ImageIngestionLimits(max_pixels=0)
    with pytest.raises(ValidationError):
        ImageIngestionLimits(unknown_limit=1)
    with pytest.raises(ValidationError):
        ImageIngestionLimits().max_pixels = 1
