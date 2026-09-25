# Benchmark baseline: baseline-2026-07-17-production-package

Generated: 2026-07-16T17:45:47.273363Z
Commit: `a73fae9`
Machine: Darwin arm64, Python 3.9.6

These are measurements, not promises. Initial budgets are derived only after this run.

| Case | Implementation | Status | Median | p95 | Peak Python | Output |
|---|---|---:|---:|---:|---:|---:|
| startup-cold | current SQLite/FastAPI startup | passed | 79.27 ms | 99.25 ms | 1.5 MiB | 304.0 KiB |
| openapi-cold | current FastAPI/Pydantic OpenAPI generation | passed | 661.06 ms | 675.94 ms | 9.0 MiB | 153.3 KiB |
| project-reopen-warm | current SQLite repositories | passed | 0.99 ms | 1.06 ms | 25.6 KiB | 656 B |
| preview-cold-wager | production chunked CIELAB classifier | passed | 424.47 ms | 428.81 ms | 32.7 MiB | 4.0 MiB |
| preview-warm-wager | production chunked CIELAB classifier | passed | 372.16 ms | 377.43 ms | 32.6 MiB | 4.0 MiB |
| auto-palette-wager | production sampled CIELAB k-means and chunked classifier | passed | 498.98 ms | 1297.50 ms | 32.6 MiB | 4.0 MiB |
| vectorization-potrace | installed Potrace adapter | passed | 33.75 ms | 34.56 ms | 76.9 KiB | 5.7 KiB |
| geometry-openscad | installed OpenSCAD reference adapter | passed | 149.20 ms | 153.52 ms | 77.0 KiB | 9.2 MiB |
| packaging-production-3mf | production deterministic Bambu 3MF writer and reader | passed | 377.81 ms | 383.42 ms | 11.9 MiB | 47.1 KiB |
