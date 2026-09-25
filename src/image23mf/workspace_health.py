"""Workspace consistency diagnostics and race-safe garbage collection."""

import hashlib
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from image23mf.storage import CURRENT_DATABASE_VERSION, ContentAddressedStore, StoredBlob
from image23mf.storage.repositories import canonical_json

CHUNK_SIZE = 1024 * 1024
DEFAULT_MINIMUM_AGE_SECONDS = 24 * 60 * 60
MAX_MINIMUM_AGE_SECONDS = 365 * 24 * 60 * 60
SCANNED_NAMESPACES = ("assets", "artifacts", "cache", "temp")


class HealthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class WorkspaceStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


class HealthIssue(HealthModel):
    code: str
    severity: Severity
    message: str
    advice: str
    relative_path: Optional[str] = None
    record_type: Optional[str] = None
    record_id: Optional[str] = None


class StorageBucket(HealthModel):
    name: str
    file_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)


class MigrationHealth(HealthModel):
    database_version: int = Field(ge=0)
    application_version: int = Field(ge=0)
    current: bool
    applied: tuple[int, ...]


class ToolHealth(HealthModel):
    id: str
    name: str
    available: bool
    compatible: bool
    version: Optional[str] = None
    path: Optional[str] = None
    purpose: str
    advice: str


class CacheHealth(HealthModel):
    file_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)
    disposable: bool = True
    advice: str


class WorkspaceHealthReport(HealthModel):
    generated_at: str
    status: WorkspaceStatus
    database_integrity: bool
    foreign_key_integrity: bool
    migration: MigrationHealth
    buckets: tuple[StorageBucket, ...]
    disk_total_bytes: int
    disk_free_bytes: int
    disk_free_ratio: float = Field(ge=0, le=1)
    cache: CacheHealth
    tools: tuple[ToolHealth, ...]
    referenced_file_count: int
    orphan_file_count: int
    cache_file_count: int
    stale_temp_file_count: int
    issues: tuple[HealthIssue, ...]


class GCCandidate(HealthModel):
    relative_path: str
    namespace: str
    reason: str
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mtime_ns: int = Field(ge=0)


class GCPlan(HealthModel):
    generated_at: str
    minimum_age_seconds: int = Field(ge=0)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_count: int = Field(ge=0)
    reclaimable_bytes: int = Field(ge=0)
    candidates: tuple[GCCandidate, ...]


class GCExecution(HealthModel):
    applied: bool
    plan_sha256: str
    eligible_count: int = Field(ge=0)
    deleted_count: int = Field(ge=0)
    reclaimed_bytes: int = Field(ge=0)
    skipped: tuple[HealthIssue, ...]


class GCApplyRequest(HealthModel):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    minimum_age_seconds: int = Field(
        default=DEFAULT_MINIMUM_AGE_SECONDS,
        ge=0,
        le=MAX_MINIMUM_AGE_SECONDS,
    )


class StaleGCPlanError(RuntimeError):
    pass


class InvalidGCPlanError(RuntimeError):
    pass


class WorkspaceHealthService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        blob_store: ContentAddressedStore,
    ) -> None:
        self.connection = connection
        self.blob_store = blob_store

    def inspect(
        self,
        *,
        stale_temp_age_seconds: int = DEFAULT_MINIMUM_AGE_SECONDS,
        tools: tuple[dict[str, object], ...] = (),
    ) -> WorkspaceHealthReport:
        now = time.time()
        issues = []
        integrity_rows = [row[0] for row in self.connection.execute("PRAGMA integrity_check")]
        database_integrity = integrity_rows == ["ok"]
        if not database_integrity:
            issues.append(
                HealthIssue(
                    code="database_integrity_failed",
                    severity=Severity.ERROR,
                    message="SQLite integrity_check reported structural database errors.",
                    advice=(
                        "Stop editing, preserve the workspace, and restore the latest "
                        "verified bundle."
                    ),
                )
            )
        foreign_rows = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        foreign_key_integrity = not foreign_rows
        if not foreign_key_integrity:
            issues.append(
                HealthIssue(
                    code="foreign_key_integrity_failed",
                    severity=Severity.ERROR,
                    message=f"SQLite reported {len(foreign_rows)} broken relationship(s).",
                    advice=(
                        "Do not run garbage collection; export diagnostics and restore a "
                        "verified bundle."
                    ),
                )
            )

        applied = tuple(
            int(row["version"])
            for row in self.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        database_version = max(applied, default=0)
        migration = MigrationHealth(
            database_version=database_version,
            application_version=CURRENT_DATABASE_VERSION,
            current=database_version == CURRENT_DATABASE_VERSION,
            applied=applied,
        )
        if not migration.current:
            severity = (
                Severity.ERROR if database_version > CURRENT_DATABASE_VERSION else Severity.WARNING
            )
            issues.append(
                HealthIssue(
                    code="migration_state_mismatch",
                    severity=severity,
                    message=(
                        f"Workspace schema is {database_version}; this application expects "
                        f"{CURRENT_DATABASE_VERSION}."
                    ),
                    advice=(
                        "Open the workspace with the application version that created it."
                        if database_version > CURRENT_DATABASE_VERSION
                        else "Restart the application to complete pending migrations."
                    ),
                )
            )

        referenced = self._referenced_blobs()
        actual, filesystem_issues = self._actual_files()
        issues.extend(filesystem_issues)
        missing = set(referenced) - set(actual)
        for relative_path in sorted(missing):
            record_type, record_id, _ = referenced[relative_path]
            is_critical_source = record_type in {"asset", "calibration_evidence"}
            issues.append(
                HealthIssue(
                    code="missing_referenced_blob",
                    severity=Severity.ERROR if is_critical_source else Severity.WARNING,
                    message=f"Referenced {record_type} bytes are missing.",
                    advice=(
                        "Restore a verified workspace/evidence bundle or re-import the source."
                        if is_critical_source
                        else "Re-run the producing job or restore an artifact-inclusive bundle."
                    ),
                    relative_path=relative_path,
                    record_type=record_type,
                    record_id=record_id,
                )
            )
        for relative_path in sorted(set(referenced) & set(actual)):
            record_type, record_id, blob = referenced[relative_path]
            path = actual[relative_path]
            if not _matches_blob(path, blob):
                issues.append(
                    HealthIssue(
                        code="corrupt_referenced_blob",
                        severity=Severity.ERROR,
                        message=(
                            f"Referenced {record_type} bytes fail size or SHA-256 verification."
                        ),
                        advice=(
                            "Quarantine the workspace and restore this record from a "
                            "verified bundle before printing or exporting."
                        ),
                        relative_path=relative_path,
                        record_type=record_type,
                        record_id=record_id,
                    )
                )

        managed_actual = {
            path for path in actual if PurePosixPath(path).parts[0] in {"assets", "artifacts"}
        }
        orphaned = managed_actual - set(referenced)
        for relative_path in sorted(orphaned):
            issues.append(
                HealthIssue(
                    code="orphan_blob",
                    severity=Severity.INFO,
                    message="Content-addressed bytes are not referenced by the database.",
                    advice=(
                        "Review a garbage-collection dry run; the file becomes eligible only after "
                        "the configured safety age."
                    ),
                    relative_path=relative_path,
                )
            )

        stale_temp = {
            relative
            for relative, path in actual.items()
            if relative.startswith("temp/") and now - path.stat().st_mtime >= stale_temp_age_seconds
        }
        if stale_temp:
            issues.append(
                HealthIssue(
                    code="stale_temporary_files",
                    severity=Severity.INFO,
                    message=f"{len(stale_temp)} temporary file(s) survived a previous operation.",
                    advice=(
                        "Review a garbage-collection dry run; startup never removes temporary "
                        "files automatically."
                    ),
                )
            )
        buckets = self._storage_buckets(actual)
        disk = shutil.disk_usage(self.blob_store.root)
        disk_free_ratio = 0.0 if disk.total <= 0 else disk.free / disk.total
        if disk.free < 512 * 1024 * 1024 or disk_free_ratio < 0.01:
            issues.append(
                HealthIssue(
                    code="disk_space_critical",
                    severity=Severity.ERROR,
                    message="Workspace disk capacity is critically low.",
                    advice="Free disk space before generating or exporting another model.",
                )
            )
        elif disk.free < 2 * 1024 * 1024 * 1024 or disk_free_ratio < 0.05:
            issues.append(
                HealthIssue(
                    code="disk_space_low",
                    severity=Severity.WARNING,
                    message="Workspace disk capacity is running low.",
                    advice="Review workspace storage and free space before a large export.",
                )
            )
        tool_health = tuple(_tool_health(item) for item in tools)
        for tool in tool_health:
            if not tool.available or not tool.compatible:
                issues.append(
                    HealthIssue(
                        code="tool_unavailable",
                        severity=Severity.INFO,
                        message=f"{tool.name} is not currently usable.",
                        advice=tool.advice,
                        record_type="tool",
                        record_id=tool.id,
                    )
                )
        cache_files = [path for relative, path in actual.items() if relative.startswith("cache/")]
        cache = CacheHealth(
            file_count=len(cache_files),
            byte_size=sum(path.stat().st_size for path in cache_files),
            advice=(
                "Cache entries are disposable derived bytes; review a cleanup dry run before "
                "removing them."
            ),
        )
        status = _status_for(issues)
        return WorkspaceHealthReport(
            generated_at=_utc_now(),
            status=status,
            database_integrity=database_integrity,
            foreign_key_integrity=foreign_key_integrity,
            migration=migration,
            buckets=buckets,
            disk_total_bytes=disk.total,
            disk_free_bytes=disk.free,
            disk_free_ratio=disk_free_ratio,
            cache=cache,
            tools=tool_health,
            referenced_file_count=len(referenced),
            orphan_file_count=len(orphaned),
            cache_file_count=cache.file_count,
            stale_temp_file_count=len(stale_temp),
            issues=tuple(issues),
        )

    def plan_garbage_collection(
        self, *, minimum_age_seconds: int = DEFAULT_MINIMUM_AGE_SECONDS
    ) -> GCPlan:
        if not 0 <= minimum_age_seconds <= MAX_MINIMUM_AGE_SECONDS:
            raise ValueError("minimum_age_seconds is outside the supported range")
        now = time.time()
        referenced = set(self._referenced_blobs())
        actual, _ = self._actual_files()
        candidates = []
        for relative_path, path in sorted(actual.items()):
            namespace = PurePosixPath(relative_path).parts[0]
            if namespace in {"assets", "artifacts"}:
                if relative_path in referenced:
                    continue
                reason = "orphan"
            elif namespace == "cache":
                reason = "cache"
            elif namespace == "temp":
                reason = "stale_temp"
            else:  # pragma: no cover - scanner only emits managed namespaces
                continue
            stat_result = path.stat()
            if now - stat_result.st_mtime < minimum_age_seconds:
                continue
            digest, size = _hash_file(path)
            candidates.append(
                GCCandidate(
                    relative_path=relative_path,
                    namespace=namespace,
                    reason=reason,
                    byte_size=size,
                    sha256=digest,
                    mtime_ns=stat_result.st_mtime_ns,
                )
            )
        candidate_tuple = tuple(candidates)
        fingerprint = _plan_fingerprint(minimum_age_seconds, candidate_tuple)
        return GCPlan(
            generated_at=_utc_now(),
            minimum_age_seconds=minimum_age_seconds,
            plan_sha256=fingerprint,
            candidate_count=len(candidate_tuple),
            reclaimable_bytes=sum(item.byte_size for item in candidate_tuple),
            candidates=candidate_tuple,
        )

    def execute_garbage_collection(self, plan: GCPlan, *, apply: bool = False) -> GCExecution:
        expected = _plan_fingerprint(plan.minimum_age_seconds, plan.candidates)
        if expected != plan.plan_sha256:
            raise InvalidGCPlanError("garbage-collection plan fingerprint is invalid")
        referenced = set(self._referenced_blobs())
        deleted_count = 0
        reclaimed = 0
        eligible = 0
        skipped = []
        for candidate in plan.candidates:
            if candidate.relative_path in referenced:
                skipped.append(
                    HealthIssue(
                        code="gc_candidate_became_referenced",
                        severity=Severity.WARNING,
                        message="Candidate is now referenced and was preserved.",
                        advice="Generate a fresh dry-run plan before retrying.",
                        relative_path=candidate.relative_path,
                    )
                )
                continue
            try:
                path = self._safe_managed_path(candidate.relative_path)
                stat_result = path.stat()
            except (FileNotFoundError, ValueError, OSError):
                skipped.append(
                    HealthIssue(
                        code="gc_candidate_missing_or_unsafe",
                        severity=Severity.INFO,
                        message="Candidate disappeared or no longer resolves safely.",
                        advice="Generate a fresh dry-run plan.",
                        relative_path=candidate.relative_path,
                    )
                )
                continue
            if path.is_symlink() or not path.is_file():
                skipped.append(
                    HealthIssue(
                        code="gc_candidate_not_regular_file",
                        severity=Severity.WARNING,
                        message="Candidate is no longer a regular file and was preserved.",
                        advice="Inspect the workspace path manually; do not follow symlinks.",
                        relative_path=candidate.relative_path,
                    )
                )
                continue
            digest, size = _hash_file(path)
            if (
                stat_result.st_mtime_ns != candidate.mtime_ns
                or size != candidate.byte_size
                or digest != candidate.sha256
            ):
                skipped.append(
                    HealthIssue(
                        code="gc_candidate_changed",
                        severity=Severity.WARNING,
                        message="Candidate changed after planning and was preserved.",
                        advice="Generate and review a fresh dry-run plan.",
                        relative_path=candidate.relative_path,
                    )
                )
                continue
            eligible += 1
            if apply:
                path.unlink()
                deleted_count += 1
                reclaimed += size
                _remove_empty_parents(path.parent, self.blob_store.root / candidate.namespace)
        return GCExecution(
            applied=apply,
            plan_sha256=plan.plan_sha256,
            eligible_count=eligible,
            deleted_count=deleted_count,
            reclaimed_bytes=reclaimed,
            skipped=tuple(skipped),
        )

    def apply_current_plan(self, *, plan_sha256: str, minimum_age_seconds: int) -> GCExecution:
        current = self.plan_garbage_collection(minimum_age_seconds=minimum_age_seconds)
        if current.plan_sha256 != plan_sha256:
            raise StaleGCPlanError("workspace changed after the reviewed garbage-collection plan")
        return self.execute_garbage_collection(current, apply=True)

    def _referenced_blobs(self) -> dict[str, tuple[str, str, StoredBlob]]:
        result = {}
        for row in self.connection.execute("SELECT * FROM assets ORDER BY id"):
            blob = StoredBlob(
                sha256=row["sha256"],
                relative_path=row["relative_path"],
                byte_size=row["byte_size"],
                media_type=row["media_type"],
                extension=row["extension"],
            )
            result[row["relative_path"]] = ("asset", row["id"], blob)
        for row in self.connection.execute("SELECT * FROM artifacts ORDER BY id"):
            extension = Path(row["relative_path"]).suffix.lower()
            blob = StoredBlob(
                sha256=row["sha256"],
                relative_path=row["relative_path"],
                byte_size=row["byte_size"],
                media_type=row["media_type"],
                extension=extension,
            )
            previous = result.get(row["relative_path"])
            if previous is not None and previous[2] != blob:
                # Preserve both records as a consistency issue through a synthetic impossible hash.
                blob = StoredBlob(
                    sha256="0" * 64,
                    relative_path=row["relative_path"],
                    byte_size=-1,
                    media_type=row["media_type"],
                    extension=extension,
                )
            result[row["relative_path"]] = ("artifact", row["id"], blob)
        for table in (
            "calibration_artifact_members",
            "calibration_run_members",
            "calibration_run_draft_members",
        ):
            for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY id"):  # noqa: S608
                blob = StoredBlob(
                    sha256=row["sha256"],
                    relative_path=row["relative_path"],
                    byte_size=row["byte_size"],
                    media_type=row["media_type"],
                    extension=row["extension"],
                )
                _record_reference(
                    result,
                    record_type="calibration_evidence",
                    record_id=row["id"],
                    blob=blob,
                )
        for row in self.connection.execute("SELECT * FROM calibration_runs ORDER BY id"):
            blob = StoredBlob(
                sha256=row["source_bundle_sha256"],
                relative_path=row["source_bundle_relative_path"],
                byte_size=row["source_bundle_byte_size"],
                media_type=row["source_bundle_media_type"],
                extension=row["source_bundle_extension"],
            )
            _record_reference(
                result,
                record_type="calibration_evidence",
                record_id=row["id"],
                blob=blob,
            )
        return result

    def _actual_files(self) -> tuple[dict[str, Path], list[HealthIssue]]:
        files = {}
        issues = []
        for namespace in SCANNED_NAMESPACES:
            root = self.blob_store.root / namespace
            if not root.exists():
                continue
            for path in root.rglob("*"):
                relative = path.relative_to(self.blob_store.root).as_posix()
                if path.is_symlink():
                    issues.append(
                        HealthIssue(
                            code="workspace_symlink",
                            severity=Severity.WARNING,
                            message="Managed workspace contains a symlink that was not followed.",
                            advice="Remove the symlink after confirming it is not needed.",
                            relative_path=relative,
                        )
                    )
                elif path.is_file():
                    files[relative] = path
        return files, issues

    def _storage_buckets(self, files: dict[str, Path]) -> tuple[StorageBucket, ...]:
        buckets = []
        for namespace in SCANNED_NAMESPACES:
            selected = [
                path for relative, path in files.items() if relative.startswith(f"{namespace}/")
            ]
            buckets.append(
                StorageBucket(
                    name=namespace,
                    file_count=len(selected),
                    byte_size=sum(path.stat().st_size for path in selected),
                )
            )
        database_files = [
            path
            for path in (
                self.blob_store.root / "image23mf.sqlite3",
                self.blob_store.root / "image23mf.sqlite3-wal",
                self.blob_store.root / "image23mf.sqlite3-shm",
            )
            if path.is_file()
        ]
        buckets.append(
            StorageBucket(
                name="database",
                file_count=len(database_files),
                byte_size=sum(path.stat().st_size for path in database_files),
            )
        )
        return tuple(buckets)

    def _safe_managed_path(self, relative_path: str) -> Path:
        if "\\" in relative_path or "\x00" in relative_path:
            raise ValueError("unsafe managed path")
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe managed path")
        if not relative.parts or relative.parts[0] not in SCANNED_NAMESPACES:
            raise ValueError("unmanaged namespace")
        path = self.blob_store.root / Path(*relative.parts)
        return path


def _record_reference(
    result: dict[str, tuple[str, str, StoredBlob]],
    *,
    record_type: str,
    record_id: str,
    blob: StoredBlob,
) -> None:
    previous = result.get(blob.relative_path)
    if previous is not None and previous[2] != blob:
        blob = StoredBlob(
            sha256="0" * 64,
            relative_path=blob.relative_path,
            byte_size=-1,
            media_type=blob.media_type,
            extension=blob.extension,
        )
    result[blob.relative_path] = (record_type, record_id, blob)


def _matches_blob(path: Path, blob: StoredBlob) -> bool:
    if blob.byte_size < 0 or path.stat().st_size != blob.byte_size:
        return False
    digest, _ = _hash_file(path)
    return digest == blob.sha256


def _tool_health(item: dict[str, object]) -> ToolHealth:
    available = bool(item.get("available"))
    compatible = bool(item.get("compatible"))
    reason = item.get("unavailable_reason")
    name = str(item.get("name") or item.get("id") or "External tool")
    return ToolHealth(
        id=str(item.get("id") or "unknown"),
        name=name,
        available=available,
        compatible=compatible,
        version=None if item.get("version") is None else str(item["version"]),
        path=None if item.get("path") is None else str(item["path"]),
        purpose=str(item.get("purpose") or "Optional external processing"),
        advice=(
            "No action is required."
            if available and compatible
            else str(reason or f"Install or configure a compatible {name} executable.")
        ),
    )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _plan_fingerprint(minimum_age_seconds: int, candidates: tuple[GCCandidate, ...]) -> str:
    payload = {
        "minimum_age_seconds": minimum_age_seconds,
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _status_for(issues: list[HealthIssue]) -> WorkspaceStatus:
    if any(issue.severity == Severity.ERROR for issue in issues):
        return WorkspaceStatus.CRITICAL
    if any(issue.severity == Severity.WARNING for issue in issues):
        return WorkspaceStatus.DEGRADED
    return WorkspaceStatus.HEALTHY


def _remove_empty_parents(directory: Path, stop: Path) -> None:
    stop = stop.resolve()
    current = directory.resolve()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
