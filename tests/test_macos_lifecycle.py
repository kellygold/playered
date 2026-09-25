from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import image23mf.macos.lifecycle as lifecycle
from image23mf.macos.lifecycle import (
    InstallError,
    MacOSPaths,
    install_release,
    load_workspace,
    uninstall_application,
)


def make_inputs(root: Path, *, marker: str = "one") -> tuple[Path, Path]:
    wheel = root / f"image23mf-{marker}.whl"
    wheel.write_bytes(f"wheel-{marker}".encode())
    frontend = root / f"frontend-{marker}"
    frontend.mkdir()
    (frontend / "index.html").write_text(f"<h1>{marker}</h1>", encoding="utf-8")
    (frontend / "asset.js").write_text(f"export default {marker!r}", encoding="utf-8")
    return wheel, frontend


def fake_environment(destination: Path, wheel: Path) -> Path:
    python = destination / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!{sys.executable}\n# {wheel.name}\n", encoding="utf-8")
    python.chmod(0o755)
    return python


def migration_that_sets(value: str):
    def migrate(_python: Path, workspace: Path) -> None:
        with sqlite3.connect(workspace / "image23mf.sqlite3") as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS release_marker(value TEXT NOT NULL)")
            connection.execute("DELETE FROM release_marker")
            connection.execute("INSERT INTO release_marker VALUES (?)", (value,))

    return migrate


def marker(workspace: Path) -> str:
    with sqlite3.connect(workspace / "image23mf.sqlite3") as connection:
        return connection.execute("SELECT value FROM release_marker").fetchone()[0]


def test_clean_install_builds_immutable_release_app_and_selected_workspace(tmp_path: Path) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    workspace = tmp_path / "projects"
    wheel, frontend = make_inputs(tmp_path)

    result = install_release(
        wheel=wheel,
        frontend=frontend,
        version="1.2.3",
        paths=paths,
        workspace=workspace,
        environment_builder=fake_environment,
        migration_runner=migration_that_sets("v1"),
    )

    assert result.previous_version is None
    assert paths.current.resolve() == paths.releases / "1.2.3"
    assert (result.release / "web" / "index.html").read_text() == "<h1>one</h1>"
    assert load_workspace(paths) == workspace.resolve()
    assert stat.S_IMODE(paths.config.stat().st_mode) == 0o600
    assert marker(workspace) == "v1"
    executable = paths.app_bundle / "Contents" / "MacOS" / "Image23MF Studio"
    assert executable.stat().st_mode & 0o111
    assert str(paths.current) in executable.read_text(encoding="utf-8")
    manifest = json.loads((result.release / "manifest.json").read_text())
    assert manifest["version"] == "1.2.3"
    assert manifest["wheel_sha256"]
    assert manifest["frontend_sha256"]


def test_failed_upgrade_restores_sqlite_and_never_switches_current(tmp_path: Path) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    workspace = tmp_path / "projects"
    wheel_v1, frontend_v1 = make_inputs(tmp_path, marker="one")
    install_release(
        wheel=wheel_v1,
        frontend=frontend_v1,
        version="1.0.0",
        paths=paths,
        workspace=workspace,
        environment_builder=fake_environment,
        migration_runner=migration_that_sets("healthy"),
    )
    wheel_v2, frontend_v2 = make_inputs(tmp_path, marker="two")

    def corrupt_then_fail(_python: Path, selected_workspace: Path) -> None:
        migration_that_sets("corrupt")(_python, selected_workspace)
        raise RuntimeError("bad migration")

    with pytest.raises(InstallError, match="previous release and database were restored"):
        install_release(
            wheel=wheel_v2,
            frontend=frontend_v2,
            version="2.0.0",
            paths=paths,
            environment_builder=fake_environment,
            migration_runner=corrupt_then_fail,
        )

    assert paths.current.resolve() == paths.releases / "1.0.0"
    assert not (paths.releases / "2.0.0").exists()
    assert marker(workspace) == "healthy"
    backups = list((workspace / "backups" / "upgrades").glob("before-2.0.0-*.sqlite3"))
    assert len(backups) == 1


def test_failed_first_migration_removes_partial_new_database(tmp_path: Path) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    workspace = tmp_path / "projects"
    wheel, frontend = make_inputs(tmp_path)

    def create_partial_database_then_fail(_python: Path, selected_workspace: Path) -> None:
        migration_that_sets("partial")(_python, selected_workspace)
        raise RuntimeError("migration failed")

    with pytest.raises(InstallError):
        install_release(
            wheel=wheel,
            frontend=frontend,
            version="1.0.0",
            paths=paths,
            workspace=workspace,
            environment_builder=fake_environment,
            migration_runner=create_partial_database_then_fail,
        )

    assert not (workspace / "image23mf.sqlite3").exists()
    assert not paths.current.exists()
    assert not (paths.releases / "1.0.0").exists()


def test_successful_upgrade_switches_only_after_migration_and_retains_rollback_release(
    tmp_path: Path,
) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    workspace = tmp_path / "projects"
    wheel_v1, frontend_v1 = make_inputs(tmp_path, marker="one")
    install_release(
        wheel=wheel_v1,
        frontend=frontend_v1,
        version="1.0.0",
        paths=paths,
        workspace=workspace,
        environment_builder=fake_environment,
        migration_runner=migration_that_sets("v1"),
    )
    wheel_v2, frontend_v2 = make_inputs(tmp_path, marker="two")
    observed_current: list[Path] = []

    def migrate_v2(python: Path, selected_workspace: Path) -> None:
        observed_current.append(paths.current.resolve())
        migration_that_sets("v2")(python, selected_workspace)

    result = install_release(
        wheel=wheel_v2,
        frontend=frontend_v2,
        version="2.0.0",
        paths=paths,
        environment_builder=fake_environment,
        migration_runner=migrate_v2,
    )

    assert observed_current == [paths.releases / "1.0.0"]
    assert result.previous_version == "1.0.0"
    assert paths.current.resolve() == paths.releases / "2.0.0"
    assert (paths.releases / "1.0.0").is_dir()
    assert marker(workspace) == "v2"
    assert result.backup is not None
    assert stat.S_IMODE(result.backup.stat().st_mode) == 0o600


def test_same_version_cannot_silently_replace_frontend_contents(tmp_path: Path) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    wheel, frontend = make_inputs(tmp_path, marker="one")
    workspace = tmp_path / "projects"
    install_release(
        wheel=wheel,
        frontend=frontend,
        version="1.0.0",
        paths=paths,
        workspace=workspace,
        environment_builder=fake_environment,
        migration_runner=migration_that_sets("v1"),
    )
    (frontend / "index.html").write_text("changed", encoding="utf-8")

    with pytest.raises(InstallError, match="different contents"):
        install_release(
            wheel=wheel,
            frontend=frontend,
            version="1.0.0",
            paths=paths,
            environment_builder=fake_environment,
            migration_runner=migration_that_sets("unexpected"),
        )

    assert marker(workspace) == "v1"


def test_uninstall_preserves_workspace_even_when_nested_under_app_support(tmp_path: Path) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    workspace = paths.support / "my-workspace"
    wheel, frontend = make_inputs(tmp_path)
    install_release(
        wheel=wheel,
        frontend=frontend,
        version="1.0.0",
        paths=paths,
        workspace=workspace,
        environment_builder=fake_environment,
        migration_runner=migration_that_sets("keep"),
    )
    sentinel = workspace / "source-image.png"
    sentinel.write_bytes(b"irreplaceable")

    preserved = uninstall_application(paths)

    assert preserved == workspace.resolve()
    assert sentinel.read_bytes() == b"irreplaceable"
    assert marker(workspace) == "keep"
    assert not paths.app_bundle.exists()
    assert not paths.releases.exists()
    assert not paths.current.exists()
    assert not paths.config.exists()


def test_uninstall_is_possible_with_corrupt_config_and_does_not_delete_unknown_data(
    tmp_path: Path,
) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    paths.support.mkdir(parents=True)
    paths.config.write_text("not json", encoding="utf-8")
    unknown = paths.support / "unknown-user-data"
    unknown.mkdir()
    sentinel = unknown / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    assert uninstall_application(paths) is None
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_stale_recycled_pid_is_never_signalled(tmp_path: Path, monkeypatch) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "server.pid").write_text("4242\n", encoding="ascii")
    (paths.runtime / "server.json").write_text("{}", encoding="utf-8")
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(lifecycle, "_is_launcher_process", lambda _pid: False)
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    lifecycle.stop_running_instance(paths)

    assert signals == []
    assert not (paths.runtime / "server.pid").exists()
    assert not (paths.runtime / "server.json").exists()


def test_permission_denied_stopping_live_process_cancels_update_and_keeps_state(
    tmp_path: Path, monkeypatch
) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "server.pid").write_text("4242\n", encoding="ascii")
    monkeypatch.setattr(lifecycle, "_is_launcher_process", lambda _pid: True)
    monkeypatch.setattr(
        lifecycle.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(InstallError, match="cancelled before migration"):
        lifecycle.stop_running_instance(paths)

    assert (paths.runtime / "server.pid").read_text(encoding="ascii") == "4242\n"


@pytest.mark.parametrize(
    "inspection_error",
    [
        PermissionError("ps denied"),
        subprocess.TimeoutExpired(["/bin/ps"], timeout=2),
    ],
)
def test_process_identity_inspection_failure_is_never_treated_as_stale(
    tmp_path: Path, monkeypatch, inspection_error: Exception
) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    paths.runtime.mkdir(parents=True)
    pid_path = paths.runtime / "server.pid"
    pid_path.write_text("4242\n", encoding="ascii")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(inspection_error),
    )

    with pytest.raises(InstallError, match="cancelled before migration"):
        lifecycle.stop_running_instance(paths)

    assert pid_path.read_text(encoding="ascii") == "4242\n"


def test_pid_state_read_permission_failure_is_never_treated_as_stale(
    tmp_path: Path, monkeypatch
) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    paths.runtime.mkdir(parents=True)
    pid_path = paths.runtime / "server.pid"
    pid_path.write_text("4242\n", encoding="ascii")
    original_read_text = Path.read_text

    def denied_read(path: Path, *args, **kwargs):
        if path == pid_path:
            raise PermissionError("read denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied_read)

    with pytest.raises(InstallError, match="cancelled before migration"):
        lifecycle.stop_running_instance(paths)

    assert pid_path.exists()


def test_failed_ps_for_clearly_nonexistent_pid_is_safe_stale_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    paths = MacOSPaths.for_home(tmp_path / "home")
    paths.runtime.mkdir(parents=True)
    pid_path = paths.runtime / "server.pid"
    pid_path.write_text("4242\n", encoding="ascii")
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["/bin/ps"])
        ),
    )
    monkeypatch.setattr(
        lifecycle.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )

    lifecycle.stop_running_instance(paths)

    assert not pid_path.exists()


def test_generated_app_launcher_shell_quotes_adversarial_home_path(tmp_path: Path) -> None:
    home = tmp_path / 'home "$(touch pwned)" `touch pwned2` $HOME'
    paths = MacOSPaths.for_home(home)
    fake_python = paths.current / "venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    arguments = tmp_path / "arguments.txt"
    fake_python.write_text(
        f'#!/bin/zsh\nprint -r -- "$@" > {lifecycle.shlex.quote(str(arguments))}\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    lifecycle._write_app_bundle(paths, version="1.2.3")
    executable = paths.app_bundle / "Contents" / "MacOS" / "Image23MF Studio"
    subprocess.run([str(executable), "--probe"], check=True, cwd=tmp_path)

    assert arguments.read_text(encoding="utf-8").strip() == ("-m image23mf.macos.launcher --probe")
    assert not (tmp_path / "pwned").exists()
    assert not (tmp_path / "pwned2").exists()
