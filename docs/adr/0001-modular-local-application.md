# ADR 0001: Modular local application

- Status: Accepted
- Date: 2026-07-16
- Linear: 24K-19

## Context

Image processing and geometry are strongest in Python, while the required interactive canvas,
sliders, warnings, and history need a durable browser UI. The first release is private and local,
but processing must remain reusable from tests and future CLI/batch surfaces.

## Decision

Use a modular monolith:

- React and TypeScript own browser interaction and presentation.
- FastAPI owns the loopback HTTP boundary, resource orchestration, and job lifecycle.
- `image23mf.engine` and related domain packages have no FastAPI or React dependency.
- Production packaging may serve the built client from the Python application, but development
  keeps Vite and FastAPI separate with a proxy.
- Configuration contracts are versioned in Python and mirrored in TypeScript until generation is
  introduced.

## Consequences

This adds two toolchains but keeps the color/geometry implementation testable without a browser.
It avoids a premature desktop shell or distributed services. Cloud deployment is not assumed.

