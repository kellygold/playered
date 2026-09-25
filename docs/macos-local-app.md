# Local macOS application

Image23MF Studio can run as a private, per-user macOS application without exposing the API to the
LAN and without coupling user data to a source checkout. This packaging is intentionally unsigned
and local for now. It is structured so signing, notarization, and a public update feed can be added
later without changing the workspace contract.

## Install and launch

Prerequisites are macOS, Python 3.9 or newer, Node/npm, and internet access when Python or npm must
download dependencies. From the repository:

```bash
make macos-install
open "$HOME/Applications/Image23MF Studio.app"
```

The installer builds a production Vite bundle and Python wheel in a temporary directory, creates
an immutable release-specific virtual environment, runs the release's database migrations, and
only then switches the stable application to it. The first launch asks for a workspace folder. If
the Finder chooser is cancelled, the safe default is:

```text
~/Documents/Image23MF Studio
```

To select a workspace non-interactively:

```bash
./packaging/macos/install.command --workspace "$HOME/Documents/My Image23MF Workspace"
```

Pass `--launch` to open the app after installation. Launching a second copy reuses the already
running loopback URL instead of starting a competing server.

## Runtime and network boundary

The `.app` starts one Uvicorn process bound to `127.0.0.1:8323`. There is deliberately no host
option in the application launcher. FastAPI registers `/api/*` first and then serves the immutable
production frontend as the final catch-all route, so the installed UI and API share one loopback
origin. No Vite development server is involved and users can never observe a partially written
frontend release.

The installed launcher records its PID, URL, version, start time, and selected workspace for local
diagnostics. The exclusive file lock is held for the server lifetime. Runtime state is removed on
a normal exit; a stale state file is harmless because the lock, rather than the file, establishes
ownership.

## Data layout

Application code and user data have separate lifecycles:

```text
~/Applications/Image23MF Studio.app/
~/Library/Application Support/Image23MF Studio/
  config.json                  selected workspace only
  current -> releases/<id>     atomically replaced symlink
  releases/<id>/
    manifest.json              version and wheel/frontend hashes
    venv/                      immutable Python runtime
    web/                       immutable production frontend
  runtime/                     lock, PID, and running-version state
~/Library/Logs/Image23MF Studio/
  studio.log                   5 MiB rotating log, three archives

<selected workspace>/
  image23mf.sqlite3            relational metadata, migrations, jobs, revisions
  blobs/ and project files     content-addressed images and generated artifacts
  backups/upgrades/            pre-migration SQLite backups
```

SQLite stores structured metadata; source images and large generated artifacts remain files in the
workspace/content-addressed store. The database points to them. This avoids database bloat while
preserving transactional project relationships and portable workspace backup behavior.

Use the packaged status command to diagnose the installation:

```bash
"$HOME/Library/Application Support/Image23MF Studio/current/venv/bin/image23mf-macos" status
```

The JSON output includes the installed manifest, workspace, log path, and live server state. The
release manifest hashes both the wheel and the complete frontend tree, preventing the same release
identifier from silently referring to different code.

The same installed environment exposes whole-workspace disaster recovery without requiring a
source checkout. Quit Image23MF Studio before an explicit replacement, then use:

```bash
RECOVERY="$HOME/Library/Application Support/Image23MF Studio/current/venv/bin/image23mf-workspace"
"$RECOVERY" backup --workspace "$HOME/Documents/Image23MF Studio" \
  --output "$HOME/Documents/Image23MF-backup.image23mf-workspace"
"$RECOVERY" preflight --bundle "$HOME/Documents/Image23MF-backup.image23mf-workspace" \
  --target "$HOME/Documents/Image23MF Restored"
"$RECOVERY" restore --bundle "$HOME/Documents/Image23MF-backup.image23mf-workspace" \
  --target "$HOME/Documents/Image23MF Restored"
"$RECOVERY" reveal-workspace --workspace "$HOME/Documents/Image23MF Restored"
```

`restore` defaults to empty targets. `--choice replace` is required for a populated target, creates
a rollback archive first, and refuses the active packaged workspace while the app lock is held.

## Update and migration guarantees

Running `make macos-install` again creates a unique local release identifier. An update follows
this order:

1. Build the wheel and production frontend outside the active installation.
2. Create a new immutable release directory and install all Python dependencies there.
3. Validate the release manifest, Python runtime, and frontend entry point.
4. Gracefully stop a running old server so two application schemas can never write one workspace.
5. If a workspace database exists, use SQLite's online backup API to create a consistent backup.
   This is safe with WAL mode; copying only the `.sqlite3` file would not be.
6. Run migrations with the staged release while `current` still points at the old release.
7. If migration fails, restore the SQLite backup, delete the failed staged release, and leave the
   old release active.
8. Atomically replace the `current` symlink and write the stable `.app` launcher. Pass `--launch`
   when an immediate restart is desired.

This is a forward-migration rollback, not a general data time machine. Backups are deliberately
kept under the workspace after a successful update so they are covered by the user's workspace
backup policy. A failed update never activates its code.

## Uninstall

```bash
make macos-uninstall
```

Uninstall stops the local process and removes the `.app`, immutable releases, launcher preferences,
runtime state, and application logs. It never removes the selected workspace, its SQLite database,
source images, exports, or upgrade backups—even when a user deliberately selected a folder nested
under Application Support. If preferences are corrupt and the installer cannot identify the
workspace, it removes only known application-owned paths and leaves every unknown directory alone.

Delete a preserved workspace manually only after independently confirming it is no longer needed.

For checksummed whole-workspace backup, preflight, rollback-backed replacement, restore reports,
and the separate non-destructive project-import behavior, see
[workspace-backup-restore.md](workspace-backup-restore.md).

## Verification

Hermetic tests exercise clean installation, workspace selection, production frontend/API serving,
loopback-only binding, duplicate launch, version/log runtime evidence, same-version hash rejection,
successful upgrade, failed migration with byte-safe SQLite restoration, and uninstall preservation:

```bash
.venv/bin/pytest -q tests/test_macos_lifecycle.py tests/test_macos_launcher.py
```

For a real-machine smoke test, install with a disposable workspace, launch the app, confirm
`image23mf-macos status`, create/reopen a project, update once, then uninstall and verify the
workspace remains. Public distribution still requires an Apple Developer identity, hardened
runtime signing, notarization, a signed update manifest, and clean macOS VM coverage.
