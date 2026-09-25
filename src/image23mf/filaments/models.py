"""Strict contracts for bundled and future downloaded filament catalogs."""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.storage import FilamentRecord, FilamentRepository

FILAMENT_CATALOG_SCHEMA_VERSION = 1


class FilamentCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FilamentCatalogEntry(FilamentCatalogModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,99}$")
    manufacturer: str = Field(min_length=1, max_length=120)
    family: str = Field(default="", max_length=120)
    name: str = Field(min_length=1, max_length=160)
    hex_color: str = Field(pattern=r"^#[0-9A-F]{6}$")
    material: str = Field(min_length=1, max_length=80)
    finish: str = Field(default="", max_length=120)


class FilamentCatalog(FilamentCatalogModel):
    schema_version: Literal[1] = FILAMENT_CATALOG_SCHEMA_VERSION
    catalog_id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,99}$")
    catalog_version: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=160)
    entries: tuple[FilamentCatalogEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def entry_ids_are_unique(self) -> "FilamentCatalog":
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("filament catalog entry ids must be unique")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FilamentCatalogImport:
    catalog_id: str
    catalog_version: str
    catalog_fingerprint: str
    filaments: tuple[FilamentRecord, ...]


class UnknownCatalogEntryError(ValueError):
    def __init__(self, entry_ids: tuple[str, ...]) -> None:
        self.entry_ids = entry_ids
        super().__init__(f"unknown filament catalog entries: {', '.join(entry_ids)}")


class FilamentCatalogService:
    def __init__(self, catalog: FilamentCatalog) -> None:
        self.catalog = catalog

    @classmethod
    def bundled(cls) -> "FilamentCatalogService":
        resource = files("image23mf.filaments").joinpath("bambu-starter-v1.json")
        return cls(FilamentCatalog.model_validate_json(resource.read_text(encoding="utf-8")))

    def import_entries(
        self,
        repository: FilamentRepository,
        entry_ids: tuple[str, ...],
        *,
        owned: bool = True,
    ) -> FilamentCatalogImport:
        requested = tuple(dict.fromkeys(entry_ids))
        by_id = {entry.id: entry for entry in self.catalog.entries}
        missing = tuple(identifier for identifier in requested if identifier not in by_id)
        if missing:
            raise UnknownCatalogEntryError(missing)
        records = []
        for identifier in requested:
            entry = by_id[identifier]
            payload = entry.model_dump(mode="json")
            payload["id"] = f"filament_{entry.id.replace('.', '_').replace('-', '_')}"
            payload["metadata"] = {
                "catalog_id": self.catalog.catalog_id,
                "catalog_version": self.catalog.catalog_version,
                "catalog_entry_id": entry.id,
            }
            records.append(payload)
        imported = repository.import_records(records, owned=owned)
        return FilamentCatalogImport(
            catalog_id=self.catalog.catalog_id,
            catalog_version=self.catalog.catalog_version,
            catalog_fingerprint=self.catalog.fingerprint(),
            filaments=imported,
        )
