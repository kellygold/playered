import uuid

import pytest
from fastapi.testclient import TestClient

from image23mf import __version__
from image23mf.api.app import create_app
from image23mf.contracts.job import JobConfig
from image23mf.filaments import FilamentCatalogService, UnknownCatalogEntryError
from image23mf.settings import Settings
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    DuplicateFilamentError,
    FilamentInUseError,
    FilamentRepository,
    InvalidFilamentReferenceError,
    ProjectRepository,
    RevisionPublisher,
    open_database,
)


def config(asset_id: str, filament_id: str) -> JobConfig:
    return JobConfig.model_validate(
        {
            "source_asset_id": asset_id,
            "canvas": {"width_mm": 200, "height_mm": 160},
            "palette": {
                "colors": [
                    {
                        "id": "bone",
                        "name": "Bone White",
                        "hex": "#CBC6B8",
                        "filament_id": filament_id,
                    },
                    {"id": "black", "name": "Charcoal", "hex": "#000000"},
                ]
            },
        }
    )


def source(connection, store):
    blob = store.put_bytes(
        b"filament-reference-source",
        namespace="assets",
        extension=".png",
        media_type="image/png",
    )
    return AssetRepository(connection, store).register(
        blob,
        original_filename="source.png",
        width_px=100,
        height_px=80,
    )


def test_custom_filament_crud_filters_and_canonical_values(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        repository = FilamentRepository(connection)
        created = repository.create(
            manufacturer="  Jessie ",
            family="Elixir",
            name="  Miami Vice  ",
            hex_color="#fa28a0",
            material="pla",
            finish="Silk",
        )
        updated = repository.update(created.id, {"owned": True, "finish": "Glossy"})

        assert created.hex_color == "#FA28A0"
        assert updated.owned is True
        assert updated.finish == "Glossy"
        assert repository.list(owned=True, query="miami", material="PLA") == (updated,)
        assert repository.list(manufacturer="other") == ()

        repository.delete(created.id)
        assert repository.list() == ()
    finally:
        connection.close()


def test_duplicate_identity_is_case_insensitive_and_updates_are_atomic(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        repository = FilamentRepository(connection)
        first = repository.create(
            manufacturer="Bambu Lab",
            family="Matte",
            name="Bone White",
            hex_color="#CBC6B8",
        )
        second = repository.create(
            manufacturer="Bambu Lab",
            family="Matte",
            name="Charcoal",
            hex_color="#000000",
        )

        with pytest.raises(DuplicateFilamentError):
            repository.create(
                manufacturer="bambu lab",
                family="matte",
                name="bone white",
                hex_color="#cbc6b8",
            )
        with pytest.raises(DuplicateFilamentError):
            repository.update(
                second.id,
                {"name": "Bone White", "hex_color": "#CBC6B8"},
            )

        assert repository.get(first.id).name == "Bone White"
        assert repository.get(second.id).name == "Charcoal"
    finally:
        connection.close()


def test_bundled_catalog_import_is_selected_idempotent_and_owned(tmp_path) -> None:
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        repository = FilamentRepository(connection)
        service = FilamentCatalogService.bundled()
        selected = ("bambu-matte-bone-white", "bambu-matte-charcoal")

        first = service.import_entries(repository, selected)
        second = service.import_entries(repository, selected)

        assert first.catalog_fingerprint == service.catalog.fingerprint()
        assert [item.id for item in first.filaments] == [item.id for item in second.filaments]
        assert len(repository.list()) == 2
        assert all(item.owned for item in first.filaments)
        assert first.filaments[0].metadata["catalog_entry_id"] == selected[0]
        with pytest.raises(UnknownCatalogEntryError):
            service.import_entries(repository, ("not-real",))
        assert len(repository.list()) == 2
    finally:
        connection.close()


def test_missing_filament_references_cannot_enter_drafts_or_revisions(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        project = ProjectRepository(connection).create("Reference integrity")
        asset = source(connection, store)
        invalid = config(asset.id, "filament_missing")

        with pytest.raises(InvalidFilamentReferenceError) as draft_error:
            DraftRepository(connection).save(
                project_id=project.id,
                config=invalid,
                expected_generation=0,
            )
        with pytest.raises(InvalidFilamentReferenceError):
            RevisionPublisher(connection, store).publish(
                project_id=project.id,
                source_asset_id=asset.id,
                config=invalid,
                engine_version=__version__,
            )

        assert draft_error.value.missing_ids == ("filament_missing",)
        assert connection.execute("SELECT count(*) FROM project_drafts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
    finally:
        connection.close()


def test_retained_history_blocks_filament_delete_and_corrupt_target_undo_rolls_back(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        filaments = FilamentRepository(connection)
        filament = filaments.create(
            manufacturer="Bambu Lab",
            family="Matte",
            name="History Bone",
            hex_color="#DDD6C5",
        )
        project = ProjectRepository(connection).create("History reference")
        asset = source(connection, store)
        drafts = DraftRepository(connection)
        first_config = config(asset.id, filament.id)
        first = drafts.save(
            project_id=project.id,
            config=first_config,
            expected_generation=0,
        )
        colors = tuple(
            color.model_copy(update={"filament_id": None})
            if color.filament_id == filament.id
            else color
            for color in first_config.palette.colors
        )
        current_config = first_config.model_copy(
            update={"palette": first_config.palette.model_copy(update={"colors": colors})}
        )
        current = drafts.save(
            project_id=project.id,
            config=current_config,
            expected_generation=first.generation,
        )

        with pytest.raises(FilamentInUseError, match="history-state"):
            filaments.delete(filament.id)
        assert filaments.get(filament.id) == filament

        # Simulate external database damage and prove the target is rejected atomically.
        connection.execute("DELETE FROM filaments WHERE id = ?", (filament.id,))
        connection.commit()
        with pytest.raises(InvalidFilamentReferenceError):
            drafts.move_history(
                project_id=project.id,
                direction="undo",
                request_id=str(uuid.uuid4()),
                expected_generation=current.generation,
                expected_cursor_node_id=current.history.cursor_node_id,
                validate_candidate=lambda *_: None,
            )
        unchanged = drafts.get(project.id)
        assert unchanged.generation == current.generation
        assert unchanged.history.cursor_node_id == current.history.cursor_node_id
        assert unchanged.config_sha256 == current.config_sha256
    finally:
        connection.close()


def test_referenced_filament_cannot_be_deleted(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    connection = open_database(workspace / "image23mf.sqlite3")
    store = ContentAddressedStore(workspace)
    try:
        filaments = FilamentRepository(connection)
        filament = filaments.create(
            manufacturer="Bambu Lab",
            family="Matte",
            name="Bone White",
            hex_color="#CBC6B8",
        )
        project = ProjectRepository(connection).create("Protected reference")
        asset = source(connection, store)
        DraftRepository(connection).save(
            project_id=project.id,
            config=config(asset.id, filament.id),
            expected_generation=0,
        )

        with pytest.raises(FilamentInUseError, match="draft"):
            filaments.delete(filament.id)
        assert filaments.get(filament.id) == filament
    finally:
        connection.close()


def test_filament_api_user_flow_and_stable_failures(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        catalog = client.get("/api/filament-catalogs/bambu-lab-starter")
        imported = client.post(
            "/api/filament-catalogs/bambu-lab-starter/import",
            json={"entry_ids": ["bambu-matte-bone-white"]},
        )
        created = client.post(
            "/api/filaments",
            json={
                "manufacturer": "Polymaker",
                "name": "Teal",
                "hex_color": "#00A6A6",
                "owned": False,
            },
        )
        custom_id = created.json()["id"]
        updated = client.patch(f"/api/filaments/{custom_id}", json={"owned": True})
        owned = client.get("/api/filaments?owned=true&query=teal")
        duplicate = client.post(
            "/api/filaments",
            json={
                "manufacturer": "polymaker",
                "name": "teal",
                "hex_color": "#00a6a6",
            },
        )
        deleted = client.delete(f"/api/filaments/{custom_id}")
        missing = client.get(f"/api/filaments/{custom_id}")

    assert catalog.status_code == 200
    assert catalog.json()["fingerprint"]
    assert imported.status_code == 200
    assert imported.json()["filaments"][0]["owned"] is True
    assert created.status_code == 201
    assert updated.json()["owned"] is True
    assert owned.json()["items"] == [updated.json()]
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"
    assert deleted.status_code == 204
    assert missing.status_code == 404
    assert missing.json()["error"]["details"]["resource"] == "filament"
