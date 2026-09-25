import sqlite3
from dataclasses import dataclass


class UnsupportedDatabaseVersionError(RuntimeError):
    """Raised when a workspace was created by a newer application version."""


class MigrationError(RuntimeError):
    """Raised after a migration transaction is rolled back."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


INITIAL_SCHEMA = Migration(
    version=1,
    name="initial_workspace_schema",
    statements=(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            description TEXT NOT NULL DEFAULT '',
            active_revision_id TEXT,
            preferences_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            archived_at TEXT
        )
        """,
        """
        CREATE TABLE assets (
            id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
            media_type TEXT NOT NULL,
            extension TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            relative_path TEXT NOT NULL UNIQUE,
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            width_px INTEGER CHECK (width_px > 0),
            height_px INTEGER CHECK (height_px > 0),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE revisions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
            parent_revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
            schema_version INTEGER NOT NULL CHECK (schema_version > 0),
            engine_version TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
            label TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE project_drafts (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            base_revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
            schema_version INTEGER NOT NULL CHECK (schema_version > 0),
            config_json TEXT NOT NULL,
            operation_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
            job_type TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('queued', 'running', 'succeeded', 'failed', 'canceled', 'superseded')
            ),
            stage TEXT NOT NULL DEFAULT '',
            progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
            request_key TEXT,
            error_code TEXT,
            error_message TEXT,
            error_details_json TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            started_at TEXT,
            finished_at TEXT,
            canceled_at TEXT
        )
        """,
        """
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            revision_id TEXT REFERENCES revisions(id) ON DELETE CASCADE,
            job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
            kind TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            derivation_key TEXT NOT NULL,
            media_type TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (derivation_key, kind)
        )
        """,
        """
        CREATE TABLE filaments (
            id TEXT PRIMARY KEY,
            manufacturer TEXT NOT NULL,
            family TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            hex_color TEXT NOT NULL CHECK (hex_color GLOB '#[0-9A-Fa-f]*'),
            material TEXT NOT NULL DEFAULT 'PLA',
            finish TEXT NOT NULL DEFAULT '',
            owned INTEGER NOT NULL DEFAULT 0 CHECK (owned IN (0, 1)),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (manufacturer, family, name, hex_color)
        )
        """,
        """
        CREATE TABLE presets (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            name TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK (schema_version > 0),
            settings_json TEXT NOT NULL,
            built_in INTEGER NOT NULL DEFAULT 0 CHECK (built_in IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (scope, name)
        )
        """,
        """
        CREATE TABLE region_operations (
            id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK (sequence >= 0),
            operation_type TEXT NOT NULL,
            selection_json TEXT NOT NULL,
            parameters_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL CHECK (source IN ('automatic', 'manual', 'model')),
            provenance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (revision_id, sequence)
        )
        """,
        "CREATE INDEX revisions_project_published ON revisions(project_id, published_at)",
        "CREATE INDEX jobs_project_created ON jobs(project_id, created_at)",
        "CREATE INDEX jobs_state ON jobs(state)",
        "CREATE INDEX artifacts_revision_kind ON artifacts(revision_id, kind)",
        "CREATE INDEX artifacts_sha256 ON artifacts(sha256)",
        "CREATE INDEX region_operations_revision ON region_operations(revision_id, sequence)",
    ),
)


REVISION_INTEGRITY = Migration(
    version=2,
    name="revision_relationship_integrity",
    statements=(
        """
        CREATE TRIGGER revisions_parent_same_project_insert
        BEFORE INSERT ON revisions
        WHEN NEW.parent_revision_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM revisions parent
              WHERE parent.id = NEW.parent_revision_id
                AND parent.project_id = NEW.project_id
          )
        BEGIN
            SELECT RAISE(ABORT, 'parent revision must belong to the same project');
        END
        """,
        """
        CREATE TRIGGER revisions_are_immutable
        BEFORE UPDATE ON revisions
        BEGIN
            SELECT RAISE(ABORT, 'published revisions are immutable');
        END
        """,
        """
        CREATE TRIGGER projects_active_revision_insert
        BEFORE INSERT ON projects
        WHEN NEW.active_revision_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM revisions active
              WHERE active.id = NEW.active_revision_id
                AND active.project_id = NEW.id
          )
        BEGIN
            SELECT RAISE(ABORT, 'active revision must belong to the project');
        END
        """,
        """
        CREATE TRIGGER projects_active_revision_update
        BEFORE UPDATE OF active_revision_id ON projects
        WHEN NEW.active_revision_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM revisions active
              WHERE active.id = NEW.active_revision_id
                AND active.project_id = NEW.id
          )
        BEGIN
            SELECT RAISE(ABORT, 'active revision must belong to the project');
        END
        """,
    ),
)


DRAFT_BRANCHING = Migration(
    version=3,
    name="draft_branching_and_operation_integrity",
    statements=(
        """
        ALTER TABLE project_drafts
        ADD COLUMN generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0)
        """,
        """
        ALTER TABLE project_drafts
        ADD COLUMN config_sha256 TEXT
        """,
        """
        CREATE TRIGGER project_drafts_base_revision_insert
        BEFORE INSERT ON project_drafts
        WHEN NEW.base_revision_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM revisions base
              WHERE base.id = NEW.base_revision_id
                AND base.project_id = NEW.project_id
          )
        BEGIN
            SELECT RAISE(ABORT, 'draft base revision must belong to the project');
        END
        """,
        """
        CREATE TRIGGER project_drafts_base_revision_update
        BEFORE UPDATE OF base_revision_id ON project_drafts
        WHEN NEW.base_revision_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM revisions base
              WHERE base.id = NEW.base_revision_id
                AND base.project_id = NEW.project_id
          )
        BEGIN
            SELECT RAISE(ABORT, 'draft base revision must belong to the project');
        END
        """,
        """
        CREATE TRIGGER project_drafts_generation_increases
        BEFORE UPDATE ON project_drafts
        WHEN NEW.generation <= OLD.generation
        BEGIN
            SELECT RAISE(ABORT, 'draft generation must increase');
        END
        """,
        """
        CREATE TRIGGER region_operations_are_immutable
        BEFORE UPDATE ON region_operations
        BEGIN
            SELECT RAISE(ABORT, 'published region operations are immutable');
        END
        """,
    ),
)


JOB_CONTRACT = Migration(
    version=4,
    name="stable_job_state_contract",
    statements=(
        """
        UPDATE jobs
        SET stage = CASE
            WHEN state = 'queued' THEN 'queued'
            WHEN state = 'running' THEN 'analyzing'
            WHEN state = 'succeeded' THEN 'complete'
            WHEN state = 'failed' THEN 'failed'
            WHEN state = 'canceled' THEN 'canceled'
            WHEN state = 'superseded' THEN 'superseded'
        END
        WHERE stage = ''
        """,
        """
        CREATE TRIGGER jobs_contract_insert
        BEFORE INSERT ON jobs
        BEGIN
            SELECT CASE
                WHEN NEW.job_type NOT IN ('preview', 'geometry', 'export', 'validation')
                THEN RAISE(ABORT, 'unsupported job type')
            END;
            SELECT CASE
                WHEN NEW.stage NOT IN (
                    'queued', 'ingesting', 'normalizing', 'quantizing', 'analyzing',
                    'cleaning', 'vectorizing', 'meshing', 'packaging', 'slicing',
                    'validating', 'complete', 'failed', 'canceled', 'superseded'
                )
                THEN RAISE(ABORT, 'unsupported job stage')
            END;
            SELECT CASE
                WHEN (NEW.state = 'queued' AND NEW.stage != 'queued')
                  OR (NEW.state = 'running' AND NEW.stage IN (
                      'queued', 'complete', 'failed', 'canceled', 'superseded'
                  ))
                  OR (NEW.state = 'succeeded' AND (NEW.stage != 'complete' OR NEW.progress != 1))
                  OR (NEW.state = 'failed' AND (NEW.stage != 'failed' OR NEW.error_code IS NULL))
                  OR (NEW.state = 'canceled' AND NEW.stage != 'canceled')
                  OR (NEW.state = 'superseded' AND NEW.stage != 'superseded')
                  OR (NEW.state IN ('succeeded', 'failed', 'canceled', 'superseded')
                      AND NEW.finished_at IS NULL)
                  OR (NEW.state IN ('queued', 'running') AND NEW.finished_at IS NOT NULL)
                  OR (NEW.state = 'canceled' AND NEW.canceled_at IS NULL)
                  OR (NEW.state != 'canceled' AND NEW.canceled_at IS NOT NULL)
                  OR (NEW.state != 'failed' AND NEW.error_code IS NOT NULL)
                THEN RAISE(ABORT, 'job state and stage are inconsistent')
            END;
        END
        """,
        """
        CREATE TRIGGER jobs_contract_update
        BEFORE UPDATE ON jobs
        BEGIN
            SELECT CASE
                WHEN NEW.job_type NOT IN ('preview', 'geometry', 'export', 'validation')
                THEN RAISE(ABORT, 'unsupported job type')
            END;
            SELECT CASE
                WHEN NEW.stage NOT IN (
                    'queued', 'ingesting', 'normalizing', 'quantizing', 'analyzing',
                    'cleaning', 'vectorizing', 'meshing', 'packaging', 'slicing',
                    'validating', 'complete', 'failed', 'canceled', 'superseded'
                )
                THEN RAISE(ABORT, 'unsupported job stage')
            END;
            SELECT CASE
                WHEN (NEW.state = 'queued' AND NEW.stage != 'queued')
                  OR (NEW.state = 'running' AND NEW.stage IN (
                      'queued', 'complete', 'failed', 'canceled', 'superseded'
                  ))
                  OR (NEW.state = 'succeeded' AND (NEW.stage != 'complete' OR NEW.progress != 1))
                  OR (NEW.state = 'failed' AND (NEW.stage != 'failed' OR NEW.error_code IS NULL))
                  OR (NEW.state = 'canceled' AND NEW.stage != 'canceled')
                  OR (NEW.state = 'superseded' AND NEW.stage != 'superseded')
                  OR (NEW.state IN ('succeeded', 'failed', 'canceled', 'superseded')
                      AND NEW.finished_at IS NULL)
                  OR (NEW.state IN ('queued', 'running') AND NEW.finished_at IS NOT NULL)
                  OR (NEW.state = 'canceled' AND NEW.canceled_at IS NULL)
                  OR (NEW.state != 'canceled' AND NEW.canceled_at IS NOT NULL)
                  OR (NEW.state != 'failed' AND NEW.error_code IS NOT NULL)
                THEN RAISE(ABORT, 'job state and stage are inconsistent')
            END;
        END
        """,
    ),
)


JOB_SUPERSESSION = Migration(
    version=5,
    name="job_supersession_heads",
    statements=(
        "ALTER TABLE jobs ADD COLUMN supersession_key TEXT",
        "ALTER TABLE jobs ADD COLUMN generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0)",
        """
        CREATE TABLE job_heads (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            supersession_key TEXT NOT NULL,
            job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL CHECK (generation > 0),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (project_id, supersession_key)
        )
        """,
        """
        CREATE UNIQUE INDEX jobs_supersession_generation
        ON jobs(project_id, supersession_key, generation)
        WHERE supersession_key IS NOT NULL
        """,
        """
        CREATE TRIGGER jobs_supersession_contract_insert
        BEFORE INSERT ON jobs
        WHEN (NEW.supersession_key IS NULL AND NEW.generation != 0)
          OR (NEW.supersession_key IS NOT NULL AND NEW.generation < 1)
        BEGIN
            SELECT RAISE(ABORT, 'job supersession generation is inconsistent');
        END
        """,
        """
        CREATE TRIGGER jobs_supersession_contract_update
        BEFORE UPDATE OF supersession_key, generation ON jobs
        WHEN NEW.supersession_key IS NOT OLD.supersession_key
          OR NEW.generation != OLD.generation
        BEGIN
            SELECT RAISE(ABORT, 'job supersession identity is immutable');
        END
        """,
    ),
)


BUNDLE_IMPORTS = Migration(
    version=6,
    name="portable_bundle_import_history",
    statements=(
        """
        CREATE TABLE bundle_imports (
            bundle_sha256 TEXT PRIMARY KEY CHECK (length(bundle_sha256) = 64),
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_project_id TEXT NOT NULL,
            manifest_schema_version INTEGER NOT NULL CHECK (manifest_schema_version > 0),
            imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "CREATE INDEX bundle_imports_project ON bundle_imports(project_id, imported_at)",
    ),
)


ARTIFACT_OWNERSHIP_UNIQUENESS = Migration(
    version=7,
    name="artifact_ownership_scoped_derivations",
    statements=(
        "ALTER TABLE artifacts RENAME TO artifacts_global_derivations",
        """
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            revision_id TEXT REFERENCES revisions(id) ON DELETE CASCADE,
            job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
            kind TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            derivation_key TEXT NOT NULL,
            media_type TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        INSERT INTO artifacts(
            id, revision_id, job_id, kind, sha256, derivation_key, media_type,
            relative_path, byte_size, metadata_json, created_at
        )
        SELECT
            id, revision_id, job_id, kind, sha256, derivation_key, media_type,
            relative_path, byte_size, metadata_json, created_at
        FROM artifacts_global_derivations
        """,
        "DROP TABLE artifacts_global_derivations",
        "CREATE INDEX artifacts_revision_kind ON artifacts(revision_id, kind)",
        "CREATE INDEX artifacts_sha256 ON artifacts(sha256)",
        """
        CREATE UNIQUE INDEX artifacts_job_derivation_kind
        ON artifacts(job_id, derivation_key, kind)
        WHERE job_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX artifacts_revision_derivation_kind
        ON artifacts(revision_id, derivation_key, kind)
        WHERE revision_id IS NOT NULL
        """,
    ),
)


REVISION_PUBLICATION_EVIDENCE = Migration(
    version=8,
    name="immutable_revision_publication_evidence",
    statements=(
        """
        CREATE TABLE revision_publications (
            revision_id TEXT PRIMARY KEY REFERENCES revisions(id) ON DELETE CASCADE,
            source_draft_generation INTEGER NOT NULL CHECK (source_draft_generation > 0),
            editor_sequence_sha256 TEXT NOT NULL CHECK (length(editor_sequence_sha256) = 64),
            preview_status TEXT NOT NULL CHECK (
                preview_status IN ('fresh', 'missing', 'stale')
            ),
            preview_job_id TEXT,
            preview_derivation_key TEXT,
            preview_reason TEXT NOT NULL DEFAULT '',
            artifact_manifest_sha256 TEXT CHECK (
                artifact_manifest_sha256 IS NULL
                OR length(artifact_manifest_sha256) = 64
            ),
            artifact_count INTEGER NOT NULL DEFAULT 0 CHECK (artifact_count >= 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CHECK (
                (preview_status = 'fresh'
                    AND preview_job_id IS NOT NULL
                    AND preview_derivation_key IS NOT NULL
                    AND artifact_manifest_sha256 IS NOT NULL
                    AND artifact_count > 0)
                OR
                (preview_status != 'fresh'
                    AND artifact_manifest_sha256 IS NULL
                    AND artifact_count = 0)
            )
        )
        """,
        """
        CREATE TRIGGER revision_publications_are_immutable
        BEFORE UPDATE ON revision_publications
        BEGIN
            SELECT RAISE(ABORT, 'revision publication evidence is immutable');
        END
        """,
        """
        CREATE TRIGGER published_artifacts_are_immutable
        BEFORE UPDATE ON artifacts
        WHEN OLD.revision_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'published revision artifacts are immutable');
        END
        """,
    ),
)


SEALED_REVISION_HISTORY = Migration(
    version=9,
    name="sealed_revision_history",
    statements=(
        """
        CREATE TABLE revision_history_seals (
            revision_id TEXT PRIMARY KEY REFERENCES revisions(id) ON DELETE CASCADE,
            sealed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        "INSERT INTO revision_history_seals(revision_id) SELECT id FROM revisions",
        """
        CREATE TRIGGER revisions_reject_replacement
        BEFORE INSERT ON revisions
        WHEN EXISTS (SELECT 1 FROM revisions WHERE id = NEW.id)
        BEGIN
            SELECT RAISE(ABORT, 'published revision replacement is forbidden');
        END
        """,
        """
        CREATE TRIGGER revisions_reject_direct_delete
        BEFORE DELETE ON revisions
        WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
        BEGIN
            SELECT RAISE(ABORT, 'published revision deletion is forbidden');
        END
        """,
        """
        CREATE TRIGGER region_operations_reject_late_insert
        BEFORE INSERT ON region_operations
        WHEN EXISTS (
            SELECT 1 FROM revision_history_seals WHERE revision_id = NEW.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'published revision operation insertion is forbidden');
        END
        """,
        """
        CREATE TRIGGER region_operations_reject_direct_delete
        BEFORE DELETE ON region_operations
        WHEN EXISTS (
            SELECT 1 FROM revisions
            JOIN projects ON projects.id = revisions.project_id
            WHERE revisions.id = OLD.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'published revision operation deletion is forbidden');
        END
        """,
        """
        CREATE TRIGGER artifacts_reject_late_revision_insert
        BEFORE INSERT ON artifacts
        WHEN NEW.revision_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM revision_history_seals WHERE revision_id = NEW.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'published revision artifact insertion is forbidden');
        END
        """,
        """
        CREATE TRIGGER artifacts_reject_direct_revision_delete
        BEFORE DELETE ON artifacts
        WHEN OLD.revision_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM revisions
            JOIN projects ON projects.id = revisions.project_id
            WHERE revisions.id = OLD.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'published revision artifact deletion is forbidden');
        END
        """,
        """
        CREATE TRIGGER revision_publications_reject_late_insert
        BEFORE INSERT ON revision_publications
        WHEN EXISTS (
            SELECT 1 FROM revision_history_seals WHERE revision_id = NEW.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'revision publication insertion is forbidden after sealing');
        END
        """,
        """
        CREATE TRIGGER revision_publications_reject_direct_delete
        BEFORE DELETE ON revision_publications
        WHEN EXISTS (
            SELECT 1 FROM revisions
            JOIN projects ON projects.id = revisions.project_id
            WHERE revisions.id = OLD.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'revision publication deletion is forbidden');
        END
        """,
        """
        CREATE TRIGGER revision_history_seals_are_immutable
        BEFORE UPDATE ON revision_history_seals
        BEGIN
            SELECT RAISE(ABORT, 'revision history seal is immutable');
        END
        """,
        """
        CREATE TRIGGER revision_history_seals_reject_replacement
        BEFORE INSERT ON revision_history_seals
        WHEN EXISTS (
            SELECT 1 FROM revision_history_seals WHERE revision_id = NEW.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'revision history seal replacement is forbidden');
        END
        """,
        """
        CREATE TRIGGER revision_history_seals_reject_direct_delete
        BEFORE DELETE ON revision_history_seals
        WHEN EXISTS (
            SELECT 1 FROM revisions
            JOIN projects ON projects.id = revisions.project_id
            WHERE revisions.id = OLD.revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'revision history seal deletion is forbidden');
        END
        """,
    ),
)


DRAFT_COMMAND_HISTORY = Migration(
    version=10,
    name="server_authoritative_draft_command_history",
    statements=(
        """
        CREATE TABLE draft_history_lineages (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            base_revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
            source_asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            closed_at TEXT
        )
        """,
        """
        CREATE TABLE draft_history_states (
            sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
            lineage_id TEXT NOT NULL REFERENCES draft_history_lineages(id) ON DELETE CASCADE,
            schema_version INTEGER NOT NULL CHECK (schema_version > 0),
            base_revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
            source_asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
            config_json TEXT NOT NULL,
            operation_json TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE draft_history_nodes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            lineage_id TEXT NOT NULL REFERENCES draft_history_lineages(id) ON DELETE CASCADE,
            parent_node_id TEXT REFERENCES draft_history_nodes(id) ON DELETE CASCADE,
            state_sha256 TEXT NOT NULL REFERENCES draft_history_states(sha256) ON DELETE CASCADE,
            depth INTEGER NOT NULL CHECK (depth >= 0),
            request_id TEXT,
            schema_version INTEGER NOT NULL CHECK (schema_version > 0),
            command_type TEXT NOT NULL,
            label TEXT NOT NULL,
            checkpoint_revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (project_id, request_id)
        )
        """,
        """
        CREATE TABLE draft_history_heads (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            lineage_id TEXT NOT NULL REFERENCES draft_history_lineages(id) ON DELETE CASCADE,
            checkpoint_node_id TEXT NOT NULL REFERENCES draft_history_nodes(id) ON DELETE CASCADE,
            undo_floor_node_id TEXT NOT NULL REFERENCES draft_history_nodes(id) ON DELETE CASCADE,
            cursor_node_id TEXT NOT NULL REFERENCES draft_history_nodes(id) ON DELETE CASCADE,
            tip_node_id TEXT NOT NULL REFERENCES draft_history_nodes(id) ON DELETE CASCADE,
            undo_limit INTEGER NOT NULL DEFAULT 100 CHECK (undo_limit > 0),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE draft_history_receipts (
            request_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            mutation_type TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE INDEX draft_history_lineages_project
        ON draft_history_lineages(project_id, created_at)
        """,
        """
        CREATE INDEX draft_history_nodes_lineage_depth
        ON draft_history_nodes(lineage_id, depth)
        """,
        "CREATE INDEX draft_history_nodes_parent ON draft_history_nodes(parent_node_id)",
        """
        CREATE INDEX draft_history_receipts_project
        ON draft_history_receipts(project_id, created_at)
        """,
        """
        CREATE TRIGGER draft_history_nodes_are_immutable
        BEFORE UPDATE ON draft_history_nodes
        BEGIN
            SELECT RAISE(ABORT, 'draft history nodes are immutable');
        END
        """,
        """
        CREATE TRIGGER draft_history_states_are_immutable
        BEFORE UPDATE ON draft_history_states
        BEGIN
            SELECT RAISE(ABORT, 'draft history states are immutable');
        END
        """,
        """
        CREATE TRIGGER draft_history_nodes_reject_direct_delete
        BEFORE DELETE ON draft_history_nodes
        WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
        BEGIN
            SELECT RAISE(ABORT, 'draft history nodes are append-only');
        END
        """,
        """
        CREATE TRIGGER draft_history_states_reject_direct_delete
        BEFORE DELETE ON draft_history_states
        WHEN EXISTS (
            SELECT 1 FROM draft_history_lineages lineage
            JOIN projects ON projects.id = lineage.project_id
            WHERE lineage.id = OLD.lineage_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'draft history states are append-only');
        END
        """,
        """
        CREATE TRIGGER draft_history_node_parent_same_lineage
        BEFORE INSERT ON draft_history_nodes
        WHEN NEW.parent_node_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM draft_history_nodes parent
            WHERE parent.id = NEW.parent_node_id
              AND parent.project_id = NEW.project_id
              AND parent.lineage_id = NEW.lineage_id
              AND parent.depth + 1 = NEW.depth
        )
        BEGIN
            SELECT RAISE(ABORT, 'draft history parent must precede node in the same lineage');
        END
        """,
    ),
)


DRAFT_HISTORY_LIFECYCLE = Migration(
    version=11,
    name="draft_history_lifecycle_cleanup",
    statements=(
        """
        UPDATE draft_history_lineages
        SET closed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id IN (
            SELECT head.lineage_id
            FROM draft_history_heads head
            LEFT JOIN project_drafts draft ON draft.project_id = head.project_id
            LEFT JOIN draft_history_nodes node ON node.id = head.cursor_node_id
            LEFT JOIN draft_history_states state ON state.sha256 = node.state_sha256
            WHERE draft.project_id IS NULL
               OR state.sha256 IS NULL
               OR state.schema_version != draft.schema_version
               OR state.base_revision_id IS NOT draft.base_revision_id
               OR state.config_json != draft.config_json
               OR state.operation_json != draft.operation_json
        ) AND closed_at IS NULL
        """,
        """
        DELETE FROM draft_history_heads
        WHERE project_id IN (
            SELECT head.project_id
            FROM draft_history_heads head
            LEFT JOIN project_drafts draft ON draft.project_id = head.project_id
            LEFT JOIN draft_history_nodes node ON node.id = head.cursor_node_id
            LEFT JOIN draft_history_states state ON state.sha256 = node.state_sha256
            WHERE draft.project_id IS NULL
               OR state.sha256 IS NULL
               OR state.schema_version != draft.schema_version
               OR state.base_revision_id IS NOT draft.base_revision_id
               OR state.config_json != draft.config_json
               OR state.operation_json != draft.operation_json
        )
        """,
        """
        CREATE TRIGGER project_draft_delete_closes_history
        AFTER DELETE ON project_drafts
        WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
        BEGIN
            UPDATE draft_history_lineages
            SET closed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = (
                SELECT lineage_id FROM draft_history_heads WHERE project_id = OLD.project_id
            ) AND closed_at IS NULL;
            DELETE FROM draft_history_heads WHERE project_id = OLD.project_id;
        END
        """,
        """
        CREATE TRIGGER draft_history_lineage_ownership_insert
        BEFORE INSERT ON draft_history_lineages
        WHEN NEW.base_revision_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM revisions
            WHERE id = NEW.base_revision_id AND project_id = NEW.project_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'draft history lineage base must belong to its project');
        END
        """,
        """
        CREATE TRIGGER draft_history_lineage_identity_immutable
        BEFORE UPDATE ON draft_history_lineages
        WHEN NEW.id != OLD.id
          OR NEW.project_id != OLD.project_id
          OR NEW.base_revision_id IS NOT OLD.base_revision_id
          OR NEW.source_asset_id != OLD.source_asset_id
          OR NEW.created_at != OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'draft history lineage identity is immutable');
        END
        """,
        """
        CREATE TRIGGER draft_history_state_ownership_insert
        BEFORE INSERT ON draft_history_states
        WHEN NOT EXISTS (
            SELECT 1 FROM draft_history_lineages lineage
            WHERE lineage.id = NEW.lineage_id
              AND NEW.source_asset_id = lineage.source_asset_id
              AND NEW.base_revision_id IS lineage.base_revision_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'draft history state identity does not match lineage');
        END
        """,
        """
        CREATE TRIGGER draft_history_node_ownership_insert
        BEFORE INSERT ON draft_history_nodes
        WHEN NOT EXISTS (
            SELECT 1 FROM draft_history_lineages lineage
            JOIN draft_history_states state ON state.sha256 = NEW.state_sha256
            WHERE lineage.id = NEW.lineage_id
              AND lineage.project_id = NEW.project_id
              AND state.lineage_id = NEW.lineage_id
              AND (NEW.checkpoint_revision_id IS NULL OR EXISTS (
                  SELECT 1 FROM revisions revision
                  WHERE revision.id = NEW.checkpoint_revision_id
                    AND revision.project_id = NEW.project_id
              ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'draft history node identity is cross-project or cross-lineage');
        END
        """,
        """
        CREATE TRIGGER draft_history_head_ownership_insert
        BEFORE INSERT ON draft_history_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM draft_history_lineages lineage
            JOIN draft_history_nodes checkpoint ON checkpoint.id = NEW.checkpoint_node_id
            JOIN draft_history_nodes floor ON floor.id = NEW.undo_floor_node_id
            JOIN draft_history_nodes cursor ON cursor.id = NEW.cursor_node_id
            JOIN draft_history_nodes tip ON tip.id = NEW.tip_node_id
            WHERE lineage.id = NEW.lineage_id
              AND lineage.project_id = NEW.project_id
              AND checkpoint.project_id = NEW.project_id
              AND checkpoint.lineage_id = NEW.lineage_id
              AND floor.project_id = NEW.project_id
              AND floor.lineage_id = NEW.lineage_id
              AND cursor.project_id = NEW.project_id
              AND cursor.lineage_id = NEW.lineage_id
              AND tip.project_id = NEW.project_id
              AND tip.lineage_id = NEW.lineage_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'draft history head identity is cross-project or cross-lineage');
        END
        """,
        """
        CREATE TRIGGER draft_history_head_ownership_update
        BEFORE UPDATE ON draft_history_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM draft_history_lineages lineage
            JOIN draft_history_nodes checkpoint ON checkpoint.id = NEW.checkpoint_node_id
            JOIN draft_history_nodes floor ON floor.id = NEW.undo_floor_node_id
            JOIN draft_history_nodes cursor ON cursor.id = NEW.cursor_node_id
            JOIN draft_history_nodes tip ON tip.id = NEW.tip_node_id
            WHERE lineage.id = NEW.lineage_id
              AND lineage.project_id = NEW.project_id
              AND checkpoint.project_id = NEW.project_id
              AND checkpoint.lineage_id = NEW.lineage_id
              AND floor.project_id = NEW.project_id
              AND floor.lineage_id = NEW.lineage_id
              AND cursor.project_id = NEW.project_id
              AND cursor.lineage_id = NEW.lineage_id
              AND tip.project_id = NEW.project_id
              AND tip.lineage_id = NEW.lineage_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'draft history head identity is cross-project or cross-lineage');
        END
        """,
        """
        CREATE TRIGGER draft_history_head_identity_immutable
        BEFORE UPDATE ON draft_history_heads
        WHEN NEW.project_id != OLD.project_id OR NEW.lineage_id != OLD.lineage_id
        BEGIN
            SELECT RAISE(ABORT, 'draft history head project and lineage are immutable');
        END
        """,
        """
        CREATE TRIGGER draft_history_receipts_are_immutable
        BEFORE UPDATE ON draft_history_receipts
        BEGIN
            SELECT RAISE(ABORT, 'draft history receipts are immutable');
        END
        """,
        """
        CREATE TRIGGER draft_history_receipts_reject_direct_delete
        BEFORE DELETE ON draft_history_receipts
        WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
        BEGIN
            SELECT RAISE(ABORT, 'draft history receipts cannot be deleted');
        END
        """,
    ),
)


STARTUP_RECONCILIATION_HISTORY = Migration(
    version=12,
    name="startup_reconciliation_history",
    statements=(
        """
        CREATE TABLE startup_reconciliations (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN ('healthy', 'attention', 'critical')),
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            interrupted_job_count INTEGER NOT NULL CHECK (interrupted_job_count >= 0),
            stale_temp_file_count INTEGER NOT NULL CHECK (stale_temp_file_count >= 0),
            orphan_file_count INTEGER NOT NULL CHECK (orphan_file_count >= 0),
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TRIGGER startup_reconciliations_are_immutable
        BEFORE UPDATE ON startup_reconciliations
        BEGIN
            SELECT RAISE(ABORT, 'startup reconciliation evidence is immutable');
        END
        """,
        """
        CREATE INDEX startup_reconciliations_completed
        ON startup_reconciliations(completed_at DESC, id DESC)
        """,
    ),
)


MURAL_PLANS = Migration(
    version=13,
    name="versioned_master_canvas_mural_plans",
    statements=(
        """
        CREATE TABLE mural_plans (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            schema_version INTEGER NOT NULL CHECK (schema_version > 0),
            generation INTEGER NOT NULL CHECK (generation > 0),
            source_asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
            source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
            request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
            request_json TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TRIGGER mural_plan_generation_increases_exactly_once
        BEFORE UPDATE ON mural_plans
        WHEN NEW.generation != OLD.generation + 1
        BEGIN
            SELECT RAISE(ABORT, 'mural plan generation must increase exactly once');
        END
        """,
        """
        CREATE TRIGGER mural_plan_identity_is_immutable
        BEFORE UPDATE ON mural_plans
        WHEN NEW.project_id != OLD.project_id OR NEW.created_at != OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'mural plan project identity is immutable');
        END
        """,
        """
        CREATE INDEX mural_plans_source_asset ON mural_plans(source_asset_id)
        """,
    ),
)


PROMPTED_EDIT_ALTERNATIVES = Migration(
    version=14,
    name="durable_prompted_edit_alternatives",
    statements=(
        """
        CREATE TABLE prompted_edit_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            parent_revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
            retry_of_session_id TEXT REFERENCES prompted_edit_sessions(id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (status IN (
                'prepared', 'queued', 'running', 'complete', 'partial', 'failed',
                'canceled', 'rejected', 'accepted'
            )),
            request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
            selection_sha256 TEXT NOT NULL CHECK (length(selection_sha256) = 64),
            source_asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
            mask_sha256 TEXT NOT NULL CHECK (length(mask_sha256) = 64),
            mask_relative_path TEXT NOT NULL,
            mask_byte_size INTEGER NOT NULL CHECK (mask_byte_size > 0),
            provider_json TEXT NOT NULL,
            disclosure_json TEXT NOT NULL,
            request_json TEXT NOT NULL,
            failures_json TEXT NOT NULL DEFAULT '[]',
            accepted_alternative_id TEXT,
            accepted_revision_id TEXT REFERENCES revisions(id) ON DELETE RESTRICT,
            rejection_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(project_id, request_sha256, retry_of_session_id)
        )
        """,
        """
        CREATE TABLE prompted_edit_alternatives (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES prompted_edit_sessions(id) ON DELETE CASCADE,
            alternative_index INTEGER NOT NULL CHECK (alternative_index BETWEEN 0 AND 15),
            status TEXT NOT NULL CHECK (status IN ('review', 'rejected', 'accepted')),
            output_asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
            output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
            changed_mask_sha256 TEXT NOT NULL CHECK (length(changed_mask_sha256) = 64),
            changed_mask_relative_path TEXT NOT NULL,
            changed_mask_byte_size INTEGER NOT NULL CHECK (changed_mask_byte_size > 0),
            changed_mask_preview_sha256 TEXT NOT NULL CHECK (
                length(changed_mask_preview_sha256) = 64
            ),
            changed_mask_preview_relative_path TEXT NOT NULL,
            changed_mask_preview_byte_size INTEGER NOT NULL CHECK (
                changed_mask_preview_byte_size > 0
            ),
            changed_pixel_count INTEGER NOT NULL CHECK (changed_pixel_count >= 0),
            width_px INTEGER NOT NULL CHECK (width_px > 0),
            height_px INTEGER NOT NULL CHECK (height_px > 0),
            provenance_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(session_id, alternative_index),
            UNIQUE(session_id, output_sha256)
        )
        """,
        """
        CREATE INDEX prompted_edit_sessions_project_created
        ON prompted_edit_sessions(project_id, created_at DESC, id DESC)
        """,
        """
        CREATE INDEX prompted_edit_alternatives_session
        ON prompted_edit_alternatives(session_id, alternative_index)
        """,
        """
        CREATE TRIGGER prompted_edit_session_parent_ownership_insert
        BEFORE INSERT ON prompted_edit_sessions
        WHEN NOT EXISTS (
            SELECT 1 FROM revisions
            WHERE id = NEW.parent_revision_id AND project_id = NEW.project_id
        ) OR (
            NEW.retry_of_session_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM prompted_edit_sessions
                WHERE id = NEW.retry_of_session_id AND project_id = NEW.project_id
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'prompted edit parent identity is cross-project');
        END
        """,
        """
        CREATE TRIGGER prompted_edit_session_identity_immutable
        BEFORE UPDATE ON prompted_edit_sessions
        WHEN NEW.project_id != OLD.project_id
          OR NEW.parent_revision_id != OLD.parent_revision_id
          OR NEW.request_sha256 != OLD.request_sha256
          OR NEW.selection_sha256 != OLD.selection_sha256
          OR NEW.source_asset_id != OLD.source_asset_id
          OR NEW.mask_sha256 != OLD.mask_sha256
          OR NEW.provider_json != OLD.provider_json
          OR NEW.disclosure_json != OLD.disclosure_json
          OR NEW.request_json != OLD.request_json
          OR NEW.retry_of_session_id IS NOT OLD.retry_of_session_id
        BEGIN
            SELECT RAISE(ABORT, 'prompted edit session identity is immutable');
        END
        """,
        """
        CREATE TRIGGER prompted_edit_session_transition
        BEFORE UPDATE OF status ON prompted_edit_sessions
        WHEN NEW.status != OLD.status AND NOT (
            (OLD.status = 'prepared' AND NEW.status IN ('queued', 'canceled'))
            OR (OLD.status = 'queued' AND NEW.status IN ('running', 'failed', 'canceled'))
            OR (OLD.status = 'running' AND NEW.status IN (
                'complete', 'partial', 'failed', 'canceled'
            ))
            OR (OLD.status IN ('complete', 'partial') AND NEW.status IN (
                'accepted', 'rejected'
            ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid prompted edit session transition');
        END
        """,
        """
        CREATE TRIGGER prompted_edit_alternative_ownership_insert
        BEFORE INSERT ON prompted_edit_alternatives
        WHEN NOT EXISTS (
            SELECT 1 FROM prompted_edit_sessions session
            JOIN assets output ON output.id = NEW.output_asset_id
            WHERE session.id = NEW.session_id AND output.sha256 = NEW.output_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'prompted edit alternative output identity is invalid');
        END
        """,
        """
        CREATE TRIGGER prompted_edit_alternative_identity_immutable
        BEFORE UPDATE ON prompted_edit_alternatives
        WHEN NEW.session_id != OLD.session_id
          OR NEW.alternative_index != OLD.alternative_index
          OR NEW.output_asset_id != OLD.output_asset_id
          OR NEW.output_sha256 != OLD.output_sha256
          OR NEW.changed_mask_sha256 != OLD.changed_mask_sha256
          OR NEW.provenance_json != OLD.provenance_json
        BEGIN
            SELECT RAISE(ABORT, 'prompted edit alternative identity is immutable');
        END
        """,
        """
        CREATE TRIGGER prompted_edit_alternative_transition
        BEFORE UPDATE OF status ON prompted_edit_alternatives
        WHEN NEW.status != OLD.status AND NOT (
            OLD.status = 'review' AND NEW.status IN ('accepted', 'rejected')
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid prompted edit alternative transition');
        END
        """,
    ),
)


PROMPTED_EDIT_JOBS = Migration(
    version=15,
    name="prompted_edit_job_contract",
    statements=(
        "DROP TRIGGER jobs_contract_insert",
        "DROP TRIGGER jobs_contract_update",
        """
        CREATE TRIGGER jobs_contract_insert
        BEFORE INSERT ON jobs
        BEGIN
            SELECT CASE
                WHEN NEW.job_type NOT IN (
                    'preview', 'geometry', 'export', 'validation', 'prompted_edit'
                ) THEN RAISE(ABORT, 'unsupported job type')
            END;
            SELECT CASE
                WHEN NEW.stage NOT IN (
                    'queued', 'ingesting', 'normalizing', 'quantizing', 'analyzing',
                    'cleaning', 'vectorizing', 'meshing', 'packaging', 'slicing',
                    'validating', 'generating', 'complete', 'failed', 'canceled', 'superseded'
                ) THEN RAISE(ABORT, 'unsupported job stage')
            END;
            SELECT CASE
                WHEN (NEW.state = 'queued' AND NEW.stage != 'queued')
                  OR (NEW.state = 'running' AND NEW.stage IN (
                      'queued', 'complete', 'failed', 'canceled', 'superseded'
                  ))
                  OR (NEW.state = 'succeeded' AND (NEW.stage != 'complete' OR NEW.progress != 1))
                  OR (NEW.state = 'failed' AND (NEW.stage != 'failed' OR NEW.error_code IS NULL))
                  OR (NEW.state = 'canceled' AND NEW.stage != 'canceled')
                  OR (NEW.state = 'superseded' AND NEW.stage != 'superseded')
                  OR (NEW.state IN ('succeeded', 'failed', 'canceled', 'superseded')
                      AND NEW.finished_at IS NULL)
                  OR (NEW.state IN ('queued', 'running') AND NEW.finished_at IS NOT NULL)
                  OR (NEW.state = 'canceled' AND NEW.canceled_at IS NULL)
                  OR (NEW.state != 'canceled' AND NEW.canceled_at IS NOT NULL)
                  OR (NEW.state != 'failed' AND NEW.error_code IS NOT NULL)
                THEN RAISE(ABORT, 'job state and stage are inconsistent')
            END;
        END
        """,
        """
        CREATE TRIGGER jobs_contract_update
        BEFORE UPDATE ON jobs
        BEGIN
            SELECT CASE
                WHEN NEW.job_type NOT IN (
                    'preview', 'geometry', 'export', 'validation', 'prompted_edit'
                ) THEN RAISE(ABORT, 'unsupported job type')
            END;
            SELECT CASE
                WHEN NEW.stage NOT IN (
                    'queued', 'ingesting', 'normalizing', 'quantizing', 'analyzing',
                    'cleaning', 'vectorizing', 'meshing', 'packaging', 'slicing',
                    'validating', 'generating', 'complete', 'failed', 'canceled', 'superseded'
                ) THEN RAISE(ABORT, 'unsupported job stage')
            END;
            SELECT CASE
                WHEN (NEW.state = 'queued' AND NEW.stage != 'queued')
                  OR (NEW.state = 'running' AND NEW.stage IN (
                      'queued', 'complete', 'failed', 'canceled', 'superseded'
                  ))
                  OR (NEW.state = 'succeeded' AND (NEW.stage != 'complete' OR NEW.progress != 1))
                  OR (NEW.state = 'failed' AND (NEW.stage != 'failed' OR NEW.error_code IS NULL))
                  OR (NEW.state = 'canceled' AND NEW.stage != 'canceled')
                  OR (NEW.state = 'superseded' AND NEW.stage != 'superseded')
                  OR (NEW.state IN ('succeeded', 'failed', 'canceled', 'superseded')
                      AND NEW.finished_at IS NULL)
                  OR (NEW.state IN ('queued', 'running') AND NEW.finished_at IS NOT NULL)
                  OR (NEW.state = 'canceled' AND NEW.canceled_at IS NULL)
                  OR (NEW.state != 'canceled' AND NEW.canceled_at IS NOT NULL)
                  OR (NEW.state != 'failed' AND NEW.error_code IS NOT NULL)
                THEN RAISE(ABORT, 'job state and stage are inconsistent')
            END;
        END
        """,
    ),
)


CALIBRATION_EVIDENCE_REGISTRY = Migration(
    version=16,
    name="tamper_evident_calibration_evidence_registry",
    statements=(
        """
        CREATE TABLE calibration_artifacts (
            id TEXT PRIMARY KEY,
            catalog_id TEXT NOT NULL,
            catalog_version TEXT NOT NULL,
            catalog_fingerprint TEXT NOT NULL CHECK (length(catalog_fingerprint) = 64),
            profile_id TEXT NOT NULL,
            profile_fingerprint TEXT NOT NULL CHECK (length(profile_fingerprint) = 64),
            artifact_fingerprint TEXT NOT NULL UNIQUE CHECK (length(artifact_fingerprint) = 64),
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE calibration_artifact_members (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES calibration_artifacts(id) ON DELETE RESTRICT,
            role TEXT NOT NULL CHECK (role IN ('coupon_svg', 'coupon_png')),
            ordinal INTEGER NOT NULL CHECK (ordinal = 0),
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            relative_path TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size > 0),
            media_type TEXT NOT NULL,
            extension TEXT NOT NULL,
            UNIQUE(artifact_id, role, ordinal),
            UNIQUE(artifact_id, filename)
        )
        """,
        """
        CREATE TABLE calibration_runs (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES calibration_artifacts(id) ON DELETE RESTRICT,
            catalog_id TEXT NOT NULL,
            catalog_version TEXT NOT NULL,
            catalog_fingerprint TEXT NOT NULL CHECK (length(catalog_fingerprint) = 64),
            profile_id TEXT NOT NULL,
            profile_fingerprint TEXT NOT NULL CHECK (length(profile_fingerprint) = 64),
            printer_id TEXT NOT NULL,
            nozzle_id TEXT NOT NULL,
            material_class TEXT NOT NULL,
            process_fingerprint TEXT NOT NULL CHECK (length(process_fingerprint) = 64),
            process_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE CHECK (length(record_sha256) = 64),
            evidence_sha256 TEXT NOT NULL UNIQUE CHECK (length(evidence_sha256) = 64),
            source_bundle_sha256 TEXT NOT NULL CHECK (length(source_bundle_sha256) = 64),
            source_bundle_relative_path TEXT NOT NULL UNIQUE,
            source_bundle_byte_size INTEGER NOT NULL CHECK (source_bundle_byte_size > 0),
            source_bundle_media_type TEXT NOT NULL
                CHECK (source_bundle_media_type = 'application/zip'),
            source_bundle_extension TEXT NOT NULL CHECK (source_bundle_extension = '.zip'),
            record_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            slicer_json TEXT NOT NULL,
            filament_json TEXT NOT NULL,
            attestation_json TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(id, evidence_sha256)
        )
        """,
        """
        CREATE TABLE calibration_run_members (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES calibration_runs(id) ON DELETE RESTRICT,
            role TEXT NOT NULL CHECK (role IN (
                'project_3mf', 'sliced_gcode', 'slicer_settings', 'photo'
            )),
            ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 31),
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            relative_path TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size > 0),
            media_type TEXT NOT NULL,
            extension TEXT NOT NULL,
            UNIQUE(run_id, role, ordinal),
            UNIQUE(run_id, filename)
        )
        """,
        """
        CREATE INDEX calibration_runs_profile_imported
        ON calibration_runs(profile_id, imported_at DESC, id DESC)
        """,
        """
        CREATE TRIGGER calibration_artifacts_are_immutable
        BEFORE UPDATE ON calibration_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'calibration artifacts are immutable');
        END
        """,
        """
        CREATE TRIGGER calibration_artifacts_reject_delete
        BEFORE DELETE ON calibration_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'calibration artifacts cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER calibration_artifact_members_are_immutable
        BEFORE UPDATE ON calibration_artifact_members
        BEGIN
            SELECT RAISE(ABORT, 'calibration artifact members are immutable');
        END
        """,
        """
        CREATE TRIGGER calibration_artifact_members_reject_delete
        BEFORE DELETE ON calibration_artifact_members
        BEGIN
            SELECT RAISE(ABORT, 'calibration artifact members cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER calibration_runs_are_immutable
        BEFORE UPDATE ON calibration_runs
        BEGIN
            SELECT RAISE(ABORT, 'calibration runs are immutable');
        END
        """,
        """
        CREATE TRIGGER calibration_runs_reject_delete
        BEFORE DELETE ON calibration_runs
        BEGIN
            SELECT RAISE(ABORT, 'calibration runs cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER calibration_run_members_are_immutable
        BEFORE UPDATE ON calibration_run_members
        BEGIN
            SELECT RAISE(ABORT, 'calibration run members are immutable');
        END
        """,
        """
        CREATE TRIGGER calibration_run_members_reject_delete
        BEFORE DELETE ON calibration_run_members
        BEGIN
            SELECT RAISE(ABORT, 'calibration run members cannot be deleted');
        END
        """,
    ),
)


CALIBRATION_CATALOG_PROMOTIONS = Migration(
    version=17,
    name="versioned_calibration_catalog_promotions",
    statements=(
        """
        CREATE TABLE calibration_catalog_versions (
            fingerprint TEXT PRIMARY KEY CHECK (length(fingerprint) = 64),
            catalog_id TEXT NOT NULL,
            catalog_version TEXT NOT NULL,
            catalog_json TEXT NOT NULL,
            parent_fingerprint TEXT REFERENCES calibration_catalog_versions(fingerprint)
                ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(catalog_id, catalog_version)
        )
        """,
        """
        CREATE TABLE calibration_catalog_head (
            singleton TEXT PRIMARY KEY CHECK (singleton = 'active'),
            fingerprint TEXT NOT NULL REFERENCES calibration_catalog_versions(fingerprint)
                ON DELETE RESTRICT,
            activated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE calibration_profile_proposals (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            process_fingerprint TEXT NOT NULL CHECK (length(process_fingerprint) = 64),
            base_catalog_fingerprint TEXT NOT NULL
                REFERENCES calibration_catalog_versions(fingerprint) ON DELETE RESTRICT,
            proposed_catalog_fingerprint TEXT NOT NULL CHECK (
                length(proposed_catalog_fingerprint) = 64
            ),
            algorithm_version TEXT NOT NULL,
            proposal_sha256 TEXT NOT NULL UNIQUE CHECK (length(proposal_sha256) = 64),
            proposal_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending', 'accepted', 'rejected')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            reviewed_at TEXT,
            reviewer TEXT,
            review_reason TEXT,
            CHECK (
                (state = 'pending' AND reviewed_at IS NULL AND reviewer IS NULL)
                OR
                (state != 'pending' AND reviewed_at IS NOT NULL AND reviewer IS NOT NULL)
            )
        )
        """,
        """
        CREATE TABLE calibration_proposal_contributions (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL REFERENCES calibration_profile_proposals(id)
                ON DELETE RESTRICT,
            run_id TEXT NOT NULL REFERENCES calibration_runs(id) ON DELETE RESTRICT,
            feature_id TEXT NOT NULL,
            feature_kind TEXT NOT NULL CHECK (
                feature_kind IN ('dot', 'hole', 'line', 'neck', 'gap')
            ),
            included INTEGER NOT NULL CHECK (included IN (0, 1)),
            exclusion_reason TEXT,
            observation_json TEXT NOT NULL,
            UNIQUE(proposal_id, run_id, feature_id)
        )
        """,
        """
        CREATE TABLE calibration_catalog_promotions (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL UNIQUE REFERENCES calibration_profile_proposals(id)
                ON DELETE RESTRICT,
            previous_fingerprint TEXT NOT NULL
                REFERENCES calibration_catalog_versions(fingerprint) ON DELETE RESTRICT,
            activated_fingerprint TEXT NOT NULL
                REFERENCES calibration_catalog_versions(fingerprint) ON DELETE RESTRICT,
            reviewer TEXT NOT NULL,
            activated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE INDEX calibration_profile_proposals_profile_created
        ON calibration_profile_proposals(profile_id, created_at DESC, id DESC)
        """,
        """
        CREATE TRIGGER calibration_catalog_versions_are_immutable
        BEFORE UPDATE ON calibration_catalog_versions
        BEGIN
            SELECT RAISE(ABORT, 'calibration catalog versions are immutable');
        END
        """,
        """
        CREATE TRIGGER calibration_catalog_versions_reject_delete
        BEFORE DELETE ON calibration_catalog_versions
        BEGIN
            SELECT RAISE(ABORT, 'calibration catalog versions cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER calibration_catalog_head_requires_promotion
        BEFORE UPDATE ON calibration_catalog_head
        WHEN NOT EXISTS (
            SELECT 1 FROM calibration_catalog_promotions
            WHERE previous_fingerprint = OLD.fingerprint
              AND activated_fingerprint = NEW.fingerprint
        )
        BEGIN
            SELECT RAISE(ABORT, 'calibration catalog head requires promotion history');
        END
        """,
        """
        CREATE TRIGGER calibration_catalog_head_reject_delete
        BEFORE DELETE ON calibration_catalog_head
        BEGIN
            SELECT RAISE(ABORT, 'calibration catalog head cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER calibration_terminal_proposals_are_immutable
        BEFORE UPDATE ON calibration_profile_proposals
        WHEN OLD.state != 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'reviewed calibration proposals are immutable');
        END
        """,
        """
        CREATE TRIGGER calibration_proposals_reject_delete
        BEFORE DELETE ON calibration_profile_proposals
        BEGIN
            SELECT RAISE(ABORT, 'calibration proposals cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER calibration_proposal_contributions_are_immutable
        BEFORE UPDATE ON calibration_proposal_contributions
        BEGIN
            SELECT RAISE(ABORT, 'calibration proposal contributions are immutable');
        END
        """,
        """
        CREATE TRIGGER calibration_proposal_contributions_reject_delete
        BEFORE DELETE ON calibration_proposal_contributions
        BEGIN
            SELECT RAISE(ABORT, 'calibration proposal contributions cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER calibration_catalog_promotions_are_immutable
        BEFORE UPDATE ON calibration_catalog_promotions
        BEGIN
            SELECT RAISE(ABORT, 'calibration catalog promotions are immutable');
        END
        """,
        """
        CREATE TRIGGER calibration_catalog_promotions_reject_delete
        BEFORE DELETE ON calibration_catalog_promotions
        BEGIN
            SELECT RAISE(ABORT, 'calibration catalog promotions cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER calibration_runs_require_retained_catalog
        BEFORE INSERT ON calibration_runs
        WHEN NOT EXISTS (
            SELECT 1 FROM calibration_catalog_versions
            WHERE fingerprint = NEW.catalog_fingerprint
              AND catalog_id = NEW.catalog_id
              AND catalog_version = NEW.catalog_version
        )
        BEGIN
            SELECT RAISE(ABORT, 'calibration run requires a retained exact catalog');
        END
        """,
        """
        CREATE TRIGGER calibration_artifacts_require_retained_catalog
        BEFORE INSERT ON calibration_artifacts
        WHEN NOT EXISTS (
            SELECT 1 FROM calibration_catalog_versions
            WHERE fingerprint = NEW.catalog_fingerprint
              AND catalog_id = NEW.catalog_id
              AND catalog_version = NEW.catalog_version
        )
        BEGIN
            SELECT RAISE(ABORT, 'calibration artifact requires a retained exact catalog');
        END
        """,
    ),
)


CALIBRATION_RUN_DRAFTS = Migration(
    version=18,
    name="resumable_calibration_run_drafts",
    statements=(
        """
        CREATE TABLE calibration_run_drafts (
            id TEXT PRIMARY KEY,
            catalog_fingerprint TEXT NOT NULL
                REFERENCES calibration_catalog_versions(fingerprint) ON DELETE RESTRICT,
            profile_id TEXT NOT NULL,
            artifact_fingerprint TEXT NOT NULL CHECK (length(artifact_fingerprint) = 64),
            artifact_json TEXT NOT NULL,
            record_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
            attested_at TEXT,
            attested_candidate_sha256 TEXT CHECK (
                attested_candidate_sha256 IS NULL OR length(attested_candidate_sha256) = 64
            ),
            finalized_run_id TEXT UNIQUE REFERENCES calibration_runs(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        """
        CREATE TABLE calibration_run_draft_members (
            id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES calibration_run_drafts(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (
                role IN (
                    'coupon_svg', 'coupon_png', 'project_3mf',
                    'sliced_gcode', 'slicer_settings', 'photo'
                )
            ),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0 AND ordinal <= 31),
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            relative_path TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size > 0),
            media_type TEXT NOT NULL,
            extension TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(draft_id, role, ordinal),
            UNIQUE(draft_id, filename)
        )
        """,
        """
        CREATE INDEX calibration_run_drafts_updated
        ON calibration_run_drafts(updated_at DESC, id DESC)
        """,
        """
        CREATE INDEX calibration_run_draft_members_owner
        ON calibration_run_draft_members(draft_id, role, ordinal)
        """,
        """
        CREATE TRIGGER finalized_calibration_drafts_are_immutable
        BEFORE UPDATE ON calibration_run_drafts
        WHEN OLD.finalized_run_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'finalized calibration drafts are immutable');
        END
        """,
        """
        CREATE TRIGGER finalized_calibration_draft_members_are_immutable
        BEFORE UPDATE ON calibration_run_draft_members
        WHEN EXISTS (
            SELECT 1 FROM calibration_run_drafts
            WHERE id = OLD.draft_id AND finalized_run_id IS NOT NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'finalized calibration draft members are immutable');
        END
        """,
        """
        CREATE TRIGGER finalized_calibration_draft_members_reject_delete
        BEFORE DELETE ON calibration_run_draft_members
        WHEN EXISTS (
            SELECT 1 FROM calibration_run_drafts
            WHERE id = OLD.draft_id AND finalized_run_id IS NOT NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'finalized calibration draft members cannot be deleted');
        END
        """,
    ),
)


MIGRATIONS = (
    INITIAL_SCHEMA,
    REVISION_INTEGRITY,
    DRAFT_BRANCHING,
    JOB_CONTRACT,
    JOB_SUPERSESSION,
    BUNDLE_IMPORTS,
    ARTIFACT_OWNERSHIP_UNIQUENESS,
    REVISION_PUBLICATION_EVIDENCE,
    SEALED_REVISION_HISTORY,
    DRAFT_COMMAND_HISTORY,
    DRAFT_HISTORY_LIFECYCLE,
    STARTUP_RECONCILIATION_HISTORY,
    MURAL_PLANS,
    PROMPTED_EDIT_ALTERNATIVES,
    PROMPTED_EDIT_JOBS,
    CALIBRATION_EVIDENCE_REGISTRY,
    CALIBRATION_CATALOG_PROMOTIONS,
    CALIBRATION_RUN_DRAFTS,
)
CURRENT_DATABASE_VERSION = MIGRATIONS[-1].version


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    connection.commit()


def apply_migrations(
    connection: sqlite3.Connection, migrations: tuple[Migration, ...] = MIGRATIONS
) -> int:
    _ensure_migration_table(connection)
    row = connection.execute("SELECT max(version) AS version FROM schema_migrations").fetchone()
    database_version = int(row[0] or 0)
    supported_version = migrations[-1].version if migrations else 0
    if database_version > supported_version:
        raise UnsupportedDatabaseVersionError(
            f"database schema {database_version} is newer than supported schema {supported_version}"
        )

    applied = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
    for migration in migrations:
        if migration.version in applied:
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) failed"
            ) from error
    return supported_version
