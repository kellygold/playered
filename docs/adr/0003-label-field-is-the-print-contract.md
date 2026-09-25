# ADR 0003: The label field is the print contract

- Status: Accepted
- Date: 2026-07-16
- Linear: 24K-10, 24K-50

## Context

Prior scripts independently changed color masks and later vectorization, which could create gaps,
overlap, hollow rings, and previews that did not describe the geometry actually exported.

## Decision

- The final processed raster is one integer label field.
- Every pixel has exactly one valid palette label.
- Per-color masks are derived views and are never independently authoritative.
- Cleanup and manual operations transform labels while preserving exhaustive, disjoint coverage.
- The Processed UI view renders directly from that final field.
- Vector/mesh simplification produces a separate Geometry preview and difference report.
- Risks use stable region IDs derived from labels and active physical profile.

## Consequences

Color ownership and manual operations become unambiguous. Some topology operations are harder than
editing masks independently, but the system can prove what changed and prevent invisible gaps.

