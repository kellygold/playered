import hashlib
import json
import sqlite3

import pytest

from image23mf import __version__
from image23mf.contracts.job import JobConfig, load_job_config
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    InvalidPublicationError,
    ProjectRepository,
    RegionOperation,
    RegionOperationRepository,
    RevisionPublisher,
    RevisionRepository,
    StaleDraftError,
    draft_state_fingerprint,
    open_database,
)


def config(asset_id: str, *, width_mm: float = 200) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": width_mm, "height_mm": 160},
            "palette": {
                "colors": [
                    {"id": "cream", "name": "Bone White", "hex": "#CBC6B8"},
                    {"id": "orange", "name": "Mandarin Orange", "hex": "#F99963"},
                    {"id": "blue", "name": "Marine Blue", "hex": "#0078BF"},
                    {"id": "black", "name": "Charcoal", "hex": "#000000"},
                ]
            },
        }
    )


def register_source(connection, store: ContentAddressedStore):
    blob = store.put_bytes(
        b"draft-source", namespace="assets", extension=".png", media_type="image/png"
    )
    return AssetRepository(connection, store).register(
        blob, original_filename="source.png", width_px=1000, height_px=800
    )


def cleanup_operation() -> RegionOperation:
    return RegionOperation(
        operation_type="merge-small-island",
        selection={"region_id": "orange:17"},
        parameters={"target_label": "cream"},
        source="manual",
        provenance={"tool": "region-inspector"},
    )


def test_autosave_generation_rejects_delayed_slider_write_and_reopens_exact_config(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Autosave")
        source = register_source(connection, store)
        drafts = DraftRepository(connection)

        first = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=180),
            operations=(cleanup_operation(),),
            expected_generation=0,
        )
        second = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=220),
            operations=(cleanup_operation(),),
            expected_generation=first.generation,
        )

        # A slower debounce response from the earlier slider position cannot overwrite it.
        with pytest.raises(StaleDraftError, match="expected 1, current 2"):
            drafts.save(
                project_id=project.id,
                config=config(source.id, width_mm=190),
                expected_generation=first.generation,
            )

        reopened = drafts.get(project.id)
        assert reopened == second
        assert reopened is not None
        assert reopened.generation == 2
        assert reopened.config_sha256 == config(source.id, width_mm=220).fingerprint()
        assert drafts.load_config(project.id) == config(source.id, width_mm=220)
        assert reopened.operations == (cleanup_operation(),)
    finally:
        connection.close()


def test_candidate_validation_runs_after_generation_guard_and_before_atomic_autosave(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Validated autosave")
        source = register_source(connection, store)
        drafts = DraftRepository(connection)
        original = drafts.save(
            project_id=project.id,
            config=config(source.id),
            expected_generation=0,
        )
        calls = []

        def reject_candidate(candidate, operations):
            calls.append((connection.in_transaction, candidate, operations))
            raise ValueError("candidate history does not match config")

        with pytest.raises(StaleDraftError):
            drafts.save(
                project_id=project.id,
                config=config(source.id, width_mm=210),
                expected_generation=0,
                validate_candidate=reject_candidate,
            )
        assert calls == []

        with pytest.raises(ValueError, match="history does not match"):
            drafts.save(
                project_id=project.id,
                config=config(source.id, width_mm=210),
                expected_generation=original.generation,
                validate_candidate=reject_candidate,
            )

        assert calls == [(True, config(source.id, width_mm=210), ())]
        assert connection.in_transaction is False
        assert drafts.get(project.id) == original
    finally:
        connection.close()


def test_autosave_captures_the_exact_written_draft_inside_its_transaction(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)

    class ObservedDraftRepository(DraftRepository):
        def __init__(self, observed_connection):
            super().__init__(observed_connection)
            self.read_transaction_states = []

        def get(self, project_id):
            self.read_transaction_states.append(self.connection.in_transaction)
            return super().get(project_id)

    try:
        project = ProjectRepository(connection).create("Exact autosave result")
        source = register_source(connection, store)
        drafts = ObservedDraftRepository(connection)

        written = drafts.save(
            project_id=project.id,
            config=config(source.id),
            expected_generation=0,
        )

        assert written.generation == 1
        assert drafts.read_transaction_states == [True]
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_additive_cleanup_defaults_do_not_invalidate_an_older_exact_stored_contract(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Schema-compatible draft")
        source = register_source(connection, store)
        expected = config(source.id)
        DraftRepository(connection).save(
            project_id=project.id,
            config=expected,
            expected_generation=0,
        )
        stored = expected.model_dump(mode="json")
        for field in (
            "minimum_island_diameter_mm",
            "maximum_tiny_hole_diameter_mm",
            "minimum_ring_width_mm",
            "minimum_line_width_mm",
            "minimum_neck_width_mm",
            "minimum_gap_width_mm",
            "long_line_minimum_length_mm",
        ):
            stored["cleanup"].pop(field)
        encoded = json.dumps(
            stored,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE project_drafts SET config_json = ?, config_sha256 = ?, generation = 2 "
            "WHERE project_id = ?",
            (encoded, fingerprint, project.id),
        )
        connection.commit()

        assert DraftRepository(connection).load_config(project.id) == expected

        connection.execute(
            "UPDATE project_drafts SET config_json = replace(config_json, '200.0', '201.0'), "
            "generation = 3 "
            "WHERE project_id = ?",
            (project.id,),
        )
        connection.commit()
        with pytest.raises(RuntimeError, match="fingerprint does not match"):
            DraftRepository(connection).load_config(project.id)
    finally:
        connection.close()


def test_publish_draft_clears_mutable_state_and_persists_ordered_operation_history(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Publish draft")
        source = register_source(connection, store)
        drafts = DraftRepository(connection)
        draft = drafts.save(
            project_id=project.id,
            config=config(source.id),
            operations=(
                cleanup_operation(),
                RegionOperation(
                    operation_type="fill-hole",
                    selection={"region_id": "cream:3"},
                    parameters={"label": "cream"},
                    source="automatic",
                ),
            ),
            expected_generation=0,
        )

        published = RevisionPublisher(connection, store).publish_draft(
            project_id=project.id,
            expected_generation=draft.generation,
            engine_version=__version__,
            label="Approved cleanup",
        )

        assert drafts.get(project.id) is None
        assert (
            ProjectRepository(connection).get(project.id).active_revision_id
            == published.revision.id
        )
        operations = RegionOperationRepository(connection).list_for_revision(published.revision.id)
        assert [item.sequence for item in operations] == [0, 1]
        assert [item.operation_type for item in operations] == [
            "merge-small-island",
            "fill-hole",
        ]
        assert load_job_config(dict(published.revision.config)) == config(source.id)
        assert published.revision.config_sha256 == config(source.id).fingerprint()
    finally:
        connection.close()


def test_publish_and_branch_capture_the_returned_draft_inside_the_write_transaction(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Atomic continuation")
        source = register_source(connection, store)
        drafts = DraftRepository(connection)
        projects = ProjectRepository(connection)
        draft = drafts.save(
            project_id=project.id,
            config=config(source.id),
            expected_generation=0,
        )
        publisher = RevisionPublisher(connection, store)

        class ObservedDrafts:
            def __init__(self) -> None:
                self.transaction_states: list[bool] = []

            def get(self, project_id: str):
                self.transaction_states.append(connection.in_transaction)
                return drafts.get(project_id)

        observed = ObservedDrafts()
        project_transaction_states: list[bool] = []

        class ObservedProjects:
            def get(self, project_id: str):
                project_transaction_states.append(connection.in_transaction)
                return projects.get(project_id)

        publisher.drafts = observed  # type: ignore[assignment]
        publisher.projects = ObservedProjects()  # type: ignore[assignment]
        published = publisher.publish_draft_and_continue(
            project_id=project.id,
            expected_generation=draft.generation,
            expected_draft_state_sha256=draft_state_fingerprint(
                draft.config_sha256,
                draft.operations,
                base_revision_id=draft.base_revision_id,
            ),
            engine_version=__version__,
            editor_sequence_sha256="a" * 64,
            expected_preview_derivation_key="b" * 64,
            label="Atomic checkpoint",
        )
        assert observed.transaction_states == [True, True]
        assert project_transaction_states and all(project_transaction_states)
        assert published.continuation_draft is not None
        assert published.continuation_draft.generation == draft.generation + 1

        observed.transaction_states.clear()
        project_transaction_states.clear()
        branched = publisher.branch_revision(
            project_id=project.id,
            revision_id=published.revision.id,
            expected_generation=published.continuation_draft.generation,
            validate_replay=lambda _config, _operations: None,
        )
        assert observed.transaction_states == [True, True]
        assert project_transaction_states == [True, True]
        assert branched.draft.generation == draft.generation + 2
        assert branched.draft.base_revision_id == published.revision.id
        assert branched.project.active_revision_id == published.revision.id
    finally:
        connection.close()


def test_branching_preserves_parent_bytes_and_reproduces_both_configurations(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Branches")
        source = register_source(connection, store)
        drafts = DraftRepository(connection)
        publisher = RevisionPublisher(connection, store)

        root_draft = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=180),
            expected_generation=0,
        )
        root = publisher.publish_draft(
            project_id=project.id,
            expected_generation=root_draft.generation,
            engine_version=__version__,
            label="Root",
        ).revision

        branch_draft = drafts.save(
            project_id=project.id,
            base_revision_id=root.id,
            config=config(source.id, width_mm=240),
            operations=(cleanup_operation(),),
            expected_generation=0,
        )
        branch = publisher.publish_draft(
            project_id=project.id,
            expected_generation=branch_draft.generation,
            engine_version=__version__,
            label="Branch",
        ).revision

        reopened = RevisionRepository(connection).list_for_project(project.id)
        by_id = {item.id: item for item in reopened}
        assert len(reopened) == 2
        assert by_id[branch.id].parent_revision_id == root.id
        assert load_job_config(dict(by_id[root.id].config)).canvas.width_mm == 180
        assert load_job_config(dict(by_id[branch.id].config)).canvas.width_mm == 240
        assert by_id[root.id].config_sha256 == config(source.id, width_mm=180).fingerprint()
    finally:
        connection.close()


def test_failed_draft_publication_rolls_back_revision_and_preserves_draft(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Recoverable publish")
        source = register_source(connection, store)
        drafts = DraftRepository(connection)
        draft = drafts.save(
            project_id=project.id,
            config=config(source.id),
            operations=(
                RegionOperation(
                    operation_type="reject-me",
                    selection={"region_id": "x"},
                ),
            ),
            expected_generation=0,
        )
        connection.execute(
            """
            CREATE TRIGGER reject_draft_operation
            BEFORE INSERT ON region_operations
            WHEN NEW.operation_type = 'reject-me'
            BEGIN
                SELECT RAISE(ABORT, 'simulated operation failure');
            END
            """
        )
        connection.commit()

        with pytest.raises(InvalidPublicationError, match="simulated operation failure"):
            RevisionPublisher(connection, store).publish_draft(
                project_id=project.id,
                expected_generation=draft.generation,
                engine_version=__version__,
            )

        assert drafts.get(project.id) == draft
        assert RevisionRepository(connection).list_for_project(project.id) == ()
        assert ProjectRepository(connection).get(project.id).active_revision_id is None
    finally:
        connection.close()


def test_stale_publish_and_discard_leave_newer_draft_intact(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Stale actions")
        source = register_source(connection, store)
        drafts = DraftRepository(connection)
        first = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=180),
            expected_generation=0,
        )
        latest = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=200),
            expected_generation=first.generation,
        )

        with pytest.raises(StaleDraftError, match="cannot publish stale"):
            RevisionPublisher(connection, store).publish_draft(
                project_id=project.id,
                expected_generation=first.generation,
                engine_version=__version__,
            )
        with pytest.raises(StaleDraftError, match="current 2"):
            drafts.discard(project.id, expected_generation=first.generation)

        assert drafts.get(project.id) == latest
        assert drafts.discard(project.id, expected_generation=latest.generation)
        assert drafts.get(project.id) is None
        assert (
            connection.execute(
                "SELECT count(*) FROM draft_history_heads WHERE project_id = ?", (project.id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_consuming_publish_closes_history_before_recreate_and_edit(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Publish then recreate")
        source = register_source(connection, store)
        drafts = DraftRepository(connection)
        first = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=180),
            expected_generation=0,
        )
        edited = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=190),
            expected_generation=first.generation,
        )
        old_lineage = edited.history.lineage_id

        published = RevisionPublisher(connection, store).publish_draft(
            project_id=project.id,
            expected_generation=edited.generation,
            engine_version=__version__,
        )
        assert drafts.get(project.id) is None
        assert (
            connection.execute(
                "SELECT count(*) FROM draft_history_heads WHERE project_id = ?", (project.id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT closed_at FROM draft_history_lineages WHERE id = ?", (old_lineage,)
            ).fetchone()["closed_at"]
            is not None
        )

        recreated = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=190),
            base_revision_id=published.revision.id,
            expected_generation=0,
        )
        assert recreated.history.lineage_id != old_lineage
        changed = drafts.save(
            project_id=project.id,
            config=config(source.id, width_mm=200),
            base_revision_id=published.revision.id,
            expected_generation=recreated.generation,
        )
        assert changed.history.can_undo
        assert changed.config["canvas"]["width_mm"] == 200
    finally:
        connection.close()


def test_database_rejects_cross_project_draft_base_even_outside_repository(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        projects = ProjectRepository(connection)
        first = projects.create("First")
        second = projects.create("Second")
        source = register_source(connection, store)
        root = (
            RevisionPublisher(connection, store)
            .publish(
                project_id=first.id,
                source_asset_id=source.id,
                config=config(source.id),
                engine_version=__version__,
            )
            .revision
        )

        with pytest.raises(sqlite3.IntegrityError, match="draft base revision"):
            connection.execute(
                """
                INSERT INTO project_drafts(
                    project_id, base_revision_id, schema_version, config_json,
                    operation_json, generation, config_sha256
                ) VALUES (?, ?, 1, '{}', '[]', 1, ?)
                """,
                (second.id, root.id, "0" * 64),
            )
        connection.rollback()
        assert DraftRepository(connection).get(second.id) is None
    finally:
        connection.close()
