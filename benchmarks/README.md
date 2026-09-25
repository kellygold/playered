# Performance baseline

This directory records representative, machine-readable Image23MF performance evidence. The
baseline is intentionally measured before regression ceilings are derived; no target was guessed
and then retrofitted to the result.

Run the suite from the repository root with the commit that contains the harness:

```bash
.venv/bin/python benchmarks/run_baseline.py \
  --run-id baseline-YYYY-MM-DD-machine \
  --git-commit FULL_GIT_SHA \
  --budgets benchmarks/budgets.json
```

The explicit `--budgets` command writes a JSON result, a human-readable Markdown summary, and
`budgets.json`. Each
budget file contains the exact SHA-256 of the canonical source run. `verify_budget_source` rejects
a result/budget mismatch.

Release verification never overwrites the committed ceilings:

```bash
.venv/bin/python benchmarks/run_baseline.py \
  --run-id release-YYYY-MM-DD-machine \
  --git-commit FULL_GIT_SHA \
  --output-dir workspace/qa/release-gate/performance \
  --verify-against benchmarks/budgets.json
```

This writes a separate budget report and exits non-zero for missing, skipped, failed, unbudgeted,
or over-budget cases. Updating budgets remains an explicit reviewed action.

The 1024×1024 fractional mural partition has a separate production-path budget because Python
allocation tracing materially distorts this object-heavy workload. Generate reviewed evidence and
an optional cumulative profile with:

```bash
.venv/bin/python benchmarks/run_mural_partition.py \
  --run-id baseline-YYYY-MM-DD-mural-partition \
  --git-commit FULL_GIT_SHA \
  --output-dir benchmarks/results \
  --budgets benchmarks/mural-partition-budgets.json \
  --profile-output benchmarks/results/baseline-YYYY-MM-DD-mural-partition-profile.txt
```

The timed region is the production `partition_master_topology` call for a deterministic four-label
1024×1024 source split into a 3×2 mural. Authoritative master-topology construction, label
partition construction, and the independent acceptance-oracle replay are prepared outside the
timed region and reported as metadata. The reviewed ceiling is a 15-second median, 18-second p95,
and 300 MiB whole-process RSS. Run `make qa-mural-performance` to create fresh, non-destructive
verification evidence against those committed ceilings.

## Workloads

- cold SQLite migration and FastAPI construction;
- cold construction and generation of the complete OpenAPI contract as a separate workload;
- warm reopen/list/get against a 25-project workspace;
- cold decode and warm production CIELAB classification for the procedural 2520×1680 geometry-stress fixture;
- deterministic sampled CIELAB auto-palette fitting and classification for the procedural stress fixture;
- installed Potrace vectorization of the nozzle-scale synthetic geometry fixture;
- installed OpenSCAD base/relief extrusion;
- production deterministic Bambu 3MF build and independent parse for a representative 30-part,
  four-material, 7,200-triangle plate.
- exact production partitioning of an authoritative 1024×1024 mural topology across six
  fractional-cell panels, with whole-process RSS enforcement.

Preview, vector, geometry, and package implementations are named explicitly in each result. Palette
classification and fitting use the production M1 engine. Packaging uses the same deterministic
Bambu writer and independent reader as the production export service.

Every measured sample records duration and output size. The general suite also records traced peak
Python allocation; the mural case deliberately disables tracing and records zero for that metric.
Every run records the Python process high-water RSS. Child-process memory for Potrace and OpenSCAD
is not yet isolated, so the baseline does not claim to measure their private peak memory.

## Budget policy

Current ceilings were regenerated from `baseline-2026-09-24-public-fixtures` on
Darwin arm64 with Python 3.9.6 after the fixture replacement. They use 2× measured
median/p95 duration and 1.5× traced Python peak allocation (minimum 1 KiB) and
process high-water RSS. Startup and OpenAPI measurements also reflect the current
application and dependency set, so this refresh is not a claim of a speedup. They are regression alarms, not product latency
promises. Hardware-specific CI enforcement should compare like-for-like machines and require a
reviewed baseline update when implementation or representative fixtures change.

Historical results referencing private artwork are retained as development history
only; those images are not distributed. The public fixture is generated from
original shapes and seeded texture. Its measurements are a new workload and must
not be presented as equivalent to timings of the removed private artwork.
