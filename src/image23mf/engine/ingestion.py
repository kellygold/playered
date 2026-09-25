"""Bounded, signature-based raster ingestion and deterministic sRGB normalization."""

import hashlib
import io
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

INGESTION_SCHEMA_VERSION = 1
EXIF_ORIENTATION_TAG = 274
DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_PIXELS = 50_000_000
DEFAULT_MAX_DIMENSION = 32_768
DEFAULT_MAX_ICC_BYTES = 4 * 1024 * 1024


class IngestionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RasterFormat(str, Enum):
    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"

    @property
    def pillow_name(self) -> str:
        return self.value.upper()

    @property
    def media_type(self) -> str:
        return "image/jpeg" if self == RasterFormat.JPEG else f"image/{self.value}"


class ImageIngestionErrorCode(str, Enum):
    EMPTY_INPUT = "empty_input"
    INPUT_TOO_LARGE = "input_too_large"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CORRUPT_IMAGE = "corrupt_image"
    DIMENSIONS_TOO_LARGE = "dimensions_too_large"
    PIXEL_LIMIT_EXCEEDED = "pixel_limit_exceeded"
    ANIMATION_UNSUPPORTED = "animation_unsupported"
    COLOR_PROFILE_TOO_LARGE = "color_profile_too_large"
    COLOR_PROFILE_INVALID = "color_profile_invalid"
    DECODE_RESOURCE_EXHAUSTED = "decode_resource_exhausted"


class ImageIngestionError(ValueError):
    def __init__(
        self,
        code: ImageIngestionErrorCode,
        message: str,
        *,
        suggestion: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.details = details or {}


class ImageIngestionLimits(IngestionModel):
    max_input_bytes: int = Field(default=DEFAULT_MAX_INPUT_BYTES, ge=1, le=1024**3)
    max_pixels: int = Field(default=DEFAULT_MAX_PIXELS, ge=1, le=250_000_000)
    max_width_px: int = Field(default=DEFAULT_MAX_DIMENSION, ge=1, le=100_000)
    max_height_px: int = Field(default=DEFAULT_MAX_DIMENSION, ge=1, le=100_000)
    max_icc_profile_bytes: int = Field(default=DEFAULT_MAX_ICC_BYTES, ge=1, le=64 * 1024 * 1024)


class SourceImageMetadata(IngestionModel):
    schema_version: int = INGESTION_SCHEMA_VERSION
    original_filename: str = Field(min_length=1, max_length=255)
    declared_extension: Optional[str] = None
    detected_format: RasterFormat
    source_media_type: str
    source_byte_size: int = Field(gt=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_width_px: int = Field(gt=0)
    source_height_px: int = Field(gt=0)
    source_mode: str
    frame_count: int = Field(ge=1)
    exif_orientation: int = Field(ge=1, le=8)
    orientation_applied: bool
    embedded_icc_profile: bool
    source_icc_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    color_conversion: str
    source_had_alpha: bool
    source_had_transparency: bool
    normalized_width_px: int = Field(gt=0)
    normalized_height_px: int = Field(gt=0)
    normalized_mode: Literal["RGBA"]
    normalized_color_space: Literal["srgb"]
    normalized_media_type: Literal["image/png"]
    normalized_byte_size: int = Field(gt=0)
    normalized_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class NormalizedImage:
    png_bytes: bytes
    metadata: SourceImageMetadata


def ingest_image(
    payload: bytes,
    *,
    filename: str = "image",
    limits: Optional[ImageIngestionLimits] = None,
) -> NormalizedImage:
    """Validate and normalize one still PNG/JPEG/WebP without trusting its filename."""
    resolved_limits = limits or ImageIngestionLimits()
    if not payload:
        raise ImageIngestionError(
            ImageIngestionErrorCode.EMPTY_INPUT,
            "The selected file is empty.",
            suggestion="Choose a non-empty PNG, JPEG, or WebP image.",
        )
    if len(payload) > resolved_limits.max_input_bytes:
        raise ImageIngestionError(
            ImageIngestionErrorCode.INPUT_TOO_LARGE,
            "The image file exceeds the configured byte limit.",
            suggestion="Export a smaller image or raise the local import limit intentionally.",
            details={
                "actual_bytes": len(payload),
                "maximum_bytes": resolved_limits.max_input_bytes,
            },
        )

    detected_format = _detect_format(payload)
    try:
        _verify_encoded_image(payload, detected_format, resolved_limits)
        return _decode_and_normalize(
            payload,
            filename=_safe_filename(filename),
            detected_format=detected_format,
            limits=resolved_limits,
        )
    except ImageIngestionError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise _resource_error(str(error), resolved_limits) from error
    except MemoryError as error:
        raise ImageIngestionError(
            ImageIngestionErrorCode.DECODE_RESOURCE_EXHAUSTED,
            "The image could not be decoded within available memory.",
            suggestion="Reduce the image dimensions before importing it.",
        ) from error
    except (OSError, SyntaxError, ValueError) as error:
        raise _corrupt_error(error) from error


def _decode_and_normalize(
    payload: bytes,
    *,
    filename: str,
    detected_format: RasterFormat,
    limits: ImageIngestionLimits,
) -> NormalizedImage:
    with _open_image(payload, detected_format) as source:
        _validate_header(source, detected_format, limits)
        source_width, source_height = source.size
        source_mode = source.mode
        frame_count = int(getattr(source, "n_frames", 1))
        orientation = _exif_orientation(source)
        icc_profile = _icc_profile(source, limits)
        source_had_alpha = _has_alpha(source)
        source.load()
        oriented = ImageOps.exif_transpose(source)
        try:
            source_had_transparency = _has_transparency(oriented)
            normalized, conversion = _convert_to_srgb_rgba(oriented, icc_profile)
        finally:
            if oriented is not source:
                oriented.close()

    try:
        normalized_bytes = _encode_normalized_png(normalized)
        normalized_width, normalized_height = normalized.size
    finally:
        normalized.close()
    normalized_sha = _sha256(normalized_bytes)
    source_sha = _sha256(payload)
    source_icc_sha = _sha256(icc_profile) if icc_profile else None
    return NormalizedImage(
        png_bytes=normalized_bytes,
        metadata=SourceImageMetadata(
            original_filename=filename,
            declared_extension=_extension(filename),
            detected_format=detected_format,
            source_media_type=detected_format.media_type,
            source_byte_size=len(payload),
            source_sha256=source_sha,
            source_width_px=source_width,
            source_height_px=source_height,
            source_mode=source_mode,
            frame_count=frame_count,
            exif_orientation=orientation,
            orientation_applied=orientation != 1,
            embedded_icc_profile=icc_profile is not None,
            source_icc_sha256=source_icc_sha,
            color_conversion=conversion,
            source_had_alpha=source_had_alpha,
            source_had_transparency=source_had_transparency,
            normalized_width_px=normalized_width,
            normalized_height_px=normalized_height,
            normalized_mode="RGBA",
            normalized_color_space="srgb",
            normalized_media_type="image/png",
            normalized_byte_size=len(normalized_bytes),
            normalized_sha256=normalized_sha,
        ),
    )


def _detect_format(payload: bytes) -> RasterFormat:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return RasterFormat.PNG
    if len(payload) >= 3 and payload[:3] == b"\xff\xd8\xff":
        return RasterFormat.JPEG
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return RasterFormat.WEBP
    raise ImageIngestionError(
        ImageIngestionErrorCode.UNSUPPORTED_FORMAT,
        "The file signature is not a supported image format.",
        suggestion="Choose a PNG, JPEG, or WebP file; renaming an extension does not convert it.",
        details={"supported_formats": [item.value for item in RasterFormat]},
    )


def _verify_encoded_image(
    payload: bytes, detected_format: RasterFormat, limits: ImageIngestionLimits
) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with _open_image(payload, detected_format) as image:
                _validate_header(image, detected_format, limits)
                image.verify()
    except ImageIngestionError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ImageIngestionError(
            ImageIngestionErrorCode.PIXEL_LIMIT_EXCEEDED,
            "The image exceeds Pillow's safe decompression limit.",
            suggestion="Reduce the source dimensions before importing it.",
            details={"decoder_message": str(error)},
        ) from error
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise _corrupt_error(error) from error


def _open_image(payload: bytes, detected_format: RasterFormat) -> Image.Image:
    try:
        return Image.open(io.BytesIO(payload), formats=[detected_format.pillow_name])
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        raise _corrupt_error(error) from error


def _validate_header(
    image: Image.Image, detected_format: RasterFormat, limits: ImageIngestionLimits
) -> None:
    decoded_format = (image.format or "").lower()
    decoded_format = {"jpg": "jpeg"}.get(decoded_format, decoded_format)
    if decoded_format != detected_format.value:
        raise ImageIngestionError(
            ImageIngestionErrorCode.CORRUPT_IMAGE,
            "The decoder format does not match the file signature.",
            suggestion="Re-export the original image as PNG, JPEG, or WebP.",
            details={"signature": detected_format.value, "decoder": decoded_format or None},
        )
    width, height = image.size
    if width <= 0 or height <= 0:
        raise _corrupt_error(ValueError("image dimensions are not positive"))
    if width > limits.max_width_px or height > limits.max_height_px:
        raise ImageIngestionError(
            ImageIngestionErrorCode.DIMENSIONS_TOO_LARGE,
            "The image width or height exceeds the configured dimension limit.",
            suggestion="Resize the source image before importing it.",
            details={
                "actual_width_px": width,
                "actual_height_px": height,
                "maximum_width_px": limits.max_width_px,
                "maximum_height_px": limits.max_height_px,
            },
        )
    pixels = width * height
    if pixels > limits.max_pixels:
        raise ImageIngestionError(
            ImageIngestionErrorCode.PIXEL_LIMIT_EXCEEDED,
            "The decoded image exceeds the configured pixel limit.",
            suggestion="Resize or crop the source image before importing it.",
            details={"actual_pixels": pixels, "maximum_pixels": limits.max_pixels},
        )
    frame_count = int(getattr(image, "n_frames", 1))
    if frame_count != 1 or bool(getattr(image, "is_animated", False)):
        raise ImageIngestionError(
            ImageIngestionErrorCode.ANIMATION_UNSUPPORTED,
            "Animated or multi-frame images are not supported.",
            suggestion="Export the intended frame as a still PNG, JPEG, or WebP image.",
            details={"frame_count": frame_count},
        )


def _icc_profile(image: Image.Image, limits: ImageIngestionLimits) -> Optional[bytes]:
    profile = image.info.get("icc_profile")
    if profile is None:
        return None
    if not isinstance(profile, bytes):
        raise ImageIngestionError(
            ImageIngestionErrorCode.COLOR_PROFILE_INVALID,
            "The embedded color profile is malformed.",
            suggestion="Re-export the image with a valid ICC profile or standard sRGB colors.",
        )
    if len(profile) > limits.max_icc_profile_bytes:
        raise ImageIngestionError(
            ImageIngestionErrorCode.COLOR_PROFILE_TOO_LARGE,
            "The embedded color profile exceeds the configured size limit.",
            suggestion="Remove the oversized profile or re-export the image as sRGB.",
            details={
                "actual_bytes": len(profile),
                "maximum_bytes": limits.max_icc_profile_bytes,
            },
        )
    return profile


def _convert_to_srgb_rgba(
    image: Image.Image, icc_profile: Optional[bytes]
) -> tuple[Image.Image, str]:
    alpha = _alpha_channel(image)
    try:
        if icc_profile is None:
            rgb = image.convert("RGB")
            if image.mode in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                conversion = "assumed-srgb"
            else:
                conversion = f"pillow-{image.mode.lower()}-to-srgb-without-profile"
        else:
            try:
                source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                color_source = (
                    image if image.mode in {"RGB", "CMYK", "LAB"} else image.convert("RGB")
                )
                try:
                    rgb = ImageCms.profileToProfile(
                        color_source,
                        source_profile,
                        _srgb_profile(),
                        outputMode="RGB",
                    )
                finally:
                    if color_source is not image:
                        color_source.close()
            except (ImageCms.PyCMSError, OSError, TypeError, ValueError) as error:
                raise ImageIngestionError(
                    ImageIngestionErrorCode.COLOR_PROFILE_INVALID,
                    "The embedded color profile could not be converted to sRGB.",
                    suggestion=(
                        "Re-export the image with a valid ICC profile or standard sRGB colors."
                    ),
                    details={"profile_error": str(error)[:300]},
                ) from error
            conversion = "embedded-icc-to-srgb"
    except Exception:
        alpha.close()
        raise
    try:
        red, green, blue = rgb.split()
        normalized = Image.merge("RGBA", (red, green, blue, alpha))
    finally:
        rgb.close()
        alpha.close()
    return normalized, conversion


def _alpha_channel(image: Image.Image) -> Image.Image:
    if _has_alpha(image) or "transparency" in image.info:
        rgba = image.convert("RGBA")
        try:
            return rgba.getchannel("A")
        finally:
            rgba.close()
    return Image.new("L", image.size, 255)


def _has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _has_transparency(image: Image.Image) -> bool:
    if not _has_alpha(image):
        return False
    alpha = _alpha_channel(image)
    try:
        minimum, _ = alpha.getextrema()
        return minimum < 255
    finally:
        alpha.close()


def _encode_normalized_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=6, optimize=False)
    return output.getvalue()


def _exif_orientation(image: Image.Image) -> int:
    try:
        value = int(image.getexif().get(EXIF_ORIENTATION_TAG, 1))
    except (TypeError, ValueError):
        return 1
    return value if 1 <= value <= 8 else 1


def _safe_filename(filename: str) -> str:
    portable = filename.strip().replace("\\", "/")
    name = Path(portable).name[:255]
    return name or "image"


def _extension(filename: str) -> Optional[str]:
    suffix = Path(filename).suffix.lower()
    return suffix or None


def _corrupt_error(error: Exception) -> ImageIngestionError:
    return ImageIngestionError(
        ImageIngestionErrorCode.CORRUPT_IMAGE,
        "The image is corrupt or incomplete and could not be decoded.",
        suggestion="Re-export or download the source again, then retry the import.",
        details={"decoder_message": str(error)[:300]},
    )


def _resource_error(message: str, limits: ImageIngestionLimits) -> ImageIngestionError:
    return ImageIngestionError(
        ImageIngestionErrorCode.PIXEL_LIMIT_EXCEEDED,
        "The image exceeds a safe decompression limit.",
        suggestion="Reduce the image dimensions before importing it.",
        details={"decoder_message": message, "configured_max_pixels": limits.max_pixels},
    )


def _srgb_profile() -> ImageCms.ImageCmsProfile:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
