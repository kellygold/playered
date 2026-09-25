#!/usr/bin/env python3
"""Seed an isolated interrupted workspace and prove startup recovery in real Chrome."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from image23mf.contracts.jobs import JobStage, JobType
from image23mf.storage import (
    ContentAddressedStore,
    JobRepository,
    ProjectRepository,
    open_database,
)

ROOT = Path(__file__).resolve().parents[1]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--devtools-url",
        default=os.environ.get("IMAGE23MF_DEVTOOLS_URL", "http://127.0.0.1:9223"),
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=ROOT / "workspace" / "qa" / "startup-recovery",
    )
    return parser.parse_args()


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def seed_interruption(workspace: Path) -> dict[str, str]:
    store = ContentAddressedStore(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Recovery browser fixture")
        jobs = JobRepository(connection)
        job = jobs.create(project_id=project.id, job_type=JobType.EXPORT)
        jobs.start(job.id, stage=JobStage.PACKAGING)
        orphan = store.put_bytes(
            b"completed-before-database-publication",
            namespace="artifacts",
            extension=".bin",
            media_type="application/octet-stream",
        )
        temp = workspace / "temp" / "interrupted.tmp"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(b"partial export")
        # The cleanup preview intentionally ignores recent files. Age both seeded
        # candidates so the browser proof exercises the visible dry-run list while
        # still proving that startup itself deletes nothing.
        stale_timestamp = time.time() - (2 * 24 * 60 * 60)
        os.utime(workspace / orphan.relative_path, (stale_timestamp, stale_timestamp))
        os.utime(temp, (stale_timestamp, stale_timestamp))
        return {
            "project_id": project.id,
            "job_id": job.id,
            "orphan_relative_path": orphan.relative_path,
            "temp_relative_path": temp.relative_to(workspace).as_posix(),
        }
    finally:
        connection.close()


def wait_for_json(url: str, *, timeout_seconds: float = 60) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    arguments = parse_arguments()
    evidence_dir = arguments.evidence_dir.expanduser().resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    api_log = evidence_dir / "api.log"
    web_log = evidence_dir / "web.log"
    browser_log = evidence_dir / "browser.log"
    screenshot = evidence_dir / "startup-recovery.png"
    api_port = available_port()
    web_port = available_port()
    api_process: subprocess.Popen[str] | None = None
    web_process: subprocess.Popen[str] | None = None

    with tempfile.TemporaryDirectory(prefix="image23mf-recovery-qa-") as directory:
        workspace = Path(directory) / "workspace"
        seeded = seed_interruption(workspace)
        environment = os.environ.copy()
        environment.update(
            {
                "IMAGE23MF_WORKSPACE": str(workspace),
                "IMAGE23MF_HOST": "127.0.0.1",
                "IMAGE23MF_PORT": str(api_port),
            }
        )
        try:
            with (
                api_log.open("w", encoding="utf-8") as api_output,
                web_log.open("w", encoding="utf-8") as web_output,
            ):
                api_process = subprocess.Popen(
                    (
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "image23mf.api.app:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(api_port),
                    ),
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    stdout=api_output,
                    stderr=subprocess.STDOUT,
                )
                health = wait_for_json(f"http://127.0.0.1:{api_port}/api/health")
                if Path(str(health["workspace"])).resolve() != workspace.resolve():
                    raise RuntimeError("isolated API did not use the seeded recovery workspace")

                web_environment = os.environ.copy()
                web_environment["IMAGE23MF_API_TARGET"] = f"http://127.0.0.1:{api_port}"
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
                wait_for_json(f"http://127.0.0.1:{web_port}/api/health")

                browser_environment = os.environ.copy()
                browser_environment.update(
                    {
                        "IMAGE23MF_DEVTOOLS_URL": arguments.devtools_url,
                        "IMAGE23MF_WEB_URL": f"http://127.0.0.1:{web_port}",
                        "IMAGE23MF_QA_SCREENSHOT": str(screenshot),
                    }
                )
                try:
                    completed = subprocess.run(
                        ("node", "scripts/recovery_qa.mjs"),
                        cwd=ROOT,
                        env=browser_environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=120,
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    output = error.stdout or ""
                    if isinstance(output, bytes):
                        output = output.decode("utf-8", errors="replace")
                    browser_log.write_text(output, encoding="utf-8")
                    raise RuntimeError(
                        "Startup-recovery browser proof exceeded its 120-second deadline; "
                        f"inspect {browser_log}."
                    ) from error
                browser_log.write_text(completed.stdout, encoding="utf-8")
                print(completed.stdout, end="")
                if completed.returncode != 0:
                    return completed.returncode

                latest = wait_for_json(f"http://127.0.0.1:{api_port}/api/workspace/recovery/latest")
                if latest["automatic_deletions"] != 0:
                    raise RuntimeError("startup recovery deleted bytes automatically")
                if latest["interrupted_jobs"][0]["job_id"] != seeded["job_id"]:
                    raise RuntimeError("browser evidence does not describe the seeded job")
                if not (workspace / seeded["orphan_relative_path"]).is_file():
                    raise RuntimeError("startup recovery deleted the orphan candidate")
                if not (workspace / seeded["temp_relative_path"]).is_file():
                    raise RuntimeError("startup recovery deleted the interrupted temp file")
                print(
                    json.dumps(
                        {
                            "isolated_workspace": True,
                            "seeded_job_id": seeded["job_id"],
                            "bytes_preserved": True,
                            "api_port": api_port,
                            "web_port": web_port,
                            "evidence_dir": str(evidence_dir),
                        },
                        indent=2,
                    )
                )
                return 0
        finally:
            if web_process is not None:
                terminate(web_process)
            if api_process is not None:
                terminate(api_process)


if __name__ == "__main__":
    raise SystemExit(main())
