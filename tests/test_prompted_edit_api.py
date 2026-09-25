import io
import time

from fastapi.testclient import TestClient
from PIL import Image

from image23mf.api.app import create_app
from image23mf.contracts.job import CanvasConfig, JobConfig, PaletteColor, PaletteConfig
from image23mf.prompted_edits import (
    PromptedEditAsset,
    PromptedEditService,
    ProviderAlternative,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderEditResponse,
    ProviderPrivacy,
)
from image23mf.settings import Settings
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    ProjectRepository,
    RevisionPublisher,
    RevisionRepository,
    open_database,
)


def png(color=(20, 80, 140, 255)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


class Provider:
    descriptor = ProviderDescriptor(
        provider_id="local-mock",
        display_name="Local Mock",
        model_id="pixel-edit",
        model_version="1",
        capabilities=ProviderCapabilities(),
        privacy=ProviderPrivacy(
            policy_version="1",
            data_residency="local",
            retention="none",
            training_use="none",
        ),
    )

    def edit(self, request, cancellation):
        image = Image.open(io.BytesIO(request.source.data)).convert("RGBA")
        image.putpixel((3, 3), (240, 90, 20, 255))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return ProviderEditResponse(
            alternatives=(
                ProviderAlternative(
                    index=0,
                    output=PromptedEditAsset.from_bytes(output.getvalue(), "image/png"),
                ),
            )
        )


def test_prompted_edit_api_reviews_evidence_and_accepts_an_immutable_child(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    store = ContentAddressedStore(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        source_blob = store.put_bytes(
            png(), namespace="assets", extension=".png", media_type="image/png"
        )
        source = AssetRepository(connection, store).register(
            source_blob,
            original_filename="source.png",
            width_px=8,
            height_px=8,
        )
        project = ProjectRepository(connection).create("Prompt API")
        config = JobConfig(
            source_asset_id=source.id,
            canvas=CanvasConfig(width_mm=80, height_mm=80),
            palette=PaletteConfig(
                colors=(
                    PaletteColor(id="blue", name="Blue", hex="#14508C"),
                    PaletteColor(id="orange", name="Orange", hex="#F05A14"),
                )
            ),
        )
        parent = RevisionPublisher(connection, store).publish(
            project_id=project.id,
            source_asset_id=source.id,
            config=config,
            engine_version="test",
            label="Parent",
        )
        DraftRepository(connection).save(
            project_id=project.id,
            config=config,
            base_revision_id=parent.revision.id,
            expected_generation=0,
        )
        preview_job_id = connection.execute(
            """
            INSERT INTO jobs(
                id, project_id, revision_id, job_type, state, stage, progress, finished_at
            ) VALUES (
                'job_prompt_preview', ?, ?, 'preview', 'succeeded', 'complete', 1,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ) RETURNING id
            """,
            (project.id, parent.revision.id),
        ).fetchone()["id"]
        connection.commit()
    finally:
        connection.close()

    app = create_app(Settings(workspace=workspace))
    app.state.prompted_edit_service = PromptedEditService((Provider(),))
    with TestClient(app) as client:
        assert client.get("/api/capabilities").json()["features"]["prompted_editing"] is True
        prepared = client.post(
            f"/api/projects/{project.id}/prompted-edits/prepare",
            json={
                "parent_revision_id": parent.revision.id,
                "preview_job_id": preview_job_id,
                "provider_id": "local-mock",
                "selection": {
                    "source_width_px": 8,
                    "source_height_px": 8,
                    "primitives": [
                        {
                            "kind": "rectangle",
                            "primitive_id": "selection_prompted_api",
                            "combine": "add",
                            "x": 2,
                            "y": 2,
                            "width": 3,
                            "height": 3,
                        }
                    ],
                },
                "prompt": "Make the selected pixel orange.",
            },
        )
        assert prepared.status_code == 200, prepared.text
        disclosure = prepared.json()
        started = client.post(
            f"/api/prompted-edits/{disclosure['session_id']}/execute",
            json={
                "request_sha256": disclosure["request_sha256"],
                "disclosure_sha256": disclosure["disclosure"]["disclosure_sha256"],
                "approved": True,
            },
        )
        assert started.status_code == 202, started.text
        deadline = time.monotonic() + 3
        while True:
            session = client.get(f"/api/prompted-edits/{disclosure['session_id']}").json()
            if session["status"] in {"complete", "failed"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert session["status"] == "complete", session
        alternative = session["alternatives"][0]
        assert alternative["changed_pixel_count"] == 1
        assert client.get(alternative["changed_mask_url"]).content.count(1) == 1
        assert client.get(alternative["changed_mask_preview_url"]).status_code == 200
        retried = client.post(f"/api/prompted-edits/{session['id']}/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json()["session_id"] != session["id"]

        accepted = client.post(
            f"/api/projects/{project.id}/prompted-edits/{session['id']}/alternatives/"
            f"{alternative['id']}/accept",
            json={"expected_draft_generation": 1, "label": "Accepted orange pixel"},
        )
        assert accepted.status_code == 200, accepted.text
        body = accepted.json()
        assert body["session"]["status"] == "accepted"
        assert body["draft_generation"] == 2

    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        child = RevisionRepository(connection).get(body["revision_id"])
        assert child.parent_revision_id == parent.revision.id
        assert child.source_asset_id != source.id
        assert RevisionRepository(connection).get(parent.revision.id).source_asset_id == source.id
    finally:
        connection.close()
