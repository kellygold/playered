"""Typed repositories and atomic revision publication."""

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Callable, Optional

from image23mf.contracts.job import JobConfig, load_job_config
from image23mf.contracts.provenance import validate_operation_provenance
from image23mf.derivation import DerivationIdentity, explain_staleness
from image23mf.storage.blob_store import ContentAddressedStore, StoredBlob
from image23mf.storage.history import (
    DraftHistoryBoundaryError,
    DraftHistoryConflictError,
    DraftHistoryRepository,
    mutation_payload_sha256,
)
from image23mf.storage.models import (
    ArtifactPublication,
    ArtifactRecord,
    AssetRecord,
    BranchedRevision,
    DraftHistoryCommand,
    DraftRecord,
    FilamentRecord,
    JsonObject,
    ProjectRecord,
    PublishedRevision,
    RegionOperation,
    RegionOperationRecord,
    RevisionPublicationRecord,
    RevisionRecord,
    RevisionSummaryRecord,
)


class RepositoryError(RuntimeError):
    """Base class for typed persistence failures."""


class RecordNotFoundError(RepositoryError):
    """A requested project, asset, revision, or artifact does not exist."""


class InvalidPublicationError(RepositoryError):
    """A revision publication violates a domain/storage invariant."""


class MissingBlobError(RepositoryError):
    """A database publication references bytes that are absent or corrupted."""


class StaleDraftError(RepositoryError):
    """A delayed autosave attempted to replace a newer draft generation."""


class PreviewEvidenceConflictError(RepositoryError):
    """A guarded publication named preview evidence that is not current and exact."""

    def __init__(self, reason: str, *, preview_job_id: Optional[str] = None) -> None:
        self.reason = reason
        self.preview_job_id = preview_job_id
        super().__init__(reason)


class InvalidDraftHistoryError(RepositoryError):
    """A history mutation crossed a boundary or reused stale identity."""


class DraftHistoryBoundaryReachedError(RepositoryError):
    """Undo or redo was requested beyond the active history horizon."""


class DuplicateFilamentError(RepositoryError):
    """A filament already exists with the same printable identity."""


class FilamentInUseError(RepositoryError):
    """A filament cannot be deleted while a saved palette references it."""


class InvalidFilamentReferenceError(RepositoryError):
    """A job configuration references filament records that do not exist."""

    def __init__(self, missing_ids: Sequence[str]) -> None:
        self.missing_ids = tuple(sorted(set(missing_ids)))
        super().__init__(f"unknown filament references: {', '.join(self.missing_ids)}")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_object(raw: str, *, field_name: str) -> JsonObject:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RepositoryError(f"stored {field_name} must be a JSON object")
    return value


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run one non-nested atomic write transaction.

    `BEGIN IMMEDIATE` reserves the writer before validation/insertion while WAL readers continue
    to see a consistent prior snapshot.
    """

    if connection.in_transaction:
        raise RepositoryError("nested repository transactions are not supported")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except Exception:
        connection.rollback()
        raise


class ProjectRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(
        self,
        name: str,
        *,
        description: str = "",
        preferences: Optional[Mapping[str, Any]] = None,
        project_id: Optional[str] = None,
    ) -> ProjectRecord:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("project name cannot be empty")
        identifier = project_id or new_id("project")
        with immediate_transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO projects(id, name, description, preferences_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    identifier,
                    normalized_name,
                    description,
                    canonical_json(preferences or {}),
                ),
            )
        return self.get(identifier)

    def get(self, project_id: str) -> ProjectRecord:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"project not found: {project_id}")
        return _project_from_row(row)

    def list(self, *, include_archived: bool = False) -> tuple[ProjectRecord, ...]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        rows = self.connection.execute(
            f"SELECT * FROM projects {where} ORDER BY updated_at DESC, id"  # noqa: S608
        ).fetchall()
        return tuple(_project_from_row(row) for row in rows)

    def set_archived(self, project_id: str, *, archived: bool) -> ProjectRecord:
        archived_at = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')" if archived else "NULL"
        with immediate_transaction(self.connection):
            changed = self.connection.execute(
                f"""
                UPDATE projects
                SET archived_at = {archived_at},
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,  # noqa: S608 - SQL fragment is selected from two constants above.
                (project_id,),
            ).rowcount
            if changed != 1:
                raise RecordNotFoundError(f"project not found: {project_id}")
        return self.get(project_id)


class AssetRepository:
    def __init__(self, connection: sqlite3.Connection, blob_store: ContentAddressedStore) -> None:
        self.connection = connection
        self.blob_store = blob_store

    def register(
        self,
        blob: StoredBlob,
        *,
        original_filename: str,
        width_px: Optional[int] = None,
        height_px: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        asset_id: Optional[str] = None,
    ) -> AssetRecord:
        if not original_filename.strip():
            raise ValueError("original filename cannot be empty")
        if (width_px is None) != (height_px is None):
            raise ValueError("image width and height must be provided together")
        if width_px is not None and (width_px <= 0 or height_px is None or height_px <= 0):
            raise ValueError("image dimensions must be positive")
        _require_namespace(blob, "assets")
        _verify_blob(self.blob_store, blob)

        existing = self.find_by_sha256(blob.sha256)
        if existing is not None:
            return existing

        identifier = asset_id or new_id("asset")
        try:
            with immediate_transaction(self.connection):
                self.connection.execute(
                    """
                    INSERT INTO assets(
                        id, sha256, media_type, extension, original_filename, relative_path,
                        byte_size, width_px, height_px, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        blob.sha256,
                        blob.media_type,
                        blob.extension,
                        original_filename.strip(),
                        blob.relative_path,
                        blob.byte_size,
                        width_px,
                        height_px,
                        canonical_json(metadata or {}),
                    ),
                )
        except sqlite3.IntegrityError:
            # A concurrent importer may have registered identical bytes after our initial read.
            existing = self.find_by_sha256(blob.sha256)
            if existing is None:
                raise
            return existing
        return self.get(identifier)

    def get(self, asset_id: str) -> AssetRecord:
        row = self.connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError(f"asset not found: {asset_id}")
        return _asset_from_row(row)

    def find_by_sha256(self, sha256: str) -> Optional[AssetRecord]:
        row = self.connection.execute("SELECT * FROM assets WHERE sha256 = ?", (sha256,)).fetchone()
        return None if row is None else _asset_from_row(row)


class FilamentRepository:
    """Owned and custom physical filament records with stable palette references."""

    EDITABLE_FIELDS = frozenset(
        {"manufacturer", "family", "name", "hex_color", "material", "finish", "owned", "metadata"}
    )

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(
        self,
        *,
        manufacturer: str,
        name: str,
        hex_color: str,
        family: str = "",
        material: str = "PLA",
        finish: str = "",
        owned: bool = False,
        metadata: Optional[Mapping[str, Any]] = None,
        filament_id: Optional[str] = None,
    ) -> FilamentRecord:
        values = _normalize_filament_values(
            {
                "manufacturer": manufacturer,
                "family": family,
                "name": name,
                "hex_color": hex_color,
                "material": material,
                "finish": finish,
                "owned": owned,
                "metadata": metadata or {},
            }
        )
        identifier = filament_id or new_id("filament")
        try:
            with immediate_transaction(self.connection):
                if self._find_identity(values) is not None:
                    raise DuplicateFilamentError("that filament color already exists")
                self._insert_locked(identifier, values)
        except sqlite3.IntegrityError as error:
            if self._find_identity(values) is not None:
                raise DuplicateFilamentError("that filament color already exists") from error
            raise
        return self.get(identifier)

    def get(self, filament_id: str) -> FilamentRecord:
        row = self.connection.execute(
            "SELECT * FROM filaments WHERE id = ?", (filament_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"filament not found: {filament_id}")
        return _filament_from_row(row)

    def list(
        self,
        *,
        owned: Optional[bool] = None,
        query: str = "",
        manufacturer: str = "",
        material: str = "",
    ) -> tuple[FilamentRecord, ...]:
        clauses = []
        parameters: list[Any] = []
        if owned is not None:
            clauses.append("owned = ?")
            parameters.append(int(owned))
        if manufacturer.strip():
            clauses.append("lower(manufacturer) = lower(?)")
            parameters.append(manufacturer.strip())
        if material.strip():
            clauses.append("lower(material) = lower(?)")
            parameters.append(material.strip())
        if query.strip():
            clauses.append(
                "lower(manufacturer || ' ' || family || ' ' || name || ' ' || "
                "material || ' ' || finish) LIKE ?"
            )
            parameters.append(f"%{query.strip().lower()}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT * FROM filaments {where}
            ORDER BY owned DESC, lower(manufacturer), lower(family), lower(name), hex_color, id
            """,  # noqa: S608
            tuple(parameters),
        ).fetchall()
        return tuple(_filament_from_row(row) for row in rows)

    def update(self, filament_id: str, changes: Mapping[str, Any]) -> FilamentRecord:
        unknown = set(changes) - self.EDITABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported filament fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get(filament_id)
        current = self.get(filament_id)
        merged = {
            "manufacturer": current.manufacturer,
            "family": current.family,
            "name": current.name,
            "hex_color": current.hex_color,
            "material": current.material,
            "finish": current.finish,
            "owned": current.owned,
            "metadata": current.metadata,
            **dict(changes),
        }
        values = _normalize_filament_values(merged)
        try:
            with immediate_transaction(self.connection):
                duplicate = self._find_identity(values)
                if duplicate is not None and duplicate.id != filament_id:
                    raise DuplicateFilamentError("that filament color already exists")
                changed = self.connection.execute(
                    """
                    UPDATE filaments
                    SET manufacturer = ?, family = ?, name = ?, hex_color = ?, material = ?,
                        finish = ?, owned = ?, metadata_json = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    (*_filament_sql_values(values), filament_id),
                ).rowcount
                if changed != 1:
                    raise RecordNotFoundError(f"filament not found: {filament_id}")
        except sqlite3.IntegrityError as error:
            duplicate = self._find_identity(values)
            if duplicate is not None and duplicate.id != filament_id:
                raise DuplicateFilamentError("that filament color already exists") from error
            raise
        return self.get(filament_id)

    def delete(self, filament_id: str) -> None:
        with immediate_transaction(self.connection):
            self.get(filament_id)
            references = _filament_reference_locations(self.connection, filament_id)
            if references:
                raise FilamentInUseError(
                    f"filament is referenced by saved palettes: {', '.join(references)}"
                )
            self.connection.execute("DELETE FROM filaments WHERE id = ?", (filament_id,))

    def import_records(
        self, records: Sequence[Mapping[str, Any]], *, owned: bool = True
    ) -> tuple[FilamentRecord, ...]:
        normalized = []
        for record in records:
            values = _normalize_filament_values({**dict(record), "owned": owned})
            normalized.append((record.get("id"), values))
        imported_ids = []
        with immediate_transaction(self.connection):
            for preferred_id, values in normalized:
                existing = self._find_identity(values)
                if existing is not None:
                    merged_metadata = {**dict(existing.metadata), **dict(values["metadata"])}
                    self.connection.execute(
                        """
                        UPDATE filaments SET owned = ?, metadata_json = ?,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE id = ?
                        """,
                        (
                            int(owned or existing.owned),
                            canonical_json(merged_metadata),
                            existing.id,
                        ),
                    )
                    imported_ids.append(existing.id)
                    continue
                identifier = str(preferred_id or new_id("filament"))
                if self.connection.execute(
                    "SELECT 1 FROM filaments WHERE id = ?", (identifier,)
                ).fetchone():
                    identifier = new_id("filament")
                self._insert_locked(identifier, values)
                imported_ids.append(identifier)
        return tuple(self.get(identifier) for identifier in imported_ids)

    def _insert_locked(self, identifier: str, values: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO filaments(
                id, manufacturer, family, name, hex_color, material, finish, owned, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (identifier, *_filament_sql_values(values)),
        )

    def _find_identity(self, values: Mapping[str, Any]) -> Optional[FilamentRecord]:
        row = self.connection.execute(
            """
            SELECT * FROM filaments
            WHERE lower(manufacturer) = lower(?) AND lower(family) = lower(?)
                AND lower(name) = lower(?) AND hex_color = ?
            """,
            (
                values["manufacturer"],
                values["family"],
                values["name"],
                values["hex_color"],
            ),
        ).fetchone()
        return None if row is None else _filament_from_row(row)


class RevisionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, revision_id: str) -> RevisionRecord:
        row = self.connection.execute(
            "SELECT * FROM revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"revision not found: {revision_id}")
        return _revision_from_row(row)

    def list_for_project(self, project_id: str) -> tuple[RevisionRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM revisions
            WHERE project_id = ?
            ORDER BY published_at DESC, id DESC
            """,
            (project_id,),
        ).fetchall()
        return tuple(_revision_from_row(row) for row in rows)

    def list_summary_page(
        self,
        project_id: str,
        *,
        limit: int,
        before: Optional[tuple[str, str]] = None,
    ) -> tuple[tuple[RevisionSummaryRecord, ...], int, bool]:
        if limit < 1:
            raise ValueError("revision page limit must be positive")
        total = int(
            self.connection.execute(
                "SELECT count(*) FROM revisions WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
        )
        cursor_clause = ""
        parameters: list[Any] = [project_id]
        if before is not None:
            cursor_clause = "AND (published_at < ? OR (published_at = ? AND id < ?))"
            parameters.extend((before[0], before[0], before[1]))
        parameters.append(limit + 1)
        rows = self.connection.execute(
            f"""
            WITH page AS (
                SELECT * FROM revisions
                WHERE project_id = ? {cursor_clause}
                ORDER BY published_at DESC, id DESC
                LIMIT ?
            ),
            operation_counts AS (
                SELECT revision_id, count(*) AS operation_count
                FROM region_operations
                WHERE revision_id IN (SELECT id FROM page)
                GROUP BY revision_id
            ),
            artifact_counts AS (
                SELECT revision_id, count(*) AS artifact_count
                FROM artifacts
                WHERE revision_id IN (SELECT id FROM page)
                GROUP BY revision_id
            )
            SELECT
                page.*,
                coalesce(operation_counts.operation_count, 0) AS operation_count,
                coalesce(artifact_counts.artifact_count, 0) AS artifact_count,
                publication.source_draft_generation AS publication_source_draft_generation,
                publication.editor_sequence_sha256 AS publication_editor_sequence_sha256,
                publication.preview_status AS publication_preview_status,
                publication.preview_job_id AS publication_preview_job_id,
                publication.preview_derivation_key AS publication_preview_derivation_key,
                publication.preview_reason AS publication_preview_reason,
                publication.artifact_manifest_sha256 AS publication_artifact_manifest_sha256,
                publication.artifact_count AS publication_artifact_count,
                publication.created_at AS publication_created_at
            FROM page
            LEFT JOIN operation_counts ON operation_counts.revision_id = page.id
            LEFT JOIN artifact_counts ON artifact_counts.revision_id = page.id
            LEFT JOIN revision_publications publication ON publication.revision_id = page.id
            ORDER BY page.published_at DESC, page.id DESC
            """,  # noqa: S608
            tuple(parameters),
        ).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        return (
            tuple(_revision_summary_from_row(row) for row in visible),
            total,
            has_more,
        )


class RevisionPublicationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, revision_id: str) -> Optional[RevisionPublicationRecord]:
        row = self.connection.execute(
            "SELECT * FROM revision_publications WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        return None if row is None else _revision_publication_from_row(row)


class DraftRepository:
    """Mutable autosave state guarded by an optimistic generation counter."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.projects = ProjectRepository(connection)
        self.revisions = RevisionRepository(connection)
        self.history = DraftHistoryRepository(connection)

    def get(self, project_id: str) -> Optional[DraftRecord]:
        row = self.connection.execute(
            "SELECT * FROM project_drafts WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        draft = _draft_from_row(row)
        if self.connection.in_transaction:
            return _draft_with_history(draft, self.history.ensure(draft))
        existing_history = self.connection.execute(
            "SELECT 1 FROM draft_history_heads WHERE project_id = ?", (project_id,)
        ).fetchone()
        if existing_history is not None:
            return _draft_with_history(draft, self.history.summary(project_id))
        with immediate_transaction(self.connection):
            current = self.connection.execute(
                "SELECT * FROM project_drafts WHERE project_id = ?", (project_id,)
            ).fetchone()
            if current is None:
                return None
            draft = _draft_from_row(current)
            return _draft_with_history(draft, self.history.ensure(draft))

    def load_config(self, project_id: str) -> JobConfig:
        draft = self.get(project_id)
        if draft is None:
            raise RecordNotFoundError(f"draft not found for project: {project_id}")
        if _config_fingerprint(draft.config) != draft.config_sha256:
            raise RepositoryError("stored draft configuration fingerprint does not match")
        return load_job_config(dict(draft.config))

    def save(
        self,
        *,
        project_id: str,
        config: JobConfig,
        operations: Sequence[RegionOperation] = (),
        base_revision_id: Optional[str] = None,
        expected_generation: int,
        validate_candidate: Optional[
            Callable[[JobConfig, tuple[RegionOperation, ...]], object]
        ] = None,
        history_command: Optional[DraftHistoryCommand] = None,
    ) -> DraftRecord:
        if expected_generation < 0:
            raise ValueError("expected draft generation cannot be negative")
        operations = tuple(operations)
        _validate_operations(operations)
        operation_json = canonical_json([_operation_to_json(item) for item in operations])
        written: Optional[DraftRecord] = None

        try:
            with immediate_transaction(self.connection):
                self.projects.get(project_id)
                asset = self.connection.execute(
                    "SELECT 1 FROM assets WHERE id = ?", (config.source_asset_id,)
                ).fetchone()
                if asset is None:
                    raise RecordNotFoundError(
                        f"draft source asset not found: {config.source_asset_id}"
                    )
                _require_filament_references(self.connection, config)
                if base_revision_id is not None:
                    base = self.revisions.get(base_revision_id)
                    if base.project_id != project_id:
                        raise InvalidPublicationError(
                            "draft base revision belongs to a different project"
                        )

                current = self.connection.execute(
                    "SELECT * FROM project_drafts WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                current_generation = 0 if current is None else int(current["generation"])
                current_draft = None if current is None else _draft_from_row(current)
                current_history = (
                    None if current_draft is None else self.history.ensure(current_draft)
                )
                payload_sha = None
                if history_command is not None:
                    payload_sha = mutation_payload_sha256(
                        {
                            "project_id": project_id,
                            "expected_generation": expected_generation,
                            "history_command": history_command.__dict__,
                            "config": json.loads(config.canonical_json()),
                            "operations": [_operation_to_json(item) for item in operations],
                            "base_revision_id": base_revision_id,
                        }
                    )
                    retried = self.history.retry(history_command.request_id, "edit", payload_sha)
                    if retried is not None:
                        written = retried
                        return written
                if current_generation != expected_generation:
                    raise StaleDraftError(
                        "stale draft generation: "
                        f"expected {expected_generation}, current {current_generation}"
                    )

                if validate_candidate is not None:
                    validate_candidate(config, operations)

                next_generation = current_generation + 1
                if current is None:
                    self.connection.execute(
                        """
                        INSERT INTO project_drafts(
                            project_id, base_revision_id, schema_version, config_json,
                            operation_json, generation, config_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project_id,
                            base_revision_id,
                            config.schema_version,
                            config.canonical_json(),
                            operation_json,
                            next_generation,
                            config.fingerprint(),
                        ),
                    )
                else:
                    changed = self.connection.execute(
                        """
                        UPDATE project_drafts
                        SET base_revision_id = ?, schema_version = ?, config_json = ?,
                            operation_json = ?, generation = ?, config_sha256 = ?,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE project_id = ? AND generation = ?
                        """,
                        (
                            base_revision_id,
                            config.schema_version,
                            config.canonical_json(),
                            operation_json,
                            next_generation,
                            config.fingerprint(),
                            project_id,
                            expected_generation,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise StaleDraftError("draft changed during autosave")
                candidate = self.get(project_id)
                if candidate is None:  # pragma: no cover - guarded by insert/update above
                    raise RepositoryError("draft disappeared during autosave")
                if current_draft is None:
                    written = candidate
                else:
                    command = history_command or DraftHistoryCommand(
                        request_id=str(uuid.uuid4()),
                        schema_version=1,
                        command_type="editor_update",
                        label="Editor update",
                        before_state_sha256=current_history.state_sha256,
                        expected_cursor_node_id=current_history.cursor_node_id,
                    )
                    effective_payload_sha = payload_sha or mutation_payload_sha256(
                        {
                            "project_id": project_id,
                            "expected_generation": expected_generation,
                            "history_command": command.__dict__,
                            "config": json.loads(config.canonical_json()),
                            "operations": [_operation_to_json(item) for item in operations],
                            "base_revision_id": base_revision_id,
                        }
                    )
                    written = self.history.append(
                        _draft_with_history(current_draft, current_history),
                        candidate,
                        command,
                        payload_sha256=effective_payload_sha,
                        store_receipt=history_command is not None,
                    )
        except sqlite3.IntegrityError as error:
            raise InvalidPublicationError(f"draft autosave failed: {error}") from error
        except DraftHistoryConflictError as error:
            raise InvalidDraftHistoryError(str(error)) from error

        if written is None:  # pragma: no cover - assigned before transaction commit
            raise RepositoryError("draft autosave returned no state")
        return written

    def move_history(
        self,
        *,
        project_id: str,
        direction: str,
        request_id: str,
        expected_generation: int,
        expected_cursor_node_id: str,
        validate_candidate: Callable[[JobConfig, tuple[RegionOperation, ...]], object],
    ) -> DraftRecord:
        payload_sha = mutation_payload_sha256(
            {
                "project_id": project_id,
                "direction": direction,
                "request_id": request_id,
                "expected_generation": expected_generation,
                "expected_cursor_node_id": expected_cursor_node_id,
            }
        )
        try:
            with immediate_transaction(self.connection):
                retried = self.history.retry(request_id, direction, payload_sha)
                if retried is not None:
                    return retried
                row = self.connection.execute(
                    "SELECT * FROM project_drafts WHERE project_id = ?", (project_id,)
                ).fetchone()
                if row is None:
                    raise RecordNotFoundError(f"draft not found for project: {project_id}")
                current = _draft_from_row(row)
                if current.generation != expected_generation:
                    raise StaleDraftError(
                        "stale draft generation: "
                        f"expected {expected_generation}, current {current.generation}"
                    )
                prior, state, target_id = self.history.target(
                    current,
                    direction=direction,
                    request_id=request_id,
                    expected_cursor_node_id=expected_cursor_node_id,
                    payload_sha256=payload_sha,
                )
                if prior is not None:
                    return prior
                if state is None:  # pragma: no cover - target contract
                    raise RepositoryError("history target state is missing")
                config = load_job_config(dict(state.config))
                _require_filament_references(self.connection, config)
                validate_candidate(config, state.operations)
                next_generation = current.generation + 1
                changed = self.connection.execute(
                    """
                    UPDATE project_drafts
                    SET base_revision_id = ?, schema_version = ?, config_json = ?,
                        operation_json = ?, generation = ?, config_sha256 = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE project_id = ? AND generation = ?
                    """,
                    (
                        state.base_revision_id,
                        state.schema_version,
                        canonical_json(state.config),
                        canonical_json([_operation_to_json(item) for item in state.operations]),
                        next_generation,
                        _config_fingerprint(state.config),
                        project_id,
                        expected_generation,
                    ),
                ).rowcount
                if changed != 1:
                    raise StaleDraftError("draft changed during history mutation")
                result_row = self.connection.execute(
                    "SELECT * FROM project_drafts WHERE project_id = ?", (project_id,)
                ).fetchone()
                result = _draft_from_row(result_row)
                return self.history.finish_move(
                    result,
                    direction=direction,
                    request_id=request_id,
                    payload_sha256=payload_sha,
                    target_node_id=target_id,
                )
        except DraftHistoryBoundaryError as error:
            raise DraftHistoryBoundaryReachedError(str(error)) from error
        except DraftHistoryConflictError as error:
            raise InvalidDraftHistoryError(str(error)) from error

    def discard(self, project_id: str, *, expected_generation: int) -> bool:
        with immediate_transaction(self.connection):
            changed = self.connection.execute(
                "DELETE FROM project_drafts WHERE project_id = ? AND generation = ?",
                (project_id, expected_generation),
            ).rowcount
            if changed == 0:
                current = self.connection.execute(
                    "SELECT generation FROM project_drafts WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if current is not None:
                    raise StaleDraftError(
                        f"stale draft generation: current {int(current['generation'])}"
                    )
            if changed == 1:
                head = self.connection.execute(
                    "SELECT lineage_id FROM draft_history_heads WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if head is not None:
                    self.connection.execute(
                        """
                        UPDATE draft_history_lineages
                        SET closed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE id = ? AND closed_at IS NULL
                        """,
                        (head["lineage_id"],),
                    )
                    self.connection.execute(
                        "DELETE FROM draft_history_heads WHERE project_id = ?", (project_id,)
                    )
        return changed == 1


class ArtifactRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, artifact_id: str) -> ArtifactRecord:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"artifact not found: {artifact_id}")
        return _artifact_from_row(row)

    def owner_project_id(self, artifact_id: str) -> str:
        row = self.connection.execute(
            """
            SELECT coalesce(revisions.project_id, jobs.project_id) AS project_id
            FROM artifacts
            LEFT JOIN revisions ON revisions.id = artifacts.revision_id
            LEFT JOIN jobs ON jobs.id = artifacts.job_id
            WHERE artifacts.id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None or row["project_id"] is None:
            raise RecordNotFoundError(f"artifact not found: {artifact_id}")
        return str(row["project_id"])

    def list_for_revision(self, revision_id: str) -> tuple[ArtifactRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM artifacts
            WHERE revision_id = ?
            ORDER BY kind, created_at, id
            """,
            (revision_id,),
        ).fetchall()
        return tuple(_artifact_from_row(row) for row in rows)

    def list_for_revisions(
        self, revision_ids: Sequence[str]
    ) -> dict[str, tuple[ArtifactRecord, ...]]:
        if not revision_ids:
            return {}
        placeholders = ",".join("?" for _ in revision_ids)
        rows = self.connection.execute(
            f"""
            SELECT * FROM artifacts
            WHERE revision_id IN ({placeholders})
            ORDER BY revision_id, kind, created_at, id
            """,  # noqa: S608
            tuple(revision_ids),
        ).fetchall()
        grouped: dict[str, list[ArtifactRecord]] = {item: [] for item in revision_ids}
        for row in rows:
            record = _artifact_from_row(row)
            if record.revision_id is not None:
                grouped[record.revision_id].append(record)
        return {key: tuple(value) for key, value in grouped.items()}

    def list_for_job(self, job_id: str) -> tuple[ArtifactRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM artifacts
            WHERE job_id = ?
            ORDER BY kind, created_at, id
            """,
            (job_id,),
        ).fetchall()
        return tuple(_artifact_from_row(row) for row in rows)


class RegionOperationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def list_for_revision(self, revision_id: str) -> tuple[RegionOperationRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM region_operations
            WHERE revision_id = ?
            ORDER BY sequence
            """,
            (revision_id,),
        ).fetchall()
        return tuple(_region_operation_from_row(row) for row in rows)


class RevisionPublisher:
    """Publish one immutable revision and all requested artifact records atomically."""

    def __init__(self, connection: sqlite3.Connection, blob_store: ContentAddressedStore) -> None:
        self.connection = connection
        self.blob_store = blob_store
        self.projects = ProjectRepository(connection)
        self.assets = AssetRepository(connection, blob_store)
        self.revisions = RevisionRepository(connection)
        self.publications = RevisionPublicationRepository(connection)
        self.artifacts = ArtifactRepository(connection)
        self.drafts = DraftRepository(connection)
        self.operations = RegionOperationRepository(connection)

    def publish(
        self,
        *,
        project_id: str,
        source_asset_id: str,
        config: JobConfig,
        engine_version: str,
        artifacts: Sequence[ArtifactPublication] = (),
        operations: Sequence[RegionOperation] = (),
        parent_revision_id: Optional[str] = None,
        label: str = "",
        notes: str = "",
        revision_id: Optional[str] = None,
    ) -> PublishedRevision:
        identifier = revision_id or new_id("revision")
        artifact_ids = [new_id("artifact") for _ in artifacts]
        try:
            with immediate_transaction(self.connection):
                self._publish_locked(
                    identifier=identifier,
                    artifact_ids=artifact_ids,
                    project_id=project_id,
                    source_asset_id=source_asset_id,
                    config=config,
                    engine_version=engine_version,
                    artifacts=artifacts,
                    operations=operations,
                    parent_revision_id=parent_revision_id,
                    label=label,
                    notes=notes,
                )
                self._seal_revision_locked(identifier)
        except sqlite3.IntegrityError as error:
            raise InvalidPublicationError(f"revision publication failed: {error}") from error

        return self._publication_result(identifier, artifact_ids)

    def publish_draft(
        self,
        *,
        project_id: str,
        expected_generation: int,
        engine_version: str,
        artifacts: Sequence[ArtifactPublication] = (),
        label: str = "",
        notes: str = "",
        revision_id: Optional[str] = None,
    ) -> PublishedRevision:
        """Atomically publish one exact draft generation and remove the mutable draft."""

        identifier = revision_id or new_id("revision")
        artifact_ids = [new_id("artifact") for _ in artifacts]
        try:
            with immediate_transaction(self.connection):
                draft = self.drafts.get(project_id)
                if draft is None:
                    raise RecordNotFoundError(f"draft not found for project: {project_id}")
                if draft.generation != expected_generation:
                    raise StaleDraftError(
                        "cannot publish stale draft generation: "
                        f"expected {expected_generation}, current {draft.generation}"
                    )
                config = load_job_config(dict(draft.config))
                if _config_fingerprint(draft.config) != draft.config_sha256:
                    raise InvalidPublicationError("draft configuration fingerprint is invalid")

                self._publish_locked(
                    identifier=identifier,
                    artifact_ids=artifact_ids,
                    project_id=project_id,
                    source_asset_id=config.source_asset_id,
                    config=config,
                    engine_version=engine_version,
                    artifacts=artifacts,
                    operations=draft.operations,
                    parent_revision_id=draft.base_revision_id,
                    label=label,
                    notes=notes,
                )
                deleted = self.connection.execute(
                    "DELETE FROM project_drafts WHERE project_id = ? AND generation = ?",
                    (project_id, expected_generation),
                ).rowcount
                if deleted != 1:
                    raise StaleDraftError("draft changed during publication")
                self._seal_revision_locked(identifier)
        except sqlite3.IntegrityError as error:
            raise InvalidPublicationError(f"draft publication failed: {error}") from error

        return self._publication_result(identifier, artifact_ids)

    def publish_draft_and_continue(
        self,
        *,
        project_id: str,
        expected_generation: int,
        expected_draft_state_sha256: str,
        publication_config: Optional[JobConfig] = None,
        engine_version: str,
        editor_sequence_sha256: str,
        expected_preview_derivation_key: str,
        expected_preview_derivation_metadata: Optional[Mapping[str, Any]] = None,
        preview_job_id: Optional[str] = None,
        label: str,
        notes: str = "",
        revision_id: Optional[str] = None,
    ) -> PublishedRevision:
        """Publish an exact draft and atomically continue from the new revision."""

        normalized_label = label.strip()
        if not normalized_label:
            raise ValueError("revision label cannot be empty")
        if len(editor_sequence_sha256) != 64:
            raise ValueError("editor sequence fingerprint must contain 64 characters")
        if len(expected_draft_state_sha256) != 64:
            raise ValueError("draft state fingerprint must contain 64 characters")
        if len(expected_preview_derivation_key) != 64:
            raise ValueError("preview derivation key must contain 64 characters")
        identifier = revision_id or new_id("revision")
        artifact_ids: list[str] = []
        try:
            with immediate_transaction(self.connection):
                draft = self.drafts.get(project_id)
                if draft is None:
                    raise RecordNotFoundError(f"draft not found for project: {project_id}")
                if draft.generation != expected_generation:
                    raise StaleDraftError(
                        "cannot publish stale draft generation: "
                        f"expected {expected_generation}, current {draft.generation}"
                    )
                if draft_state_fingerprint(
                    draft.config_sha256,
                    draft.operations,
                    base_revision_id=draft.base_revision_id,
                ) != (expected_draft_state_sha256):
                    raise StaleDraftError("draft contents changed before publication")
                config = load_job_config(dict(draft.config))
                if _config_fingerprint(draft.config) != draft.config_sha256:
                    raise InvalidPublicationError("draft configuration fingerprint is invalid")
                effective_config = publication_config or config
                if effective_config.source_asset_id != config.source_asset_id:
                    raise InvalidPublicationError(
                        "publication normalization cannot replace the draft source asset"
                    )

                preview_status, preview_row, preview_reason, preview_artifacts = (
                    self._select_preview_evidence_locked(
                        project_id=project_id,
                        derivation_key=expected_preview_derivation_key,
                        expected_derivation_metadata=expected_preview_derivation_metadata,
                        guarded_job_id=preview_job_id,
                    )
                )
                preview_id = None if preview_row is None else str(preview_row["id"])
                publications = tuple(
                    _preview_artifact_publication(record, preview_id)
                    for record in preview_artifacts
                )
                artifact_ids = [new_id("artifact") for _ in publications]
                self._publish_locked(
                    identifier=identifier,
                    artifact_ids=artifact_ids,
                    project_id=project_id,
                    source_asset_id=effective_config.source_asset_id,
                    config=effective_config,
                    engine_version=engine_version,
                    artifacts=publications,
                    operations=draft.operations,
                    parent_revision_id=draft.base_revision_id,
                    label=normalized_label,
                    notes=notes,
                )
                manifest_sha256 = (
                    artifact_manifest_sha256(preview_artifacts)
                    if preview_status == "fresh"
                    else None
                )
                self.connection.execute(
                    """
                    INSERT INTO revision_publications(
                        revision_id, source_draft_generation, editor_sequence_sha256,
                        preview_status, preview_job_id, preview_derivation_key,
                        preview_reason, artifact_manifest_sha256, artifact_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        expected_generation,
                        editor_sequence_sha256,
                        preview_status,
                        preview_id,
                        None if preview_row is None else preview_row["request_key"],
                        preview_reason,
                        manifest_sha256,
                        len(preview_artifacts),
                    ),
                )
                self._seal_revision_locked(identifier)
                effective_payload = effective_config.model_dump(mode="json")
                changed = self.connection.execute(
                    """
                    UPDATE project_drafts
                    SET base_revision_id = ?, generation = ?, config_json = ?, config_sha256 = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE project_id = ? AND generation = ?
                    """,
                    (
                        identifier,
                        expected_generation + 1,
                        canonical_json(effective_payload),
                        _config_fingerprint(effective_payload),
                        project_id,
                        expected_generation,
                    ),
                ).rowcount
                if changed != 1:
                    raise StaleDraftError("draft changed during publication")
                continuation = self.drafts.get(project_id)
                if continuation is None:  # pragma: no cover - guarded by the update above
                    raise RepositoryError("continuation draft disappeared during publication")
                continuation = _draft_with_history(
                    continuation,
                    DraftHistoryRepository(self.connection).checkpoint_publication(
                        continuation, identifier
                    ),
                )
                published_project = self.projects.get(project_id)
        except sqlite3.IntegrityError as error:
            raise InvalidPublicationError(f"draft publication failed: {error}") from error

        result = self._publication_result(identifier, artifact_ids)
        return PublishedRevision(
            revision=result.revision,
            artifacts=result.artifacts,
            publication=self.publications.get(identifier),
            continuation_draft=continuation,
            project=published_project,
        )

    def accept_prompted_alternative(
        self,
        *,
        project_id: str,
        session_id: str,
        alternative_id: str,
        expected_generation: int,
        engine_version: str,
        label: str,
        notes: str = "",
        revision_id: Optional[str] = None,
    ) -> PublishedRevision:
        """Atomically publish a provider candidate and continue from the immutable child.

        Provider output remains only a review candidate until this transaction succeeds. The
        parent revision is never updated, and a stale draft prevents every acceptance write.
        """

        normalized_label = label.strip()
        if not normalized_label:
            raise ValueError("accepted prompted revisions require a label")
        identifier = revision_id or new_id("revision")
        try:
            with immediate_transaction(self.connection):
                row = self.connection.execute(
                    """
                    SELECT sessions.*, alternatives.id AS alternative_id,
                        alternatives.status AS alternative_status,
                        alternatives.output_asset_id, alternatives.output_sha256,
                        alternatives.alternative_index, alternatives.provenance_json
                    FROM prompted_edit_sessions AS sessions
                    JOIN prompted_edit_alternatives AS alternatives
                        ON alternatives.session_id = sessions.id
                    WHERE sessions.id = ? AND alternatives.id = ?
                    """,
                    (session_id, alternative_id),
                ).fetchone()
                if row is None or row["project_id"] != project_id:
                    raise RecordNotFoundError("prompted-edit alternative not found")
                if row["status"] not in {"complete", "partial"}:
                    raise InvalidPublicationError("prompted-edit session is not reviewable")
                if row["alternative_status"] != "review":
                    raise InvalidPublicationError("prompted-edit alternative is not reviewable")

                draft = self.drafts.get(project_id)
                current_generation = 0 if draft is None else draft.generation
                if current_generation != expected_generation:
                    raise StaleDraftError(
                        "cannot accept over a stale draft generation: "
                        f"expected {expected_generation}, current {current_generation}"
                    )
                parent_id = str(row["parent_revision_id"])
                if draft is not None and draft.base_revision_id != parent_id:
                    raise StaleDraftError("draft no longer continues from the prompted-edit parent")
                parent = self.revisions.get(parent_id)
                if parent.project_id != project_id:
                    raise InvalidPublicationError("prompted-edit parent belongs to another project")
                config = load_job_config(dict(parent.config)).model_copy(
                    update={"source_asset_id": str(row["output_asset_id"])}
                )
                request = json.loads(row["request_json"])
                selection = request.get("selection")
                if not isinstance(selection, dict):
                    raise InvalidPublicationError("prompted-edit selection evidence is missing")
                provenance = json.loads(row["provenance_json"])
                operations = tuple(
                    RegionOperation(
                        operation_type=item.operation_type,
                        selection=item.selection,
                        parameters=item.parameters,
                        source=item.source,
                        provenance=item.provenance,
                    )
                    for item in self.operations.list_for_revision(parent_id)
                ) + (
                    RegionOperation(
                        operation_type="prompted_edit",
                        selection={"selection": selection},
                        parameters={
                            "prompt": request["prompt"],
                            "options": request.get("options", {}),
                            "alternative_index": int(row["alternative_index"]),
                        },
                        source="model",
                        provenance={"regional_edit": provenance},
                    ),
                )
                self._publish_locked(
                    identifier=identifier,
                    artifact_ids=(),
                    project_id=project_id,
                    source_asset_id=config.source_asset_id,
                    config=config,
                    engine_version=engine_version,
                    artifacts=(),
                    operations=operations,
                    parent_revision_id=parent_id,
                    label=normalized_label,
                    notes=notes,
                )
                self._seal_revision_locked(identifier)
                operation_json = canonical_json([_operation_to_json(item) for item in operations])
                next_generation = current_generation + 1
                if draft is None:
                    self.connection.execute(
                        """
                        INSERT INTO project_drafts(
                            project_id, base_revision_id, schema_version, config_json,
                            operation_json, generation, config_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project_id,
                            identifier,
                            config.schema_version,
                            config.canonical_json(),
                            operation_json,
                            next_generation,
                            config.fingerprint(),
                        ),
                    )
                else:
                    changed = self.connection.execute(
                        """
                        UPDATE project_drafts
                        SET base_revision_id = ?, schema_version = ?, config_json = ?,
                            operation_json = ?, generation = ?, config_sha256 = ?,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE project_id = ? AND generation = ?
                        """,
                        (
                            identifier,
                            config.schema_version,
                            config.canonical_json(),
                            operation_json,
                            next_generation,
                            config.fingerprint(),
                            project_id,
                            expected_generation,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise StaleDraftError("draft changed during prompted-edit acceptance")
                continuation = self.drafts.get(project_id)
                if continuation is None:  # pragma: no cover - guarded by insert/update
                    raise RepositoryError("accepted prompted draft disappeared")
                continuation = _draft_with_history(
                    continuation,
                    DraftHistoryRepository(self.connection).reset_lineage(continuation),
                )
                self.connection.execute(
                    """
                    UPDATE prompted_edit_alternatives
                    SET status = CASE WHEN id = ? THEN 'accepted' ELSE 'rejected' END,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE session_id = ? AND status = 'review'
                    """,
                    (alternative_id, session_id),
                )
                changed = self.connection.execute(
                    """
                    UPDATE prompted_edit_sessions
                    SET status = 'accepted', accepted_alternative_id = ?,
                        accepted_revision_id = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ? AND status IN ('complete', 'partial')
                    """,
                    (alternative_id, identifier, session_id),
                ).rowcount
                if changed != 1:
                    raise InvalidPublicationError("prompted-edit acceptance lost its review state")
                published_project = self.projects.get(project_id)
        except sqlite3.IntegrityError as error:
            raise InvalidPublicationError(
                f"prompted-edit acceptance failed atomically: {error}"
            ) from error

        result = self._publication_result(identifier, ())
        return PublishedRevision(
            revision=result.revision,
            artifacts=(),
            continuation_draft=continuation,
            project=published_project,
        )

    def branch_revision(
        self,
        *,
        project_id: str,
        revision_id: str,
        expected_generation: int,
        validate_replay: Callable[[JobConfig, tuple[RegionOperation, ...]], None],
    ) -> BranchedRevision:
        """Replace mutable state with exact historical state without mutating history."""

        if expected_generation < 0:
            raise ValueError("expected draft generation cannot be negative")
        with immediate_transaction(self.connection):
            self.projects.get(project_id)
            revision = self.revisions.get(revision_id)
            if revision.project_id != project_id:
                raise RecordNotFoundError(f"revision not found: {revision_id}")
            current = self.drafts.get(project_id)
            current_generation = 0 if current is None else current.generation
            if current_generation != expected_generation:
                raise StaleDraftError(
                    "cannot branch over stale draft generation: "
                    f"expected {expected_generation}, current {current_generation}"
                )
            config = load_job_config(dict(revision.config))
            if _config_fingerprint(revision.config) != revision.config_sha256:
                raise InvalidPublicationError("revision configuration fingerprint is invalid")
            _require_filament_references(self.connection, config)
            operations = tuple(
                RegionOperation(
                    operation_type=item.operation_type,
                    selection=item.selection,
                    parameters=item.parameters,
                    source=item.source,
                    provenance=item.provenance,
                )
                for item in self.operations.list_for_revision(revision_id)
            )
            validate_replay(config, operations)
            operation_json = canonical_json([_operation_to_json(item) for item in operations])
            next_generation = current_generation + 1
            if current is None:
                self.connection.execute(
                    """
                    INSERT INTO project_drafts(
                        project_id, base_revision_id, schema_version, config_json,
                        operation_json, generation, config_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        revision_id,
                        revision.schema_version,
                        canonical_json(revision.config),
                        operation_json,
                        next_generation,
                        revision.config_sha256,
                    ),
                )
            else:
                changed = self.connection.execute(
                    """
                    UPDATE project_drafts
                    SET base_revision_id = ?, schema_version = ?, config_json = ?,
                        operation_json = ?, generation = ?, config_sha256 = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE project_id = ? AND generation = ?
                    """,
                    (
                        revision_id,
                        revision.schema_version,
                        canonical_json(revision.config),
                        operation_json,
                        next_generation,
                        revision.config_sha256,
                        project_id,
                        expected_generation,
                    ),
                ).rowcount
                if changed != 1:
                    raise StaleDraftError("draft changed during branch")
            result = self.drafts.get(project_id)
            if result is None:  # pragma: no cover - guarded by the insert/update above
                raise RepositoryError("branched draft disappeared during transaction")
            result = _draft_with_history(
                result,
                DraftHistoryRepository(self.connection).reset_lineage(result),
            )
            branched_project = self.projects.get(project_id)
        return BranchedRevision(project=branched_project, draft=result)

    def _select_preview_evidence_locked(
        self,
        *,
        project_id: str,
        derivation_key: str,
        expected_derivation_metadata: Optional[Mapping[str, Any]],
        guarded_job_id: Optional[str],
    ) -> tuple[str, Optional[sqlite3.Row], str, tuple[ArtifactRecord, ...]]:
        head = self.connection.execute(
            """
            SELECT jobs.* FROM job_heads
            JOIN jobs ON jobs.id = job_heads.job_id
            WHERE job_heads.project_id = ? AND job_heads.supersession_key = ?
            """,
            (project_id, f"preview:{project_id}"),
        ).fetchone()
        if guarded_job_id is not None:
            guarded = self.connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (guarded_job_id,)
            ).fetchone()
            if (
                guarded is None
                or guarded["project_id"] != project_id
                or guarded["job_type"] != "preview"
                or head is None
                or head["id"] != guarded_job_id
            ):
                raise PreviewEvidenceConflictError(
                    "The selected preview is not current for this project.",
                    preview_job_id=guarded_job_id,
                )
            head = guarded
        if head is None:
            return "missing", None, "No preview has been generated for this draft.", ()
        recorded_metadata = None
        metadata_row = None
        if head["request_key"] != derivation_key:
            metadata_row = self.connection.execute(
                """
                SELECT metadata_json, derivation_key FROM artifacts WHERE job_id = ?
                ORDER BY CASE kind WHEN 'preview-statistics' THEN 0 ELSE 1 END, kind LIMIT 1
                """,
                (head["id"],),
            ).fetchone()
            if metadata_row is not None:
                try:
                    decoded = json.loads(metadata_row["metadata_json"])
                    if isinstance(decoded, dict):
                        recorded_metadata = decoded
                except (TypeError, ValueError):
                    recorded_metadata = None
        artifact_matches_expected_key = (
            metadata_row is not None and metadata_row["derivation_key"] == derivation_key
        )
        reason = _preview_staleness_reason(
            head,
            derivation_key,
            recorded_metadata=recorded_metadata,
            expected_derivation_metadata=(
                None if artifact_matches_expected_key else expected_derivation_metadata
            ),
        )
        if reason is not None:
            if guarded_job_id is not None:
                raise PreviewEvidenceConflictError(reason, preview_job_id=guarded_job_id)
            return "stale", head, reason, ()
        artifact_rows = self.connection.execute(
            """
            SELECT * FROM artifacts WHERE job_id = ? ORDER BY kind, created_at, id
            """,
            (head["id"],),
        ).fetchall()
        artifacts = tuple(_artifact_from_row(row) for row in artifact_rows)
        invalid_reason = _preview_artifact_problem(
            self.blob_store, artifacts, derivation_key=derivation_key
        )
        if invalid_reason is not None:
            if guarded_job_id is not None:
                raise PreviewEvidenceConflictError(invalid_reason, preview_job_id=guarded_job_id)
            return "stale", head, invalid_reason, ()
        return "fresh", head, "Exact successful preview artifacts were verified.", artifacts

    def _seal_revision_locked(self, revision_id: str) -> None:
        self.connection.execute(
            "INSERT INTO revision_history_seals(revision_id) VALUES (?)", (revision_id,)
        )

    def _publish_locked(
        self,
        *,
        identifier: str,
        artifact_ids: Sequence[str],
        project_id: str,
        source_asset_id: str,
        config: JobConfig,
        engine_version: str,
        artifacts: Sequence[ArtifactPublication],
        operations: Sequence[RegionOperation],
        parent_revision_id: Optional[str],
        label: str,
        notes: str,
    ) -> None:
        if not engine_version.strip():
            raise ValueError("engine version cannot be empty")
        if config.source_asset_id != source_asset_id:
            raise InvalidPublicationError(
                "job configuration source_asset_id does not match publication source"
            )
        _validate_artifact_inputs(artifacts)
        _validate_operations(operations)

        project = self.projects.get(project_id)
        source = self.assets.get(source_asset_id)
        _verify_blob(self.blob_store, source.stored_blob())
        _require_filament_references(self.connection, config)
        if parent_revision_id is not None:
            parent = self.revisions.get(parent_revision_id)
            if parent.project_id != project.id:
                raise InvalidPublicationError("parent revision belongs to a different project")

        for artifact in artifacts:
            _require_namespace(artifact.blob, "artifacts")
            _verify_blob(self.blob_store, artifact.blob)

        self.connection.execute(
            """
            INSERT INTO revisions(
                id, project_id, source_asset_id, parent_revision_id, schema_version,
                engine_version, config_json, config_sha256, label, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                project.id,
                source.id,
                parent_revision_id,
                config.schema_version,
                engine_version.strip(),
                config.canonical_json(),
                config.fingerprint(),
                label,
                notes,
            ),
        )

        for artifact_id, artifact in zip(artifact_ids, artifacts):
            self._insert_artifact(identifier, artifact_id, artifact)
        for sequence, operation in enumerate(operations):
            self._insert_operation(identifier, sequence, operation)

        changed = self.connection.execute(
            """
            UPDATE projects
            SET active_revision_id = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (identifier, project.id),
        ).rowcount
        if changed != 1:
            raise RepositoryError("project disappeared during revision publication")

    def _publication_result(
        self, revision_id: str, artifact_ids: Sequence[str]
    ) -> PublishedRevision:
        revision = self.revisions.get(revision_id)
        published_artifacts = tuple(self.artifacts.get(item) for item in artifact_ids)
        return PublishedRevision(revision=revision, artifacts=published_artifacts)

    def _insert_artifact(
        self, revision_id: str, artifact_id: str, artifact: ArtifactPublication
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO artifacts(
                id, revision_id, job_id, kind, sha256, derivation_key, media_type,
                relative_path, byte_size, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                revision_id,
                artifact.job_id,
                artifact.kind,
                artifact.blob.sha256,
                artifact.derivation_key,
                artifact.blob.media_type,
                artifact.blob.relative_path,
                artifact.blob.byte_size,
                canonical_json(artifact.metadata),
            ),
        )

    def _insert_operation(
        self, revision_id: str, sequence: int, operation: RegionOperation
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO region_operations(
                id, revision_id, sequence, operation_type, selection_json, parameters_json,
                source, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("operation"),
                revision_id,
                sequence,
                operation.operation_type,
                canonical_json(operation.selection),
                canonical_json(operation.parameters),
                operation.source,
                canonical_json(operation.provenance),
            ),
        )


def _validate_artifact_inputs(artifacts: Sequence[ArtifactPublication]) -> None:
    seen = set()
    for artifact in artifacts:
        if not artifact.kind.strip():
            raise ValueError("artifact kind cannot be empty")
        if not artifact.derivation_key.strip():
            raise ValueError("artifact derivation key cannot be empty")
        key = (artifact.derivation_key, artifact.kind)
        if key in seen:
            raise InvalidPublicationError(
                "artifact kind and derivation key must be unique within a publication"
            )
        seen.add(key)


def _validate_operations(operations: Sequence[RegionOperation]) -> None:
    for operation in operations:
        if not operation.operation_type.strip():
            raise ValueError("operation type cannot be empty")
        if operation.source not in {"automatic", "manual", "model"}:
            raise ValueError(f"unsupported operation source: {operation.source}")
        if not isinstance(operation.selection, Mapping):
            raise ValueError("operation selection must be an object")
        if not isinstance(operation.parameters, Mapping):
            raise ValueError("operation parameters must be an object")
        if not isinstance(operation.provenance, Mapping):
            raise ValueError("operation provenance must be an object")
        validate_operation_provenance(
            selection=operation.selection,
            parameters=operation.parameters,
            provenance=operation.provenance,
        )


def _preview_staleness_reason(
    row: sqlite3.Row,
    derivation_key: str,
    *,
    recorded_metadata: Optional[Mapping[str, Any]] = None,
    expected_derivation_metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    if row["state"] != "succeeded":
        return "The current preview did not complete successfully."
    if row["request_key"] != derivation_key:
        raw_expected = (
            None
            if expected_derivation_metadata is None
            else expected_derivation_metadata.get("derivation_identity")
        )
        if isinstance(raw_expected, Mapping):
            try:
                expected = DerivationIdentity(
                    pipeline=str(raw_expected["pipeline"]),
                    source_fingerprint=str(raw_expected["source_fingerprint"]),
                    config_fingerprint=str(raw_expected["config_fingerprint"]),
                    operations_fingerprint=str(raw_expected["operations_fingerprint"]),
                    engine_version=str(raw_expected["engine_version"]),
                    adapter_versions=dict(raw_expected["adapter_versions"]),
                    dependencies=dict(raw_expected["dependencies"]),
                    policy_schema_version=int(raw_expected["policy_schema_version"]),
                )
            except (KeyError, TypeError, ValueError):
                pass
            else:
                return explain_staleness(recorded_metadata, expected)
        return "The current preview was generated from different draft state."
    return None


def _preview_artifact_problem(
    blob_store: ContentAddressedStore,
    artifacts: Sequence[ArtifactRecord],
    *,
    derivation_key: str,
) -> Optional[str]:
    if not artifacts:
        return "The successful preview has no persisted artifacts."
    seen_kinds: set[str] = set()
    for artifact in artifacts:
        if artifact.revision_id is not None:
            return "The preview artifact ownership is invalid."
        if artifact.derivation_key != derivation_key:
            return "The preview contains artifacts from different draft state."
        if artifact.kind in seen_kinds:
            return "The preview contains duplicate artifact kinds."
        seen_kinds.add(artifact.kind)
        blob = StoredBlob(
            sha256=artifact.sha256,
            relative_path=artifact.relative_path,
            byte_size=artifact.byte_size,
            media_type=artifact.media_type,
            extension=_artifact_extension(artifact.relative_path),
        )
        try:
            verified = blob_store.verify(blob)
        except FileNotFoundError:
            verified = False
        if not verified:
            return f"Preview artifact {artifact.kind!r} is missing or corrupt."
    return None


def _artifact_extension(relative_path: str) -> str:
    filename = relative_path.rsplit("/", 1)[-1]
    position = filename.rfind(".")
    if position <= 0:
        raise InvalidPublicationError("artifact blob path is missing an extension")
    return filename[position:]


def _preview_artifact_publication(
    artifact: ArtifactRecord, preview_job_id: Optional[str]
) -> ArtifactPublication:
    metadata = dict(artifact.metadata)
    metadata["publication_provenance"] = {
        "source_preview_job_id": preview_job_id,
        "source_artifact_id": artifact.id,
        "source_artifact_created_at": artifact.created_at,
        "verified_sha256": artifact.sha256,
    }
    return ArtifactPublication(
        kind=artifact.kind,
        derivation_key=artifact.derivation_key,
        blob=StoredBlob(
            sha256=artifact.sha256,
            relative_path=artifact.relative_path,
            byte_size=artifact.byte_size,
            media_type=artifact.media_type,
            extension=_artifact_extension(artifact.relative_path),
        ),
        metadata=metadata,
    )


def artifact_manifest_sha256(artifacts: Sequence[ArtifactRecord]) -> str:
    manifest = [
        {
            "source_artifact_id": (
                artifact.metadata.get("publication_provenance", {}).get("source_artifact_id")
                if isinstance(artifact.metadata.get("publication_provenance"), dict)
                else None
            )
            or artifact.id,
            "kind": artifact.kind,
            "sha256": artifact.sha256,
            "derivation_key": artifact.derivation_key,
            "media_type": artifact.media_type,
            "byte_size": artifact.byte_size,
        }
        for artifact in artifacts
    ]
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def draft_state_fingerprint(
    config_sha256: str,
    operations: Sequence[RegionOperation],
    *,
    base_revision_id: Optional[str] = None,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "config_sha256": config_sha256,
                "base_revision_id": base_revision_id,
                "operations": [_operation_to_json(operation) for operation in operations],
            }
        ).encode("utf-8")
    ).hexdigest()


def _operation_to_json(operation: RegionOperation) -> JsonObject:
    return {
        "operation_type": operation.operation_type,
        "selection": dict(operation.selection),
        "parameters": dict(operation.parameters),
        "source": operation.source,
        "provenance": dict(operation.provenance),
    }


def _operation_from_json(value: Any) -> RegionOperation:
    if not isinstance(value, dict):
        raise RepositoryError("stored draft operation must be a JSON object")
    try:
        operation = RegionOperation(
            operation_type=value["operation_type"],
            selection=value["selection"],
            parameters=value.get("parameters", {}),
            source=value.get("source", "manual"),
            provenance=value.get("provenance", {}),
        )
    except (KeyError, TypeError) as error:
        raise RepositoryError("stored draft operation is malformed") from error
    _validate_operations((operation,))
    return operation


def _require_namespace(blob: StoredBlob, expected: str) -> None:
    if not blob.relative_path.startswith(f"{expected}/"):
        raise InvalidPublicationError(f"expected blob in {expected} namespace")


def _verify_blob(store: ContentAddressedStore, blob: StoredBlob) -> None:
    try:
        valid = store.verify(blob)
    except FileNotFoundError as error:
        raise MissingBlobError(f"blob is missing: {blob.relative_path}") from error
    if not valid:
        raise MissingBlobError(f"blob failed integrity verification: {blob.relative_path}")


def _normalize_filament_values(values: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "manufacturer": str(values.get("manufacturer", "")).strip(),
        "family": str(values.get("family", "")).strip(),
        "name": str(values.get("name", "")).strip(),
        "hex_color": str(values.get("hex_color", "")).strip().upper(),
        "material": str(values.get("material", "PLA")).strip().upper(),
        "finish": str(values.get("finish", "")).strip(),
        "owned": bool(values.get("owned", False)),
        "metadata": values.get("metadata", {}),
    }
    if not result["manufacturer"] or len(result["manufacturer"]) > 120:
        raise ValueError("filament manufacturer must contain 1 to 120 characters")
    if not result["name"] or len(result["name"]) > 160:
        raise ValueError("filament name must contain 1 to 160 characters")
    if len(result["family"]) > 120 or len(result["finish"]) > 120:
        raise ValueError("filament family and finish must be at most 120 characters")
    if not result["material"] or len(result["material"]) > 80:
        raise ValueError("filament material must contain 1 to 80 characters")
    color = result["hex_color"]
    if (
        len(color) != 7
        or color[0] != "#"
        or any(character not in "0123456789ABCDEF" for character in color[1:])
    ):
        raise ValueError("filament color must be a six-digit hex color")
    if not isinstance(result["metadata"], Mapping):
        raise ValueError("filament metadata must be an object")
    result["metadata"] = dict(result["metadata"])
    canonical_json(result["metadata"])
    return result


def _filament_sql_values(values: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        values["manufacturer"],
        values["family"],
        values["name"],
        values["hex_color"],
        values["material"],
        values["finish"],
        int(values["owned"]),
        canonical_json(values["metadata"]),
    )


def _filament_ids(config: JobConfig) -> tuple[str, ...]:
    return tuple(
        color.filament_id for color in config.palette.colors if color.filament_id is not None
    )


def _require_filament_references(connection: sqlite3.Connection, config: JobConfig) -> None:
    identifiers = tuple(sorted(set(_filament_ids(config))))
    if not identifiers:
        return
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        f"SELECT id FROM filaments WHERE id IN ({placeholders})",  # noqa: S608
        identifiers,
    ).fetchall()
    found = {row["id"] for row in rows}
    missing = [identifier for identifier in identifiers if identifier not in found]
    if missing:
        raise InvalidFilamentReferenceError(missing)


def _filament_reference_locations(
    connection: sqlite3.Connection, filament_id: str
) -> tuple[str, ...]:
    locations = []
    for table, label, id_field in (
        ("revisions", "revision", "id"),
        ("project_drafts", "draft", "project_id"),
        ("draft_history_states", "history-state", "sha256"),
    ):
        rows = connection.execute(f"SELECT {id_field}, config_json FROM {table}").fetchall()  # noqa: S608
        for row in rows:
            try:
                payload = _json_object(row["config_json"], field_name="config")
                config = load_job_config(dict(payload))
            except (TypeError, ValueError) as error:
                raise RepositoryError(f"stored {label} configuration is malformed") from error
            if filament_id in _filament_ids(config):
                locations.append(f"{label}:{row[id_field]}")
    return tuple(locations)


def _project_from_row(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        active_revision_id=row["active_revision_id"],
        preferences=_json_object(row["preferences_json"], field_name="project preferences"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _asset_from_row(row: sqlite3.Row) -> AssetRecord:
    return AssetRecord(
        id=row["id"],
        sha256=row["sha256"],
        media_type=row["media_type"],
        extension=row["extension"],
        original_filename=row["original_filename"],
        relative_path=row["relative_path"],
        byte_size=row["byte_size"],
        width_px=row["width_px"],
        height_px=row["height_px"],
        metadata=_json_object(row["metadata_json"], field_name="asset metadata"),
        created_at=row["created_at"],
    )


def _filament_from_row(row: sqlite3.Row) -> FilamentRecord:
    return FilamentRecord(
        id=row["id"],
        manufacturer=row["manufacturer"],
        family=row["family"],
        name=row["name"],
        hex_color=row["hex_color"],
        material=row["material"],
        finish=row["finish"],
        owned=bool(row["owned"]),
        metadata=_json_object(row["metadata_json"], field_name="filament metadata"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _revision_from_row(row: sqlite3.Row) -> RevisionRecord:
    return RevisionRecord(
        id=row["id"],
        project_id=row["project_id"],
        source_asset_id=row["source_asset_id"],
        parent_revision_id=row["parent_revision_id"],
        schema_version=row["schema_version"],
        engine_version=row["engine_version"],
        config=_json_object(row["config_json"], field_name="revision config"),
        config_sha256=row["config_sha256"],
        label=row["label"],
        notes=row["notes"],
        published_at=row["published_at"],
    )


def _revision_publication_from_row(row: sqlite3.Row) -> RevisionPublicationRecord:
    return RevisionPublicationRecord(
        revision_id=row["revision_id"],
        source_draft_generation=row["source_draft_generation"],
        editor_sequence_sha256=row["editor_sequence_sha256"],
        preview_status=row["preview_status"],
        preview_job_id=row["preview_job_id"],
        preview_derivation_key=row["preview_derivation_key"],
        preview_reason=row["preview_reason"],
        artifact_manifest_sha256=row["artifact_manifest_sha256"],
        artifact_count=row["artifact_count"],
        created_at=row["created_at"],
    )


def _revision_summary_from_row(row: sqlite3.Row) -> RevisionSummaryRecord:
    publication = None
    if row["publication_preview_status"] is not None:
        publication = RevisionPublicationRecord(
            revision_id=row["id"],
            source_draft_generation=row["publication_source_draft_generation"],
            editor_sequence_sha256=row["publication_editor_sequence_sha256"],
            preview_status=row["publication_preview_status"],
            preview_job_id=row["publication_preview_job_id"],
            preview_derivation_key=row["publication_preview_derivation_key"],
            preview_reason=row["publication_preview_reason"],
            artifact_manifest_sha256=row["publication_artifact_manifest_sha256"],
            artifact_count=row["publication_artifact_count"],
            created_at=row["publication_created_at"],
        )
    return RevisionSummaryRecord(
        revision=_revision_from_row(row),
        publication=publication,
        operation_count=row["operation_count"],
        artifact_count=row["artifact_count"],
    )


def _draft_from_row(row: sqlite3.Row) -> DraftRecord:
    config = _json_object(row["config_json"], field_name="draft config")
    operation_values = json.loads(row["operation_json"])
    if not isinstance(operation_values, list):
        raise RepositoryError("stored draft operation history must be a JSON list")
    operations = tuple(_operation_from_json(item) for item in operation_values)
    fingerprint = row["config_sha256"]
    if fingerprint is None:
        # Legacy rows created before migration 3 remain readable and reproducible.
        fingerprint = hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()
    return DraftRecord(
        project_id=row["project_id"],
        base_revision_id=row["base_revision_id"],
        schema_version=row["schema_version"],
        config=config,
        config_sha256=fingerprint,
        operations=operations,
        generation=row["generation"],
        updated_at=row["updated_at"],
    )


def _draft_with_history(draft: DraftRecord, history) -> DraftRecord:
    return DraftRecord(
        project_id=draft.project_id,
        base_revision_id=draft.base_revision_id,
        schema_version=draft.schema_version,
        config=draft.config,
        config_sha256=draft.config_sha256,
        operations=draft.operations,
        generation=draft.generation,
        updated_at=draft.updated_at,
        history=history,
    )


def _config_fingerprint(config: Mapping[str, Any]) -> str:
    """Hash the exact stored contract, before additive defaults or migrations are applied."""

    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"],
        revision_id=row["revision_id"],
        job_id=row["job_id"],
        kind=row["kind"],
        sha256=row["sha256"],
        derivation_key=row["derivation_key"],
        media_type=row["media_type"],
        relative_path=row["relative_path"],
        byte_size=row["byte_size"],
        metadata=_json_object(row["metadata_json"], field_name="artifact metadata"),
        created_at=row["created_at"],
    )


def _region_operation_from_row(row: sqlite3.Row) -> RegionOperationRecord:
    return RegionOperationRecord(
        id=row["id"],
        revision_id=row["revision_id"],
        sequence=row["sequence"],
        operation_type=row["operation_type"],
        selection=_json_object(row["selection_json"], field_name="operation selection"),
        parameters=_json_object(row["parameters_json"], field_name="operation parameters"),
        source=row["source"],
        provenance=_json_object(row["provenance_json"], field_name="operation provenance"),
        created_at=row["created_at"],
    )
