import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_release_gate():
    path = ROOT / "scripts" / "release_gate.py"
    spec = importlib.util.spec_from_file_location("release_gate_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_matrix_keeps_software_gates_and_optional_physical_evidence() -> None:
    gate = load_release_gate()
    gates = {item.id: item for item in gate.GATES}
    blockers = {item.id: item for item in gate.KNOWN_BLOCKERS}

    assert gates["risk-overlay-fit-and-zoom"].named_acceptance == (
        "Risk overlay Fit 100% and zoom 125%"
    )
    assert gates["preview-build-download-discoverability"].named_acceptance == (
        "Preview → Build → Download discoverability"
    )
    assert not blockers
    assert gate.PHYSICAL_FOLLOWUPS[0].id == "optional-physical-specimens"
    assert "startup-recovery-browser" in gates
    command = gate.performance_command("run", "abc123", ROOT / "evidence")
    assert "benchmarks/run_performance_gates.py" in command


def test_partial_release_report_cannot_claim_automated_or_release_readiness() -> None:
    gate = load_release_gate()
    result = gate.GateResult(
        id="process-death-recovery",
        title="Process recovery",
        area="resilience",
        status="passed",
        duration_seconds=1.0,
        command=["pytest"],
        log="evidence.log",
        exit_code=0,
        named_acceptance=None,
    )

    payload = gate.report_payload("partial", "abc123", [result])

    assert payload["matrix_complete"] is False
    assert payload["automated_ready"] is False
    assert payload["release_ready"] is False
    assert "performance-budgets" in payload["not_run_gate_ids"]


def test_physical_specimen_generator_retains_unsigned_exact_packages(tmp_path) -> None:
    completed = subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts" / "generate_physical_release_specimens.py"),
            "--output",
            str(tmp_path),
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    manifest = json.loads((tmp_path / "physical-observations.json").read_text())
    golden = json.loads((ROOT / "tests" / "goldens" / "release_gate_v1.json").read_text())
    assert manifest["status"] == "not_observed"
    assert [(item["nozzle_mm"], item["layer_height_mm"]) for item in manifest["specimens"]] == [
        (0.2, 0.1),
        (0.4, 0.2),
    ]
    for specimen in manifest["specimens"]:
        profile_key = f"{specimen['nozzle_mm']:.1f}/{specimen['layer_height_mm']:.2f}"
        assert specimen["sha256"] == golden["profiles"][profile_key]["package_sha256"]
        assert {item["outcome"] for item in specimen["observations"]} == {"not_observed"}
        assert (tmp_path / specimen["filename"]).is_file()


def test_complete_software_matrix_can_release_beta_without_print_certification(monkeypatch):
    gate = load_release_gate()
    monkeypatch.setattr(gate, "git_output", lambda *args: "")
    results = [
        gate.GateResult(
            id=item.id,
            title=item.title,
            area=item.area,
            status="passed",
            duration_seconds=1.0,
            command=list(item.command),
            log="evidence.log",
            exit_code=0,
            named_acceptance=item.named_acceptance,
        )
        for item in gate.GATES
    ]
    payload = gate.report_payload("complete", "abc123", results)
    assert payload["release_scope"] == "open-source-beta"
    assert payload["release_ready"] is True
    assert payload["physical_certification"] == "not_claimed"
    assert payload["physical_followups"]
    for status in ("failed", "skipped", "blocked"):
        results[0].status = status
        assert gate.report_payload("not-ready", "abc123", results)["release_ready"] is False
