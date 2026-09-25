# Benchmark baseline: baseline-2026-07-17-mural-partition

Generated: 2026-07-17T07:25:46.830211Z
Commit: `2aeb2ea3d29a6d189e446d434e024855fef3ff78`
Machine: Darwin arm64, Python 3.9.6

These are measurements, not promises. Initial budgets are derived only after this run.

| Case | Implementation | Status | Median | p95 | Peak Python | Output |
|---|---|---:|---:|---:|---:|---:|
| mural-topology-partition-1024-3x2 | production partition_master_topology without audit-oracle replay | passed | 3036.19 ms | 3059.61 ms | 0 B | 17.1 KiB |
