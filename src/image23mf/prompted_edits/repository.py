"""SQLite/content-store lifecycle for immutable prompted-edit review candidates."""

# ruff: noqa: UP045 -- Runtime supports Python 3.9, which cannot evaluate PEP 604 unions.

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from image23mf.contracts.prompted import (
    PromptedAlternativeStatus,
    PromptedSessionStatus,
)
from image23mf.contracts.provenance import ProviderRegionalEditProvenance
from image23mf.prompted_edits.models import (
    EgressDisclosure,
    PromptedEditExecution,
    ProviderAlternativeFailure,
    ProviderDescriptor,
)
from image23mf.storage import AssetRecord, ContentAddressedStore, StoredBlob
from image23mf.storage.repositories import (
    RecordNotFoundError,
    canonical_json,
    immediate_transaction,
    new_id,
)


class PromptedEditConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptedAlternativeRecord:
    id: str
    session_id: str
    index: int
    status: PromptedAlternativeStatus
    output_asset_id: str
    output_sha256: str
    changed_mask: StoredBlob
    changed_mask_preview: StoredBlob
    changed_pixel_count: int
    width_px: int
    height_px: int
    provenance: ProviderRegionalEditProvenance
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PromptedSessionRecord:
    id: str
    project_id: str
    parent_revision_id: str
    retry_of_session_id: Optional[str]
    status: PromptedSessionStatus
    request_sha256: str
    selection_sha256: str
    source_asset_id: str
    mask: StoredBlob
    provider: ProviderDescriptor
    disclosure: EgressDisclosure
    request: dict[str, Any]
    failures: tuple[ProviderAlternativeFailure, ...]
    accepted_alternative_id: Optional[str]
    accepted_revision_id: Optional[str]
    rejection_reason: Optional[str]
    created_at: str
    updated_at: str
    alternatives: tuple[PromptedAlternativeRecord, ...] = ()


@dataclass(frozen=True)
class StoredPromptedAlternative:
    index: int
    output_asset: AssetRecord
    changed_mask: StoredBlob
    changed_mask_preview: StoredBlob
    changed_pixel_count: int
    width_px: int
    height_px: int
    provenance: ProviderRegionalEditProvenance


class PromptedEditRepository:
    def __init__(self, connection: sqlite3.Connection, blob_store: ContentAddressedStore) -> None:
        self.connection = connection
        self.blob_store = blob_store

    def create_prepared(
        self,
        *,
        project_id: str,
        parent_revision_id: str,
        request_sha256: str,
        selection_sha256: str,
        source_asset_id: str,
        mask: StoredBlob,
        provider: ProviderDescriptor,
        disclosure: EgressDisclosure,
        request: dict[str, Any],
        retry_of_session_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> PromptedSessionRecord:
        if not self.blob_store.verify(mask):
            raise ValueError("prompted-edit selection mask failed content verification")
        identifier = session_id or new_id("prompted")
        try:
            with immediate_transaction(self.connection):
                self.connection.execute(
                    """
                    INSERT INTO prompted_edit_sessions(
                        id, project_id, parent_revision_id, retry_of_session_id, status,
                        request_sha256, selection_sha256, source_asset_id,
                        mask_sha256, mask_relative_path, mask_byte_size,
                        provider_json, disclosure_json, request_json
                    ) VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        project_id,
                        parent_revision_id,
                        retry_of_session_id,
                        request_sha256,
                        selection_sha256,
                        source_asset_id,
                        mask.sha256,
                        mask.relative_path,
                        mask.byte_size,
                        canonical_json(provider.model_dump(mode="json")),
                        canonical_json(disclosure.model_dump(mode="json")),
                        canonical_json(request),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise PromptedEditConflictError(
                "prompted-edit preparation conflicts with existing immutable state"
            ) from error
        return self.get(identifier)

    def get(self, session_id: str) -> PromptedSessionRecord:
        row = self.connection.execute(
            "SELECT * FROM prompted_edit_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"prompted-edit session not found: {session_id}")
        alternatives = self.connection.execute(
            """
            SELECT * FROM prompted_edit_alternatives
            WHERE session_id = ? ORDER BY alternative_index
            """,
            (session_id,),
        ).fetchall()
        return _session_from_row(row, tuple(_alternative_from_row(item) for item in alternatives))

    def list_for_project(self, project_id: str) -> tuple[PromptedSessionRecord, ...]:
        rows = self.connection.execute(
            """
            SELECT id FROM prompted_edit_sessions
            WHERE project_id = ? ORDER BY created_at DESC, id DESC
            """,
            (project_id,),
        ).fetchall()
        return tuple(self.get(str(row["id"])) for row in rows)

    def transition(self, session_id: str, status: PromptedSessionStatus) -> PromptedSessionRecord:
        try:
            with immediate_transaction(self.connection):
                changed = self.connection.execute(
                    """
                    UPDATE prompted_edit_sessions
                    SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    (status.value, session_id),
                ).rowcount
                if changed != 1:
                    raise RecordNotFoundError(f"prompted-edit session not found: {session_id}")
        except sqlite3.IntegrityError as error:
            raise PromptedEditConflictError("invalid prompted-edit lifecycle transition") from error
        return self.get(session_id)

    def complete(
        self,
        session_id: str,
        *,
        execution: PromptedEditExecution,
        alternatives: tuple[StoredPromptedAlternative, ...],
    ) -> PromptedSessionRecord:
        if tuple(item.index for item in alternatives) != tuple(
            item.index for item in execution.alternatives
        ):
            raise ValueError("stored prompted alternatives do not match provider execution order")
        for stored, executed in zip(alternatives, execution.alternatives):
            if stored.output_asset.sha256 != executed.output.sha256:
                raise ValueError("stored prompted output does not match provider execution hash")
            if stored.provenance != executed.provenance:
                raise ValueError("stored prompted provenance does not match provider execution")
            if not self.blob_store.verify(stored.changed_mask) or not self.blob_store.verify(
                stored.changed_mask_preview
            ):
                raise ValueError("prompted changed-area evidence failed content verification")
        target = PromptedSessionStatus(execution.status)
        try:
            with immediate_transaction(self.connection):
                current = self.connection.execute(
                    "SELECT status FROM prompted_edit_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if current is None:
                    raise RecordNotFoundError(f"prompted-edit session not found: {session_id}")
                if current["status"] != PromptedSessionStatus.RUNNING.value:
                    raise PromptedEditConflictError("only a running prompted edit can complete")
                for item in alternatives:
                    self.connection.execute(
                        """
                        INSERT INTO prompted_edit_alternatives(
                            id, session_id, alternative_index, status, output_asset_id,
                            output_sha256, changed_mask_sha256, changed_mask_relative_path,
                            changed_mask_byte_size, changed_mask_preview_sha256,
                            changed_mask_preview_relative_path, changed_mask_preview_byte_size,
                            changed_pixel_count, width_px, height_px, provenance_json
                        ) VALUES (?, ?, ?, 'review', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("alternative"),
                            session_id,
                            item.index,
                            item.output_asset.id,
                            item.output_asset.sha256,
                            item.changed_mask.sha256,
                            item.changed_mask.relative_path,
                            item.changed_mask.byte_size,
                            item.changed_mask_preview.sha256,
                            item.changed_mask_preview.relative_path,
                            item.changed_mask_preview.byte_size,
                            item.changed_pixel_count,
                            item.width_px,
                            item.height_px,
                            canonical_json(item.provenance.model_dump(mode="json")),
                        ),
                    )
                self.connection.execute(
                    """
                    UPDATE prompted_edit_sessions
                    SET status = ?, failures_json = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    (
                        target.value,
                        canonical_json(
                            [item.model_dump(mode="json") for item in execution.failures]
                        ),
                        session_id,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise PromptedEditConflictError(
                "prompted-edit completion conflicts with immutable lifecycle evidence"
            ) from error
        return self.get(session_id)

    def reject(self, session_id: str, *, reason: str) -> PromptedSessionRecord:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("prompted-edit rejection reason cannot be empty")
        try:
            with immediate_transaction(self.connection):
                session = self.connection.execute(
                    "SELECT status FROM prompted_edit_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session is None:
                    raise RecordNotFoundError(f"prompted-edit session not found: {session_id}")
                if session["status"] not in {"complete", "partial"}:
                    raise PromptedEditConflictError(
                        "only reviewable prompted edits can be rejected"
                    )
                self.connection.execute(
                    """
                    UPDATE prompted_edit_alternatives SET status = 'rejected',
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE session_id = ? AND status = 'review'
                    """,
                    (session_id,),
                )
                self.connection.execute(
                    """
                    UPDATE prompted_edit_sessions SET status = 'rejected', rejection_reason = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?
                    """,
                    (normalized, session_id),
                )
        except sqlite3.IntegrityError as error:
            raise PromptedEditConflictError("prompted-edit rejection conflicted") from error
        return self.get(session_id)


def _blob(row: sqlite3.Row, prefix: str, *, extension: str, media_type: str) -> StoredBlob:
    return StoredBlob(
        sha256=str(row[f"{prefix}_sha256"]),
        relative_path=str(row[f"{prefix}_relative_path"]),
        byte_size=int(row[f"{prefix}_byte_size"]),
        extension=extension,
        media_type=media_type,
    )


def _alternative_from_row(row: sqlite3.Row) -> PromptedAlternativeRecord:
    return PromptedAlternativeRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        index=int(row["alternative_index"]),
        status=PromptedAlternativeStatus(row["status"]),
        output_asset_id=str(row["output_asset_id"]),
        output_sha256=str(row["output_sha256"]),
        changed_mask=_blob(
            row,
            "changed_mask",
            extension=".mask",
            media_type="application/octet-stream",
        ),
        changed_mask_preview=_blob(
            row,
            "changed_mask_preview",
            extension=".png",
            media_type="image/png",
        ),
        changed_pixel_count=int(row["changed_pixel_count"]),
        width_px=int(row["width_px"]),
        height_px=int(row["height_px"]),
        provenance=ProviderRegionalEditProvenance.model_validate_json(row["provenance_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _session_from_row(
    row: sqlite3.Row,
    alternatives: tuple[PromptedAlternativeRecord, ...],
) -> PromptedSessionRecord:
    return PromptedSessionRecord(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        parent_revision_id=str(row["parent_revision_id"]),
        retry_of_session_id=row["retry_of_session_id"],
        status=PromptedSessionStatus(row["status"]),
        request_sha256=str(row["request_sha256"]),
        selection_sha256=str(row["selection_sha256"]),
        source_asset_id=str(row["source_asset_id"]),
        mask=_blob(row, "mask", extension=".png", media_type="image/png"),
        provider=ProviderDescriptor.model_validate_json(row["provider_json"]),
        disclosure=EgressDisclosure.model_validate_json(row["disclosure_json"]),
        request=json.loads(row["request_json"]),
        failures=tuple(
            ProviderAlternativeFailure.model_validate(item)
            for item in json.loads(row["failures_json"])
        ),
        accepted_alternative_id=row["accepted_alternative_id"],
        accepted_revision_id=row["accepted_revision_id"],
        rejection_reason=row["rejection_reason"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        alternatives=alternatives,
    )
