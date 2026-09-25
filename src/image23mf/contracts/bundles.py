"""HTTP resources for portable project bundle import evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from image23mf.bundles import BundleRestoreResult


class ProjectBundleImportResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    source_project_id: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duplicate: bool
    imported_assets: int = Field(ge=0)
    imported_revisions: int = Field(ge=0)
    imported_artifacts: int = Field(ge=0)
    history_mode: Literal["full_history", "current_state_only"]
    history_import: Literal["full_history", "legacy_anchor", "none"]
    imported_history_nodes: int = Field(ge=0)
    abandoned_history_nodes: int = Field(ge=0)

    @classmethod
    def from_result(cls, result: BundleRestoreResult) -> ProjectBundleImportResource:
        return cls(**result.__dict__)
