# Self-contained Mac desktop beta

Target: Apple Silicon macOS 14+, a signed/notarized app with an AppKit/WKWebView
window and the existing React UI. A PyInstaller directory bundles Python 3.12,
NumPy/Pillow/Triangle and the existing local engine. Bambu Studio remains an
external prerequisite. No Python, Node, Terminal, hosted conversion service or
new native processing implementation is required on the user's machine.

The original development checkout and its workspace remain unchanged. Work starts
from sanitized source `ee4ff3d`; public source/release publication follows final
validation. A built or signed candidate alone is not a release-readiness claim.

## Scope and acceptance

- [ ] Freeze the engine and prove native extensions and spawned workers work.
- [ ] Pin build dependencies; include runtime licenses and corresponding required sources.
- [ ] Native window: startup/error state, file pickers, save/download, Edit menu,
      keyboard shortcuts, external-link handling and workspace reveal.
- [ ] Loopback ephemeral port, per-launch session cookie, origin/host checks,
      single-instance lock and no remote navigation or Node/JS native bridge.
- [ ] Quit confirmation during jobs, graceful worker cancellation, crash/parent
      cleanup, reopen and persistent data outside the app.
- [ ] Fresh-machine app-copy/install proof on M2 Pro without developer runtimes.
- [ ] Real UI import, preview, 3MF export and Bambu open; negative and recovery proof.
- [ ] Complete regression gates and scoped independent final review.
- [ ] Sign nested code, notarize/staple and verify the exact distributable artifact.
- [ ] Publish a versioned beta and verified Burner Tools entry only after gates pass.

Manual updates initially replace the .app after quitting. Saved projects remain
outside it; database backups precede migration. Automatic updates, Intel/Windows,
App Store distribution, a SwiftUI rewrite and new printer profiles are deferred.
