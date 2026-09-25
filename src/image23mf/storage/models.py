"""Typed records at the SQLite/content-store boundary.

These records intentionally live in the storage package. Domain and processing code use
asset/revision identifiers and never need to know where a blob is stored on disk.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from image23mf.storage.blob_store import StoredBlob

JsonObject = Mapping[str, Any]


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    description: str
    active_revision_id: Optional[str]
    preferences: JsonObject
    created_at: str
    updated_at: str
    archived_at: Optional[str]


@dataclass(frozen=True)
class AssetRecord:
    id: str
    sha256: str
    media_type: str
    extension: str
    original_filename: str
    relative_path: str
    byte_size: int
    width_px: Optional[int]
    height_px: Optional[int]
    metadata: JsonObject
    created_at: str

    def stored_blob(self) -> StoredBlob:
        return StoredBlob(
            sha256=self.sha256,
            relative_path=self.relative_path,
            byte_size=self.byte_size,
            media_type=self.media_type,
            extension=self.extension,
        )


@dataclass(frozen=True)
class RevisionRecord:
    id: str
    project_id: str
    source_asset_id: str
    parent_revision_id: Optional[str]
    schema_version: int
    engine_version: str
    config: JsonObject
    config_sha256: str
    label: str
    notes: str
    published_at: str


@dataclass(frozen=True)
class RevisionPublicationRecord:
    revision_id: str
    source_draft_generation: int
    editor_sequence_sha256: str
    preview_status: str
    preview_job_id: Optional[str]
    preview_derivation_key: Optional[str]
    preview_reason: str
    artifact_manifest_sha256: Optional[str]
    artifact_count: int
    created_at: str


@dataclass(frozen=True)
class RevisionSummaryRecord:
    revision: RevisionRecord
    publication: Optional[RevisionPublicationRecord]
    operation_count: int
    artifact_count: int


@dataclass(frozen=True)
class RegionOperation:
    operation_type: str
    selection: JsonObject
    parameters: JsonObject = field(default_factory=dict)
    source: str = "manual"
    provenance: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class RegionOperationRecord:
    id: str
    revision_id: str
    sequence: int
    operation_type: str
    selection: JsonObject
    parameters: JsonObject
    source: str
    provenance: JsonObject
    created_at: str


@dataclass(frozen=True)
class DraftRecord:
    project_id: str
    base_revision_id: Optional[str]
    schema_version: int
    config: JsonObject
    config_sha256: str
    operations: tuple[RegionOperation, ...]
    generation: int
    updated_at: str
    history: Optional["DraftHistorySummary"] = None


@dataclass(frozen=True)
class DraftHistorySummary:
    lineage_id: str
    cursor_node_id: str
    tip_node_id: str
    cursor: int
    total: int
    limit: int
    can_undo: bool
    can_redo: bool
    undo_label: Optional[str]
    redo_label: Optional[str]
    state_sha256: str


@dataclass(frozen=True)
class DraftHistoryCommand:
    request_id: str
    schema_version: int
    command_type: str
    label: str
    before_state_sha256: str
    expected_cursor_node_id: str


@dataclass(frozen=True)
class FilamentRecord:
    id: str
    manufacturer: str
    family: str
    name: str
    hex_color: str
    material: str
    finish: str
    owned: bool
    metadata: JsonObject
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    revision_id: Optional[str]
    job_id: Optional[str]
    kind: str
    sha256: str
    derivation_key: str
    media_type: str
    relative_path: str
    byte_size: int
    metadata: JsonObject
    created_at: str


@dataclass(frozen=True)
class ArtifactPublication:
    kind: str
    derivation_key: str
    blob: StoredBlob
    metadata: JsonObject = field(default_factory=dict)
    job_id: Optional[str] = None


@dataclass(frozen=True)
class PublishedRevision:
    revision: RevisionRecord
    artifacts: tuple[ArtifactRecord, ...]
    publication: Optional[RevisionPublicationRecord] = None
    continuation_draft: Optional[DraftRecord] = None
    project: Optional[ProjectRecord] = None


@dataclass(frozen=True)
class BranchedRevision:
    project: ProjectRecord
    draft: DraftRecord
