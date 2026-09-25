"""Server-authoritative, append-only draft command history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from image23mf.storage.models import (
    DraftHistoryCommand,
    DraftHistorySummary,
    DraftRecord,
    JsonObject,
    RegionOperation,
)

UNDO_LIMIT = 100


class DraftHistoryError(RuntimeError):
    pass


class DraftHistoryConflictError(DraftHistoryError):
    pass


class DraftHistoryBoundaryError(DraftHistoryError):
    pass


@dataclass(frozen=True)
class DraftHistoryState:
    sha256: str
    lineage_id: str
    schema_version: int
    base_revision_id: str | None
    source_asset_id: str
    config: JsonObject
    operations: tuple[RegionOperation, ...]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def operation_json(operation: RegionOperation) -> dict[str, Any]:
    return {
        "operation_type": operation.operation_type,
        "selection": dict(operation.selection),
        "parameters": dict(operation.parameters),
        "source": operation.source,
        "provenance": dict(operation.provenance),
    }


def exact_state_sha256(
    *,
    lineage_id: str,
    schema_version: int,
    base_revision_id: str | None,
    source_asset_id: str,
    config: JsonObject,
    operations: Sequence[RegionOperation],
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "schema_version": schema_version,
                "lineage_id": lineage_id,
                "base_revision_id": base_revision_id,
                "source_asset_id": source_asset_id,
                "config": dict(config),
                "operations": [operation_json(item) for item in operations],
            }
        ).encode("utf-8")
    ).hexdigest()


def mutation_payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class DraftHistoryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure(self, draft: DraftRecord) -> DraftHistorySummary:
        head = self._head(draft.project_id)
        if head is None:
            return self.start_lineage(draft)
        return self.summary(draft.project_id)

    def start_lineage(self, draft: DraftRecord) -> DraftHistorySummary:
        lineage_id = _new_id("lineage")
        node_id = _new_id("history")
        self.connection.execute(
            """
            INSERT INTO draft_history_lineages(id, project_id, base_revision_id, source_asset_id)
            VALUES (?, ?, ?, ?)
            """,
            (lineage_id, draft.project_id, draft.base_revision_id, draft.config["source_asset_id"]),
        )
        state_sha = self._insert_state(lineage_id, draft)
        self.connection.execute(
            """
            INSERT INTO draft_history_nodes(
                id, project_id, lineage_id, parent_node_id, state_sha256, depth,
                request_id, schema_version, command_type, label
            ) VALUES (?, ?, ?, NULL, ?, 0, NULL, 1, 'lineage_anchor', 'History started')
            """,
            (node_id, draft.project_id, lineage_id, state_sha),
        )
        self.connection.execute(
            """
            INSERT INTO draft_history_heads(
                project_id, lineage_id, checkpoint_node_id, undo_floor_node_id,
                cursor_node_id, tip_node_id, undo_limit
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (draft.project_id, lineage_id, node_id, node_id, node_id, node_id, UNDO_LIMIT),
        )
        return self.summary(draft.project_id)

    def append(
        self,
        before: DraftRecord,
        after: DraftRecord,
        command: DraftHistoryCommand,
        *,
        payload_sha256: str,
        store_receipt: bool = True,
    ) -> DraftRecord:
        if store_receipt:
            retried = self.retry(command.request_id, "edit", payload_sha256)
            if retried is not None:
                return retried
        summary = self.ensure(before)
        if command.expected_cursor_node_id != summary.cursor_node_id:
            raise DraftHistoryConflictError("draft history cursor changed")
        if command.before_state_sha256 != summary.state_sha256:
            raise DraftHistoryConflictError("draft history state changed")
        self._assert_materialized_state(before, summary)
        if self._same_editor_state(before, after):
            result = _with_history(after, summary)
            if store_receipt:
                self._store_receipt(
                    command.request_id, before.project_id, "edit", payload_sha256, result
                )
            return result
        self._validate_command(before, after, command.command_type)

        parent = self._node(summary.cursor_node_id)
        state_sha = self._insert_state(summary.lineage_id, after)
        node_id = _new_id("history")
        self.connection.execute(
            """
            INSERT INTO draft_history_nodes(
                id, project_id, lineage_id, parent_node_id, state_sha256, depth,
                request_id, schema_version, command_type, label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                before.project_id,
                summary.lineage_id,
                summary.cursor_node_id,
                state_sha,
                int(parent["depth"]) + 1,
                command.request_id,
                command.schema_version,
                command.command_type,
                command.label,
            ),
        )
        floor = self._ancestor(
            node_id,
            UNDO_LIMIT,
            stop_at=self._head(before.project_id)["checkpoint_node_id"],
        )
        self.connection.execute(
            """
            UPDATE draft_history_heads
            SET cursor_node_id = ?, tip_node_id = ?, undo_floor_node_id = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE project_id = ?
            """,
            (node_id, node_id, floor, before.project_id),
        )
        result = _with_history(after, self.summary(before.project_id))
        if store_receipt:
            self._store_receipt(
                command.request_id, before.project_id, "edit", payload_sha256, result
            )
        return result

    def target(
        self,
        draft: DraftRecord,
        *,
        direction: str,
        request_id: str,
        expected_cursor_node_id: str,
        payload_sha256: str,
    ) -> tuple[DraftRecord | None, DraftHistoryState | None, str]:
        retried = self.retry(request_id, direction, payload_sha256)
        if retried is not None:
            return retried, None, retried.history.cursor_node_id  # type: ignore[union-attr]
        summary = self.ensure(draft)
        if expected_cursor_node_id != summary.cursor_node_id:
            raise DraftHistoryConflictError("draft history cursor changed")
        self._assert_materialized_state(draft, summary)
        if direction == "undo":
            if not summary.can_undo:
                raise DraftHistoryBoundaryError("no earlier command is available")
            target_id = str(self._node(summary.cursor_node_id)["parent_node_id"])
        elif direction == "redo":
            if not summary.can_redo:
                raise DraftHistoryBoundaryError("no redo command is available")
            target_id = self._next_on_tip_path(summary.cursor_node_id, summary.tip_node_id)
        else:  # pragma: no cover - private caller controls this
            raise ValueError(direction)
        return None, self.state_for_node(target_id), target_id

    def finish_move(
        self,
        draft: DraftRecord,
        *,
        direction: str,
        request_id: str,
        payload_sha256: str,
        target_node_id: str,
    ) -> DraftRecord:
        self.connection.execute(
            """
            UPDATE draft_history_heads
            SET cursor_node_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE project_id = ?
            """,
            (target_node_id, draft.project_id),
        )
        result = _with_history(draft, self.summary(draft.project_id))
        self._store_receipt(request_id, draft.project_id, direction, payload_sha256, result)
        return result

    def checkpoint_publication(self, draft: DraftRecord, revision_id: str) -> DraftHistorySummary:
        summary = self.ensure(draft)
        parent = self._node(summary.cursor_node_id)
        node_id = _new_id("history")
        self.connection.execute(
            """
            INSERT INTO draft_history_nodes(
                id, project_id, lineage_id, parent_node_id, state_sha256, depth,
                schema_version, command_type, label, checkpoint_revision_id
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 'publication_checkpoint', 'Published revision', ?)
            """,
            (
                node_id,
                draft.project_id,
                summary.lineage_id,
                summary.cursor_node_id,
                parent["state_sha256"],
                int(parent["depth"]) + 1,
                revision_id,
            ),
        )
        self.connection.execute(
            """
            UPDATE draft_history_lineages
            SET closed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ? AND closed_at IS NULL
            """,
            (summary.lineage_id,),
        )
        self.connection.execute(
            "DELETE FROM draft_history_heads WHERE project_id = ?", (draft.project_id,)
        )
        return self.start_lineage(draft)

    def reset_lineage(self, draft: DraftRecord) -> DraftHistorySummary:
        head = self._head(draft.project_id)
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
                "DELETE FROM draft_history_heads WHERE project_id = ?", (draft.project_id,)
            )
        return self.start_lineage(draft)

    def summary(self, project_id: str) -> DraftHistorySummary:
        head = self._head(project_id)
        if head is None:
            raise DraftHistoryError("draft history head is missing")
        cursor = self._node(head["cursor_node_id"])
        tip = self._node(head["tip_node_id"])
        checkpoint = self._node(head["checkpoint_node_id"])
        can_undo = cursor["id"] != head["undo_floor_node_id"]
        can_redo = cursor["id"] != tip["id"]
        redo = self._next_on_tip_path(str(cursor["id"]), str(tip["id"])) if can_redo else None
        redo_label = None if redo is None else str(self._node(redo)["label"])
        return DraftHistorySummary(
            lineage_id=str(head["lineage_id"]),
            cursor_node_id=str(cursor["id"]),
            tip_node_id=str(tip["id"]),
            cursor=int(cursor["depth"]) - int(checkpoint["depth"]),
            total=int(tip["depth"]) - int(checkpoint["depth"]),
            limit=int(head["undo_limit"]),
            can_undo=can_undo,
            can_redo=can_redo,
            undo_label=str(cursor["label"]) if can_undo else None,
            redo_label=redo_label,
            state_sha256=str(cursor["state_sha256"]),
        )

    def state_for_node(self, node_id: str) -> DraftHistoryState:
        row = self.connection.execute(
            """
            SELECT states.* FROM draft_history_nodes nodes
            JOIN draft_history_states states ON states.sha256 = nodes.state_sha256
            WHERE nodes.id = ?
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            raise DraftHistoryError("draft history state is missing")
        config = json.loads(row["config_json"])
        operations = json.loads(row["operation_json"])
        if not isinstance(config, dict) or not isinstance(operations, list):
            raise DraftHistoryError("draft history state payload is invalid")
        parsed = tuple(_operation_from_json(item) for item in operations)
        expected = exact_state_sha256(
            lineage_id=row["lineage_id"],
            schema_version=row["schema_version"],
            base_revision_id=row["base_revision_id"],
            source_asset_id=row["source_asset_id"],
            config=config,
            operations=parsed,
        )
        if expected != row["sha256"]:
            raise DraftHistoryError("draft history state fingerprint is invalid")
        return DraftHistoryState(
            sha256=row["sha256"],
            lineage_id=row["lineage_id"],
            schema_version=row["schema_version"],
            base_revision_id=row["base_revision_id"],
            source_asset_id=row["source_asset_id"],
            config=config,
            operations=parsed,
        )

    def retry(self, request_id: str, mutation_type: str, payload_sha256: str) -> DraftRecord | None:
        row = self.connection.execute(
            "SELECT * FROM draft_history_receipts WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["mutation_type"] != mutation_type or row["payload_sha256"] != payload_sha256:
            raise DraftHistoryConflictError("history request id was reused with different input")
        return _draft_from_receipt(row["result_json"])

    def _insert_state(self, lineage_id: str, draft: DraftRecord) -> str:
        source_asset_id = str(draft.config["source_asset_id"])
        config_json = canonical_json(draft.config)
        operations_json = canonical_json([operation_json(item) for item in draft.operations])
        sha = exact_state_sha256(
            lineage_id=lineage_id,
            schema_version=draft.schema_version,
            base_revision_id=draft.base_revision_id,
            source_asset_id=source_asset_id,
            config=draft.config,
            operations=draft.operations,
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO draft_history_states(
                sha256, lineage_id, schema_version, base_revision_id, source_asset_id,
                config_json, operation_json, byte_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha,
                lineage_id,
                draft.schema_version,
                draft.base_revision_id,
                source_asset_id,
                config_json,
                operations_json,
                len(config_json.encode()) + len(operations_json.encode()),
            ),
        )
        return sha

    def _store_receipt(
        self,
        request_id: str,
        project_id: str,
        mutation_type: str,
        payload_sha256: str,
        result: DraftRecord,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO draft_history_receipts(
                request_id, project_id, mutation_type, payload_sha256, result_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (request_id, project_id, mutation_type, payload_sha256, _draft_receipt_json(result)),
        )

    def _validate_command(self, before: DraftRecord, after: DraftRecord, kind: str) -> None:
        config_changed = before.config_sha256 != after.config_sha256
        before_ops = before.operations
        after_ops = after.operations
        if kind == "manual_operation":
            if (
                config_changed
                or len(after_ops) <= len(before_ops)
                or after_ops[: len(before_ops)] != before_ops
            ):
                raise DraftHistoryConflictError(
                    "manual operation must append operations without changing config"
                )
        elif kind == "clear_manual_and_apply":
            retained = tuple(item for item in before_ops if item.operation_type == "palette-edit")
            if not config_changed or after_ops != retained:
                raise DraftHistoryConflictError(
                    "clear-and-apply must change config and retain only palette audit operations"
                )
        elif kind == "palette_change":
            if not config_changed:
                raise DraftHistoryConflictError("palette change did not change config")
            before_config = dict(before.config)
            after_config = dict(after.config)
            before_config.pop("palette", None)
            after_config.pop("palette", None)
            if before_config != after_config:
                raise DraftHistoryConflictError("palette change modified non-palette config")
            appended = (
                after_ops[len(before_ops) :] if after_ops[: len(before_ops)] == before_ops else ()
            )
            if after_ops != before_ops and (
                not appended or any(item.operation_type != "palette-edit" for item in appended)
            ):
                raise DraftHistoryConflictError(
                    "palette change may only append palette audit operations"
                )
        elif kind == "config_change":
            if not config_changed or before_ops != after_ops:
                raise DraftHistoryConflictError("config change did not change config")
        elif kind not in {"editor_update", "preview_commit"}:
            raise DraftHistoryConflictError("unsupported history command type")

    @staticmethod
    def _same_editor_state(before: DraftRecord, after: DraftRecord) -> bool:
        return (
            before.base_revision_id == after.base_revision_id
            and before.config_sha256 == after.config_sha256
            and before.operations == after.operations
        )

    @staticmethod
    def _assert_materialized_state(draft: DraftRecord, summary: DraftHistorySummary) -> None:
        actual = exact_state_sha256(
            lineage_id=summary.lineage_id,
            schema_version=draft.schema_version,
            base_revision_id=draft.base_revision_id,
            source_asset_id=str(draft.config["source_asset_id"]),
            config=draft.config,
            operations=draft.operations,
        )
        if actual != summary.state_sha256:
            raise DraftHistoryConflictError("materialized draft does not match history cursor")

    def _head(self, project_id: str):
        return self.connection.execute(
            "SELECT * FROM draft_history_heads WHERE project_id = ?", (project_id,)
        ).fetchone()

    def _node(self, node_id: str):
        row = self.connection.execute(
            "SELECT * FROM draft_history_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise DraftHistoryError("draft history node is missing")
        return row

    def _ancestor(self, node_id: str, steps: int, *, stop_at: str) -> str:
        current = node_id
        for _ in range(steps):
            if current == stop_at:
                break
            parent = self._node(current)["parent_node_id"]
            if parent is None:
                break
            current = str(parent)
        return current

    def _next_on_tip_path(self, cursor_id: str, tip_id: str) -> str:
        current = tip_id
        while True:
            row = self._node(current)
            parent = row["parent_node_id"]
            if parent == cursor_id:
                return current
            if parent is None:
                raise DraftHistoryError("history cursor is not an ancestor of the active tip")
            current = str(parent)


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex}"


def _operation_from_json(value: Any) -> RegionOperation:
    if not isinstance(value, dict):
        raise DraftHistoryError("draft history operation must be an object")
    try:
        return RegionOperation(
            operation_type=str(value["operation_type"]),
            selection=dict(value.get("selection", {})),
            parameters=dict(value.get("parameters", {})),
            source=str(value.get("source", "manual")),
            provenance=dict(value.get("provenance", {})),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DraftHistoryError("draft history operation is invalid") from error


def _with_history(draft: DraftRecord, history: DraftHistorySummary) -> DraftRecord:
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


def _draft_receipt_json(draft: DraftRecord) -> str:
    if draft.history is None:
        raise DraftHistoryError("history receipt requires a history summary")
    return canonical_json(
        {
            "project_id": draft.project_id,
            "base_revision_id": draft.base_revision_id,
            "schema_version": draft.schema_version,
            "config": dict(draft.config),
            "config_sha256": draft.config_sha256,
            "operations": [operation_json(item) for item in draft.operations],
            "generation": draft.generation,
            "updated_at": draft.updated_at,
            "history": draft.history.__dict__,
        }
    )


def _draft_from_receipt(raw: str) -> DraftRecord:
    value = json.loads(raw)
    history = DraftHistorySummary(**value["history"])
    return DraftRecord(
        project_id=value["project_id"],
        base_revision_id=value["base_revision_id"],
        schema_version=value["schema_version"],
        config=value["config"],
        config_sha256=value["config_sha256"],
        operations=tuple(_operation_from_json(item) for item in value["operations"]),
        generation=value["generation"],
        updated_at=value["updated_at"],
        history=history,
    )
