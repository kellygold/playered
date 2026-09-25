# ADR 0004: Cancelable jobs and controlled tools

- Status: Accepted
- Date: 2026-07-16
- Linear: 24K-18, 24K-22, 24K-24

## Context

Quantization, topology, meshing, and slicing can outlive an HTTP request. Slider changes can rapidly
supersede older work. Potrace, OpenSCAD, and Bambu Studio are external processes that can fail or
hang and must not receive shell-interpreted input.

## Decision

- Preview, geometry, and validation execute as jobs with explicit stages and terminal states.
- The API returns identifiers and progress; request handlers do not perform long CPU work.
- New preview requests may supersede older jobs; stale results cannot publish as current.
- External adapters use argument arrays, controlled temporary directories, captured logs, timeouts,
  cancellation, and detected version/path metadata.
- A process executor is sufficient initially; the interface permits a durable local queue later.

## Consequences

The UI can remain responsive and diagnostics are reproducible. Job recovery and atomic artifact
publication become core requirements instead of polish.

