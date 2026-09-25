# Local storage schema

Linear issue: `24K-32`

The workspace database is `workspace/image23mf.sqlite3` by default. SQLite stores metadata and
canonical JSON; binary sources and generated artifacts will be content-addressed files introduced
under `24K-23`.

## Runtime guarantees

- foreign keys enabled on every connection;
- WAL journal mode for local read/write concurrency;
- five-second busy timeout;
- forward-only, transactional migrations;
- refusal to open a schema newer than the running application;
- UTC ISO timestamps;
- no image, mesh, or 3MF BLOB columns.

## Tables

- `schema_migrations`: applied database migrations.
- `projects`: user projects, active revision, archive state, and preferences.
- `assets`: immutable original-file metadata and relative content-addressed paths.
- `revisions`: immutable published configuration linked to a source and optional parent.
- `project_drafts`: replaceable autosave state with a monotonic generation, canonical config
  fingerprint, base revision, and ordered operation history.
- `jobs`: preview/geometry/export/validation state, progress, errors, and cancellation.
- `artifacts`: derived previews, masks, vectors, meshes, 3MFs, and reports.
- `filaments`: owned/custom physical colors with manufacturer, family, name, canonical hex,
  material, finish, catalog provenance, and timestamps.
- `presets`: versioned cleanup/printer/geometry presets.
- `region_operations`: ordered automatic, manual, or model-backed revision operations.

Published revision rows are immutable. Database triggers also reject cross-project parent links and
active-revision pointers that do not resolve to a revision owned by the same project; these
invariants hold even for writes outside the typed repository layer.

Autosave clients use compare-and-swap generations so a delayed debounce response cannot overwrite
newer slider state. Publishing a draft validates its fingerprint, creates a child of its selected
base revision, copies ordered operations to immutable revision history, updates the project's active
revision, and removes that exact draft generation in one transaction. A failed publish leaves the
draft available for recovery.

Filament writes are normalized through a typed repository. Exact physical identities cannot be
duplicated by casing changes, catalog imports are atomic and idempotent, and deletion scans both
mutable drafts and immutable revisions. Draft save and revision publication validate every optional
palette filament reference inside the same write transaction. Bundle restore inserts/remaps its
closed filament set before inserting rewritten configurations.

IDs are application-generated opaque strings. Paths are always relative to the configured
workspace and are never accepted directly from an API client.

Artifact derivation keys are unique within their owning job or revision, not globally. Identical
work in two projects therefore creates independent durable artifact records while reusing the same
verified content-addressed bytes. This preserves job/revision ownership without duplicating payloads.

## Content-addressed files

`ContentAddressedStore` streams bytes into `workspace/temp`, hashes them with SHA-256, fsyncs the
temporary file, and atomically publishes to:

```text
assets/ab/<sha256>.<ext>
artifacts/cd/<sha256>.<ext>
cache/ef/<sha256>.<ext>
```

Duplicate bytes reuse the immutable target after re-verifying size and hash. A corrupted existing
target raises a collision error rather than being silently replaced. Namespace, extension, and
relative-path validation prevent path traversal. Database publication remains a separate
transactional repository concern under `24K-21`.
