"""Optimistically versioned persistence for one mural plan per project."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pydantic import ValidationError

from image23mf.mural.planner import MuralPlan, MuralPlanRequest, build_mural_plan
from image23mf.mural.provenance import MuralProvenanceValidator
from image23mf.storage.blob_store import ContentAddressedStore
from image23mf.storage.repositories import (
    RecordNotFoundError,
    RepositoryError,
    canonical_json,
    immediate_transaction,
)


class StaleMuralPlanError(RepositoryError):
    """A delayed planner save attempted to overwrite a newer generation."""

    def __init__(self, expected_generation: int, current_generation: int) -> None:
        self.expected_generation = expected_generation
        self.current_generation = current_generation
        super().__init__(
            "mural plan generation conflict: "
            f"expected {expected_generation}, current {current_generation}"
        )


@dataclass(frozen=True)
class MuralPlanRecord:
    project_id: str
    schema_version: int
    generation: int
    request: MuralPlanRequest
    plan: MuralPlan
    created_at: str
    updated_at: str


class MuralPlanRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        blob_store: ContentAddressedStore,
    ) -> None:
        self.connection = connection
        self.provenance = MuralProvenanceValidator(connection, blob_store)

    def get(self, project_id: str) -> MuralPlanRecord | None:
        row = self.connection.execute(
            "SELECT * FROM mural_plans WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            request = MuralPlanRequest.model_validate_json(row["request_json"])
            plan = MuralPlan.model_validate_json(row["plan_json"])
        except (ValidationError, ValueError) as error:
            raise RepositoryError(f"stored mural plan is invalid: {project_id}") from error
        if row["schema_version"] != request.schema_version:
            raise RepositoryError("stored mural plan schema version does not match its request")
        if row["source_asset_id"] != request.source.source_asset_id:
            raise RepositoryError("stored mural plan source identity does not match its request")
        if row["source_fingerprint"] != plan.source_fingerprint:
            raise RepositoryError("stored mural plan source fingerprint does not match its plan")
        if row["request_fingerprint"] != plan.request_fingerprint:
            raise RepositoryError("stored mural plan request fingerprint does not match its plan")
        return MuralPlanRecord(
            project_id=row["project_id"],
            schema_version=row["schema_version"],
            generation=row["generation"],
            request=request,
            plan=plan,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save(
        self,
        project_id: str,
        request: MuralPlanRequest,
        *,
        expected_generation: int,
    ) -> MuralPlanRecord:
        if expected_generation < 0:
            raise ValueError("expected mural plan generation cannot be negative")
        plan = build_mural_plan(request)
        request_json = canonical_json(request.model_dump(mode="json"))
        plan_json = canonical_json(plan.model_dump(mode="json"))
        with immediate_transaction(self.connection):
            self._validate_project_source(project_id, request)
            current = self.connection.execute(
                "SELECT generation FROM mural_plans WHERE project_id = ?", (project_id,)
            ).fetchone()
            current_generation = 0 if current is None else int(current["generation"])
            if expected_generation != current_generation:
                raise StaleMuralPlanError(expected_generation, current_generation)
            if current is None:
                self.connection.execute(
                    """
                    INSERT INTO mural_plans(
                        project_id, schema_version, generation, source_asset_id,
                        source_fingerprint, request_fingerprint, request_json, plan_json
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        request.schema_version,
                        request.source.source_asset_id,
                        plan.source_fingerprint,
                        plan.request_fingerprint,
                        request_json,
                        plan_json,
                    ),
                )
            else:
                changed = self.connection.execute(
                    """
                    UPDATE mural_plans
                    SET schema_version = ?, generation = generation + 1,
                        source_asset_id = ?, source_fingerprint = ?, request_fingerprint = ?,
                        request_json = ?, plan_json = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE project_id = ? AND generation = ?
                    """,
                    (
                        request.schema_version,
                        request.source.source_asset_id,
                        plan.source_fingerprint,
                        plan.request_fingerprint,
                        request_json,
                        plan_json,
                        project_id,
                        expected_generation,
                    ),
                ).rowcount
                if changed != 1:  # pragma: no cover - the write lock preserves the checked row
                    raise StaleMuralPlanError(expected_generation, current_generation)
            self.connection.execute(
                """
                UPDATE projects
                SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (project_id,),
            )
        record = self.get(project_id)
        if record is None:  # pragma: no cover - committed insert/update guarantees the row
            raise RepositoryError(f"mural plan disappeared after save: {project_id}")
        return record

    def delete(self, project_id: str, *, expected_generation: int) -> bool:
        if expected_generation < 1:
            raise ValueError("an existing mural plan generation must be positive")
        with immediate_transaction(self.connection):
            current = self.connection.execute(
                "SELECT generation FROM mural_plans WHERE project_id = ?", (project_id,)
            ).fetchone()
            if current is None:
                return False
            current_generation = int(current["generation"])
            if expected_generation != current_generation:
                raise StaleMuralPlanError(expected_generation, current_generation)
            self.connection.execute("DELETE FROM mural_plans WHERE project_id = ?", (project_id,))
            self.connection.execute(
                """
                UPDATE projects
                SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (project_id,),
            )
        return True

    def _validate_project_source(
        self,
        project_id: str,
        request: MuralPlanRequest,
    ) -> None:
        project = self.connection.execute(
            "SELECT id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise RecordNotFoundError(f"project not found: {project_id}")
        self.provenance.validate(project_id, request)
