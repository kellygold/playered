# ADR 0002: SQLite and content-addressed files

- Status: Accepted
- Date: 2026-07-16
- Linear: 24K-6, 24K-32

## Context

Projects need queryable metadata, immutable revisions, recoverable jobs, and artifact provenance.
Originals, previews, masks, meshes, and 3MFs are large binary objects that should remain easy to
inspect, back up, hash, and restore.

## Decision

- SQLite stores relational metadata and canonical JSON configuration, not image/mesh BLOBs.
- WAL, foreign keys, busy timeout, UTC timestamps, and forward-only migrations are mandatory.
- Immutable source/artifact bytes use SHA-256-sharded relative paths in the workspace.
- Publication writes a temporary file, fsyncs, atomically renames, then commits references.
- Revisions are append-only after publication. Drafts may be replaced.
- Portable bundles contain manifests and relative paths; absolute machine paths are forbidden.
- Repository and blob-store interfaces isolate domain code from the concrete local implementation.

## Consequences

The workspace is understandable and portable without inflating or locking the database around
large writes. Transactional coordination and explicit garbage collection require careful tests.

