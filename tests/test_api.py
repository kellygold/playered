import sys
import threading
import time

from fastapi.testclient import TestClient

from image23mf import __version__
from image23mf.api.app import create_app
from image23mf.contracts.job import JobConfig
from image23mf.contracts.jobs import JobStage, JobType
from image23mf.settings import Settings
from image23mf.storage import (
    ArtifactPublication,
    AssetRepository,
    ContentAddressedStore,
    JobRepository,
    ProjectRepository,
    RevisionPublisher,
    open_database,
)


def job_config(asset_id: str) -> JobConfig:
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": asset_id,
            "canvas": {"width_mm": 200, "height_mm": 200},
            "palette": {
                "colors": [
                    {"id": "cream", "name": "Cream", "hex": "#CBC6B8"},
                    {"id": "black", "name": "Black", "hex": "#000000"},
                ]
            },
        }
    )


def configured_app(tmp_path):
    workspace = tmp_path / "workspace"
    return create_app(Settings(workspace=workspace)), workspace


def test_health_and_capabilities_report_stable_typed_resources(tmp_path) -> None:
    app, _ = configured_app(tmp_path)
    client = TestClient(app)

    health = client.get("/api/health")
    capabilities = client.get("/api/capabilities")

    assert health.status_code == 200
    assert health.json()["version"] == __version__
    assert {tool["name"] for tool in health.json()["capabilities"]} == {
        "Potrace",
        "OpenSCAD",
        "Bambu Studio",
    }
    assert capabilities.status_code == 200
    assert set(capabilities.json()["features"]) == {
        "discrete_color_processing",
        "vectorization",
        "reference_geometry",
        "bambu_validation",
        "prompted_editing",
        "artifact_lifecycle",
        "finder_reveal",
    }
    assert capabilities.json()["features"]["artifact_lifecycle"] is True
    assert capabilities.json()["features"]["finder_reveal"] is (sys.platform == "darwin")
    assert all("available" in tool for tool in capabilities.json()["tools"])


def test_openapi_documents_job_artifact_capability_and_error_contracts(tmp_path) -> None:
    app, _ = configured_app(tmp_path)
    schema = TestClient(app).get("/openapi.json").json()

    assert {
        "/api/health",
        "/api/capabilities",
        "/api/profiles",
        "/api/profiles/validate",
        "/api/filaments",
        "/api/filaments/{filament_id}",
        "/api/filament-catalogs/bambu-lab-starter",
        "/api/filament-catalogs/bambu-lab-starter/import",
        "/api/projects/import",
        "/api/projects/{project_id}",
        "/api/projects/{project_id}/previews",
        "/api/projects/{project_id}/exports",
        "/api/projects/{project_id}/exports/{job_id}",
        "/api/projects/{project_id}/mural-plan",
        "/api/projects/{project_id}/mural-plan/preview",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/cancel",
        "/api/jobs/{job_id}/events",
        "/api/jobs/{job_id}/result",
        "/api/projects/{project_id}/artifacts/{artifact_id}",
        "/api/projects/{project_id}/artifacts/{artifact_id}/inspection",
        "/api/projects/{project_id}/artifacts/{artifact_id}/reveal",
        "/api/workspace/health",
        "/api/workspace/recovery/latest",
        "/api/workspace/recovery/history",
        "/api/workspace/recovery/reconcile",
        "/api/workspace/gc/plan",
        "/api/workspace/gc/apply",
    } <= set(schema["paths"])
    components = schema["components"]["schemas"]
    assert {
        "ApiErrorEnvelope",
        "JobResource",
        "CapabilitiesResponse",
        "ProfileCatalog",
        "PrintSetupRequest",
        "ValidatedPrintSetup",
        "ProjectWorkspaceResource",
        "PreviewStartResponse",
        "PreviewJobResult",
        "PreviewStatistics",
        "ExportStartResponse",
        "ExportJobResult",
    } <= set(components)
    job_schema = components["JobResource"]
    assert job_schema["properties"]["state"]["$ref"].endswith("/JobState")
    missing_response = schema["paths"]["/api/jobs/{job_id}"]["get"]["responses"]["404"]
    assert missing_response["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ApiErrorEnvelope"
    )


def test_profile_catalog_and_validation_are_data_driven_api_resources(tmp_path) -> None:
    app, _ = configured_app(tmp_path)
    client = TestClient(app)

    catalog_response = client.get("/api/profiles")
    catalog = catalog_response.json()
    printer = catalog["printers"][0]
    setup = {
        "printer_id": printer["id"],
        "nozzle_id": "nozzle-0.2-hardened-steel",
        "plate_id": printer["default_plate_id"],
        "layer_height_mm": 0.1,
        "canvas_width_mm": 200,
        "canvas_height_mm": 200,
        "base_thickness_mm": 1.2,
        "art_thickness_mm": 0.6,
    }
    validation_response = client.post("/api/profiles/validate", json=setup)

    assert catalog_response.status_code == 200
    assert catalog["catalog_id"] == "image23mf-bundled-printers"
    assert catalog["source"]["version"] == "02.07.01.62"
    assert validation_response.status_code == 200
    assert validation_response.json()["nozzle"]["diameter_mm"] == 0.2
    assert len(validation_response.json()["profile_catalog_fingerprint"]) == 64


def test_profile_validation_returns_all_actionable_compatibility_issues(tmp_path) -> None:
    app, _ = configured_app(tmp_path)
    response = TestClient(app).post(
        "/api/profiles/validate",
        headers={"X-Request-ID": "profile-proof"},
        json={
            "printer_id": "bambu-p2s",
            "nozzle_id": "unknown-nozzle",
            "plate_id": "glass",
            "layer_height_mm": 0.2,
            "canvas_width_mm": 200,
            "canvas_height_mm": 200,
            "base_thickness_mm": 1.2,
            "art_thickness_mm": 0.6,
        },
    )

    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "profile-proof"
    problem = response.json()["error"]
    assert problem["code"] == "validation_error"
    assert problem["request_id"] == "profile-proof"
    assert [issue["code"] for issue in problem["details"]["issues"]] == [
        "incompatible_nozzle",
        "incompatible_plate",
    ]
    assert all(issue["suggestion"] for issue in problem["details"]["issues"])


def test_user_can_read_and_cancel_job_with_idempotent_terminal_result(tmp_path) -> None:
    app, workspace = configured_app(tmp_path)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Cancelable")
        job = JobRepository(connection).create(project_id=project.id, job_type=JobType.PREVIEW)
        JobRepository(connection).start(job.id, stage=JobStage.QUANTIZING)
    finally:
        connection.close()
    client = TestClient(app)

    running = client.get(f"/api/jobs/{job.id}")
    canceled = client.post(f"/api/jobs/{job.id}/cancel")
    canceled_again = client.post(f"/api/jobs/{job.id}/cancel")

    assert running.json()["state"] == "running"
    assert canceled.status_code == 200
    assert canceled.json()["state"] == "canceled"
    assert canceled.json()["finished_at"] is not None
    assert canceled_again.json() == canceled.json()


def test_canceling_completed_job_returns_stable_state_conflict(tmp_path) -> None:
    app, workspace = configured_app(tmp_path)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Already complete")
        jobs = JobRepository(connection)
        job = jobs.create(project_id=project.id, job_type=JobType.VALIDATION)
        jobs.start(job.id, stage=JobStage.VALIDATING)
        jobs.succeed(job.id)
    finally:
        connection.close()

    response = TestClient(app).post(f"/api/jobs/{job.id}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_job_state"
    assert response.json()["error"]["details"] == {"job_id": job.id}

    preview_result = TestClient(app).get(f"/api/jobs/{job.id}/result")
    assert preview_result.status_code == 409
    assert preview_result.json()["error"]["code"] == "conflict"


def test_api_cancel_reaches_live_worker_token_and_stops_user_job(tmp_path) -> None:
    app, workspace = configured_app(tmp_path)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("API worker cancel")
    finally:
        connection.close()
    started = threading.Event()

    def work(context):
        started.set()
        while True:
            context.check_canceled()
            time.sleep(0.01)

    with TestClient(app) as client:
        job = app.state.worker_manager.submit(
            project_id=project.id,
            job_type=JobType.PREVIEW,
            initial_stage=JobStage.QUANTIZING,
            work=work,
        )
        assert started.wait(2)

        response = client.post(f"/api/jobs/{job.id}/cancel")
        app.state.worker_manager.wait_until_idle(job.id)

        assert response.status_code == 200
        assert response.json()["state"] == "canceled"
        assert client.get(f"/api/jobs/{job.id}").json()["state"] == "canceled"


def test_application_startup_recovers_job_lost_during_previous_process(tmp_path) -> None:
    app, workspace = configured_app(tmp_path)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Restarted app")
        jobs = JobRepository(connection)
        job = jobs.create(project_id=project.id, job_type=JobType.GEOMETRY)
        jobs.start(job.id, stage=JobStage.MESHING)
    finally:
        connection.close()

    with TestClient(app) as client:
        response = client.get(f"/api/jobs/{job.id}")

    assert response.status_code == 200
    assert response.json()["state"] == "failed"
    assert response.json()["failure"]["code"] == "worker_restarted"
    assert response.json()["failure"]["retryable"] is True


def test_recovery_api_exposes_durable_non_destructive_startup_evidence(tmp_path) -> None:
    app, workspace = configured_app(tmp_path)
    store = ContentAddressedStore(workspace)
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        project = ProjectRepository(connection).create("Recovery API")
        jobs = JobRepository(connection)
        job = jobs.create(project_id=project.id, job_type=JobType.EXPORT)
        jobs.start(job.id, stage=JobStage.PACKAGING)
        orphan = store.put_bytes(
            b"survives-startup",
            namespace="artifacts",
            extension=".3mf",
            media_type="model/3mf",
        )
        temp = workspace / "temp" / "survives-startup.tmp"
        temp.write_bytes(b"partial")
    finally:
        connection.close()

    with TestClient(app) as client:
        latest = client.get("/api/workspace/recovery/latest")
        history = client.get("/api/workspace/recovery/history")
        second = client.post("/api/workspace/recovery/reconcile")

        assert latest.status_code == 200
        evidence = latest.json()
        assert evidence["status"] == "attention"
        assert evidence["automatic_deletions"] == 0
        assert evidence["stale_temp_file_count"] == 1
        assert evidence["orphan_file_count"] == 1
        assert evidence["interrupted_jobs"][0]["job_id"] == job.id
        assert evidence["interrupted_jobs"][0]["resumable"] is False
        assert evidence["interrupted_jobs"][0]["retryable"] is True
        assert history.status_code == 200
        assert history.json()["items"][0]["id"] == evidence["id"]
        assert second.status_code == 200
        assert second.json()["interrupted_jobs"] == []
        assert store.path_for(orphan.relative_path).read_bytes() == b"survives-startup"
        assert temp.read_bytes() == b"partial"


def test_api_errors_are_structured_correlated_and_do_not_leak_internals(tmp_path) -> None:
    app, _ = configured_app(tmp_path)
    client = TestClient(app)

    missing = client.get("/api/jobs/job_missing", headers={"X-Request-ID": "user-flow-proof"})
    invalid = client.get("/api/jobs/!")
    unknown = client.get("/api/not-a-route")

    assert missing.status_code == 404
    assert missing.headers["X-Request-ID"] == "user-flow-proof"
    assert missing.json() == {
        "error": {
            "code": "not_found",
            "message": "The requested job does not exist.",
            "request_id": "user-flow-proof",
            "retryable": False,
            "details": {"resource": "job", "id": "job_missing"},
        }
    }
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert invalid.json()["error"]["details"]["issues"]
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"


def test_user_can_download_verified_artifact_and_get_actionable_corruption_error(tmp_path) -> None:
    app, workspace = configured_app(tmp_path)
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Download")
        source_blob = store.put_bytes(
            b"source", namespace="assets", extension=".png", media_type="image/png"
        )
        source = AssetRepository(connection, store).register(
            source_blob, original_filename="source.png", width_px=8, height_px=8
        )
        result_blob = store.put_bytes(
            b"artifact-payload",
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        published = RevisionPublisher(connection, store).publish(
            project_id=project.id,
            source_asset_id=source.id,
            config=job_config(source.id),
            engine_version=__version__,
            artifacts=(
                ArtifactPublication(
                    kind="processed-preview",
                    derivation_key="download-proof",
                    blob=result_blob,
                ),
            ),
        )
        artifact = published.artifacts[0]
    finally:
        connection.close()
    client = TestClient(app)

    downloaded = client.get(f"/api/projects/{project.id}/artifacts/{artifact.id}")
    assert downloaded.status_code == 200
    assert downloaded.content == b"artifact-payload"
    assert "attachment" in downloaded.headers["content-disposition"]

    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        other = ProjectRepository(connection).create("Other artifact owner")
    finally:
        connection.close()
    foreign = client.get(f"/api/projects/{other.id}/artifacts/{artifact.id}")
    absent = client.get(f"/api/projects/{other.id}/artifacts/artifact_missing")
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json()["error"]["message"] == absent.json()["error"]["message"]
    assert foreign.json()["error"]["details"]["project_id"] == other.id

    store.path_for(artifact.relative_path).unlink()
    missing = client.get(f"/api/projects/{project.id}/artifacts/{artifact.id}")
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "blob_unavailable"


def test_user_reviews_gc_dry_run_before_explicit_apply_and_referenced_file_survives(
    tmp_path,
) -> None:
    app, workspace = configured_app(tmp_path)
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        ProjectRepository(connection).create("GC API")
        source_blob = store.put_bytes(
            b"source", namespace="assets", extension=".png", media_type="image/png"
        )
        source = AssetRepository(connection, store).register(
            source_blob, original_filename="source.png", width_px=8, height_px=8
        )
        orphan = store.put_bytes(
            b"orphan",
            namespace="artifacts",
            extension=".bin",
            media_type="application/octet-stream",
        )
    finally:
        connection.close()

    with TestClient(app) as client:
        health = client.get("/api/workspace/health")
        plan = client.post("/api/workspace/gc/plan?minimum_age_seconds=0")

        assert health.status_code == 200
        assert health.json()["orphan_file_count"] == 1
        assert plan.status_code == 200
        assert plan.json()["candidate_count"] == 1
        assert store.path_for(orphan.relative_path).is_file()
        assert store.path_for(source.relative_path).is_file()

        applied = client.post(
            "/api/workspace/gc/apply",
            json={
                "plan_sha256": plan.json()["plan_sha256"],
                "minimum_age_seconds": 0,
            },
        )

        assert applied.status_code == 200
        assert applied.json()["deleted_count"] == 1
        assert not (workspace / orphan.relative_path).exists()
        assert store.path_for(source.relative_path).is_file()
