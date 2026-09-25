"""Deterministic raster preview rendering through the canonical coordinate transform."""

import hashlib
import io
from dataclasses import dataclass

from PIL import Image

from image23mf.contracts.job import JobConfig
from image23mf.contracts.processing import (
    AlphaStatistics,
    PhysicalDimensions,
    PixelDimensions,
    PreviewStatistics,
)
from image23mf.engine.ingestion import SourceImageMetadata
from image23mf.engine.transform import (
    CanonicalTransform,
    CoordinateStage,
    MillimetreSize,
    PixelSize,
)

DEFAULT_PREVIEW_MAX_DIMENSION = 1024


@dataclass(frozen=True)
class RenderedPreview:
    png_bytes: bytes
    statistics: PreviewStatistics


def render_preview(
    normalized_png: bytes,
    *,
    source_metadata: SourceImageMetadata,
    config: JobConfig,
    profile_catalog_fingerprint: str,
    max_dimension: int = DEFAULT_PREVIEW_MAX_DIMENSION,
) -> RenderedPreview:
    if max_dimension < 32 or max_dimension > 4096:
        raise ValueError("preview max dimension must be between 32 and 4096 pixels")
    working_size = _working_size(config, max_dimension=max_dimension)
    transform = CanonicalTransform.from_source_metadata(
        source=source_metadata,
        crop=config.crop,
        working_size=working_size,
        canvas_size=MillimetreSize(
            width=config.canvas.width_mm,
            height=config.canvas.height_mm,
        ),
    )
    output_to_source = transform.matrix(CoordinateStage.WORKING, CoordinateStage.NORMALIZED)

    with Image.open(io.BytesIO(normalized_png), formats=["PNG"]) as encoded:
        encoded.load()
        source = encoded.convert("RGBA")
    try:
        preview = source.transform(
            (working_size.width, working_size.height),
            Image.Transform.AFFINE,
            data=(
                output_to_source.m00,
                output_to_source.m01,
                output_to_source.tx,
                output_to_source.m10,
                output_to_source.m11,
                output_to_source.ty,
            ),
            resample=Image.Resampling.BICUBIC,
            fillcolor=(0, 0, 0, 0),
        )
    finally:
        source.close()

    try:
        alpha = _alpha_statistics(preview)
        payload = _encode_png(preview)
    finally:
        preview.close()
    preview_sha = hashlib.sha256(payload).hexdigest()
    return RenderedPreview(
        png_bytes=payload,
        statistics=PreviewStatistics(
            config_sha256=config.fingerprint(),
            profile_catalog_fingerprint=profile_catalog_fingerprint,
            source_asset_id=config.source_asset_id,
            source=PixelDimensions(
                width=source_metadata.normalized_width_px,
                height=source_metadata.normalized_height_px,
            ),
            preview=PixelDimensions(width=working_size.width, height=working_size.height),
            physical=PhysicalDimensions(
                width_mm=config.canvas.width_mm,
                height_mm=config.canvas.height_mm,
                mm_per_pixel_x=config.canvas.width_mm / working_size.width,
                mm_per_pixel_y=config.canvas.height_mm / working_size.height,
            ),
            alpha=alpha,
            transform=transform,
            preview_sha256=preview_sha,
        ),
    )


def _working_size(config: JobConfig, *, max_dimension: int) -> PixelSize:
    width = config.canvas.width_mm
    height = config.canvas.height_mm
    scale = max_dimension / max(width, height)
    return PixelSize(
        width=max(1, round(width * scale)),
        height=max(1, round(height * scale)),
    )


def _alpha_statistics(image: Image.Image) -> AlphaStatistics:
    histogram = image.getchannel("A").histogram()
    return AlphaStatistics(
        transparent_pixels=histogram[0],
        translucent_pixels=sum(histogram[1:255]),
        opaque_pixels=histogram[255],
    )


def _encode_png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
