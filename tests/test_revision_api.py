from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from image23mf import __version__
from image23mf.api.app import create_app
from image23mf.contracts.editor import (
    EditorCommandSequence,
    KeepRegionsCommand,
    RegionSelector,
)
from image23mf.contracts.job import JobConfig, load_job_config
from image23mf.contracts.jobs import JobStage, JobType
from image23mf.editor import command_to_storage_payload
from image23mf.processing import preview_derivation_key
from image23mf.settings import Settings
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    JobRepository,
    ProjectRepository,
    RegionOperation,
    RevisionPublisher,
    StaleDraftError,
    draft_state_fingerprint,
    open_database,
)
from image23mf.storage.repositories import canonical_json


def config(asset_id: str, *, width_mm: float = 200, filament_id: str | None = None) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": width_mm, "height_mm": 200},
            "palette": {
                "colors": [
                    {
                        "id": "cream",
                        "name": "Cream",
                        "hex": "#CBC6B8",
                        "filament_id": filament_id,
                    },
                    {"id": "black", "name": "Black", "hex": "#000000"},
                ]
            },
        }
    )


def operation(name: str) -> RegionOperation:
    return RegionOperation(
        operation_type="palette-edit",
        selection={"palette_id": name},
        parameters={"hex": "#010203"},
        source="manual",
        provenance={"command_id": name},
    )


def workspace(tmp_path, *, name: str = "Revision history"):
    root = tmp_path / "workspace"
    app = create_app(Settings(workspace=root))
    image = Image.new("RGBA", (8, 8), "white")
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    image.close()
    imported = app.state.processing_service.import_project(
        payload.getvalue(), filename="source.png", project_name=name
    )
    connection = open_database(root / "image23mf.sqlite3")
    project = ProjectRepository(connection).get(imported.project.id)
    asset = AssetRepository(connection, ContentAddressedStore(root)).get(imported.source_asset.id)
    current = DraftRepository(connection).get(project.id)
    assert current is not None
    connection.close()
    app.state.processing_service.save_draft(
        project_id=project.id,
        config=config(asset.id),
        operations=(operation("first"), operation("second")),
        expected_draft_generation=current.generation,
    )
    connection = open_database(root / "image23mf.sqlite3")
    draft = DraftRepository(connection).get(project.id)
    assert draft is not None
    connection.close()
    return app, root, project, asset, draft


def exact_preview(app, root, project_id: str, draft, *, artifact_payload: bytes = b"preview"):
    service = app.state.processing_service
    draft_config = JobConfig.model_validate(draft.config)
    validated = service._validate_config(draft_config)
    lookup = open_database(root / "image23mf.sqlite3")
    try:
        source = AssetRepository(lookup, ContentAddressedStore(root)).get(
            draft_config.source_asset_id
        )
    finally:
        lookup.close()
    derivation_key = preview_derivation_key(
        draft_config,
        validated.profile_catalog_fingerprint,
        service.printability_profiles.catalog.fingerprint(),
        draft.operations,
        source_sha256=source.sha256,
    )
    connection = open_database(root / "image23mf.sqlite3")
    store = ContentAddressedStore(root)
    jobs = JobRepository(connection)
    job = jobs.create(
        project_id=project_id,
        job_type=JobType.PREVIEW,
        request_key=derivation_key,
        supersession_key=f"preview:{project_id}",
    )
    jobs.start(job.id, stage=JobStage.NORMALIZING)
    jobs.succeed(job.id)
    blob = store.put_bytes(
        artifact_payload,
        namespace="artifacts",
        extension=".png",
        media_type="image/png",
    )
    artifact_id = f"artifact_{uuid.uuid4().hex}"
    connection.execute(
        """
        INSERT INTO artifacts(
            id, revision_id, job_id, kind, sha256, derivation_key,
            media_type, relative_path, byte_size, metadata_json
        ) VALUES (?, NULL, ?, 'preview-image', ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            job.id,
            blob.sha256,
            derivation_key,
            blob.media_type,
            blob.relative_path,
            blob.byte_size,
            canonical_json({"preview_sha256": blob.sha256}),
        ),
    )
    connection.commit()
    connection.close()
    return job, artifact_id, derivation_key, blob


def test_publish_without_preview_is_atomic_and_recovers_exact_state_after_restart(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project.id}/revisions",
            json={
                "label": "  First decision  ",
                "notes": "Exact state",
                "expected_draft_generation": draft.generation,
            },
        )
    assert response.status_code == 201
    body = response.json()
    revision = body["revision"]
    assert revision["label"] == "First decision"
    assert [item["provenance"]["command_id"] for item in revision["operations"]] == [
        "first",
        "second",
    ]
    assert revision["preview_evidence"]["status"] == "missing"
    assert revision["artifacts"] == []
    assert body["draft"]["generation"] == draft.generation + 1
    assert body["draft"]["base_revision_id"] == revision["id"]
    assert body["draft"]["config"] == revision["config"]
    assert body["draft"]["operations"] == revision["operations"]
    assert body["project"]["active_revision_id"] == revision["id"]

    restarted = create_app(Settings(workspace=root))
    with TestClient(restarted) as client:
        detail = client.get(f"/api/projects/{project.id}/revisions/{revision['id']}").json()
        opened = client.get(f"/api/projects/{project.id}").json()
        listing = client.get(f"/api/projects/{project.id}/revisions").json()
    assert detail == revision
    assert opened["draft"] == body["draft"]
    assert listing["total"] == 1
    assert listing["items"][0]["is_active"] is True
    assert "config" not in listing["items"][0]
    assert "operations" not in listing["items"][0]


def test_publish_atomically_pins_legacy_draft_without_catalog_provenance(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    legacy_config = load_job_config(dict(draft.config))
    legacy_cleanup = legacy_config.cleanup.model_copy(
        update={
            "printability_profile_id": None,
            "printability_profile_catalog_fingerprint": None,
            "override_fields": (),
        }
    )
    connection = open_database(root / "image23mf.sqlite3")
    legacy = DraftRepository(connection).save(
        project_id=project.id,
        config=legacy_config.model_copy(update={"cleanup": legacy_cleanup}),
        operations=draft.operations,
        base_revision_id=draft.base_revision_id,
        expected_generation=draft.generation,
    )
    connection.close()

    response = TestClient(app).post(
        f"/api/projects/{project.id}/revisions",
        json={
            "label": "Legacy normalization",
            "expected_draft_generation": legacy.generation,
        },
    )

    assert response.status_code == 201
    body = response.json()
    fingerprint = body["revision"]["config"]["cleanup"]["printability_profile_catalog_fingerprint"]
    assert fingerprint == app.state.processing_service.printability_profiles.catalog.fingerprint()
    assert body["draft"]["config"] == body["revision"]["config"]


def test_exact_current_preview_is_verified_copied_and_carries_provenance(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    job, source_artifact_id, derivation_key, blob = exact_preview(app, root, project.id, draft)

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project.id}/revisions",
            json={
                "label": "Previewed",
                "expected_draft_generation": draft.generation,
                "preview_job_id": job.id,
            },
        )

    assert response.status_code == 201
    revision = response.json()["revision"]
    evidence = revision["preview_evidence"]
    assert evidence["status"] == "fresh"
    assert evidence["preview_job_id"] == job.id
    assert evidence["derivation_key"] == derivation_key
    assert evidence["artifact_count"] == 1
    assert len(evidence["artifact_manifest_sha256"]) == 64
    artifact = revision["artifacts"][0]
    assert artifact["sha256"] == blob.sha256
    assert artifact["job_id"] is None
    assert artifact["metadata"]["publication_provenance"] == {
        "source_preview_job_id": job.id,
        "source_artifact_id": source_artifact_id,
        "source_artifact_created_at": artifact["metadata"]["publication_provenance"][
            "source_artifact_created_at"
        ],
        "verified_sha256": blob.sha256,
    }


def test_stale_preview_guard_conflicts_and_rolls_back_every_publication_write(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    job, _, _, _ = exact_preview(app, root, project.id, draft)
    connection = open_database(root / "image23mf.sqlite3")
    connection.execute("UPDATE jobs SET request_key = ? WHERE id = ?", ("0" * 64, job.id))
    connection.commit()
    connection.close()

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project.id}/revisions",
            json={
                "label": "Must not publish",
                "expected_draft_generation": draft.generation,
                "preview_job_id": job.id,
            },
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "different draft state" in response.json()["error"]["details"]["reason"]

    connection = open_database(root / "image23mf.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM revision_publications").fetchone()[0] == 0
        persisted = DraftRepository(connection).get(project.id)
        assert persisted is not None and persisted.generation == draft.generation
        assert ProjectRepository(connection).get(project.id).active_revision_id is None
    finally:
        connection.close()


def test_guarded_preview_with_corrupt_artifact_never_enters_immutable_history(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    job, _, _, blob = exact_preview(app, root, project.id, draft)
    ContentAddressedStore(root).path_for(blob.relative_path).unlink()

    response = TestClient(app).post(
        f"/api/projects/{project.id}/revisions",
        json={
            "label": "Corrupt evidence",
            "expected_draft_generation": draft.generation,
            "preview_job_id": job.id,
        },
    )
    assert response.status_code == 409
    assert "missing or corrupt" in response.json()["error"]["details"]["reason"]
    connection = open_database(root / "image23mf.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert DraftRepository(connection).get(project.id).generation == draft.generation
    finally:
        connection.close()


def test_missing_source_blob_rolls_back_revision_active_pointer_and_continuation(tmp_path) -> None:
    app, root, project, asset, draft = workspace(tmp_path)
    ContentAddressedStore(root).path_for(asset.relative_path).unlink()

    response = TestClient(app).post(
        f"/api/projects/{project.id}/revisions",
        json={"label": "Unavailable source", "expected_draft_generation": draft.generation},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    connection = open_database(root / "image23mf.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert ProjectRepository(connection).get(project.id).active_revision_id is None
        assert DraftRepository(connection).get(project.id).generation == draft.generation
    finally:
        connection.close()


def test_unguarded_stale_preview_publishes_state_but_never_copies_stale_artifacts(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    job, _, _, _ = exact_preview(app, root, project.id, draft)
    connection = open_database(root / "image23mf.sqlite3")
    connection.execute("UPDATE jobs SET request_key = ? WHERE id = ?", ("f" * 64, job.id))
    connection.commit()
    connection.close()

    response = TestClient(app).post(
        f"/api/projects/{project.id}/revisions",
        json={"label": "State only", "expected_draft_generation": draft.generation},
    )
    assert response.status_code == 201
    revision = response.json()["revision"]
    assert revision["preview_evidence"]["status"] == "stale"
    assert revision["preview_evidence"]["preview_job_id"] == job.id
    assert revision["preview_evidence"]["artifact_count"] == 0
    assert revision["artifacts"] == []


def test_duplicate_and_concurrent_publish_of_one_generation_produces_one_revision(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def publish(label: str) -> None:
        barrier.wait()
        with TestClient(app) as client:
            statuses.append(
                client.post(
                    f"/api/projects/{project.id}/revisions",
                    json={"label": label, "expected_draft_generation": draft.generation},
                ).status_code
            )

    threads = [
        threading.Thread(target=publish, args=(f"Concurrent {index}",)) for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [201, 409]

    connection = open_database(root / "image23mf.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM revision_publications").fetchone()[0] == 1
    finally:
        connection.close()


def test_branching_old_revision_restores_exact_state_and_preserves_lineage(tmp_path) -> None:
    app, root, project, asset, draft = workspace(tmp_path)
    client = TestClient(app)
    root_result = client.post(
        f"/api/projects/{project.id}/revisions",
        json={"label": "Root", "expected_draft_generation": draft.generation},
    ).json()
    root_revision = root_result["revision"]
    continued = root_result["draft"]
    changed = client.put(
        f"/api/projects/{project.id}/draft",
        json={
            "config": config(asset.id, width_mm=240).model_dump(mode="json"),
            "operations": [
                {
                    "operation_type": "palette-edit",
                    "selection": {"palette_id": "branch-child"},
                    "parameters": {"hex": "#112233"},
                    "source": "manual",
                    "provenance": {"command_id": "branch-child"},
                }
            ],
            "expected_draft_generation": continued["generation"],
        },
    ).json()
    child_result = client.post(
        f"/api/projects/{project.id}/revisions",
        json={"label": "Child", "expected_draft_generation": changed["generation"]},
    ).json()
    child_revision = child_result["revision"]
    assert child_revision["parent_revision_id"] == root_revision["id"]

    branched = client.post(
        f"/api/projects/{project.id}/revisions/{root_revision['id']}/branch",
        json={"expected_draft_generation": child_result["draft"]["generation"]},
    )
    assert branched.status_code == 200
    body = branched.json()
    assert body["base_revision"] == root_revision
    assert body["draft"]["base_revision_id"] == root_revision["id"]
    assert body["draft"]["config"] == root_revision["config"]
    assert body["draft"]["operations"] == root_revision["operations"]
    assert body["project"]["active_revision_id"] == child_revision["id"]

    restarted = TestClient(create_app(Settings(workspace=root)))
    reopened = restarted.get(f"/api/projects/{project.id}").json()
    assert reopened["draft"] == body["draft"]
    history = restarted.get(f"/api/projects/{project.id}/revisions").json()
    assert history["total"] == 2


def test_cross_project_revision_access_and_branch_do_not_disclose_history(tmp_path) -> None:
    app, _, first, _, draft = workspace(tmp_path, name="First")
    client = TestClient(app)
    revision = client.post(
        f"/api/projects/{first.id}/revisions",
        json={"label": "Private", "expected_draft_generation": draft.generation},
    ).json()["revision"]

    root = app.state.settings.workspace
    connection = open_database(root / "image23mf.sqlite3")
    second = ProjectRepository(connection).create("Second")
    source = connection.execute("SELECT id FROM assets LIMIT 1").fetchone()["id"]
    second_draft = DraftRepository(connection).save(
        project_id=second.id, config=config(source), expected_generation=0
    )
    connection.close()

    assert client.get(f"/api/projects/{second.id}/revisions/{revision['id']}").status_code == 404
    assert (
        client.post(
            f"/api/projects/{second.id}/revisions/{revision['id']}/branch",
            json={"expected_draft_generation": second_draft.generation},
        ).status_code
        == 404
    )


def test_publication_validates_filaments_and_preserves_draft_on_failure(tmp_path) -> None:
    app, root, project, asset, draft = workspace(tmp_path)
    missing = config(asset.id, filament_id="filament_missing")
    connection = open_database(root / "image23mf.sqlite3")
    connection.execute(
        """
        UPDATE project_drafts
        SET config_json = ?, config_sha256 = ?, generation = generation + 1
        WHERE project_id = ?
        """,
        (missing.canonical_json(), missing.fingerprint(), project.id),
    )
    connection.commit()
    connection.close()

    response = TestClient(app).post(
        f"/api/projects/{project.id}/revisions",
        json={"label": "Invalid", "expected_draft_generation": draft.generation + 1},
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["filament_ids"] == ["filament_missing"]
    connection = open_database(root / "image23mf.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert DraftRepository(connection).get(project.id).generation == draft.generation + 1
    finally:
        connection.close()


def test_published_revision_operations_artifacts_and_evidence_are_immutable(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    job, _, _, _ = exact_preview(app, root, project.id, draft)
    revision = (
        TestClient(app)
        .post(
            f"/api/projects/{project.id}/revisions",
            json={
                "label": "Immutable",
                "expected_draft_generation": draft.generation,
                "preview_job_id": job.id,
            },
        )
        .json()["revision"]
    )
    connection = open_database(root / "image23mf.sqlite3")
    try:
        for statement, parameters, message in (
            (
                "UPDATE revisions SET label = 'changed' WHERE id = ?",
                (revision["id"],),
                "published revisions are immutable",
            ),
            (
                "UPDATE region_operations SET operation_type = 'changed' WHERE revision_id = ?",
                (revision["id"],),
                "published region operations are immutable",
            ),
            (
                "UPDATE revision_publications SET preview_reason = 'changed' WHERE revision_id = ?",
                (revision["id"],),
                "revision publication evidence is immutable",
            ),
            (
                "UPDATE artifacts SET kind = 'changed' WHERE revision_id = ?",
                (revision["id"],),
                "published revision artifacts are immutable",
            ),
        ):
            try:
                connection.execute(statement, parameters)
            except sqlite3.IntegrityError as error:
                assert message in str(error)
                connection.rollback()
            else:  # pragma: no cover - an immutable row must reject the mutation
                raise AssertionError(f"mutation unexpectedly succeeded: {statement}")
    finally:
        connection.close()


def test_editor_sequence_fingerprint_in_history_binds_config_and_operation_order(tmp_path) -> None:
    app, _, project, _, draft = workspace(tmp_path)
    revision = (
        TestClient(app)
        .post(
            f"/api/projects/{project.id}/revisions",
            json={"label": "Fingerprinted", "expected_draft_generation": draft.generation},
        )
        .json()["revision"]
    )
    expected = EditorCommandSequence(
        config_fingerprint=revision["config_sha256"], commands=()
    ).fingerprint()
    # Palette edits are config-level history and intentionally do not replay as raster commands.
    assert revision["editor_sequence_sha256"] == expected
    assert (
        hashlib.sha256(json.dumps(revision["operations"], sort_keys=True).encode()).hexdigest()
        != revision["editor_sequence_sha256"]
    )


def test_locked_publication_rejects_same_generation_with_different_state_fingerprint(
    tmp_path,
) -> None:
    _, root, project, _, draft = workspace(tmp_path)
    connection = open_database(root / "image23mf.sqlite3")
    try:
        with pytest.raises(StaleDraftError, match="contents changed"):
            RevisionPublisher(connection, ContentAddressedStore(root)).publish_draft_and_continue(
                project_id=project.id,
                expected_generation=draft.generation,
                expected_draft_state_sha256=draft_state_fingerprint(
                    draft.config_sha256,
                    tuple(reversed(draft.operations)),
                    base_revision_id=draft.base_revision_id,
                ),
                engine_version=__version__,
                editor_sequence_sha256="a" * 64,
                expected_preview_derivation_key="b" * 64,
                label="Must roll back",
            )
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert DraftRepository(connection).get(project.id) == draft
    finally:
        connection.close()


def test_guarded_absent_and_foreign_preview_ids_have_indistinguishable_reason(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    connection = open_database(root / "image23mf.sqlite3")
    try:
        foreign_project = ProjectRepository(connection).create("Foreign preview")
        foreign_job = JobRepository(connection).create(
            project_id=foreign_project.id,
            job_type=JobType.PREVIEW,
            request_key="a" * 64,
            supersession_key=f"preview:{foreign_project.id}",
        )
    finally:
        connection.close()
    client = TestClient(app)
    responses = [
        client.post(
            f"/api/projects/{project.id}/revisions",
            json={
                "label": "Guarded",
                "expected_draft_generation": draft.generation,
                "preview_job_id": preview_id,
            },
        )
        for preview_id in ("job_missing", foreign_job.id)
    ]
    assert [item.status_code for item in responses] == [409, 409]
    assert {item.json()["error"]["details"]["reason"] for item in responses} == {
        "The selected preview is not current for this project."
    }


def test_legacy_free_form_history_lists_and_reads_but_failed_branch_is_atomic(tmp_path) -> None:
    app, root, project, asset, draft = workspace(tmp_path)
    connection = open_database(root / "image23mf.sqlite3")
    try:
        legacy = (
            RevisionPublisher(connection, ContentAddressedStore(root))
            .publish(
                project_id=project.id,
                source_asset_id=asset.id,
                config=config(asset.id),
                engine_version="legacy-engine",
                operations=(
                    RegionOperation(
                        operation_type="legacy-free-form-brush",
                        selection={"pixels": [1, 2, 3]},
                        provenance={"legacy": True},
                    ),
                ),
                label="Pre-v8 free form",
            )
            .revision
        )
    finally:
        connection.close()
    client = TestClient(app)
    listing = client.get(f"/api/projects/{project.id}/revisions?limit=50")
    detail = client.get(f"/api/projects/{project.id}/revisions/{legacy.id}")
    assert listing.status_code == detail.status_code == 200
    summary = next(item for item in listing.json()["items"] if item["id"] == legacy.id)
    assert summary["preview_evidence"]["status"] == "legacy"
    assert detail.json()["preview_evidence"]["status"] == "legacy"
    assert summary["editor_sequence_sha256"] == detail.json()["editor_sequence_sha256"]

    before = client.get(f"/api/projects/{project.id}").json()["draft"]
    branch = client.post(
        f"/api/projects/{project.id}/revisions/{legacy.id}/branch",
        json={"expected_draft_generation": before["generation"]},
    )
    assert branch.status_code == 422
    assert "cannot be reopened" in branch.json()["error"]["message"]
    after = client.get(f"/api/projects/{project.id}").json()["draft"]
    assert after == before


def test_branch_rejects_selector_bound_to_another_config_without_mutating_draft(tmp_path) -> None:
    app, root, project, asset, _draft = workspace(tmp_path)
    revision_config = config(asset.id)
    stale_command = KeepRegionsCommand(
        command_id="cmd_branch_config_guard",
        source="manual",
        created_at="2026-07-16T03:30:00Z",
        selector=RegionSelector(
            graph_fingerprint="a" * 64,
            config_fingerprint=config(asset.id, width_mm=199).fingerprint(),
            region_ids=("region_" + "b" * 24,),
        ),
    )
    payload = command_to_storage_payload(stale_command)
    connection = open_database(root / "image23mf.sqlite3")
    try:
        revision = (
            RevisionPublisher(connection, ContentAddressedStore(root))
            .publish(
                project_id=project.id,
                source_asset_id=asset.id,
                config=revision_config,
                engine_version="selector-binding-test",
                operations=(
                    RegionOperation(
                        operation_type=payload["operation_type"],
                        selection=payload["selection"],
                        parameters=payload["parameters"],
                        source=payload["source"],
                        provenance=payload["provenance"],
                    ),
                ),
                label="Mismatched selector fixture",
            )
            .revision
        )
    finally:
        connection.close()

    client = TestClient(app)
    before = client.get(f"/api/projects/{project.id}").json()["draft"]
    branch = client.post(
        f"/api/projects/{project.id}/revisions/{revision.id}/branch",
        json={"expected_draft_generation": before["generation"]},
    )

    assert branch.status_code == 422
    assert branch.json()["error"]["code"] == "incompatible_editor_history"
    assert branch.json()["error"]["details"] == {
        "project_id": project.id,
        "reason": "selector_config_mismatch",
        "operation_count": 1,
        "conflict_count": 1,
        "action": "clear_or_restore_config",
    }
    after = client.get(f"/api/projects/{project.id}")
    assert after.status_code == 200
    assert after.json()["draft"] == before


def test_fresh_manifest_is_recomputed_when_revision_history_is_read(tmp_path) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    job, _, _, _ = exact_preview(app, root, project.id, draft)
    revision = (
        TestClient(app)
        .post(
            f"/api/projects/{project.id}/revisions",
            json={
                "label": "Manifest proof",
                "expected_draft_generation": draft.generation,
                "preview_job_id": job.id,
            },
        )
        .json()["revision"]
    )
    connection = open_database(root / "image23mf.sqlite3")
    try:
        connection.execute("DROP TRIGGER published_artifacts_are_immutable")
        connection.execute(
            "UPDATE artifacts SET metadata_json = '{}' WHERE revision_id = ?",
            (revision["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match="artifact manifest"):
        app.state.processing_service.get_revision(project_id=project.id, revision_id=revision["id"])


def test_sealed_history_rejects_delete_late_insert_and_replacement_but_project_cascades(
    tmp_path,
) -> None:
    app, root, project, _, draft = workspace(tmp_path)
    job, _, _, _ = exact_preview(app, root, project.id, draft)
    revision = (
        TestClient(app)
        .post(
            f"/api/projects/{project.id}/revisions",
            json={
                "label": "Sealed",
                "expected_draft_generation": draft.generation,
                "preview_job_id": job.id,
            },
        )
        .json()["revision"]
    )
    connection = open_database(root / "image23mf.sqlite3")
    try:
        revision_id = revision["id"]
        operation_id = connection.execute(
            "SELECT id FROM region_operations WHERE revision_id = ? LIMIT 1", (revision_id,)
        ).fetchone()["id"]
        artifact_id = connection.execute(
            "SELECT id FROM artifacts WHERE revision_id = ? LIMIT 1", (revision_id,)
        ).fetchone()["id"]
        attempts = (
            ("DELETE FROM revisions WHERE id = ?", (revision_id,)),
            ("DELETE FROM region_operations WHERE id = ?", (operation_id,)),
            ("DELETE FROM artifacts WHERE id = ?", (artifact_id,)),
            ("DELETE FROM revision_publications WHERE revision_id = ?", (revision_id,)),
            ("DELETE FROM revision_history_seals WHERE revision_id = ?", (revision_id,)),
            (
                """
                INSERT INTO region_operations(
                    id, revision_id, sequence, operation_type, selection_json,
                    parameters_json, source, provenance_json
                ) VALUES ('operation_late', ?, 99, 'late', '{}', '{}', 'manual', '{}')
                """,
                (revision_id,),
            ),
            (
                """
                INSERT INTO artifacts(
                    id, revision_id, kind, sha256, derivation_key, media_type,
                    relative_path, byte_size, metadata_json
                ) VALUES ('artifact_late', ?, 'late', ?, 'late', 'text/plain',
                    'artifacts/00/late.txt', 0, '{}')
                """,
                (revision_id, "0" * 64),
            ),
            (
                """
                INSERT INTO revision_publications(
                    revision_id, source_draft_generation, editor_sequence_sha256,
                    preview_status, preview_reason
                ) VALUES (?, 1, ?, 'missing', 'late')
                """,
                (revision_id, "0" * 64),
            ),
            (
                "INSERT OR REPLACE INTO revision_history_seals(revision_id) VALUES (?)",
                (revision_id,),
            ),
            (
                """
                INSERT OR REPLACE INTO revisions(
                    id, project_id, source_asset_id, parent_revision_id, schema_version,
                    engine_version, config_json, config_sha256, label, notes, published_at
                ) SELECT
                    id, project_id, source_asset_id, parent_revision_id, schema_version,
                    engine_version, config_json, config_sha256, label, notes, published_at
                  FROM revisions WHERE id = ?
                """,
                (revision_id,),
            ),
            (
                """
                INSERT OR REPLACE INTO region_operations(
                    id, revision_id, sequence, operation_type, selection_json,
                    parameters_json, source, provenance_json, created_at
                ) SELECT
                    id, revision_id, sequence, operation_type, selection_json,
                    parameters_json, source, provenance_json, created_at
                  FROM region_operations WHERE id = ?
                """,
                (operation_id,),
            ),
            (
                """
                INSERT OR REPLACE INTO artifacts(
                    id, revision_id, job_id, kind, sha256, derivation_key, media_type,
                    relative_path, byte_size, metadata_json, created_at
                ) SELECT
                    id, revision_id, job_id, kind, sha256, derivation_key, media_type,
                    relative_path, byte_size, metadata_json, created_at
                  FROM artifacts WHERE id = ?
                """,
                (artifact_id,),
            ),
        )
        for statement, parameters in attempts:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
            connection.rollback()

        connection.execute("DELETE FROM projects WHERE id = ?", (project.id,))
        connection.commit()
        assert (
            connection.execute(
                "SELECT count(*) FROM revisions WHERE project_id = ?", (project.id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM revision_history_seals WHERE revision_id = ?", (revision_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_revision_history_keyset_pagination_is_complete_stable_and_query_bounded(
    tmp_path, monkeypatch
) -> None:
    app, root, project, asset, _ = workspace(tmp_path)
    connection = open_database(root / "image23mf.sqlite3")
    try:
        publisher = RevisionPublisher(connection, ContentAddressedStore(root))
        for index in range(123):
            publisher.publish(
                project_id=project.id,
                source_asset_id=asset.id,
                config=config(asset.id),
                engine_version="pagination",
                label=f"Revision {index:03d}",
            )
    finally:
        connection.close()

    statements: list[str] = []
    from image23mf import processing as processing_module

    real_open_database = processing_module.open_database

    def traced_open_database(path):
        traced = real_open_database(path)
        traced.set_trace_callback(statements.append)
        return traced

    monkeypatch.setattr(processing_module, "open_database", traced_open_database)
    first = app.state.processing_service.list_revisions(project.id, limit=50)
    select_count = sum(
        statement.lstrip().upper().startswith(("SELECT", "WITH")) for statement in statements
    )
    assert select_count <= 4
    assert first.total == 123
    assert len(first.items) == 50
    assert first.next_cursor is not None

    seen = [item.id for item in first.items]
    cursor = first.next_cursor
    while cursor is not None:
        page = app.state.processing_service.list_revisions(project.id, limit=50, cursor=cursor)
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
    assert len(seen) == len(set(seen)) == 123
