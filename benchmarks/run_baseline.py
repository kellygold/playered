#!/usr/bin/env python3
"""Run representative Image23MF workloads and derive post-measurement budgets."""

import argparse
import json
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image

from image23mf.api.app import create_app
from image23mf.bambu import BambuProjectSettings, build_bambu_3mf, read_bambu_3mf
from image23mf.bambu import Material as BambuMaterial
from image23mf.bambu import Mesh as BambuMesh
from image23mf.engine import classify_palette, quantize_auto_palette
from image23mf.external import ExternalToolRegistry, ToolId, ToolRunner
from image23mf.quality.benchmarks import (
    BenchmarkBudgetReport,
    BenchmarkBudgets,
    BenchmarkCase,
    BenchmarkObservation,
    BenchmarkSkip,
    derive_budgets,
    run_benchmarks,
    verify_run_against_budgets,
    write_benchmark_run,
    write_budgets,
)
from image23mf.settings import Settings
from image23mf.storage import ProjectRepository, open_database

ROOT = Path(__file__).resolve().parents[1]
STRESS = ROOT / "tests" / "fixtures" / "public-regression" / "geometry-stress.png"
GEOMETRY_FIXTURE = ROOT / "tests" / "fixtures" / "synthetic" / "geometry-nozzle-040.png"
PALETTE = ("#F2D6AA", "#E75B12", "#0A3A78", "#202428")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", required=True, help="stable identifier used in result filenames"
    )
    parser.add_argument(
        "--git-commit", required=True, help="commit containing the measured harness"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results",
        help="directory for JSON and Markdown baseline evidence",
    )
    parser.add_argument(
        "--budgets",
        type=Path,
        default=None,
        help="optional post-measurement budget destination",
    )
    parser.add_argument(
        "--verify-against",
        type=Path,
        default=None,
        help="verify this fresh run against committed budgets without overwriting them",
    )
    arguments = parser.parse_args()
    if arguments.budgets is not None and arguments.verify_against is not None:
        parser.error("--budgets and --verify-against are mutually exclusive")
    return arguments


class RepresentativeWorkloads:
    """Own temporary inputs/outputs for one baseline run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.registry = ExternalToolRegistry()
        self.runner = ToolRunner(temp_root=root / "tool-workspaces")
        self.potrace = self.registry.detect(ToolId.POTRACE)
        self.openscad = self.registry.detect(ToolId.OPENSCAD)
        self.preview_source = _load_rgba(STRESS)
        self.reopen_database = root / "reopen" / "image23mf.sqlite3"
        connection = open_database(self.reopen_database)
        try:
            repository = ProjectRepository(connection)
            self.reopen_project_id = repository.create("Benchmark reopen").id
            for index in range(24):
                repository.create(f"Representative project {index + 1:02d}")
        finally:
            connection.close()
        self.mask_path = root / "reference-mask.pbm"
        self.svg_path = root / "reference-mask.svg"
        self.scad_path = root / "reference.scad"
        self.stl_path = root / "reference.stl"
        self.package_path = root / "representative-production.3mf"
        self.package_meshes = _representative_bambu_meshes()
        self.package_materials = tuple(
            BambuMaterial(
                name=name,
                color=color,
                extruder=index,
                preset="Bambu PLA Matte",
            )
            for index, (name, color) in enumerate(
                (
                    ("Bone White", "#CBC6B8"),
                    ("Mandarin Orange", "#F99963"),
                    ("Marine Blue", "#0078BF"),
                    ("Charcoal", "#000000"),
                ),
                start=1,
            )
        )
        self._prepare_mask()
        self._prepare_reference_geometry_inputs()

    def cases(self) -> tuple[BenchmarkCase, ...]:
        tool_metadata = {
            "potrace_version": self.potrace.version,
            "openscad_version": self.openscad.version,
        }
        return (
            BenchmarkCase(
                id="startup-cold",
                category="startup",
                description="Fresh workspace migration and API construction.",
                implementation="current SQLite/FastAPI startup",
                fixture="fresh temporary workspace",
                operation=self.startup_cold,
                warmup_iterations=0,
                measured_iterations=3,
            ),
            BenchmarkCase(
                id="openapi-cold",
                category="contract",
                description="Fresh API construction and complete OpenAPI contract generation.",
                implementation="current FastAPI/Pydantic OpenAPI generation",
                fixture="fresh temporary workspace",
                operation=self.openapi_cold,
                warmup_iterations=0,
                measured_iterations=3,
                metadata={"contract_surface": "all registered API routes and schemas"},
            ),
            BenchmarkCase(
                id="project-reopen-warm",
                category="reopen",
                description="Open a migrated database, list 25 projects, and retrieve one project.",
                implementation="current SQLite repositories",
                fixture="25-project temporary workspace",
                operation=self.project_reopen,
                warmup_iterations=1,
                measured_iterations=7,
            ),
            BenchmarkCase(
                id="preview-cold-stress",
                category="preview",
                description="Classify the full-resolution procedural image to four labels.",
                implementation="production chunked CIELAB classifier",
                fixture="public-regression/geometry-stress.png (2520x1680)",
                operation=self.preview_cold,
                warmup_iterations=0,
                measured_iterations=3,
                metadata={"palette": PALETTE, "distance": "CIE76", "dither": "none"},
            ),
            BenchmarkCase(
                id="preview-warm-stress",
                category="preview",
                description="Recompute four labels from the decoded procedural image.",
                implementation="production chunked CIELAB classifier",
                fixture="preloaded geometry-stress.png (2520x1680 RGBA)",
                operation=self.preview_warm,
                warmup_iterations=1,
                measured_iterations=7,
                metadata={"palette": PALETTE, "distance": "CIE76", "dither": "none"},
            ),
            BenchmarkCase(
                id="auto-palette-stress",
                category="palette",
                description="Fit and classify a deterministic four-color procedural palette.",
                implementation="production sampled CIELAB k-means and chunked classifier",
                fixture="preloaded geometry-stress.png (2520x1680 RGBA)",
                operation=self.auto_palette,
                warmup_iterations=1,
                measured_iterations=5,
                metadata={
                    "color_count": 4,
                    "distance": "CIE76",
                    "sampling": "seeded-systematic",
                },
            ),
            BenchmarkCase(
                id="vectorization-potrace",
                category="vectorization",
                description="Trace the nozzle-scale synthetic binary mask to SVG.",
                implementation="installed Potrace adapter",
                fixture="synthetic/geometry-nozzle-040.png",
                operation=self.vectorize,
                warmup_iterations=1,
                measured_iterations=5,
                metadata=tool_metadata,
            ),
            BenchmarkCase(
                id="geometry-openscad",
                category="geometry",
                description="Extrude a 120x80 mm base and traced relief to binary STL.",
                implementation="installed OpenSCAD reference adapter",
                fixture="Potrace SVG from geometry-nozzle-040.png",
                operation=self.geometry,
                warmup_iterations=1,
                measured_iterations=3,
                metadata=tool_metadata,
            ),
            BenchmarkCase(
                id="packaging-production-3mf",
                category="packaging",
                description="Build and independently parse a representative production Bambu 3MF.",
                implementation="production deterministic Bambu 3MF writer and reader",
                fixture="30 homogeneous parts, 4 materials, 4,800 vertices, 7,200 triangles",
                operation=self.package,
                warmup_iterations=1,
                measured_iterations=5,
                metadata={
                    "mesh_count": len(self.package_meshes),
                    "vertex_count": sum(len(mesh.vertices) for mesh in self.package_meshes),
                    "triangle_count": sum(len(mesh.triangles) for mesh in self.package_meshes),
                    **tool_metadata,
                },
            ),
        )

    def startup_cold(self) -> BenchmarkObservation:
        with tempfile.TemporaryDirectory(prefix="startup-", dir=self.root) as directory:
            workspace = Path(directory)
            connection = open_database(workspace / "image23mf.sqlite3")
            connection.close()
            app = create_app(Settings(workspace=workspace))
            try:
                return BenchmarkObservation(
                    output_bytes=(workspace / "image23mf.sqlite3").stat().st_size
                )
            finally:
                app.state.worker_manager.shutdown()

    def openapi_cold(self) -> BenchmarkObservation:
        with tempfile.TemporaryDirectory(prefix="openapi-", dir=self.root) as directory:
            workspace = Path(directory)
            connection = open_database(workspace / "image23mf.sqlite3")
            connection.close()
            app = create_app(Settings(workspace=workspace))
            try:
                schema_bytes = json.dumps(app.openapi(), sort_keys=True).encode("utf-8")
                return BenchmarkObservation(output_bytes=len(schema_bytes))
            finally:
                app.state.worker_manager.shutdown()

    def project_reopen(self) -> BenchmarkObservation:
        connection = open_database(self.reopen_database)
        try:
            repository = ProjectRepository(connection)
            projects = repository.list()
            selected = repository.get(self.reopen_project_id)
        finally:
            connection.close()
        return BenchmarkObservation(
            output_bytes=sum(len(project.name.encode("utf-8")) for project in projects)
            + len(selected.id.encode("utf-8"))
        )

    def preview_cold(self) -> BenchmarkObservation:
        source = _load_rgba(STRESS)
        try:
            return _classify(source)
        finally:
            source.close()

    def preview_warm(self) -> BenchmarkObservation:
        return _classify(self.preview_source)

    def auto_palette(self) -> BenchmarkObservation:
        result = quantize_auto_palette(self.preview_source, 4)
        return BenchmarkObservation(output_bytes=len(result.labels.pixels))

    def vectorize(self) -> BenchmarkObservation:
        executable = _available_path(self.potrace.unavailable_reason, self.potrace.path)
        self.runner.run(
            executable,
            ("--svg", "--output", str(self.svg_path), str(self.mask_path)),
            working_directory=self.root,
            timeout_seconds=60,
        )
        return BenchmarkObservation(output_bytes=self.svg_path.stat().st_size)

    def geometry(self) -> BenchmarkObservation:
        executable = _available_path(self.openscad.unavailable_reason, self.openscad.path)
        self.runner.run(
            executable,
            ("--backend", "Manifold", "-o", str(self.stl_path), str(self.scad_path)),
            working_directory=self.root,
            timeout_seconds=120,
        )
        return BenchmarkObservation(output_bytes=self.stl_path.stat().st_size)

    def package(self) -> BenchmarkObservation:
        payload = build_bambu_3mf(
            name="Image23MF production packaging benchmark",
            meshes=self.package_meshes,
            materials=self.package_materials,
            settings=BambuProjectSettings(
                nozzle_diameter=0.4,
                layer_height=0.2,
                plate_center_x=128,
                plate_center_y=128,
            ),
        )
        project = read_bambu_3mf(payload)
        if len(project.meshes) != len(self.package_meshes):
            raise RuntimeError("production 3MF round trip lost representative mesh parts")
        self.package_path.write_bytes(payload)
        return BenchmarkObservation(output_bytes=len(payload))

    def _prepare_mask(self) -> None:
        with Image.open(GEOMETRY_FIXTURE) as image:
            grayscale = image.convert("L")
        grayscale.point(lambda value: 0 if value < 128 else 255).convert("1").save(self.mask_path)

    def _prepare_reference_geometry_inputs(self) -> None:
        if self.potrace.available:
            self.vectorize()
        else:
            self.svg_path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="120mm" height="80mm">'
                '<path d="M8 8h104v64H8z M26 20h18v18H26z" fill="black"/>'
                "</svg>\n",
                encoding="utf-8",
            )
        self.scad_path.write_text(
            "$fn=48;\nunion() {\n  cube([120, 80, 0.48]);\n"
            "  translate([0, 0, 0.48]) linear_extrude(height=1.12) "
            'resize([120, 80]) import("reference-mask.svg");\n}\n',
            encoding="utf-8",
        )
        if self.openscad.available:
            self.geometry()
        else:
            self.stl_path.write_bytes(b"OpenSCAD unavailable during reference setup.\n")


def _available_path(reason: Optional[str], path: Optional[Path]) -> Path:
    if path is None:
        raise BenchmarkSkip(reason or "required local executable is unavailable")
    return path


def _load_rgba(path: Path) -> Image.Image:
    with Image.open(path) as image:
        image.load()
        return image.convert("RGBA")


def _classify(source: Image.Image) -> BenchmarkObservation:
    result = classify_palette(source, PALETTE)
    return BenchmarkObservation(output_bytes=len(result.labels.pixels))


def _representative_bambu_meshes() -> tuple[BambuMesh, ...]:
    meshes = []
    cube_triangles = (
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    )
    for mesh_index in range(30):
        vertices = []
        triangles = []
        for cube_index in range(20):
            x = (mesh_index % 10) * 20 + (cube_index % 5) * 2.4
            y = (mesh_index // 10) * 20 + (cube_index // 5) * 2.4
            z = 0.0
            offset = len(vertices)
            vertices.extend(
                (
                    (x, y, z),
                    (x + 2, y, z),
                    (x + 2, y + 2, z),
                    (x, y + 2, z),
                    (x, y, z + 1.6),
                    (x + 2, y, z + 1.6),
                    (x + 2, y + 2, z + 1.6),
                    (x, y + 2, z + 1.6),
                )
            )
            triangles.extend(
                tuple(offset + vertex for vertex in triangle) for triangle in cube_triangles
            )
        meshes.append(
            BambuMesh(
                name=f"Representative part {mesh_index + 1:02d}",
                vertices=tuple(vertices),
                triangles=tuple(triangles),
                material_index=mesh_index % 4,
            )
        )
    return tuple(meshes)


def main() -> int:
    arguments = parse_arguments()
    with tempfile.TemporaryDirectory(prefix="image23mf-baseline-") as directory:
        workloads = RepresentativeWorkloads(Path(directory))
        run = run_benchmarks(
            workloads.cases(), run_id=arguments.run_id, git_commit=arguments.git_commit
        )
    result_path = arguments.output_dir / f"{arguments.run_id}.json"
    summary_path = arguments.output_dir / f"{arguments.run_id}.md"
    write_benchmark_run(run, result_path, summary_path)
    failed = [case for case in run.cases if case.status.value == "failed"]
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
        print(
            "Performance budgets: "
            + (
                "PASS"
                if budget_report.passed
                else f"FAIL ({len(budget_report.violations)} violations)"
            )
        )
    elif arguments.budgets is not None:
        write_budgets(derive_budgets(run), arguments.budgets)
        print(f"Wrote {arguments.budgets}")
    return 1 if failed or (budget_report is not None and not budget_report.passed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
