"""Machine-readable benchmark execution, summaries, and measured budget derivation."""

import gc
import hashlib
import json
import math
import platform
import resource
import statistics
import time
import tracemalloc
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

BENCHMARK_SCHEMA_VERSION = 1


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkStatus(str, Enum):
    PASSED = "passed"
    SKIPPED = "skipped"
    FAILED = "failed"


class BenchmarkSample(BenchmarkModel):
    duration_ms: float = Field(ge=0)
    peak_python_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)


class BenchmarkCaseResult(BenchmarkModel):
    id: str
    category: str
    description: str
    implementation: str
    fixture: str
    status: BenchmarkStatus
    warmup_iterations: int = Field(ge=0)
    measured_iterations: int = Field(ge=0)
    samples: tuple[BenchmarkSample, ...]
    min_ms: Optional[float] = Field(default=None, ge=0)
    median_ms: Optional[float] = Field(default=None, ge=0)
    p95_ms: Optional[float] = Field(default=None, ge=0)
    peak_python_bytes: Optional[int] = Field(default=None, ge=0)
    process_peak_rss_bytes: Optional[int] = Field(default=None, ge=0)
    error: Optional[str] = None
    metadata: dict[str, object] = Field(default_factory=dict)


class BenchmarkRun(BenchmarkModel):
    schema_version: int
    run_id: str
    generated_at: str
    git_commit: str
    machine: dict[str, str]
    cases: tuple[BenchmarkCaseResult, ...]


class CaseBudget(BenchmarkModel):
    case_id: str
    max_median_ms: float = Field(gt=0)
    max_p95_ms: float = Field(gt=0)
    max_peak_python_bytes: int = Field(gt=0)
    max_process_peak_rss_bytes: Optional[int] = Field(default=None, gt=0)


class BenchmarkBudgets(BenchmarkModel):
    schema_version: int
    source_run_sha256: str
    source_run_id: str
    derivation: str
    cases: tuple[CaseBudget, ...]


class BudgetViolation(BenchmarkModel):
    case_id: str
    metric: str
    actual: Optional[float] = Field(default=None, ge=0)
    limit: Optional[float] = Field(default=None, ge=0)
    reason: str


class BenchmarkBudgetReport(BenchmarkModel):
    passed: bool
    checked_case_ids: tuple[str, ...]
    violations: tuple[BudgetViolation, ...]


@dataclass(frozen=True)
class BenchmarkObservation:
    output_bytes: int = 0


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    category: str
    description: str
    implementation: str
    fixture: str
    operation: Callable[[], BenchmarkObservation]
    warmup_iterations: int = 1
    measured_iterations: int = 5
    metadata: Optional[dict[str, object]] = None
    trace_python_allocations: bool = True


class BenchmarkSkip(RuntimeError):
    pass


def run_benchmarks(
    cases: tuple[BenchmarkCase, ...], *, run_id: str, git_commit: str
) -> BenchmarkRun:
    results = tuple(_run_case(case) for case in cases)
    return BenchmarkRun(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        run_id=run_id,
        generated_at=_utc_now(),
        git_commit=git_commit,
        machine={
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "python": platform.python_version(),
        },
        cases=results,
    )


def write_benchmark_run(run: BenchmarkRun, json_path: Path, summary_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_path.write_text(_markdown_summary(run), encoding="utf-8")


def derive_budgets(run: BenchmarkRun) -> BenchmarkBudgets:
    """Derive initial regression ceilings only from successful measured cases."""
    serialized = json.dumps(
        run.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    budgets = []
    for case in run.cases:
        if (
            case.status != BenchmarkStatus.PASSED
            or case.median_ms is None
            or case.p95_ms is None
            or case.peak_python_bytes is None
        ):
            continue
        budgets.append(
            CaseBudget(
                case_id=case.id,
                max_median_ms=_ceiling(case.median_ms * 2.0, 0.1),
                max_p95_ms=_ceiling(case.p95_ms * 2.0, 0.1),
                max_peak_python_bytes=max(1024, math.ceil(case.peak_python_bytes * 1.5)),
                max_process_peak_rss_bytes=(
                    None
                    if case.process_peak_rss_bytes is None
                    else math.ceil(case.process_peak_rss_bytes * 1.5)
                ),
            )
        )
    return BenchmarkBudgets(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        source_run_sha256=hashlib.sha256(serialized).hexdigest(),
        source_run_id=run.run_id,
        derivation=(
            "Generated after the first measured baseline: 2.0x median/p95 duration and "
            "1.5x peak traced Python allocation (minimum 1 KiB) and process RSS."
        ),
        cases=tuple(budgets),
    )


def write_budgets(budgets: BenchmarkBudgets, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(budgets.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_budget_source(run: BenchmarkRun, budgets: BenchmarkBudgets) -> bool:
    serialized = json.dumps(
        run.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return (
        budgets.source_run_id == run.run_id
        and budgets.source_run_sha256 == hashlib.sha256(serialized).hexdigest()
    )


def verify_run_against_budgets(
    run: BenchmarkRun, budgets: BenchmarkBudgets
) -> BenchmarkBudgetReport:
    """Fail closed when a fresh run is incomplete, unbudgeted, or over a ceiling."""
    budget_by_id = {budget.case_id: budget for budget in budgets.cases}
    result_by_id = {case.id: case for case in run.cases}
    violations: list[BudgetViolation] = []

    for case_id in sorted(set(budget_by_id) - set(result_by_id)):
        violations.append(
            BudgetViolation(
                case_id=case_id,
                metric="case",
                reason="Budgeted case is missing from the fresh benchmark run.",
            )
        )
    for case_id in sorted(set(result_by_id) - set(budget_by_id)):
        violations.append(
            BudgetViolation(
                case_id=case_id,
                metric="case",
                reason="Fresh benchmark case has no committed regression budget.",
            )
        )

    metrics = (
        ("median_ms", "max_median_ms"),
        ("p95_ms", "max_p95_ms"),
        ("peak_python_bytes", "max_peak_python_bytes"),
    )
    for case_id in sorted(set(result_by_id) & set(budget_by_id)):
        case = result_by_id[case_id]
        budget = budget_by_id[case_id]
        if case.status != BenchmarkStatus.PASSED:
            violations.append(
                BudgetViolation(
                    case_id=case_id,
                    metric="status",
                    reason=(
                        f"Fresh benchmark did not pass ({case.status.value}): "
                        f"{case.error or 'no diagnostic'}"
                    ),
                )
            )
            continue
        for result_field, budget_field in metrics:
            actual = getattr(case, result_field)
            limit = getattr(budget, budget_field)
            if actual is None:
                violations.append(
                    BudgetViolation(
                        case_id=case_id,
                        metric=result_field,
                        limit=float(limit),
                        reason="Passing benchmark omitted a required measured value.",
                    )
                )
            elif actual > limit:
                violations.append(
                    BudgetViolation(
                        case_id=case_id,
                        metric=result_field,
                        actual=float(actual),
                        limit=float(limit),
                        reason="Fresh measurement exceeds the committed regression ceiling.",
                    )
                )
        if budget.max_process_peak_rss_bytes is not None:
            actual_rss = case.process_peak_rss_bytes
            if actual_rss is None:
                violations.append(
                    BudgetViolation(
                        case_id=case_id,
                        metric="process_peak_rss_bytes",
                        limit=float(budget.max_process_peak_rss_bytes),
                        reason="Passing benchmark omitted required process RSS evidence.",
                    )
                )
            elif actual_rss > budget.max_process_peak_rss_bytes:
                violations.append(
                    BudgetViolation(
                        case_id=case_id,
                        metric="process_peak_rss_bytes",
                        actual=float(actual_rss),
                        limit=float(budget.max_process_peak_rss_bytes),
                        reason="Fresh process RSS exceeds the committed regression ceiling.",
                    )
                )

    return BenchmarkBudgetReport(
        passed=not violations,
        checked_case_ids=tuple(sorted(set(result_by_id) & set(budget_by_id))),
        violations=tuple(violations),
    )


def _run_case(case: BenchmarkCase) -> BenchmarkCaseResult:
    if case.warmup_iterations < 0 or case.measured_iterations <= 0:
        raise ValueError("benchmark iterations must include at least one measured run")
    try:
        for _ in range(case.warmup_iterations):
            case.operation()
    except BenchmarkSkip as error:
        return _nonpassing_result(case, BenchmarkStatus.SKIPPED, str(error))
    except Exception as error:  # benchmark reports the case and continues the suite
        return _nonpassing_result(case, BenchmarkStatus.FAILED, _safe_error(error))

    samples = []
    try:
        for _ in range(case.measured_iterations):
            gc.collect()
            if case.trace_python_allocations:
                tracemalloc.start()
            started = time.perf_counter_ns()
            observation = case.operation()
            duration_ms = (time.perf_counter_ns() - started) / 1_000_000
            if case.trace_python_allocations:
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            else:
                peak = 0
            samples.append(
                BenchmarkSample(
                    duration_ms=duration_ms,
                    peak_python_bytes=peak,
                    output_bytes=observation.output_bytes,
                )
            )
    except BenchmarkSkip as error:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        return _nonpassing_result(case, BenchmarkStatus.SKIPPED, str(error))
    except Exception as error:  # benchmark reports the case and continues the suite
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        return _nonpassing_result(case, BenchmarkStatus.FAILED, _safe_error(error))

    durations = [sample.duration_ms for sample in samples]
    return BenchmarkCaseResult(
        id=case.id,
        category=case.category,
        description=case.description,
        implementation=case.implementation,
        fixture=case.fixture,
        status=BenchmarkStatus.PASSED,
        warmup_iterations=case.warmup_iterations,
        measured_iterations=case.measured_iterations,
        samples=tuple(samples),
        min_ms=min(durations),
        median_ms=statistics.median(durations),
        p95_ms=_percentile(durations, 0.95),
        peak_python_bytes=max(sample.peak_python_bytes for sample in samples),
        process_peak_rss_bytes=_process_peak_rss_bytes(),
        metadata=case.metadata or {},
    )


def _nonpassing_result(
    case: BenchmarkCase, status: BenchmarkStatus, error: str
) -> BenchmarkCaseResult:
    return BenchmarkCaseResult(
        id=case.id,
        category=case.category,
        description=case.description,
        implementation=case.implementation,
        fixture=case.fixture,
        status=status,
        warmup_iterations=case.warmup_iterations,
        measured_iterations=0,
        samples=(),
        error=error,
        metadata=case.metadata or {},
    )


def _markdown_summary(run: BenchmarkRun) -> str:
    lines = [
        f"# Benchmark baseline: {run.run_id}",
        "",
        f"Generated: {run.generated_at}",
        f"Commit: `{run.git_commit}`",
        (
            f"Machine: {run.machine['system']} {run.machine['machine']}, "
            f"Python {run.machine['python']}"
        ),
        "",
        "These are measurements, not promises. Initial budgets are derived only after this run.",
        "",
        "| Case | Implementation | Status | Median | p95 | Peak Python | Output |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for case in run.cases:
        if case.status == BenchmarkStatus.PASSED:
            output = max((sample.output_bytes for sample in case.samples), default=0)
            lines.append(
                f"| {case.id} | {case.implementation} | {case.status.value} | "
                f"{case.median_ms:.2f} ms | {case.p95_ms:.2f} ms | "
                f"{_human_bytes(case.peak_python_bytes or 0)} | {_human_bytes(output)} |"
            )
        else:
            lines.append(
                f"| {case.id} | {case.implementation} | {case.status.value} | — | — | — | — |"
            )
    skipped = [case for case in run.cases if case.status != BenchmarkStatus.PASSED]
    if skipped:
        lines.extend(["", "## Skipped or failed cases", ""])
        lines.extend(f"- **{case.id}:** {case.error}" for case in skipped)
    lines.append("")
    return "\n".join(lines)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _process_peak_rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss if platform.system() == "Darwin" else rss * 1024)


def _safe_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:500]


def _ceiling(value: float, precision: float) -> float:
    return round(math.ceil(value / precision) * precision, 10)


def _human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
