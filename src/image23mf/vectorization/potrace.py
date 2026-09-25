"""Deterministic Potrace adapter from binary masks to physical Geometry IR paths."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

from image23mf.external import (
    CancellationToken,
    ExternalToolRegistry,
    ToolCanceledError,
    ToolId,
    ToolRunner,
    ToolRunResult,
    ToolTimeoutError,
    ToolUnavailableError,
)
from image23mf.geometry import CubicSegment, LineSegment, Path2D, Point2

POTRACE_ADAPTER_VERSION = 2
# Potrace serializes its SVG scale to six decimals. At the 1024 px preview
# resolution this can put an edge a fraction of a micron outside the canvas.
# Bound that correction in physical units; never clip an actual escaped path.
CANVAS_ROUNDING_SLACK_MM = 0.001
MAX_MASK_DIMENSION = 100_000
MAX_MASK_PIXELS = 100_000_000
MAX_SVG_BYTES = 32 * 1024 * 1024
MAX_SVG_ELEMENTS = 1_000_000
MAX_PATH_DATA_CHARACTERS = 64 * 1024 * 1024
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
PATH_TOKEN = re.compile(rf"[MmLlHhVvCcZz]|{NUMBER}")
TRANSFORM = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
POTRACE_SVG_DOCTYPE = re.compile(
    rb'<!DOCTYPE\s+svg\s+PUBLIC\s+"-//W3C//DTD SVG 20010904//EN"\s+'
    rb'"http://www\.w3\.org/TR/2001/REC-SVG-20010904/DTD/svg10\.dtd"\s*>',
    flags=re.IGNORECASE,
)


def _canonical_mm(value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 1_000_000:
        raise ValueError("physical values must be finite and in (0, 1000000] mm")
    normalized = round(number, 6)
    if normalized <= 0:
        raise ValueError("physical values must align to 0.000001 mm precision")
    return normalized


class PotraceParameters(BaseModel):
    """Every user-visible and effective Potrace choice except mask-dependent conversions."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    canvas_width_mm: float
    canvas_height_mm: float
    turd_area_mm2: float = Field(ge=0, le=1_000_000)
    curve_tolerance_mm: float = Field(gt=0, le=1_000)
    corner_threshold: float = Field(default=1.0, ge=0, le=1.334)
    turn_policy: Literal["black", "white", "left", "right", "minority", "majority", "random"] = (
        "minority"
    )
    optimize_curves: bool = True
    nozzle_diameter_mm: Optional[float] = Field(default=None, gt=0, le=10)

    _canvas_width = field_validator("canvas_width_mm")(_canonical_mm)
    _canvas_height = field_validator("canvas_height_mm")(_canonical_mm)
    _curve_tolerance = field_validator("curve_tolerance_mm")(_canonical_mm)

    @field_validator("turd_area_mm2", "corner_threshold", "nozzle_diameter_mm")
    @classmethod
    def canonical_scalar(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Potrace parameters must be finite")
        return round(number, 6)

    @classmethod
    def for_nozzle(
        cls,
        *,
        canvas_width_mm: float,
        canvas_height_mm: float,
        nozzle_diameter_mm: float,
    ) -> PotraceParameters:
        nozzle = _canonical_mm(nozzle_diameter_mm)
        return cls(
            canvas_width_mm=canvas_width_mm,
            canvas_height_mm=canvas_height_mm,
            turd_area_mm2=round(nozzle * nozzle, 6),
            curve_tolerance_mm=round(nozzle / 4, 6),
            nozzle_diameter_mm=nozzle,
        )

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


@dataclass(frozen=True)
class PotraceEvidence:
    executable: Path
    tool_version: str
    command: tuple[str, ...]
    stdout: str
    stderr: str
    duration_seconds: float
    pixel_width_mm: float
    pixel_height_mm: float
    turd_size_pixels: int
    curve_tolerance_pixels: float


@dataclass(frozen=True)
class PotraceResult:
    mask_sha256: str
    cache_key: str
    parameters: PotraceParameters
    normalized_svg: bytes
    normalized_svg_sha256: str
    paths: tuple[Path2D, ...]
    evidence: PotraceEvidence


class PotraceError(RuntimeError):
    def __init__(self, message: str, tool_result: Optional[ToolRunResult] = None) -> None:
        super().__init__(message)
        self.tool_result = tool_result


class PotraceUnavailableError(PotraceError):
    pass


class PotraceExecutionError(PotraceError):
    pass


class PotraceTimeoutError(PotraceError):
    pass


class PotraceCanceledError(PotraceError):
    pass


class PotraceOutputError(PotraceError):
    pass


class PotraceVectorizer:
    def __init__(
        self,
        *,
        runner: Optional[ToolRunner] = None,
        registry: Optional[ExternalToolRegistry] = None,
        timeout_seconds: float = 120,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.runner = runner or ToolRunner()
        self.registry = registry or ExternalToolRegistry(runner=self.runner)
        self.timeout_seconds = timeout_seconds

    def vectorize(
        self,
        mask: np.ndarray,
        parameters: PotraceParameters,
        *,
        cancellation: Optional[CancellationToken] = None,
    ) -> PotraceResult:
        binary = _validate_mask(mask)
        if cancellation is not None and cancellation.canceled:
            raise PotraceCanceledError("Potrace vectorization was canceled before discovery")
        detection = self.registry.detect(ToolId.POTRACE)
        if not detection.available or detection.path is None or detection.version is None:
            raise PotraceUnavailableError(
                detection.unavailable_reason or "Potrace is unavailable or incompatible"
            )
        if cancellation is not None and cancellation.canceled:
            raise PotraceCanceledError("Potrace vectorization was canceled before launch")

        height_px, width_px = binary.shape
        pixel_width_mm = parameters.canvas_width_mm / width_px
        pixel_height_mm = parameters.canvas_height_mm / height_px
        pixel_area_mm2 = pixel_width_mm * pixel_height_mm
        turd_ratio = parameters.turd_area_mm2 / pixel_area_mm2
        turd_size_pixels = max(0, math.floor(turd_ratio + 1e-9))
        curve_tolerance_pixels = parameters.curve_tolerance_mm / max(
            pixel_width_mm, pixel_height_mm
        )
        mask_bytes = _encode_pbm(binary)
        mask_sha256 = hashlib.sha256(mask_bytes).hexdigest()
        cache_key = _cache_key(
            mask_sha256=mask_sha256,
            parameters=parameters,
            tool_version=detection.version,
            width_px=width_px,
            height_px=height_px,
            turd_size_pixels=turd_size_pixels,
            curve_tolerance_pixels=curve_tolerance_pixels,
        )

        with self.runner.temporary_workspace(prefix="potrace-") as workspace:
            source = workspace / f"mask-{mask_sha256[:24]}.pbm"
            output = workspace / f"vector-{cache_key.rsplit(':', 1)[1][:24]}.svg"
            source.write_bytes(mask_bytes)
            arguments = (
                "--svg",
                "--output",
                str(output),
                "--turdsize",
                str(turd_size_pixels),
                "--turnpolicy",
                parameters.turn_policy,
                "--alphamax",
                _number(parameters.corner_threshold),
                "--opttolerance",
                _number(curve_tolerance_pixels),
                "--unit",
                "1",
                "--width",
                f"{_number(parameters.canvas_width_mm)}mm",
                "--height",
                f"{_number(parameters.canvas_height_mm)}mm",
                *(("--longcurve",) if not parameters.optimize_curves else ()),
                str(source),
            )
            try:
                tool_result = self.runner.run(
                    detection.path,
                    arguments,
                    timeout_seconds=self.timeout_seconds,
                    cancellation=cancellation,
                    working_directory=workspace,
                    check=False,
                )
            except ToolCanceledError as error:
                raise PotraceCanceledError(
                    "Potrace vectorization was canceled", error.result
                ) from error
            except ToolTimeoutError as error:
                raise PotraceTimeoutError(
                    f"Potrace vectorization exceeded {self.timeout_seconds:g} seconds",
                    error.result,
                ) from error
            except ToolUnavailableError as error:
                raise PotraceUnavailableError(str(error)) from error
            if tool_result.return_code != 0:
                raise PotraceExecutionError(
                    f"Potrace exited with status {tool_result.return_code}: {tool_result.stderr}",
                    tool_result,
                )
            if output.is_symlink() or not output.is_file():
                raise PotraceOutputError("Potrace produced no SVG artifact", tool_result)
            size = output.stat().st_size
            if size <= 0 or size > MAX_SVG_BYTES:
                raise PotraceOutputError(
                    "Potrace SVG size is empty or exceeds the output limit", tool_result
                )
            try:
                paths = _parse_svg(output.read_bytes(), parameters)
            except PotraceOutputError as error:
                raise PotraceOutputError(str(error), tool_result) from error
            normalized_svg = _normalized_svg(paths, parameters)
            evidence = PotraceEvidence(
                executable=detection.path,
                tool_version=detection.version,
                command=tool_result.command,
                stdout=tool_result.stdout,
                stderr=tool_result.stderr,
                duration_seconds=tool_result.duration_seconds,
                pixel_width_mm=round(pixel_width_mm, 12),
                pixel_height_mm=round(pixel_height_mm, 12),
                turd_size_pixels=turd_size_pixels,
                curve_tolerance_pixels=round(curve_tolerance_pixels, 12),
            )
            return PotraceResult(
                mask_sha256=mask_sha256,
                cache_key=cache_key,
                parameters=parameters,
                normalized_svg=normalized_svg,
                normalized_svg_sha256=hashlib.sha256(normalized_svg).hexdigest(),
                paths=paths,
                evidence=evidence,
            )


def _validate_mask(mask: np.ndarray) -> np.ndarray:
    array = np.asarray(mask)
    if array.ndim != 2 or not array.shape[0] or not array.shape[1]:
        raise ValueError("binary mask must be a non-empty two-dimensional array")
    if max(array.shape) > MAX_MASK_DIMENSION or array.size > MAX_MASK_PIXELS:
        raise ValueError("binary mask exceeds vectorizer limits")
    if array.dtype != np.bool_ and not np.all((array == 0) | (array == 1)):
        raise ValueError("binary mask values must be exactly 0 or 1")
    return np.ascontiguousarray(array, dtype=np.bool_)


def _encode_pbm(mask: np.ndarray) -> bytes:
    packed = np.packbits(mask, axis=1, bitorder="big")
    return f"P4\n{mask.shape[1]} {mask.shape[0]}\n".encode("ascii") + packed.tobytes()


def _cache_key(
    *,
    mask_sha256: str,
    parameters: PotraceParameters,
    tool_version: str,
    width_px: int,
    height_px: int,
    turd_size_pixels: int,
    curve_tolerance_pixels: float,
) -> str:
    payload = {
        "adapter_version": POTRACE_ADAPTER_VERSION,
        "curve_tolerance_pixels": _number(curve_tolerance_pixels),
        "height_px": height_px,
        "mask_sha256": mask_sha256,
        "parameters": json.loads(parameters.canonical_bytes()),
        "tool": "potrace",
        "tool_version": tool_version,
        "turd_size_pixels": turd_size_pixels,
        "width_px": width_px,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"image23mf-potrace-v{POTRACE_ADAPTER_VERSION}:{digest}"


def _parse_svg(payload: bytes, parameters: PotraceParameters) -> tuple[Path2D, ...]:
    if b"<!ENTITY" in payload.upper():
        raise PotraceOutputError("Potrace SVG cannot contain entity declarations")
    doctypes = re.findall(rb"<!DOCTYPE[\s\S]*?>", payload, flags=re.IGNORECASE)
    if any(POTRACE_SVG_DOCTYPE.fullmatch(item) is None for item in doctypes):
        raise PotraceOutputError("Potrace SVG contains an unrecognized DTD")
    payload = POTRACE_SVG_DOCTYPE.sub(b"", payload)
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, RecursionError) as error:
        raise PotraceOutputError("Potrace produced malformed SVG") from error
    if _local_name(root.tag) != "svg":
        raise PotraceOutputError("Potrace output root must be SVG")
    view_box = _numbers(root.attrib.get("viewBox", ""))
    if (
        len(view_box) != 4
        or view_box[2] <= 0
        or view_box[3] <= 0
        or any(abs(value) > 1_000_000_000 for value in view_box)
    ):
        raise PotraceOutputError("Potrace SVG requires a finite positive viewBox")
    _validate_physical_extent(root.attrib.get("width"), parameters.canvas_width_mm, "width")
    _validate_physical_extent(root.attrib.get("height"), parameters.canvas_height_mm, "height")

    paths: list[Path2D] = []
    element_count = 0
    path_characters = 0

    pending = [(root, _identity())]
    while pending:
        element, parent_transform = pending.pop()
        element_count += 1
        if element_count > MAX_SVG_ELEMENTS:
            raise PotraceOutputError("Potrace SVG exceeds the element limit")
        if _namespace(element.tag) != SVG_NAMESPACE:
            raise PotraceOutputError("Potrace SVG elements must use the SVG namespace")
        name = _local_name(element.tag)
        if name not in ("svg", "g", "path", "metadata"):
            raise PotraceOutputError(f"Potrace SVG contains forbidden element {name!r}")
        current = _multiply(parent_transform, _parse_transform(element.attrib.get("transform", "")))
        if name == "path":
            data = element.attrib.get("d")
            if not data:
                raise PotraceOutputError("Potrace SVG path is missing path data")
            path_characters += len(data)
            if path_characters > MAX_PATH_DATA_CHARACTERS:
                raise PotraceOutputError("Potrace SVG path data exceeds the limit")
            paths.extend(_parse_path_data(data, current, view_box, parameters))
        pending.extend((child, current) for child in reversed(element))

    if not paths:
        return ()
    return tuple(sorted(paths, key=lambda item: item.id))


def _parse_path_data(
    data: str,
    transform: tuple[float, ...],
    view_box: list[float],
    parameters: PotraceParameters,
) -> list[Path2D]:
    compact = re.sub(r"[\s,]+", "", data)
    tokens = PATH_TOKEN.findall(data)
    if "".join(tokens) != compact:
        raise PotraceOutputError("Potrace SVG path contains unsupported syntax")
    index = 0
    command: Optional[str] = None
    current = (0.0, 0.0)
    start: Optional[tuple[float, float]] = None
    segments: list[tuple[str, tuple[float, ...]]] = []
    parsed: list[Path2D] = []

    def number() -> float:
        nonlocal index
        if index >= len(tokens) or tokens[index].isalpha():
            raise PotraceOutputError("Potrace SVG path command is truncated")
        value = float(tokens[index])
        index += 1
        if not math.isfinite(value) or abs(value) > 1_000_000_000:
            raise PotraceOutputError("Potrace SVG path coordinate is invalid")
        return value

    def point(relative: bool) -> tuple[float, float]:
        x, y = number(), number()
        return (x + current[0], y + current[1]) if relative else (x, y)

    def finish() -> None:
        nonlocal start, segments
        if start is None or not segments:
            raise PotraceOutputError("Potrace SVG contains an empty subpath")
        if segments[-1][1][-2:] != start:
            segments.append(("L", start))
        parsed.append(_geometry_path(start, segments, transform, view_box, parameters))
        start = None
        segments = []

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
        if command is None:
            raise PotraceOutputError("Potrace SVG path must begin with a command")
        relative = command.islower()
        op = command.upper()
        if op == "M":
            if start is not None:
                raise PotraceOutputError("Potrace subpaths must close before a new move")
            current = point(relative)
            start = current
            command = "l" if relative else "L"
        elif op == "L":
            current = point(relative)
            segments.append(("L", current))
        elif op == "H":
            x = number() + (current[0] if relative else 0)
            current = (x, current[1])
            segments.append(("L", current))
        elif op == "V":
            y = number() + (current[1] if relative else 0)
            current = (current[0], y)
            segments.append(("L", current))
        elif op == "C":
            control_1 = point(relative)
            control_2 = point(relative)
            end = point(relative)
            segments.append(("C", (*control_1, *control_2, *end)))
            current = end
        elif op == "Z":
            closed_start = start
            finish()
            current = closed_start if closed_start is not None else current
            command = None
        else:  # pragma: no cover - tokenizer restricts command alphabet
            raise PotraceOutputError(f"unsupported SVG path command {command!r}")
    if start is not None:
        raise PotraceOutputError("Potrace SVG paths must be closed")
    return parsed


def _geometry_path(
    raw_start: tuple[float, float],
    raw_segments: list[tuple[str, tuple[float, ...]]],
    transform: tuple[float, ...],
    view_box: list[float],
    parameters: PotraceParameters,
) -> Path2D:
    def physical(raw: tuple[float, float]) -> Point2:
        x, y = _apply(transform, raw)
        x_mm = (x - view_box[0]) * parameters.canvas_width_mm / view_box[2]
        y_mm = parameters.canvas_height_mm - (
            (y - view_box[1]) * parameters.canvas_height_mm / view_box[3]
        )
        tolerance = 0.0001
        if abs(x_mm) <= tolerance:
            x_mm = 0
        elif abs(x_mm - parameters.canvas_width_mm) <= tolerance:
            x_mm = parameters.canvas_width_mm
        if abs(y_mm) <= tolerance:
            y_mm = 0
        elif abs(y_mm - parameters.canvas_height_mm) <= tolerance:
            y_mm = parameters.canvas_height_mm
        if not (
            -CANVAS_ROUNDING_SLACK_MM
            <= x_mm
            <= parameters.canvas_width_mm + CANVAS_ROUNDING_SLACK_MM
            and -CANVAS_ROUNDING_SLACK_MM
            <= y_mm
            <= parameters.canvas_height_mm + CANVAS_ROUNDING_SLACK_MM
        ):
            raise PotraceOutputError(
                f"Potrace path escapes the physical canvas at ({x_mm:g}, {y_mm:g})"
            )
        # Only outside points get the wider correction. Interior features retain
        # the existing precision rather than being flattened against the edge.
        x_mm = min(parameters.canvas_width_mm, max(0.0, x_mm))
        y_mm = min(parameters.canvas_height_mm, max(0.0, y_mm))
        try:
            return Point2(x_mm=round(x_mm, 6), y_mm=round(y_mm, 6))
        except ValueError as error:
            raise PotraceOutputError("Potrace path escapes the physical canvas") from error

    start = physical(raw_start)
    geometry_segments: list[LineSegment | CubicSegment] = []
    current = start
    for kind, values in raw_segments:
        if kind == "L":
            end = physical((values[0], values[1]))
            if end != current:
                geometry_segments.append(LineSegment(end=end))
                current = end
        else:
            control_1 = physical((values[0], values[1]))
            control_2 = physical((values[2], values[3]))
            end = physical((values[4], values[5]))
            if end == current:
                if control_1 == control_2 == current:
                    continue
                raise PotraceOutputError(
                    "Potrace emitted a closed-loop cubic unsupported by Geometry IR v1"
                )
            geometry_segments.append(
                CubicSegment(control_1=control_1, control_2=control_2, end=end)
            )
            current = end
    if not geometry_segments:
        raise PotraceOutputError("Potrace subpath collapsed below Geometry IR precision")
    segment_starts = [start, *(segment.end for segment in geometry_segments[:-1])]

    def segment_key(index: int) -> tuple[object, ...]:
        segment = geometry_segments[index]
        prefix: tuple[object, ...] = (
            segment_starts[index].x_mm,
            segment_starts[index].y_mm,
            segment.kind,
        )
        if isinstance(segment, CubicSegment):
            return (
                *prefix,
                segment.control_1.x_mm,
                segment.control_1.y_mm,
                segment.control_2.x_mm,
                segment.control_2.y_mm,
                segment.end.x_mm,
                segment.end.y_mm,
            )
        return (*prefix, segment.end.x_mm, segment.end.y_mm)

    offset = min(range(len(geometry_segments)), key=segment_key)
    start = segment_starts[offset]
    geometry_segments = geometry_segments[offset:] + geometry_segments[:offset]
    return Path2D.create(
        purpose="construction", start=start, segments=tuple(geometry_segments), closed=True
    )


def _normalized_svg(paths: tuple[Path2D, ...], parameters: PotraceParameters) -> bytes:
    compounds = []
    for path in paths:
        commands = [f"M{_point(path.start)}"]
        for segment in path.segments:
            if isinstance(segment, LineSegment):
                commands.append(f"L{_point(segment.end)}")
            else:
                commands.append(
                    "C"
                    f"{_point(segment.control_1)} {_point(segment.control_2)} "
                    f"{_point(segment.end)}"
                )
        commands.append("Z")
        compounds.append("".join(commands))
    width = _number(parameters.canvas_width_mm)
    height = _number(parameters.canvas_height_mm)
    path_data = "".join(compounds)
    artwork = f'<path d="{path_data}" fill="#000000" fill-rule="nonzero"/>' if path_data else ""
    return (
        f'<svg xmlns="{SVG_NAMESPACE}" width="{width}mm" height="{height}mm" '
        f'viewBox="0 0 {width} {height}"><g transform="translate(0 {height}) scale(1 -1)">'
        f"{artwork}</g></svg>"
    ).encode()


def _validate_physical_extent(value: Optional[str], expected: float, name: str) -> None:
    if value is None:
        raise PotraceOutputError(f"Potrace SVG is missing physical {name}")
    match = re.fullmatch(rf"\s*({NUMBER})\s*(mm|pt)\s*", value)
    if match is None:
        raise PotraceOutputError(f"Potrace SVG physical {name} does not match the request")
    physical_mm = float(match.group(1)) * (25.4 / 72 if match.group(2) == "pt" else 1)
    if not math.isclose(physical_mm, expected, abs_tol=0.00001):
        raise PotraceOutputError(f"Potrace SVG physical {name} does not match the request")


def _parse_transform(value: str) -> tuple[float, ...]:
    if not value.strip():
        return _identity()
    compact = re.sub(r"\s+", "", value)
    matches = list(TRANSFORM.finditer(value))
    if "".join(re.sub(r"\s+", "", match.group(0)) for match in matches) != compact:
        raise PotraceOutputError("Potrace SVG contains an unsupported transform")
    result = _identity()
    for match in matches:
        values = _numbers(match.group(2))
        if match.group(1) == "translate" and len(values) in (1, 2):
            operation = (1, 0, 0, 1, values[0], values[1] if len(values) == 2 else 0)
        elif match.group(1) == "scale" and len(values) in (1, 2):
            operation = (values[0], 0, 0, values[-1], 0, 0)
        else:
            raise PotraceOutputError("Potrace SVG transform must use only translate and scale")
        result = _multiply(result, operation)
    return result


def _numbers(value: str) -> list[float]:
    tokens = re.findall(NUMBER, value)
    compact = re.sub(r"[\s,]+", "", value)
    if "".join(tokens) != compact:
        return []
    numbers = [float(item) for item in tokens]
    return numbers if all(math.isfinite(item) for item in numbers) else []


def _identity() -> tuple[float, ...]:
    return (1, 0, 0, 1, 0, 0)


def _multiply(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f = left
    g, h, i, j, k, length = right
    return (
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * k + c * length + e,
        b * k + d * length + f,
    )


def _apply(matrix: tuple[float, ...], point: tuple[float, float]) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * point[0] + c * point[1] + e, b * point[0] + d * point[1] + f)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _point(point: Point2) -> str:
    return f"{_number(point.x_mm)} {_number(point.y_mm)}"


def _number(value: float) -> str:
    rendered = format(float(value), ".12f").rstrip("0").rstrip(".")
    return "0" if rendered in ("", "-0") else rendered
