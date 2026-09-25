import io
import sqlite3
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from image23mf import __version__
from image23mf.api.errors import install_error_handlers
from image23mf.api.mural import create_mural_router
from image23mf.contracts.job import CropConfig, JobConfig
from image23mf.contracts.jobs import JobStage, JobType
from image23mf.contracts.mural import MuralPlanSettings, SaveMuralPlanRequest
from image23mf.contracts.processing import (
    AlphaStatistics,
    PhysicalDimensions,
    PixelDimensions,
    PreviewStatistics,
)
from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize
from image23mf.mural import (
    BedEnvelope,
    BedRectangle,
    InvalidMuralPlanSourceError,
    MuralLayout,
    MuralPlanRepository,
    MuralPlanRequest,
    MuralSourceProvenance,
    StaleMuralPlanError,
)
from image23mf.mural.service import MuralPlanningService
from image23mf.profiles import ProfileCatalogService
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    JobRepository,
    ProjectRepository,
    open_database,
)
from image23mf.storage.repositories import RepositoryError

PROFILE_SHA = ProfileCatalogService.bundled().catalog.fingerprint()


@dataclass(frozen=True)
class MuralFixture:
    project_id: str
    source_asset_id: str
    processed_artifact_id: str
    preview_artifact_id: str
    statistics_artifact_id: str
    job_id: str
    request: MuralPlanRequest
    store: ContentAddressedStore


def png_bytes(width: int, height: int, color: tuple[int, int, int, int]) -> bytes:
    encoded = io.BytesIO()
    Image.new("RGBA", (width, height), color).save(encoded, format="PNG")
    return encoded.getvalue()


def job_config(source_asset_id: str) -> JobConfig:
    return JobConfig.model_validate(
        {
            "source_asset_id": source_asset_id,
            "canvas": {"width_mm": 200, "height_mm": 120},
            "palette": {
                "colors": [
                    {"id": "bone", "name": "Bone", "hex": "#CBC6B8"},
                    {"id": "black", "name": "Black", "hex": "#000000"},
                ]
            },
        }
    )


def setup_workspace(connection: sqlite3.Connection, workspace) -> MuralFixture:
    store = ContentAddressedStore(workspace)
    project = ProjectRepository(connection).create("The Wager mural")
    source_blob = store.put_bytes(
        png_bytes(13, 9, (205, 198, 184, 255)),
        namespace="assets",
        extension=".png",
        media_type="image/png",
    )
    asset = AssetRepository(connection, store).register(
        source_blob,
        original_filename="wager.png",
        width_px=13,
        height_px=9,
        metadata={"fixture": True},
    )
    config = job_config(asset.id)
    draft = DraftRepository(connection).save(
        project_id=project.id,
        config=config,
        expected_generation=0,
    )
    jobs = JobRepository(connection)
    job = jobs.create(
        project_id=project.id,
        job_type=JobType.PREVIEW,
        request_key="wager-preview",
        supersession_key=f"preview:{project.id}",
    )
    jobs.start(job.id, stage=JobStage.NORMALIZING)
    jobs.succeed(job.id)
    size = PixelSize(width=13, height=9)
    transform = CanonicalTransform.from_crop_config(
        original_size=size,
        normalized_size=size,
        exif_orientation=1,
        crop=CropConfig(mode="stretch"),
        working_size=size,
        canvas_size=MillimetreSize(width=200, height=120),
    )
    processed_blob = store.put_bytes(
        png_bytes(13, 9, (0, 120, 191, 255)),
        namespace="artifacts",
        extension=".png",
        media_type="image/png",
    )
    preview_blob = store.put_bytes(
        png_bytes(13, 9, (205, 198, 184, 255)),
        namespace="artifacts",
        extension=".png",
        media_type="image/png",
    )
    statistics = PreviewStatistics(
        config_sha256=config.fingerprint(),
        profile_catalog_fingerprint=PROFILE_SHA,
        source_asset_id=asset.id,
        source=PixelDimensions(width=13, height=9),
        preview=PixelDimensions(width=13, height=9),
        physical=PhysicalDimensions(
            width_mm=200,
            height_mm=120,
            mm_per_pixel_x=200 / 13,
            mm_per_pixel_y=120 / 9,
        ),
        alpha=AlphaStatistics(opaque_pixels=117, translucent_pixels=0, transparent_pixels=0),
        transform=transform,
        preview_sha256=preview_blob.sha256,
    )
    statistics_blob = store.put_bytes(
        statistics.model_dump_json().encode(),
        namespace="artifacts",
        extension=".json",
        media_type="application/json",
    )
    processed_id = "artifact_wager_master"
    preview_id = "artifact_wager_preview"
    statistics_id = "artifact_wager_statistics"
    for identifier, kind, blob in (
        (processed_id, "palette-preview-image", processed_blob),
        (preview_id, "preview-image", preview_blob),
        (statistics_id, "preview-statistics", statistics_blob),
    ):
        connection.execute(
            """
            INSERT INTO artifacts(
                id, job_id, kind, sha256, derivation_key, media_type,
                relative_path, byte_size, metadata_json
            ) VALUES (?, ?, ?, ?, 'wager-preview', ?, ?, ?, '{}')
            """,
            (
                identifier,
                job.id,
                kind,
                blob.sha256,
                blob.media_type,
                blob.relative_path,
                blob.byte_size,
            ),
        )
    connection.commit()
    request = MuralPlanRequest(
        source=MuralSourceProvenance(
            source_asset_id=asset.id,
            source_asset_sha256=asset.sha256,
            processed_artifact_id=processed_id,
            processed_artifact_sha256=processed_blob.sha256,
            processed_size=size,
            canonical_transform=transform,
            config_sha256=config.fingerprint(),
            engine_version=__version__,
            draft_generation=draft.generation,
        ),
        layout=MuralLayout(
            rows=2,
            columns=3,
            panel_width_mm=200,
            panel_height_mm=200,
        ),
        bed=BedEnvelope(
            printer_id="bambu-p2s",
            plate_id="textured-pei",
            profile_catalog_fingerprint=PROFILE_SHA,
            width_mm=256,
            height_mm=256,
        ),
    )
    return MuralFixture(
        project_id=project.id,
        source_asset_id=asset.id,
        processed_artifact_id=processed_id,
        preview_artifact_id=preview_id,
        statistics_artifact_id=statistics_id,
        job_id=job.id,
        request=request,
        store=store,
    )


def resized_request(fixture: MuralFixture, width_mm: float) -> MuralPlanRequest:
    return fixture.request.model_copy(
        update={"layout": fixture.request.layout.model_copy(update={"panel_width_mm": width_mm})}
    )


def published_request(
    connection: sqlite3.Connection,
    fixture: MuralFixture,
) -> MuralPlanRequest:
    draft = connection.execute(
        "SELECT config_json, config_sha256 FROM project_drafts WHERE project_id = ?",
        (fixture.project_id,),
    ).fetchone()
    revision_id = "revision_wager_master"
    connection.execute(
        """
        INSERT INTO revisions(
            id, project_id, source_asset_id, schema_version, engine_version,
            config_json, config_sha256, label
        ) VALUES (?, ?, ?, 1, ?, ?, ?, 'Wager mural master')
        """,
        (
            revision_id,
            fixture.project_id,
            fixture.source_asset_id,
            __version__,
            draft["config_json"],
            draft["config_sha256"],
        ),
    )
    published_processed_id = "artifact_wager_published_master"
    for source_id, identifier in (
        (fixture.processed_artifact_id, published_processed_id),
        (fixture.preview_artifact_id, "artifact_wager_published_preview"),
        (fixture.statistics_artifact_id, "artifact_wager_published_statistics"),
    ):
        artifact = connection.execute(
            "SELECT * FROM artifacts WHERE id = ?", (source_id,)
        ).fetchone()
        connection.execute(
            """
            INSERT INTO artifacts(
                id, revision_id, kind, sha256, derivation_key, media_type,
                relative_path, byte_size, metadata_json
            ) VALUES (?, ?, ?, ?, 'wager-published', ?, ?, ?, ?)
            """,
            (
                identifier,
                revision_id,
                artifact["kind"],
                artifact["sha256"],
                artifact["media_type"],
                artifact["relative_path"],
                artifact["byte_size"],
                artifact["metadata_json"],
            ),
        )
    connection.commit()
    return fixture.request.model_copy(
        update={
            "source": fixture.request.source.model_copy(
                update={
                    "processed_artifact_id": published_processed_id,
                    "revision_id": revision_id,
                    "draft_generation": None,
                }
            )
        }
    )


def test_plan_save_round_trip_and_optimistic_generation(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        repository = MuralPlanRepository(connection, fixture.store)

        created = repository.save(fixture.project_id, fixture.request, expected_generation=0)
        updated = repository.save(
            fixture.project_id,
            resized_request(fixture, 190),
            expected_generation=created.generation,
        )

        assert created.generation == 1
        assert updated.generation == 2
        assert updated.created_at == created.created_at
        assert updated.request.layout.panel_width_mm == 190
        assert updated.plan.master_size_mm.width == 570
        assert repository.get(fixture.project_id) == updated
    finally:
        connection.close()


def test_service_derives_trusted_request_and_reports_stale_plan(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        service = MuralPlanningService(
            connection,
            fixture.store,
            ProfileCatalogService.bundled(),
        )
        saved = service.save(
            fixture.project_id,
            SaveMuralPlanRequest(
                expected_generation=0,
                processed_artifact_id=fixture.processed_artifact_id,
                layout=fixture.request.layout,
                edge_clearance_mm=3,
            ),
        )

        assert saved.freshness == "current"
        assert saved.request.source == fixture.request.source
        assert saved.request.bed.edge_clearance_mm == 3
        assert service.get(fixture.project_id) == saved

        current = DraftRepository(connection).get(fixture.project_id)
        assert current is not None
        config = job_config(fixture.source_asset_id).model_copy(
            update={
                "canvas": job_config(fixture.source_asset_id).canvas.model_copy(
                    update={"width_mm": 180}
                )
            }
        )
        DraftRepository(connection).save(
            project_id=fixture.project_id,
            config=config,
            expected_generation=current.generation,
        )

        reopened = service.get(fixture.project_id)
        assert reopened is not None
        assert reopened.freshness == "stale"
        assert "generation is stale" in reopened.stale_reason
    finally:
        connection.close()


def test_mural_router_create_get_conflict_and_delete_journey(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
    finally:
        connection.close()

    def database():
        request_connection = open_database(workspace / "image23mf.sqlite3")
        try:
            yield request_connection
        finally:
            request_connection.close()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_mural_router(
            database=database,
            blob_store=lambda: ContentAddressedStore(workspace),
            profiles=ProfileCatalogService.bundled,
        )
    )
    client = TestClient(app)
    endpoint = f"/api/projects/{fixture.project_id}/mural-plan"

    missing = client.get(endpoint)
    preview = client.post(
        f"{endpoint}/preview",
        json=MuralPlanSettings(
            processed_artifact_id=fixture.processed_artifact_id,
            layout=fixture.request.layout,
        ).model_dump(mode="json"),
    )
    still_missing = client.get(endpoint)
    created = client.put(
        endpoint,
        json=SaveMuralPlanRequest(
            expected_generation=0,
            processed_artifact_id=fixture.processed_artifact_id,
            layout=fixture.request.layout,
        ).model_dump(mode="json"),
    )
    reopened = client.get(endpoint)
    conflict = client.put(
        endpoint,
        json=SaveMuralPlanRequest(
            expected_generation=0,
            processed_artifact_id=fixture.processed_artifact_id,
            layout=fixture.request.layout,
        ).model_dump(mode="json"),
    )
    deleted = client.delete(endpoint, params={"expected_generation": 1})

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert preview.status_code == 200
    assert len(preview.json()["plan"]["tiles"]) == 6
    assert still_missing.status_code == 404
    assert created.status_code == 200
    assert created.json()["generation"] == 1
    assert len(created.json()["plan"]["tiles"]) == 6
    assert reopened.status_code == 200
    assert reopened.json()["freshness"] == "current"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"] == {
        "resource": "mural_plan",
        "project_id": fixture.project_id,
        "expected_generation": 0,
        "current_generation": 1,
    }
    assert deleted.status_code == 204
    assert client.get(endpoint).status_code == 404


def test_mural_preview_rejects_invalid_bed_reservations_but_reports_impossible_fit(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
    finally:
        connection.close()

    def database():
        request_connection = open_database(workspace / "image23mf.sqlite3")
        try:
            yield request_connection
        finally:
            request_connection.close()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_mural_router(
            database=database,
            blob_store=lambda: ContentAddressedStore(workspace),
            profiles=ProfileCatalogService.bundled,
        )
    )
    client = TestClient(app)
    endpoint = f"/api/projects/{fixture.project_id}/mural-plan/preview"
    base = MuralPlanSettings(
        processed_artifact_id=fixture.processed_artifact_id,
        layout=fixture.request.layout,
    )

    outside = client.post(
        endpoint,
        json=base.model_copy(
            update={
                "reserved_rectangles": (BedRectangle(x_mm=250, y_mm=0, width_mm=10, height_mm=10),)
            }
        ).model_dump(mode="json"),
    )
    clearance = client.post(
        endpoint,
        json=base.model_copy(update={"edge_clearance_mm": 128}).model_dump(mode="json"),
    )
    impossible = client.post(
        endpoint,
        json=base.model_copy(
            update={
                "layout": base.layout.model_copy(
                    update={"panel_width_mm": 300, "panel_height_mm": 280}
                )
            }
        ).model_dump(mode="json"),
    )

    assert outside.status_code == 422
    assert outside.json()["error"]["code"] == "validation_error"
    assert clearance.status_code == 422
    assert clearance.json()["error"]["code"] == "validation_error"
    assert impossible.status_code == 200
    assert impossible.json()["plan"]["all_tiles_fit"] is False
    assert "reduce panel size" in impossible.json()["plan"]["warnings"][0]


def test_published_revision_provenance_is_exact_and_cross_project_safe(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        request = published_request(connection, fixture)
        saved = MuralPlanRepository(connection, fixture.store).save(
            fixture.project_id, request, expected_generation=0
        )
        assert saved.request.source.revision_id == "revision_wager_master"

        other = ProjectRepository(connection).create("Other revision owner")
        draft = connection.execute(
            "SELECT config_json, config_sha256 FROM project_drafts WHERE project_id = ?",
            (fixture.project_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO revisions(
                id, project_id, source_asset_id, schema_version, engine_version,
                config_json, config_sha256, label
            ) VALUES ('revision_other', ?, ?, 1, ?, ?, ?, 'Other')
            """,
            (
                other.id,
                fixture.source_asset_id,
                __version__,
                draft["config_json"],
                draft["config_sha256"],
            ),
        )
        connection.commit()
        forged = request.model_copy(
            update={"source": request.source.model_copy(update={"revision_id": "revision_other"})}
        )

        with pytest.raises(InvalidMuralPlanSourceError, match="different project or artifact"):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id, forged, expected_generation=saved.generation
            )
    finally:
        connection.close()


def test_stale_save_and_delete_cannot_overwrite_newer_plan(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        repository = MuralPlanRepository(connection, fixture.store)
        repository.save(fixture.project_id, fixture.request, expected_generation=0)
        repository.save(fixture.project_id, resized_request(fixture, 190), expected_generation=1)

        with pytest.raises(StaleMuralPlanError) as save_error:
            repository.save(fixture.project_id, fixture.request, expected_generation=1)
        with pytest.raises(StaleMuralPlanError) as delete_error:
            repository.delete(fixture.project_id, expected_generation=1)

        assert save_error.value.current_generation == 2
        assert delete_error.value.current_generation == 2
        assert repository.get(fixture.project_id) is not None
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda fixture: fixture.request.model_copy(
                update={
                    "source": fixture.request.source.model_copy(
                        update={"source_asset_sha256": "f" * 64}
                    )
                }
            ),
            "source SHA-256",
        ),
        (
            lambda fixture: fixture.request.model_copy(
                update={
                    "source": fixture.request.source.model_copy(
                        update={"processed_artifact_sha256": "f" * 64}
                    )
                }
            ),
            "artifact SHA-256",
        ),
        (
            lambda fixture: fixture.request.model_copy(
                update={
                    "source": fixture.request.source.model_copy(
                        update={"processed_artifact_id": fixture.statistics_artifact_id}
                    )
                }
            ),
            "palette-preview-image",
        ),
        (
            lambda fixture: fixture.request.model_copy(
                update={
                    "source": fixture.request.source.model_copy(
                        update={"draft_generation": fixture.request.source.draft_generation + 1}
                    )
                }
            ),
            "generation is stale",
        ),
        (
            lambda fixture: fixture.request.model_copy(
                update={
                    "source": fixture.request.source.model_copy(update={"config_sha256": "f" * 64})
                }
            ),
            "configuration fingerprint is stale",
        ),
    ],
)
def test_forged_or_stale_processed_provenance_is_rejected(tmp_path, mutation, message: str) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)

        with pytest.raises(InvalidMuralPlanSourceError, match=message):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id,
                mutation(fixture),
                expected_generation=0,
            )
    finally:
        connection.close()


def test_cross_project_processed_artifact_is_rejected(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        other = ProjectRepository(connection).create("Other project")
        other_job = JobRepository(connection).create(
            project_id=other.id,
            job_type=JobType.PREVIEW,
        )
        connection.execute(
            "UPDATE artifacts SET job_id = ? WHERE id = ?",
            (other_job.id, fixture.processed_artifact_id),
        )
        connection.commit()

        with pytest.raises(InvalidMuralPlanSourceError, match="different project"):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id, fixture.request, expected_generation=0
            )
    finally:
        connection.close()


def test_missing_processed_blob_is_rejected(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        artifact_path = connection.execute(
            "SELECT relative_path FROM artifacts WHERE id = ?",
            (fixture.processed_artifact_id,),
        ).fetchone()["relative_path"]
        fixture.store.path_for(artifact_path).unlink()

        with pytest.raises(InvalidMuralPlanSourceError, match="missing or corrupt"):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id, fixture.request, expected_generation=0
            )
    finally:
        connection.close()


def test_missing_statistics_blob_and_wrong_processed_dimensions_are_rejected(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        statistics_path = connection.execute(
            "SELECT relative_path FROM artifacts WHERE id = ?",
            (fixture.statistics_artifact_id,),
        ).fetchone()["relative_path"]
        fixture.store.path_for(statistics_path).unlink()
        with pytest.raises(InvalidMuralPlanSourceError, match="statistics blob"):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id, fixture.request, expected_generation=0
            )
    finally:
        connection.close()

    other_workspace = tmp_path / "dimensions"
    connection = open_database(other_workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, other_workspace)
        wrong_blob = fixture.store.put_bytes(
            png_bytes(12, 9, (0, 120, 191, 255)),
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        connection.execute(
            """
            UPDATE artifacts
            SET sha256 = ?, relative_path = ?, byte_size = ?
            WHERE id = ?
            """,
            (
                wrong_blob.sha256,
                wrong_blob.relative_path,
                wrong_blob.byte_size,
                fixture.processed_artifact_id,
            ),
        )
        connection.commit()
        request = fixture.request.model_copy(
            update={
                "source": fixture.request.source.model_copy(
                    update={"processed_artifact_sha256": wrong_blob.sha256}
                )
            }
        )
        with pytest.raises(InvalidMuralPlanSourceError, match="dimensions"):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id, request, expected_generation=0
            )
    finally:
        connection.close()


def test_preview_statistics_require_matching_image_hash_and_derivation(tmp_path) -> None:
    workspace = tmp_path / "hash"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        replacement = fixture.store.put_bytes(
            png_bytes(13, 9, (1, 2, 3, 255)),
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        connection.execute(
            """
            UPDATE artifacts
            SET sha256 = ?, relative_path = ?, byte_size = ?
            WHERE id = ?
            """,
            (
                replacement.sha256,
                replacement.relative_path,
                replacement.byte_size,
                fixture.preview_artifact_id,
            ),
        )
        connection.commit()

        with pytest.raises(InvalidMuralPlanSourceError, match="image fingerprint"):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id, fixture.request, expected_generation=0
            )
    finally:
        connection.close()

    workspace = tmp_path / "derivation"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        connection.execute(
            "UPDATE artifacts SET derivation_key = 'other-preview' WHERE id = ?",
            (fixture.statistics_artifact_id,),
        )
        connection.commit()

        with pytest.raises(InvalidMuralPlanSourceError, match="matching preview-statistics"):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id, fixture.request, expected_generation=0
            )
    finally:
        connection.close()


def test_superseded_preview_cannot_become_a_new_mural_master(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        JobRepository(connection).create(
            project_id=fixture.project_id,
            job_type=JobType.PREVIEW,
            supersession_key=f"preview:{fixture.project_id}",
        )

        with pytest.raises(InvalidMuralPlanSourceError, match="superseded"):
            MuralPlanRepository(connection, fixture.store).save(
                fixture.project_id, fixture.request, expected_generation=0
            )
    finally:
        connection.close()


def test_project_deletion_cascades_plan_and_delete_is_generation_guarded(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        repository = MuralPlanRepository(connection, fixture.store)
        saved = repository.save(fixture.project_id, fixture.request, expected_generation=0)

        assert repository.delete(fixture.project_id, expected_generation=saved.generation) is True
        assert repository.delete(fixture.project_id, expected_generation=saved.generation) is False
        repository.save(fixture.project_id, fixture.request, expected_generation=0)
        connection.execute("DELETE FROM projects WHERE id = ?", (fixture.project_id,))
        connection.commit()

        assert repository.get(fixture.project_id) is None
    finally:
        connection.close()


def test_corrupt_or_mismatched_persisted_plan_fails_closed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        repository = MuralPlanRepository(connection, fixture.store)
        repository.save(fixture.project_id, fixture.request, expected_generation=0)
        connection.execute(
            """
            UPDATE mural_plans
            SET request_fingerprint = ?, generation = generation + 1
            WHERE project_id = ?
            """,
            ("0" * 64, fixture.project_id),
        )
        connection.commit()

        with pytest.raises(RepositoryError, match="request fingerprint"):
            repository.get(fixture.project_id)
    finally:
        connection.close()


def test_database_trigger_rejects_generation_skips(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    try:
        fixture = setup_workspace(connection, workspace)
        MuralPlanRepository(connection, fixture.store).save(
            fixture.project_id, fixture.request, expected_generation=0
        )

        with pytest.raises(sqlite3.IntegrityError, match="increase exactly once"):
            connection.execute(
                "UPDATE mural_plans SET generation = 4 WHERE project_id = ?",
                (fixture.project_id,),
            )
    finally:
        connection.close()
