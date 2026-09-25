"""Stable, streaming extraction of slicer validation evidence from Bambu G-code.

The report contract intentionally depends on documented G-code comments and commands rather than
the Bambu Studio UI.  Unknown or absent metadata remains ``None`` instead of being guessed.  This
keeps reports useful across slicer versions while preserving the raw process logs for diagnosis.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import BinaryIO, Optional

REPORT_SCHEMA_VERSION = 1
MAX_GCODE_LINE_BYTES = 1024 * 1024

_HEADER_VALUE = re.compile(r"^;\s*([^:]+?)\s*:\s*(.*?)\s*$")
_CONFIG_VALUE = re.compile(r"^;\s*([^=]+?)\s*=\s*(.*?)\s*$")
_COMMAND = re.compile(r"^\s*([GMT]\d+(?:\.\d+)?)\b(.*)$", re.IGNORECASE)
_PARAMETER = re.compile(r"(?:^|\s)([XYZE])([-+]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
_TOOL = re.compile(r"^\s*T(\d+)\s*(?:;.*)?$", re.IGNORECASE)
_TOOLCHANGE_NUMBER = re.compile(r"^;\s*toolchange\s*#(\d+)\s*$", re.IGNORECASE)
_HEX_COLOR = re.compile(r"^#[0-9A-F]{6}$")
_FLOATING_WARNING = re.compile(
    r"\b(?:floating|unsupported)\s+(?:region|regions|part|parts|island|islands|shell|shells)\b",
    re.IGNORECASE,
)
_SUPPORT_WARNING = re.compile(
    r"(?:\b(?:requires?|needs?|enable|add)\b.{0,48}\bsupports?\b|"
    r"\bsupports?\b.{0,48}\b(?:required|needed|recommended)\b)",
    re.IGNORECASE,
)
_GENERIC_WARNING = re.compile(r"\bwarn(?:ing)?\b", re.IGNORECASE)


class SlicerWarningCategory(str, Enum):
    FLOATING_REGION = "floating_region"
    SUPPORT_REQUIRED = "support_required"
    GENERIC = "generic"


@dataclass(frozen=True)
class SlicerBounds:
    min_x_mm: float
    min_y_mm: float
    min_z_mm: float
    max_x_mm: float
    max_y_mm: float
    max_z_mm: float

    def as_contract(self) -> dict[str, float]:
        return {
            "min_x_mm": self.min_x_mm,
            "min_y_mm": self.min_y_mm,
            "min_z_mm": self.min_z_mm,
            "max_x_mm": self.max_x_mm,
            "max_y_mm": self.max_y_mm,
            "max_z_mm": self.max_z_mm,
        }


@dataclass(frozen=True)
class SlicerFilamentUsage:
    """Usage aligned to Bambu's one-based ``; filament:`` identifiers."""

    extruder: int
    color_hex: Optional[str]
    length_mm: Optional[float]
    volume_mm3: Optional[float]
    weight_g: Optional[float]

    def as_contract(self) -> dict[str, object]:
        return {
            "extruder": self.extruder,
            "color_hex": self.color_hex,
            "length_mm": self.length_mm,
            "volume_mm3": self.volume_mm3,
            "weight_g": self.weight_g,
        }


@dataclass(frozen=True)
class SlicerToolChange:
    ordinal: int
    target_extruder: Optional[int]
    layer: Optional[int]
    z_height_mm: Optional[float]

    def as_contract(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "target_extruder": self.target_extruder,
            "layer": self.layer,
            "z_height_mm": self.z_height_mm,
        }


@dataclass(frozen=True)
class SlicerPlateReport:
    member: str
    declared_layer_count: Optional[int]
    observed_layer_count: int
    max_z_height_mm: Optional[float]
    used_extruders: tuple[int, ...]
    observed_colors: tuple[str, ...]
    filament_usage: tuple[SlicerFilamentUsage, ...]
    tool_changes: tuple[SlicerToolChange, ...]
    model_extrusion_bounds: Optional[SlicerBounds]
    has_extrusion: bool

    def as_contract(self) -> dict[str, object]:
        return {
            "member": self.member,
            "declared_layer_count": self.declared_layer_count,
            "observed_layer_count": self.observed_layer_count,
            "max_z_height_mm": self.max_z_height_mm,
            "used_extruders": list(self.used_extruders),
            "observed_colors": list(self.observed_colors),
            "filament_usage": [item.as_contract() for item in self.filament_usage],
            "tool_changes": [item.as_contract() for item in self.tool_changes],
            "model_extrusion_bounds": (
                self.model_extrusion_bounds.as_contract()
                if self.model_extrusion_bounds is not None
                else None
            ),
            "has_extrusion": self.has_extrusion,
        }


@dataclass(frozen=True)
class SlicerWarning:
    category: SlicerWarningCategory
    source: str
    message: str
    line_number: int

    def as_contract(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "source": self.source,
            "message": self.message,
            "line_number": self.line_number,
        }


@dataclass(frozen=True)
class SlicerRawLogs:
    stdout: str
    stderr: str

    def as_contract(self) -> dict[str, str]:
        return {"stdout": self.stdout, "stderr": self.stderr}


@dataclass(frozen=True)
class SlicerValidationExpectation:
    expected_colors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        canonical = tuple(_canonical_color(item) for item in self.expected_colors)
        if len(canonical) != len(set(canonical)):
            raise ValueError("expected slicer colors must be unique")
        object.__setattr__(self, "expected_colors", canonical)


@dataclass(frozen=True)
class SlicerValidationReport:
    schema_version: int
    plates: tuple[SlicerPlateReport, ...]
    expected_colors: tuple[str, ...]
    observed_colors: tuple[str, ...]
    missing_expected_colors: tuple[str, ...]
    tool_change_count: int
    warnings: tuple[SlicerWarning, ...]
    raw_logs: SlicerRawLogs

    @property
    def expected_colors_present(self) -> bool:
        return not self.missing_expected_colors

    @property
    def has_floating_warning(self) -> bool:
        return any(item.category == SlicerWarningCategory.FLOATING_REGION for item in self.warnings)

    @property
    def has_support_warning(self) -> bool:
        return any(
            item.category == SlicerWarningCategory.SUPPORT_REQUIRED for item in self.warnings
        )

    def as_contract(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plates": [item.as_contract() for item in self.plates],
            "expected_colors": list(self.expected_colors),
            "observed_colors": list(self.observed_colors),
            "missing_expected_colors": list(self.missing_expected_colors),
            "expected_colors_present": self.expected_colors_present,
            "tool_change_count": self.tool_change_count,
            "warnings": [item.as_contract() for item in self.warnings],
            "has_floating_warning": self.has_floating_warning,
            "has_support_warning": self.has_support_warning,
            "raw_logs": self.raw_logs.as_contract(),
        }


class _BoundsAccumulator:
    def __init__(self) -> None:
        self.minimum: Optional[list[float]] = None
        self.maximum: Optional[list[float]] = None

    def include(self, x: float, y: float, z: float) -> None:
        values = [x, y, z]
        if self.minimum is None or self.maximum is None:
            self.minimum = values.copy()
            self.maximum = values.copy()
            return
        self.minimum = [min(old, new) for old, new in zip(self.minimum, values)]
        self.maximum = [max(old, new) for old, new in zip(self.maximum, values)]

    def freeze(self) -> Optional[SlicerBounds]:
        if self.minimum is None or self.maximum is None:
            return None
        return SlicerBounds(
            min_x_mm=self.minimum[0],
            min_y_mm=self.minimum[1],
            min_z_mm=self.minimum[2],
            max_x_mm=self.maximum[0],
            max_y_mm=self.maximum[1],
            max_z_mm=self.maximum[2],
        )


def parse_slicer_plate(gcode: BinaryIO, *, member: str) -> SlicerPlateReport:
    """Parse one plate G-code stream without loading it into memory."""

    header: dict[str, str] = {}
    colors: tuple[str, ...] = ()
    used_extruders: list[int] = []
    tool_changes: list[SlicerToolChange] = []
    in_toolchange = False
    pending_toolchange_ordinal: Optional[int] = None
    observed_layers = 0
    current_layer: Optional[int] = None
    current_layer_z: Optional[float] = None
    active_feature: Optional[str] = None
    after_first_layer = False
    xyz_absolute = True
    e_absolute = False
    x = 0.0
    y = 0.0
    z = 0.0
    e = 0.0
    all_bounds = _BoundsAccumulator()
    model_bounds = _BoundsAccumulator()
    has_extrusion = False

    for raw in _safe_lines(gcode):
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        stripped = line.strip()

        header_match = _HEADER_VALUE.match(stripped)
        if header_match:
            key = header_match.group(1).strip().lower()
            value = header_match.group(2).strip()
            if key in {
                "total layer number",
                "total filament length [mm]",
                "total filament volume [cm^3]",
                "total filament weight [g]",
                "max_z_height",
                "filament",
            }:
                header[key] = value

        config_match = _CONFIG_VALUE.match(stripped)
        if config_match and config_match.group(1).strip().lower() == "filament_colour":
            colors = tuple(
                color
                for color in (_try_color(item) for item in config_match.group(2).split(";"))
                if color is not None
            )

        if stripped.upper() == "; CHANGE_LAYER":
            observed_layers += 1
            current_layer = observed_layers
            after_first_layer = True
            active_feature = None
            continue
        if stripped.upper().startswith("; Z_HEIGHT:"):
            current_layer_z = _optional_float(stripped.split(":", 1)[1])
            continue
        if stripped.upper().startswith("; FEATURE:"):
            active_feature = stripped.split(":", 1)[1].strip().lower()
            continue
        if stripped.upper() == "; CP TOOLCHANGE START":
            in_toolchange = True
            pending_toolchange_ordinal = None
            continue
        if stripped.upper() == "; CP TOOLCHANGE END":
            in_toolchange = False
            pending_toolchange_ordinal = None
            continue
        toolchange_number = _TOOLCHANGE_NUMBER.match(stripped)
        if in_toolchange and toolchange_number:
            pending_toolchange_ordinal = int(toolchange_number.group(1))
            continue
        tool = _TOOL.match(stripped)
        if tool:
            tool_index = int(tool.group(1))
            if 0 <= tool_index < 255 and tool_index + 1 not in used_extruders:
                used_extruders.append(tool_index + 1)
            if in_toolchange:
                ordinal = pending_toolchange_ordinal or len(tool_changes) + 1
                tool_changes.append(
                    SlicerToolChange(
                        ordinal=ordinal,
                        target_extruder=tool_index + 1 if tool_index < 255 else None,
                        layer=current_layer,
                        z_height_mm=current_layer_z,
                    )
                )
                in_toolchange = False
                pending_toolchange_ordinal = None
            continue

        code = line.split(";", 1)[0].strip()
        command_match = _COMMAND.match(code)
        if not command_match:
            continue
        command = command_match.group(1).upper()
        arguments = command_match.group(2)
        if command == "G90":
            xyz_absolute = True
            continue
        if command == "G91":
            xyz_absolute = False
            continue
        if command == "M82":
            e_absolute = True
            continue
        if command == "M83":
            e_absolute = False
            continue
        parameters = {key.upper(): float(value) for key, value in _PARAMETER.findall(arguments)}
        if command == "G92":
            x = parameters.get("X", x)
            y = parameters.get("Y", y)
            z = parameters.get("Z", z)
            e = parameters.get("E", e)
            continue
        if command not in {"G0", "G1", "G2", "G3"}:
            continue

        next_x = parameters.get("X", x)
        next_y = parameters.get("Y", y)
        next_z = parameters.get("Z", z)
        if not xyz_absolute:
            next_x = x + parameters.get("X", 0.0)
            next_y = y + parameters.get("Y", 0.0)
            next_z = z + parameters.get("Z", 0.0)
        extrusion_delta = 0.0
        if "E" in parameters:
            next_e = parameters["E"] if e_absolute else e + parameters["E"]
            extrusion_delta = next_e - e
            e = next_e
        x, y, z = next_x, next_y, next_z
        if extrusion_delta <= 0 or not ({"X", "Y"} & parameters.keys()):
            continue
        has_extrusion = True
        model_z = current_layer_z if current_layer_z is not None else z
        all_bounds.include(x, y, model_z)
        if after_first_layer and active_feature not in {"prime tower", "custom"}:
            model_bounds.include(x, y, model_z)

    declared_used = _integer_list(header.get("filament"))
    if declared_used:
        used_extruders = declared_used
    observed_colors = tuple(
        colors[index - 1] for index in used_extruders if 0 < index <= len(colors)
    )
    usage = _filament_usage(header, colors, used_extruders)
    return SlicerPlateReport(
        member=member,
        declared_layer_count=_optional_int(header.get("total layer number")),
        observed_layer_count=observed_layers,
        max_z_height_mm=_optional_float(header.get("max_z_height")),
        used_extruders=tuple(used_extruders),
        observed_colors=observed_colors,
        filament_usage=usage,
        tool_changes=tuple(tool_changes),
        model_extrusion_bounds=model_bounds.freeze() or all_bounds.freeze(),
        has_extrusion=has_extrusion,
    )


def build_slicer_validation_report(
    plates: Iterable[SlicerPlateReport],
    *,
    expectation: Optional[SlicerValidationExpectation] = None,
    stdout: str = "",
    stderr: str = "",
) -> SlicerValidationReport:
    frozen_plates = tuple(plates)
    expected = expectation.expected_colors if expectation is not None else ()
    observed = tuple(
        dict.fromkeys(color for plate in frozen_plates for color in plate.observed_colors)
    )
    missing = tuple(color for color in expected if color not in observed)
    warnings = tuple(_log_warnings("stdout", stdout) + _log_warnings("stderr", stderr))
    return SlicerValidationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        plates=frozen_plates,
        expected_colors=expected,
        observed_colors=observed,
        missing_expected_colors=missing,
        tool_change_count=sum(len(plate.tool_changes) for plate in frozen_plates),
        warnings=warnings,
        raw_logs=SlicerRawLogs(stdout=stdout, stderr=stderr),
    )


def _safe_lines(gcode: BinaryIO) -> Iterable[bytes]:
    while True:
        line = gcode.readline(MAX_GCODE_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_GCODE_LINE_BYTES and not line.endswith(b"\n"):
            raise ValueError("Bambu Studio plate G-code contains an oversized line.")
        yield line


def _filament_usage(
    header: dict[str, str],
    colors: tuple[str, ...],
    used_extruders: list[int],
) -> tuple[SlicerFilamentUsage, ...]:
    lengths = _float_list(header.get("total filament length [mm]"))
    volumes = _float_list(header.get("total filament volume [cm^3]"))
    weights = _float_list(header.get("total filament weight [g]"))
    return tuple(
        SlicerFilamentUsage(
            extruder=extruder,
            color_hex=colors[extruder - 1] if 0 < extruder <= len(colors) else None,
            length_mm=lengths[index] if index < len(lengths) else None,
            # Bambu labels this field cm^3 but emits the numerical value in mm^3
            # (filament length multiplied by 1.75 mm cross-sectional area).
            volume_mm3=volumes[index] if index < len(volumes) else None,
            weight_g=weights[index] if index < len(weights) else None,
        )
        for index, extruder in enumerate(used_extruders)
    )


def _log_warnings(source: str, payload: str) -> list[SlicerWarning]:
    result: list[SlicerWarning] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        message = line.strip()
        category: Optional[SlicerWarningCategory] = None
        if _FLOATING_WARNING.search(message):
            category = SlicerWarningCategory.FLOATING_REGION
        elif _SUPPORT_WARNING.search(message):
            category = SlicerWarningCategory.SUPPORT_REQUIRED
        elif _GENERIC_WARNING.search(message):
            category = SlicerWarningCategory.GENERIC
        if message and category is not None:
            result.append(
                SlicerWarning(
                    category=category,
                    source=source,
                    message=message,
                    line_number=line_number,
                )
            )
    return result


def _canonical_color(value: str) -> str:
    color = value.strip().upper()
    if not color.startswith("#"):
        color = f"#{color}"
    if not _HEX_COLOR.fullmatch(color):
        raise ValueError(f"invalid expected slicer color {value!r}")
    return color


def _try_color(value: str) -> Optional[str]:
    try:
        return _canonical_color(value)
    except ValueError:
        return None


def _float_list(value: Optional[str]) -> tuple[float, ...]:
    if value is None:
        return ()
    result: list[float] = []
    for item in value.split(","):
        parsed = _optional_float(item)
        if parsed is None:
            return ()
        result.append(parsed)
    return tuple(result)


def _integer_list(value: Optional[str]) -> list[int]:
    if value is None:
        return []
    result: list[int] = []
    for item in value.split(","):
        parsed = _optional_int(item)
        if parsed is None or parsed <= 0 or parsed in result:
            return []
        result.append(parsed)
    return result


def _optional_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def _optional_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None
