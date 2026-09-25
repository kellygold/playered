from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "calibration_review_browser_qa.py"
SPEC = importlib.util.spec_from_file_location("calibration_review_browser_qa", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


def test_control_markers_and_browser_result_are_atomic_and_schema_tolerant(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "control" / qa.RESTARTED_MARKER
    qa.atomic_json(marker, {"draft_id": "draft-1", "generation": 3})

    assert qa.wait_for_json_file(marker, timeout_seconds=0.1) == {
        "draft_id": "draft-1",
        "generation": 3,
    }
    assert not marker.with_suffix(".json.tmp").exists()

    log = tmp_path / "browser.log"
    log.write_text(
        "diagnostic\n" + json.dumps({"ok": True, "finalizedRunId": "run-1"}) + "\n",
        encoding="utf-8",
    )
    assert qa.parse_browser_result(log)["finalizedRunId"] == "run-1"


def test_upload_fixtures_are_closed_and_nonempty(tmp_path: Path) -> None:
    fixtures = qa.create_fixtures(tmp_path)

    assert fixtures["gcode"].read_text(encoding="utf-8").startswith("; browser QA")
    assert json.loads(fixtures["settings"].read_text(encoding="utf-8"))["plate"] == ("textured-pei")
    assert len(fixtures["photos"]) == 2
    assert all(path.stat().st_size > 0 for path in fixtures["photos"])
    with zipfile.ZipFile(fixtures["project"]) as archive:
        assert set(archive.namelist()) == {"[Content_Types].xml", "3D/3dmodel.model"}


def test_tamper_proof_restores_exact_original_bytes(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    member = workspace / "artifacts" / "sealed.zip"
    member.parent.mkdir(parents=True)
    original = b"immutable calibration evidence"
    member.write_bytes(original)
    calls = []

    def request_json(url: str, *, timeout_seconds: float = 10):
        calls.append((url, timeout_seconds, hashlib.sha256(member.read_bytes()).hexdigest()))
        if len(calls) == 1:
            return 409, {"error": {"code": "conflict"}}
        return 200, {"integrity": "verified"}

    monkeypatch.setattr(qa, "request_json", request_json)
    report = qa.prove_tamper_rejection(
        workspace,
        api_url="http://127.0.0.1:1",
        run_id="run-1",
        relative_path="artifacts/sealed.zip",
    )

    assert member.read_bytes() == original
    assert report["original_sha256"] == report["recovered_sha256"]
    assert report["tampered_sha256"] != report["original_sha256"]
    assert [item[2] for item in calls] == [
        report["tampered_sha256"],
        report["original_sha256"],
    ]
