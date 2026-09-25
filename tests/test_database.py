import hashlib
import json
import sqlite3
import threading

import pytest

from image23mf.storage import CURRENT_DATABASE_VERSION, DraftRepository, open_database
from image23mf.storage.migrations import (
    MIGRATIONS,
    Migration,
    MigrationError,
    UnsupportedDatabaseVersionError,
    apply_migrations,
)
from image23mf.storage.repositories import ProjectRepository

EXPECTED_TABLES = {
    "artifacts",
    "assets",
    "bundle_imports",
    "filaments",
    "jobs",
    "mural_plans",
    "presets",
    "project_drafts",
    "projects",
    "region_operations",
    "revision_publications",
    "revision_history_seals",
    "draft_history_lineages",
    "draft_history_states",
    "draft_history_nodes",
    "draft_history_heads",
    "draft_history_receipts",
    "revisions",
    "schema_migrations",
}


def test_empty_database_bootstraps_with_required_pragmas_and_tables(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]

        assert tables >= EXPECTED_TABLES
        assert version == CURRENT_DATABASE_VERSION
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        connection.close()


def test_migrations_are_idempotent_on_reopen(tmp_path) -> None:
    path = tmp_path / "image23mf.sqlite3"
    open_database(path).close()
    connection = open_database(path)
    try:
        rows = connection.execute("SELECT version, name FROM schema_migrations").fetchall()
        assert [(row["version"], row["name"]) for row in rows] == [
            (1, "initial_workspace_schema"),
            (2, "revision_relationship_integrity"),
            (3, "draft_branching_and_operation_integrity"),
            (4, "stable_job_state_contract"),
            (5, "job_supersession_heads"),
            (6, "portable_bundle_import_history"),
            (7, "artifact_ownership_scoped_derivations"),
            (8, "immutable_revision_publication_evidence"),
            (9, "sealed_revision_history"),
            (10, "server_authoritative_draft_command_history"),
            (11, "draft_history_lifecycle_cleanup"),
            (12, "startup_reconciliation_history"),
            (13, "versioned_master_canvas_mural_plans"),
            (14, "durable_prompted_edit_alternatives"),
            (15, "prompted_edit_job_contract"),
            (16, "tamper_evident_calibration_evidence_registry"),
            (17, "versioned_calibration_catalog_promotions"),
            (18, "resumable_calibration_run_drafts"),
        ]
    finally:
        connection.close()


def test_pre_v8_workspace_upgrades_evidence_and_seals_without_data_loss(
    tmp_path,
) -> None:
    path = tmp_path / "existing.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection, MIGRATIONS[:7])
    legacy_project = ProjectRepository(connection).create("Before revision evidence")
    connection.close()

    upgraded = open_database(path)
    try:
        assert ProjectRepository(upgraded).get(legacy_project.id).name == legacy_project.name
        table = upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'revision_publications'"
        ).fetchone()
        assert table is not None
        assert (
            upgraded.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            == CURRENT_DATABASE_VERSION
        )
    finally:
        upgraded.close()


def test_pre_v9_workspace_backfills_history_seals(tmp_path) -> None:
    path = tmp_path / "pre-v9.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection, MIGRATIONS[:8])
    connection.execute("INSERT INTO projects(id, name) VALUES ('project_old', 'Old')")
    connection.execute(
        """
        INSERT INTO assets(
            id, sha256, media_type, extension, original_filename,
            relative_path, byte_size
        ) VALUES ('asset_old', ?, 'image/png', '.png', 'old.png', 'assets/00/old.png', 0)
        """,
        ("0" * 64,),
    )
    connection.execute(
        """
        INSERT INTO revisions(
            id, project_id, source_asset_id, schema_version, engine_version,
            config_json, config_sha256
        ) VALUES ('revision_old', 'project_old', 'asset_old', 1, 'old', '{}', ?)
        """,
        ("1" * 64,),
    )
    connection.commit()
    connection.close()

    upgraded = open_database(path)
    try:
        seal = upgraded.execute(
            "SELECT revision_id FROM revision_history_seals WHERE revision_id = 'revision_old'"
        ).fetchone()
        assert seal is not None
        assert (
            upgraded.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            == CURRENT_DATABASE_VERSION
        )
    finally:
        upgraded.close()


def test_pre_v10_draft_lazily_synthesizes_one_exact_anchor(tmp_path) -> None:
    path = tmp_path / "pre-v10.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection, MIGRATIONS[:9])
    connection.execute("INSERT INTO projects(id, name) VALUES ('project_old', 'Old')")
    connection.execute(
        """
        INSERT INTO assets(
            id, sha256, media_type, extension, original_filename, relative_path, byte_size
        ) VALUES ('asset_old', ?, 'image/png', '.png', 'old.png', 'assets/00/old.png', 0)
        """,
        ("0" * 64,),
    )
    config = {"schema_version": 1, "source_asset_id": "asset_old"}
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """
        INSERT INTO project_drafts(
            project_id, schema_version, config_json, operation_json, generation, config_sha256
        ) VALUES ('project_old', 1, ?, '[]', 7, ?)
        """,
        (raw, hashlib.sha256(raw.encode()).hexdigest()),
    )
    connection.commit()
    connection.close()

    upgraded = open_database(path)
    try:
        assert upgraded.execute("SELECT count(*) FROM draft_history_heads").fetchone()[0] == 0
        draft = DraftRepository(upgraded).get("project_old")
        assert draft is not None and draft.history is not None
        assert draft.history.cursor == 0
        assert draft.history.total == 0
        assert not draft.history.can_undo
        assert upgraded.execute("SELECT count(*) FROM draft_history_heads").fetchone()[0] == 1
        assert upgraded.execute("SELECT count(*) FROM draft_history_nodes").fetchone()[0] == 1
    finally:
        upgraded.close()


def test_v11_closes_orphaned_pre_fix_history_heads(tmp_path) -> None:
    path = tmp_path / "pre-v11.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection, MIGRATIONS[:10])
    connection.execute("INSERT INTO projects(id, name) VALUES ('project_old', 'Old')")
    connection.execute(
        """
        INSERT INTO assets(
            id, sha256, media_type, extension, original_filename, relative_path, byte_size
        ) VALUES ('asset_old', ?, 'image/png', '.png', 'old.png', 'assets/00/old.png', 0)
        """,
        ("0" * 64,),
    )
    config = {"schema_version": 1, "source_asset_id": "asset_old"}
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """
        INSERT INTO project_drafts(
            project_id, schema_version, config_json, operation_json, generation, config_sha256
        ) VALUES ('project_old', 1, ?, '[]', 1, ?)
        """,
        (raw, hashlib.sha256(raw.encode()).hexdigest()),
    )
    connection.commit()
    draft = DraftRepository(connection).get("project_old")
    lineage_id = draft.history.lineage_id
    connection.execute("DELETE FROM project_drafts WHERE project_id = 'project_old'")
    connection.commit()
    assert connection.execute("SELECT count(*) FROM draft_history_heads").fetchone()[0] == 1
    connection.close()

    upgraded = open_database(path)
    try:
        assert upgraded.execute("SELECT count(*) FROM draft_history_heads").fetchone()[0] == 0
        assert (
            upgraded.execute(
                "SELECT closed_at FROM draft_history_lineages WHERE id = ?", (lineage_id,)
            ).fetchone()["closed_at"]
            is not None
        )
    finally:
        upgraded.close()


def test_failed_migration_rolls_back_all_its_schema_changes() -> None:
    connection = sqlite3.connect(":memory:")
    bad = Migration(
        version=1,
        name="deliberate_failure",
        statements=("CREATE TABLE must_rollback(id TEXT)", "NOT VALID SQL"),
    )
    try:
        with pytest.raises(MigrationError, match="deliberate_failure"):
            apply_migrations(connection, (bad,))

        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'must_rollback'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
    finally:
        connection.close()


def test_newer_database_schema_is_rejected(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (99, 'future', 'now')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedDatabaseVersionError, match="newer than supported"):
        open_database(path)


def test_foreign_keys_reject_orphan_revision(tmp_path) -> None:
    connection = open_database(tmp_path / "foreign-keys.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO revisions(
                    id, project_id, source_asset_id, schema_version, engine_version,
                    config_json, config_sha256
                ) VALUES ('r1', 'missing', 'missing', 1, '0.1.0', '{}', ?)
                """,
                ("0" * 64,),
            )
    finally:
        connection.close()


def test_request_scoped_connection_allows_sequential_worker_thread_handoff(tmp_path) -> None:
    connection = open_database(tmp_path / "thread-handoff.sqlite3")
    results: list[int] = []
    failures: list[BaseException] = []

    def query_from_worker() -> None:
        try:
            results.append(int(connection.execute("SELECT 23").fetchone()[0]))
        except BaseException as error:  # pragma: no cover - assertion reports the failure
            failures.append(error)

    worker = threading.Thread(target=query_from_worker)
    worker.start()
    worker.join(timeout=2)
    connection.close()

    assert not worker.is_alive()
    assert failures == []
    assert results == [23]
