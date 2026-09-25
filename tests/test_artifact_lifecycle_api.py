import io

from fastapi.testclient import TestClient
from PIL import Image

from image23mf.api.app import create_app
from image23mf.artifact_reveal import ArtifactRevealResult, FinderRevealFailedError
from image23mf.settings import Settings


def _encoded_image() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (80, 50), (20, 80, 140, 255)).save(output, format="PNG")
    return output.getvalue()


def import_project(client: TestClient) -> dict:
    response = client.post(
        "/api/projects/import?project_name=Artifact%20lifecycle%20proof",
        content=_encoded_image(),
        headers={"X-Filename": "art.png", "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def start_preview(client: TestClient, workspace: dict) -> dict:
    response = client.post(
        f"/api/projects/{workspace['project']['id']}/previews",
        json={
            "config": workspace["draft"]["config"],
            "expected_draft_generation": workspace["draft"]["generation"],
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_preview_artifact_inspect_delete_and_regenerate_recovery(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        started = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(started["job"]["id"])
        result = client.get(f"/api/jobs/{started['job']['id']}/result").json()
        artifact = next(
            item for item in result["artifacts"] if item["kind"] == "palette-preview-image"
        )
        path = f"/api/projects/{workspace['project']['id']}/artifacts/{artifact['id']}"

        inspection = client.get(f"{path}/inspection")
        assert inspection.status_code == 200, inspection.text
        assert inspection.json() == {
            "artifact_id": artifact["id"],
            "kind": "palette-preview-image",
            "integrity": "verified",
            "immutable": False,
            "regenerable": True,
            "revealable": False,
            "recovery_action": "Generate a new preview to recreate this artifact.",
        }
        deleted = client.delete(path)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_record"] is True
        assert deleted.json()["recovery_action"].startswith("Generate a new preview")
        assert client.get(f"{path}/inspection").status_code == 404

        reopened = client.get(f"/api/projects/{workspace['project']['id']}").json()
        regenerated = start_preview(client, reopened)
        app.state.worker_manager.wait_for_terminal(regenerated["job"]["id"])
        recovered = client.get(f"/api/jobs/{regenerated['job']['id']}/result").json()
        replacement = next(
            item for item in recovered["artifacts"] if item["kind"] == "palette-preview-image"
        )
        assert replacement["id"] != artifact["id"]


def test_reveal_route_preserves_typed_capability_response(tmp_path, monkeypatch) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        started = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(started["job"]["id"])
        result = client.get(f"/api/jobs/{started['job']['id']}/result").json()
        artifact = result["artifacts"][0]
        monkeypatch.setattr(
            "image23mf.api.app.reveal_artifact_in_finder",
            lambda **_kwargs: ArtifactRevealResult(
                artifact_id=artifact["id"],
                supported=False,
                revealed=False,
                reason="Reveal in Finder is unavailable in this test runtime.",
            ),
        )
        response = client.post(
            f"/api/projects/{workspace['project']['id']}/artifacts/{artifact['id']}/reveal"
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "artifact_id": artifact["id"],
        "supported": False,
        "revealed": False,
        "reason": "Reveal in Finder is unavailable in this test runtime.",
    }


def test_published_revision_artifact_rejects_delete_and_remains_downloadable(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        started = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(started["job"]["id"])
        publication = client.post(
            f"/api/projects/{workspace['project']['id']}/revisions",
            json={
                "label": "Immutable proof",
                "expected_draft_generation": started["draft"]["generation"],
                "preview_job_id": started["job"]["id"],
            },
        )
        assert publication.status_code == 201, publication.text
        artifact = publication.json()["revision"]["artifacts"][0]
        path = f"/api/projects/{workspace['project']['id']}/artifacts/{artifact['id']}"

        rejected = client.delete(path)
        retained = client.get(path)
        inspection = client.get(f"{path}/inspection")

    assert rejected.status_code == 409
    assert rejected.json()["error"]["details"]["reason"].startswith(
        "Published revision artifacts are immutable"
    )
    assert retained.status_code == 200
    assert inspection.status_code == 200
    assert inspection.json()["immutable"] is True


def test_finder_launch_failure_is_typed_retryable_capability_error(tmp_path, monkeypatch) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        started = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(started["job"]["id"])
        result = client.get(f"/api/jobs/{started['job']['id']}/result").json()
        artifact = result["artifacts"][0]
        monkeypatch.setattr(
            "image23mf.api.app.reveal_artifact_in_finder",
            lambda **_kwargs: (_ for _ in ()).throw(
                FinderRevealFailedError("Finder launch failed safely.")
            ),
        )
        response = client.post(
            f"/api/projects/{workspace['project']['id']}/artifacts/{artifact['id']}/reveal"
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "capability_unavailable",
        "message": "Finder is temporarily unable to reveal the artifact.",
        "request_id": response.headers["X-Request-ID"],
        "retryable": True,
        "details": {
            "artifact_id": artifact["id"],
            "reason": "Finder launch failed safely.",
            "action": "Retry reveal, or download the retained artifact instead.",
        },
    }
