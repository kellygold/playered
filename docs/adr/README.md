# Architecture decision records

ADRs describe decisions that constrain more than one feature or would be expensive to reverse.
They record context and consequences rather than duplicating the product backlog.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-modular-local-application.md) | Modular local application with React, FastAPI, and an HTTP-independent engine | Accepted |
| [0002](0002-sqlite-and-content-addressed-files.md) | SQLite metadata with content-addressed filesystem assets | Accepted |
| [0003](0003-label-field-is-the-print-contract.md) | One exhaustive label field is the processing/preview contract | Accepted |
| [0004](0004-cancelable-jobs-and-controlled-tools.md) | Cancelable local jobs and controlled external-tool adapters | Accepted |
| [0005](0005-geometry-and-bambu-validation-boundary.md) | Geometry difference reporting and Bambu CLI as final export proof | Accepted |

New ADRs use the next four-digit number. Superseded decisions remain in the repository and link to
their replacement.

