from pathlib import Path

import pytest

from image23mf import __version__
from image23mf.artifact_reveal import (
    FINDER_EXECUTABLE,
    ArtifactBlobUnavailableError,
    ArtifactNotRevealableError,
    FinderOpener,
    FinderRevealFailedError,
    reveal_artifact_in_finder,
)
from image23mf.contracts.job import JobConfig
from image23mf.external.runner import ToolExecutionError, ToolRunResult
from image23mf.storage import (
    ArtifactPublication,
    AssetRepository,
    ContentAddressedStore,
    ProjectRepository,
    RecordNotFoundError,
    RevisionPublisher,
    open_database,
)


def _config(asset_id: str) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": 80, "height_mm": 80},
            "palette": {
                "colors": [
                    {"id": "light", "name": "Light", "hex": "#CBC6B8"},
                    {"id": "dark", "name": "Dark", "hex": "#000000"},
                ]
            },
        }
    )


def _seed(tmp_path: Path, *, kind: str = "bambu-3mf"):
    workspace = tmp_path / "workspace"
    store = ContentAddressedStore(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    project = ProjectRepository(connection).create("Reveal")
    foreign = ProjectRepository(connection).create("Foreign")
    source_blob = store.put_bytes(
        b"source", namespace="assets", extension=".png", media_type="image/png"
    )
    source = AssetRepository(connection, store).register(
        source_blob, original_filename="source.png", width_px=8, height_px=8
    )
    extension = ".3mf" if kind == "bambu-3mf" else ".json"
    media_type = "model/3mf" if kind == "bambu-3mf" else "application/json"
    package = store.put_bytes(
        b"retained-package",
        namespace="artifacts",
        extension=extension,
        media_type=media_type,
    )
    published = RevisionPublisher(connection, store).publish(
        project_id=project.id,
        source_asset_id=source.id,
        config=_config(source.id),
        engine_version=__version__,
        artifacts=(ArtifactPublication(kind=kind, derivation_key="reveal-proof", blob=package),),
    )
    return connection, store, project, foreign, published.artifacts[0]


class RecordingRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def run(self, executable, arguments=(), **kwargs):
        self.calls.append((executable, tuple(arguments), kwargs))
        result = ToolRunResult(
            command=(str(executable), *arguments),
            return_code=17 if self.fail else 0,
            stdout="",
            stderr="finder failed" if self.fail else "",
            duration_seconds=0.01,
        )
        if self.fail:
            raise ToolExecutionError("tool exited with status 17", result)
        return result


def test_reveal_uses_verified_server_path_and_exact_no_shell_argv(tmp_path) -> None:
    connection, store, project, _foreign, artifact = _seed(tmp_path)
    runner = RecordingRunner()
    try:
        result = reveal_artifact_in_finder(
            connection=connection,
            store=store,
            project_id=project.id,
            artifact_id=artifact.id,
            platform_name="darwin",
            opener=FinderOpener(runner),
        )
    finally:
        connection.close()

    assert result.supported is result.revealed is True
    assert result.reason is None
    assert runner.calls == [
        (
            FINDER_EXECUTABLE,
            ("-R", "--", str(store.path_for(artifact.relative_path))),
            {"timeout_seconds": 10},
        )
    ]


def test_reveal_hides_foreign_artifact_and_rejects_non_export_kind(tmp_path) -> None:
    connection, store, project, foreign, artifact = _seed(tmp_path)
    try:
        with pytest.raises(RecordNotFoundError):
            reveal_artifact_in_finder(
                connection=connection,
                store=store,
                project_id=foreign.id,
                artifact_id=artifact.id,
                platform_name="darwin",
                opener=FinderOpener(RecordingRunner()),
            )
    finally:
        connection.close()

    connection, store, project, _foreign, artifact = _seed(tmp_path / "non-export", kind="report")
    try:
        with pytest.raises(ArtifactNotRevealableError):
            reveal_artifact_in_finder(
                connection=connection,
                store=store,
                project_id=project.id,
                artifact_id=artifact.id,
                platform_name="darwin",
                opener=FinderOpener(RecordingRunner()),
            )
    finally:
        connection.close()


def test_reveal_rejects_missing_or_corrupt_blob_before_opening_finder(tmp_path) -> None:
    connection, store, project, _foreign, artifact = _seed(tmp_path)
    runner = RecordingRunner()
    store.path_for(artifact.relative_path).write_bytes(b"corrupt")
    try:
        with pytest.raises(ArtifactBlobUnavailableError):
            reveal_artifact_in_finder(
                connection=connection,
                store=store,
                project_id=project.id,
                artifact_id=artifact.id,
                platform_name="darwin",
                opener=FinderOpener(runner),
            )
    finally:
        connection.close()
    assert runner.calls == []


def test_unsupported_platform_is_typed_and_finder_failure_is_actionable(tmp_path) -> None:
    connection, store, project, _foreign, artifact = _seed(tmp_path)
    runner = RecordingRunner()
    try:
        unsupported = reveal_artifact_in_finder(
            connection=connection,
            store=store,
            project_id=project.id,
            artifact_id=artifact.id,
            platform_name="linux",
            opener=FinderOpener(runner),
        )
        assert unsupported.supported is unsupported.revealed is False
        assert unsupported.reason == "Reveal in Finder is available only on macOS."
        assert runner.calls == []

        with pytest.raises(FinderRevealFailedError):
            reveal_artifact_in_finder(
                connection=connection,
                store=store,
                project_id=project.id,
                artifact_id=artifact.id,
                platform_name="darwin",
                opener=FinderOpener(RecordingRunner(fail=True)),
            )
    finally:
        connection.close()
