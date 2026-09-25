from __future__ import annotations

import errno
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from image23mf.artifact_reveal import FinderOpener
from image23mf.external.runner import ToolRunResult
from image23mf.macos.launcher import InstanceLock
from image23mf.macos.lifecycle import MacOSPaths
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    ProjectRepository,
    open_database,
)
from image23mf.workspace_bundle_cli import main as workspace_cli
from image23mf.workspace_bundles import (
    WorkspaceActiveError,
    WorkspaceBundleError,
    WorkspaceBundleIntegrityError,
    WorkspaceBundleService,
    WorkspaceBundleVersionError,
    WorkspaceConflictError,
    WorkspacePermissionError,
    WorkspaceSwapRecoveryError,
    packaged_app_uses_workspace,
    reveal_workspace_bundle_in_finder,
    reveal_workspace_in_finder,
)


def populated_workspace(root: Path, *, project_name: str, sentinel: bytes) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    connection = open_database(root / "image23mf.sqlite3")
    store = ContentAddressedStore(root)
    try:
        ProjectRepository(connection).create(project_name)
        blob = store.put_bytes(
            sentinel,
            namespace="assets",
            extension=".png",
            media_type="image/png",
        )
        AssetRepository(connection, store).register(
            blob,
            original_filename=f"{project_name}.png",
            width_px=20,
            height_px=10,
        )
        connection.commit()
    finally:
        connection.close()
    (root / "notes" / "user.txt").parent.mkdir(parents=True)
    (root / "notes" / "user.txt").write_bytes(sentinel)
    return root


def project_names(workspace: Path) -> list[str]:
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        return [row[0] for row in connection.execute("SELECT name FROM projects ORDER BY name")]
    finally:
        connection.close()


def rewrite_zip(source: Path, destination: Path, transform) -> None:
    with (
        zipfile.ZipFile(source, "r") as original,
        zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as rewritten,
    ):
        for source_info in original.infolist():
            replacement = transform(source_info, original.read(source_info))
            if replacement is None:
                continue
            info = zipfile.ZipInfo(source_info.filename, source_info.date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            rewritten.writestr(info, replacement)


def test_workspace_backup_preflight_and_restore_into_missing_workspace(tmp_path: Path) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Source mural", sentinel=b"art")
    bundle = tmp_path / "source.image23mf-workspace"
    export = WorkspaceBundleService(source).export(bundle)
    target = tmp_path / "restored"

    preflight = WorkspaceBundleService(source).preflight(bundle, target)
    assert preflight.target_state == "missing"
    assert preflight.conflict_choices == ("empty_only",)
    assert preflight.bundle_sha256 == export.sha256
    assert preflight.project_count == 1
    assert preflight.asset_count == 1
    assert preflight.member_count >= 3

    report = WorkspaceBundleService(source).restore(bundle, target)

    assert project_names(target) == ["Source mural"]
    assert (target / "notes" / "user.txt").read_bytes() == b"art"
    assert report.original_target_state == "missing"
    assert report.rollback_bundle is None
    report_files = list((target / ".image23mf" / "restore-reports").glob("restore-*.json"))
    assert len(report_files) == 1
    persisted_report = json.loads(report_files[0].read_text())
    assert persisted_report["bundle_sha256"] == export.sha256
    assert stat.S_IMODE(report_files[0].stat().st_mode) == 0o600


def test_existing_empty_directory_restores_without_destructive_choice(tmp_path: Path) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Source", sentinel=b"source")
    bundle = WorkspaceBundleService(source).export(tmp_path / "source.bundle").path
    target = tmp_path / "empty"
    target.mkdir()

    report = WorkspaceBundleService(source).restore(bundle, target)

    assert report.original_target_state == "empty"
    assert project_names(target) == ["Source"]


def test_populated_target_defaults_to_no_change_then_explicit_replace_is_recoverable(
    tmp_path: Path,
) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Incoming", sentinel=b"new")
    target = populated_workspace(tmp_path / "target", project_name="Existing", sentinel=b"old")
    bundle = WorkspaceBundleService(source).export(tmp_path / "incoming.bundle").path
    service = WorkspaceBundleService(source)

    preflight = service.preflight(bundle, target)
    assert preflight.target_state == "populated"
    assert preflight.default_choice == "empty_only"
    assert preflight.conflict_choices == ("empty_only", "replace")
    with pytest.raises(WorkspaceConflictError, match="Nothing was changed") as blocked:
        service.restore(bundle, target)
    assert blocked.value.preflight.bundle_sha256 == preflight.bundle_sha256
    assert project_names(target) == ["Existing"]
    assert (target / "notes" / "user.txt").read_bytes() == b"old"

    report = service.restore(bundle, target, choice="replace")

    assert project_names(target) == ["Incoming"]
    assert (target / "notes" / "user.txt").read_bytes() == b"new"
    assert report.rollback_bundle is not None
    rollback = Path(report.rollback_bundle)
    assert rollback.is_file()
    recovered_old = tmp_path / "recovered-old"
    WorkspaceBundleService(target).restore(rollback, recovered_old)
    assert project_names(recovered_old) == ["Existing"]
    assert (recovered_old / "notes" / "user.txt").read_bytes() == b"old"


@pytest.mark.parametrize("damage", ["missing", "checksum", "manifest"])
def test_corrupt_archive_is_rejected_before_target_mutation(tmp_path: Path, damage: str) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Incoming", sentinel=b"new")
    valid = WorkspaceBundleService(source).export(tmp_path / "valid.bundle").path
    broken = tmp_path / f"{damage}.bundle"
    with zipfile.ZipFile(valid, "r") as archive:
        payload_name = next(name for name in archive.namelist() if name.startswith("payload/"))

    def damage_member(info: zipfile.ZipInfo, payload: bytes):
        if damage == "missing" and info.filename == payload_name:
            return None
        if damage == "checksum" and info.filename == payload_name:
            return payload + b"changed"
        if damage == "manifest" and info.filename == "manifest.json":
            return b"{not-json"
        return payload

    rewrite_zip(valid, broken, damage_member)
    target = populated_workspace(tmp_path / "target", project_name="Keep", sentinel=b"keep")

    with pytest.raises(WorkspaceBundleIntegrityError):
        WorkspaceBundleService(source).restore(broken, target, choice="replace")

    assert project_names(target) == ["Keep"]
    assert (target / "notes" / "user.txt").read_bytes() == b"keep"
    assert not list(tmp_path.glob(".target.before-restore-*.image23mf-workspace"))


def test_archive_rejects_traversal_duplicate_symlink_and_suspicious_expansion(
    tmp_path: Path,
) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Source", sentinel=b"x")
    valid = WorkspaceBundleService(source).export(tmp_path / "valid.bundle").path
    service = WorkspaceBundleService(source)

    traversal = tmp_path / "traversal.bundle"
    rewrite_zip(valid, traversal, lambda _info, payload: payload)
    with zipfile.ZipFile(traversal, "a") as archive:
        archive.writestr("../escaped", b"bad")
    with pytest.raises(WorkspaceBundleIntegrityError, match="unsafe member path"):
        service.verify(traversal)

    duplicate = tmp_path / "duplicate.bundle"
    rewrite_zip(valid, duplicate, lambda _info, payload: payload)
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(duplicate, "a") as archive,
    ):
        archive.writestr("manifest.json", b"duplicate")
    with pytest.raises(WorkspaceBundleIntegrityError, match="duplicate member"):
        service.verify(duplicate)

    symlink = tmp_path / "symlink.bundle"
    rewrite_zip(valid, symlink, lambda _info, payload: payload)
    with zipfile.ZipFile(symlink, "a") as archive:
        info = zipfile.ZipInfo("payload/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"../../outside")
    with pytest.raises(WorkspaceBundleIntegrityError, match="symlink or special"):
        service.verify(symlink)

    expansion = tmp_path / "expansion.bundle"
    rewrite_zip(valid, expansion, lambda _info, payload: payload)
    with zipfile.ZipFile(expansion, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("payload/zeros")
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        archive.writestr(info, b"\0" * (2 * 1024 * 1024))
    with pytest.raises(WorkspaceBundleIntegrityError, match="compression ratio"):
        service.verify(expansion)
    assert not (tmp_path / "escaped").exists()


def test_source_workspace_symlinks_and_special_files_are_not_followed(tmp_path: Path) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Source", sentinel=b"x")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (source / "linked.txt").symlink_to(outside)

    with pytest.raises(WorkspaceBundleIntegrityError, match="symbolic-link file"):
        WorkspaceBundleService(source).export(tmp_path / "unsafe.bundle")
    assert not (tmp_path / "unsafe.bundle").exists()


def test_future_manifest_schema_is_rejected(tmp_path: Path) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Source", sentinel=b"x")
    valid = WorkspaceBundleService(source).export(tmp_path / "valid.bundle").path
    future = tmp_path / "future.bundle"

    def bump_schema(info: zipfile.ZipInfo, payload: bytes):
        if info.filename == "manifest.json":
            manifest = json.loads(payload)
            manifest["schema_version"] = 999
            return json.dumps(manifest).encode()
        return payload

    rewrite_zip(valid, future, bump_schema)
    with pytest.raises(WorkspaceBundleVersionError, match="newer than supported"):
        WorkspaceBundleService(source).verify(future)


def test_report_failure_and_activation_failure_leave_populated_target_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Incoming", sentinel=b"new")
    target = populated_workspace(tmp_path / "target", project_name="Keep", sentinel=b"keep")
    bundle = WorkspaceBundleService(source).export(tmp_path / "valid.bundle").path
    service = WorkspaceBundleService(source)
    monkeypatch.setattr(
        service,
        "_write_restore_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("report failed")),
    )
    with pytest.raises(RuntimeError, match="report failed"):
        service.restore(bundle, target, choice="replace")
    assert project_names(target) == ["Keep"]

    def fail_stage_activation(source_path: Path, destination_path: Path) -> None:
        if ".restore-" in source_path.name and destination_path == target:
            raise OSError("injected activation failure")
        os.replace(source_path, destination_path)

    service = WorkspaceBundleService(source, replace_fn=fail_stage_activation)
    with pytest.raises(WorkspaceBundleError, match="previous workspace was put back"):
        service.restore(bundle, target, choice="replace")
    assert project_names(target) == ["Keep"]
    assert (target / "notes" / "user.txt").read_bytes() == b"keep"


def test_swap_rollback_failure_preserves_named_recovery_paths(tmp_path: Path) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Incoming", sentinel=b"new")
    target = populated_workspace(tmp_path / "target", project_name="Keep", sentinel=b"keep")
    bundle = WorkspaceBundleService(source).export(tmp_path / "valid.bundle").path
    calls = 0

    def fail_activation_and_rollback(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError("injected double failure")
        os.replace(source_path, destination_path)

    with pytest.raises(WorkspaceSwapRecoveryError) as failed:
        WorkspaceBundleService(source, replace_fn=fail_activation_and_rollback).restore(
            bundle, target, choice="replace"
        )

    assert failed.value.previous_workspace.exists()
    assert failed.value.staged_workspace.exists()
    assert failed.value.rollback_bundle is not None
    assert failed.value.rollback_bundle.exists()
    assert "Do not delete anything" in str(failed.value)


def test_permission_failure_is_actionable_and_preserves_existing_destination(
    tmp_path: Path,
) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Source", sentinel=b"x")
    destination = tmp_path / "existing.bundle"
    destination.write_bytes(b"keep")

    def denied(_source: Path, _destination: Path) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    with pytest.raises(WorkspacePermissionError) as failed:
        WorkspaceBundleService(source, replace_fn=denied).export(destination)
    assert destination.read_bytes() == b"keep"
    assert "Privacy & Security" in str(failed.value)
    assert "Nothing was intentionally overwritten" in str(failed.value)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls = []

    def run(self, executable, arguments, *, timeout_seconds):
        self.calls.append((Path(executable), tuple(arguments), timeout_seconds))
        return ToolRunResult(
            command=(str(executable), *arguments),
            return_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.001,
        )


def test_finder_reveal_verifies_bundle_and_uses_exact_no_shell_argv(tmp_path: Path) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Source", sentinel=b"x")
    bundle = WorkspaceBundleService(source).export(tmp_path / "source.bundle").path
    runner = RecordingRunner()

    result = reveal_workspace_bundle_in_finder(
        bundle,
        verifier=WorkspaceBundleService(source),
        platform_name="darwin",
        opener=FinderOpener(runner),
    )

    assert result.supported is result.revealed is True
    assert runner.calls == [(Path("/usr/bin/open"), ("-R", "--", str(bundle.resolve())), 10)]


def test_finder_reveal_validates_workspace_and_uses_exact_no_shell_argv(tmp_path: Path) -> None:
    workspace = populated_workspace(tmp_path / "workspace", project_name="Source", sentinel=b"x")
    runner = RecordingRunner()

    result = reveal_workspace_in_finder(
        workspace,
        platform_name="darwin",
        opener=FinderOpener(runner),
    )

    assert result.supported is result.revealed is True
    assert runner.calls == [(Path("/usr/bin/open"), ("-R", "--", str(workspace.resolve())), 10)]


def test_workspace_reveal_rejects_missing_database_and_symlink(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(WorkspaceBundleError, match="database is missing or unsafe"):
        reveal_workspace_in_finder(
            empty, platform_name="darwin", opener=FinderOpener(RecordingRunner())
        )

    workspace = populated_workspace(tmp_path / "workspace", project_name="Source", sentinel=b"x")
    link = tmp_path / "linked-workspace"
    link.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(WorkspaceBundleError, match="symbolic link"):
        reveal_workspace_in_finder(
            link, platform_name="darwin", opener=FinderOpener(RecordingRunner())
        )


def test_restore_refuses_workspace_owned_by_running_packaged_app(tmp_path: Path) -> None:
    source = populated_workspace(tmp_path / "source", project_name="Incoming", sentinel=b"new")
    target = populated_workspace(tmp_path / "target", project_name="Keep", sentinel=b"keep")
    bundle = WorkspaceBundleService(source).export(tmp_path / "incoming.bundle").path
    paths = MacOSPaths.for_home(tmp_path / "home")

    with InstanceLock(paths, url="http://127.0.0.1:8323", workspace=target, version="1.0"):
        assert packaged_app_uses_workspace(target, paths=paths) is True
        service = WorkspaceBundleService(
            source,
            active_workspace_check=lambda workspace: packaged_app_uses_workspace(
                workspace, paths=paths
            ),
        )
        with pytest.raises(WorkspaceActiveError, match="Quit the application completely"):
            service.restore(bundle, target, choice="replace")

    assert project_names(target) == ["Keep"]
    assert (target / "notes" / "user.txt").read_bytes() == b"keep"
    assert not list(tmp_path.glob(".target.before-restore-*.image23mf-workspace"))


def test_stale_packaged_runtime_state_does_not_block_restore_check(tmp_path: Path) -> None:
    target = tmp_path / "workspace"
    paths = MacOSPaths.for_home(tmp_path / "home")
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "server.lock").touch()
    (paths.runtime / "server.json").write_text(
        json.dumps({"workspace": str(target)}), encoding="utf-8"
    )

    assert packaged_app_uses_workspace(target, paths=paths) is False


def test_cli_backup_preflight_and_restore_emit_machine_readable_reports(
    tmp_path: Path, capsys
) -> None:
    source = populated_workspace(tmp_path / "source", project_name="CLI", sentinel=b"cli")
    bundle = tmp_path / "cli.bundle"
    target = tmp_path / "restored"

    assert workspace_cli(["backup", "--workspace", str(source), "--output", str(bundle)]) == 0
    backup = json.loads(capsys.readouterr().out)
    assert backup["ok"] is True
    assert backup["sha256"]

    assert workspace_cli(["preflight", "--bundle", str(bundle), "--target", str(target)]) == 0
    preflight = json.loads(capsys.readouterr().out)
    assert preflight["preflight"]["target_state"] == "missing"

    assert workspace_cli(["restore", "--bundle", str(bundle), "--target", str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["report"]["restored_projects"] == 1
    assert project_names(target) == ["CLI"]
