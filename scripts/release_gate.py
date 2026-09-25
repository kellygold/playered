#!/usr/bin/env python3
"""Run the reproducible Image23MF release matrix and retain durable evidence."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / "workspace" / "qa" / "release-gate"
Status = Literal["passed", "failed", "blocked", "skipped"]


@dataclass(frozen=True)
class Gate:
    id: str
    title: str
    area: str
    command: tuple[str, ...]
    browser: bool = False
    named_acceptance: str | None = None


@dataclass(frozen=True)
class Blocker:
    id: str
    title: str
    reason: str
    priority: str
    linear_relation: str
    acceptance: tuple[str, ...]


@dataclass
class GateResult:
    id: str
    title: str
    area: str
    status: Status
    duration_seconds: float
    command: list[str]
    log: str | None
    exit_code: int | None
    named_acceptance: str | None
    reason: str | None = None


GATES = (
    Gate(
        "clean-source-tree",
        "Release evidence is bound to a clean committed source tree",
        "reproducibility",
        ("git", "status", "--porcelain"),
    ),
    Gate(
        "static-unit-package",
        "Static analysis, all unit/integration tests, and production packages",
        "core",
        ("make", "check"),
    ),
    Gate(
        "golden-corpus",
        "Committed golden corpus and deterministic software slice",
        "quality",
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_goldens.py",
            "tests/test_geometry_ir.py",
            "tests/test_geometry_ir_stdlib_consumer.py",
            "tests/test_bambu_report.py",
            "tests/test_release_golden.py",
            "-k",
            "not installed",
        ),
    ),
    Gate(
        "installed-bambu-golden",
        "Installed Bambu CLI golden for 0.2 mm and 0.4 mm profiles",
        "quality",
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_release_golden.py",
            "-k",
            "installed",
        ),
    ),
    Gate(
        "storage-corruption-tools",
        "Storage corruption, recovery, and external-tool fallbacks",
        "resilience",
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_blob_store.py",
            "tests/test_workspace_health.py",
            "tests/test_ingestion.py",
            "tests/test_exporting.py",
            "tests/test_revision_api.py",
            "tests/test_artifact_reveal.py",
            "tests/test_tool_runner.py",
            "tests/test_capabilities.py",
            "tests/test_bambu_cli.py",
            "tests/test_potrace_vectorizer.py",
            "tests/test_macos_lifecycle.py",
            "tests/test_workspace_bundles.py",
        ),
    ),
    Gate(
        "process-death-recovery",
        "Process-death and startup-reconciliation recovery",
        "resilience",
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_recovery.py",
            "tests/test_process_death_recovery.py",
            "tests/test_api.py",
            "-k",
            "recovery or startup or workspace_health or artifact",
        ),
    ),
    Gate(
        "startup-recovery-browser",
        "Self-contained isolated startup-recovery browser journey",
        "browser",
        ("make", "qa-recovery"),
        browser=True,
    ),
    Gate(
        "performance-budgets",
        "Fresh representative workloads remain inside committed budgets",
        "performance",
        (),
    ),
    Gate(
        "accessibility-responsive-keyboard",
        "WCAG A/AA, keyboard, reduced-motion, and 320 px reflow",
        "browser",
        ("make", "qa-accessibility"),
        browser=True,
    ),
    Gate(
        "risk-overlay-fit-and-zoom",
        "Replacement/revision lifecycle and exact Risk overlay alignment",
        "browser",
        ("make", "qa-replacement-lifecycle"),
        browser=True,
        named_acceptance="Risk overlay Fit 100% and zoom 125%",
    ),
    Gate(
        "preview-build-download-discoverability",
        "First-run Preview to Build to validated Download journey",
        "browser",
        ("make", "qa-first-run"),
        browser=True,
        named_acceptance="Preview → Build → Download discoverability",
    ),
    Gate(
        "output-download-package",
        "Actual 3MF build, geometry view, download, and ZIP validation",
        "browser",
        ("make", "qa-output"),
        browser=True,
    ),
    Gate(
        "revision-artifact-lifecycle",
        "Revision comparison, immutable artifacts, regeneration, branch, archive, restore",
        "browser",
        ("node", "scripts/project_browser_qa.mjs"),
        browser=True,
    ),
)

KNOWN_BLOCKERS: tuple[Blocker, ...] = ()

# Useful characterization, not a prerequisite for the free local-web beta.
PHYSICAL_FOLLOWUPS = (
    Blocker(
        "optional-physical-specimens",
        "Record physical 0.2 mm and 0.4 mm P2S print-quality evidence",
        (
            "Software and installed-slicer goldens cannot prove dot survival, holes, line "
            "continuity, adhesion, or color appearance."
        ),
        "Optional",
        "Does not block the open-source beta",
        (
            "Print the committed release fixture on P2S with both nozzle profiles.",
            (
                "Photograph and score dots, holes, thin lines, islands, registration, "
                "warping, and face quality."
            ),
            "Record filament, plate, orientation, slicer version, and pass/fail decision.",
        ),
    ),
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--only", action="append", choices=[gate.id for gate in GATES])
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="report software readiness; retained as an alias for existing local audit commands",
    )
    parser.add_argument("--list", action="store_true", help="list the matrix without running it")
    return parser.parse_args()


def git_output(*arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments), cwd=ROOT, text=True, capture_output=True, check=False
    )
    return result.stdout.strip()


def performance_command(run_id: str, commit: str, evidence_dir: Path) -> tuple[str, ...]:
    return (
        ".venv/bin/python",
        "benchmarks/run_performance_gates.py",
        "--run-id",
        run_id,
        "--git-commit",
        commit,
        "--output-dir",
        str(evidence_dir / "performance"),
    )


def run_gate(gate: Gate, *, evidence_dir: Path, run_id: str, commit: str) -> GateResult:
    command = gate.command or performance_command(run_id, commit, evidence_dir)
    log_path = evidence_dir / "logs" / f"{gate.id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    if gate.id == "installed-bambu-golden":
        environment["IMAGE23MF_RUN_INSTALLED_BAMBU"] = "1"
    started = time.monotonic()
    if gate.id == "clean-source-tree":
        dirty = git_output("status", "--porcelain")
        completed = subprocess.CompletedProcess(
            command,
            1 if dirty else 0,
            stdout=(dirty + "\n") if dirty else "Clean committed source tree.\n",
        )
    else:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    duration = time.monotonic() - started
    log_path.write_text(completed.stdout, encoding="utf-8")
    status: Status = "passed" if completed.returncode == 0 else "failed"
    return GateResult(
        id=gate.id,
        title=gate.title,
        area=gate.area,
        status=status,
        duration_seconds=round(duration, 3),
        command=list(command),
        log=str(log_path.relative_to(ROOT)),
        exit_code=completed.returncode,
        named_acceptance=gate.named_acceptance,
        reason=None if status == "passed" else "Command returned a non-zero exit code.",
    )


def skipped_gate(gate: Gate, reason: str) -> GateResult:
    return GateResult(
        id=gate.id,
        title=gate.title,
        area=gate.area,
        status="skipped",
        duration_seconds=0,
        command=list(gate.command),
        log=None,
        exit_code=None,
        named_acceptance=gate.named_acceptance,
        reason=reason,
    )


def report_payload(run_id: str, commit: str, results: list[GateResult]) -> dict[str, object]:
    failed = [result.id for result in results if result.status in {"failed", "blocked"}]
    skipped = [result.id for result in results if result.status == "skipped"]
    observed = {result.id for result in results}
    missing = [gate.id for gate in GATES if gate.id not in observed]
    automated_ready = not failed and not skipped and not missing
    return {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "git_commit": commit,
        "git_dirty": bool(git_output("status", "--porcelain")),
        "machine": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "matrix_complete": not missing,
        "automated_ready": automated_ready,
        "release_scope": "open-source-beta",
        "release_ready": automated_ready and not KNOWN_BLOCKERS,
        "physical_certification": "not_claimed",
        "physical_followups": [asdict(item) for item in PHYSICAL_FOLLOWUPS],
        "failed_gate_ids": failed,
        "skipped_gate_ids": skipped,
        "not_run_gate_ids": missing,
        "results": [asdict(result) for result in results],
        "known_blockers": [asdict(blocker) for blocker in KNOWN_BLOCKERS],
    }


def write_reports(evidence_dir: Path, payload: dict[str, object]) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "report.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        f"# Image23MF release gate: {payload['run_id']}",
        "",
        f"Commit: `{payload['git_commit']}`",
        f"Automated ready: **{payload['automated_ready']}**",
        f"Open-source beta ready: **{payload['release_ready']}**",
        "Physical print certification: **not claimed**",
        "",
        "## Automated matrix",
        "",
        "| Gate | Area | Status | Duration | Evidence |",
        "|---|---|---:|---:|---|",
    ]
    for result in payload["results"]:
        evidence = f"`{result['log']}`" if result["log"] else result.get("reason") or "—"
        lines.append(
            f"| {result['title']} | {result['area']} | {result['status']} | "
            f"{result['duration_seconds']:.3f}s | {evidence} |"
        )
    lines.extend(["", "## Known blockers", ""])
    for blocker in payload["known_blockers"]:
        lines.extend(
            [
                f"### {blocker['title']}",
                "",
                blocker["reason"],
                "",
                f"Priority: **{blocker['priority']}** · Relation: {blocker['linear_relation']}",
                "",
                *[f"- {item}" for item in blocker["acceptance"]],
                "",
            ]
        )
    lines.extend(
        [
            "",
            "## Optional physical characterization",
            "",
            "Prior pipeline prints are user-reported experience. This software report does "
            "not certify a printer/nozzle combination or require a new specimen campaign.",
            "",
        ]
    )
    (evidence_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    arguments = parse_arguments()
    selected = [gate for gate in GATES if not arguments.only or gate.id in arguments.only]
    if arguments.list:
        for gate in selected:
            print(f"{gate.id:42} {gate.title}")
        print("\nSoftware blockers:")
        for blocker in KNOWN_BLOCKERS:
            print(f"{blocker.id:42} {blocker.title}")
        print("\nPhysical specimen characterization is optional for this beta.")
        return 0

    commit = git_output("rev-parse", "--short=12", "HEAD") or "unknown"
    run_id = datetime.now(timezone.utc).strftime("release-%Y%m%dT%H%M%SZ")
    results: list[GateResult] = []
    dirty_source = bool(git_output("status", "--porcelain"))
    for gate in selected:
        if arguments.skip_browser and gate.browser:
            result = skipped_gate(gate, "Browser gate omitted by --skip-browser.")
        elif gate.id == "performance-budgets" and dirty_source:
            result = skipped_gate(
                gate,
                "Performance evidence is invalid while tracked or untracked source changes exist.",
            )
        else:
            print(f"[{gate.id}] {shlex.join(gate.command) if gate.command else 'fresh benchmark'}")
            result = run_gate(
                gate,
                evidence_dir=arguments.evidence_dir,
                run_id=run_id,
                commit=commit,
            )
            print(f"[{gate.id}] {result.status} in {result.duration_seconds:.3f}s")
        results.append(result)
        write_reports(arguments.evidence_dir, report_payload(run_id, commit, results))

    payload = report_payload(run_id, commit, results)
    write_reports(arguments.evidence_dir, payload)
    print(f"Report: {arguments.evidence_dir / 'report.md'}")
    if payload["failed_gate_ids"] or payload["skipped_gate_ids"]:
        return 1
    if KNOWN_BLOCKERS and not arguments.audit:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
