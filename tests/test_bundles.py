import json
import uuid
import zipfile
from pathlib import Path

import pytest

from image23mf import __version__
from image23mf.bundles import (
    BundleError,
    BundleIntegrityError,
    ProjectBundleService,
    UnsupportedBundleVersionError,
)
from image23mf.contracts.editor import (
    CanvasSelectionState,
    EditorCommandSequence,
    SourceRectangleSelection,
    canonical_fingerprint,
)
from image23mf.contracts.job import JobConfig, load_job_config
from image23mf.contracts.provenance import ConsentEvent, ProviderRegionalEditProvenance
from image23mf.storage import (
    ArtifactPublication,
    ArtifactRepository,
    AssetRepository,
    ContentAddressedStore,
    DraftHistoryCommand,
    DraftRepository,
    ProjectRepository,
    RegionOperation,
    RevisionPublisher,
    artifact_manifest_sha256,
    open_database,
)
from image23mf.storage.repositories import canonical_json


def config(asset_id: str, filament_id: str, *, width_mm: float = 200) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": width_mm, "height_mm": 160},
            "palette": {
                "colors": [
                    {
                        "id": "cream",
                        "name": "Bone White",
                        "hex": "#CBC6B8",
                        "filament_id": filament_id,
                    },
                    {"id": "black", "name": "Charcoal", "hex": "#000000"},
                ]
            },
        }
    )


def populated_workspace(tmp_path, name: str = "source"):
    workspace = tmp_path / name
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    connection.execute(
        """
        INSERT INTO filaments(
            id, manufacturer, family, name, hex_color, material, finish, owned, metadata_json
        ) VALUES ('filament_bone', 'BambuLab', 'Matte', 'Bone White', '#CBC6B8',
                  'PLA', 'matte', 1, '{"td":1.6}')
        """
    )
    connection.execute(
        """
        INSERT INTO presets(id, scope, name, schema_version, settings_json, built_in)
        VALUES ('preset_detail', 'cleanup', 'Mural detail', 1,
                '{"min_island_mm2":0.2}', 0)
        """
    )
    connection.commit()
    project = ProjectRepository(connection).create(
        "The Wager", description="Portable mural", preferences={"units": "mm"}
    )
    source_blob = store.put_bytes(
        b"source-image", namespace="assets", extension=".png", media_type="image/png"
    )
    source = AssetRepository(connection, store).register(
        source_blob,
        original_filename="/Users/example/private/wager.png",
        width_px=1200,
        height_px=800,
        metadata={"icc": "sRGB"},
    )
    preview_blob = store.put_bytes(
        b"processed-preview",
        namespace="artifacts",
        extension=".png",
        media_type="image/png",
    )
    labels_blob = store.put_bytes(
        b"label-field",
        namespace="artifacts",
        extension=".bin",
        media_type="application/octet-stream",
    )
    published = RevisionPublisher(connection, store).publish(
        project_id=project.id,
        source_asset_id=source.id,
        config=config(source.id, "filament_bone"),
        engine_version=__version__,
        artifacts=(
            ArtifactPublication(
                kind="processed-preview",
                derivation_key="preview:root",
                blob=preview_blob,
            ),
            ArtifactPublication(
                kind="label-field",
                derivation_key="labels:root",
                blob=labels_blob,
            ),
        ),
        operations=(
            RegionOperation(
                operation_type="merge-small-island",
                selection={"region_id": "cream:2"},
                parameters={"target": "cream"},
            ),
        ),
        label="Printable root",
    )
    DraftRepository(connection).save(
        project_id=project.id,
        base_revision_id=published.revision.id,
        config=config(source.id, "filament_bone", width_mm=220),
        operations=(
            RegionOperation(
                operation_type="fill-hole",
                selection={"region_id": "black:9"},
                parameters={"target": "cream"},
            ),
        ),
        expected_generation=0,
    )
    return connection, store, project, source, published


def add_evidenced_revision(connection, store, project, source, status: str):
    revision_id = f"revision_{status}_{uuid.uuid4().hex}"
    revision_config = config(source.id, "filament_bone")
    connection.execute(
        """
        INSERT INTO revisions(
            id, project_id, source_asset_id, schema_version, engine_version,
            config_json, config_sha256, label
        ) VALUES (?, ?, ?, 1, 'bundle-evidence', ?, ?, ?)
        """,
        (
            revision_id,
            project.id,
            source.id,
            revision_config.canonical_json(),
            revision_config.fingerprint(),
            status,
        ),
    )
    artifacts = ()
    if status == "fresh":
        blob = store.put_bytes(
            b"fresh-bundle-artifact",
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        source_artifact_id = f"source_artifact_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO artifacts(
                id, revision_id, kind, sha256, derivation_key, media_type,
                relative_path, byte_size, metadata_json
            ) VALUES (?, ?, 'preview-image', ?, 'bundle:fresh', ?, ?, ?, ?)
            """,
            (
                f"artifact_{uuid.uuid4().hex}",
                revision_id,
                blob.sha256,
                blob.media_type,
                blob.relative_path,
                blob.byte_size,
                canonical_json(
                    {
                        "publication_provenance": {
                            "source_artifact_id": source_artifact_id,
                            "source_preview_job_id": "job_bundle_fresh",
                        }
                    }
                ),
            ),
        )
        artifacts = ArtifactRepository(connection).list_for_revision(revision_id)
    editor_sha = EditorCommandSequence(
        config_fingerprint=revision_config.fingerprint(), commands=()
    ).fingerprint()
    preview_job_id = (
        "job_bundle_fresh"
        if status == "fresh"
        else "job_bundle_stale"
        if status == "stale"
        else None
    )
    preview_derivation_key = (
        "bundle:fresh" if status == "fresh" else "bundle:stale" if status == "stale" else None
    )
    connection.execute(
        """
        INSERT INTO revision_publications(
            revision_id, source_draft_generation, editor_sequence_sha256,
            preview_status, preview_job_id, preview_derivation_key,
            preview_reason, artifact_manifest_sha256, artifact_count
        ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            editor_sha,
            status,
            preview_job_id,
            preview_derivation_key,
            f"{status} evidence",
            artifact_manifest_sha256(artifacts) if status == "fresh" else None,
            len(artifacts) if status == "fresh" else 0,
        ),
    )
    connection.execute("INSERT INTO revision_history_seals(revision_id) VALUES (?)", (revision_id,))
    connection.execute(
        "UPDATE projects SET active_revision_id = ? WHERE id = ?",
        (revision_id, project.id),
    )
    connection.commit()
    return revision_id


def rewrite_zip(source: Path, destination: Path, transform) -> None:
    with (
        zipfile.ZipFile(source, "r") as original,
        zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as rewritten,
    ):
        for info in original.infolist():
            replacement = transform(info.filename, original.read(info))
            if replacement is not None:
                rewritten.writestr(info.filename, replacement)


def add_branching_history(connection, project_id: str, asset_id: str) -> None:
    drafts = DraftRepository(connection)
    current = drafts.get(project_id)
    assert current is not None and current.history is not None

    def save_width(width_mm: float, label: str):
        nonlocal current
        assert current is not None and current.history is not None
        current = drafts.save(
            project_id=project_id,
            base_revision_id=current.base_revision_id,
            config=config(asset_id, "filament_bone", width_mm=width_mm),
            operations=current.operations,
            expected_generation=current.generation,
            history_command=DraftHistoryCommand(
                request_id=f"request-{label}",
                schema_version=1,
                command_type="config_change",
                label=label,
                before_state_sha256=current.history.state_sha256,
                expected_cursor_node_id=current.history.cursor_node_id,
            ),
        )

    def move(direction: str, request_id: str):
        nonlocal current
        assert current is not None and current.history is not None
        current = drafts.move_history(
            project_id=project_id,
            direction=direction,
            request_id=request_id,
            expected_generation=current.generation,
            expected_cursor_node_id=current.history.cursor_node_id,
            validate_candidate=lambda *_args: None,
        )

    save_width(230, "Width 230")
    save_width(240, "Abandoned width 240")
    move("undo", "undo-before-branch")
    save_width(250, "Active branch width 250")
    move("undo", "undo-active-branch")
    assert current.history is not None
    assert current.history.can_undo and current.history.can_redo


def test_project_bundle_round_trip_restores_every_payload(tmp_path) -> None:
    connection, store, project, source, published = populated_workspace(tmp_path)
    bundle = tmp_path / "the-wager.image23mf"
    second_bundle = tmp_path / "the-wager-copy.image23mf"
    try:
        service = ProjectBundleService(connection, store)
        first = service.export(project.id, bundle)
        second = service.export(project.id, second_bundle)

        assert first.sha256 == second.sha256
        assert bundle.read_bytes() == second_bundle.read_bytes()
        assert service.verify(bundle)[0] == first.sha256
        assert first.manifest.project.id == project.id
        assert first.manifest.assets[0].original_filename == "wager.png"
        assert len(first.manifest.artifacts) == 2
    finally:
        connection.close()

    destination = tmp_path / "destination"
    restored_connection = open_database(destination / "image23mf.sqlite3")
    restored_store = ContentAddressedStore(destination)
    try:
        result = ProjectBundleService(restored_connection, restored_store).restore(bundle)

        assert not result.duplicate
        assert result.project_id == project.id
        assert result.imported_assets == 1
        assert result.imported_revisions == 1
        assert result.imported_artifacts == 2
        restored_project = ProjectRepository(restored_connection).get(result.project_id)
        assert restored_project.active_revision_id == published.revision.id
        revision = restored_connection.execute(
            "SELECT * FROM revisions WHERE id = ?", (published.revision.id,)
        ).fetchone()
        restored_config = load_job_config(json.loads(revision["config_json"]))
        assert restored_config.source_asset_id == source.id
        assert restored_config.palette.colors[0].filament_id == "filament_bone"
        draft = DraftRepository(restored_connection).get(result.project_id)
        assert draft is not None
        assert draft.base_revision_id == published.revision.id
        assert result.history_mode == "full_history"
        assert result.history_import == "full_history"
        assert result.imported_history_nodes == 1
        assert draft.history is not None
        assert draft.history.total == 0
        assert not draft.history.can_undo
        assert not draft.history.can_redo
        assert load_job_config(dict(draft.config)).canvas.width_mm == 220
        assert (
            restored_connection.execute("SELECT count(*) FROM region_operations").fetchone()[0] == 1
        )
        assert restored_connection.execute("SELECT count(*) FROM presets").fetchone()[0] == 1
        assert restored_connection.execute("SELECT count(*) FROM filaments").fetchone()[0] == 1
        for artifact in restored_connection.execute("SELECT relative_path FROM artifacts"):
            assert restored_store.path_for(artifact["relative_path"]).is_file()
    finally:
        restored_connection.close()


def test_project_bundle_round_trip_preserves_complete_regional_edit_provenance(tmp_path) -> None:
    connection, store, project, source, published = populated_workspace(tmp_path)
    selection = CanvasSelectionState(
        source_width_px=1200,
        source_height_px=800,
        primitives=(
            SourceRectangleSelection(
                primitive_id="selection_bundle_provenance",
                x=120,
                y=80,
                width=240,
                height=160,
            ),
        ),
    )
    provenance = ProviderRegionalEditProvenance(
        selection_sha256=canonical_fingerprint(selection),
        parent_revision_id=published.revision.id,
        prompt="Make the selected eyes larger while preserving the stencil linework.",
        provider_id="example-provider",
        model_id="image-editor",
        model_version="2026-07-01",
        seed=8317,
        options={"temperature": 0, "output_format": "png"},
        reference_sha256=("a" * 64,),
        output_sha256="b" * 64,
        consent_event=ConsentEvent(
            event_id="consent_bundle_provenance",
            recorded_at="2026-07-17T08:00:00Z",
            disclosure="Selected pixels, prompt, and hashed references may leave this device.",
            policy_version="regional-edit-v1",
        ),
        reproducibility="exact_seeded",
        reproducibility_reason="Provider supports seeded replay for this pinned model version.",
    )
    operation = RegionOperation(
        operation_type="regional-model-edit",
        selection={"selection": selection.model_dump(mode="json")},
        parameters={"output_asset_sha256": "b" * 64},
        source="model",
        provenance={"regional_edit": provenance.model_dump(mode="json")},
    )
    bundle = tmp_path / "provenance.image23mf"
    try:
        child = RevisionPublisher(connection, store).publish(
            project_id=project.id,
            source_asset_id=source.id,
            config=config(source.id, "filament_bone"),
            engine_version=__version__,
            operations=(operation,),
            parent_revision_id=published.revision.id,
            label="Prompted regional edit",
        )
        current = DraftRepository(connection).get(project.id)
        assert current is not None
        DraftRepository(connection).save(
            project_id=project.id,
            base_revision_id=current.base_revision_id,
            config=config(source.id, "filament_bone", width_mm=220),
            operations=(operation,),
            expected_generation=current.generation,
        )
        exported = ProjectBundleService(connection, store).export(project.id, bundle)
        bundled_operation = next(
            item
            for item in exported.manifest.operations
            if item.operation_type == "regional-model-edit"
        )
        regional = bundled_operation.provenance["regional_edit"]
        assert regional == provenance.model_dump(mode="json")
        assert exported.manifest.draft is not None
        assert exported.manifest.draft.operations[0]["provenance"]["regional_edit"] == regional
    finally:
        connection.close()

    destination = tmp_path / "provenance-destination"
    restored = open_database(destination / "image23mf.sqlite3")
    try:
        result = ProjectBundleService(restored, ContentAddressedStore(destination)).restore(bundle)
        restored_draft = DraftRepository(restored).get(result.project_id)
        assert restored_draft is not None
        assert restored_draft.operations[0].provenance["regional_edit"] == provenance.model_dump(
            mode="json"
        )
        stored = restored.execute(
            "SELECT provenance_json FROM region_operations WHERE revision_id = ?",
            (child.revision.id,),
        ).fetchone()
        assert json.loads(stored["provenance_json"])["regional_edit"] == provenance.model_dump(
            mode="json"
        )
    finally:
        restored.close()


def test_project_bundle_rejects_sensitive_tokens_in_operation_history(tmp_path) -> None:
    connection, store, project, _, _ = populated_workspace(tmp_path)
    valid = tmp_path / "valid-provenance.image23mf"
    unsafe = tmp_path / "unsafe-provenance.image23mf"
    try:
        service = ProjectBundleService(connection, store)
        service.export(project.id, valid)

        def inject_secret(name: str, payload: bytes):
            if name != "manifest.json":
                return payload
            manifest = json.loads(payload)
            manifest["draft"]["operations"][0]["provenance"]["api_key"] = (
                "should-never-enter-a-bundle"
            )
            return canonical_json(manifest).encode("utf-8")

        rewrite_zip(valid, unsafe, inject_secret)

        with pytest.raises(BundleIntegrityError, match="manifest is invalid"):
            service.verify(unsafe)
    finally:
        connection.close()


def test_duplicate_restore_is_idempotent_and_source_workspace_collision_creates_one_copy(
    tmp_path,
) -> None:
    connection, store, project, _, _ = populated_workspace(tmp_path)
    bundle = tmp_path / "duplicate.image23mf"
    try:
        service = ProjectBundleService(connection, store)
        service.export(project.id, bundle)

        first = service.restore(bundle)
        project_count = connection.execute("SELECT count(*) FROM projects").fetchone()[0]
        revision_count = connection.execute("SELECT count(*) FROM revisions").fetchone()[0]
        second = service.restore(bundle)

        assert first.project_id != project.id
        assert not first.duplicate
        assert second.duplicate
        assert second.project_id == first.project_id
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == project_count
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == revision_count
        assert ProjectRepository(connection).get(first.project_id).name.endswith("(Imported)")
    finally:
        connection.close()


def test_full_history_bundle_preserves_undo_redo_and_abandoned_branch(tmp_path) -> None:
    connection, store, project, source, _ = populated_workspace(tmp_path)
    add_branching_history(connection, project.id, source.id)
    bundle = tmp_path / "full-history.image23mf"
    try:
        exported = ProjectBundleService(connection, store).export(project.id, bundle)
        assert exported.manifest.history_mode == "full_history"
        assert len(exported.manifest.history_nodes) == 4
        assert any(node.label == "Abandoned width 240" for node in exported.manifest.history_nodes)
        assert exported.manifest.history_head is not None
    finally:
        connection.close()

    destination = tmp_path / "history-destination"
    restored = open_database(destination / "image23mf.sqlite3")
    try:
        result = ProjectBundleService(restored, ContentAddressedStore(destination)).restore(bundle)
        assert result.history_import == "full_history"
        assert result.imported_history_nodes == 4
        assert result.abandoned_history_nodes == 1
        assert restored.execute("SELECT count(*) FROM draft_history_receipts").fetchone()[0] == 0
        assert (
            restored.execute(
                "SELECT count(*) FROM draft_history_nodes WHERE request_id IS NOT NULL"
            ).fetchone()[0]
            == 3
        )

        drafts = DraftRepository(restored)
        current = drafts.get(result.project_id)
        assert current is not None and current.history is not None
        assert load_job_config(dict(current.config)).canvas.width_mm == 230
        assert current.history.can_undo and current.history.can_redo

        current = drafts.move_history(
            project_id=result.project_id,
            direction="redo",
            request_id="restored-redo",
            expected_generation=current.generation,
            expected_cursor_node_id=current.history.cursor_node_id,
            validate_candidate=lambda *_args: None,
        )
        assert load_job_config(dict(current.config)).canvas.width_mm == 250
        assert current.history is not None and not current.history.can_redo
        current = drafts.move_history(
            project_id=result.project_id,
            direction="undo",
            request_id="restored-undo",
            expected_generation=current.generation,
            expected_cursor_node_id=current.history.cursor_node_id,
            validate_candidate=lambda *_args: None,
        )
        assert load_job_config(dict(current.config)).canvas.width_mm == 230
    finally:
        restored.close()


def test_full_history_collision_remaps_lineages_nodes_and_state_hashes(tmp_path) -> None:
    connection, store, project, source, _ = populated_workspace(tmp_path)
    add_branching_history(connection, project.id, source.id)
    bundle = tmp_path / "history-collision.image23mf"
    try:
        service = ProjectBundleService(connection, store)
        exported = service.export(project.id, bundle)
        source_lineages = {item.id for item in exported.manifest.history_lineages}
        source_nodes = {item.id for item in exported.manifest.history_nodes}
        source_states = {item.sha256 for item in exported.manifest.history_states}

        result = service.restore(bundle)
        imported_lineages = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM draft_history_lineages WHERE project_id = ?", (result.project_id,)
            )
        }
        imported_nodes = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM draft_history_nodes WHERE project_id = ?", (result.project_id,)
            )
        }
        imported_states = {
            row[0]
            for row in connection.execute(
                """
                SELECT state.sha256 FROM draft_history_states state
                JOIN draft_history_lineages lineage ON lineage.id = state.lineage_id
                WHERE lineage.project_id = ?
                """,
                (result.project_id,),
            )
        }
        assert source_lineages.isdisjoint(imported_lineages)
        assert source_nodes.isdisjoint(imported_nodes)
        assert source_states.isdisjoint(imported_states)
        assert result.imported_history_nodes == len(source_nodes)

        imported = DraftRepository(connection).get(result.project_id)
        assert imported is not None and imported.history is not None and imported.history.can_redo
        redone = DraftRepository(connection).move_history(
            project_id=result.project_id,
            direction="redo",
            request_id="collision-redo",
            expected_generation=imported.generation,
            expected_cursor_node_id=imported.history.cursor_node_id,
            validate_candidate=lambda *_args: None,
        )
        assert load_job_config(dict(redone.config)).canvas.width_mm == 250
    finally:
        connection.close()


@pytest.mark.parametrize("damage", ["state", "cycle"])
def test_full_history_manifest_rejects_tampered_state_and_cycle(tmp_path, damage: str) -> None:
    connection, store, project, source, _ = populated_workspace(tmp_path)
    add_branching_history(connection, project.id, source.id)
    valid = tmp_path / "history-valid.image23mf"
    broken = tmp_path / f"history-{damage}.image23mf"
    try:
        ProjectBundleService(connection, store).export(project.id, valid)

        def corrupt(name: str, payload: bytes):
            if name != "manifest.json":
                return payload
            manifest = json.loads(payload)
            if damage == "state":
                manifest["history_states"][0]["config"]["canvas"]["width_mm"] += 1
            else:
                node = manifest["history_nodes"][-1]
                node["parent_node_id"] = node["id"]
            return canonical_json(manifest).encode()

        rewrite_zip(valid, broken, corrupt)
        with pytest.raises(BundleIntegrityError, match="manifest is invalid"):
            ProjectBundleService(connection, store).verify(broken)
    finally:
        connection.close()


def test_current_state_only_export_is_explicit_and_restores_visible_legacy_anchor(tmp_path) -> None:
    connection, store, project, source, _ = populated_workspace(tmp_path)
    add_branching_history(connection, project.id, source.id)
    bundle = tmp_path / "current-only.image23mf"
    try:
        exported = ProjectBundleService(connection, store).export(
            project.id, bundle, history_mode="current_state_only"
        )
        assert exported.manifest.history_mode == "current_state_only"
        assert exported.manifest.history_nodes == ()
        assert exported.manifest.history_head is None
    finally:
        connection.close()

    destination = tmp_path / "current-only-destination"
    restored = open_database(destination / "image23mf.sqlite3")
    try:
        result = ProjectBundleService(restored, ContentAddressedStore(destination)).restore(bundle)
        assert result.history_mode == "current_state_only"
        assert result.history_import == "legacy_anchor"
        assert result.imported_history_nodes == 0
        draft = DraftRepository(restored).get(result.project_id)
        assert draft is not None and draft.history is not None
        assert draft.history.total == 0
        assert not draft.history.can_undo and not draft.history.can_redo
    finally:
        restored.close()


def test_selected_artifacts_only_are_included(tmp_path) -> None:
    connection, store, project, _, published = populated_workspace(tmp_path)
    bundle = tmp_path / "selected.image23mf"
    try:
        selected = published.artifacts[0]
        manifest = (
            ProjectBundleService(connection, store)
            .export(project.id, bundle, artifact_ids=(selected.id,))
            .manifest
        )

        assert [item.id for item in manifest.artifacts] == [selected.id]
        assert {item.relative_path for item in manifest.members} == {
            manifest.assets[0].relative_path,
            selected.relative_path,
        }
    finally:
        connection.close()


def test_bundle_v3_round_trips_fresh_missing_and_stale_publication_evidence(
    tmp_path,
) -> None:
    connection, store, project, source, _ = populated_workspace(tmp_path)
    revision_ids = {
        status: add_evidenced_revision(connection, store, project, source, status)
        for status in ("fresh", "missing", "stale")
    }
    bundle = tmp_path / "evidence.image23mf"
    try:
        exported = ProjectBundleService(connection, store).export(project.id, bundle)
        assert exported.manifest.schema_version == 3
        assert {item.preview_status for item in exported.manifest.revision_publications} == {
            "fresh",
            "missing",
            "stale",
        }
    finally:
        connection.close()

    destination = tmp_path / "evidence-destination"
    restored = open_database(destination / "image23mf.sqlite3")
    try:
        result = ProjectBundleService(restored, ContentAddressedStore(destination)).restore(bundle)
        rows = restored.execute(
            """
            SELECT revision_id, preview_status, artifact_count, artifact_manifest_sha256
            FROM revision_publications
            WHERE revision_id IN (?, ?, ?)
            """,
            tuple(revision_ids.values()),
        ).fetchall()
        by_id = {row["revision_id"]: row for row in rows}
        assert by_id[revision_ids["fresh"]]["preview_status"] == "fresh"
        assert by_id[revision_ids["fresh"]]["artifact_count"] == 1
        assert by_id[revision_ids["fresh"]]["artifact_manifest_sha256"] is not None
        assert by_id[revision_ids["missing"]]["preview_status"] == "missing"
        assert by_id[revision_ids["stale"]]["preview_status"] == "stale"
        assert result.project_id == project.id
    finally:
        restored.close()


def test_collision_restore_downgrades_fresh_evidence_when_derivation_identity_changes(
    tmp_path,
) -> None:
    connection, store, project, source, _ = populated_workspace(tmp_path)
    fresh_revision_id = add_evidenced_revision(connection, store, project, source, "fresh")
    bundle = tmp_path / "collision-evidence.image23mf"
    try:
        service = ProjectBundleService(connection, store)
        service.export(project.id, bundle)
        result = service.restore(bundle)
        imported = connection.execute(
            """
            SELECT publication.preview_status, publication.preview_reason,
                   publication.artifact_count, revision.id
            FROM revisions revision
            JOIN revision_publications publication ON publication.revision_id = revision.id
            WHERE revision.project_id = ? AND revision.label = 'fresh'
            """,
            (result.project_id,),
        ).fetchone()
        assert imported is not None
        assert imported["id"] != fresh_revision_id
        assert imported["preview_status"] == "stale"
        assert imported["artifact_count"] == 0
        assert "downgraded" in imported["preview_reason"]
    finally:
        connection.close()


def test_selected_artifact_export_rejects_incomplete_fresh_evidence_set(tmp_path) -> None:
    connection, store, project, source, _ = populated_workspace(tmp_path)
    add_evidenced_revision(connection, store, project, source, "fresh")
    try:
        with pytest.raises(BundleError, match="complete fresh evidence set"):
            ProjectBundleService(connection, store).export(
                project.id,
                tmp_path / "incomplete.image23mf",
                artifact_ids=(),
            )
    finally:
        connection.close()


def test_schema_v1_bundle_without_publication_field_remains_readable(tmp_path) -> None:
    connection, store, project, _, _ = populated_workspace(tmp_path)
    current = tmp_path / "current.image23mf"
    legacy = tmp_path / "legacy.image23mf"
    try:
        ProjectBundleService(connection, store).export(project.id, current)

        def downgrade(name, payload):
            if name != "manifest.json":
                return payload
            manifest = json.loads(payload)
            manifest["schema_version"] = 1
            manifest.pop("revision_publications", None)
            manifest.pop("history_mode", None)
            manifest.pop("history_lineages", None)
            manifest.pop("history_states", None)
            manifest.pop("history_nodes", None)
            manifest.pop("history_head", None)
            return canonical_json(manifest).encode()

        rewrite_zip(current, legacy, downgrade)
        _, manifest = ProjectBundleService(connection, store).verify(legacy)
        assert manifest.schema_version == 1
        assert manifest.revision_publications == ()
    finally:
        connection.close()

    destination = tmp_path / "legacy-destination"
    restored = open_database(destination / "image23mf.sqlite3")
    try:
        result = ProjectBundleService(restored, ContentAddressedStore(destination)).restore(legacy)
        assert not result.duplicate
        assert result.source_project_id == project.id
        assert (
            restored.execute(
                "SELECT count(*) FROM revisions WHERE project_id = ?", (result.project_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            restored.execute(
                """
                SELECT count(*)
                FROM revision_publications publication
                JOIN revisions revision ON revision.id = publication.revision_id
                WHERE revision.project_id = ?
                """,
                (result.project_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        restored.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_member_is_rejected_before_restore(tmp_path, damage: str) -> None:
    connection, store, project, _, _ = populated_workspace(tmp_path)
    source_bundle = tmp_path / "valid.image23mf"
    broken_bundle = tmp_path / f"{damage}.image23mf"
    try:
        exported = ProjectBundleService(connection, store).export(project.id, source_bundle)
        target = exported.manifest.members[0].archive_path

        def damage_member(name: str, payload: bytes):
            if name != target:
                return payload
            if damage == "missing":
                return None
            return bytes([payload[0] ^ 0xFF]) + payload[1:]

        rewrite_zip(source_bundle, broken_bundle, damage_member)
    finally:
        connection.close()

    destination = tmp_path / "broken-destination"
    restored_connection = open_database(destination / "image23mf.sqlite3")
    try:
        with pytest.raises(BundleIntegrityError, match="member|checksum"):
            ProjectBundleService(restored_connection, ContentAddressedStore(destination)).restore(
                broken_bundle
            )
        assert restored_connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
        assert restored_connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 0
    finally:
        restored_connection.close()


def test_traversal_and_unlisted_members_are_rejected_without_extraction(tmp_path) -> None:
    connection, store, project, _, _ = populated_workspace(tmp_path)
    valid = tmp_path / "valid.image23mf"
    malicious = tmp_path / "traversal.image23mf"
    try:
        ProjectBundleService(connection, store).export(project.id, valid)
    finally:
        connection.close()
    with (
        zipfile.ZipFile(valid, "r") as source,
        zipfile.ZipFile(malicious, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            target.writestr(info.filename, source.read(info))
        target.writestr("../escaped.txt", b"never extract me")

    destination = tmp_path / "safe-destination"
    restored_connection = open_database(destination / "image23mf.sqlite3")
    try:
        with pytest.raises(BundleIntegrityError, match="unsafe archive member"):
            ProjectBundleService(restored_connection, ContentAddressedStore(destination)).restore(
                malicious
            )
        assert not (tmp_path / "escaped.txt").exists()
    finally:
        restored_connection.close()


def test_manifest_and_member_paths_never_contain_workspace_absolute_paths(tmp_path) -> None:
    connection, store, project, _, _ = populated_workspace(tmp_path)
    bundle = tmp_path / "portable.image23mf"
    try:
        ProjectBundleService(connection, store).export(project.id, bundle)
    finally:
        connection.close()

    with zipfile.ZipFile(bundle, "r") as archive:
        manifest_bytes = archive.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        assert str((tmp_path / "source").resolve()).encode() not in manifest_bytes
        assert all(not Path(item["relative_path"]).is_absolute() for item in manifest["members"])
        assert all(not Path(item["archive_path"]).is_absolute() for item in manifest["members"])
        assert manifest["assets"][0]["original_filename"] == "wager.png"


def test_future_manifest_schema_and_corrupt_workspace_source_are_rejected(tmp_path) -> None:
    connection, store, project, _, _ = populated_workspace(tmp_path)
    valid = tmp_path / "valid.image23mf"
    future = tmp_path / "future.image23mf"
    try:
        exported = ProjectBundleService(connection, store).export(project.id, valid)

        def future_manifest(name: str, payload: bytes):
            if name == "manifest.json":
                value = json.loads(payload)
                value["schema_version"] = 99
                return json.dumps(value).encode()
            return payload

        rewrite_zip(valid, future, future_manifest)
        with pytest.raises(UnsupportedBundleVersionError, match="newer than supported"):
            ProjectBundleService(connection, store).verify(future)

        store.path_for(exported.manifest.members[0].relative_path).write_bytes(b"corrupt")
        with pytest.raises(BundleIntegrityError, match="workspace blob is corrupt"):
            ProjectBundleService(connection, store).export(
                project.id, tmp_path / "must-not-export.image23mf"
            )
    finally:
        connection.close()
