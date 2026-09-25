"""Checksummed whole-workspace backup and rollback-safe disaster recovery."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, Optional

from image23mf import __version__
from image23mf.artifact_reveal import FinderOpener, FinderRevealFailedError
from image23mf.contracts.workspace_bundle import (
    WorkspaceBundleManifest,
    WorkspaceBundleMember,
    WorkspaceBundlePreflight,
    WorkspaceRestoreReport,
)
from image23mf.macos.lifecycle import MacOSPaths
from image23mf.storage import CURRENT_DATABASE_VERSION, open_database
from image23mf.storage.repositories import canonical_json

WORKSPACE_BUNDLE_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "image23mf.sqlite3"
REPORT_DIRECTORY = ".image23mf/restore-reports"
CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MEMBER_COUNT = 100_000
MAX_MEMBER_BYTES = 20 * 1024 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
EXCLUDED_ROOT_FILES = frozenset({f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm"})
EXCLUDED_ROOT_DIRECTORIES = frozenset({"temp"})
RestoreChoice = Literal["empty_only", "replace"]


class WorkspaceBundleError(RuntimeError):
    """Base class for safe workspace backup and restore failures."""


class WorkspaceBundleIntegrityError(WorkspaceBundleError):
    """Archive bytes, paths, manifest, or database evidence are invalid."""


class WorkspaceBundleVersionError(WorkspaceBundleError):
    """Archive or database schema is newer than this application supports."""


class WorkspaceConflictError(WorkspaceBundleError):
    """The default non-destructive restore policy refused a populated target."""

    def __init__(self, preflight: WorkspaceBundlePreflight) -> None:
        self.preflight = preflight
        super().__init__(
            "The target workspace is populated. Nothing was changed. Choose a different empty "
            "folder, import a project bundle as a copy, or explicitly choose replace after "
            "reviewing the automatic rollback-backup location."
        )


class WorkspaceActiveError(WorkspaceBundleError):
    """A running packaged application still owns the restore target."""

    def __init__(self, target: Path) -> None:
        self.target = target
        super().__init__(
            "Image23MF Studio is currently using this workspace. Nothing was changed. Quit the "
            f"application completely, confirm it is no longer running, then retry: '{target}'."
        )


class WorkspacePermissionError(WorkspaceBundleError):
    """macOS denied a filesystem action required for safe backup or restore."""

    def __init__(self, action: str, path: Path, error: OSError) -> None:
        self.action = action
        self.path = path
        self.errno = error.errno
        super().__init__(
            f"Image23MF Studio cannot {action} '{path}'. Nothing was intentionally overwritten. "
            "Choose a writable folder or allow Image23MF Studio access in System Settings > "
            "Privacy & Security > Files and Folders (or Full Disk Access), then retry. "
            f"macOS reported: {error.strerror or error}."
        )


class WorkspaceSwapRecoveryError(WorkspaceBundleError):
    """The final swap and its automatic directory rollback both failed."""

    def __init__(
        self,
        *,
        target: Path,
        previous_workspace: Path,
        staged_workspace: Path,
        rollback_bundle: Optional[Path],
    ) -> None:
        self.target = target
        self.previous_workspace = previous_workspace
        self.staged_workspace = staged_workspace
        self.rollback_bundle = rollback_bundle
        rollback_text = str(rollback_bundle) if rollback_bundle else "not required"
        super().__init__(
            "The restored workspace could not be activated and the previous directory could not "
            f"be moved back automatically. Do not delete anything. Previous data: "
            f"'{previous_workspace}'. Verified staged restore: '{staged_workspace}'. Expected "
            f"workspace: '{target}'. Rollback archive: '{rollback_text}'. Quit Image23MF Studio, "
            "make a copy of both directories, then move the previous directory back to the target "
            "path or restore the rollback archive into an empty folder."
        )


class WorkspaceFinderError(WorkspaceBundleError):
    """Finder could not reveal a verified backup package."""


@dataclass(frozen=True)
class WorkspaceBundleExportResult:
    path: Path
    sha256: str
    byte_size: int
    manifest: WorkspaceBundleManifest


@dataclass(frozen=True)
class WorkspaceFinderResult:
    path: Path
    supported: bool
    revealed: bool
    reason: Optional[str] = None


class WorkspaceBundleService:
    """Create and restore complete workspace snapshots without in-place extraction."""

    def __init__(
        self,
        workspace: Path,
        *,
        replace_fn: Callable[[Path, Path], None] = os.replace,
        active_workspace_check: Optional[Callable[[Path], bool]] = None,
    ) -> None:
        if workspace.is_symlink():
            raise WorkspaceBundleError("workspace root cannot be a symbolic link")
        self.workspace = _absolute_without_following_leaf(workspace)
        self._replace = replace_fn
        self._active_workspace_check = active_workspace_check or packaged_app_uses_workspace

    def export(self, destination: Path) -> WorkspaceBundleExportResult:
        """Publish one atomic archive from a SQLite-consistent workspace snapshot."""

        destination = _absolute_without_following_leaf(destination)
        if _is_within(destination, self.workspace):
            raise WorkspaceBundleError("backup destination must be outside the source workspace")
        database = self.workspace / DATABASE_NAME
        if not database.is_file():
            raise WorkspaceBundleError(f"workspace database is missing: {database}")
        _ensure_directory(destination.parent, "create the backup folder")

        try:
            with tempfile.TemporaryDirectory(
                prefix=f".{destination.name}.stage-", dir=destination.parent
            ) as temporary_name:
                stage = Path(temporary_name)
                stage.chmod(0o700)
                database_snapshot = stage / DATABASE_NAME
                _backup_sqlite(database, database_snapshot)
                manifest, source_by_relative = self._build_manifest(database_snapshot)
                temporary_archive = stage / "archive.tmp"
                self._write_archive(
                    temporary_archive,
                    manifest,
                    database_snapshot=database_snapshot,
                    source_by_relative=source_by_relative,
                )
                _fsync_file(temporary_archive)
                _replace_file(temporary_archive, destination, self._replace)
                _fsync_directory(destination.parent)
        except OSError as error:
            raise _permission_or_bundle_error(
                "write the workspace backup", destination, error
            ) from error

        digest, size = _hash_file(destination)
        return WorkspaceBundleExportResult(destination, digest, size, manifest)

    def verify(self, bundle_path: Path) -> tuple[str, int, WorkspaceBundleManifest]:
        """Fully stream-check the archive and its embedded SQLite snapshot."""

        bundle = _absolute_without_following_leaf(bundle_path)
        try:
            size_on_disk = bundle.stat().st_size
            if size_on_disk > MAX_ARCHIVE_BYTES:
                raise WorkspaceBundleIntegrityError("workspace backup exceeds the archive limit")
            bundle_sha256, bundle_size = _hash_file(bundle)
            with zipfile.ZipFile(bundle, "r") as archive:
                infos = _validated_infos(archive)
                manifest_info = infos.get(MANIFEST_NAME)
                if manifest_info is None:
                    raise WorkspaceBundleIntegrityError("workspace backup is missing manifest.json")
                if manifest_info.file_size > MAX_MANIFEST_BYTES:
                    raise WorkspaceBundleIntegrityError("workspace backup manifest is too large")
                try:
                    manifest = WorkspaceBundleManifest.model_validate_json(
                        archive.read(manifest_info)
                    )
                except Exception as error:
                    raise WorkspaceBundleIntegrityError(
                        "workspace backup manifest is invalid"
                    ) from error
                self._validate_manifest_version(manifest)
                expected = {MANIFEST_NAME, *(member.archive_path for member in manifest.members)}
                if set(infos) != expected:
                    missing = sorted(expected - set(infos))
                    extra = sorted(set(infos) - expected)
                    raise WorkspaceBundleIntegrityError(
                        "workspace backup member set is not closed; "
                        f"missing={missing}, extra={extra}"
                    )
                database_member = next(
                    member for member in manifest.members if member.kind == "database"
                )
                with tempfile.TemporaryDirectory(prefix="image23mf-preflight-") as temporary_name:
                    temporary = Path(temporary_name)
                    temporary.chmod(0o700)
                    database_copy = temporary / DATABASE_NAME
                    for member in manifest.members:
                        info = infos[member.archive_path]
                        if info.file_size != member.byte_size:
                            raise WorkspaceBundleIntegrityError(
                                f"workspace backup member size mismatch: {member.archive_path}"
                            )
                        if member is database_member:
                            digest, size = _copy_and_hash(archive.open(info), database_copy)
                        else:
                            digest, size = _hash_stream(archive.open(info))
                        if digest != member.sha256 or size != member.byte_size:
                            raise WorkspaceBundleIntegrityError(
                                f"workspace backup member checksum mismatch: {member.archive_path}"
                            )
                    self._validate_database_snapshot(database_copy, manifest)
        except WorkspaceBundleError:
            raise
        except OSError as error:
            raise _permission_or_bundle_error("read the workspace backup", bundle, error) from error
        except (zipfile.BadZipFile, RuntimeError) as error:
            raise WorkspaceBundleIntegrityError(
                "workspace backup is not a readable safe ZIP archive"
            ) from error
        return bundle_sha256, bundle_size, manifest

    def preflight(self, bundle_path: Path, target_workspace: Path) -> WorkspaceBundlePreflight:
        bundle = _absolute_without_following_leaf(bundle_path)
        target = _validate_target_path(target_workspace)
        bundle_sha256, bundle_size, manifest = self.verify(bundle)
        state = _target_state(target)
        target_bytes = _workspace_size(target) if state == "populated" else 0
        multiplier_bytes = manifest.total_payload_bytes + target_bytes
        required = multiplier_bytes + max(64 * 1024 * 1024, multiplier_bytes // 10)
        disk_anchor = _nearest_existing_directory(target.parent)
        try:
            available = shutil.disk_usage(disk_anchor).free
        except OSError as error:
            raise _permission_or_bundle_error(
                "inspect free disk space", disk_anchor, error
            ) from error
        warnings = []
        if state == "populated":
            warnings.append(
                "The default choice will not modify this populated workspace. Explicit replace "
                "first creates a separate rollback archive."
            )
        if available < required:
            warnings.append(
                "Available disk space is below the conservative staging and rollback estimate."
            )
        return WorkspaceBundlePreflight(
            bundle_path=str(bundle),
            bundle_sha256=bundle_sha256,
            bundle_byte_size=bundle_size,
            source_workspace_name=manifest.source_workspace_name,
            application_version=manifest.application_version,
            database_schema_version=manifest.database_schema_version,
            project_count=manifest.project_count,
            asset_count=manifest.asset_count,
            artifact_count=manifest.artifact_count,
            member_count=len(manifest.members),
            payload_bytes=manifest.total_payload_bytes,
            target_workspace=str(target),
            target_state=state,
            conflict_choices=("empty_only", "replace") if state == "populated" else ("empty_only",),
            required_free_bytes=required,
            available_free_bytes=available,
            warnings=tuple(warnings),
        )

    def restore(
        self,
        bundle_path: Path,
        target_workspace: Path,
        *,
        choice: RestoreChoice = "empty_only",
    ) -> WorkspaceRestoreReport:
        """Restore through a fully verified sibling stage and one atomic directory swap."""

        if choice not in {"empty_only", "replace"}:
            raise WorkspaceBundleError(f"unsupported restore conflict choice: {choice}")
        bundle = _absolute_without_following_leaf(bundle_path)
        target = _validate_target_path(target_workspace)
        preflight = self.preflight(bundle, target)
        if preflight.target_state == "populated" and choice != "replace":
            raise WorkspaceConflictError(preflight)
        if self._active_workspace_check(target):
            raise WorkspaceActiveError(target)
        if preflight.available_free_bytes < preflight.required_free_bytes:
            raise WorkspaceBundleError(
                "Restore was not started because free disk space is below the conservative "
                "staging and rollback requirement. Choose another volume or free space first."
            )
        _ensure_directory(target.parent, "create the restore parent folder")

        rollback_bundle = None
        if preflight.target_state == "populated":
            rollback_bundle = target.parent / (
                f".{target.name}.before-restore-{_filename_timestamp()}-"
                f"{uuid.uuid4().hex[:8]}.image23mf-workspace"
            )
            # A destructive choice remains recoverable even if the incoming archive later fails.
            WorkspaceBundleService(target).export(rollback_bundle)

        try:
            stage = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
            ).resolve()
        except OSError as error:
            raise _permission_or_bundle_error(
                "create the staged restore workspace", target.parent, error
            ) from error
        stage.chmod(0o700)
        previous = target.parent / f".{target.name}.previous-{uuid.uuid4().hex}"
        preserve_previous = False
        try:
            # Verify again after preflight to close the archive-change TOCTOU window.
            digest, _, manifest = self.verify(bundle)
            if digest != preflight.bundle_sha256:
                raise WorkspaceBundleIntegrityError(
                    "workspace backup changed after preflight; restore was cancelled"
                )
            self._materialize(bundle, manifest, stage)
            migrated_schema = self._validate_staged_workspace(stage, manifest)
            report = WorkspaceRestoreReport(
                restored_at=_utc_now(),
                bundle_path=str(bundle),
                bundle_sha256=digest,
                target_workspace=str(target),
                original_target_state=preflight.target_state,
                conflict_choice=choice,
                rollback_bundle=str(rollback_bundle) if rollback_bundle else None,
                restored_projects=manifest.project_count,
                restored_assets=manifest.asset_count,
                restored_artifacts=manifest.artifact_count,
                restored_members=len(manifest.members),
                restored_payload_bytes=manifest.total_payload_bytes,
                database_schema_version=migrated_schema,
                warnings=preflight.warnings,
            )
            self._write_restore_report(stage, report)
            self._activate_restore(
                stage=stage,
                target=target,
                previous=previous,
                rollback_bundle=rollback_bundle,
            )
            stage = target
            return report
        except WorkspaceSwapRecoveryError:
            # The exception names every surviving directory. Keep them all for manual recovery.
            preserve_previous = True
            raise
        except Exception:
            if stage.exists() and stage != target:
                shutil.rmtree(stage, ignore_errors=True)
            raise
        finally:
            if previous.exists() and not preserve_previous:
                # Activation succeeded and the separate rollback archive is durable. Failure to
                # remove this hidden copy preserves rather than destroys data.
                shutil.rmtree(previous, ignore_errors=True)

    def _build_manifest(
        self, database_snapshot: Path
    ) -> tuple[WorkspaceBundleManifest, dict[str, Path]]:
        connection = _open_snapshot(database_snapshot)
        try:
            database_schema = _database_schema_version(connection)
            if database_schema > CURRENT_DATABASE_VERSION:
                raise WorkspaceBundleVersionError(
                    f"workspace database schema {database_schema} is newer than supported "
                    f"schema {CURRENT_DATABASE_VERSION}"
                )
            counts = {
                "project_count": _table_count(connection, "projects"),
                "asset_count": _table_count(connection, "assets"),
                "artifact_count": _table_count(connection, "artifacts"),
            }
            referenced = _referenced_content(connection)
        finally:
            connection.close()

        sources = {relative: path for relative, path in _walk_workspace_files(self.workspace)}
        members = []
        source_by_relative = {}
        db_hash, db_size = _hash_file(database_snapshot)
        members.append(
            WorkspaceBundleMember(
                archive_path=f"payload/{DATABASE_NAME}",
                relative_path=DATABASE_NAME,
                sha256=db_hash,
                byte_size=db_size,
                kind="database",
            )
        )
        source_by_relative[DATABASE_NAME] = database_snapshot
        for relative in sorted(sources):
            source = sources[relative]
            digest, size = _hash_file(source)
            if size > MAX_MEMBER_BYTES:
                raise WorkspaceBundleError(f"workspace file exceeds member limit: {relative}")
            members.append(
                WorkspaceBundleMember(
                    archive_path=f"payload/{relative}",
                    relative_path=relative,
                    sha256=digest,
                    byte_size=size,
                    kind="workspace-file",
                )
            )
            source_by_relative[relative] = source
        if len(members) > MAX_MEMBER_COUNT:
            raise WorkspaceBundleError("workspace contains too many files to back up safely")
        total = sum(member.byte_size for member in members)
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise WorkspaceBundleError("workspace exceeds the uncompressed backup size limit")
        by_relative = {member.relative_path: member for member in members}
        for relative, expected_hash, expected_size in referenced:
            member = by_relative.get(relative)
            if member is None:
                raise WorkspaceBundleIntegrityError(
                    f"database references a missing workspace payload: {relative}"
                )
            if member.sha256 != expected_hash or member.byte_size != expected_size:
                raise WorkspaceBundleIntegrityError(
                    f"database payload identity is corrupt: {relative}"
                )
        return (
            WorkspaceBundleManifest(
                schema_version=WORKSPACE_BUNDLE_SCHEMA_VERSION,
                application_version=__version__,
                database_schema_version=database_schema,
                created_at=_utc_now(),
                source_workspace_name=self.workspace.name,
                members=tuple(members),
                total_payload_bytes=total,
                **counts,
            ),
            source_by_relative,
        )

    def _write_archive(
        self,
        destination: Path,
        manifest: WorkspaceBundleManifest,
        *,
        database_snapshot: Path,
        source_by_relative: dict[str, Path],
    ) -> None:
        del database_snapshot  # documented explicitly at the call site; map is authoritative.
        manifest_bytes = canonical_json(manifest.model_dump(mode="json")).encode("utf-8")
        with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            _write_zip_bytes(archive, MANIFEST_NAME, manifest_bytes)
            for member in sorted(manifest.members, key=lambda item: item.archive_path):
                _write_zip_file_verified(
                    archive,
                    member.archive_path,
                    source_by_relative[member.relative_path],
                    expected_sha256=member.sha256,
                    expected_size=member.byte_size,
                )

    def _validate_manifest_version(self, manifest: WorkspaceBundleManifest) -> None:
        if manifest.schema_version > WORKSPACE_BUNDLE_SCHEMA_VERSION:
            raise WorkspaceBundleVersionError(
                f"workspace backup schema {manifest.schema_version} is newer than supported "
                f"schema {WORKSPACE_BUNDLE_SCHEMA_VERSION}"
            )
        if manifest.database_schema_version > CURRENT_DATABASE_VERSION:
            raise WorkspaceBundleVersionError(
                f"workspace database schema {manifest.database_schema_version} is newer than "
                f"supported schema {CURRENT_DATABASE_VERSION}"
            )

    def _validate_database_snapshot(
        self, database: Path, manifest: WorkspaceBundleManifest
    ) -> None:
        connection = _open_snapshot(database)
        try:
            schema = _database_schema_version(connection)
            if schema != manifest.database_schema_version:
                raise WorkspaceBundleIntegrityError(
                    "workspace backup database schema does not match its manifest"
                )
            expected_counts = (
                ("projects", manifest.project_count),
                ("assets", manifest.asset_count),
                ("artifacts", manifest.artifact_count),
            )
            for table, expected in expected_counts:
                if _table_count(connection, table) != expected:
                    raise WorkspaceBundleIntegrityError(
                        f"workspace backup {table} count does not match its manifest"
                    )
            members = {member.relative_path: member for member in manifest.members}
            for relative, digest, size in _referenced_content(connection):
                member = members.get(relative)
                if member is None or member.sha256 != digest or member.byte_size != size:
                    raise WorkspaceBundleIntegrityError(
                        f"workspace backup database reference is not closed: {relative}"
                    )
        finally:
            connection.close()

    def _materialize(self, bundle: Path, manifest: WorkspaceBundleManifest, stage: Path) -> None:
        with zipfile.ZipFile(bundle, "r") as archive:
            infos = _validated_infos(archive)
            for member in manifest.members:
                relative = _portable_path(member.relative_path)
                destination = stage.joinpath(*relative.parts)
                _ensure_stage_parent(stage, destination.parent)
                digest, size = _copy_and_hash(archive.open(infos[member.archive_path]), destination)
                if digest != member.sha256 or size != member.byte_size:
                    raise WorkspaceBundleIntegrityError(
                        f"workspace backup changed while restoring: {member.archive_path}"
                    )

    def _validate_staged_workspace(self, stage: Path, manifest: WorkspaceBundleManifest) -> int:
        connection = open_database(stage / DATABASE_NAME)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise WorkspaceBundleIntegrityError(
                    f"restored workspace database failed quick_check: {result}"
                )
            schema = _database_schema_version(connection)
            expected_counts = (
                ("projects", manifest.project_count),
                ("assets", manifest.asset_count),
                ("artifacts", manifest.artifact_count),
            )
            for table, expected in expected_counts:
                if _table_count(connection, table) != expected:
                    raise WorkspaceBundleIntegrityError(
                        f"restored workspace {table} count changed during validation"
                    )
            for relative, expected_hash, expected_size in _referenced_content(connection):
                path = stage.joinpath(*_portable_path(relative).parts)
                digest, size = _hash_file(path)
                if digest != expected_hash or size != expected_size:
                    raise WorkspaceBundleIntegrityError(
                        f"restored workspace payload failed validation: {relative}"
                    )
            return schema
        finally:
            connection.close()

    def _write_restore_report(self, stage: Path, report: WorkspaceRestoreReport) -> Path:
        report_dir = stage.joinpath(*PurePosixPath(REPORT_DIRECTORY).parts)
        report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = report_dir / f"restore-{_filename_timestamp()}.json"
        _atomic_write_bytes(
            path,
            canonical_json(report.model_dump(mode="json")).encode("utf-8") + b"\n",
        )
        return path

    def _activate_restore(
        self,
        *,
        stage: Path,
        target: Path,
        previous: Path,
        rollback_bundle: Optional[Path],
    ) -> None:
        if stage.stat().st_dev != target.parent.stat().st_dev:
            raise WorkspaceBundleError(
                "staged restore is not on the target filesystem; atomic activation was refused"
            )
        moved_previous = False
        recoverable_stage = stage
        try:
            if target.exists():
                self._replace(target, previous)
                moved_previous = True
            self._replace(stage, target)
            _fsync_directory(target.parent)
        except Exception as activation_error:
            if moved_previous:
                try:
                    if target.exists():
                        failed = target.parent / f".{target.name}.failed-{uuid.uuid4().hex}"
                        self._replace(target, failed)
                        recoverable_stage = failed
                    self._replace(previous, target)
                    _fsync_directory(target.parent)
                except Exception as rollback_error:
                    raise WorkspaceSwapRecoveryError(
                        target=target,
                        previous_workspace=previous,
                        staged_workspace=recoverable_stage,
                        rollback_bundle=rollback_bundle,
                    ) from rollback_error
            elif not stage.exists() and target.exists():
                try:
                    self._replace(target, stage)
                    _fsync_directory(target.parent)
                except Exception as rollback_error:
                    raise WorkspaceSwapRecoveryError(
                        target=target,
                        previous_workspace=previous,
                        staged_workspace=target if target.exists() else stage,
                        rollback_bundle=rollback_bundle,
                    ) from rollback_error
            if isinstance(activation_error, OSError) and activation_error.errno in {
                errno.EACCES,
                errno.EPERM,
                errno.EROFS,
            }:
                raise WorkspacePermissionError(
                    "activate the verified restored workspace", target, activation_error
                ) from activation_error
            raise WorkspaceBundleError(
                "The verified restore could not be activated. The previous workspace was put "
                "back and no replacement was committed."
            ) from activation_error


def reveal_workspace_bundle_in_finder(
    bundle_path: Path,
    *,
    verifier: WorkspaceBundleService,
    platform_name: Optional[str] = None,
    opener: Optional[FinderOpener] = None,
) -> WorkspaceFinderResult:
    """Checksum a server-selected backup before invoking Finder without a shell."""

    bundle = _absolute_without_following_leaf(bundle_path)
    verifier.verify(bundle)
    effective_platform = sys.platform if platform_name is None else platform_name
    if effective_platform != "darwin":
        return WorkspaceFinderResult(
            path=bundle,
            supported=False,
            revealed=False,
            reason="Reveal in Finder is available only on macOS.",
        )
    try:
        (opener or FinderOpener()).reveal(bundle)
    except FinderRevealFailedError as error:
        raise WorkspaceFinderError(
            "Finder could not reveal the verified backup. Confirm the file still exists and allow "
            "Image23MF Studio access in System Settings > Privacy & Security > Files and Folders."
        ) from error
    return WorkspaceFinderResult(path=bundle, supported=True, revealed=True)


def reveal_workspace_in_finder(
    workspace_path: Path,
    *,
    platform_name: Optional[str] = None,
    opener: Optional[FinderOpener] = None,
) -> WorkspaceFinderResult:
    """Validate and reveal a local workspace directory without invoking a shell."""

    workspace = _validate_existing_workspace(workspace_path)
    effective_platform = sys.platform if platform_name is None else platform_name
    if effective_platform != "darwin":
        return WorkspaceFinderResult(
            path=workspace,
            supported=False,
            revealed=False,
            reason="Reveal in Finder is available only on macOS.",
        )
    try:
        (opener or FinderOpener()).reveal(workspace)
    except FinderRevealFailedError as error:
        raise WorkspaceFinderError(
            "Finder could not reveal the validated workspace. Confirm it still exists and allow "
            "Image23MF Studio access in System Settings > Privacy & Security > Files and Folders."
        ) from error
    return WorkspaceFinderResult(path=workspace, supported=True, revealed=True)


def packaged_app_uses_workspace(
    workspace_path: Path,
    *,
    paths: Optional[MacOSPaths] = None,
) -> bool:
    """Return whether the packaged app's held runtime lock names this workspace.

    A held lock with unreadable or incomplete state is treated as unsafe rather than guessing that
    replacement is allowed. A stale state file without a held lock is harmless.
    """

    try:
        import fcntl
    except ImportError:
        # The supported packaged application is macOS-only. Other platforms have no launcher
        # runtime lock to inspect and must not fail merely by importing the recovery CLI.
        return False
    target = _absolute_without_following_leaf(workspace_path)
    runtime_paths = paths or MacOSPaths.for_home()
    lock_path = runtime_paths.runtime / "server.lock"
    try:
        stream = lock_path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _permission_or_bundle_error(
            "verify that the workspace is not active", lock_path, error
        ) from error
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                payload = json.loads(
                    (runtime_paths.runtime / "server.json").read_text(encoding="utf-8")
                )
                running_workspace = payload.get("workspace")
                if not isinstance(running_workspace, str) or not running_workspace.strip():
                    raise ValueError("running workspace is missing")
                return Path(running_workspace).expanduser().resolve() == target
            except (OSError, ValueError) as error:
                raise WorkspaceBundleError(
                    "A running Image23MF Studio instance holds the application lock, but its "
                    "workspace state cannot be verified. Nothing was changed. Quit the app "
                    "completely before restoring any workspace."
                ) from error
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
    finally:
        stream.close()


def _validate_existing_workspace(workspace_path: Path) -> Path:
    workspace = _absolute_without_following_leaf(workspace_path)
    if workspace_path.expanduser().is_symlink():
        raise WorkspaceBundleError("workspace root cannot be a symbolic link")
    try:
        if not workspace.is_dir():
            raise WorkspaceBundleError(f"workspace directory does not exist: {workspace}")
        database = workspace / DATABASE_NAME
        if not database.is_file() or database.is_symlink():
            raise WorkspaceBundleError(f"workspace database is missing or unsafe: {database}")
        connection = _open_snapshot(database)
        try:
            schema = _database_schema_version(connection)
            if schema > CURRENT_DATABASE_VERSION:
                raise WorkspaceBundleVersionError(
                    f"workspace database schema {schema} is newer than supported schema "
                    f"{CURRENT_DATABASE_VERSION}"
                )
        finally:
            connection.close()
    except WorkspaceBundleError:
        raise
    except OSError as error:
        raise _permission_or_bundle_error("validate the workspace", workspace, error) from error
    return workspace


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        target_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(target_connection)
            # A backup copies the source's WAL journal-mode header, but not a live -shm file.
            # Normalize the standalone snapshot so read-only preflight never needs sidecars.
            target_connection.execute("PRAGMA journal_mode = DELETE")
        finally:
            target_connection.close()
            source_connection.close()
        destination.chmod(0o600)
        connection = _open_snapshot(destination)
        connection.close()
    except sqlite3.Error as error:
        destination.unlink(missing_ok=True)
        raise WorkspaceBundleIntegrityError(
            "workspace database could not be snapshotted consistently"
        ) from error


def _open_snapshot(path: Path) -> sqlite3.Connection:
    connection = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise WorkspaceBundleIntegrityError(
                f"workspace database failed SQLite quick_check: {result}"
            )
        return connection
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise WorkspaceBundleIntegrityError(
            "workspace backup database is unreadable or corrupt"
        ) from error
    except Exception:
        if connection is not None:
            connection.close()
        raise


def _database_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.Error as error:
        raise WorkspaceBundleIntegrityError(
            "workspace database has no readable migration history"
        ) from error
    return int(row[0] or 0)


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    if table not in {"projects", "assets", "artifacts"}:
        raise ValueError("unsupported workspace count table")
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
    except sqlite3.Error as error:
        raise WorkspaceBundleIntegrityError(
            f"workspace database table is unreadable: {table}"
        ) from error


def _referenced_content(connection: sqlite3.Connection) -> tuple[tuple[str, str, int], ...]:
    queries = [
        "SELECT relative_path, sha256, byte_size FROM assets",
        "SELECT relative_path, sha256, byte_size FROM artifacts",
    ]
    for table in (
        "calibration_artifact_members",
        "calibration_run_members",
        "calibration_run_draft_members",
    ):
        if _table_exists(connection, table):
            queries.append(f"SELECT relative_path, sha256, byte_size FROM {table}")
    if _table_exists(connection, "calibration_runs"):
        queries.append(
            "SELECT source_bundle_relative_path, source_bundle_sha256, "
            "source_bundle_byte_size FROM calibration_runs"
        )
    try:
        rows = connection.execute(
            " UNION ALL ".join(queries) + " ORDER BY relative_path"
        ).fetchall()
    except sqlite3.Error as error:
        raise WorkspaceBundleIntegrityError(
            "workspace database content references are unreadable"
        ) from error
    return tuple((str(row[0]), str(row[1]), int(row[2])) for row in rows)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _walk_workspace_files(workspace: Path) -> Iterator[tuple[str, Path]]:
    try:
        for root, directories, filenames in os.walk(workspace, topdown=True, followlinks=False):
            root_path = Path(root)
            relative_root = root_path.relative_to(workspace)
            kept_directories = []
            for name in sorted(directories):
                path = root_path / name
                relative = (relative_root / name).as_posix()
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise WorkspaceBundleIntegrityError(
                        f"workspace contains a symbolic-link directory: {relative}"
                    )
                if not stat.S_ISDIR(mode):
                    raise WorkspaceBundleIntegrityError(
                        f"workspace contains a special directory entry: {relative}"
                    )
                if relative_root == Path(".") and name in EXCLUDED_ROOT_DIRECTORIES:
                    continue
                kept_directories.append(name)
            directories[:] = kept_directories
            for name in sorted(filenames):
                path = root_path / name
                relative_path = relative_root / name
                relative = relative_path.as_posix()
                if relative_root == Path(".") and (
                    name == DATABASE_NAME or name in EXCLUDED_ROOT_FILES
                ):
                    continue
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise WorkspaceBundleIntegrityError(
                        f"workspace contains a symbolic-link file: {relative}"
                    )
                if not stat.S_ISREG(mode):
                    raise WorkspaceBundleIntegrityError(
                        f"workspace contains a special file: {relative}"
                    )
                _portable_path(relative)
                yield relative, path
    except OSError as error:
        raise _permission_or_bundle_error("read the source workspace", workspace, error) from error


def _validated_infos(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBER_COUNT + 1:
        raise WorkspaceBundleIntegrityError("workspace backup has too many members")
    by_name = {}
    total = 0
    for info in infos:
        _portable_path(info.filename)
        if info.filename in by_name:
            raise WorkspaceBundleIntegrityError(
                f"workspace backup has a duplicate member: {info.filename}"
            )
        if info.is_dir():
            raise WorkspaceBundleIntegrityError("workspace backup cannot contain directory entries")
        if info.flag_bits & 0x1:
            raise WorkspaceBundleIntegrityError(
                "encrypted workspace backup members are unsupported"
            )
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and not stat.S_ISREG(unix_mode):
            raise WorkspaceBundleIntegrityError(
                f"workspace backup has a symlink or special member: {info.filename}"
            )
        if info.file_size > MAX_MEMBER_BYTES:
            raise WorkspaceBundleIntegrityError(
                f"workspace backup member exceeds the size limit: {info.filename}"
            )
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES + MAX_MANIFEST_BYTES:
            raise WorkspaceBundleIntegrityError("workspace backup expansion exceeds the size limit")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO
        ):
            raise WorkspaceBundleIntegrityError(
                f"workspace backup member has a suspicious compression ratio: {info.filename}"
            )
        by_name[info.filename] = info
    return by_name


def _write_zip_bytes(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    archive.writestr(_zip_info(name), payload)


def _write_zip_file_verified(
    archive: zipfile.ZipFile,
    name: str,
    source: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_stream, archive.open(_zip_info(name), "w") as output_stream:
        for chunk in iter(lambda: input_stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
            output_stream.write(chunk)
    if digest.hexdigest() != expected_sha256 or size != expected_size:
        raise WorkspaceBundleIntegrityError(
            f"workspace file changed while backup was being written: {source}"
        )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _hash_file(path: Path) -> tuple[str, int]:
    try:
        with path.open("rb") as source:
            return _hash_stream(source)
    except OSError as error:
        raise _permission_or_bundle_error("read a workspace file", path, error) from error


def _hash_stream(source) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _copy_and_hash(source, destination: Path) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise _permission_or_bundle_error(
            "write staged restore data", destination, error
        ) from error
    return digest.hexdigest(), size


def _ensure_stage_parent(stage: Path, parent: Path) -> None:
    if not _is_within(parent, stage):
        raise WorkspaceBundleIntegrityError("staged restore path escaped its workspace")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = parent
    while current != stage:
        if current.is_symlink():
            raise WorkspaceBundleIntegrityError("staged restore encountered a symbolic link")
        current = current.parent


def _target_state(target: Path) -> Literal["missing", "empty", "populated"]:
    if target.is_symlink():
        raise WorkspaceBundleError("restore target cannot be a symbolic link")
    if not target.exists():
        return "missing"
    if not target.is_dir():
        raise WorkspaceBundleError("restore target exists but is not a directory")
    try:
        return "populated" if next(target.iterdir(), None) is not None else "empty"
    except OSError as error:
        raise _permission_or_bundle_error("inspect the restore target", target, error) from error


def _workspace_size(workspace: Path) -> int:
    try:
        return sum(path.stat().st_size for _, path in _walk_workspace_files(workspace)) + (
            (workspace / DATABASE_NAME).stat().st_size
            if (workspace / DATABASE_NAME).is_file()
            else 0
        )
    except OSError as error:
        raise _permission_or_bundle_error(
            "measure the populated restore target", workspace, error
        ) from error


def _validate_target_path(path: Path) -> Path:
    if path.is_symlink():
        raise WorkspaceBundleError("restore target cannot be a symbolic link")
    target = _absolute_without_following_leaf(path)
    if target == Path(target.anchor) or target == Path.home().resolve():
        raise WorkspaceBundleError("restore target must be a dedicated workspace directory")
    return target


def _portable_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise WorkspaceBundleIntegrityError("workspace backup contains an unsafe member path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix():
        raise WorkspaceBundleIntegrityError("workspace backup contains an unsafe member path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceBundleIntegrityError("workspace backup contains an unsafe member path")
    return path


def _absolute_without_following_leaf(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded.parent.resolve() / expanded.name


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _nearest_existing_directory(path: Path) -> Path:
    current = path
    while not current.exists():
        if current == current.parent:
            break
        current = current.parent
    if not current.is_dir():
        raise WorkspaceBundleError("restore target has no usable parent directory")
    return current


def _ensure_directory(path: Path, action: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise _permission_or_bundle_error(action, path, error) from error


def _replace_file(source: Path, destination: Path, replace_fn) -> None:
    try:
        replace_fn(source, destination)
        destination.chmod(0o600)
    except OSError as error:
        raise _permission_or_bundle_error(
            "publish the workspace backup", destination, error
        ) from error


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise _permission_or_bundle_error("write the restore report", path, error) from error


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _permission_or_bundle_error(action: str, path: Path, error: OSError) -> WorkspaceBundleError:
    if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return WorkspacePermissionError(action, path, error)
    return WorkspaceBundleError(f"Could not {action} '{path}': {error}")


def _filename_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
