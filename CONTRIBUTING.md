# Contributing

Use Python 3.12 and Node 22.14+ on macOS. Run `make bootstrap`, then `make api`
and `make web` in separate terminals. Run `make check` before proposing a change.
The app is a local web interface and Python service; do not expose its API publicly.

Keep pull requests focused and describe the user-visible behavior, verification
performed, and any checks that could not run. Use issues and pull requests for
public coordination; access to a private planning system is not required.

Use generated or independently redistributable artwork in tests and screenshots.
Do not commit personal photos, purchased models, workspace databases, credentials,
or private project exports. The public regression fixture generator records its
provenance and hashes; changing fixtures requires reviewing the protected behavior.

Software checks run in CI. Installed Bambu Studio, real-browser workflows, and
hardware-specific performance checks are separate local evidence. Do not replace
an unexecuted check with a claim that it passed. Physical prints are welcome user
feedback, not a mandatory two-nozzle certification exercise for every contribution.
