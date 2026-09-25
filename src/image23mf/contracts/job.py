import hashlib
import json
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

CURRENT_JOB_SCHEMA_VERSION = 1


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CropMode(str, Enum):
    CONTAIN = "contain"
    COVER = "cover"
    STRETCH = "stretch"
    EXTEND = "extend"


class MergePolicy(str, Enum):
    REVIEW = "review"
    KEEP = "keep"
    DOMINANT_NEIGHBOR = "dominant_neighbor"
    PERCEPTUAL_NEIGHBOR = "perceptual_neighbor"


class CleanupOverrideField(str, Enum):
    MIN_ISLAND_MM2 = "min_island_mm2"
    MINIMUM_ISLAND_DIAMETER_MM = "minimum_island_diameter_mm"
    MAX_HOLE_MM2 = "max_hole_mm2"
    MAXIMUM_TINY_HOLE_DIAMETER_MM = "maximum_tiny_hole_diameter_mm"
    MINIMUM_RING_WIDTH_MM = "minimum_ring_width_mm"
    MINIMUM_LINE_WIDTH_MM = "minimum_line_width_mm"
    MINIMUM_NECK_WIDTH_MM = "minimum_neck_width_mm"
    MINIMUM_GAP_WIDTH_MM = "minimum_gap_width_mm"
    LONG_LINE_MINIMUM_LENGTH_MM = "long_line_minimum_length_mm"
    SMOOTHING_RADIUS_MM = "smoothing_radius_mm"


class ArtStyle(str, Enum):
    FLUSH_INLAY = "flush_inlay"
    RAISED = "raised"


class CanvasConfig(StrictModel):
    width_mm: float = Field(ge=20, le=1000)
    height_mm: float = Field(ge=20, le=1000)


class CropConfig(StrictModel):
    mode: CropMode = CropMode.COVER
    x: float = Field(default=0, ge=0, le=1)
    y: float = Field(default=0, ge=0, le=1)
    width: float = Field(default=1, gt=0, le=1)
    height: float = Field(default=1, gt=0, le=1)

    @model_validator(mode="after")
    def crop_stays_inside_source(self) -> "CropConfig":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("crop bounds must remain inside the normalized source")
        return self


class PrinterConfig(StrictModel):
    profile_catalog_id: str = "image23mf-bundled-printers"
    profile_catalog_version: str = "2026.07.16"
    printer_id: str = "bambu-p2s"
    nozzle_id: str = "nozzle-0.4-hardened-steel"
    nozzle_mm: float = Field(default=0.4, gt=0, le=2)
    layer_height_mm: float = Field(default=0.2, gt=0, le=0.4)
    plate_id: str = "textured-pei"

    @model_validator(mode="after")
    def layer_height_matches_nozzle(self) -> "PrinterConfig":
        if self.layer_height_mm > self.nozzle_mm:
            raise ValueError("layer height cannot exceed nozzle diameter")
        return self


class PaletteColor(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    locked: bool = False
    filament_id: Optional[str] = None


class PaletteConfig(StrictModel):
    colors: tuple[PaletteColor, ...] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def color_ids_are_unique(self) -> "PaletteConfig":
        ids = [color.id for color in self.colors]
        if len(ids) != len(set(ids)):
            raise ValueError("palette color ids must be unique")
        return self


class CleanupConfig(StrictModel):
    min_island_mm2: float = Field(default=0.3, ge=0, le=25)
    minimum_island_diameter_mm: Optional[float] = Field(default=None, ge=0, le=10)
    max_hole_mm2: float = Field(default=0.5, ge=0, le=25)
    maximum_tiny_hole_diameter_mm: Optional[float] = Field(default=None, ge=0, le=10)
    minimum_ring_width_mm: Optional[float] = Field(default=None, ge=0, le=10)
    minimum_line_width_mm: Optional[float] = Field(default=None, ge=0, le=10)
    minimum_neck_width_mm: Optional[float] = Field(default=None, ge=0, le=10)
    minimum_gap_width_mm: Optional[float] = Field(default=None, ge=0, le=10)
    long_line_minimum_length_mm: Optional[float] = Field(default=None, ge=0, le=1000)
    smoothing_radius_mm: float = Field(default=0, ge=0, le=5)
    merge_policy: MergePolicy = MergePolicy.REVIEW
    preserve_long_lines: bool = True
    printability_profile_id: Optional[str] = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9.-]{1,119}$",
    )
    printability_profile_catalog_fingerprint: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    override_fields: tuple[CleanupOverrideField, ...] = ()

    @model_validator(mode="after")
    def overrides_are_canonical(self) -> "CleanupConfig":
        if self.override_fields != tuple(
            sorted(set(self.override_fields), key=lambda item: item.value)
        ):
            raise ValueError("cleanup override fields must be unique and canonical")
        missing_values = [
            item.value for item in self.override_fields if getattr(self, item.value) is None
        ]
        if missing_values:
            raise ValueError(
                f"cleanup override fields require numeric values: {', '.join(missing_values)}"
            )
        if (self.printability_profile_id is None) != (
            self.printability_profile_catalog_fingerprint is None
        ):
            raise ValueError("cleanup printability profile identity must be complete")
        return self


class GeometryConfig(StrictModel):
    style: ArtStyle = ArtStyle.FLUSH_INLAY
    base_thickness_mm: float = Field(default=1.2, gt=0, le=10)
    art_thickness_mm: float = Field(default=0.6, gt=0, le=5)
    corner_radius_mm: float = Field(default=0, ge=0, le=100)


class JobConfig(StrictModel):
    schema_version: Literal[1] = CURRENT_JOB_SCHEMA_VERSION
    source_asset_id: str = Field(min_length=1, max_length=128)
    canvas: CanvasConfig
    crop: CropConfig = CropConfig()
    printer: PrinterConfig = PrinterConfig()
    palette: PaletteConfig
    cleanup: CleanupConfig = CleanupConfig()
    geometry: GeometryConfig = GeometryConfig()

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _migrate_v0(payload: dict[str, Any]) -> dict[str, Any]:
    colors = payload.get("palette", [])
    migrated_colors = [
        {
            "id": f"color-{index + 1}",
            "name": f"Color {index + 1}",
            "hex": color,
            "locked": False,
            "filament_id": None,
        }
        for index, color in enumerate(colors)
    ]
    nozzle_mm = float(payload.get("nozzle_mm", 0.4))
    return {
        "schema_version": 1,
        "source_asset_id": payload["source_asset_id"],
        "canvas": {
            "width_mm": payload["width_mm"],
            "height_mm": payload["height_mm"],
        },
        "crop": {"mode": "cover", "x": 0, "y": 0, "width": 1, "height": 1},
        "printer": {
            "profile_catalog_id": "image23mf-bundled-printers",
            "profile_catalog_version": "2026.07.16",
            "printer_id": "bambu-p2s",
            "nozzle_id": f"nozzle-{nozzle_mm:g}-hardened-steel",
            "nozzle_mm": nozzle_mm,
            "layer_height_mm": payload.get("layer_height_mm", 0.2),
            "plate_id": "textured-pei",
        },
        "palette": {"colors": migrated_colors},
        "cleanup": {
            "min_island_mm2": 0.3,
            "max_hole_mm2": 0.5,
            "smoothing_radius_mm": 0,
            "merge_policy": "review",
            "preserve_long_lines": True,
        },
        "geometry": {
            "style": "flush_inlay",
            "base_thickness_mm": 1.2,
            "art_thickness_mm": 0.6,
            "corner_radius_mm": 0,
        },
    }


def _enrich_v1_profile_fields(payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    printer = dict(enriched.get("printer", {}))
    nozzle_mm = float(printer.get("nozzle_mm", 0.4))
    printer.setdefault("profile_catalog_id", "image23mf-bundled-printers")
    printer.setdefault("profile_catalog_version", "2026.07.16")
    printer.setdefault("nozzle_id", f"nozzle-{nozzle_mm:g}-hardened-steel")
    enriched["printer"] = printer
    return enriched


def load_job_config(payload: dict[str, Any]) -> JobConfig:
    version = payload.get("schema_version", 0)
    if version == 0:
        payload = _migrate_v0(payload)
    elif version == CURRENT_JOB_SCHEMA_VERSION:
        payload = _enrich_v1_profile_fields(payload)
    elif version != CURRENT_JOB_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported job schema version {version}; expected <= {CURRENT_JOB_SCHEMA_VERSION}"
        )
    return JobConfig.model_validate(payload)
