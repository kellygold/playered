"""HTTP resources for the physical filament library."""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from image23mf.filaments import FilamentCatalog
from image23mf.storage import FilamentRecord


class FilamentApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FilamentResource(FilamentApiModel):
    id: str
    manufacturer: str
    family: str
    name: str
    hex_color: str = Field(pattern=r"^#[0-9A-F]{6}$")
    material: str
    finish: str
    owned: bool
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: FilamentRecord) -> "FilamentResource":
        values = {**record.__dict__, "metadata": dict(record.metadata)}
        return cls(**values)


class FilamentCollection(FilamentApiModel):
    items: tuple[FilamentResource, ...]
    total: int = Field(ge=0)


class CreateFilamentRequest(FilamentApiModel):
    manufacturer: str = Field(min_length=1, max_length=120)
    family: str = Field(default="", max_length=120)
    name: str = Field(min_length=1, max_length=160)
    hex_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    material: str = Field(default="PLA", min_length=1, max_length=80)
    finish: str = Field(default="", max_length=120)
    owned: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateFilamentRequest(FilamentApiModel):
    manufacturer: Optional[str] = Field(default=None, min_length=1, max_length=120)
    family: Optional[str] = Field(default=None, max_length=120)
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    hex_color: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    material: Optional[str] = Field(default=None, min_length=1, max_length=80)
    finish: Optional[str] = Field(default=None, max_length=120)
    owned: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class FilamentCatalogResource(FilamentApiModel):
    catalog: FilamentCatalog
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImportFilamentCatalogRequest(FilamentApiModel):
    entry_ids: tuple[str, ...] = Field(min_length=1)
    owned: bool = True


class FilamentCatalogImportResource(FilamentApiModel):
    catalog_id: str
    catalog_version: str
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    filaments: tuple[FilamentResource, ...]
