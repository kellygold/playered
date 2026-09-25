from __future__ import annotations

import importlib
import io
import json
import uuid
import zipfile
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from image23mf.api.app import create_app
from image23mf.settings import Settings


def _temporary_bundles(workspace: Path) -> list[Path]:
    directory = workspace / "temp" / "project-bundles"
    return list(directory.iterdir()) if directory.exists() else []


def _tamper_history_state(bundle: bytes) -> bytes:
    source = io.BytesIO(bundle)
    destination = io.BytesIO()
    with (
        zipfile.ZipFile(source, "r") as archive,
        zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as rewritten,
    ):
        for info in archive.infolist():
            payload = archive.read(info)
            if info.filename == "manifest.json":
                manifest = json.loads(payload)
                manifest["history_states"][0]["exact_state_sha256"] = "0" * 64
                payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            rewritten.writestr(info, payload)
    return destination.getvalue()


def _branching_project(client: TestClient) -> str:
    fixture = Path("tests/fixtures/synthetic/geometry-nozzle-040.png")
    imported = client.post(
        "/api/projects/import?project_name=Portable%20history",
        content=fixture.read_bytes(),
        headers={"X-Filename": "portable-history.png", "Content-Type": "image/png"},
    )
    assert imported.status_code == 201, imported.text
    project_id = imported.json()["project"]["id"]
    current = imported.json()["draft"]

    def save_width(width_mm: float, label: str):
        nonlocal current
        config = deepcopy(current["config"])
        config["canvas"]["width_mm"] = width_mm
        saved = client.put(
            f"/api/projects/{project_id}/draft",
            json={
                "config": config,
                "operations": current["operations"],
                "expected_draft_generation": current["generation"],
                "history_command": {
                    "schema_version": 1,
                    "id": str(uuid.uuid4()),
                    "command_type": "config_change",
                    "label": label,
                    "before_state_sha256": current["history"]["state_sha256"],
                    "expected_cursor_node_id": current["history"]["cursor_node_id"],
                },
            },
        )
        assert saved.status_code == 200, saved.text
        current = saved.json()

    def move(direction: str):
        nonlocal current
        moved = client.post(
            f"/api/projects/{project_id}/draft/history/{direction}",
            json={
                "request_id": str(uuid.uuid4()),
                "expected_draft_generation": current["generation"],
                "expected_cursor_node_id": current["history"]["cursor_node_id"],
            },
        )
        assert moved.status_code == 200, moved.text
        current = moved.json()

    save_width(230, "Width 230")
    save_width(240, "Abandoned width 240")
    move("undo")
    save_width(250, "Active branch width 250")
    move("undo")
    return project_id


def test_api_exports_full_history_into_clean_workspace_and_redoes_after_import(
    tmp_path: Path,
) -> None:
    source_workspace = tmp_path / "source"
    source_app = create_app(Settings(workspace=source_workspace))
    with TestClient(source_app) as source_client:
        project_id = _branching_project(source_client)
        exported = source_client.get(f"/api/projects/{project_id}/bundle")
    assert exported.status_code == 200
    assert exported.headers["x-image23mf-history-mode"] == "full_history"
    assert exported.headers["x-image23mf-bundle-sha256"]
    assert 'filename="portable-history.image23mf"' in exported.headers["content-disposition"]
    assert _temporary_bundles(source_workspace) == []

    destination_workspace = tmp_path / "destination"
    destination_app = create_app(Settings(workspace=destination_workspace))
    with TestClient(destination_app) as destination_client:
        imported = destination_client.post(
            "/api/project-bundles/import",
            content=exported.content,
            headers={"Content-Type": "application/vnd.image23mf.project+zip"},
        )
        assert imported.status_code == 201, imported.text
        evidence = imported.json()
        assert evidence["history_import"] == "full_history"
        assert evidence["imported_history_nodes"] == 4
        assert evidence["abandoned_history_nodes"] == 1

        restored = destination_client.get(f"/api/projects/{evidence['project_id']}")
        assert restored.status_code == 200
        draft = restored.json()["draft"]
        assert draft["config"]["canvas"]["width_mm"] == 230
        assert draft["history"]["can_undo"] is True
        assert draft["history"]["can_redo"] is True

        redone = destination_client.post(
            f"/api/projects/{evidence['project_id']}/draft/history/redo",
            json={
                "request_id": str(uuid.uuid4()),
                "expected_draft_generation": draft["generation"],
                "expected_cursor_node_id": draft["history"]["cursor_node_id"],
            },
        )
        assert redone.status_code == 200, redone.text
        assert redone.json()["config"]["canvas"]["width_mm"] == 250
        assert redone.json()["history"]["can_redo"] is False
        assert _temporary_bundles(destination_workspace) == []


def test_api_exports_explicit_current_state_only_with_safe_project_filename(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    app = create_app(Settings(workspace=workspace))
    with TestClient(app) as client:
        project_id = _branching_project(client)
        renamed = client.get(f"/api/projects/{project_id}").json()
        # The API import already proves name-based filenames; punctuation is normalized safely.
        assert renamed["project"]["name"] == "Portable history"
        exported = client.get(f"/api/projects/{project_id}/bundle?history_mode=current_state_only")
    assert exported.status_code == 200
    assert exported.headers["x-image23mf-history-mode"] == "current_state_only"
    assert 'filename="portable-history.image23mf"' in exported.headers["content-disposition"]
    assert _temporary_bundles(workspace) == []


def test_api_rejects_tampered_bundle_atomically_and_cleans_temporary_file(
    tmp_path: Path,
) -> None:
    source_app = create_app(Settings(workspace=tmp_path / "source"))
    with TestClient(source_app) as source_client:
        project_id = _branching_project(source_client)
        exported = source_client.get(f"/api/projects/{project_id}/bundle")
    tampered = _tamper_history_state(exported.content)

    destination_workspace = tmp_path / "destination"
    destination_app = create_app(Settings(workspace=destination_workspace))
    with TestClient(destination_app) as destination_client:
        before = destination_client.get("/api/projects?include_archived=true").json()
        rejected = destination_client.post(
            "/api/project-bundles/import",
            content=tampered,
            headers={"Content-Type": "application/vnd.image23mf.project+zip"},
        )
        after = destination_client.get("/api/projects?include_archived=true").json()
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "validation_error"
    assert "invalid" in rejected.json()["error"]["message"].lower()
    assert before["total"] == after["total"] == 0
    assert _temporary_bundles(destination_workspace) == []


def test_api_duplicate_restore_is_typed_and_idempotent(tmp_path: Path) -> None:
    source_app = create_app(Settings(workspace=tmp_path / "source"))
    with TestClient(source_app) as source_client:
        project_id = _branching_project(source_client)
        bundle = source_client.get(f"/api/projects/{project_id}/bundle").content

    destination_workspace = tmp_path / "destination"
    destination_app = create_app(Settings(workspace=destination_workspace))
    with TestClient(destination_app) as destination_client:
        first = destination_client.post("/api/project-bundles/import", content=bundle)
        second = destination_client.post("/api/project-bundles/import", content=bundle)
        projects = destination_client.get("/api/projects?include_archived=true").json()
    assert first.status_code == second.status_code == 201
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert second.json()["project_id"] == first.json()["project_id"]
    assert projects["total"] == 1
    assert _temporary_bundles(destination_workspace) == []


def test_api_preflights_content_length_and_enforces_streamed_upload_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api_module = importlib.import_module("image23mf.api.app")
    monkeypatch.setattr(api_module, "MAX_PROJECT_BUNDLE_UPLOAD_BYTES", 8)
    workspace = tmp_path / "workspace"
    app = create_app(Settings(workspace=workspace))
    with TestClient(app) as client:
        preflight = client.post(
            "/api/project-bundles/import",
            content=b"x",
            headers={"Content-Length": "9"},
        )
        streamed = client.post(
            "/api/project-bundles/import",
            content=(part for part in (b"12345", b"6789")),
            headers={"Transfer-Encoding": "chunked"},
        )
    assert preflight.status_code == streamed.status_code == 413
    assert preflight.json()["error"]["code"] == "resource_limit"
    assert streamed.json()["error"]["code"] == "resource_limit"
    assert preflight.json()["error"]["details"]["maximum_bytes"] == 8
    assert _temporary_bundles(workspace) == []
