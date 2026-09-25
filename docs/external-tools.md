# External tool boundary

Linear issue: `24K-22`

Potrace, OpenSCAD, and Bambu Studio are discovered through versioned `ToolSpec` records. Detection
reports separate `detected`, `compatible`, and usable `available` states, an absolute executable
path, parsed version, purpose, and actionable reason when unavailable. Bambu Studio is allowed to
return a non-zero status from `--version` because current releases print their version and usage
before rejecting that option; its output must still match the expected version pattern.

All invocation goes through `ToolRunner`:

- executable and arguments are an array passed with `shell=False`;
- executable paths must resolve to an executable file;
- NUL bytes and invalid environment overrides are rejected;
- callers may redact sensitive argument positions from retained command diagnostics;
- stdout and stderr are captured separately to files and bounded before loading into memory;
- the process runs in a new process group and timeout/cancellation terminates descendants;
- automatic working directories and capture files are cleaned after success or failure;
- return code, bounded logs, duration, timeout, and cancellation state are retained.

Feature adapters should create an explicit `temporary_workspace()` when their output files must be
inspected or published after execution. Only validated artifacts move into content-addressed
storage; temporary paths never become domain identifiers or API inputs.

The tool registry currently requires Potrace 1.16, OpenSCAD 2021.01, and Bambu Studio 2.0 or newer.
Version minimums and arguments are data, so a compatibility change does not alter process-safety
code.
