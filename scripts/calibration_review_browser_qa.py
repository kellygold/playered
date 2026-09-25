#!/usr/bin/env python3
"""Prove calibration review, restart, backup, restore, and tamper safety in isolation.

The browser driver and this orchestrator synchronize through two bounded marker files in
``IMAGE23MF_QA_CONTROL_DIR``:

* ``browser-ready-for-restart.json`` is atomically written by the browser after it has
  persisted an incomplete draft. It contains ``draft_id``, ``generation``, and
  ``candidate_sha256``.
* ``api-restarted.json`` is atomically written here only after the first API process has
  exited, the replacement process is healthy on the same port, and its reported workspace
  is the same isolated workspace.

No user workspace or browser profile is touched. When no reachable DevTools URL is supplied,
the harness launches its own headless Google Chrome with a temporary profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from PIL import Image

from image23mf.calibration.catalogs import CalibrationCatalogRepository
from image23mf.calibration.drafts import CalibrationDraftService
from image23mf.calibration.promotion import CalibrationPromotionService
from image23mf.calibration.registry import CalibrationRegistry
from image23mf.storage import ContentAddressedStore, DraftRepository, open_database
from image23mf.workspace_health import WorkspaceHealthService

ROOT = Path(__file__).resolve().parents[1]
READY_MARKER = "browser-ready-for-restart.json"
RESTARTED_MARKER = "api-restarted.json"


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--devtools-url",
        default=os.environ.get("IMAGE23MF_DEVTOOLS_URL"),
        help="use an existing Chrome DevTools endpoint instead of launching isolated Chrome",
    )
    parser.add_argument(
        "--chrome-binary",
        type=Path,
        default=Path(os.environ["IMAGE23MF_CHROME_BINARY"])
        if os.environ.get("IMAGE23MF_CHROME_BINARY")
        else None,
    )
    parser.add_argument(
        "--browser-script",
        type=Path,
        default=ROOT / "scripts" / "calibration_review_browser_qa.mjs",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=ROOT / "workspace" / "qa" / "calibration-review",
    )
    parser.add_argument("--browser-timeout", type=float, default=300)
    parser.add_argument("--restart-timeout", type=float, default=90)
    return parser.parse_args(argv)


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def wait_for_json_file(
    path: Path,
    *,
    timeout_seconds: float,
    watched_process: subprocess.Popen[str] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if watched_process is not None and watched_process.poll() is not None:
            raise RuntimeError(
                f"browser exited with {watched_process.returncode} before writing {path.name}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("marker root is not an object")
            return payload
        except (OSError, json.JSONDecodeError, ValueError) as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {path}: {last_error}")


def request_json(url: str, *, timeout_seconds: float = 10) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
            return response.status, payload
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read())
        except json.JSONDecodeError:
            payload = {"message": str(error)}
        return error.code, payload


def wait_for_json(url: str, *, timeout_seconds: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, payload = request_json(url, timeout_seconds=2)
            if status == 200:
                return payload
            last_error = RuntimeError(f"HTTP {status}: {payload}")
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def find_chrome(explicit: Path | None = None) -> Path:
    candidates = [
        explicit,
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        discovered = shutil.which(name)
        if discovered:
            candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(
        "no Chrome binary was found; pass --chrome-binary or a reachable --devtools-url"
    )


def devtools_is_ready(url: str) -> bool:
    try:
        status, payload = request_json(f"{url.rstrip('/')}/json/version", timeout_seconds=1)
        return status == 200 and "webSocketDebuggerUrl" in payload
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def start_api(
    *, workspace: Path, port: int, output: IO[str], generation: int
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "IMAGE23MF_WORKSPACE": str(workspace),
            "IMAGE23MF_HOST": "127.0.0.1",
            "IMAGE23MF_PORT": str(port),
        }
    )
    output.write(f"\n=== API generation {generation} ===\n")
    output.flush()
    return subprocess.Popen(
        (
            sys.executable,
            "-m",
            "uvicorn",
            "image23mf.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=output,
        stderr=subprocess.STDOUT,
    )


def assert_isolated_health(health: Mapping[str, object], workspace: Path) -> None:
    reported = Path(str(health.get("workspace", ""))).resolve()
    if reported != workspace.resolve():
        raise RuntimeError(
            f"isolated API reported the wrong workspace: expected {workspace}, got {reported}"
        )


def create_fixtures(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    project = directory / "physical-calibration.3mf"
    with zipfile.ZipFile(project, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "3D/3dmodel.model",
            '<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"/>',
        )
    gcode = directory / "physical-calibration.gcode"
    gcode.write_text("; browser QA physical calibration\nG28\nM84\n", encoding="utf-8")
    settings = directory / "slicer-settings.json"
    settings.write_text(
        json.dumps(
            {
                "application": "Bambu Studio",
                "version": "2.3.0.70",
                "layer_height_mm": 0.2,
                "plate": "textured-pei",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    photos = []
    for index, color in enumerate(((20, 40, 80), (210, 120, 40))):
        photo = directory / f"printed-coupon-{index + 1}.png"
        with Image.new("RGB", (24, 24), color) as image:
            image.save(photo, format="PNG")
        photos.append(photo)
    return {
        "project": project,
        "gcode": gcode,
        "settings": settings,
        "photos": photos,
    }


def run_workspace_cli(arguments: Sequence[str], *, log: Path) -> dict[str, Any]:
    completed = subprocess.run(
        (sys.executable, "-m", "image23mf.workspace_bundle_cli", *arguments),
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    with log.open("a", encoding="utf-8") as output:
        output.write(f"\n$ image23mf-workspace {' '.join(arguments)}\n")
        output.write(completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(f"workspace CLI exited with {completed.returncode}; inspect {log}")
    payload = json.loads(completed.stdout)
    if payload.get("ok") is not True:
        raise RuntimeError(f"workspace CLI did not report success: {payload}")
    return payload


def parse_browser_result(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (value.get("status") == "passed" or value.get("ok") is True):
            return value
    raise RuntimeError(f"browser log has no final passed JSON object: {path}")


def require_identifier(payload: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    raise RuntimeError(f"browser result is missing one of: {', '.join(names)}")


def verify_workspace_closure(
    workspace: Path,
    *,
    draft_id: str,
    run_id: str,
    proposal_id: str,
    base_catalog_fingerprint: str,
    active_catalog_fingerprint: str,
    old_project_id: str,
    new_project_id: str,
) -> dict[str, Any]:
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        catalogs = CalibrationCatalogRepository(connection).list()
        if catalogs.active_fingerprint != active_catalog_fingerprint:
            raise RuntimeError("restored active catalog does not match browser-reviewed head")
        if len(catalogs.items) < 2:
            raise RuntimeError("restored workspace did not retain historical catalog versions")
        run = CalibrationRegistry(connection, store, catalogs.items[0].catalog).get(run_id)
        draft = CalibrationDraftService(connection, store).get(draft_id)
        proposal = CalibrationPromotionService(connection, store).get(proposal_id)
        if draft.finalized_run_id != run_id:
            raise RuntimeError("restored draft is not linked to its sealed run")
        if proposal.state.value != "accepted":
            raise RuntimeError("restored proposal is not accepted")
        project_pins: dict[str, str] = {}
        for label, project_id in (("before", old_project_id), ("after", new_project_id)):
            retained = DraftRepository(connection).get(project_id)
            if retained is None:
                raise RuntimeError(f"restored {label}-promotion project draft is missing")
            cleanup = retained.config.get("cleanup")
            pin = (
                cleanup.get("printability_profile_catalog_fingerprint")
                if isinstance(cleanup, dict)
                else None
            )
            if not isinstance(pin, str) or not pin:
                raise RuntimeError(
                    f"restored {label}-promotion project has no retained catalog pin"
                )
            project_pins[label] = pin
        if project_pins["before"] != base_catalog_fingerprint:
            raise RuntimeError("pre-promotion project does not retain the exact base catalog")
        if project_pins["after"] != active_catalog_fingerprint:
            raise RuntimeError("post-promotion project did not pin the reviewed catalog head")
        health = WorkspaceHealthService(connection, store).inspect()
        error_issues = [item for item in health.issues if item.severity.value == "error"]
        if error_issues:
            raise RuntimeError(f"restored workspace health reports errors: {error_issues}")
        return {
            "catalog_fingerprints": sorted(item.fingerprint for item in catalogs.items),
            "active_catalog_fingerprint": catalogs.active_fingerprint,
            "run_evidence_sha256": run.evidence_sha256,
            "run_bundle_sha256": run.source_bundle.sha256,
            "run_bundle_relative_path": run.source_bundle.relative_path,
            "draft_generation": draft.generation,
            "draft_candidate_sha256": draft.candidate_sha256,
            "proposal_state": proposal.state.value,
            "project_catalog_pins": project_pins,
            "workspace_referenced_files": health.referenced_file_count,
        }
    finally:
        connection.close()


def prove_tamper_rejection(
    workspace: Path, *, api_url: str, run_id: str, relative_path: str
) -> dict[str, Any]:
    target = workspace / relative_path
    original = target.read_bytes()
    original_sha256 = hashlib.sha256(original).hexdigest()
    if not original:
        raise RuntimeError("selected retained calibration member is empty")
    damaged = bytearray(original)
    damaged[len(damaged) // 2] ^= 0xFF
    target.write_bytes(damaged)
    tampered_sha256 = sha256_path(target)
    try:
        status, payload = request_json(f"{api_url}/api/calibration/runs/{run_id}")
        if status != 409 or payload.get("error", {}).get("code") != "conflict":
            raise RuntimeError(
                f"tampered calibration evidence was not rejected: HTTP {status} {payload}"
            )
    finally:
        target.write_bytes(original)
    recovered_sha256 = sha256_path(target)
    status, recovered = request_json(f"{api_url}/api/calibration/runs/{run_id}")
    if status != 200 or recovered.get("integrity") != "verified":
        raise RuntimeError(f"recovered calibration evidence did not verify: {status} {recovered}")
    if recovered_sha256 != original_sha256 or tampered_sha256 == original_sha256:
        raise RuntimeError("tamper/recovery hashes do not prove an exact byte restoration")
    return {
        "relative_path": relative_path,
        "original_sha256": original_sha256,
        "tampered_sha256": tampered_sha256,
        "recovered_sha256": recovered_sha256,
        "rejection_status": 409,
        "recovery_status": 200,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    evidence_dir = arguments.evidence_dir.expanduser().resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    api_log = evidence_dir / "api.log"
    web_log = evidence_dir / "web.log"
    chrome_log = evidence_dir / "chrome.log"
    browser_log = evidence_dir / "browser.log"
    workspace_log = evidence_dir / "workspace-cli.log"
    report_path = evidence_dir / "report.json"
    api_port, web_port = available_port(), available_port()
    api_url = f"http://127.0.0.1:{api_port}"
    web_url = f"http://127.0.0.1:{web_port}"
    api_process: subprocess.Popen[str] | None = None
    web_process: subprocess.Popen[str] | None = None
    chrome_process: subprocess.Popen[str] | None = None
    browser_process: subprocess.Popen[str] | None = None

    with tempfile.TemporaryDirectory(prefix="image23mf-calibration-review-qa-") as directory:
        temporary = Path(directory)
        workspace = temporary / "workspace"
        restored_workspace = temporary / "restored-workspace"
        control_dir = temporary / "control"
        fixtures = create_fixtures(temporary / "fixtures")
        bundle = temporary / "calibration-review.image23mf-workspace"
        devtools_url = arguments.devtools_url
        api_output = api_log.open("w", encoding="utf-8")
        web_output = web_log.open("w", encoding="utf-8")
        chrome_output = chrome_log.open("w", encoding="utf-8")
        browser_output = browser_log.open("w", encoding="utf-8")
        try:
            api_process = start_api(
                workspace=workspace, port=api_port, output=api_output, generation=1
            )
            assert_isolated_health(wait_for_json(f"{api_url}/api/health"), workspace)

            web_environment = os.environ.copy()
            web_environment["IMAGE23MF_API_TARGET"] = api_url
            web_process = subprocess.Popen(
                (
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    "--strictPort",
                ),
                cwd=ROOT / "frontend",
                env=web_environment,
                text=True,
                stdout=web_output,
                stderr=subprocess.STDOUT,
            )
            assert_isolated_health(wait_for_json(f"{web_url}/api/health"), workspace)

            if not devtools_url or not devtools_is_ready(devtools_url):
                chrome_port = available_port()
                devtools_url = f"http://127.0.0.1:{chrome_port}"
                chrome = find_chrome(arguments.chrome_binary)
                chrome_process = subprocess.Popen(
                    (
                        str(chrome),
                        "--headless=new",
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--no-default-browser-check",
                        "--no-first-run",
                        "--remote-debugging-address=127.0.0.1",
                        f"--remote-debugging-port={chrome_port}",
                        f"--user-data-dir={temporary / 'chrome-profile'}",
                        "about:blank",
                    ),
                    cwd=ROOT,
                    text=True,
                    stdout=chrome_output,
                    stderr=subprocess.STDOUT,
                )
                wait_for_json(f"{devtools_url}/json/version")

            browser_environment = os.environ.copy()
            browser_environment.update(
                {
                    "IMAGE23MF_DEVTOOLS_URL": devtools_url,
                    "IMAGE23MF_WEB_URL": web_url,
                    "IMAGE23MF_API_URL": api_url,
                    "IMAGE23MF_QA_CONTROL_DIR": str(control_dir),
                    "IMAGE23MF_CALIBRATION_QA_OUTPUT": str(evidence_dir),
                    "IMAGE23MF_CALIBRATION_QA_REPORT": str(evidence_dir / "browser-report.json"),
                    "IMAGE23MF_CALIBRATION_PROJECT_3MF": str(fixtures["project"]),
                    "IMAGE23MF_CALIBRATION_GCODE": str(fixtures["gcode"]),
                    "IMAGE23MF_CALIBRATION_SETTINGS": str(fixtures["settings"]),
                    "IMAGE23MF_CALIBRATION_PHOTOS": os.pathsep.join(
                        str(item) for item in fixtures["photos"]
                    ),
                    "IMAGE23MF_CALIBRATION_ATTESTATION": "confirmed",
                }
            )
            if not arguments.browser_script.is_file():
                raise RuntimeError(f"browser script does not exist: {arguments.browser_script}")
            browser_process = subprocess.Popen(
                ("node", str(arguments.browser_script)),
                cwd=ROOT,
                env=browser_environment,
                text=True,
                stdout=browser_output,
                stderr=subprocess.STDOUT,
            )
            restart_request = wait_for_json_file(
                control_dir / READY_MARKER,
                timeout_seconds=arguments.restart_timeout,
                watched_process=browser_process,
            )
            draft_id = require_identifier(restart_request, "draft_id")
            candidate_sha256 = require_identifier(restart_request, "candidate_sha256")
            generation = restart_request.get("generation")
            if not isinstance(generation, int) or generation <= 0:
                raise RuntimeError("browser restart marker has an invalid draft generation")

            terminate(api_process)
            api_process = start_api(
                workspace=workspace, port=api_port, output=api_output, generation=2
            )
            assert_isolated_health(wait_for_json(f"{api_url}/api/health"), workspace)
            status, resumed = request_json(f"{api_url}/api/calibration/drafts/{draft_id}")
            if (
                status != 200
                or resumed.get("generation") != generation
                or resumed.get("candidate_sha256") != candidate_sha256
            ):
                raise RuntimeError(
                    "API restart did not retain the exact browser-authored calibration draft"
                )
            atomic_json(
                control_dir / RESTARTED_MARKER,
                {
                    "draft_id": draft_id,
                    "generation": generation,
                    "candidate_sha256": candidate_sha256,
                    "api_pid": api_process.pid,
                    "restarted_at_unix": time.time(),
                },
            )
            try:
                browser_returncode = browser_process.wait(timeout=arguments.browser_timeout)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    f"calibration browser proof exceeded its deadline; inspect {browser_log}"
                ) from error
            browser_output.flush()
            if browser_returncode != 0:
                raise RuntimeError(
                    "calibration browser proof exited with "
                    f"{browser_returncode}; inspect {browser_log}"
                )
            browser_result = parse_browser_result(browser_log)
            run_id = require_identifier(browser_result, "run_id", "finalizedRunId")
            proposal_id = require_identifier(browser_result, "proposal_id", "proposalId")
            base_fingerprint = require_identifier(
                browser_result, "base_catalog_fingerprint", "baseCatalogFingerprint"
            )
            active_fingerprint = require_identifier(
                browser_result,
                "active_catalog_fingerprint",
                "activeCatalogFingerprint",
                "catalog_fingerprint",
            )
            old_project_id = require_identifier(browser_result, "old_project_id", "oldProjectId")
            new_project_id = require_identifier(browser_result, "new_project_id", "newProjectId")

            source_closure = verify_workspace_closure(
                workspace,
                draft_id=draft_id,
                run_id=run_id,
                proposal_id=proposal_id,
                base_catalog_fingerprint=base_fingerprint,
                active_catalog_fingerprint=active_fingerprint,
                old_project_id=old_project_id,
                new_project_id=new_project_id,
            )
            backup = run_workspace_cli(
                ("backup", "--workspace", str(workspace), "--output", str(bundle)),
                log=workspace_log,
            )
            if backup["sha256"] != sha256_path(bundle):
                raise RuntimeError("workspace backup CLI hash does not match retained bytes")
            preflight = run_workspace_cli(
                ("preflight", "--bundle", str(bundle), "--target", str(restored_workspace)),
                log=workspace_log,
            )
            if preflight["preflight"]["target_state"] != "missing":
                raise RuntimeError("workspace restore preflight did not target a missing workspace")
            restored = run_workspace_cli(
                ("restore", "--bundle", str(bundle), "--target", str(restored_workspace)),
                log=workspace_log,
            )
            restored_closure = verify_workspace_closure(
                restored_workspace,
                draft_id=draft_id,
                run_id=run_id,
                proposal_id=proposal_id,
                base_catalog_fingerprint=base_fingerprint,
                active_catalog_fingerprint=active_fingerprint,
                old_project_id=old_project_id,
                new_project_id=new_project_id,
            )
            if source_closure != restored_closure:
                raise RuntimeError("restored calibration closure differs from the source workspace")

            tamper = prove_tamper_rejection(
                workspace,
                api_url=api_url,
                run_id=run_id,
                relative_path=source_closure["run_bundle_relative_path"],
            )
            report = {
                "status": "passed",
                "isolated_workspace": True,
                "isolated_browser_profile": chrome_process is not None,
                "api_restart": {
                    "draft_id": draft_id,
                    "generation": generation,
                    "candidate_sha256": candidate_sha256,
                    "exact_resume_verified": True,
                },
                "browser": browser_result,
                "source_closure": source_closure,
                "restored_closure": restored_closure,
                "backup": {
                    "path": str(bundle),
                    "sha256": backup["sha256"],
                    "verified_sha256": sha256_path(bundle),
                    "byte_size": backup["byte_size"],
                    "preflight": preflight["preflight"],
                    "restore_report": restored["report"],
                },
                "tamper": tamper,
                "evidence": {
                    "api_log": str(api_log),
                    "web_log": str(web_log),
                    "chrome_log": str(chrome_log),
                    "browser_log": str(browser_log),
                    "workspace_cli_log": str(workspace_log),
                    "browser_report": str(evidence_dir / "browser-report.json"),
                    "desktop_screenshot": str(evidence_dir / "proposal-review-desktop.png"),
                    "compact_screenshot": str(evidence_dir / "proposal-review-compact.png"),
                },
            }
            atomic_json(report_path, report)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        finally:
            if browser_process is not None:
                terminate(browser_process)
            if web_process is not None:
                terminate(web_process)
            if api_process is not None:
                terminate(api_process)
            if chrome_process is not None:
                terminate(chrome_process)
            browser_output.close()
            chrome_output.close()
            web_output.close()
            api_output.close()


if __name__ == "__main__":
    raise SystemExit(main())
