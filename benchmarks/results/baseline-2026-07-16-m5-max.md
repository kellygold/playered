# Benchmark baseline: baseline-2026-07-16-m5-max

Generated: 2026-07-15T19:00:14.553958Z
Commit: `23ae4676469b4ba6a451dc77d8e482ca759a63c2`
Machine: Darwin arm64, Python 3.9.6

These are measurements, not promises. Initial budgets are derived only after this run.

| Case | Implementation | Status | Median | p95 | Peak Python | Output |
|---|---|---:|---:|---:|---:|---:|
| startup-cold | current SQLite/FastAPI startup | passed | 60.44 ms | 66.56 ms | 1.4 MiB | 192.8 KiB |
| project-reopen-warm | current SQLite repositories | passed | 0.79 ms | 0.85 ms | 25.6 KiB | 656 B |
| preview-cold-wager | reference Pillow discrete-color preview | passed | 44.81 ms | 47.17 ms | 8.1 MiB | 4.0 MiB |
| preview-warm-wager | reference Pillow discrete-color preview | passed | 2.45 ms | 2.54 ms | 8.1 MiB | 4.0 MiB |
| vectorization-potrace | installed Potrace adapter | passed | 32.68 ms | 33.11 ms | 76.8 KiB | 5.7 KiB |
| geometry-openscad | installed OpenSCAD reference adapter | passed | 148.11 ms | 152.46 ms | 76.9 KiB | 9.2 MiB |
| packaging-reference | reference ZIP packaging (pre-3MF writer) | passed | 245.42 ms | 246.23 ms | 23.3 MiB | 7.3 MiB |
