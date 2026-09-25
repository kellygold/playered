"""Deterministic CIELAB palette fitting and exhaustive raster classification."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

import numpy as np
from PIL import Image

from image23mf.engine.labels import LabelField

_D65_REFERENCE = np.array((0.95047, 1.0, 1.08883), dtype=np.float64)
_RGB_TO_XYZ = np.array(
    (
        (0.4124564, 0.3575761, 0.1804375),
        (0.2126729, 0.7151522, 0.0721750),
        (0.0193339, 0.1191920, 0.9503041),
    ),
    dtype=np.float64,
)
_XYZ_TO_RGB = np.linalg.inv(_RGB_TO_XYZ)
_LAB_EPSILON = 216 / 24389
_LAB_KAPPA = 24389 / 27


class PaletteErrorCode(str, Enum):
    INVALID_COLOR_COUNT = "invalid_color_count"
    INVALID_COLOR = "invalid_color"
    INVALID_LOCK = "invalid_lock"
    NO_VISIBLE_PIXELS = "no_visible_pixels"


class PaletteQuantizationError(ValueError):
    """Stable engine error for invalid or unquantizable palette inputs."""

    def __init__(self, code: PaletteErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QuantizationOptions:
    """Bounded, versionable controls for palette fitting and classification."""

    sample_pixels: int = 65_536
    chunk_pixels: int = 65_536
    max_iterations: int = 32
    convergence_delta_e: float = 0.01
    sampling_seed: int = 23
    alpha_threshold: int = 0

    def __post_init__(self) -> None:
        if self.sample_pixels < 256 or self.sample_pixels > 1_048_576:
            raise ValueError("sample_pixels must be between 256 and 1,048,576")
        if self.chunk_pixels < 256 or self.chunk_pixels > 1_048_576:
            raise ValueError("chunk_pixels must be between 256 and 1,048,576")
        if self.max_iterations < 1 or self.max_iterations > 256:
            raise ValueError("max_iterations must be between 1 and 256")
        if self.convergence_delta_e <= 0 or self.convergence_delta_e > 10:
            raise ValueError("convergence_delta_e must be greater than zero and at most 10")
        if self.sampling_seed < 0 or self.sampling_seed > 2**32 - 1:
            raise ValueError("sampling_seed must fit in an unsigned 32-bit integer")
        if self.alpha_threshold < 0 or self.alpha_threshold > 254:
            raise ValueError("alpha_threshold must be between 0 and 254")

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "alpha_threshold": self.alpha_threshold,
                "chunk_pixels": self.chunk_pixels,
                "convergence_delta_e": self.convergence_delta_e,
                "max_iterations": self.max_iterations,
                "sample_pixels": self.sample_pixels,
                "sampling_seed": self.sampling_seed,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


DEFAULT_QUANTIZATION_OPTIONS = QuantizationOptions()


@dataclass(frozen=True)
class PaletteFit:
    colors: tuple[str, ...]
    iterations: int
    converged: bool
    sample_size: int
    visible_pixel_count: int
    options_fingerprint: str

    @property
    def unique_color_count(self) -> int:
        return len(set(self.colors))


@dataclass(frozen=True)
class QuantizationResult:
    palette: tuple[str, ...]
    labels: LabelField
    alpha: bytes
    options_fingerprint: str
    fit: PaletteFit | None = None

    def __post_init__(self) -> None:
        if len(self.palette) < 2 or len(self.palette) > 8:
            raise ValueError("quantized palettes must contain between two and eight colors")
        normalized = tuple(_normalize_hex(color) for color in self.palette)
        if normalized != self.palette:
            raise ValueError("quantized palette colors must use normalized #RRGGBB notation")
        if self.labels.label_values != tuple(range(len(self.palette))):
            raise ValueError("label values must match ordered palette indices")
        if len(self.alpha) != self.labels.width * self.labels.height:
            raise ValueError("alpha byte count must match label dimensions")
        if len(self.options_fingerprint) != 64:
            raise ValueError("options fingerprint must be a SHA-256 hex digest")

    @property
    def effective_label_count(self) -> int:
        return len(set(self.labels.pixels))

    @property
    def unique_palette_count(self) -> int:
        return len(set(self.palette))

    def to_image(self) -> Image.Image:
        palette = np.array([_hex_to_rgb(color) for color in self.palette], dtype=np.uint8)
        labels = np.frombuffer(self.labels.pixels, dtype=np.uint8)
        rgb = palette[labels].reshape((self.labels.height, self.labels.width, 3))
        alpha = np.frombuffer(self.alpha, dtype=np.uint8).reshape(
            (self.labels.height, self.labels.width, 1)
        )
        rgba = np.concatenate((rgb, alpha), axis=2)
        return Image.fromarray(rgba)

    def png_bytes(self) -> bytes:
        image = self.to_image()
        try:
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=False, compress_level=9)
            return output.getvalue()
        finally:
            image.close()

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(b"quantization-v1\0")
        digest.update(self.options_fingerprint.encode("ascii"))
        for color in self.palette:
            digest.update(color.encode("ascii"))
            digest.update(b"\0")
        digest.update(self.labels.pixels)
        digest.update(self.alpha)
        return digest.hexdigest()


def rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """Convert one 8-bit sRGB color to CIELAB using the D65 reference white."""

    array = np.asarray((_validate_rgb(rgb),), dtype=np.uint8)
    converted = _srgb8_to_lab(array)[0]
    return tuple(float(value) for value in converted)


def lab_to_rgb(lab: tuple[float, float, float]) -> tuple[int, int, int]:
    """Convert one D65 CIELAB color to a clamped 8-bit sRGB color."""

    if len(lab) != 3 or not all(np.isfinite(value) for value in lab):
        raise ValueError("Lab colors require three finite components")
    converted = _lab_to_srgb8(np.asarray((lab,), dtype=np.float64))[0]
    return tuple(int(value) for value in converted)


def delta_e_76(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    """Return Euclidean CIE76 distance between two CIELAB colors."""

    left = np.asarray(first, dtype=np.float64)
    right = np.asarray(second, dtype=np.float64)
    if left.shape != (3,) or right.shape != (3,) or not np.isfinite((left, right)).all():
        raise ValueError("Delta E inputs require three finite Lab components")
    return float(np.linalg.norm(left - right))


def nearest_palette_index(color: str, palette: Sequence[str]) -> int:
    """Classify one sRGB color in Lab space; exact ties keep the earliest palette index."""

    normalized = _normalize_palette(palette)
    target = _srgb8_to_lab(np.asarray((_hex_to_rgb(_normalize_hex(color)),), dtype=np.uint8))
    swatches = _srgb8_to_lab(np.asarray([_hex_to_rgb(item) for item in normalized], dtype=np.uint8))
    distances = np.sum((swatches - target[0]) ** 2, axis=1)
    return int(np.argmin(distances))


def normalize_palette(palette: Sequence[str]) -> tuple[str, ...]:
    """Validate and canonicalize an ordered two-through-eight-color palette."""

    return _normalize_palette(palette)


def srgb8_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert an ``(n, 3)`` uint8 sRGB array to an ``(n, 3)`` D65 Lab array."""

    array = np.asarray(rgb)
    if array.ndim != 2 or array.shape[1] != 3:
        raise ValueError("sRGB pixel arrays must have shape (n, 3)")
    if array.dtype != np.uint8:
        raise ValueError("sRGB pixel arrays must use uint8 components")
    return _srgb8_to_lab(array)


def fit_auto_palette(
    image: Image.Image,
    color_count: int,
    *,
    locked_colors: Mapping[int, str] | None = None,
    options: QuantizationOptions = DEFAULT_QUANTIZATION_OPTIONS,
) -> PaletteFit:
    """Fit an ordered palette with deterministic weighted Lab k-means."""

    _validate_color_count(color_count)
    locked = _normalize_locks(locked_colors or {}, color_count)
    rgba = _rgba_array(image)
    samples, weights, visible_pixel_count = _systematic_visible_sample(rgba, options)
    sample_lab = _srgb8_to_lab(samples[:, :3])
    centroids = _initialize_centroids(sample_lab, weights, color_count, locked)
    locked_indices = frozenset(locked)
    converged = False
    iterations = 0

    for _iterations in range(1, options.max_iterations + 1):
        iterations = _iterations
        assignments = _nearest_centroids(sample_lab, centroids)
        updated = centroids.copy()
        for index in range(color_count):
            if index in locked_indices:
                continue
            members = assignments == index
            if np.any(members):
                updated[index] = np.average(sample_lab[members], axis=0, weights=weights[members])
            else:
                distances = _minimum_squared_distances(sample_lab, updated)
                updated[index] = sample_lab[int(np.argmax(distances * weights))]
        movement = np.sqrt(np.sum((updated - centroids) ** 2, axis=1))
        centroids = updated
        if float(np.max(movement)) <= options.convergence_delta_e:
            converged = True
            break

    rgb = _lab_to_srgb8(centroids)
    colors = ["#{:02X}{:02X}{:02X}".format(*item) for item in rgb.tolist()]
    for index, color in locked.items():
        colors[index] = color
    return PaletteFit(
        colors=tuple(colors),
        iterations=iterations,
        converged=converged,
        sample_size=len(samples),
        visible_pixel_count=visible_pixel_count,
        options_fingerprint=options.fingerprint(),
    )


def classify_palette(
    image: Image.Image,
    palette: Sequence[str],
    *,
    options: QuantizationOptions = DEFAULT_QUANTIZATION_OPTIONS,
) -> QuantizationResult:
    """Classify every pixel against a saved palette using bounded working chunks."""

    normalized = _normalize_palette(palette)
    rgba = _rgba_array(image)
    flat = rgba.reshape((-1, 4))
    swatches = _srgb8_to_lab(
        np.asarray([_hex_to_rgb(color) for color in normalized], dtype=np.uint8)
    )
    labels = np.zeros(len(flat), dtype=np.uint8)
    for start in range(0, len(flat), options.chunk_pixels):
        stop = min(len(flat), start + options.chunk_pixels)
        chunk = flat[start:stop]
        visible = chunk[:, 3] > options.alpha_threshold
        if not np.any(visible):
            continue
        lab = _srgb8_to_lab(chunk[visible, :3])
        labels[start:stop][visible] = _nearest_centroids(lab, swatches).astype(np.uint8)
    field = LabelField(
        width=rgba.shape[1],
        height=rgba.shape[0],
        label_values=tuple(range(len(normalized))),
        pixels=labels.tobytes(),
    )
    return QuantizationResult(
        palette=normalized,
        labels=field,
        alpha=flat[:, 3].tobytes(),
        options_fingerprint=options.fingerprint(),
    )


def quantize_auto_palette(
    image: Image.Image,
    color_count: int,
    *,
    locked_colors: Mapping[int, str] | None = None,
    options: QuantizationOptions = DEFAULT_QUANTIZATION_OPTIONS,
) -> QuantizationResult:
    """Fit and classify using only the normalized saved palette returned by the fit."""

    fit = fit_auto_palette(
        image,
        color_count,
        locked_colors=locked_colors,
        options=options,
    )
    classified = classify_palette(image, fit.colors, options=options)
    return QuantizationResult(
        palette=classified.palette,
        labels=classified.labels,
        alpha=classified.alpha,
        options_fingerprint=classified.options_fingerprint,
        fit=fit,
    )


def _systematic_visible_sample(
    rgba: np.ndarray, options: QuantizationOptions
) -> tuple[np.ndarray, np.ndarray, int]:
    flat = rgba.reshape((-1, 4))
    visible_count = 0
    for start in range(0, len(flat), options.chunk_pixels):
        chunk = flat[start : start + options.chunk_pixels]
        visible_count += int(np.count_nonzero(chunk[:, 3] > options.alpha_threshold))
    if visible_count == 0:
        raise PaletteQuantizationError(
            PaletteErrorCode.NO_VISIBLE_PIXELS,
            "palette fitting requires at least one visible pixel",
        )

    sample_count = min(visible_count, options.sample_pixels)
    step = visible_count / sample_count
    phase = ((options.sampling_seed * 0.6180339887498949) % 1.0) * step
    target_ranks = np.floor(phase + np.arange(sample_count, dtype=np.float64) * step).astype(
        np.int64
    )
    np.minimum(target_ranks, visible_count - 1, out=target_ranks)
    samples = np.empty((sample_count, 4), dtype=np.uint8)
    output_at = 0
    visible_before = 0
    for start in range(0, len(flat), options.chunk_pixels):
        chunk = flat[start : start + options.chunk_pixels]
        visible_pixels = chunk[chunk[:, 3] > options.alpha_threshold]
        visible_after = visible_before + len(visible_pixels)
        until = int(np.searchsorted(target_ranks, visible_after, side="left"))
        if until > output_at:
            local = target_ranks[output_at:until] - visible_before
            samples[output_at:until] = visible_pixels[local]
            output_at = until
        visible_before = visible_after
    if output_at != sample_count:
        raise RuntimeError("systematic palette sampler did not fill its bounded output")
    weights = samples[:, 3].astype(np.float64) / 255.0
    return samples, weights, visible_count


def _initialize_centroids(
    sample_lab: np.ndarray,
    weights: np.ndarray,
    color_count: int,
    locked: Mapping[int, str],
) -> np.ndarray:
    centroids = np.empty((color_count, 3), dtype=np.float64)
    initialized: list[int] = []
    for index, color in locked.items():
        centroids[index] = _srgb8_to_lab(np.asarray((_hex_to_rgb(color),), dtype=np.uint8))[0]
        initialized.append(index)

    available = [index for index in range(color_count) if index not in locked]
    if not initialized and available:
        mean = np.average(sample_lab, axis=0, weights=weights)
        distances = np.sum((sample_lab - mean) ** 2, axis=1)
        selected = int(np.argmax(distances * weights))
        index = available.pop(0)
        centroids[index] = sample_lab[selected]
        initialized.append(index)

    for index in available:
        distances = _minimum_squared_distances(sample_lab, centroids[initialized])
        selected = int(np.argmax(distances * weights))
        centroids[index] = sample_lab[selected]
        initialized.append(index)
    return centroids


def _nearest_centroids(lab: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    distances = np.sum((lab[:, None, :] - centroids[None, :, :]) ** 2, axis=2)
    return np.argmin(distances, axis=1)


def _minimum_squared_distances(lab: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    distances = np.sum((lab[:, None, :] - centroids[None, :, :]) ** 2, axis=2)
    return np.min(distances, axis=1)


def _srgb8_to_lab(rgb: np.ndarray) -> np.ndarray:
    srgb = rgb.astype(np.float64) / 255.0
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    xyz = np.column_stack(
        tuple(
            linear[:, 0] * row[0] + linear[:, 1] * row[1] + linear[:, 2] * row[2]
            for row in _RGB_TO_XYZ
        )
    )
    scaled = xyz / _D65_REFERENCE
    transformed = np.where(
        scaled > _LAB_EPSILON,
        np.cbrt(scaled),
        (_LAB_KAPPA * scaled + 16) / 116,
    )
    return np.column_stack(
        (
            116 * transformed[:, 1] - 16,
            500 * (transformed[:, 0] - transformed[:, 1]),
            200 * (transformed[:, 1] - transformed[:, 2]),
        )
    )


def _lab_to_srgb8(lab: np.ndarray) -> np.ndarray:
    fy = (lab[:, 0] + 16) / 116
    fx = fy + lab[:, 1] / 500
    fz = fy - lab[:, 2] / 200
    transformed = np.column_stack((fx, fy, fz))
    cubed = transformed**3
    scaled = np.where(
        cubed > _LAB_EPSILON,
        cubed,
        (116 * transformed - 16) / _LAB_KAPPA,
    )
    xyz = scaled * _D65_REFERENCE
    linear = np.column_stack(
        tuple(xyz[:, 0] * row[0] + xyz[:, 1] * row[1] + xyz[:, 2] * row[2] for row in _XYZ_TO_RGB)
    )
    linear = np.clip(linear, 0.0, 1.0)
    srgb = np.where(
        linear <= 0.0031308,
        12.92 * linear,
        1.055 * (linear ** (1 / 2.4)) - 0.055,
    )
    return np.rint(np.clip(srgb, 0.0, 1.0) * 255).astype(np.uint8)


def _rgba_array(image: Image.Image) -> np.ndarray:
    converted = image.convert("RGBA")
    try:
        return np.array(converted, dtype=np.uint8, copy=True)
    finally:
        converted.close()


def _normalize_palette(palette: Sequence[str]) -> tuple[str, ...]:
    if len(palette) < 2 or len(palette) > 8:
        raise PaletteQuantizationError(
            PaletteErrorCode.INVALID_COLOR_COUNT,
            "palettes must contain between two and eight colors",
        )
    return tuple(_normalize_hex(color) for color in palette)


def _normalize_locks(locked: Mapping[int, str], color_count: int) -> dict[int, str]:
    normalized = {}
    for index, color in locked.items():
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= color_count
        ):
            raise PaletteQuantizationError(
                PaletteErrorCode.INVALID_LOCK,
                f"locked palette index {index!r} is outside the requested palette",
            )
        normalized[index] = _normalize_hex(color)
    return dict(sorted(normalized.items()))


def _normalize_hex(color: str) -> str:
    if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
        raise PaletteQuantizationError(
            PaletteErrorCode.INVALID_COLOR,
            f"invalid palette color: {color!r}",
        )
    try:
        int(color[1:], 16)
    except ValueError as error:
        raise PaletteQuantizationError(
            PaletteErrorCode.INVALID_COLOR,
            f"invalid palette color: {color!r}",
        ) from error
    return color.upper()


def _validate_color_count(color_count: int) -> None:
    if (
        isinstance(color_count, bool)
        or not isinstance(color_count, int)
        or not 2 <= color_count <= 8
    ):
        raise PaletteQuantizationError(
            PaletteErrorCode.INVALID_COLOR_COUNT,
            "automatic palettes require between two and eight colors",
        )


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))


def _validate_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    if len(rgb) != 3 or any(isinstance(value, bool) or not isinstance(value, int) for value in rgb):
        raise ValueError("sRGB colors require three integer components")
    if any(value < 0 or value > 255 for value in rgb):
        raise ValueError("sRGB components must be between 0 and 255")
    return rgb
