"""Deterministic normalization and changed-area evidence for prompted alternatives."""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps


@dataclass(frozen=True)
class PromptedAlternativeEvidence:
    """Canonical output plus an exact row-major binary difference plane."""

    normalized_png: bytes
    changed_mask: bytes
    changed_mask_preview_png: bytes
    changed_pixel_count: int
    width_px: int
    height_px: int


def build_prompted_alternative_evidence(
    parent_bytes: bytes,
    output_bytes: bytes,
) -> PromptedAlternativeEvidence:
    """Normalize two images and compare their visible RGBA pixels exactly.

    Provider metadata, compression choices, and color profiles do not count as edits. Both
    inputs are EXIF-normalized and converted to sRGB-like RGBA pixels before comparison.
    Alternatives must preserve the parent canvas dimensions; resizing provider output would
    make the selected-area evidence ambiguous.
    """

    parent = _rgba_image(parent_bytes)
    output = _rgba_image(output_bytes)
    if output.size != parent.size:
        raise ValueError("prompted-edit output dimensions must match the immutable parent source")
    parent_pixels = np.asarray(parent, dtype=np.uint8)
    output_pixels = np.asarray(output, dtype=np.uint8)
    changed = np.any(parent_pixels != output_pixels, axis=2)
    mask = changed.astype(np.uint8).tobytes()

    preview = np.zeros((*changed.shape, 4), dtype=np.uint8)
    preview[changed] = (255, 92, 72, 210)
    return PromptedAlternativeEvidence(
        normalized_png=_png_bytes(output),
        changed_mask=mask,
        changed_mask_preview_png=_png_bytes(Image.fromarray(preview)),
        changed_pixel_count=int(np.count_nonzero(changed)),
        width_px=output.width,
        height_px=output.height,
    )


def _rgba_image(data: bytes) -> Image.Image:
    if not data:
        raise ValueError("prompted-edit image bytes cannot be empty")
    try:
        with Image.open(io.BytesIO(data)) as source:
            normalized = ImageOps.exif_transpose(source)
            return normalized.convert("RGBA")
    except (OSError, ValueError) as error:
        raise ValueError("prompted-edit image could not be decoded safely") from error


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
