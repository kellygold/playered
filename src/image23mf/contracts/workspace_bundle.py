"""Portable whole-workspace backup and disaster-recovery contracts."""

from __future__ import annotations

# ruff: noqa: UP045 -- Pydantic evaluates annotations on supported Python 3.9.
from pathlib import PurePosixPath
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkspaceBundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceBundleMember(WorkspaceBundleModel):
    archive_path: str
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    kind: Literal["database", "workspace-file"]

    @model_validator(mode="after")
    def paths_are_closed_and_portable(self) -> WorkspaceBundleMember:
        relative = _portable_path(self.relative_path)
        archive = _portable_path(self.archive_path)
        if archive != PurePosixPath("payload") / relative:
            raise ValueError("archive path must exactly mirror its workspace-relative path")
        if self.kind == "database" and relative != PurePosixPath("image23mf.sqlite3"):
            raise ValueError("database member must be image23mf.sqlite3")
        if self.kind == "workspace-file" and relative == PurePosixPath("image23mf.sqlite3"):
            raise ValueError("database member must use the database kind")
        return self


class WorkspaceBundleManifest(WorkspaceBundleModel):
    schema_version: int = Field(gt=0)
    application_version: str = Field(min_length=1, max_length=128)
    database_schema_version: int = Field(ge=0)
    created_at: str
    source_workspace_name: str = Field(min_length=1, max_length=255)
    members: tuple[WorkspaceBundleMember, ...]
    project_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    total_payload_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def members_are_closed(self) -> WorkspaceBundleManifest:
        archive_paths = {member.archive_path for member in self.members}
        relative_paths = {member.relative_path for member in self.members}
        if len(archive_paths) != len(self.members) or len(relative_paths) != len(self.members):
            raise ValueError("workspace bundle member paths must be unique")
        if sum(member.byte_size for member in self.members) != self.total_payload_bytes:
            raise ValueError("workspace bundle payload total does not match its members")
        databases = [member for member in self.members if member.kind == "database"]
        if len(databases) != 1:
            raise ValueError("workspace bundle must contain exactly one database snapshot")
        return self


class WorkspaceBundlePreflight(WorkspaceBundleModel):
    bundle_path: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_byte_size: int = Field(ge=0)
    source_workspace_name: str
    application_version: str
    database_schema_version: int = Field(ge=0)
    project_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    member_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    target_workspace: str
    target_state: Literal["missing", "empty", "populated"]
    conflict_choices: tuple[Literal["empty_only", "replace"], ...]
    default_choice: Literal["empty_only"] = "empty_only"
    required_free_bytes: int = Field(ge=0)
    available_free_bytes: int = Field(ge=0)
    warnings: tuple[str, ...] = ()


class WorkspaceRestoreReport(WorkspaceBundleModel):
    report_schema_version: Literal[1] = 1
    restored_at: str
    bundle_path: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_workspace: str
    original_target_state: Literal["missing", "empty", "populated"]
    conflict_choice: Literal["empty_only", "replace"]
    rollback_bundle: Optional[str]
    restored_projects: int = Field(ge=0)
    restored_assets: int = Field(ge=0)
    restored_artifacts: int = Field(ge=0)
    restored_members: int = Field(ge=0)
    restored_payload_bytes: int = Field(ge=0)
    database_schema_version: int = Field(ge=0)
    warnings: tuple[str, ...] = ()
    recovery_instructions: Optional[str] = None


def _portable_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("workspace bundle paths must be non-empty canonical POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix():
        raise ValueError("workspace bundle paths must be canonical relative POSIX paths")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("workspace bundle paths cannot traverse directories")
    return path
