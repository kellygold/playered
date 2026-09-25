# PLAyered for Mac

## Install and use

The desktop app is for Apple Silicon Macs (M1 or newer). The build targets
macOS 14 and newer; release validation currently covers macOS 26 on M2 Pro and
M5 Max. Older macOS versions are not yet validated. Intel and Windows desktop
packages are not included.

1. Install Bambu Studio 2.8 or newer in Applications and open it once to finish its setup.
2. Download the versioned `PLAyered-0.1.0-arm64.zip` release and unzip it.
3. Move **PLAyered.app** to Applications, then open it.
4. Choose a folder for saved projects. Keep this folder when updating the app.
5. Select **Try guided sample**, or import a PNG, JPEG or WebP. Review dimensions,
   colors and detail cleanup, then build and download the validated 3MF.
6. Open the 3MF in Bambu Studio and review its plate and print settings.

No Python, Node.js, API key, account or conversion server is needed for the core
workflow. The interface talks to a private service on your own Mac. Bambu Studio
is required for validated export; the validated export profile supports Bambu P2S, 0.2/0.4 mm nozzles,
Textured PEI Plate and the supported PLA Basic/Matte profiles. Processing a large
or noisy image can still take several minutes.

The app asks before quitting with active builds or downloads. Quit it normally
before moving or replacing the application. Interrupted work may need rebuilding.

## Updates, saved work and troubleshooting

Updates are manual: quit, replace the app in Applications, and reopen. Keep a
workspace backup before updating; see [backup and restore](workspace-backup-restore.md).
The application binary contains no user projects. Saved projects live in the
folder you chose. Application settings and migration backups live under
`~/Library/Application Support/Image23MF Studio`; logs live under
`~/Library/Logs/Image23MF Studio`. The Project menu reveals the workspace and logs.

If startup fails, quit and reopen, and check that the workspace is still available
and writable. A second running PLAyered instance must be closed first. If Bambu
validation fails, confirm Bambu Studio is installed and opens normally. Keep your
original project and inspect the error before retrying.

Removing the app does not delete projects. Delete its settings or chosen workspace
only if you deliberately want to remove that saved data. The app does not install
a login item or a permanently running background service.

## Build from source

On an Apple Silicon Mac with Python 3.12, Node.js 22.14+ and Xcode installed:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r packaging/macos/desktop-requirements.lock
.venv/bin/python -m pip install --no-deps -e .
cd frontend
npm ci
cd ..
.venv/bin/python scripts/build_desktop.py
```

The output is `dist/macos/PLAyered.app` and a ZIP plus checksum. The
script compiles the included Triangle source, freezes the engine, builds the web
assets, compiles the Swift window and signs the result. Default ad-hoc signing is
for local builds. For distribution use `--identity 'Developer ID Application: …'`,
then notarize and staple the app, recreate the ZIP, and update its checksum.
Signing credentials are local release infrastructure and are never stored here.
`--skip-engine` is an iteration shortcut, not a release build mode.

For an isolated real-engine test, with Bambu Studio installed:

```sh
.venv/bin/python scripts/desktop_smoke.py 'dist/macos/PLAyered.app' \
  --output workspace/desktop-smoke-new
```

The test uses synthetic images, a fresh workspace and a minimal system PATH. It
checks access isolation, import, preview, geometry, validated export, shutdown and
saved-project recovery. Its receipt identifies the build commit and exported file.
It does not replace native window, file-picker or download testing.

Dependency notices and corresponding Triangle source are inside the app under
`Contents/Resources/LICENSES`; Help → Licenses and Notices opens the overview.
See [third-party terms](../THIRD_PARTY_NOTICES.md) before redistribution.
