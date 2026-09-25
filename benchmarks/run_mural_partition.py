#!/usr/bin/env python3
"""Benchmark exact 1024px fractional mural topology partitioning outside unit tests."""

import argparse
import cProfile
import hashlib
import io
import json
import pstats
import time
from pathlib import Path
from typing import Optional

from image23mf.contracts.job import CropConfig
from image23mf.engine.labels import LabelField
from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize
from image23mf.geometry.model import (
    LineSegment,
    Material,
    Path2D,
    Point2,
    RectangleBase,
    SourceLabel,
)
from image23mf.geometry.topology import build_shared_boundary_topology
from image23mf.mural.partition import partition_master_labels
from image23mf.mural.planner import (
    BedEnvelope,
    MuralLayout,
    MuralPlanRequest,
    MuralSourceProvenance,
    build_mural_plan,
)
from image23mf.mural.topology_partition import partition_master_topology
from image23mf.quality.benchmarks import (
    BenchmarkBudgetReport,
    BenchmarkBudgets,
    BenchmarkCase,
    BenchmarkObservation,
    BenchmarkRun,
    BenchmarkStatus,
    derive_budgets,
    run_benchmarks,
    verify_run_against_budgets,
    write_benchmark_run,
    write_budgets,
)

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "mural-topology-partition-1024-3x2"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
MAX_MEDIAN_MS = 15_000.0
MAX_P95_MS = 18_000.0
MAX_PROCESS_RSS_BYTES = 300 * 1024 * 1024


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results",
    )
    parser.add_argument("--budgets", type=Path, default=None)
    parser.add_argument("--verify-against", type=Path, default=None)
    parser.add_argument(
        "--profile-output",
        type=Path,
        default=None,
        help="optional one-iteration cumulative cProfile report",
    )
    arguments = parser.parse_args()
    if arguments.budgets is not None and arguments.verify_against is not None:
        parser.error("--budgets and --verify-against are mutually exclusive")
    return arguments


class MuralPartitionWorkload:
    """Prepare immutable master evidence once, then time only production partitioning."""

    def __init__(self) -> None:
        self.labels = _field(1024, 1024)
        self.request = _request(self.labels)
        self.plan = build_mural_plan(self.request)

        started = time.perf_counter_ns()
        self.master = _authoritative_topology(self.labels, self.request)
        self.master_topology_ms = (time.perf_counter_ns() - started) / 1_000_000

        started = time.perf_counter_ns()
        self.label_partition = partition_master_labels(
            self.labels,
            self.request,
            self.plan,
            authoritative_vector_sha256=HASH_E,
            authoritative_topology_sha256=self.master.topology_artifact_sha256,
        )
        self.label_partition_ms = (time.perf_counter_ns() - started) / 1_000_000

    def case(self) -> BenchmarkCase:
        return BenchmarkCase(
            id=CASE_ID,
            category="mural-topology",
            description=(
                "Partition one authoritative 1024×1024 label/topology master into six exact "
                "fractional-cell tile documents."
            ),
            implementation="production partition_master_topology without audit-oracle replay",
            fixture="deterministic public 1024×1024 four-label field; 3 columns × 2 rows",
            operation=self.partition,
            warmup_iterations=0,
            measured_iterations=3,
            trace_python_allocations=False,
            metadata={
                "authoritative_master_topology_setup_ms": round(self.master_topology_ms, 3),
                "label_partition_setup_ms": round(self.label_partition_ms, 3),
                "master_labels_sha256": hashlib.sha256(self.labels.pixels).hexdigest(),
                "master_pixel_count": len(self.labels.pixels),
                "panel_count": len(self.plan.tiles),
                "fractional_clip_required": True,
                "excluded_from_timing": [
                    "authoritative master topology construction",
                    "label partition construction",
                    "independent acceptance-oracle replay",
                ],
            },
        )

    def partition(self) -> BenchmarkObservation:
        result = partition_master_topology(self.label_partition, self.master)
        if len(result.tiles) != 6:
            raise RuntimeError("production mural benchmark did not produce six tiles")
        if result.manifest.source_pixel_count != result.manifest.represented_pixel_count:
            raise RuntimeError("production mural benchmark lost master pixel ownership")
        payload = result.manifest.model_dump_json().encode("utf-8")
        return BenchmarkObservation(output_bytes=len(payload))

    def write_profile(self, path: Path) -> None:
        profile = cProfile.Profile()
        profile.enable()
        self.partition()
        profile.disable()
        stream = io.StringIO()
        pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(60)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stream.getvalue(), encoding="utf-8")


def _field(width: int, height: int) -> LabelField:
    pixels = bytes(
        ((x * 4 // width) + (y * 3 // height) + (1 if x == width // 2 and y > height // 3 else 0))
        % 4
        for y in range(height)
        for x in range(width)
    )
    return LabelField(
        width=width,
        height=height,
        label_values=(0, 1, 2, 3),
        pixels=pixels,
    )


def _request(labels: LabelField) -> MuralPlanRequest:
    size = PixelSize(width=labels.width, height=labels.height)
    canvas = MillimetreSize(width=600, height=400)
    return MuralPlanRequest(
        source=MuralSourceProvenance(
            source_asset_id="asset_public_mural_benchmark",
            source_asset_sha256=HASH_A,
            processed_artifact_id="artifact_public_mural_benchmark",
            processed_artifact_sha256=HASH_B,
            processed_size=size,
            canonical_transform=CanonicalTransform.from_crop_config(
                original_size=size,
                normalized_size=size,
                exif_orientation=1,
                crop=CropConfig(mode="stretch"),
                working_size=size,
                canvas_size=canvas,
            ),
            config_sha256=HASH_C,
            engine_version="0.1.0",
            revision_id="revision_public_mural_benchmark",
        ),
        layout=MuralLayout(
            rows=2,
            columns=3,
            panel_width_mm=200,
            panel_height_mm=200,
        ),
        bed=BedEnvelope(
            printer_id="bambu-p2s",
            plate_id="textured-pei",
            profile_catalog_fingerprint=HASH_D,
            width_mm=256,
            height_mm=256,
        ),
    )


def _authoritative_topology(labels: LabelField, request: MuralPlanRequest):
    plan = build_mural_plan(request)
    colors = ("#111111", "#F99963", "#0078BF", "#CBC6B8")
    materials = tuple(
        sorted(
            (
                Material.create(
                    name=f"Color {index}",
                    color_hex=colors[index],
                    palette_color_id=f"color-{index}",
                    filament_id=f"filament-{index}",
                )
                for index in labels.label_values
            ),
            key=lambda item: item.id,
        )
    )
    material_by_palette = {item.palette_color_id: item for item in materials}
    labels_sha256 = hashlib.sha256(labels.pixels).hexdigest()
    source_labels = tuple(
        sorted(
            (
                SourceLabel.create(
                    source_asset_id=request.source.source_asset_id,
                    processed_labels_sha256=labels_sha256,
                    label_index=index,
                    name=f"Color {index}",
                    color_hex=colors[index],
                    palette_color_id=f"color-{index}",
                    material_id=material_by_palette[f"color-{index}"].id,
                    classification="background" if index == 0 else "artwork",
                )
                for index in labels.label_values
            ),
            key=lambda item: item.id,
        )
    )
    return build_shared_boundary_topology(
        labels,
        source_asset_sha256=request.source.source_asset_sha256,
        source_labels=source_labels,
        materials=materials,
        base=RectangleBase.create(
            center=Point2(
                x_mm=plan.master_size_mm.width / 2,
                y_mm=plan.master_size_mm.height / 2,
            ),
            width_mm=plan.master_size_mm.width,
            height_mm=plan.master_size_mm.height,
        ),
        canvas_width_mm=plan.master_size_mm.width,
        canvas_height_mm=plan.master_size_mm.height,
        vector_paths=(
            _construction_rectangle(plan.master_size_mm.width, plan.master_size_mm.height),
        ),
        vector_artifact_sha256=HASH_E,
        palette_color_order=tuple(f"color-{index}" for index in labels.label_values),
    )


def _construction_rectangle(width_mm: float, height_mm: float) -> Path2D:
    origin = Point2(x_mm=0, y_mm=0)
    return Path2D.create(
        purpose="construction",
        start=origin,
        segments=(
            LineSegment(end=Point2(x_mm=width_mm, y_mm=0)),
            LineSegment(end=Point2(x_mm=width_mm, y_mm=height_mm)),
            LineSegment(end=Point2(x_mm=0, y_mm=height_mm)),
            LineSegment(end=origin),
        ),
        closed=True,
    )


def _mural_budgets(run: BenchmarkRun) -> BenchmarkBudgets:
    derived = derive_budgets(run)
    if len(derived.cases) != 1 or derived.cases[0].case_id != CASE_ID:
        raise RuntimeError("mural benchmark did not produce one successful budget source case")
    case = derived.cases[0].model_copy(
        update={
            "max_median_ms": MAX_MEDIAN_MS,
            "max_p95_ms": MAX_P95_MS,
            "max_peak_python_bytes": 1024,
            "max_process_peak_rss_bytes": MAX_PROCESS_RSS_BYTES,
        }
    )
    return derived.model_copy(
        update={
            "derivation": (
                "Reviewed M4 production budget: 1024×1024 3×2 partition median ≤15 s, "
                "p95 ≤18 s, and whole-process peak RSS ≤300 MiB on the recorded reference "
                "machine. Python allocation tracing is disabled because it distorts this "
                "object-heavy production path."
            ),
            "cases": (case,),
        }
    )


def main() -> int:
    arguments = parse_arguments()
    workload = MuralPartitionWorkload()
    run = run_benchmarks(
        (workload.case(),),
        run_id=arguments.run_id,
        git_commit=arguments.git_commit,
    )
    result_path = arguments.output_dir / f"{arguments.run_id}.json"
    summary_path = arguments.output_dir / f"{arguments.run_id}.md"
    write_benchmark_run(run, result_path, summary_path)
    print(f"Wrote {result_path}")
    print(f"Wrote {summary_path}")

    budget_report: Optional[BenchmarkBudgetReport] = None
    if arguments.verify_against is not None:
        budgets = BenchmarkBudgets.model_validate_json(
            arguments.verify_against.read_text(encoding="utf-8")
        )
        budget_report = verify_run_against_budgets(run, budgets)
        report_path = arguments.output_dir / f"{arguments.run_id}-budget-report.json"
        report_path.write_text(
            json.dumps(budget_report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {report_path}")
        print(f"Mural performance budget: {'PASS' if budget_report.passed else 'FAIL'}")
    elif arguments.budgets is not None:
        write_budgets(_mural_budgets(run), arguments.budgets)
        print(f"Wrote {arguments.budgets}")

    if arguments.profile_output is not None:
        workload.write_profile(arguments.profile_output)
        print(f"Wrote {arguments.profile_output}")

    failed = any(case.status != BenchmarkStatus.PASSED for case in run.cases)
    return 1 if failed or (budget_report is not None and not budget_report.passed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
