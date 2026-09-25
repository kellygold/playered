# Release quality gate

Run the complete local release matrix with the API, Vite frontend, and Chrome remote debugging
already available on the standard development ports:

```bash
make release-gate
```

The command writes a machine-readable report, a concise Markdown report, per-gate logs, and fresh
performance evidence under `workspace/qa/release-gate/`. It returns `1` for an automated failure or
skipped gate. Readiness means the free, source-installed local-web beta; physical printer
certification is not claimed. `make release-audit` retains the same software checks.

Use a focused run while repairing one gate:

```bash
.venv/bin/python scripts/release_gate.py --only risk-overlay-fit-and-zoom --audit
.venv/bin/python scripts/release_gate.py --only performance-budgets --audit
```

`--skip-browser` is for headless diagnostics only. Skipped browser checks remain explicit failures of
the automated matrix. `--list` prints the current matrix and blockers without executing anything.

The matrix deliberately names two high-risk user journeys:

- **Risk overlay Fit 100% and zoom 125%** proves raster, canvas, plane, and exact risk masks retain
  the same bounds through replacement and revision lifecycle changes.
- **Preview → Build → Download discoverability** proves a clean first run can find the sample,
  understand the stages, build a validated package, and download a non-empty 3MF.

Startup recovery runs in a disposable seeded workspace with its own API and Vite ports. The gate
proves the interrupted packaging job, cleanup dry run, modal keyboard behavior, and byte
preservation in Chrome without mutating the normal developer workspace.

The performance gate runs both committed budget families. The general production suite covers
startup, image processing, vectorization, geometry, and packaging. The mural suite separately
times exact 1024×1024 fractional topology partitioning into a 3×2 layout and fails closed above a
15-second median, 18-second p95, or 300 MiB process RSS. Fresh evidence is written below the release
run directory; committed baselines and ceilings are never rewritten by verification.

Physical P2S output is never inferred from software or slicer evidence. Prior pipeline
prints are user-reported experience; a prescribed two-nozzle specimen campaign is optional
characterization, not a beta-release blocker. The report keeps physical certification separate
and does not change unobserved specimen records to passed.
