# Benchmark baseline: baseline-2026-07-17-split-startup

Generated: 2026-07-16T17:37:39.157495Z
Commit: `6c107ee`
Machine: Darwin arm64, Python 3.9.6

These are measurements, not promises. Initial budgets are derived only after this run.

| Case | Implementation | Status | Median | p95 | Peak Python | Output |
|---|---|---:|---:|---:|---:|---:|
| startup-cold | current SQLite/FastAPI startup | passed | 76.27 ms | 77.54 ms | 1.5 MiB | 304.0 KiB |
| openapi-cold | current FastAPI/Pydantic OpenAPI generation | passed | 684.05 ms | 685.53 ms | 9.0 MiB | 153.3 KiB |
| project-reopen-warm | current SQLite repositories | passed | 0.97 ms | 1.05 ms | 25.6 KiB | 656 B |
| preview-cold-wager | production chunked CIELAB classifier | passed | 422.84 ms | 427.66 ms | 32.7 MiB | 4.0 MiB |
| preview-warm-wager | production chunked CIELAB classifier | passed | 388.84 ms | 398.21 ms | 32.6 MiB | 4.0 MiB |
| auto-palette-wager | production sampled CIELAB k-means and chunked classifier | passed | 447.46 ms | 450.88 ms | 32.6 MiB | 4.0 MiB |
| vectorization-potrace | installed Potrace adapter | passed | 33.07 ms | 33.29 ms | 76.9 KiB | 5.7 KiB |
| geometry-openscad | installed OpenSCAD reference adapter | passed | 140.78 ms | 146.26 ms | 77.0 KiB | 9.2 MiB |
| packaging-reference | reference ZIP packaging (pre-3MF writer) | passed | 630.53 ms | 646.13 ms | 32.6 MiB | 7.3 MiB |
