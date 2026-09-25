"""Durable immutable printability catalogs and active-head resolution."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import sqlite3
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from image23mf.calibration.models import (
    PrintabilityProfileCatalog,
    PrintabilityProfileService,
    ResolvedPrintabilitySettings,
    ResolvePrintabilityRequest,
    UnknownPrintabilityCatalogError,
    load_bundled_printability_catalog,
)
from image23mf.storage import open_database
from image23mf.storage.repositories import immediate_transaction


class CalibrationCatalogError(RuntimeError):
    """Retained catalog state is missing, corrupt, or conflicting."""


class CalibrationCatalogConflictError(CalibrationCatalogError):
    """A catalog identity or active-head expectation conflicts."""


class CalibrationCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalibrationCatalogVersionResource(CalibrationCatalogModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog: PrintabilityProfileCatalog
    parent_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: str
    active: bool


class CalibrationCatalogCollection(CalibrationCatalogModel):
    items: tuple[CalibrationCatalogVersionResource, ...]
    active_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class CalibrationCatalogRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure(
        self,
        catalog: PrintabilityProfileCatalog,
        *,
        parent_fingerprint: Optional[str] = None,
        activate_if_empty: bool = True,
    ) -> CalibrationCatalogVersionResource:
        fingerprint = catalog.fingerprint()
        canonical = catalog.canonical_json()
        with immediate_transaction(self.connection):
            row = self.connection.execute(
                "SELECT * FROM calibration_catalog_versions WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                try:
                    self.connection.execute(
                        """
                        INSERT INTO calibration_catalog_versions(
                            fingerprint, catalog_id, catalog_version, catalog_json,
                            parent_fingerprint
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            fingerprint,
                            catalog.catalog_id,
                            catalog.catalog_version,
                            canonical,
                            parent_fingerprint,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise CalibrationCatalogConflictError(
                        "catalog ID/version conflicts with retained immutable content"
                    ) from error
            elif (
                row["catalog_json"] != canonical
                or row["catalog_id"] != catalog.catalog_id
                or row["catalog_version"] != catalog.catalog_version
                or row["parent_fingerprint"] != parent_fingerprint
            ):
                raise CalibrationCatalogConflictError(
                    "catalog fingerprint conflicts with retained immutable content"
                )
            head = self.connection.execute(
                "SELECT fingerprint FROM calibration_catalog_head WHERE singleton = 'active'"
            ).fetchone()
            if head is None and activate_if_empty:
                self.connection.execute(
                    """
                    INSERT INTO calibration_catalog_head(singleton, fingerprint)
                    VALUES ('active', ?)
                    """,
                    (fingerprint,),
                )
        return self.get(fingerprint)

    def get(self, fingerprint: str) -> CalibrationCatalogVersionResource:
        row = self.connection.execute(
            "SELECT * FROM calibration_catalog_versions WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            raise UnknownPrintabilityCatalogError(fingerprint)
        catalog = _catalog_from_row(row)
        head = self.connection.execute(
            "SELECT fingerprint FROM calibration_catalog_head WHERE singleton = 'active'"
        ).fetchone()
        return CalibrationCatalogVersionResource(
            fingerprint=row["fingerprint"],
            catalog=catalog,
            parent_fingerprint=row["parent_fingerprint"],
            created_at=row["created_at"],
            active=head is not None and head["fingerprint"] == row["fingerprint"],
        )

    def active(self) -> CalibrationCatalogVersionResource:
        row = self.connection.execute(
            """
            SELECT versions.* FROM calibration_catalog_head AS head
            JOIN calibration_catalog_versions AS versions
              ON versions.fingerprint = head.fingerprint
            WHERE head.singleton = 'active'
            """
        ).fetchone()
        if row is None:
            raise CalibrationCatalogError("no active calibration catalog is configured")
        catalog = _catalog_from_row(row)
        return CalibrationCatalogVersionResource(
            fingerprint=row["fingerprint"],
            catalog=catalog,
            parent_fingerprint=row["parent_fingerprint"],
            created_at=row["created_at"],
            active=True,
        )

    def list(self) -> CalibrationCatalogCollection:
        active = self.active()
        rows = self.connection.execute(
            "SELECT * FROM calibration_catalog_versions ORDER BY created_at DESC, fingerprint DESC"
        ).fetchall()
        items = tuple(
            CalibrationCatalogVersionResource(
                fingerprint=row["fingerprint"],
                catalog=_catalog_from_row(row),
                parent_fingerprint=row["parent_fingerprint"],
                created_at=row["created_at"],
                active=row["fingerprint"] == active.fingerprint,
            )
            for row in rows
        )
        return CalibrationCatalogCollection(items=items, active_fingerprint=active.fingerprint)


class PersistentPrintabilityProfileService:
    """Resolve active or exactly pinned retained catalogs on every operation."""

    def __init__(
        self,
        database_path: Path,
        *,
        initial_catalog: Optional[PrintabilityProfileCatalog] = None,
    ) -> None:
        self.database_path = database_path
        connection = open_database(database_path)
        try:
            CalibrationCatalogRepository(connection).ensure(
                initial_catalog or load_bundled_printability_catalog()
            )
        finally:
            connection.close()

    @property
    def catalog(self) -> PrintabilityProfileCatalog:
        connection = open_database(self.database_path)
        try:
            return CalibrationCatalogRepository(connection).active().catalog
        finally:
            connection.close()

    def catalog_for(self, fingerprint: str) -> PrintabilityProfileCatalog:
        connection = open_database(self.database_path)
        try:
            return CalibrationCatalogRepository(connection).get(fingerprint).catalog
        finally:
            connection.close()

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
        return PrintabilityProfileService(catalog).resolve(
            request, catalog_fingerprint=catalog.fingerprint()
        )


def _catalog_from_row(row: sqlite3.Row) -> PrintabilityProfileCatalog:
    try:
        catalog = PrintabilityProfileCatalog.model_validate_json(row["catalog_json"])
    except Exception as error:
        raise CalibrationCatalogError("retained calibration catalog JSON is invalid") from error
    if (
        catalog.fingerprint() != row["fingerprint"]
        or catalog.catalog_id != row["catalog_id"]
        or catalog.catalog_version != row["catalog_version"]
    ):
        raise CalibrationCatalogError("retained calibration catalog identity is invalid")
    return catalog
