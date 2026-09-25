#!/usr/bin/env python3
"""Exercise a copied desktop engine using only Python's standard-library test driver.

No application module is imported by this driver. The child has no developer PATH,
PYTHONPATH or API credentials. Output contains synthetic IDs, never its session cookie.
"""

import argparse
import hashlib
import io
import json
import os
import secrets
import selectors
import signal
import struct
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path


def png():
    def chunk(kind, payload):
        return (
            struct.pack("!I", len(payload))
            + kind
            + payload
            + struct.pack("!I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(
        b"\0" + b"".join(bytes((255, 255, 255)) if x < 4 else bytes((0, 0, 0)) for x in range(8))
        for _ in range(8)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 8, 8, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class Engine:
    def __init__(self, app, output):
        self.app, self.output = app, output
        self.token = secrets.token_hex(32)
        self.process = None
        self.base = None

    def start(self):
        home = self.output / "home"
        workspace = self.output / "projects"
        workspace.mkdir(parents=True, exist_ok=True)
        resources = self.app / "Contents/Resources"
        self.log = (self.output / "engine.log").open("ab")
        self.process = subprocess.Popen(
            [str(resources / "engine/image23mf-engine")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            cwd=workspace,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
        )
        config = {
            "token": self.token,
            "home": str(home),
            "workspace": str(workspace),
            "web_root": str(resources / "web"),
        }
        self.process.stdin.write((json.dumps(config) + "\n").encode())
        self.process.stdin.flush()
        self.process.stdin.close()
        with selectors.DefaultSelector() as ready:
            ready.register(self.process.stdout, selectors.EVENT_READ)
            if not ready.select(45):
                raise RuntimeError("Engine readiness timed out")
            reply = json.loads(self.process.stdout.readline())
        if not reply.get("ready"):
            raise RuntimeError(reply)
        self.base = reply["url"]
        assert self.base.startswith("http://127.0.0.1:")
        return self

    def request(self, path, payload=None, headers=None, auth=True, method=None):
        values = {"Cookie": "image23mf_desktop=" + self.token} if auth else {}
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
            values["Content-Type"] = "application/json"
        values.update(headers or {})
        req = urllib.request.Request(self.base + path, data=payload, headers=values, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read()
                return response.status, json.loads(
                    body
                ) if response.headers.get_content_type() == "application/json" else body
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(15)
            except subprocess.TimeoutExpired:
                if os.getpgid(self.process.pid) == self.process.pid:
                    os.killpg(self.process.pid, signal.SIGKILL)
                else:
                    self.process.kill()
                self.process.wait()
                raise AssertionError("Engine did not quit gracefully") from None
        self.log.close()

    def wait_job(self, job):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            code, status = self.request("/api/jobs/" + job["id"])
            assert code == 200, status
            if status["state"] == "succeeded":
                return status
            if status["state"] in ["failed", "canceled"]:
                raise AssertionError(status)
            time.sleep(0.25)
        raise AssertionError("Job timed out: " + job["id"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    app, output = args.app.resolve(), args.output.resolve()
    if output.exists():
        raise SystemExit("Use a new, isolated output directory.")
    output.mkdir(parents=True)
    receipt = {
        "app": str(app),
        "build": json.loads((app / "Contents/Resources/build-info.json").read_text()),
        "checks": {},
    }
    engine = Engine(app, output)
    try:
        engine.start()
        assert engine.request("/api/health", auth=False)[0] == 403
        assert (
            engine.request("/api/health", headers={"Origin": "https://example.invalid"})[0] == 403
        )
        assert engine.request("/api/health", headers={"Host": "example.invalid"})[0] == 403
        assert engine.request("/")[0] == 200
        assert engine.request("/api/health")[0] == 200
        receipt["checks"]["loopback_session_boundary"] = "passed"
        code, _ = engine.request(
            "/api/projects/import", b"invalid image", {"X-Filename": "invalid.png"}
        )
        assert code in (400, 415, 422), code
        code, imported = engine.request(
            "/api/projects/import",
            png(),
            {"X-Filename": "desktop-proof.png", "Content-Type": "image/png"},
        )
        assert code == 201, imported
        project = imported["project"]["id"]
        draft = imported["draft"]
        code, preview = engine.request(
            f"/api/projects/{project}/previews",
            {"config": draft["config"], "expected_draft_generation": draft["generation"]},
        )
        assert code == 202, preview
        engine.wait_job(preview["job"])
        code, geometry = engine.request(
            f"/api/projects/{project}/geometry",
            {
                "preview_job_id": preview["job"]["id"],
                "expected_draft_generation": preview["draft"]["generation"],
            },
        )
        assert code == 202, geometry
        engine.wait_job(geometry["job"])
        code, result = engine.request(f"/api/projects/{project}/geometry/{geometry['job']['id']}")
        assert code == 200 and result["export_ready"], result
        artifact = result["geometry_ir"]
        request = {
            "geometry_artifact_id": artifact["id"],
            "geometry_sha256": artifact["sha256"],
            "name": "Desktop validation",
            "profile": {"nozzle_diameter_mm": 0.4, "layer_height_mm": 0.2},
            "minimum_part_thickness_mm": 0.4,
        }
        code, exported = engine.request(f"/api/projects/{project}/exports", request)
        assert code == 202, exported
        engine.wait_job(exported["job"])
        code, result = engine.request(f"/api/projects/{project}/exports/{exported['job']['id']}")
        assert code == 200, result
        archive = next(
            a
            for a in result["artifacts"]
            if a["media_type"]
            in ["model/3mf", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"]
            or a["kind"].endswith("3mf")
        )
        code, data = engine.request(archive["download_url"])
        assert code == 200
        with zipfile.ZipFile(io.BytesIO(data)) as zip_file:
            assert zip_file.testzip() is None and any(
                n.endswith(".model") for n in zip_file.namelist()
            )
        (output / "validated.3mf").write_bytes(data)
        receipt.update(
            {
                "project_id": project,
                "jobs": [preview["job"]["id"], geometry["job"]["id"], exported["job"]["id"]],
                "export_sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        receipt["checks"]["import_preview_geometry_bambu_export"] = "passed"
        assert engine.request("/__desktop/status")[1]["active_jobs"] == 0
        engine.stop()
        runtime = output / "home/Library/Application Support/Image23MF Studio/runtime"
        assert not (runtime / "server.json").exists() and not (runtime / "server.pid").exists()
        receipt["checks"]["graceful_shutdown_runtime_cleanup"] = "passed"
        engine = Engine(app, output).start()
        assert engine.request(f"/api/projects/{project}")[0] == 200
        receipt["checks"]["restart_saved_project"] = "passed"
    finally:
        engine.stop()
        receipt["engine_stopped"] = engine.process is None or engine.process.poll() is not None
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
