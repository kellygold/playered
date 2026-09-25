"""Regression-fixture, golden-review, invariant, and benchmark support."""

from image23mf.quality.benchmarks import (
    BenchmarkCase,
    BenchmarkObservation,
    derive_budgets,
    run_benchmarks,
)
from image23mf.quality.goldens import (
    GoldenTolerance,
    LabelResult,
    accept_golden_update,
    compare_golden,
    propose_golden_update,
)
from image23mf.quality.synthetic_fixtures import generate_synthetic_fixture_suite

__all__ = [
    "BenchmarkCase",
    "BenchmarkObservation",
    "GoldenTolerance",
    "LabelResult",
    "accept_golden_update",
    "compare_golden",
    "derive_budgets",
    "generate_synthetic_fixture_suite",
    "propose_golden_update",
    "run_benchmarks",
]
