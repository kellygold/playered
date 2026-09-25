import os
from pathlib import Path

from image23mf import __version__
from image23mf.contracts.job import JobConfig
from image23mf.storage import (
    ArtifactPublication,
    AssetRepository,
    ContentAddressedStore,
    ProjectRepository,
    RevisionPublisher,
    open_database,
)
from image23mf.workspace_health import (
    Severity,
    WorkspaceHealthService,
    WorkspaceStatus,
)


def config(asset_id: str) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": 200, "height_mm": 160},
            "palette": {
                "colors": [
                    {"id": "cream", "name": "Cream", "hex": "#CBC6B8"},
                    {"id": "black", "name": "Black", "hex": "#000000"},
                ]
            },
        }
    )


def populated_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    project = ProjectRepository(connection).create("Health proof")
    source_blob = store.put_bytes(
        b"source", namespace="assets", extension=".png", media_type="image/png"
    )
    source = AssetRepository(connection, store).register(
        source_blob, original_filename="source.png", width_px=40, height_px=30
    )
    artifact_blob = store.put_bytes(
        b"preview", namespace="artifacts", extension=".png", media_type="image/png"
    )
    published = RevisionPublisher(connection, store).publish(
        project_id=project.id,
        source_asset_id=source.id,
        config=config(source.id),
        engine_version=__version__,
        artifacts=(
            ArtifactPublication(
                kind="preview",
                derivation_key="health:preview",
                blob=artifact_blob,
            ),
        ),
    )
    return workspace, connection, store, project, source, published


def old(path: Path) -> None:
    os.utime(path, (1, 1))


def test_health_reports_consistency_disk_cache_orphans_and_migration_state(tmp_path) -> None:
    workspace, connection, store, _, _, _ = populated_workspace(tmp_path)
    orphan = store.put_bytes(
        b"orphan", namespace="artifacts", extension=".bin", media_type="application/octet-stream"
    )
    cache = store.put_bytes(
        b"cache", namespace="cache", extension=".bin", media_type="application/octet-stream"
    )
    temp = workspace / "temp" / "abandoned.tmp"
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_bytes(b"temporary")
    old(temp)
    try:
        report = WorkspaceHealthService(connection, store).inspect(stale_temp_age_seconds=0)

        assert report.status == WorkspaceStatus.HEALTHY
        assert report.database_integrity
        assert report.foreign_key_integrity
        assert report.migration.current
        assert report.migration.database_version == report.migration.application_version
        assert report.referenced_file_count == 2
        assert report.orphan_file_count == 1
        assert report.cache_file_count == 1
        assert report.stale_temp_file_count == 1
        assert report.disk_total_bytes >= report.disk_free_bytes > 0
        buckets = {item.name: item for item in report.buckets}
        assert buckets["assets"].file_count == 1
        assert buckets["artifacts"].file_count == 2
        assert buckets["cache"].byte_size == len(b"cache")
        orphan_issue = next(item for item in report.issues if item.code == "orphan_blob")
        assert orphan_issue.relative_path == orphan.relative_path
        assert "dry run" in orphan_issue.advice
        assert store.path_for(cache.relative_path).is_file()
    finally:
        connection.close()


def test_missing_artifact_and_corrupt_source_have_actionable_severity(tmp_path) -> None:
    _, connection, store, _, source, published = populated_workspace(tmp_path)
    artifact = published.artifacts[0]
    store.path_for(artifact.relative_path).unlink()
    store.path_for(source.relative_path).write_bytes(b"corrupt")
    try:
        report = WorkspaceHealthService(connection, store).inspect()

        assert report.status == WorkspaceStatus.CRITICAL
        missing = next(
            item
            for item in report.issues
            if item.code == "missing_referenced_blob" and item.record_type == "artifact"
        )
        corrupt = next(item for item in report.issues if item.code == "corrupt_referenced_blob")
        assert missing.severity == Severity.WARNING
        assert "Re-run" in missing.advice
        assert corrupt.severity == Severity.ERROR
        assert corrupt.record_id == source.id
        assert "restore" in corrupt.advice.lower()
    finally:
        connection.close()


def test_health_reports_tool_cache_and_low_disk_evidence(tmp_path, monkeypatch) -> None:
    workspace, connection, store, _, _, _ = populated_workspace(tmp_path)
    store.put_bytes(
        b"cache", namespace="cache", extension=".bin", media_type="application/octet-stream"
    )
    monkeypatch.setattr(
        "image23mf.workspace_health.shutil.disk_usage",
        lambda _path: type("Disk", (), {"total": 1000, "used": 960, "free": 40})(),
    )
    try:
        report = WorkspaceHealthService(connection, store).inspect(
            tools=(
                {
                    "id": "bambu-studio",
                    "name": "Bambu Studio",
                    "available": False,
                    "compatible": False,
                    "version": None,
                    "path": None,
                    "purpose": "Validate sliced 3MF exports",
                    "unavailable_reason": "Executable was not found.",
                },
            )
        )

        assert report.status == WorkspaceStatus.CRITICAL
        assert report.disk_free_ratio == 0.04
        assert report.cache.file_count == 1
        assert report.cache.disposable
        assert report.tools[0].id == "bambu-studio"
        assert not report.tools[0].available
        assert report.tools[0].advice == "Executable was not found."
        codes = {item.code for item in report.issues}
        assert {"disk_space_critical", "tool_unavailable"} <= codes
        assert (workspace / "cache").is_dir()
    finally:
        connection.close()


def test_gc_is_dry_run_by_default_then_deletes_only_reviewed_unreferenced_files(tmp_path) -> None:
    workspace, connection, store, _, source, published = populated_workspace(tmp_path)
    orphan = store.put_bytes(
        b"orphan", namespace="artifacts", extension=".bin", media_type="application/octet-stream"
    )
    cache = store.put_bytes(
        b"cache", namespace="cache", extension=".bin", media_type="application/octet-stream"
    )
    temp = workspace / "temp" / "stale.tmp"
    temp.write_bytes(b"temp")
    for relative in (orphan.relative_path, cache.relative_path):
        old(store.path_for(relative))
    old(temp)
    service = WorkspaceHealthService(connection, store)
    try:
        plan = service.plan_garbage_collection(minimum_age_seconds=0)
        referenced_paths = {source.relative_path, published.artifacts[0].relative_path}

        assert plan.candidate_count == 3
        assert referenced_paths.isdisjoint(item.relative_path for item in plan.candidates)
        dry_run = service.execute_garbage_collection(plan)
        assert not dry_run.applied
        assert dry_run.eligible_count == 3
        assert dry_run.deleted_count == 0
        assert all((store.root / item.relative_path).is_file() for item in plan.candidates)

        applied = service.execute_garbage_collection(plan, apply=True)
        assert applied.applied
        assert applied.deleted_count == 3
        assert applied.reclaimed_bytes == plan.reclaimable_bytes
        assert all(not (store.root / item.relative_path).exists() for item in plan.candidates)
        assert all(store.path_for(path).is_file() for path in referenced_paths)
    finally:
        connection.close()


def test_gc_rechecks_database_and_preserves_candidate_that_became_referenced(tmp_path) -> None:
    _, connection, store, _, _, _ = populated_workspace(tmp_path)
    orphan = store.put_bytes(
        b"late-reference",
        namespace="artifacts",
        extension=".bin",
        media_type="application/octet-stream",
    )
    old(store.path_for(orphan.relative_path))
    service = WorkspaceHealthService(connection, store)
    try:
        plan = service.plan_garbage_collection(minimum_age_seconds=0)
        assert any(item.relative_path == orphan.relative_path for item in plan.candidates)

        connection.execute(
            """
            INSERT INTO artifacts(
                id, revision_id, kind, sha256, derivation_key, media_type,
                relative_path, byte_size
            ) VALUES ('late_artifact', NULL, 'late', ?, 'late:reference', ?, ?, ?)
            """,
            (
                orphan.sha256,
                orphan.media_type,
                orphan.relative_path,
                orphan.byte_size,
            ),
        )
        connection.commit()

        result = service.execute_garbage_collection(plan, apply=True)
        assert result.deleted_count == 0
        assert result.skipped[0].code == "gc_candidate_became_referenced"
        assert store.path_for(orphan.relative_path).is_file()
    finally:
        connection.close()


def test_gc_preserves_file_changed_after_plan_and_requires_fresh_review(tmp_path) -> None:
    _, connection, store, _, _, _ = populated_workspace(tmp_path)
    orphan = store.put_bytes(
        b"old", namespace="artifacts", extension=".bin", media_type="application/octet-stream"
    )
    path = store.path_for(orphan.relative_path)
    old(path)
    service = WorkspaceHealthService(connection, store)
    try:
        plan = service.plan_garbage_collection(minimum_age_seconds=0)
        path.write_bytes(b"changed after review")

        result = service.execute_garbage_collection(plan, apply=True)

        assert result.deleted_count == 0
        assert result.skipped[0].code == "gc_candidate_changed"
        assert path.read_bytes() == b"changed after review"
    finally:
        connection.close()
