"""Atomic per-user macOS install, upgrade, and uninstall lifecycle.

The workspace is user data and is never part of a release directory.  Releases are immutable;
``current`` is an atomically replaced symlink.  An upgrade migrates a SQLite backup first and
does not move ``current`` until the new environment proves it can open the workspace.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
import venv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "Image23MF Studio"
CONFIG_SCHEMA_VERSION = 1
DATABASE_FILENAME = "image23mf.sqlite3"
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,79}$")


class InstallError(RuntimeError):
    """The staged release could not be installed without risking user data."""


@dataclass(frozen=True)
class MacOSPaths:
    home: Path
    support: Path
    releases: Path
    current: Path
    runtime: Path
    config: Path
    logs: Path
    app_bundle: Path

    @classmethod
    def for_home(cls, home: Optional[Path] = None) -> MacOSPaths:
        root = (home or Path.home()).expanduser().resolve()
        support = root / "Library" / "Application Support" / APP_NAME
        return cls(
            home=root,
            support=support,
            releases=support / "releases",
            current=support / "current",
            runtime=support / "runtime",
            config=support / "config.json",
            logs=root / "Library" / "Logs" / APP_NAME,
            app_bundle=root / "Applications" / f"{APP_NAME}.app",
        )


@dataclass(frozen=True)
class InstallResult:
    version: str
    release: Path
    previous_version: Optional[str]
    workspace: Optional[Path]
    backup: Optional[Path]


EnvironmentBuilder = Callable[[Path, Path], Path]
MigrationRunner = Callable[[Path, Path], None]


def load_workspace(paths: MacOSPaths) -> Optional[Path]:
    if not paths.config.is_file():
        return None
    try:
        payload = json.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InstallError(f"launcher configuration is unreadable: {error}") from error
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise InstallError("launcher configuration was written by an unsupported version")
    raw = payload.get("workspace")
    if not isinstance(raw, str) or not raw.strip():
        raise InstallError("launcher configuration has no workspace path")
    return Path(raw).expanduser().resolve()


def save_workspace(paths: MacOSPaths, workspace: Path) -> Path:
    resolved = workspace.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    paths.support.mkdir(parents=True, exist_ok=True)
    _atomic_json(
        paths.config,
        {"schema_version": CONFIG_SCHEMA_VERSION, "workspace": str(resolved)},
    )
    return resolved


def install_release(
    *,
    wheel: Path,
    frontend: Path,
    version: str,
    paths: Optional[MacOSPaths] = None,
    workspace: Optional[Path] = None,
    environment_builder: Optional[EnvironmentBuilder] = None,
    migration_runner: Optional[MigrationRunner] = None,
) -> InstallResult:
    """Stage, validate, migrate, and atomically activate one immutable release."""

    if not VERSION_PATTERN.fullmatch(version):
        raise InstallError("release version contains unsupported characters")
    wheel = wheel.expanduser().resolve()
    frontend = frontend.expanduser().resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise InstallError(f"wheel is missing or invalid: {wheel}")
    if not (frontend / "index.html").is_file():
        raise InstallError(f"frontend build is missing index.html: {frontend}")
    resolved_paths = paths or MacOSPaths.for_home()
    resolved_workspace = (
        save_workspace(resolved_paths, workspace)
        if workspace is not None
        else load_workspace(resolved_paths)
    )
    builder = environment_builder or _build_environment
    migrate = migration_runner or _run_migrations
    resolved_paths.releases.mkdir(parents=True, exist_ok=True)
    previous_version = _current_version(resolved_paths)
    destination = resolved_paths.releases / version
    backup = None

    if destination.exists():
        _validate_existing_release(destination, version, wheel, frontend)
    else:
        staging = Path(
            tempfile.mkdtemp(prefix=f".{version}-", dir=resolved_paths.releases)
        ).resolve()
        try:
            python = builder(staging / "venv", wheel)
            shutil.copytree(frontend, staging / "web", symlinks=False)
            _atomic_json(
                staging / "manifest.json",
                {
                    "schema_version": 1,
                    "version": version,
                    "wheel_sha256": _sha256(wheel),
                    "frontend_sha256": _tree_sha256(frontend),
                    "installed_at": _utc_now(),
                    "python": str(python.relative_to(staging)),
                },
            )
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    python = destination / _release_python(destination)
    if resolved_workspace is not None:
        # The old API must be quiescent before a staged release changes the database. SQLite's
        # backup API is WAL-safe, but running two application schemas against one workspace is not.
        stop_running_instance(resolved_paths)
        database_existed = (resolved_workspace / DATABASE_FILENAME).is_file()
        backup = backup_workspace_database(resolved_workspace, resolved_paths, version=version)
        try:
            migrate(python, resolved_workspace)
        except Exception as error:
            if backup is not None:
                restore_workspace_database(resolved_workspace, backup)
            elif not database_existed:
                _remove_database_files(resolved_workspace / DATABASE_FILENAME)
            if previous_version != version:
                shutil.rmtree(destination, ignore_errors=True)
            raise InstallError(
                "the new release could not migrate the workspace; the previous release and "
                "database were restored"
            ) from error

    _activate(resolved_paths, destination)
    _write_app_bundle(resolved_paths, version=version)
    return InstallResult(
        version=version,
        release=destination,
        previous_version=previous_version,
        workspace=resolved_workspace,
        backup=backup,
    )


def uninstall_application(paths: Optional[MacOSPaths] = None) -> Optional[Path]:
    """Remove application/runtime files while preserving the selected workspace verbatim."""

    resolved_paths = paths or MacOSPaths.for_home()
    try:
        workspace = load_workspace(resolved_paths)
    except InstallError:
        # A corrupt launcher preference must not make the application impossible to remove.
        # The workspace is external to the installation either way and remains untouched.
        workspace = None
    stop_running_instance(resolved_paths)
    if resolved_paths.app_bundle.exists():
        shutil.rmtree(resolved_paths.app_bundle)
    if resolved_paths.current.exists() or resolved_paths.current.is_symlink():
        resolved_paths.current.unlink()
    shutil.rmtree(resolved_paths.releases, ignore_errors=True)
    shutil.rmtree(resolved_paths.runtime, ignore_errors=True)
    shutil.rmtree(resolved_paths.logs, ignore_errors=True)
    resolved_paths.config.unlink(missing_ok=True)
    _remove_empty_parents(resolved_paths.support, stop=resolved_paths.home)
    return workspace


def backup_workspace_database(
    workspace: Path, paths: MacOSPaths, *, version: str
) -> Optional[Path]:
    source = workspace / DATABASE_FILENAME
    if not source.is_file():
        return None
    backup_dir = workspace / "backups" / "upgrades"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = backup_dir / f"before-{version}-{timestamp}-{uuid.uuid4().hex[:8]}.sqlite3"
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)
    target.chmod(0o600)
    return target


def restore_workspace_database(workspace: Path, backup: Path) -> None:
    destination = workspace / DATABASE_FILENAME
    temporary = destination.with_suffix(".restore.sqlite3")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(backup) as source, sqlite3.connect(temporary) as target:
        source.backup(target)
    for suffix in ("-wal", "-shm"):
        Path(f"{destination}{suffix}").unlink(missing_ok=True)
    os.replace(temporary, destination)


def _remove_database_files(database: Path) -> None:
    database.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)


def stop_running_instance(paths: MacOSPaths, *, timeout_seconds: float = 5.0) -> None:
    pid_path = paths.runtime / "server.pid"
    if not pid_path.is_file():
        return
    try:
        raw_pid = pid_path.read_text(encoding="ascii").strip()
    except PermissionError as error:
        raise InstallError(
            "macOS denied permission to read the Image23MF Studio process state; the update "
            "was cancelled before migration"
        ) from error
    except OSError as error:
        raise InstallError(
            "the Image23MF Studio process state could not be read safely; the update was "
            "cancelled before migration"
        ) from error
    try:
        pid = int(raw_pid)
    except ValueError:
        pid_path.unlink(missing_ok=True)
        return
    if not _is_launcher_process(pid):
        pid_path.unlink(missing_ok=True)
        (paths.runtime / "server.json").unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        return
    except PermissionError as error:
        raise InstallError(
            "macOS denied permission to stop the running Image23MF Studio process; the update "
            "was cancelled before migration"
        ) from error
    except OSError as error:
        raise InstallError(
            "the running Image23MF Studio process could not be stopped safely; the update was "
            "cancelled before migration"
        ) from error
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            break
        time.sleep(0.05)
    else:
        # The PID was verified as our launcher before SIGTERM. A wedged local server must not keep
        # the port or database open while an update switches releases.
        os.kill(pid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline and _process_exists(pid):
            time.sleep(0.05)
        if _process_exists(pid):
            raise InstallError("the running application could not be stopped safely")
    pid_path.unlink(missing_ok=True)
    (paths.runtime / "server.json").unlink(missing_ok=True)


def _is_launcher_process(pid: int) -> bool:
    """Refuse to signal a recycled PID that belongs to an unrelated user process."""

    if pid <= 1:
        return False
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.CalledProcessError as error:
        if not _process_exists(pid):
            return False
        raise InstallError(
            "the running Image23MF Studio process identity could not be confirmed; the update "
            "was cancelled before migration"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise InstallError(
            "inspection of the running Image23MF Studio process timed out; the update was "
            "cancelled before migration"
        ) from error
    except PermissionError as error:
        raise InstallError(
            "macOS denied permission to inspect the running Image23MF Studio process; the update "
            "was cancelled before migration"
        ) from error
    except OSError as error:
        raise InstallError(
            "the running Image23MF Studio process identity could not be inspected safely; the "
            "update was cancelled before migration"
        ) from error
    return "image23mf.macos.launcher" in result.stdout


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise InstallError(
            "macOS denied permission to inspect the running Image23MF Studio process"
        ) from error
    except OSError as error:
        raise InstallError(
            "the running Image23MF Studio process could not be inspected safely"
        ) from error
    return True


def _build_environment(destination: Path, wheel: Path) -> Path:
    venv.EnvBuilder(with_pip=True, clear=False, symlinks=True).create(destination)
    python = destination / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)],
        check=True,
    )
    return python


def _run_migrations(python: Path, workspace: Path) -> None:
    subprocess.run(
        [
            str(python),
            "-m",
            "image23mf.macos.launcher",
            "--migrate-only",
            "--workspace",
            str(workspace),
        ],
        check=True,
        timeout=120,
    )


def _activate(paths: MacOSPaths, release: Path) -> None:
    paths.support.mkdir(parents=True, exist_ok=True)
    temporary = paths.support / f".current-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release, target_is_directory=True)
    os.replace(temporary, paths.current)


def _write_app_bundle(paths: MacOSPaths, *, version: str) -> None:
    executable = paths.app_bundle / "Contents" / "MacOS" / APP_NAME
    resources = paths.app_bundle / "Contents" / "Resources"
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIdentifier": "local.image23mf.studio",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version.split("+")[0].split("-")[0],
        "CFBundleVersion": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
    }
    with (paths.app_bundle / "Contents" / "Info.plist").open("wb") as output:
        plistlib.dump(plist, output, sort_keys=True)
    python = shlex.quote(str(paths.current / "venv" / "bin" / "python"))
    script = f'#!/bin/zsh\nexec {python} -m image23mf.macos.launcher "$@"\n'
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)


def _validate_existing_release(
    destination: Path, version: str, wheel: Path, frontend: Path
) -> None:
    try:
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InstallError(f"existing release {version} is incomplete") from error
    if (
        manifest.get("version") != version
        or manifest.get("wheel_sha256") != _sha256(wheel)
        or manifest.get("frontend_sha256") != _tree_sha256(frontend)
    ):
        raise InstallError(f"release {version} already exists with different contents")
    if not (destination / _release_python(destination)).is_file():
        raise InstallError(f"release {version} has no Python runtime")
    if not (destination / "web" / "index.html").is_file():
        raise InstallError(f"release {version} has no frontend")


def _release_python(release: Path) -> Path:
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    value = manifest.get("python")
    if not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts:
        raise InstallError("release manifest has an unsafe Python path")
    return Path(value)


def _current_version(paths: MacOSPaths) -> Optional[str]:
    if not paths.current.is_symlink():
        return None
    try:
        manifest = json.loads((paths.current / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = manifest.get("version")
    return str(value) if value else None


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remove_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and current.exists():
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
