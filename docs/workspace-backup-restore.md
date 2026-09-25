# Whole-workspace backup and disaster recovery

Image23MF has two deliberately different portability tools:

- A **project bundle** (`.image23mf`) imports one project into the current workspace. Existing
  records are preserved; colliding IDs and names are remapped and the import becomes an isolated
  copy. See [project-bundles.md](project-bundles.md).
- A **workspace bundle** (`.image23mf-workspace`) is disaster recovery for the complete workspace.
  It carries a consistent SQLite snapshot plus every regular workspace file except live SQLite
  sidecars and the explicitly ephemeral `temp/` directory. Restoring it replaces the target as one
  unit; it does not merge databases.

Use project import to move or duplicate artwork. Use workspace restore when recovering an entire
installation or moving all projects, filaments, presets, revision history, retained artifacts, and
user-owned files together.

## Backup

```bash
image23mf-workspace backup \
  --workspace "$HOME/Documents/Image23MF Studio" \
  --output "$HOME/Documents/Image23MF-backup.image23mf-workspace"
```

Backup uses SQLite's online backup API, so WAL state is included consistently without copying a
live `-wal` file. The standalone database snapshot is normalized out of WAL mode and checked with
`PRAGMA quick_check`. The service then:

1. walks without following symlinks and rejects special files;
2. hashes every member with SHA-256;
3. proves every asset/artifact row points at a member with the same hash and byte count;
4. writes a closed manifest and canonical paths (`payload/<workspace-relative-path>`);
5. hashes each source again while streaming it into ZIP, detecting concurrent file changes;
6. `fsync`s a sibling temporary archive and atomically renames it over the selected output.

The destination must be outside the source workspace. Sensitive staged databases, launcher state,
logs, and automatic upgrade backups use user-only file permissions.

## Checksum preflight

```bash
image23mf-workspace preflight \
  --bundle "$HOME/Documents/Image23MF-backup.image23mf-workspace" \
  --target "$HOME/Documents/Image23MF Restored"
```

The JSON report includes archive SHA-256 and size, application/database schema versions, member and
payload totals, project/asset/artifact counts, target state (`missing`, `empty`, or `populated`),
available choices, conservative free-space requirements, and warnings.

Preflight rejects unreadable archives, newer schemas, unsafe or duplicate paths, encrypted,
directory, symlink and special members, missing/unlisted data, checksum/count mismatches, bounded
size/member/expansion-ratio violations, SQLite corruption, and database references not closed over
the payload set. No archive member is extracted by its ZIP path; restore constructs each output
under a private sibling stage only after validating its canonical relative path.

## Restore choices

The default is non-destructive:

```bash
image23mf-workspace restore --bundle ./backup.image23mf-workspace --target ./restored
```

`empty_only` succeeds only when the target is absent or empty. On a populated target it returns a
conflict report and changes nothing. Choose another folder or use a project bundle to import a copy.

Whole-workspace replacement is intentionally explicit:

```bash
image23mf-workspace restore \
  --bundle ./backup.image23mf-workspace \
  --target "$HOME/Documents/Image23MF Studio" \
  --choice replace
```

Quit Image23MF Studio before replacing its active workspace. Replacement:

1. Re-runs complete preflight and requires conservative free disk space.
2. Creates a checksummed rollback workspace bundle beside the populated target.
3. Re-verifies the incoming archive to close the preflight/change race.
4. Materializes into a private sibling directory with `0700` directories and `0600` files.
5. Migrates and `quick_check`s staged SQLite, verifies counts, and rehashes referenced payloads.
6. Writes a restore report into the staged workspace.
7. Confirms staging and target are on one filesystem.
8. Renames the old target aside, atomically renames the verified stage into place, and `fsync`s the
   parent directory.
9. Moves the prior target back if activation fails. The rollback archive remains after success.

The restore command also checks the packaged application's exclusive runtime lock. If the running
app owns the target workspace, replacement is refused before a rollback archive or stage is
created. A held lock with unreadable runtime state fails closed and instructs you to quit the app;
a stale state file without a held lock does not block recovery.

Reports under `.image23mf/restore-reports/` record the input checksum, source counts, target state,
explicit choice, rollback archive, restored totals, database schema, and warnings.

If activation and automatic directory rollback both fail, the error names every surviving previous
and staged directory plus the rollback archive. Do not delete anything. Quit Image23MF, copy both
directories, then move the previous directory back to the expected target or restore the rollback
archive into a new empty folder.

## macOS permissions and Finder

Permission errors identify the denied path and direct the user to **System Settings > Privacy &
Security > Files and Folders** (or Full Disk Access) or a different writable folder. Failed backup
publication preserves the prior backup; restore does not touch the target before final activation.

Finder reveal verifies the entire backup and invokes `/usr/bin/open -R -- <path>` through the
no-shell tool runner. Paths never become shell source. Non-macOS callers receive an unsupported
result. The workspace itself can be revealed only after its directory, SQLite database,
`quick_check`, and schema are validated:

```bash
image23mf-workspace reveal --bundle ./backup.image23mf-workspace
image23mf-workspace reveal-workspace --workspace "$HOME/Documents/Image23MF Studio"
```

## Verification

The end-to-end suite covers empty and populated targets, explicit rollback-backed replace, restoring
the rollback archive, reports and CLI JSON, corruption, missing/malformed/unsafe/duplicate/symlink
members, expansion bombs, future schemas, source symlinks, report failure, activation rollback,
double-swap recovery instructions, permissions, and exact Finder argv:

```bash
.venv/bin/pytest -q tests/test_workspace_bundles.py
```
