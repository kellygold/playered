# Benchmark baseline: baseline-2026-07-16-palette

Generated: 2026-07-15T21:00:20.959684Z
Commit: `009df39`
Machine: Darwin arm64, Python 3.9.6

These are measurements, not promises. Initial budgets are derived only after this run.

| Case | Implementation | Status | Median | p95 | Peak Python | Output |
|---|---|---:|---:|---:|---:|---:|
| startup-cold | current SQLite/FastAPI startup | passed | 227.16 ms | 247.80 ms | 3.3 MiB | 232.4 KiB |
| project-reopen-warm | current SQLite repositories | passed | 0.80 ms | 0.83 ms | 25.6 KiB | 656 B |
| preview-cold-wager | production chunked CIELAB classifier | passed | 410.60 ms | 415.20 ms | 32.7 MiB | 4.0 MiB |
| preview-warm-wager | production chunked CIELAB classifier | passed | 359.72 ms | 363.11 ms | 32.6 MiB | 4.0 MiB |
| auto-palette-wager | production sampled CIELAB k-means and chunked classifier | passed | 434.40 ms | 444.79 ms | 32.6 MiB | 4.0 MiB |
| vectorization-potrace | installed Potrace adapter | passed | 28.58 ms | 31.84 ms | 76.8 KiB | 5.7 KiB |
| geometry-openscad | installed OpenSCAD reference adapter | passed | 123.13 ms | 141.54 ms | 76.9 KiB | 9.2 MiB |
| packaging-reference | reference ZIP packaging (pre-3MF writer) | passed | 605.40 ms | 623.26 ms | 32.6 MiB | 7.3 MiB |
