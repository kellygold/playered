"""Versioned printability calibration profiles and visible override resolution."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
from enum import Enum
from importlib.resources import files
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

PRINTABILITY_PROFILE_SCHEMA_VERSION = 1


class CalibrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CalibrationEvidenceStatus(str, Enum):
    PENDING_PRINT_CALIBRATION = "pending_print_calibration"
    PARTIALLY_VALIDATED = "partially_validated"
    PRINT_VALIDATED = "print_validated"


class CalibrationBasis(str, Enum):
    ENGINEERING_BASELINE = "engineering_baseline"
    PRINTED_CALIBRATION = "printed_calibration"


class CalibrationConfidence(str, Enum):
    PROVISIONAL = "provisional"
    MODERATE = "moderate"
    HIGH = "high"


class RecommendationUnit(str, Enum):
    MILLIMETRES = "mm"
    SQUARE_MILLIMETRES = "mm2"


class CalibrationFeatureKind(str, Enum):
    DOT = "dot"
    HOLE = "hole"
    LINE = "line"
    NECK = "neck"
    GAP = "gap"


class CalibrationCatalogSource(CalibrationModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=80)
    captured_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    method: str = Field(min_length=1, max_length=1000)
    references: tuple[str, ...] = Field(min_length=1)


class CalibrationRecommendation(CalibrationModel):
    value: float = Field(ge=0, le=1000)
    unit: RecommendationUnit
    basis: CalibrationBasis
    confidence: CalibrationConfidence
    rationale: str = Field(min_length=1, max_length=1000)
    evidence_feature_kinds: tuple[CalibrationFeatureKind, ...] = Field(min_length=1)
    evidence_run_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def evidence_is_canonical(self) -> CalibrationRecommendation:
        if self.evidence_feature_kinds != tuple(
            sorted(set(self.evidence_feature_kinds), key=lambda item: item.value)
        ):
            raise ValueError("recommendation feature kinds must be unique and canonical")
        if self.evidence_run_ids != tuple(sorted(set(self.evidence_run_ids))):
            raise ValueError("recommendation run IDs must be unique and canonical")
        if self.basis == CalibrationBasis.PRINTED_CALIBRATION and not self.evidence_run_ids:
            raise ValueError("printed-calibration recommendations require evidence run IDs")
        return self


class PrintabilityRecommendations(CalibrationModel):
    minimum_island_area_mm2: CalibrationRecommendation
    minimum_island_diameter_mm: CalibrationRecommendation
    maximum_tiny_hole_area_mm2: CalibrationRecommendation
    maximum_tiny_hole_diameter_mm: CalibrationRecommendation
    minimum_ring_width_mm: CalibrationRecommendation
    minimum_line_width_mm: CalibrationRecommendation
    minimum_neck_width_mm: CalibrationRecommendation
    minimum_gap_width_mm: CalibrationRecommendation
    long_line_minimum_length_mm: CalibrationRecommendation
    smoothing_radius_mm: CalibrationRecommendation

    @model_validator(mode="after")
    def units_match_recommendation_names(self) -> PrintabilityRecommendations:
        for name in RECOMMENDATION_ORDER:
            expected = _RECOMMENDATION_UNITS[name]
            if getattr(self, name).unit != expected:
                raise ValueError(f"{name} must use {expected.value}")
        return self


class CalibrationSweep(CalibrationModel):
    dot_diameters_mm: tuple[float, ...] = Field(min_length=3, max_length=20)
    hole_diameters_mm: tuple[float, ...] = Field(min_length=3, max_length=20)
    line_widths_mm: tuple[float, ...] = Field(min_length=3, max_length=20)
    neck_widths_mm: tuple[float, ...] = Field(min_length=3, max_length=20)
    gap_widths_mm: tuple[float, ...] = Field(min_length=3, max_length=20)

    @model_validator(mode="after")
    def sweeps_are_positive_sorted_and_aligned(self) -> CalibrationSweep:
        lengths = set()
        for name in SWEEP_ORDER:
            values = getattr(self, name)
            lengths.add(len(values))
            if any(value <= 0 for value in values):
                raise ValueError("calibration sweep values must be positive")
            if values != tuple(sorted(set(values))):
                raise ValueError("calibration sweep values must be unique and sorted")
        if len(lengths) != 1:
            raise ValueError("calibration sweep rows must have the same number of samples")
        return self

    @property
    def sample_count(self) -> int:
        return len(self.dot_diameters_mm)


class PrintabilityProfile(CalibrationModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,119}$")
    display_name: str = Field(min_length=1, max_length=200)
    printer_id: str = Field(min_length=1, max_length=120)
    nozzle_id: str = Field(min_length=1, max_length=120)
    nozzle_diameter_mm: float = Field(gt=0, le=2)
    material_class: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    evidence_status: CalibrationEvidenceStatus
    reviewed_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    sweep: CalibrationSweep
    recommendations: PrintabilityRecommendations

    @model_validator(mode="after")
    def evidence_status_matches_recommendations(self) -> PrintabilityProfile:
        recommendations = [getattr(self.recommendations, name) for name in RECOMMENDATION_ORDER]
        if self.evidence_status == CalibrationEvidenceStatus.PRINT_VALIDATED and any(
            item.basis != CalibrationBasis.PRINTED_CALIBRATION for item in recommendations
        ):
            raise ValueError("print-validated profiles require printed-calibration recommendations")
        return self


class PrintabilityProfileCatalog(CalibrationModel):
    schema_version: Literal[1] = PRINTABILITY_PROFILE_SCHEMA_VERSION
    catalog_id: str = Field(min_length=1, max_length=120)
    catalog_version: str = Field(min_length=1, max_length=80)
    source: CalibrationCatalogSource
    profiles: tuple[PrintabilityProfile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def profile_keys_are_unique(self) -> PrintabilityProfileCatalog:
        ids = [profile.id for profile in self.profiles]
        keys = [
            (profile.printer_id, profile.nozzle_id, profile.material_class)
            for profile in self.profiles
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("printability profile IDs must be unique")
        if len(keys) != len(set(keys)):
            raise ValueError("printer, nozzle, and material profile keys must be unique")
        return self

    def profile(self, profile_id: str) -> Optional[PrintabilityProfile]:
        return next((item for item in self.profiles if item.id == profile_id), None)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class PrintabilityOverrides(CalibrationModel):
    minimum_island_area_mm2: Optional[float] = Field(default=None, ge=0, le=25)
    minimum_island_diameter_mm: Optional[float] = Field(default=None, ge=0, le=10)
    maximum_tiny_hole_area_mm2: Optional[float] = Field(default=None, ge=0, le=25)
    maximum_tiny_hole_diameter_mm: Optional[float] = Field(default=None, ge=0, le=10)
    minimum_ring_width_mm: Optional[float] = Field(default=None, ge=0, le=10)
    minimum_line_width_mm: Optional[float] = Field(default=None, ge=0, le=10)
    minimum_neck_width_mm: Optional[float] = Field(default=None, ge=0, le=10)
    minimum_gap_width_mm: Optional[float] = Field(default=None, ge=0, le=10)
    long_line_minimum_length_mm: Optional[float] = Field(default=None, ge=0, le=1000)
    smoothing_radius_mm: Optional[float] = Field(default=None, ge=0, le=25)


class ResolvePrintabilityRequest(CalibrationModel):
    printer_id: str = Field(min_length=1, max_length=120)
    nozzle_id: str = Field(min_length=1, max_length=120)
    material_class: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$")
    catalog_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    overrides: PrintabilityOverrides = PrintabilityOverrides()


class ResolvedRecommendation(CalibrationModel):
    name: str
    value: float = Field(ge=0)
    unit: RecommendationUnit
    source: Literal["profile", "user_override"]
    profile_value: float = Field(ge=0)
    profile_basis: CalibrationBasis
    profile_confidence: CalibrationConfidence
    rationale: str = Field(min_length=1, max_length=2000)


class ResolvedPrintabilitySettings(CalibrationModel):
    profile_id: str
    profile_display_name: str
    profile_catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_status: CalibrationEvidenceStatus
    warning: Optional[str]
    values: tuple[ResolvedRecommendation, ...]

    @model_validator(mode="after")
    def values_use_contract_order(self) -> ResolvedPrintabilitySettings:
        if tuple(item.name for item in self.values) != RECOMMENDATION_ORDER:
            raise ValueError("resolved recommendations must use contract order")
        return self

    def value(self, name: str) -> ResolvedRecommendation:
        match = next((item for item in self.values if item.name == name), None)
        if match is None:
            raise KeyError(name)
        return match


class UnknownPrintabilityProfileError(ValueError):
    def __init__(self, request: ResolvePrintabilityRequest, available_profile_ids: tuple[str, ...]):
        self.request = request
        self.available_profile_ids = available_profile_ids
        super().__init__(
            "No printability profile matches "
            f"{request.printer_id}/{request.nozzle_id}/{request.material_class}."
        )


class UnknownPrintabilityCatalogError(ValueError):
    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint
        super().__init__(f"Unknown retained printability catalog: {fingerprint}.")


class PrintabilityProfileService:
    def __init__(self, catalog: PrintabilityProfileCatalog) -> None:
        self.catalog = catalog

    @classmethod
    def bundled(cls) -> PrintabilityProfileService:
        return cls(load_bundled_printability_catalog())

    def catalog_for(self, fingerprint: str) -> PrintabilityProfileCatalog:
        if fingerprint != self.catalog.fingerprint():
            raise UnknownPrintabilityCatalogError(fingerprint)
        return self.catalog

    def resolve(
        self,
        request: ResolvePrintabilityRequest,
        *,
        catalog_fingerprint: Optional[str] = None,
    ) -> ResolvedPrintabilitySettings:
        if (
            catalog_fingerprint is not None
            and request.catalog_fingerprint is not None
            and catalog_fingerprint != request.catalog_fingerprint
        ):
            raise ValueError("conflicting printability catalog fingerprints")
        catalog_fingerprint = catalog_fingerprint or request.catalog_fingerprint
        catalog = (
            self.catalog if catalog_fingerprint is None else self.catalog_for(catalog_fingerprint)
        )
        profile = next(
            (
                item
                for item in catalog.profiles
                if (
                    item.printer_id,
                    item.nozzle_id,
                    item.material_class,
                )
                == (request.printer_id, request.nozzle_id, request.material_class)
            ),
            None,
        )
        if profile is None:
            raise UnknownPrintabilityProfileError(
                request,
                tuple(item.id for item in catalog.profiles),
            )
        values = []
        for name in RECOMMENDATION_ORDER:
            recommendation = getattr(profile.recommendations, name)
            override = getattr(request.overrides, name)
            if override is None:
                value = recommendation.value
                source: Literal["profile", "user_override"] = "profile"
                rationale = recommendation.rationale
            else:
                value = override
                source = "user_override"
                rationale = (
                    f"User override {override:g} {recommendation.unit.value} replaces the "
                    f"profile value {recommendation.value:g} {recommendation.unit.value}. "
                    f"Profile rationale: {recommendation.rationale}"
                )
            values.append(
                ResolvedRecommendation(
                    name=name,
                    value=value,
                    unit=recommendation.unit,
                    source=source,
                    profile_value=recommendation.value,
                    profile_basis=recommendation.basis,
                    profile_confidence=recommendation.confidence,
                    rationale=rationale,
                )
            )
        warning = None
        if profile.evidence_status != CalibrationEvidenceStatus.PRINT_VALIDATED:
            warning = (
                "These defaults are provisional engineering baselines pending a recorded local "
                "print calibration. Review the preview and override any value when needed."
            )
        return ResolvedPrintabilitySettings(
            profile_id=profile.id,
            profile_display_name=profile.display_name,
            profile_catalog_fingerprint=catalog.fingerprint(),
            evidence_status=profile.evidence_status,
            warning=warning,
            values=tuple(values),
        )


RECOMMENDATION_ORDER = (
    "minimum_island_area_mm2",
    "minimum_island_diameter_mm",
    "maximum_tiny_hole_area_mm2",
    "maximum_tiny_hole_diameter_mm",
    "minimum_ring_width_mm",
    "minimum_line_width_mm",
    "minimum_neck_width_mm",
    "minimum_gap_width_mm",
    "long_line_minimum_length_mm",
    "smoothing_radius_mm",
)

_RECOMMENDATION_UNITS = {
    "minimum_island_area_mm2": RecommendationUnit.SQUARE_MILLIMETRES,
    "minimum_island_diameter_mm": RecommendationUnit.MILLIMETRES,
    "maximum_tiny_hole_area_mm2": RecommendationUnit.SQUARE_MILLIMETRES,
    "maximum_tiny_hole_diameter_mm": RecommendationUnit.MILLIMETRES,
    "minimum_ring_width_mm": RecommendationUnit.MILLIMETRES,
    "minimum_line_width_mm": RecommendationUnit.MILLIMETRES,
    "minimum_neck_width_mm": RecommendationUnit.MILLIMETRES,
    "minimum_gap_width_mm": RecommendationUnit.MILLIMETRES,
    "long_line_minimum_length_mm": RecommendationUnit.MILLIMETRES,
    "smoothing_radius_mm": RecommendationUnit.MILLIMETRES,
}

SWEEP_ORDER = (
    "dot_diameters_mm",
    "hole_diameters_mm",
    "line_widths_mm",
    "neck_widths_mm",
    "gap_widths_mm",
)


def load_bundled_printability_catalog() -> PrintabilityProfileCatalog:
    path = files("image23mf.calibration").joinpath("printability-v1.json")
    return PrintabilityProfileCatalog.model_validate_json(path.read_text(encoding="utf-8"))
