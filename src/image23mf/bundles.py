"""Portable, checksummed project bundle export and restore."""

import copy
import hashlib
import json
import os
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf import __version__
from image23mf.contracts.editor import EditorCommandSequence
from image23mf.contracts.job import load_job_config
from image23mf.contracts.provenance import validate_operation_provenance
from image23mf.editor import EditorReplayError, load_persisted_commands
from image23mf.storage import ContentAddressedStore, DraftRepository, RegionOperation
from image23mf.storage.history import exact_state_sha256, operation_json
from image23mf.storage.repositories import (
    RecordNotFoundError,
    artifact_manifest_sha256,
    canonical_json,
    immediate_transaction,
    new_id,
)

BUNDLE_SCHEMA_VERSION = 3
MANIFEST_NAME = "manifest.json"
CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
MAX_MEMBER_COUNT = 10_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
ID_TABLES = frozenset(
    {
        "projects",
        "assets",
        "revisions",
        "artifacts",
        "filaments",
        "presets",
        "draft_history_lineages",
        "draft_history_nodes",
    }
)
HistoryMode = Literal["full_history", "current_state_only"]


class BundleError(RuntimeError):
    pass


class BundleIntegrityError(BundleError):
    pass


class UnsupportedBundleVersionError(BundleError):
    pass


class BundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleMember(BundleModel):
    archive_path: str
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    media_type: str
    extension: str

    @model_validator(mode="after")
    def paths_are_portable(self) -> "BundleMember":
        relative = _portable_path(self.relative_path)
        archive = _portable_path(self.archive_path)
        if relative.parts[0] not in {"assets", "artifacts"}:
            raise ValueError("bundle member namespace must be assets or artifacts")
        if archive != PurePosixPath("payload") / relative:
            raise ValueError("bundle archive path must mirror its relative payload path")
        if self.extension != Path(relative.name).suffix.lower():
            raise ValueError("bundle member extension does not match its path")
        return self


class BundleProject(BundleModel):
    id: str
    name: str
    description: str
    active_revision_id: Optional[str]
    preferences: dict[str, Any]
    created_at: str
    updated_at: str
    archived_at: Optional[str]


class BundleAsset(BundleModel):
    id: str
    sha256: str
    media_type: str
    extension: str
    original_filename: str
    relative_path: str
    byte_size: int
    width_px: Optional[int]
    height_px: Optional[int]
    metadata: dict[str, Any]
    created_at: str


class BundleRevision(BundleModel):
    id: str
    source_asset_id: str
    parent_revision_id: Optional[str]
    schema_version: int
    engine_version: str
    config: dict[str, Any]
    config_sha256: str
    label: str
    notes: str
    published_at: str


class BundleArtifact(BundleModel):
    id: str
    revision_id: str
    kind: str
    sha256: str
    derivation_key: str
    media_type: str
    relative_path: str
    byte_size: int
    metadata: dict[str, Any]
    created_at: str


class BundleOperation(BundleModel):
    id: str
    revision_id: str
    sequence: int
    operation_type: str
    selection: dict[str, Any]
    parameters: dict[str, Any]
    source: str
    provenance: dict[str, Any]
    created_at: str

    @model_validator(mode="after")
    def durable_payload_excludes_secrets(self) -> "BundleOperation":
        validate_operation_provenance(
            selection=self.selection,
            parameters=self.parameters,
            provenance=self.provenance,
        )
        return self


class BundleRevisionPublication(BundleModel):
    revision_id: str
    source_draft_generation: int = Field(gt=0)
    editor_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_status: Literal["fresh", "missing", "stale"]
    preview_job_id: Optional[str]
    preview_derivation_key: Optional[str]
    preview_reason: str
    artifact_manifest_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_count: int = Field(ge=0)
    created_at: str


class BundleFilament(BundleModel):
    id: str
    manufacturer: str
    family: str
    name: str
    hex_color: str
    material: str
    finish: str
    owned: bool
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class BundlePreset(BundleModel):
    id: str
    scope: str
    name: str
    schema_version: int
    settings: dict[str, Any]
    built_in: bool
    created_at: str
    updated_at: str


class BundleDraft(BundleModel):
    base_revision_id: Optional[str]
    schema_version: int
    config: dict[str, Any]
    operations: tuple[dict[str, Any], ...]
    generation: int
    config_sha256: Optional[str]
    updated_at: str

    @model_validator(mode="after")
    def operation_payloads_exclude_secrets(self) -> "BundleDraft":
        _validate_embedded_operation_payloads(self.operations)
        return self


class BundleHistoryLineage(BundleModel):
    id: str
    base_revision_id: Optional[str]
    source_asset_id: str
    created_at: str
    closed_at: Optional[str]


class BundleHistoryState(BundleModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_id: str
    schema_version: int = Field(gt=0)
    base_revision_id: Optional[str]
    source_asset_id: str
    config: dict[str, Any]
    operations: tuple[dict[str, Any], ...]
    byte_size: int = Field(ge=0)
    created_at: str

    @model_validator(mode="after")
    def operation_payloads_exclude_secrets(self) -> "BundleHistoryState":
        _validate_embedded_operation_payloads(self.operations)
        return self


class BundleHistoryNode(BundleModel):
    id: str
    lineage_id: str
    parent_node_id: Optional[str]
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    depth: int = Field(ge=0)
    request_id: Optional[str]
    schema_version: int = Field(gt=0)
    command_type: str
    label: str
    checkpoint_revision_id: Optional[str]
    created_at: str


class BundleHistoryHead(BundleModel):
    lineage_id: str
    checkpoint_node_id: str
    undo_floor_node_id: str
    cursor_node_id: str
    tip_node_id: str
    undo_limit: int = Field(gt=0)
    updated_at: str


class BundleManifest(BundleModel):
    schema_version: int = Field(gt=0)
    application_version: str
    project: BundleProject
    members: tuple[BundleMember, ...]
    assets: tuple[BundleAsset, ...]
    revisions: tuple[BundleRevision, ...]
    artifacts: tuple[BundleArtifact, ...]
    operations: tuple[BundleOperation, ...]
    filaments: tuple[BundleFilament, ...]
    presets: tuple[BundlePreset, ...]
    draft: Optional[BundleDraft]
    revision_publications: tuple[BundleRevisionPublication, ...] = ()
    history_mode: HistoryMode = "current_state_only"
    history_lineages: tuple[BundleHistoryLineage, ...] = ()
    history_states: tuple[BundleHistoryState, ...] = ()
    history_nodes: tuple[BundleHistoryNode, ...] = ()
    history_head: Optional[BundleHistoryHead] = None

    @model_validator(mode="after")
    def references_are_closed(self) -> "BundleManifest":
        asset_ids = {item.id for item in self.assets}
        revision_ids = {item.id for item in self.revisions}
        filament_ids = {item.id for item in self.filaments}
        artifact_ids = {item.id for item in self.artifacts}
        operation_ids = {item.id for item in self.operations}
        preset_ids = {item.id for item in self.presets}
        publication_revision_ids = {item.revision_id for item in self.revision_publications}
        member_paths = {item.relative_path for item in self.members}
        members = {item.relative_path: item for item in self.members}
        if len(member_paths) != len(self.members):
            raise ValueError("bundle member paths must be unique")
        if (
            len(asset_ids) != len(self.assets)
            or len(revision_ids) != len(self.revisions)
            or len(filament_ids) != len(self.filaments)
            or len(artifact_ids) != len(self.artifacts)
            or len(operation_ids) != len(self.operations)
            or len(preset_ids) != len(self.presets)
            or len(publication_revision_ids) != len(self.revision_publications)
        ):
            raise ValueError("bundle record IDs must be unique")
        operation_positions = {(item.revision_id, item.sequence) for item in self.operations}
        if len(operation_positions) != len(self.operations):
            raise ValueError("bundle operation sequence positions must be unique")
        for record in (*self.assets, *self.artifacts):
            member = members.get(record.relative_path)
            if member is None:
                raise ValueError("record references an absent payload member")
            if (
                member.sha256 != record.sha256
                or member.byte_size != record.byte_size
                or member.media_type != record.media_type
            ):
                raise ValueError("record payload identity does not match its member")
        for revision in self.revisions:
            if revision.source_asset_id not in asset_ids:
                raise ValueError("revision references an absent source asset")
            if revision.parent_revision_id and revision.parent_revision_id not in revision_ids:
                raise ValueError("revision references an absent parent")
            config = load_job_config(revision.config)
            if config.source_asset_id != revision.source_asset_id:
                raise ValueError("revision config and source asset disagree")
            if config.fingerprint() != revision.config_sha256:
                raise ValueError("revision config fingerprint is invalid")
            for color in config.palette.colors:
                if color.filament_id is not None and color.filament_id not in filament_ids:
                    raise ValueError("revision references an absent filament")
        if self.project.active_revision_id and self.project.active_revision_id not in revision_ids:
            raise ValueError("project active revision is absent")
        if any(item.revision_id not in revision_ids for item in self.artifacts):
            raise ValueError("artifact references an absent revision")
        if any(item.revision_id not in revision_ids for item in self.operations):
            raise ValueError("operation references an absent revision")
        artifacts_by_revision = {
            revision_id: tuple(item for item in self.artifacts if item.revision_id == revision_id)
            for revision_id in revision_ids
        }
        for publication in self.revision_publications:
            if publication.revision_id not in revision_ids:
                raise ValueError("publication references an absent revision")
            revision = next(item for item in self.revisions if item.id == publication.revision_id)
            operations = tuple(
                sorted(
                    (
                        item
                        for item in self.operations
                        if item.revision_id == publication.revision_id
                    ),
                    key=lambda item: item.sequence,
                )
            )
            try:
                expected_sequence = _bundle_editor_sequence_sha256(
                    revision.config_sha256, operations
                )
            except EditorReplayError as error:
                raise ValueError("publication editor sequence cannot be replayed") from error
            if publication.editor_sequence_sha256 != expected_sequence:
                raise ValueError("publication editor sequence fingerprint is invalid")
            artifacts = artifacts_by_revision[publication.revision_id]
            if publication.preview_status == "fresh":
                if publication.artifact_count != len(artifacts):
                    raise ValueError("fresh publication artifact count is incomplete")
                if publication.artifact_manifest_sha256 != artifact_manifest_sha256(artifacts):
                    raise ValueError("fresh publication artifact manifest is invalid")
            elif (
                publication.artifact_count != 0 or publication.artifact_manifest_sha256 is not None
            ):
                raise ValueError("non-fresh publication cannot claim artifact evidence")
        if self.draft and self.draft.base_revision_id not in revision_ids | {None}:
            raise ValueError("draft references an absent base revision")
        if self.draft:
            draft_config = load_job_config(self.draft.config)
            if (
                self.draft.config_sha256 is not None
                and draft_config.fingerprint() != self.draft.config_sha256
            ):
                raise ValueError("draft config fingerprint is invalid")
            for color in draft_config.palette.colors:
                if color.filament_id is not None and color.filament_id not in filament_ids:
                    raise ValueError("draft references an absent filament")
        self._validate_history(asset_ids, revision_ids, filament_ids)
        return self

    def _validate_history(
        self,
        asset_ids: set[str],
        revision_ids: set[str],
        filament_ids: set[str],
    ) -> None:
        if self.history_mode == "current_state_only":
            has_history = (
                self.history_lineages
                or self.history_states
                or self.history_nodes
                or self.history_head
            )
            if has_history:
                raise ValueError("current-state-only bundle cannot claim draft history")
            return
        if self.schema_version < 3:
            raise ValueError("full draft history requires bundle schema 3")

        lineages = {item.id: item for item in self.history_lineages}
        states = {item.sha256: item for item in self.history_states}
        nodes = {item.id: item for item in self.history_nodes}
        request_ids = [
            item.request_id for item in self.history_nodes if item.request_id is not None
        ]
        if len(lineages) != len(self.history_lineages):
            raise ValueError("draft history lineage IDs must be unique")
        if len(states) != len(self.history_states):
            raise ValueError("draft history state hashes must be unique")
        if len(nodes) != len(self.history_nodes):
            raise ValueError("draft history node IDs must be unique")
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("draft history request IDs must be unique")

        for lineage in self.history_lineages:
            if lineage.source_asset_id not in asset_ids:
                raise ValueError("draft history lineage references an absent source asset")
            if lineage.base_revision_id not in revision_ids | {None}:
                raise ValueError("draft history lineage references an absent base revision")
        for state in self.history_states:
            lineage = lineages.get(state.lineage_id)
            if lineage is None:
                raise ValueError("draft history state references an absent lineage")
            if (
                state.source_asset_id != lineage.source_asset_id
                or state.base_revision_id != lineage.base_revision_id
            ):
                raise ValueError("draft history state identity disagrees with its lineage")
            config = load_job_config(state.config)
            if config.source_asset_id != state.source_asset_id:
                raise ValueError("draft history state config and source asset disagree")
            for color in config.palette.colors:
                if color.filament_id is not None and color.filament_id not in filament_ids:
                    raise ValueError("draft history state references an absent filament")
            operations = _history_operations(state.operations)
            expected_byte_size = len(canonical_json(state.config).encode("utf-8")) + len(
                canonical_json([operation_json(item) for item in operations]).encode("utf-8")
            )
            if expected_byte_size != state.byte_size:
                raise ValueError("draft history state byte size is invalid")
            expected = exact_state_sha256(
                lineage_id=state.lineage_id,
                schema_version=state.schema_version,
                base_revision_id=state.base_revision_id,
                source_asset_id=state.source_asset_id,
                config=state.config,
                operations=operations,
            )
            if expected != state.sha256:
                raise ValueError("draft history state fingerprint is invalid")

        roots_by_lineage: dict[str, int] = {lineage_id: 0 for lineage_id in lineages}
        referenced_states = set()
        for node in self.history_nodes:
            if node.lineage_id not in lineages:
                raise ValueError("draft history node references an absent lineage")
            state = states.get(node.state_sha256)
            if state is None or state.lineage_id != node.lineage_id:
                raise ValueError("draft history node references an absent or cross-lineage state")
            referenced_states.add(node.state_sha256)
            if node.checkpoint_revision_id not in revision_ids | {None}:
                raise ValueError("draft history node references an absent checkpoint revision")
            if node.parent_node_id is None:
                if node.depth != 0:
                    raise ValueError("draft history root depth must be zero")
                roots_by_lineage[node.lineage_id] += 1
                continue
            parent = nodes.get(node.parent_node_id)
            if (
                parent is None
                or parent.lineage_id != node.lineage_id
                or parent.depth + 1 != node.depth
            ):
                raise ValueError("draft history parent must precede its child in one lineage")
        if set(states) != referenced_states:
            raise ValueError("draft history contains an unreferenced state")
        if any(count != 1 for count in roots_by_lineage.values()):
            raise ValueError("each draft history lineage must have exactly one root")

        if self.draft is not None and self.history_head is None:
            raise ValueError("full-history draft bundle requires an active history head")
        if self.history_head is None:
            return
        head = self.history_head
        if head.lineage_id not in lineages:
            raise ValueError("draft history head references an absent lineage")
        checkpoint = nodes.get(head.checkpoint_node_id)
        floor = nodes.get(head.undo_floor_node_id)
        cursor = nodes.get(head.cursor_node_id)
        tip = nodes.get(head.tip_node_id)
        if any(
            item is None or item.lineage_id != head.lineage_id
            for item in (
                checkpoint,
                floor,
                cursor,
                tip,
            )
        ):
            raise ValueError("draft history head references an absent or cross-lineage node")
        if not (
            _history_is_ancestor(nodes, head.checkpoint_node_id, head.undo_floor_node_id)
            and _history_is_ancestor(nodes, head.undo_floor_node_id, head.cursor_node_id)
            and _history_is_ancestor(nodes, head.cursor_node_id, head.tip_node_id)
        ):
            raise ValueError("draft history head nodes are not ordered on the active tip path")
        if self.draft is None:
            raise ValueError("draft history head cannot exist without a materialized draft")
        cursor_state = states[cursor.state_sha256]
        if (
            self.draft.schema_version != cursor_state.schema_version
            or self.draft.base_revision_id != cursor_state.base_revision_id
            or self.draft.config != cursor_state.config
            or tuple(self.draft.operations) != tuple(cursor_state.operations)
        ):
            raise ValueError("materialized draft does not match the history cursor state")


@dataclass(frozen=True)
class BundleExportResult:
    path: Path
    sha256: str
    byte_size: int
    manifest: BundleManifest


@dataclass(frozen=True)
class BundleRestoreResult:
    project_id: str
    source_project_id: str
    bundle_sha256: str
    duplicate: bool
    imported_assets: int
    imported_revisions: int
    imported_artifacts: int
    history_mode: HistoryMode
    history_import: Literal["full_history", "legacy_anchor", "none"]
    imported_history_nodes: int
    abandoned_history_nodes: int


class ProjectBundleService:
    def __init__(self, connection, blob_store: ContentAddressedStore) -> None:
        self.connection = connection
        self.blob_store = blob_store

    def export(
        self,
        project_id: str,
        destination: Path,
        *,
        artifact_ids: Optional[Sequence[str]] = None,
        history_mode: HistoryMode = "full_history",
    ) -> BundleExportResult:
        manifest = self._build_manifest(
            project_id,
            artifact_ids=artifact_ids,
            history_mode=history_mode,
        )
        member_by_path = {item.relative_path: item for item in manifest.members}
        for member in manifest.members:
            self._verify_local_member(member)

        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                manifest_bytes = manifest.model_dump_json(
                    indent=None, by_alias=True, exclude_none=False
                ).encode("utf-8")
                # Re-canonicalize because Pydantic preserves field order rather than key order.
                manifest_bytes = canonical_json(json.loads(manifest_bytes)).encode("utf-8")
                _write_zip_bytes(archive, MANIFEST_NAME, manifest_bytes)
                for relative_path in sorted(member_by_path):
                    member = member_by_path[relative_path]
                    _write_zip_file(
                        archive,
                        member.archive_path,
                        self.blob_store.path_for(member.relative_path),
                    )
            with temporary_path.open("rb") as output:
                os.fsync(output.fileno())
            os.replace(temporary_path, destination)
            _sync_directory(destination.parent)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        digest, size = _hash_file(destination)
        return BundleExportResult(
            path=destination,
            sha256=digest,
            byte_size=size,
            manifest=manifest,
        )

    def verify(self, bundle_path: Path) -> tuple[str, BundleManifest]:
        bundle_path = bundle_path.expanduser().resolve()
        try:
            bundle_hash, _ = _hash_file(bundle_path)
        except OSError as error:
            raise BundleIntegrityError("bundle file cannot be read") from error
        try:
            with zipfile.ZipFile(bundle_path, mode="r") as archive:
                infos = _validated_infos(archive)
                manifest_info = infos.get(MANIFEST_NAME)
                if manifest_info is None:
                    raise BundleIntegrityError("bundle is missing manifest.json")
                if manifest_info.file_size > MAX_MANIFEST_BYTES:
                    raise BundleIntegrityError("bundle manifest exceeds the size limit")
                try:
                    manifest = BundleManifest.model_validate_json(archive.read(manifest_info))
                except Exception as error:
                    raise BundleIntegrityError("bundle manifest is invalid") from error
                if manifest.schema_version > BUNDLE_SCHEMA_VERSION:
                    raise UnsupportedBundleVersionError(
                        f"bundle schema {manifest.schema_version} is newer than supported "
                        f"schema {BUNDLE_SCHEMA_VERSION}"
                    )
                expected_names = {
                    MANIFEST_NAME,
                    *(item.archive_path for item in manifest.members),
                }
                if set(infos) != expected_names:
                    missing = sorted(expected_names - set(infos))
                    extra = sorted(set(infos) - expected_names)
                    raise BundleIntegrityError(
                        "bundle member set does not match manifest; "
                        f"missing={missing}, extra={extra}"
                    )
                for member in manifest.members:
                    info = infos[member.archive_path]
                    if info.file_size != member.byte_size:
                        raise BundleIntegrityError(
                            f"bundle member size mismatch: {member.archive_path}"
                        )
                    digest, size = _hash_stream(archive.open(info, mode="r"))
                    if digest != member.sha256 or size != member.byte_size:
                        raise BundleIntegrityError(
                            f"bundle member checksum mismatch: {member.archive_path}"
                        )
        except BundleError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            raise BundleIntegrityError("bundle archive cannot be read safely") from error
        return bundle_hash, manifest

    def restore(self, bundle_path: Path) -> BundleRestoreResult:
        bundle_path = bundle_path.expanduser().resolve()
        bundle_hash, manifest = self.verify(bundle_path)
        existing = self.connection.execute(
            "SELECT project_id FROM bundle_imports WHERE bundle_sha256 = ?", (bundle_hash,)
        ).fetchone()
        if existing is not None:
            return BundleRestoreResult(
                project_id=existing["project_id"],
                source_project_id=manifest.project.id,
                bundle_sha256=bundle_hash,
                duplicate=True,
                imported_assets=0,
                imported_revisions=0,
                imported_artifacts=0,
                history_mode=manifest.history_mode,
                history_import=_history_import_kind(manifest),
                imported_history_nodes=0,
                abandoned_history_nodes=0,
            )

        stored_members = self._store_bundle_members(bundle_path, manifest)
        return self._restore_manifest(bundle_hash, manifest, stored_members)

    def _build_manifest(
        self,
        project_id: str,
        *,
        artifact_ids: Optional[Sequence[str]],
        history_mode: HistoryMode,
    ) -> BundleManifest:
        if history_mode not in {"full_history", "current_state_only"}:
            raise BundleError(f"unsupported project bundle history mode: {history_mode}")
        project_row = self.connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project_row is None:
            raise RecordNotFoundError(f"project not found: {project_id}")
        revision_rows = self.connection.execute(
            "SELECT * FROM revisions WHERE project_id = ? ORDER BY published_at, id",
            (project_id,),
        ).fetchall()
        revision_ids = [row["id"] for row in revision_rows]
        draft_row = self.connection.execute(
            "SELECT * FROM project_drafts WHERE project_id = ?", (project_id,)
        ).fetchone()

        history_lineage_rows = []
        history_state_rows = []
        history_node_rows = []
        history_head_row = None
        if history_mode == "full_history" and draft_row is not None:
            # Old/local workspaces may not have materialized their lazy anchor yet. Exporting full
            # history first asks the production repository to establish the exact canonical head.
            DraftRepository(self.connection).get(project_id)
        if history_mode == "full_history":
            history_lineage_rows = self.connection.execute(
                """
                SELECT * FROM draft_history_lineages
                WHERE project_id = ? ORDER BY created_at, id
                """,
                (project_id,),
            ).fetchall()
            lineage_ids = [row["id"] for row in history_lineage_rows]
            if lineage_ids:
                placeholders = ",".join("?" for _ in lineage_ids)
                history_state_rows = self.connection.execute(
                    f"""
                    SELECT * FROM draft_history_states
                    WHERE lineage_id IN ({placeholders}) ORDER BY created_at, sha256
                    """,  # noqa: S608
                    tuple(lineage_ids),
                ).fetchall()
                history_node_rows = self.connection.execute(
                    """
                    SELECT * FROM draft_history_nodes
                    WHERE project_id = ? ORDER BY depth, created_at, id
                    """,
                    (project_id,),
                ).fetchall()
            history_head_row = self.connection.execute(
                "SELECT * FROM draft_history_heads WHERE project_id = ?", (project_id,)
            ).fetchone()

        asset_ids = {row["source_asset_id"] for row in revision_rows}
        if draft_row is not None:
            draft_config = _json_object(draft_row["config_json"], "draft config")
            draft_source = draft_config.get("source_asset_id")
            if isinstance(draft_source, str):
                asset_ids.add(draft_source)
        asset_ids.update(row["source_asset_id"] for row in history_lineage_rows)
        assets = tuple(
            _asset_from_row(row)
            for row in _rows_for_ids(self.connection, "assets", sorted(asset_ids))
        )

        artifact_rows = []
        if revision_ids:
            placeholders = ",".join("?" for _ in revision_ids)
            artifact_rows = self.connection.execute(
                f"""
                SELECT * FROM artifacts
                WHERE revision_id IN ({placeholders})
                ORDER BY created_at, id
                """,  # noqa: S608
                tuple(revision_ids),
            ).fetchall()
        if artifact_ids is not None:
            selected = set(artifact_ids)
            found = {row["id"] for row in artifact_rows}
            if selected - found:
                raise BundleError("selected artifact is not owned by the project")
            artifact_rows = [row for row in artifact_rows if row["id"] in selected]
        artifacts = tuple(_artifact_from_row(row) for row in artifact_rows)

        publication_rows = []
        if revision_ids:
            placeholders = ",".join("?" for _ in revision_ids)
            publication_rows = self.connection.execute(
                f"""
                SELECT * FROM revision_publications
                WHERE revision_id IN ({placeholders})
                ORDER BY revision_id
                """,  # noqa: S608
                tuple(revision_ids),
            ).fetchall()
        publications = tuple(_publication_from_row(row) for row in publication_rows)
        artifacts_by_revision = {
            revision_id: tuple(item for item in artifacts if item.revision_id == revision_id)
            for revision_id in revision_ids
        }
        for publication in publications:
            if publication.preview_status != "fresh":
                continue
            selected_artifacts = artifacts_by_revision[publication.revision_id]
            if (
                len(selected_artifacts) != publication.artifact_count
                or artifact_manifest_sha256(selected_artifacts)
                != publication.artifact_manifest_sha256
            ):
                raise BundleError(
                    "selected artifact export must include the complete fresh evidence set "
                    f"for revision {publication.revision_id}"
                )

        operation_rows = []
        if revision_ids:
            placeholders = ",".join("?" for _ in revision_ids)
            operation_rows = self.connection.execute(
                f"""
                SELECT * FROM region_operations
                WHERE revision_id IN ({placeholders})
                ORDER BY revision_id, sequence
                """,  # noqa: S608
                tuple(revision_ids),
            ).fetchall()

        filament_ids = _referenced_filament_ids(
            revision_rows, draft_row, history_state_rows=history_state_rows
        )
        filament_rows = _rows_for_ids(self.connection, "filaments", sorted(filament_ids))
        preset_rows = self.connection.execute(
            "SELECT * FROM presets WHERE built_in = 0 ORDER BY scope, name, id"
        ).fetchall()

        members = _members_for((*assets, *artifacts))
        return BundleManifest(
            schema_version=BUNDLE_SCHEMA_VERSION,
            application_version=__version__,
            project=BundleProject(
                id=project_row["id"],
                name=project_row["name"],
                description=project_row["description"],
                active_revision_id=project_row["active_revision_id"],
                preferences=_json_object(project_row["preferences_json"], "project preferences"),
                created_at=project_row["created_at"],
                updated_at=project_row["updated_at"],
                archived_at=project_row["archived_at"],
            ),
            members=members,
            assets=assets,
            revisions=tuple(_revision_from_row(row) for row in revision_rows),
            artifacts=artifacts,
            operations=tuple(_operation_from_row(row) for row in operation_rows),
            filaments=tuple(_filament_from_row(row) for row in filament_rows),
            presets=tuple(_preset_from_row(row) for row in preset_rows),
            draft=_draft_from_row(draft_row) if draft_row is not None else None,
            revision_publications=publications,
            history_mode=history_mode,
            history_lineages=tuple(_history_lineage_from_row(row) for row in history_lineage_rows),
            history_states=tuple(_history_state_from_row(row) for row in history_state_rows),
            history_nodes=tuple(_history_node_from_row(row) for row in history_node_rows),
            history_head=(
                _history_head_from_row(history_head_row) if history_head_row is not None else None
            ),
        )

    def _verify_local_member(self, member: BundleMember) -> None:
        path = self.blob_store.path_for(member.relative_path)
        digest, size = _hash_file(path)
        if digest != member.sha256 or size != member.byte_size:
            raise BundleIntegrityError(f"workspace blob is corrupt: {member.relative_path}")

    def _store_bundle_members(self, bundle_path: Path, manifest: BundleManifest) -> dict[str, Any]:
        stored = {}
        with zipfile.ZipFile(bundle_path, mode="r") as archive:
            for member in manifest.members:
                namespace = PurePosixPath(member.relative_path).parts[0]
                with archive.open(member.archive_path, mode="r") as source:
                    blob = self.blob_store.put_stream(
                        source,
                        namespace=namespace,
                        extension=member.extension,
                        media_type=member.media_type,
                    )
                if (
                    blob.sha256 != member.sha256
                    or blob.byte_size != member.byte_size
                    or blob.relative_path != member.relative_path
                ):
                    raise BundleIntegrityError(
                        f"stored bundle member identity changed: {member.archive_path}"
                    )
                stored[member.relative_path] = blob
        return stored

    def _restore_manifest(
        self,
        bundle_hash: str,
        manifest: BundleManifest,
        stored_members: dict[str, Any],
    ) -> BundleRestoreResult:
        asset_map: dict[str, str] = {}
        revision_map: dict[str, str] = {}
        revision_config_sha256: dict[str, str] = {}
        filament_map: dict[str, str] = {}
        history_import = _history_import_kind(manifest)
        imported_history_nodes = 0
        abandoned_history_nodes = _abandoned_history_node_count(manifest)
        try:
            with immediate_transaction(self.connection):
                duplicate = self.connection.execute(
                    "SELECT project_id FROM bundle_imports WHERE bundle_sha256 = ?",
                    (bundle_hash,),
                ).fetchone()
                if duplicate is not None:
                    return BundleRestoreResult(
                        project_id=duplicate["project_id"],
                        source_project_id=manifest.project.id,
                        bundle_sha256=bundle_hash,
                        duplicate=True,
                        imported_assets=0,
                        imported_revisions=0,
                        imported_artifacts=0,
                        history_mode=manifest.history_mode,
                        history_import=_history_import_kind(manifest),
                        imported_history_nodes=0,
                        abandoned_history_nodes=0,
                    )

                for filament in manifest.filaments:
                    existing = self.connection.execute(
                        """
                        SELECT id FROM filaments
                        WHERE manufacturer = ? AND family = ? AND name = ? AND hex_color = ?
                        """,
                        (
                            filament.manufacturer,
                            filament.family,
                            filament.name,
                            filament.hex_color,
                        ),
                    ).fetchone()
                    target_id = (
                        existing["id"]
                        if existing is not None
                        else _available_id(self.connection, "filaments", filament.id, "filament")
                    )
                    filament_map[filament.id] = target_id
                    if existing is None:
                        self.connection.execute(
                            """
                            INSERT INTO filaments(
                                id, manufacturer, family, name, hex_color, material, finish,
                                owned, metadata_json, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                target_id,
                                filament.manufacturer,
                                filament.family,
                                filament.name,
                                filament.hex_color,
                                filament.material,
                                filament.finish,
                                int(filament.owned),
                                canonical_json(filament.metadata),
                                filament.created_at,
                                filament.updated_at,
                            ),
                        )

                imported_assets = 0
                for asset in manifest.assets:
                    existing = self.connection.execute(
                        "SELECT id FROM assets WHERE sha256 = ?", (asset.sha256,)
                    ).fetchone()
                    target_id = (
                        existing["id"]
                        if existing is not None
                        else _available_id(self.connection, "assets", asset.id, "asset")
                    )
                    asset_map[asset.id] = target_id
                    if existing is None:
                        blob = stored_members[asset.relative_path]
                        self.connection.execute(
                            """
                            INSERT INTO assets(
                                id, sha256, media_type, extension, original_filename,
                                relative_path, byte_size, width_px, height_px, metadata_json,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                target_id,
                                blob.sha256,
                                asset.media_type,
                                asset.extension,
                                Path(asset.original_filename).name,
                                blob.relative_path,
                                blob.byte_size,
                                asset.width_px,
                                asset.height_px,
                                canonical_json(asset.metadata),
                                asset.created_at,
                            ),
                        )
                        imported_assets += 1

                project_id = _available_id(
                    self.connection, "projects", manifest.project.id, "project"
                )
                project_name = manifest.project.name
                if project_id != manifest.project.id:
                    project_name = f"{project_name} (Imported)"
                self.connection.execute(
                    """
                    INSERT INTO projects(
                        id, name, description, active_revision_id, preferences_json,
                        created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        project_name,
                        manifest.project.description,
                        canonical_json(manifest.project.preferences),
                        manifest.project.created_at,
                        manifest.project.updated_at,
                        manifest.project.archived_at,
                    ),
                )

                for revision in manifest.revisions:
                    revision_map[revision.id] = _available_id(
                        self.connection, "revisions", revision.id, "revision"
                    )
                for revision in _topological_revisions(manifest.revisions):
                    rewritten = _rewrite_config(revision.config, asset_map, filament_map)
                    config = load_job_config(rewritten)
                    source_asset_id = asset_map[revision.source_asset_id]
                    if config.source_asset_id != source_asset_id:
                        raise BundleIntegrityError(
                            "revision config source does not match revision source asset"
                        )
                    self.connection.execute(
                        """
                        INSERT INTO revisions(
                            id, project_id, source_asset_id, parent_revision_id,
                            schema_version, engine_version, config_json, config_sha256,
                            label, notes, published_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_map[revision.id],
                            project_id,
                            source_asset_id,
                            revision_map.get(revision.parent_revision_id),
                            config.schema_version,
                            revision.engine_version,
                            config.canonical_json(),
                            config.fingerprint(),
                            revision.label,
                            revision.notes,
                            revision.published_at,
                        ),
                    )
                    revision_config_sha256[revision.id] = config.fingerprint()

                for operation in manifest.operations:
                    self.connection.execute(
                        """
                        INSERT INTO region_operations(
                            id, revision_id, sequence, operation_type, selection_json,
                            parameters_json, source, provenance_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("operation"),
                            revision_map[operation.revision_id],
                            operation.sequence,
                            operation.operation_type,
                            canonical_json(operation.selection),
                            canonical_json(operation.parameters),
                            operation.source,
                            canonical_json(operation.provenance),
                            operation.created_at,
                        ),
                    )

                imported_artifacts = 0
                for artifact in manifest.artifacts:
                    blob = stored_members[artifact.relative_path]
                    artifact_id = _available_id(
                        self.connection, "artifacts", artifact.id, "artifact"
                    )
                    derivation_key = _available_derivation_key(
                        self.connection,
                        artifact.kind,
                        artifact.derivation_key,
                        bundle_hash,
                    )
                    self.connection.execute(
                        """
                        INSERT INTO artifacts(
                            id, revision_id, job_id, kind, sha256, derivation_key,
                            media_type, relative_path, byte_size, metadata_json, created_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact_id,
                            revision_map[artifact.revision_id],
                            artifact.kind,
                            blob.sha256,
                            derivation_key,
                            artifact.media_type,
                            blob.relative_path,
                            blob.byte_size,
                            canonical_json(artifact.metadata),
                            artifact.created_at,
                        ),
                    )
                    imported_artifacts += 1

                operations_by_revision = {
                    revision.id: tuple(
                        sorted(
                            (
                                item
                                for item in manifest.operations
                                if item.revision_id == revision.id
                            ),
                            key=lambda item: item.sequence,
                        )
                    )
                    for revision in manifest.revisions
                }
                source_revisions = {item.id: item for item in manifest.revisions}
                for publication in manifest.revision_publications:
                    editor_sequence_sha256 = _bundle_editor_sequence_sha256(
                        revision_config_sha256[publication.revision_id],
                        operations_by_revision[publication.revision_id],
                    )
                    target_revision_id = revision_map[publication.revision_id]
                    target_artifacts = tuple(
                        _artifact_from_row(row)
                        for row in self.connection.execute(
                            """
                            SELECT * FROM artifacts
                            WHERE revision_id = ?
                            ORDER BY created_at, id
                            """,
                            (target_revision_id,),
                        ).fetchall()
                    )
                    preview_status = publication.preview_status
                    preview_reason = publication.preview_reason
                    artifact_count = publication.artifact_count
                    artifact_manifest = publication.artifact_manifest_sha256
                    if preview_status == "fresh" and (
                        revision_config_sha256[publication.revision_id]
                        != source_revisions[publication.revision_id].config_sha256
                        or editor_sequence_sha256 != publication.editor_sequence_sha256
                        or len(target_artifacts) != publication.artifact_count
                        or artifact_manifest_sha256(target_artifacts)
                        != publication.artifact_manifest_sha256
                    ):
                        preview_status = "stale"
                        preview_reason = (
                            "Fresh bundle evidence was downgraded because import identity "
                            "rewriting changed its config, editor sequence, or artifact manifest. "
                            f"Source evidence: {publication.preview_reason}"
                        )
                        artifact_count = 0
                        artifact_manifest = None
                    self.connection.execute(
                        """
                        INSERT INTO revision_publications(
                            revision_id, source_draft_generation, editor_sequence_sha256,
                            preview_status, preview_job_id, preview_derivation_key,
                            preview_reason, artifact_manifest_sha256, artifact_count, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_revision_id,
                            publication.source_draft_generation,
                            editor_sequence_sha256,
                            preview_status,
                            publication.preview_job_id,
                            publication.preview_derivation_key,
                            preview_reason,
                            artifact_manifest,
                            artifact_count,
                            publication.created_at,
                        ),
                    )

                for target_revision_id in revision_map.values():
                    self.connection.execute(
                        "INSERT INTO revision_history_seals(revision_id) VALUES (?)",
                        (target_revision_id,),
                    )

                for preset in manifest.presets:
                    _restore_preset(self.connection, preset, bundle_hash)

                if manifest.draft is not None:
                    rewritten = _rewrite_config(manifest.draft.config, asset_map, filament_map)
                    config = load_job_config(rewritten)
                    self.connection.execute(
                        """
                        INSERT INTO project_drafts(
                            project_id, base_revision_id, schema_version, config_json,
                            operation_json, updated_at, generation, config_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project_id,
                            revision_map.get(manifest.draft.base_revision_id),
                            config.schema_version,
                            config.canonical_json(),
                            canonical_json(list(manifest.draft.operations)),
                            manifest.draft.updated_at,
                            manifest.draft.generation,
                            config.fingerprint(),
                        ),
                    )

                if manifest.history_mode == "full_history":
                    imported_history_nodes = _restore_full_history(
                        self.connection,
                        manifest=manifest,
                        project_id=project_id,
                        asset_map=asset_map,
                        revision_map=revision_map,
                        filament_map=filament_map,
                    )
                elif manifest.draft is not None:
                    # Schema 1/2 and explicit current-state-only exports intentionally receive a
                    # new exact local anchor. Runtime mutation receipts are never portable.
                    DraftRepository(self.connection).get(project_id)

                active_revision = revision_map.get(manifest.project.active_revision_id)
                if active_revision is not None:
                    self.connection.execute(
                        "UPDATE projects SET active_revision_id = ? WHERE id = ?",
                        (active_revision, project_id),
                    )
                self.connection.execute(
                    """
                    INSERT INTO bundle_imports(
                        bundle_sha256, project_id, source_project_id, manifest_schema_version
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (bundle_hash, project_id, manifest.project.id, manifest.schema_version),
                )
        except BundleError:
            raise
        except Exception as error:
            raise BundleError(
                "bundle restore failed and database changes were rolled back"
            ) from error
        return BundleRestoreResult(
            project_id=project_id,
            source_project_id=manifest.project.id,
            bundle_sha256=bundle_hash,
            duplicate=False,
            imported_assets=imported_assets,
            imported_revisions=len(manifest.revisions),
            imported_artifacts=imported_artifacts,
            history_mode=manifest.history_mode,
            history_import=history_import,
            imported_history_nodes=imported_history_nodes,
            abandoned_history_nodes=abandoned_history_nodes,
        )


def _portable_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("bundle paths must be non-empty POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("bundle paths cannot be absolute or contain traversal")
    if path.as_posix() != value:
        raise ValueError("bundle paths must use canonical POSIX spelling")
    return path


def _validated_infos(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBER_COUNT:
        raise BundleIntegrityError("bundle contains too many members")
    result = {}
    total = 0
    for info in infos:
        try:
            path = _portable_path(info.filename)
        except ValueError as error:
            raise BundleIntegrityError(f"unsafe archive member path: {info.filename}") from error
        name = path.as_posix()
        if name in result:
            raise BundleIntegrityError(f"duplicate archive member: {name}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise BundleIntegrityError(f"archive symlink is forbidden: {name}")
        if info.is_dir():
            raise BundleIntegrityError(f"archive directory entries are forbidden: {name}")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BundleIntegrityError("bundle uncompressed size exceeds the limit")
        if (
            info.compress_size > 0
            and info.file_size > 1024 * 1024
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise BundleIntegrityError(f"suspicious compression ratio: {name}")
        result[name] = info
    return result


def _write_zip_bytes(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = _zip_info(name)
    archive.writestr(info, payload)


def _write_zip_file(archive: zipfile.ZipFile, name: str, source_path: Path) -> None:
    info = _zip_info(name)
    with source_path.open("rb") as source, archive.open(info, mode="w") as destination:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            destination.write(chunk)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _hash_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as source:
        return _hash_stream(source)


def _hash_stream(source) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_object(raw: str, label: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise BundleIntegrityError(f"stored {label} must be a JSON object")
    return value


def _asset_from_row(row) -> BundleAsset:
    return BundleAsset(
        id=row["id"],
        sha256=row["sha256"],
        media_type=row["media_type"],
        extension=row["extension"],
        original_filename=Path(row["original_filename"]).name,
        relative_path=row["relative_path"],
        byte_size=row["byte_size"],
        width_px=row["width_px"],
        height_px=row["height_px"],
        metadata=_json_object(row["metadata_json"], "asset metadata"),
        created_at=row["created_at"],
    )


def _revision_from_row(row) -> BundleRevision:
    return BundleRevision(
        id=row["id"],
        source_asset_id=row["source_asset_id"],
        parent_revision_id=row["parent_revision_id"],
        schema_version=row["schema_version"],
        engine_version=row["engine_version"],
        config=_json_object(row["config_json"], "revision config"),
        config_sha256=row["config_sha256"],
        label=row["label"],
        notes=row["notes"],
        published_at=row["published_at"],
    )


def _artifact_from_row(row) -> BundleArtifact:
    return BundleArtifact(
        id=row["id"],
        revision_id=row["revision_id"],
        kind=row["kind"],
        sha256=row["sha256"],
        derivation_key=row["derivation_key"],
        media_type=row["media_type"],
        relative_path=row["relative_path"],
        byte_size=row["byte_size"],
        metadata=_json_object(row["metadata_json"], "artifact metadata"),
        created_at=row["created_at"],
    )


def _operation_from_row(row) -> BundleOperation:
    return BundleOperation(
        id=row["id"],
        revision_id=row["revision_id"],
        sequence=row["sequence"],
        operation_type=row["operation_type"],
        selection=_json_object(row["selection_json"], "operation selection"),
        parameters=_json_object(row["parameters_json"], "operation parameters"),
        source=row["source"],
        provenance=_json_object(row["provenance_json"], "operation provenance"),
        created_at=row["created_at"],
    )


def _publication_from_row(row) -> BundleRevisionPublication:
    return BundleRevisionPublication(
        revision_id=row["revision_id"],
        source_draft_generation=row["source_draft_generation"],
        editor_sequence_sha256=row["editor_sequence_sha256"],
        preview_status=row["preview_status"],
        preview_job_id=row["preview_job_id"],
        preview_derivation_key=row["preview_derivation_key"],
        preview_reason=row["preview_reason"],
        artifact_manifest_sha256=row["artifact_manifest_sha256"],
        artifact_count=row["artifact_count"],
        created_at=row["created_at"],
    )


def _filament_from_row(row) -> BundleFilament:
    return BundleFilament(
        id=row["id"],
        manufacturer=row["manufacturer"],
        family=row["family"],
        name=row["name"],
        hex_color=row["hex_color"],
        material=row["material"],
        finish=row["finish"],
        owned=bool(row["owned"]),
        metadata=_json_object(row["metadata_json"], "filament metadata"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _preset_from_row(row) -> BundlePreset:
    return BundlePreset(
        id=row["id"],
        scope=row["scope"],
        name=row["name"],
        schema_version=row["schema_version"],
        settings=_json_object(row["settings_json"], "preset settings"),
        built_in=bool(row["built_in"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _draft_from_row(row) -> BundleDraft:
    operations = json.loads(row["operation_json"])
    if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
        raise BundleIntegrityError("stored draft operations must be a list of objects")
    return BundleDraft(
        base_revision_id=row["base_revision_id"],
        schema_version=row["schema_version"],
        config=_json_object(row["config_json"], "draft config"),
        operations=tuple(operations),
        generation=row["generation"],
        config_sha256=row["config_sha256"],
        updated_at=row["updated_at"],
    )


def _history_lineage_from_row(row) -> BundleHistoryLineage:
    return BundleHistoryLineage(
        id=row["id"],
        base_revision_id=row["base_revision_id"],
        source_asset_id=row["source_asset_id"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
    )


def _history_state_from_row(row) -> BundleHistoryState:
    operations = json.loads(row["operation_json"])
    if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
        raise BundleIntegrityError("stored draft history operations must be a list of objects")
    return BundleHistoryState(
        sha256=row["sha256"],
        lineage_id=row["lineage_id"],
        schema_version=row["schema_version"],
        base_revision_id=row["base_revision_id"],
        source_asset_id=row["source_asset_id"],
        config=_json_object(row["config_json"], "draft history config"),
        operations=tuple(operations),
        byte_size=row["byte_size"],
        created_at=row["created_at"],
    )


def _history_node_from_row(row) -> BundleHistoryNode:
    return BundleHistoryNode(
        id=row["id"],
        lineage_id=row["lineage_id"],
        parent_node_id=row["parent_node_id"],
        state_sha256=row["state_sha256"],
        depth=row["depth"],
        request_id=row["request_id"],
        schema_version=row["schema_version"],
        command_type=row["command_type"],
        label=row["label"],
        checkpoint_revision_id=row["checkpoint_revision_id"],
        created_at=row["created_at"],
    )


def _history_head_from_row(row) -> BundleHistoryHead:
    return BundleHistoryHead(
        lineage_id=row["lineage_id"],
        checkpoint_node_id=row["checkpoint_node_id"],
        undo_floor_node_id=row["undo_floor_node_id"],
        cursor_node_id=row["cursor_node_id"],
        tip_node_id=row["tip_node_id"],
        undo_limit=row["undo_limit"],
        updated_at=row["updated_at"],
    )


def _members_for(records: Iterable[Any]) -> tuple[BundleMember, ...]:
    members = {}
    for record in records:
        member = BundleMember(
            archive_path=f"payload/{record.relative_path}",
            relative_path=record.relative_path,
            sha256=record.sha256,
            byte_size=record.byte_size,
            media_type=record.media_type,
            extension=record.extension
            if hasattr(record, "extension")
            else Path(record.relative_path).suffix.lower(),
        )
        previous = members.get(member.relative_path)
        if previous is not None and previous != member:
            raise BundleIntegrityError("conflicting records reference one payload path")
        members[member.relative_path] = member
    return tuple(members[key] for key in sorted(members))


def _rows_for_ids(connection, table: str, identifiers: Sequence[str]):
    if table not in ID_TABLES:
        raise ValueError("unsupported bundle table")
    if not identifiers:
        return []
    placeholders = ",".join("?" for _ in identifiers)
    return connection.execute(
        f"SELECT * FROM {table} WHERE id IN ({placeholders}) ORDER BY id",  # noqa: S608
        tuple(identifiers),
    ).fetchall()


def _referenced_filament_ids(revision_rows, draft_row, *, history_state_rows=()) -> set[str]:
    result = set()
    configs = [_json_object(row["config_json"], "revision config") for row in revision_rows]
    if draft_row is not None:
        configs.append(_json_object(draft_row["config_json"], "draft config"))
    configs.extend(
        _json_object(row["config_json"], "draft history config") for row in history_state_rows
    )
    for config in configs:
        for color in config.get("palette", {}).get("colors", []):
            filament_id = color.get("filament_id") if isinstance(color, dict) else None
            if isinstance(filament_id, str):
                result.add(filament_id)
    return result


def _history_operations(values: Sequence[dict[str, Any]]) -> tuple[RegionOperation, ...]:
    operations = []
    for value in values:
        try:
            operations.append(
                RegionOperation(
                    operation_type=str(value["operation_type"]),
                    selection=dict(value.get("selection", {})),
                    parameters=dict(value.get("parameters", {})),
                    source=str(value.get("source", "manual")),
                    provenance=dict(value.get("provenance", {})),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("draft history operation is invalid") from error
    return tuple(operations)


def _validate_embedded_operation_payloads(values: Sequence[dict[str, Any]]) -> None:
    """Apply the durable provenance boundary to draft and undo-history snapshots."""

    for value in values:
        if not isinstance(value, dict):
            raise ValueError("embedded operation must be an object")
        selection = value.get("selection", {})
        parameters = value.get("parameters", {})
        provenance = value.get("provenance", {})
        if not isinstance(selection, dict):
            raise ValueError("embedded operation selection must be an object")
        if not isinstance(parameters, dict):
            raise ValueError("embedded operation parameters must be an object")
        if not isinstance(provenance, dict):
            raise ValueError("embedded operation provenance must be an object")
        validate_operation_provenance(
            selection=selection,
            parameters=parameters,
            provenance=provenance,
        )


def _history_is_ancestor(
    nodes: dict[str, BundleHistoryNode], ancestor_id: str, descendant_id: str
) -> bool:
    current = descendant_id
    seen = set()
    while current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        node = nodes.get(current)
        if node is None or node.parent_node_id is None:
            return False
        current = node.parent_node_id
    return False


def _history_import_kind(
    manifest: BundleManifest,
) -> Literal["full_history", "legacy_anchor", "none"]:
    if manifest.history_mode == "full_history":
        return "full_history"
    return "legacy_anchor" if manifest.draft is not None else "none"


def _abandoned_history_node_count(manifest: BundleManifest) -> int:
    head = manifest.history_head
    if head is None:
        return 0
    nodes = {item.id: item for item in manifest.history_nodes}
    active_path = set()
    current: Optional[str] = head.tip_node_id
    while current is not None and current not in active_path:
        active_path.add(current)
        current = nodes[current].parent_node_id
    return sum(
        item.lineage_id == head.lineage_id and item.id not in active_path
        for item in manifest.history_nodes
    )


def _restore_full_history(
    connection,
    *,
    manifest: BundleManifest,
    project_id: str,
    asset_map: dict[str, str],
    revision_map: dict[str, str],
    filament_map: dict[str, str],
) -> int:
    lineage_map = {
        lineage.id: _available_id(connection, "draft_history_lineages", lineage.id, "lineage")
        for lineage in manifest.history_lineages
    }
    for lineage in manifest.history_lineages:
        connection.execute(
            """
            INSERT INTO draft_history_lineages(
                id, project_id, base_revision_id, source_asset_id, created_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                lineage_map[lineage.id],
                project_id,
                revision_map.get(lineage.base_revision_id),
                asset_map[lineage.source_asset_id],
                lineage.created_at,
                lineage.closed_at,
            ),
        )

    state_map: dict[str, str] = {}
    for state in manifest.history_states:
        rewritten = _rewrite_config(state.config, asset_map, filament_map)
        config = load_job_config(rewritten)
        operations = _history_operations(state.operations)
        lineage_id = lineage_map[state.lineage_id]
        base_revision_id = revision_map.get(state.base_revision_id)
        source_asset_id = asset_map[state.source_asset_id]
        sha256 = exact_state_sha256(
            lineage_id=lineage_id,
            schema_version=state.schema_version,
            base_revision_id=base_revision_id,
            source_asset_id=source_asset_id,
            config=rewritten,
            operations=operations,
        )
        config_json = config.canonical_json()
        operation_payload = [operation_json(item) for item in operations]
        operation_json_text = canonical_json(operation_payload)
        connection.execute(
            """
            INSERT INTO draft_history_states(
                sha256, lineage_id, schema_version, base_revision_id, source_asset_id,
                config_json, operation_json, byte_size, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha256,
                lineage_id,
                state.schema_version,
                base_revision_id,
                source_asset_id,
                config_json,
                operation_json_text,
                len(config_json.encode("utf-8")) + len(operation_json_text.encode("utf-8")),
                state.created_at,
            ),
        )
        state_map[state.sha256] = sha256

    node_map = {
        node.id: _available_id(connection, "draft_history_nodes", node.id, "history")
        for node in manifest.history_nodes
    }
    ordered_nodes = sorted(
        manifest.history_nodes, key=lambda item: (item.depth, item.created_at, item.id)
    )
    for node in ordered_nodes:
        connection.execute(
            """
            INSERT INTO draft_history_nodes(
                id, project_id, lineage_id, parent_node_id, state_sha256, depth,
                request_id, schema_version, command_type, label,
                checkpoint_revision_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_map[node.id],
                project_id,
                lineage_map[node.lineage_id],
                node_map.get(node.parent_node_id),
                state_map[node.state_sha256],
                node.depth,
                node.request_id,
                node.schema_version,
                node.command_type,
                node.label,
                revision_map.get(node.checkpoint_revision_id),
                node.created_at,
            ),
        )

    if manifest.history_head is not None:
        head = manifest.history_head
        connection.execute(
            """
            INSERT INTO draft_history_heads(
                project_id, lineage_id, checkpoint_node_id, undo_floor_node_id,
                cursor_node_id, tip_node_id, undo_limit, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                lineage_map[head.lineage_id],
                node_map[head.checkpoint_node_id],
                node_map[head.undo_floor_node_id],
                node_map[head.cursor_node_id],
                node_map[head.tip_node_id],
                head.undo_limit,
                head.updated_at,
            ),
        )
    return len(manifest.history_nodes)


def _topological_revisions(
    revisions: Sequence[BundleRevision],
) -> tuple[BundleRevision, ...]:
    pending = {revision.id: revision for revision in revisions}
    ordered = []
    published = set()
    while pending:
        ready = sorted(
            (
                revision
                for revision in pending.values()
                if revision.parent_revision_id is None or revision.parent_revision_id in published
            ),
            key=lambda item: (item.published_at, item.id),
        )
        if not ready:
            raise BundleIntegrityError("revision graph contains a cycle or missing parent")
        for revision in ready:
            ordered.append(revision)
            published.add(revision.id)
            pending.pop(revision.id)
    return tuple(ordered)


def _available_id(connection, table: str, preferred: str, prefix: str) -> str:
    if table not in ID_TABLES:
        raise ValueError("unsupported bundle ID table")
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE id = ?",
        (preferred,),  # noqa: S608
    ).fetchone()
    return preferred if row is None else new_id(prefix)


def _rewrite_config(
    config: dict[str, Any], asset_map: dict[str, str], filament_map: dict[str, str]
) -> dict[str, Any]:
    rewritten = copy.deepcopy(config)
    source_id = rewritten.get("source_asset_id")
    if source_id not in asset_map:
        raise BundleIntegrityError("configuration references an absent source asset")
    rewritten["source_asset_id"] = asset_map[source_id]
    for color in rewritten.get("palette", {}).get("colors", []):
        if isinstance(color, dict) and color.get("filament_id") in filament_map:
            color["filament_id"] = filament_map[color["filament_id"]]
    return rewritten


def _bundle_editor_sequence_sha256(
    config_sha256: str, operations: Sequence[BundleOperation]
) -> str:
    raster_operations = tuple(
        {
            "operation_type": operation.operation_type,
            "selection": operation.selection,
            "parameters": operation.parameters,
            "source": operation.source,
            "provenance": operation.provenance,
        }
        for operation in operations
        if operation.operation_type != "palette-edit"
    )
    commands = load_persisted_commands(raster_operations)
    return EditorCommandSequence(config_fingerprint=config_sha256, commands=commands).fingerprint()


def _available_derivation_key(connection, kind: str, preferred: str, bundle_hash: str) -> str:
    candidate = preferred
    counter = 0
    while connection.execute(
        "SELECT 1 FROM artifacts WHERE derivation_key = ? AND kind = ?", (candidate, kind)
    ).fetchone():
        counter += 1
        candidate = f"{preferred}:import:{bundle_hash[:12]}:{counter}"
    return candidate


def _restore_preset(connection, preset: BundlePreset, bundle_hash: str) -> None:
    existing = connection.execute(
        "SELECT settings_json, schema_version FROM presets WHERE scope = ? AND name = ?",
        (preset.scope, preset.name),
    ).fetchone()
    settings_json = canonical_json(preset.settings)
    if (
        existing is not None
        and existing["settings_json"] == settings_json
        and existing["schema_version"] == preset.schema_version
    ):
        return
    name = preset.name
    if existing is not None:
        name = f"{name} (Imported {bundle_hash[:8]})"
    identifier = _available_id(connection, "presets", preset.id, "preset")
    connection.execute(
        """
        INSERT INTO presets(
            id, scope, name, schema_version, settings_json, built_in, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identifier,
            preset.scope,
            name,
            preset.schema_version,
            settings_json,
            int(preset.built_in),
            preset.created_at,
            preset.updated_at,
        ),
    )
