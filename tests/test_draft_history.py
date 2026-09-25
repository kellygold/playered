from __future__ import annotations

import io
import sqlite3
import uuid
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from image23mf import __version__
from image23mf.api.app import create_app
from image23mf.contracts.job import JobConfig
from image23mf.settings import Settings
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    DraftHistoryBoundaryReachedError,
    DraftHistoryCommand,
    DraftRepository,
    InvalidDraftHistoryError,
    ProjectRepository,
    RegionOperation,
    RevisionPublisher,
    draft_state_fingerprint,
    open_database,
)


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), (30, 90, 160)).save(output, format="PNG")
    return output.getvalue()


def imported(client: TestClient) -> dict:
    response = client.post(
        "/api/projects/import?project_name=History",
        content=image_bytes(),
        headers={"X-Filename": "history.png", "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def command(draft: dict, kind: str, label: str, *, request_id: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "id": request_id or str(uuid.uuid4()),
        "command_type": kind,
        "label": label,
        "before_state_sha256": draft["history"]["state_sha256"],
        "expected_cursor_node_id": draft["history"]["cursor_node_id"],
    }


def save(client: TestClient, project_id: str, draft: dict, config: dict, metadata: dict):
    return client.put(
        f"/api/projects/{project_id}/draft",
        json={
            "config": config,
            "operations": draft["operations"],
            "expected_draft_generation": draft["generation"],
            "history_command": metadata,
        },
    )


def move(client: TestClient, project_id: str, draft: dict, direction: str, request_id=None):
    body = {
        "request_id": request_id or str(uuid.uuid4()),
        "expected_draft_generation": draft["generation"],
        "expected_cursor_node_id": draft["history"]["cursor_node_id"],
    }
    return (
        client.post(f"/api/projects/{project_id}/draft/history/{direction}", json=body),
        body,
    )


def test_api_history_reload_idempotency_undo_redo_and_abandoned_redo_audit(tmp_path) -> None:
    root = tmp_path / "workspace"
    app = create_app(Settings(workspace=root))
    with TestClient(app) as client:
        workspace = imported(client)
        project_id = workspace["project"]["id"]
        initial = workspace["draft"]
        assert len(initial["history"]["state_sha256"]) == 64
        assert initial["history"]["total"] == 0
        assert not initial["history"]["can_undo"]

        for internal_kind in ("editor_update", "preview_commit"):
            forbidden = client.put(
                f"/api/projects/{project_id}/draft",
                json={
                    "config": initial["config"],
                    "operations": initial["operations"],
                    "expected_draft_generation": initial["generation"],
                    "history_command": {
                        **command(initial, "config_change", "Internal type"),
                        "command_type": internal_kind,
                    },
                },
            )
            assert forbidden.status_code == 422
        assert client.get(f"/api/projects/{project_id}").json()["draft"] == initial

        changed_config = deepcopy(initial["config"])
        changed_config["canvas"]["width_mm"] = 199
        request_id = str(uuid.uuid4())
        metadata = command(initial, "config_change", "Narrow canvas", request_id=request_id)
        response = save(client, project_id, initial, changed_config, metadata)
        assert response.status_code == 200, response.text
        changed = response.json()
        assert changed["history"]["undo_label"] == "Narrow canvas"
        assert changed["history"]["total"] == 1

        stale_cursor = save(
            client,
            project_id,
            changed,
            {**changed_config, "canvas": {**changed_config["canvas"], "height_mm": 159}},
            {
                **command(changed, "config_change", "Must not land"),
                "expected_cursor_node_id": initial["history"]["cursor_node_id"],
            },
        )
        assert stale_cursor.status_code == 409
        assert client.get(f"/api/projects/{project_id}").json()["draft"] == changed

        retry = save(client, project_id, initial, changed_config, metadata)
        assert retry.status_code == 200
        assert retry.json() == changed
        mismatch = save(
            client,
            project_id,
            initial,
            changed_config,
            {**metadata, "label": "Reused differently"},
        )
        assert mismatch.status_code == 409

        undone_response, undo_body = move(client, project_id, changed, "undo")
        assert undone_response.status_code == 200, undone_response.text
        undone = undone_response.json()
        assert undone["config"] == initial["config"]
        assert undone["history"]["can_redo"]
        assert client.get(f"/api/projects/{project_id}").json()["draft"] == undone
        undo_retry = client.post(f"/api/projects/{project_id}/draft/history/undo", json=undo_body)
        assert undo_retry.status_code == 200
        assert undo_retry.json() == undone
        wrong_reuse = client.post(f"/api/projects/{project_id}/draft/history/redo", json=undo_body)
        assert wrong_reuse.status_code == 409

        redone_response, _ = move(client, project_id, undone, "redo")
        assert redone_response.status_code == 200, redone_response.text
        redone = redone_response.json()
        assert redone["config"] == changed["config"]
        assert client.get(f"/api/projects/{project_id}").json()["draft"] == redone

        branch_point_response, _ = move(client, project_id, redone, "undo")
        branch_point = branch_point_response.json()
        branch_config = deepcopy(branch_point["config"])
        branch_config["canvas"]["height_mm"] = 159
        branch = save(
            client,
            project_id,
            branch_point,
            branch_config,
            command(branch_point, "config_change", "Shorter canvas"),
        )
        assert branch.status_code == 200, branch.text
        branched = branch.json()
        assert not branched["history"]["can_redo"]

        reopened = client.get(f"/api/projects/{project_id}")
        assert reopened.status_code == 200
        assert reopened.json()["draft"] == branched

        # A lost response can be retried even after later work. It returns the original
        # receipt but must not move the current materialized draft or cursor backwards.
        late_retry = save(client, project_id, initial, changed_config, metadata)
        assert late_retry.status_code == 200
        assert late_retry.json() == changed
        assert client.get(f"/api/projects/{project_id}").json()["draft"] == branched

    connection = open_database(root / "image23mf.sqlite3")
    try:
        nodes = connection.execute(
            "SELECT command_type, label FROM draft_history_nodes WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        assert {(row["command_type"], row["label"]) for row in nodes} == {
            ("lineage_anchor", "History started"),
            ("config_change", "Narrow canvas"),
            ("config_change", "Shorter canvas"),
        }
        node_id = connection.execute(
            "SELECT id FROM draft_history_nodes WHERE project_id = ? LIMIT 1", (project_id,)
        ).fetchone()["id"]
        state_sha = connection.execute(
            "SELECT state_sha256 FROM draft_history_nodes WHERE id = ?", (node_id,)
        ).fetchone()["state_sha256"]
        for statement, parameters in (
            ("UPDATE draft_history_nodes SET label = 'tampered' WHERE id = ?", (node_id,)),
            ("DELETE FROM draft_history_nodes WHERE id = ?", (node_id,)),
            ("UPDATE draft_history_states SET config_json = '{}' WHERE sha256 = ?", (state_sha,)),
            ("DELETE FROM draft_history_states WHERE sha256 = ?", (state_sha,)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
            connection.rollback()
        receipt_id = connection.execute(
            "SELECT request_id FROM draft_history_receipts WHERE project_id = ? LIMIT 1",
            (project_id,),
        ).fetchone()["request_id"]
        for statement in (
            "UPDATE draft_history_receipts SET result_json = '{}' WHERE request_id = ?",
            "DELETE FROM draft_history_receipts WHERE request_id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (receipt_id,))
            connection.rollback()
        head = connection.execute(
            "SELECT cursor_node_id FROM draft_history_heads WHERE project_id = ?", (project_id,)
        ).fetchone()
        materialized = DraftRepository(connection).get(project_id)
        assert materialized is not None
        assert head["cursor_node_id"] == materialized.history.cursor_node_id
    finally:
        connection.close()


def repository_config(asset_id: str, width: float = 200) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": width, "height_mm": 160},
            "palette": {
                "colors": [
                    {"id": "light", "name": "Light", "hex": "#CBC6B8"},
                    {"id": "dark", "name": "Dark", "hex": "#000000"},
                ]
            },
        }
    )


def setup_repository(tmp_path):
    root = tmp_path / "repository"
    connection = open_database(root / "image23mf.sqlite3")
    store = ContentAddressedStore(root)
    blob = store.put_bytes(b"source", namespace="assets", extension=".png", media_type="image/png")
    asset = AssetRepository(connection, store).register(
        blob, original_filename="source.png", width_px=32, height_px=24
    )
    project = ProjectRepository(connection).create("History")
    draft = DraftRepository(connection).save(
        project_id=project.id, config=repository_config(asset.id), expected_generation=0
    )
    return connection, store, project, draft


def storage_command(draft, kind, label):
    return DraftHistoryCommand(
        request_id=str(uuid.uuid4()),
        schema_version=1,
        command_type=kind,
        label=label,
        before_state_sha256=draft.history.state_sha256,
        expected_cursor_node_id=draft.history.cursor_node_id,
    )


def test_clear_and_apply_is_one_reversible_node_and_preserves_palette_audit(tmp_path) -> None:
    connection, _, project, draft = setup_repository(tmp_path)
    drafts = DraftRepository(connection)
    palette = RegionOperation(
        operation_type="palette-edit",
        selection={},
        parameters={"action": "lock"},
        source="manual",
        provenance={},
    )
    manual = RegionOperation(
        operation_type="editor_command_v1",
        selection={},
        parameters={"command": {}},
        source="manual",
        provenance={},
    )
    try:
        seeded = drafts.save(
            project_id=project.id,
            config=repository_config(draft.config["source_asset_id"]),
            operations=(palette, manual),
            expected_generation=draft.generation,
            validate_candidate=lambda *_: None,
        )
        cleared = drafts.save(
            project_id=project.id,
            config=repository_config(draft.config["source_asset_id"], 199),
            operations=(palette,),
            expected_generation=seeded.generation,
            validate_candidate=lambda *_: None,
            history_command=storage_command(
                seeded, "clear_manual_and_apply", "Clear 1 manual edit and apply"
            ),
        )
        assert cleared.operations == (palette,)
        assert cleared.history.undo_label == "Clear 1 manual edit and apply"
        restored = drafts.move_history(
            project_id=project.id,
            direction="undo",
            request_id=str(uuid.uuid4()),
            expected_generation=cleared.generation,
            expected_cursor_node_id=cleared.history.cursor_node_id,
            validate_candidate=lambda *_: None,
        )
        assert restored.operations == (palette, manual)
        assert restored.config_sha256 == seeded.config_sha256
        redone = drafts.move_history(
            project_id=project.id,
            direction="redo",
            request_id=str(uuid.uuid4()),
            expected_generation=restored.generation,
            expected_cursor_node_id=restored.history.cursor_node_id,
            validate_candidate=lambda *_: None,
        )
        assert redone.operations == (palette,)
        assert redone.config_sha256 == cleared.config_sha256
    finally:
        connection.close()


def test_history_horizon_is_100_and_publication_and_branch_are_boundaries(tmp_path) -> None:
    connection, store, project, draft = setup_repository(tmp_path)
    drafts = DraftRepository(connection)
    try:
        current = draft
        for index in range(105):
            current = drafts.save(
                project_id=project.id,
                config=repository_config(draft.config["source_asset_id"], 100 + index / 10),
                expected_generation=current.generation,
                validate_candidate=lambda *_: None,
                history_command=storage_command(current, "config_change", f"Step {index + 1}"),
            )
        assert current.history.total == 105
        assert current.history.limit == 100
        for _ in range(100):
            current = drafts.move_history(
                project_id=project.id,
                direction="undo",
                request_id=str(uuid.uuid4()),
                expected_generation=current.generation,
                expected_cursor_node_id=current.history.cursor_node_id,
                validate_candidate=lambda *_: None,
            )
        assert not current.history.can_undo
        with pytest.raises(DraftHistoryBoundaryReachedError):
            drafts.move_history(
                project_id=project.id,
                direction="undo",
                request_id=str(uuid.uuid4()),
                expected_generation=current.generation,
                expected_cursor_node_id=current.history.cursor_node_id,
                validate_candidate=lambda *_: None,
            )

        publisher = RevisionPublisher(connection, store)
        prepublication_lineage = current.history.lineage_id
        published = publisher.publish_draft_and_continue(
            project_id=project.id,
            expected_generation=current.generation,
            expected_draft_state_sha256=draft_state_fingerprint(
                current.config_sha256,
                current.operations,
                base_revision_id=current.base_revision_id,
            ),
            engine_version=__version__,
            editor_sequence_sha256="a" * 64,
            expected_preview_derivation_key="b" * 64,
            label="Checkpoint",
        )
        assert not published.continuation_draft.history.can_undo
        assert published.continuation_draft.history.lineage_id != prepublication_lineage
        closed = connection.execute(
            "SELECT closed_at FROM draft_history_lineages WHERE id = ?",
            (prepublication_lineage,),
        ).fetchone()
        assert closed["closed_at"] is not None
        marker = connection.execute(
            """
            SELECT command_type, checkpoint_revision_id FROM draft_history_nodes
            WHERE lineage_id = ? AND checkpoint_revision_id IS NOT NULL LIMIT 1
            """,
            (prepublication_lineage,),
        ).fetchone()
        assert (marker["command_type"], marker["checkpoint_revision_id"]) == (
            "publication_checkpoint",
            published.revision.id,
        )
        lineage = published.continuation_draft.history.lineage_id
        branched = publisher.branch_revision(
            project_id=project.id,
            revision_id=published.revision.id,
            expected_generation=published.continuation_draft.generation,
            validate_replay=lambda *_: None,
        )
        assert branched.draft.history.lineage_id != lineage
        assert not branched.draft.history.can_undo
        assert (
            ProjectRepository(connection).get(project.id).active_revision_id
            == published.revision.id
        )
    finally:
        connection.close()


def test_two_clients_stale_generation_and_cursor_leave_draft_and_head_unchanged(tmp_path) -> None:
    root = tmp_path / "workspace"
    first_app = create_app(Settings(workspace=root))
    second_app = create_app(Settings(workspace=root))
    with TestClient(first_app) as first, TestClient(second_app) as second:
        workspace = imported(first)
        project_id = workspace["project"]["id"]
        initial = workspace["draft"]
        second_view = second.get(f"/api/projects/{project_id}").json()["draft"]
        candidate = deepcopy(second_view["config"])
        candidate["canvas"]["width_mm"] = 198
        landed = save(
            second,
            project_id,
            second_view,
            candidate,
            command(second_view, "config_change", "Second client wins"),
        )
        assert landed.status_code == 200
        winner = landed.json()

        stale_config = deepcopy(initial["config"])
        stale_config["canvas"]["height_mm"] = 158
        stale_generation = save(
            first,
            project_id,
            initial,
            stale_config,
            command(initial, "config_change", "Late first client"),
        )
        assert stale_generation.status_code == 409
        wrong_cursor = save(
            first,
            project_id,
            winner,
            stale_config,
            {
                **command(winner, "config_change", "Wrong cursor"),
                "expected_cursor_node_id": initial["history"]["cursor_node_id"],
            },
        )
        assert wrong_cursor.status_code == 409
        assert first.get(f"/api/projects/{project_id}").json()["draft"] == winner

    connection = open_database(root / "image23mf.sqlite3")
    try:
        head = connection.execute(
            "SELECT cursor_node_id, tip_node_id FROM draft_history_heads WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        assert head["cursor_node_id"] == winner["history"]["cursor_node_id"]
        assert head["tip_node_id"] == winner["history"]["tip_node_id"]
        assert (
            connection.execute(
                "SELECT count(*) FROM draft_history_nodes WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()


def test_semantic_commands_legacy_save_noop_receipt_and_failed_replay_are_atomic(tmp_path) -> None:
    connection, _, project, initial = setup_repository(tmp_path)
    drafts = DraftRepository(connection)
    asset_id = initial.config["source_asset_id"]
    palette_audit = RegionOperation(
        operation_type="palette-edit",
        selection={},
        parameters={"action": "rename"},
        source="manual",
        provenance={},
    )
    manual = RegionOperation(
        operation_type="editor_command_v1",
        selection={},
        parameters={"command": {}},
        source="manual",
        provenance={},
    )
    try:
        manual_step = drafts.save(
            project_id=project.id,
            config=repository_config(asset_id),
            operations=(manual,),
            expected_generation=initial.generation,
            validate_candidate=lambda *_: None,
            history_command=storage_command(initial, "manual_operation", "Keep region"),
        )
        with pytest.raises(InvalidDraftHistoryError):
            drafts.save(
                project_id=project.id,
                config=repository_config(asset_id, 199),
                operations=(manual, manual),
                expected_generation=manual_step.generation,
                validate_candidate=lambda *_: None,
                history_command=storage_command(
                    manual_step, "manual_operation", "Invalid mixed mutation"
                ),
            )
        assert drafts.get(project.id) == manual_step

        # Generic clients remain compatible and still receive a named, undoable server step.
        legacy = drafts.save(
            project_id=project.id,
            config=repository_config(asset_id, 199),
            operations=(manual,),
            expected_generation=manual_step.generation,
            validate_candidate=lambda *_: None,
        )
        assert legacy.history.undo_label == "Editor update"

        palette_config = repository_config(asset_id, 199).model_copy(
            update={
                "palette": repository_config(asset_id, 199).palette.model_copy(
                    update={
                        "colors": tuple(
                            color.model_copy(update={"name": "Renamed"}) if index == 0 else color
                            for index, color in enumerate(
                                repository_config(asset_id, 199).palette.colors
                            )
                        )
                    }
                )
            }
        )
        palette_step = drafts.save(
            project_id=project.id,
            config=palette_config,
            operations=(manual, palette_audit),
            expected_generation=legacy.generation,
            validate_candidate=lambda *_: None,
            history_command=storage_command(legacy, "palette_change", "Rename color"),
        )
        assert palette_step.history.undo_label == "Rename color"

        # An explicit no-op advances generation but adds no node and has a durable receipt.
        noop_command = storage_command(palette_step, "config_change", "Normalized no-op")
        node_count = connection.execute("SELECT count(*) FROM draft_history_nodes").fetchone()[0]
        noop = drafts.save(
            project_id=project.id,
            config=palette_config,
            operations=(manual, palette_audit),
            expected_generation=palette_step.generation,
            validate_candidate=lambda *_: None,
            history_command=noop_command,
        )
        assert noop.generation == palette_step.generation + 1
        assert (
            connection.execute("SELECT count(*) FROM draft_history_nodes").fetchone()[0]
            == node_count
        )
        retry = drafts.save(
            project_id=project.id,
            config=palette_config,
            operations=(manual, palette_audit),
            expected_generation=palette_step.generation,
            validate_candidate=lambda *_: None,
            history_command=noop_command,
        )
        assert retry == noop

        cursor = noop.history.cursor_node_id
        generation = noop.generation
        with pytest.raises(RuntimeError, match="replay rejected"):
            drafts.move_history(
                project_id=project.id,
                direction="undo",
                request_id=str(uuid.uuid4()),
                expected_generation=generation,
                expected_cursor_node_id=cursor,
                validate_candidate=lambda *_: (_ for _ in ()).throw(
                    RuntimeError("replay rejected")
                ),
            )
        unchanged = drafts.get(project.id)
        assert unchanged.generation == generation
        assert unchanged.history.cursor_node_id == cursor
    finally:
        connection.close()


def test_database_rejects_cross_project_and_cross_lineage_history_identity(tmp_path) -> None:
    connection, store, first_project, first_draft = setup_repository(tmp_path)
    drafts = DraftRepository(connection)
    try:
        asset_id = first_draft.config["source_asset_id"]
        second_project = ProjectRepository(connection).create("Other history")
        second_draft = drafts.save(
            project_id=second_project.id,
            config=repository_config(asset_id),
            expected_generation=0,
        )
        second_revision = (
            RevisionPublisher(connection, store)
            .publish(
                project_id=second_project.id,
                source_asset_id=asset_id,
                config=repository_config(asset_id),
                engine_version=__version__,
            )
            .revision
        )
        first_revision = (
            RevisionPublisher(connection, store)
            .publish(
                project_id=first_project.id,
                source_asset_id=asset_id,
                config=repository_config(asset_id),
                engine_version=__version__,
            )
            .revision
        )
        other_blob = store.put_bytes(
            b"other-source",
            namespace="assets",
            extension=".png",
            media_type="image/png",
        )
        other_asset = AssetRepository(connection, store).register(
            other_blob,
            original_filename="other.png",
            width_px=8,
            height_px=8,
        )
        first_head = connection.execute(
            "SELECT * FROM draft_history_heads WHERE project_id = ?", (first_project.id,)
        ).fetchone()
        second_head = connection.execute(
            "SELECT * FROM draft_history_heads WHERE project_id = ?", (second_project.id,)
        ).fetchone()
        second_state = connection.execute(
            "SELECT * FROM draft_history_states WHERE lineage_id = ?",
            (second_draft.history.lineage_id,),
        ).fetchone()

        attempts = (
            (
                "UPDATE draft_history_heads SET cursor_node_id = ? WHERE project_id = ?",
                (second_head["cursor_node_id"], first_project.id),
            ),
            (
                """
                INSERT INTO draft_history_nodes(
                    id, project_id, lineage_id, state_sha256, depth,
                    schema_version, command_type, label
                ) VALUES ('history_cross', ?, ?, ?, 0, 1, 'lineage_anchor', 'Cross')
                """,
                (
                    first_project.id,
                    first_head["lineage_id"],
                    second_state["sha256"],
                ),
            ),
            (
                """
                INSERT INTO draft_history_states(
                    sha256, lineage_id, schema_version, base_revision_id,
                    source_asset_id, config_json, operation_json, byte_size
                ) VALUES (?, ?, 1, ?, ?, ?, '[]', 1)
                """,
                (
                    "1" * 64,
                    first_head["lineage_id"],
                    second_revision.id,
                    asset_id,
                    second_state["config_json"],
                ),
            ),
            (
                """
                INSERT INTO draft_history_lineages(
                    id, project_id, base_revision_id, source_asset_id
                ) VALUES ('lineage_cross', ?, ?, ?)
                """,
                (first_project.id, second_revision.id, asset_id),
            ),
            (
                """
                INSERT INTO draft_history_states(
                    sha256, lineage_id, schema_version, base_revision_id,
                    source_asset_id, config_json, operation_json, byte_size
                ) VALUES (?, ?, 1, ?, ?, ?, '[]', 1)
                """,
                (
                    "2" * 64,
                    first_head["lineage_id"],
                    first_revision.id,
                    asset_id,
                    second_state["config_json"],
                ),
            ),
            (
                """
                INSERT INTO draft_history_states(
                    sha256, lineage_id, schema_version, base_revision_id,
                    source_asset_id, config_json, operation_json, byte_size
                ) VALUES (?, ?, 1, NULL, ?, ?, '[]', 1)
                """,
                (
                    "3" * 64,
                    first_head["lineage_id"],
                    other_asset.id,
                    second_state["config_json"],
                ),
            ),
        )
        for statement, parameters in attempts:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
            connection.rollback()

        unchanged = drafts.get(first_project.id)
        assert unchanged.history.cursor_node_id == first_head["cursor_node_id"]
    finally:
        connection.close()
