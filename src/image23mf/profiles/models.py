"""Versioned profile contracts and suggestion-bearing setup validation."""

import hashlib
import json
import math
from enum import Enum
from importlib.resources import files
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILE_SCHEMA_VERSION = 1
LAYER_ALIGNMENT_TOLERANCE_MM = 0.0001


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ProfileSource(ProfileModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=80)
    captured_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    profile_paths: tuple[str, ...] = Field(min_length=1)


class ExcludedRectangle(ProfileModel):
    x_mm: float = Field(ge=0)
    y_mm: float = Field(ge=0)
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)


class PrintableArea(ProfileModel):
    origin_x_mm: float = Field(ge=0)
    origin_y_mm: float = Field(ge=0)
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    excluded_rectangles: tuple[ExcludedRectangle, ...] = ()

    @model_validator(mode="after")
    def exclusions_stay_inside_area(self) -> "PrintableArea":
        for rectangle in self.excluded_rectangles:
            if (
                rectangle.x_mm + rectangle.width_mm > self.width_mm
                or rectangle.y_mm + rectangle.depth_mm > self.depth_mm
            ):
                raise ValueError("printable-area exclusion must remain inside the bed")
        return self


class NozzleProfile(ProfileModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,79}$")
    diameter_mm: float = Field(gt=0, le=2)
    material: str = Field(min_length=1, max_length=80)
    slicer_machine_name: str = Field(min_length=1, max_length=200)
    slicer_variant: str = Field(min_length=1, max_length=40)
    min_layer_height_mm: float = Field(gt=0, le=2)
    max_layer_height_mm: float = Field(gt=0, le=2)
    default_layer_height_mm: float = Field(gt=0, le=2)
    recommended_layer_heights_mm: tuple[float, ...] = Field(min_length=1)
    process_profile_names: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def layer_options_are_consistent(self) -> "NozzleProfile":
        if self.min_layer_height_mm > self.max_layer_height_mm:
            raise ValueError("nozzle minimum layer height cannot exceed maximum")
        if len(set(self.recommended_layer_heights_mm)) != len(self.recommended_layer_heights_mm):
            raise ValueError("recommended layer heights must be unique")
        if tuple(sorted(self.recommended_layer_heights_mm)) != self.recommended_layer_heights_mm:
            raise ValueError("recommended layer heights must be sorted")
        if any(
            height < self.min_layer_height_mm or height > self.max_layer_height_mm
            for height in self.recommended_layer_heights_mm
        ):
            raise ValueError("recommended layer heights must remain inside the nozzle range")
        if self.default_layer_height_mm not in self.recommended_layer_heights_mm:
            raise ValueError("default layer height must be a recommended layer height")
        return self


class PlateProfile(ProfileModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,79}$")
    display_name: str = Field(min_length=1, max_length=120)
    slicer_name: str = Field(min_length=1, max_length=120)
    surface: str = Field(min_length=1, max_length=160)
    first_layer_finish: str = Field(min_length=1, max_length=80)
    notes: str = Field(min_length=1, max_length=500)


class ThicknessPolicy(ProfileModel):
    min_base_mm: float = Field(gt=0)
    max_base_mm: float = Field(gt=0)
    default_base_target_mm: float = Field(gt=0)
    min_art_layers: int = Field(ge=1, le=100)
    max_art_mm: float = Field(gt=0)
    default_art_target_mm: float = Field(gt=0)
    max_total_mm: float = Field(gt=0)
    require_layer_multiples: bool = True

    @model_validator(mode="after")
    def thickness_ranges_are_consistent(self) -> "ThicknessPolicy":
        if not self.min_base_mm <= self.default_base_target_mm <= self.max_base_mm:
            raise ValueError("default base target must remain inside the base range")
        if self.default_art_target_mm > self.max_art_mm:
            raise ValueError("default art target cannot exceed maximum art thickness")
        if self.max_base_mm + self.max_art_mm < self.max_total_mm:
            raise ValueError("maximum total thickness exceeds the component maxima")
        return self


class PrinterProfile(ProfileModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,79}$")
    display_name: str = Field(min_length=1, max_length=160)
    manufacturer: str = Field(min_length=1, max_length=120)
    technology: str = Field(min_length=1, max_length=40)
    slicer_model_name: str = Field(min_length=1, max_length=160)
    printable_area: PrintableArea
    nozzles: tuple[NozzleProfile, ...] = Field(min_length=1)
    plates: tuple[PlateProfile, ...] = Field(min_length=1)
    default_nozzle_id: str
    default_plate_id: str
    thickness_policy: ThicknessPolicy

    @model_validator(mode="after")
    def child_ids_and_defaults_are_consistent(self) -> "PrinterProfile":
        nozzle_ids = [item.id for item in self.nozzles]
        plate_ids = [item.id for item in self.plates]
        if len(nozzle_ids) != len(set(nozzle_ids)):
            raise ValueError("printer nozzle ids must be unique")
        if len(plate_ids) != len(set(plate_ids)):
            raise ValueError("printer plate ids must be unique")
        if self.default_nozzle_id not in nozzle_ids:
            raise ValueError("default nozzle must identify a printer nozzle")
        if self.default_plate_id not in plate_ids:
            raise ValueError("default plate must identify a printer plate")
        return self

    def nozzle(self, nozzle_id: str) -> Optional[NozzleProfile]:
        return next((item for item in self.nozzles if item.id == nozzle_id), None)

    def plate(self, plate_id: str) -> Optional[PlateProfile]:
        return next((item for item in self.plates if item.id == plate_id), None)


class ProfileCatalog(ProfileModel):
    schema_version: Literal[1] = PROFILE_SCHEMA_VERSION
    catalog_id: str = Field(min_length=1, max_length=120)
    catalog_version: str = Field(min_length=1, max_length=80)
    source: ProfileSource
    printers: tuple[PrinterProfile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def printer_ids_are_unique(self) -> "ProfileCatalog":
        ids = [item.id for item in self.printers]
        if len(ids) != len(set(ids)):
            raise ValueError("catalog printer ids must be unique")
        return self

    def printer(self, printer_id: str) -> Optional[PrinterProfile]:
        return next((item for item in self.printers if item.id == printer_id), None)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class PrintSetupRequest(ProfileModel):
    printer_id: str
    nozzle_id: str
    plate_id: str
    layer_height_mm: float
    canvas_width_mm: float
    canvas_height_mm: float
    base_thickness_mm: float
    art_thickness_mm: float


class ProfileIssueCode(str, Enum):
    CATALOG_MISMATCH = "catalog_mismatch"
    UNKNOWN_PRINTER = "unknown_printer"
    INCOMPATIBLE_NOZZLE = "incompatible_nozzle"
    NOZZLE_DIAMETER_MISMATCH = "nozzle_diameter_mismatch"
    INCOMPATIBLE_PLATE = "incompatible_plate"
    LAYER_OUT_OF_RANGE = "layer_out_of_range"
    CANVAS_OUT_OF_RANGE = "canvas_out_of_range"
    BASE_OUT_OF_RANGE = "base_out_of_range"
    ART_OUT_OF_RANGE = "art_out_of_range"
    TOTAL_THICKNESS_OUT_OF_RANGE = "total_thickness_out_of_range"
    THICKNESS_NOT_LAYER_ALIGNED = "thickness_not_layer_aligned"


class ProfileIssue(ProfileModel):
    code: ProfileIssueCode
    field: str
    message: str
    suggestion: str
    details: dict[str, Any] = Field(default_factory=dict)


class ProfileValidationError(ValueError):
    def __init__(self, issues: tuple[ProfileIssue, ...]) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


class ValidatedPrintSetup(ProfileModel):
    request: PrintSetupRequest
    printer: PrinterProfile
    nozzle: NozzleProfile
    plate: PlateProfile
    profile_catalog_fingerprint: str


class ProfileCatalogService:
    def __init__(self, catalog: ProfileCatalog) -> None:
        self.catalog = catalog

    @classmethod
    def bundled(cls) -> "ProfileCatalogService":
        return cls(load_bundled_catalog())

    def default_request(self, printer_id: str) -> PrintSetupRequest:
        printer = self._require_printer(printer_id)
        nozzle = printer.nozzle(printer.default_nozzle_id)
        if nozzle is None:  # pragma: no cover - catalog validation guarantees this
            raise RuntimeError("validated catalog lost its default nozzle")
        policy = printer.thickness_policy
        layer = nozzle.default_layer_height_mm
        return PrintSetupRequest(
            printer_id=printer.id,
            nozzle_id=nozzle.id,
            plate_id=printer.default_plate_id,
            layer_height_mm=layer,
            canvas_width_mm=min(200, printer.printable_area.width_mm),
            canvas_height_mm=min(200, printer.printable_area.depth_mm),
            base_thickness_mm=_snap_to_layer(
                policy.default_base_target_mm,
                layer,
                minimum=policy.min_base_mm,
                maximum=policy.max_base_mm,
            ),
            art_thickness_mm=_snap_to_layer(
                policy.default_art_target_mm,
                layer,
                minimum=policy.min_art_layers * layer,
                maximum=policy.max_art_mm,
            ),
        )

    def validate(self, request: PrintSetupRequest) -> ValidatedPrintSetup:
        printer = self.catalog.printer(request.printer_id)
        if printer is None:
            available = [item.id for item in self.catalog.printers]
            raise ProfileValidationError(
                (
                    ProfileIssue(
                        code=ProfileIssueCode.UNKNOWN_PRINTER,
                        field="printer_id",
                        message=f"Unknown printer profile: {request.printer_id}.",
                        suggestion=f"Choose an available printer: {', '.join(available)}.",
                        details={"available_printer_ids": available},
                    ),
                )
            )

        issues: list[ProfileIssue] = []
        nozzle = printer.nozzle(request.nozzle_id)
        if nozzle is None:
            available = [item.id for item in printer.nozzles]
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.INCOMPATIBLE_NOZZLE,
                    field="nozzle_id",
                    message=(
                        f"Nozzle {request.nozzle_id} is not configured for {printer.display_name}."
                    ),
                    suggestion=f"Choose one of: {', '.join(available)}.",
                    details={"available_nozzle_ids": available},
                )
            )
        plate = printer.plate(request.plate_id)
        if plate is None:
            available = [item.id for item in printer.plates]
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.INCOMPATIBLE_PLATE,
                    field="plate_id",
                    message=(
                        f"Plate {request.plate_id} is not configured for {printer.display_name}."
                    ),
                    suggestion=f"Choose one of: {', '.join(available)}.",
                    details={"available_plate_ids": available},
                )
            )

        if nozzle is not None:
            self._validate_layer(request, nozzle, issues)
        self._validate_canvas(request, printer, issues)
        if request.layer_height_mm > 0:
            self._validate_thickness(request, printer, issues)

        if issues:
            raise ProfileValidationError(tuple(issues))
        if nozzle is None or plate is None:  # pragma: no cover - issues guarantee early raise
            raise RuntimeError("validated setup is missing a profile selection")
        return ValidatedPrintSetup(
            request=request,
            printer=printer,
            nozzle=nozzle,
            plate=plate,
            profile_catalog_fingerprint=self.catalog.fingerprint(),
        )

    def validate_pinned(
        self,
        request: PrintSetupRequest,
        *,
        catalog_id: str,
        catalog_version: str,
        nozzle_diameter_mm: float,
    ) -> ValidatedPrintSetup:
        issues: list[ProfileIssue] = []
        if catalog_id != self.catalog.catalog_id or catalog_version != self.catalog.catalog_version:
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.CATALOG_MISMATCH,
                    field="profile_catalog",
                    message=(
                        f"The job pins profile catalog {catalog_id}@{catalog_version}, but this "
                        f"installation provides {self.catalog.catalog_id}@"
                        f"{self.catalog.catalog_version}."
                    ),
                    suggestion="Review the current printer choices and save the job again.",
                    details={
                        "available_catalog_id": self.catalog.catalog_id,
                        "available_catalog_version": self.catalog.catalog_version,
                    },
                )
            )
        validated: Optional[ValidatedPrintSetup] = None
        try:
            validated = self.validate(request)
        except ProfileValidationError as error:
            issues.extend(error.issues)
        if validated is not None and not math.isclose(
            nozzle_diameter_mm,
            validated.nozzle.diameter_mm,
            rel_tol=0,
            abs_tol=LAYER_ALIGNMENT_TOLERANCE_MM,
        ):
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.NOZZLE_DIAMETER_MISMATCH,
                    field="nozzle_mm",
                    message=(
                        f"The saved nozzle diameter is {nozzle_diameter_mm:g} mm, but profile "
                        f"{validated.nozzle.id} resolves to {validated.nozzle.diameter_mm:g} mm."
                    ),
                    suggestion=(
                        f"Use {validated.nozzle.diameter_mm:g} mm or select the matching nozzle "
                        "profile."
                    ),
                    details={"suggested_mm": validated.nozzle.diameter_mm},
                )
            )
        if issues:
            raise ProfileValidationError(tuple(issues))
        if validated is None:  # pragma: no cover - validation errors guarantee an issue
            raise RuntimeError("pinned profile validation produced no result")
        return validated

    def _require_printer(self, printer_id: str) -> PrinterProfile:
        printer = self.catalog.printer(printer_id)
        if printer is None:
            available = ", ".join(item.id for item in self.catalog.printers)
            raise ValueError(f"unknown printer {printer_id}; choose one of: {available}")
        return printer

    @staticmethod
    def _validate_layer(
        request: PrintSetupRequest,
        nozzle: NozzleProfile,
        issues: list[ProfileIssue],
    ) -> None:
        if not nozzle.min_layer_height_mm <= request.layer_height_mm <= nozzle.max_layer_height_mm:
            if request.layer_height_mm <= 0:
                suggested = nozzle.default_layer_height_mm
            else:
                suggested = min(
                    max(request.layer_height_mm, nozzle.min_layer_height_mm),
                    nozzle.max_layer_height_mm,
                )
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.LAYER_OUT_OF_RANGE,
                    field="layer_height_mm",
                    message=(
                        f"Layer height {request.layer_height_mm:g} mm is outside the "
                        f"{nozzle.min_layer_height_mm:g}–{nozzle.max_layer_height_mm:g} mm "
                        f"range for the {nozzle.diameter_mm:g} mm nozzle."
                    ),
                    suggestion=(
                        f"Use {suggested:g} mm or choose an installed preset: "
                        f"{_format_values(nozzle.recommended_layer_heights_mm)} mm."
                    ),
                    details={
                        "minimum_mm": nozzle.min_layer_height_mm,
                        "maximum_mm": nozzle.max_layer_height_mm,
                        "suggested_mm": suggested,
                    },
                )
            )

    @staticmethod
    def _validate_canvas(
        request: PrintSetupRequest,
        printer: PrinterProfile,
        issues: list[ProfileIssue],
    ) -> None:
        area = printer.printable_area
        invalid = (
            request.canvas_width_mm <= 0
            or request.canvas_height_mm <= 0
            or request.canvas_width_mm > area.width_mm
            or request.canvas_height_mm > area.depth_mm
        )
        if invalid:
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.CANVAS_OUT_OF_RANGE,
                    field="canvas",
                    message=(
                        f"Canvas {request.canvas_width_mm:g}×{request.canvas_height_mm:g} mm does "
                        f"not fit the {area.width_mm:g}×{area.depth_mm:g} mm printable area."
                    ),
                    suggestion=(
                        f"Use positive dimensions no larger than {area.width_mm:g}×"
                        f"{area.depth_mm:g} mm; leave slicer-managed room for a prime tower "
                        "when needed."
                    ),
                    details={
                        "maximum_width_mm": area.width_mm,
                        "maximum_height_mm": area.depth_mm,
                    },
                )
            )

    @staticmethod
    def _validate_thickness(
        request: PrintSetupRequest,
        printer: PrinterProfile,
        issues: list[ProfileIssue],
    ) -> None:
        policy = printer.thickness_policy
        base_in_range = policy.min_base_mm <= request.base_thickness_mm <= policy.max_base_mm
        if not base_in_range:
            suggested = min(max(request.base_thickness_mm, policy.min_base_mm), policy.max_base_mm)
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.BASE_OUT_OF_RANGE,
                    field="base_thickness_mm",
                    message=(
                        f"Base thickness {request.base_thickness_mm:g} mm is outside the "
                        f"{policy.min_base_mm:g}–{policy.max_base_mm:g} mm art-plate range."
                    ),
                    suggestion=f"Use {suggested:g} mm before layer alignment.",
                    details={"suggested_mm": suggested},
                )
            )
        minimum_art = policy.min_art_layers * request.layer_height_mm
        art_in_range = minimum_art <= request.art_thickness_mm <= policy.max_art_mm
        if not art_in_range:
            suggested = min(max(request.art_thickness_mm, minimum_art), policy.max_art_mm)
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.ART_OUT_OF_RANGE,
                    field="art_thickness_mm",
                    message=(
                        f"Art thickness {request.art_thickness_mm:g} mm must be at least "
                        f"{policy.min_art_layers} layer(s) ({minimum_art:g} mm) and no more than "
                        f"{policy.max_art_mm:g} mm."
                    ),
                    suggestion=f"Use {suggested:g} mm before layer alignment.",
                    details={"minimum_mm": minimum_art, "suggested_mm": suggested},
                )
            )
        total = request.base_thickness_mm + request.art_thickness_mm
        if total > policy.max_total_mm:
            issues.append(
                ProfileIssue(
                    code=ProfileIssueCode.TOTAL_THICKNESS_OUT_OF_RANGE,
                    field="total_thickness_mm",
                    message=(
                        f"Combined base and art thickness is {total:g} mm, above the "
                        f"{policy.max_total_mm:g} mm art-plate limit."
                    ),
                    suggestion="Reduce base or art thickness until their sum fits the limit.",
                    details={"actual_mm": total, "maximum_mm": policy.max_total_mm},
                )
            )
        if policy.require_layer_multiples and base_in_range and art_in_range:
            for field, value, minimum, maximum in (
                (
                    "base_thickness_mm",
                    request.base_thickness_mm,
                    policy.min_base_mm,
                    policy.max_base_mm,
                ),
                ("art_thickness_mm", request.art_thickness_mm, minimum_art, policy.max_art_mm),
            ):
                if not _is_layer_aligned(value, request.layer_height_mm):
                    suggested = _snap_to_layer(
                        value,
                        request.layer_height_mm,
                        minimum=minimum,
                        maximum=maximum,
                    )
                    issues.append(
                        ProfileIssue(
                            code=ProfileIssueCode.THICKNESS_NOT_LAYER_ALIGNED,
                            field=field,
                            message=(
                                f"{field.replace('_', ' ').capitalize()} {value:g} mm is not a "
                                f"whole multiple of the {request.layer_height_mm:g} mm layer "
                                "height."
                            ),
                            suggestion=(
                                f"Use {suggested:g} mm "
                                f"({suggested / request.layer_height_mm:g} layers)."
                            ),
                            details={"suggested_mm": suggested},
                        )
                    )


def load_bundled_catalog() -> ProfileCatalog:
    payload = files("image23mf.profiles").joinpath("catalog-v1.json").read_text(encoding="utf-8")
    return ProfileCatalog.model_validate_json(payload)


def _is_layer_aligned(value: float, layer_height: float) -> bool:
    steps = value / layer_height
    return abs(steps - round(steps)) * layer_height <= LAYER_ALIGNMENT_TOLERANCE_MM


def _snap_to_layer(value: float, layer: float, *, minimum: float, maximum: float) -> float:
    minimum_steps = math.ceil((minimum - LAYER_ALIGNMENT_TOLERANCE_MM) / layer)
    maximum_steps = math.floor((maximum + LAYER_ALIGNMENT_TOLERANCE_MM) / layer)
    target_steps = math.floor(value / layer + 0.5)
    steps = min(max(target_steps, minimum_steps), maximum_steps)
    return round(steps * layer, 6)


def _format_values(values: tuple[float, ...]) -> str:
    return ", ".join(f"{value:g}" for value in values)
