# Portable project bundles

Linear issue: `24K-25`

An Image23MF bundle is a deterministic ZIP archive containing canonical `manifest.json` plus
content-addressed source/artifact payloads. The manifest schema currently includes:

- project metadata and active revision;
- immutable revisions and ordered region operations;
- the current mutable draft and its generation;
- every draft history lineage, canonical state, node, active head, and abandoned redo branch;
- referenced source assets and selected derived artifacts;
- referenced filaments and custom presets;
- application/schema versions, relative payload paths, byte sizes, media types, and SHA-256 hashes.

Export verifies each workspace blob before writing. ZIP timestamps and member ordering are stable,
and publication uses a sibling temporary file, `fsync`, and atomic rename. Original filenames are
reduced to their basename; workspace or machine-absolute blob paths never enter the manifest.

Restore does not extract the ZIP. It rejects absolute, backslash, traversal, non-canonical,
duplicate, symlink, unlisted, missing, oversized, suspiciously compressed, or checksum-invalid
members, then streams verified members into content-addressed storage. Database restoration is one
transaction. Failure may leave safe unreferenced content blobs, but never a partial project.

On a clean workspace, stable project/revision IDs are retained. Existing assets and filaments are
deduplicated by content/catalog identity. ID, project-name, preset-name, and artifact-derivation
collisions are remapped without mutating existing records, and embedded source/filament references
are rewritten before their canonical configuration fingerprints are recomputed.

The default schema-v3 export mode is `full_history`. History identity is remapped with the project:
lineage and node IDs are collision-safe, and every state hash is recomputed with the production
canonical hasher after asset, filament, and revision IDs are rewritten. The imported draft must
match the imported cursor exactly, so Undo and Redo continue from the same position. Abandoned redo
descendants remain available as immutable audit evidence even though they are outside the active
tip path.

`current_state_only` is an explicit opt-in compatibility mode. Its manifest and restore result say
history was omitted, and import creates one exact, non-undoable `legacy_anchor`. Schema-v1/v2
bundles use the same visibly reported legacy path.

Successful imports record the complete bundle SHA-256. Repeating the same restore returns the prior
project without creating duplicate revisions or artifacts. A bundle restored into its source
workspace creates one isolated “(Imported)” copy and is idempotent thereafter.

Runtime history mutation receipts are not portable authorship records and are excluded. Immutable
node request IDs are preserved as audit identity; their uniqueness is scoped to the newly imported
project, so source-workspace collision imports remain isolated. Bundle-level repeat-import
idempotency remains the complete archive SHA-256, and later Undo/Redo requests create fresh local
receipts.

Manifest validation rejects tampered state hashes or byte sizes, duplicate identities, malformed
roots/depths, cycles, cross-lineage references, invalid active-head ancestry, and any cursor state
that disagrees with the materialized draft.

The project library exports `full_history` bundles by default and shows a reviewable restore report
before opening an import. The report distinguishes full-history restores from legacy anchors and
lists imported and abandoned audit-node counts. The HTTP import streams into a private temporary
file, rejects uploads above the separate 2 GiB compressed wire limit (including an early
`Content-Length` preflight), and always removes the temporary file. Archive validation then applies
the independent decompressed/member limits above.

Project bundles are the non-destructive import/copy format. Complete-workspace disaster recovery,
including explicit populated-target replacement and automatic rollback backup, is documented in
[workspace-backup-restore.md](workspace-backup-restore.md).
