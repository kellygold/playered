# Benchmark baseline: baseline-2026-09-24-public-fixtures

Generated: 2026-09-24T00:32:44.587975Z
Commit: `e84136fdd1c3bb552030b0af2d29703fb89be1ab`
Machine: Darwin arm64, Python 3.9.6

These are measurements, not promises. Initial budgets are derived only after this run.

| Case | Implementation | Status | Median | p95 | Peak Python | Output |
|---|---|---:|---:|---:|---:|---:|
| startup-cold | current SQLite/FastAPI startup | passed | 217.86 ms | 233.04 ms | 4.3 MiB | 548.0 KiB |
| openapi-cold | current FastAPI/Pydantic OpenAPI generation | passed | 1356.91 ms | 1373.80 ms | 16.6 MiB | 270.0 KiB |
| project-reopen-warm | current SQLite repositories | passed | 1.51 ms | 1.59 ms | 25.6 KiB | 656 B |
| preview-cold-stress | production chunked CIELAB classifier | passed | 441.02 ms | 449.70 ms | 32.7 MiB | 4.0 MiB |
| preview-warm-stress | production chunked CIELAB classifier | passed | 409.47 ms | 413.89 ms | 32.6 MiB | 4.0 MiB |
| auto-palette-stress | production sampled CIELAB k-means and chunked classifier | passed | 452.82 ms | 464.74 ms | 32.6 MiB | 4.0 MiB |
| vectorization-potrace | installed Potrace adapter | passed | 29.34 ms | 29.65 ms | 77.7 KiB | 5.7 KiB |
| geometry-openscad | installed OpenSCAD reference adapter | passed | 154.79 ms | 173.06 ms | 77.8 KiB | 9.2 MiB |
| packaging-production-3mf | production deterministic Bambu 3MF writer and reader | passed | 435.82 ms | 451.68 ms | 11.9 MiB | 47.1 KiB |
