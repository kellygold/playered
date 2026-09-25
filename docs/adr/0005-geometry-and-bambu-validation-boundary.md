# ADR 0005: Geometry and Bambu validation boundary

- Status: Accepted
- Date: 2026-07-16
- Linear: 24K-8, 24K-11

## Context

A valid PNG, SVG, mesh, or ZIP is not proof that Bambu Studio will preserve thin colors, map every
filament, or avoid floating regions. Conversely, Bambu can override embedded process settings with
the active preset.

## Decision

- Potrace is the initial binary vectorizer adapter, not the domain model.
- Vector and mesh outputs are rasterized back through the canonical transform and compared with the
  label field before export.
- Geometry validation checks bounds, winding, watertightness, degeneracy, connectivity, overlap,
  and thickness.
- The 3MF packager is independently implemented with golden package fixtures.
- Bambu Studio CLI is the final validation oracle: successful slicing and expected color presence
  are required for a Validated result.
- Persisted Bambu settings are treated as tested metadata/hints, not assumed process control.

## Consequences

Export takes longer and requires Bambu Studio for the highest assurance, but the product can name
the stage that changed or lost detail rather than blaming an opaque “rasterization” step.

