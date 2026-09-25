from __future__ import annotations

import io
import time
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from image23mf.api.app import create_app
from image23mf.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def png() -> bytes:
    image = Image.new("RGB", (8, 8), (203, 198, 184))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def wait_for_job(client: TestClient, job_id: str, timeout: float = 20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["state"] in {"succeeded", "failed", "canceled", "superseded"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job_id}")


def test_current_preview_generates_project_scoped_geometry_bundle(tmp_path):
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        imported_response = client.post(
            "/api/projects/import",
            content=png(),
            headers={"Content-Type": "application/octet-stream", "X-Filename": "plate.png"},
        )
        assert imported_response.status_code == 201
        imported = imported_response.json()
        project_id = imported["project"]["id"]
        draft = imported["draft"]
        preview_response = client.post(
            f"/api/projects/{project_id}/previews",
            json={
                "config": draft["config"],
                "expected_draft_generation": draft["generation"],
            },
        )
        assert preview_response.status_code == 202
        preview_start = preview_response.json()
        preview_job = wait_for_job(client, preview_start["job"]["id"])
        assert preview_job["state"] == "succeeded", preview_job.get("failure")

        geometry_response = client.post(
            f"/api/projects/{project_id}/geometry",
            json={
                "schema_version": 1,
                "preview_job_id": preview_job["id"],
                "expected_draft_generation": preview_start["draft"]["generation"],
            },
        )
        assert geometry_response.status_code == 202, geometry_response.json()
        geometry_job = wait_for_job(client, geometry_response.json()["job"]["id"])
        assert geometry_job["state"] == "succeeded", geometry_job.get("failure")
        result_response = client.get(f"/api/projects/{project_id}/geometry/{geometry_job['id']}")
        assert result_response.status_code == 200
        result = result_response.json()
        assert result["export_ready"] is True
        assert {item["kind"] for item in result["artifacts"]} == {
            "geometry-svg",
            "geometry-ir",
            "geometry-mesh",
            "geometry-report",
            "geometry-preview",
        }
        ir_response = client.get(result["geometry_ir"]["download_url"])
        assert ir_response.status_code == 200
        assert ir_response.headers["content-type"].startswith(
            "application/vnd.image23mf.geometry+json"
        )


def test_nozzle_fixture_preview_labels_generate_quality_accepted_geometry(tmp_path):
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        imported_response = client.post(
            "/api/projects/import",
            content=(FIXTURES / "geometry-nozzle-040.png").read_bytes(),
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": "geometry-nozzle-040.png",
            },
        )
        assert imported_response.status_code == 201, imported_response.text
        imported = imported_response.json()
        project_id = imported["project"]["id"]
        config = imported["draft"]["config"]
        config["canvas"] = {"width_mm": 200, "height_mm": 200}
        config["geometry"].update({"base_thickness_mm": 1.2, "art_thickness_mm": 0.6})
        preview_response = client.post(
            f"/api/projects/{project_id}/previews",
            json={
                "config": config,
                "expected_draft_generation": imported["draft"]["generation"],
            },
        )
        assert preview_response.status_code == 202, preview_response.text
        preview_start = preview_response.json()
        preview_job = wait_for_job(client, preview_start["job"]["id"], timeout=60)
        assert preview_job["state"] == "succeeded", preview_job.get("failure")

        geometry_response = client.post(
            f"/api/projects/{project_id}/geometry",
            json={
                "schema_version": 1,
                "preview_job_id": preview_job["id"],
                "expected_draft_generation": preview_start["draft"]["generation"],
            },
        )
        assert geometry_response.status_code == 202, geometry_response.text
        geometry_job = wait_for_job(
            client,
            geometry_response.json()["job"]["id"],
            timeout=60,
        )
        assert geometry_job["state"] == "succeeded", geometry_job.get("failure")
        result = client.get(f"/api/projects/{project_id}/geometry/{geometry_job['id']}").json()
        report = client.get(result["geometry_report"]["download_url"]).json()
        document = client.get(result["geometry_ir"]["download_url"]).json()

    assert result["export_ready"] is True
    assert report["safe_for_export"] is True
    assert len(document["parts"]) == len(document["meshes"]) == 30


def test_geometry_rejects_wrong_project_and_stale_preview(tmp_path):
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        first = client.post(
            "/api/projects/import",
            content=png(),
            headers={"Content-Type": "application/octet-stream", "X-Filename": "first.png"},
        ).json()
        second = client.post(
            "/api/projects/import",
            content=png(),
            headers={"Content-Type": "application/octet-stream", "X-Filename": "second.png"},
        ).json()
        preview = client.post(
            f"/api/projects/{first['project']['id']}/previews",
            json={
                "config": first["draft"]["config"],
                "expected_draft_generation": first["draft"]["generation"],
            },
        ).json()
        assert wait_for_job(client, preview["job"]["id"])["state"] == "succeeded"
        response = client.post(
            f"/api/projects/{second['project']['id']}/geometry",
            json={
                "preview_job_id": preview["job"]["id"],
                "expected_draft_generation": second["draft"]["generation"],
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
