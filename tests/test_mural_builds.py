from __future__ import annotations

import io
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from image23mf.api.app import create_app
from image23mf.bambu.cli import (
    BambuSliceProfiles,
    BambuStudioUnavailableError,
    BambuValidationResult,
    BambuValidationStatus,
    PinnedBambuProfile,
)
from image23mf.bambu.mural_package import read_bambu_mural_3mf
from image23mf.mural.assembly_aids import MuralAssemblyAidsManifest
from image23mf.mural.topology_partition import MuralTopologyPartitionManifest
from image23mf.settings import Settings
from image23mf.storage import ArtifactRepository, open_database


def _png(*, single_color: bool = False) -> bytes:
    image = Image.new("RGB", (8, 8), "#CBC6B8")
    if not single_color:
        ImageDraw.Draw(image).rectangle((4, 0, 7, 7), fill="#000000")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _wait(client: TestClient, job_id: str, *, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in {"succeeded", "failed", "canceled", "superseded"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job_id}")


def _validated(_source=None, **_kwargs) -> BambuValidationResult:
    return BambuValidationResult(
        status=BambuValidationStatus.VALIDATED,
        source_sha256="a" * 64,
        executable=Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
        version="test",
        command=("BambuStudio", "--slice"),
        return_code=0,
        stdout="ok",
        stderr="",
        duration_seconds=0.1,
        timed_out=False,
        canceled=False,
        warnings=(),
    )


class _ValidatedSlicer:
    def __init__(self) -> None:
        self.expected_colors = None

    def validate(self, source, **kwargs):
        self.expected_colors = kwargs["expectation"].expected_colors
        return _validated(source, **kwargs)


class _BlockingSlicer:
    def __init__(self) -> None:
        self.entered = threading.Event()

    def validate(self, _source=None, **kwargs):
        cancellation = kwargs["cancellation"]
        self.entered.set()
        while not cancellation.canceled:
            time.sleep(0.01)
        result = BambuValidationResult(
            status=BambuValidationStatus.CANCELED,
            source_sha256=None,
            executable=None,
            version=None,
            command=(),
            return_code=None,
            stdout="",
            stderr="",
            duration_seconds=0,
            timed_out=False,
            canceled=True,
            warnings=(),
        )
        from image23mf.bambu.cli import BambuStudioCanceledError

        raise BambuStudioCanceledError("canceled", result)


class _UnavailableSlicer:
    @staticmethod
    def validate(_source=None, **_kwargs):
        result = BambuValidationResult(
            status=BambuValidationStatus.UNAVAILABLE,
            source_sha256=None,
            executable=None,
            version=None,
            command=(),
            return_code=None,
            stdout="",
            stderr="",
            duration_seconds=0,
            timed_out=False,
            canceled=False,
            warnings=(),
        )
        raise BambuStudioUnavailableError("Bambu Studio is not installed", result)


def _profiles(tmp_path: Path) -> BambuSliceProfiles:
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    pinned = PinnedBambuProfile.pin(profile)
    return BambuSliceProfiles(machine=pinned, process=pinned, filaments=(pinned, pinned))


def _prepare(client: TestClient, *, single_color: bool = False) -> dict:
    imported = client.post(
        "/api/projects/import",
        content=_png(single_color=single_color),
        headers={"Content-Type": "application/octet-stream", "X-Filename": "mural.png"},
    ).json()
    project_id = imported["project"]["id"]
    config = imported["draft"]["config"]
    config["canvas"] = {"width_mm": 200, "height_mm": 120}
    config["palette"]["colors"] = [
        {"id": "bone", "name": "Bone", "hex": "#CBC6B8"},
        {"id": "black", "name": "Black", "hex": "#000000"},
    ]
    preview_start = client.post(
        f"/api/projects/{project_id}/previews",
        json={
            "config": config,
            "expected_draft_generation": imported["draft"]["generation"],
        },
    ).json()
    preview_job = _wait(client, preview_start["job"]["id"])
    assert preview_job["state"] == "succeeded", preview_job.get("failure")
    preview_result = client.get(f"/api/jobs/{preview_job['id']}/result").json()
    processed = next(
        item for item in preview_result["artifacts"] if item["kind"] == "palette-preview-image"
    )
    plan = client.put(
        f"/api/projects/{project_id}/mural-plan",
        json={
            "expected_generation": 0,
            "processed_artifact_id": processed["id"],
            "layout": {
                "rows": 1,
                "columns": 2,
                "panel_width_mm": 120,
                "panel_height_mm": 80,
            },
            "reserved_rectangles": [{"x_mm": 205, "y_mm": 205, "width_mm": 49, "height_mm": 49}],
        },
    )
    assert plan.status_code == 200, plan.text
    geometry_start = client.post(
        f"/api/projects/{project_id}/geometry",
        json={
            "preview_job_id": preview_job["id"],
            "expected_draft_generation": preview_start["draft"]["generation"],
        },
    ).json()
    geometry_job = _wait(client, geometry_start["job"]["id"])
    assert geometry_job["state"] == "succeeded", geometry_job.get("failure")
    geometry = client.get(f"/api/projects/{project_id}/geometry/{geometry_job['id']}").json()[
        "geometry_ir"
    ]
    return {
        "project_id": project_id,
        "plan": plan.json(),
        "geometry": geometry,
        "processed_artifact_id": processed["id"],
    }


def _build_body(prepared: dict) -> dict:
    return {
        "schema_version": 1,
        "expected_plan_generation": prepared["plan"]["generation"],
        "expected_request_fingerprint": prepared["plan"]["plan"]["request_fingerprint"],
        "geometry_artifact_id": prepared["geometry"]["id"],
        "geometry_sha256": prepared["geometry"]["sha256"],
        "name": "Scaled two-panel mural",
        "profile": {
            "printer_model": "Bambu Lab P2S",
            "nozzle_diameter_mm": 0.4,
            "layer_height_mm": 0.2,
            "bed_type": "Textured PEI Plate",
        },
    }


def test_mural_build_scales_master_persists_exact_qa_and_reuses_verified_cache(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace", bambu_resources_root=None))
    slicer = _ValidatedSlicer()
    app.state.mural_build_service.validator = slicer
    app.state.mural_build_service.profile_provider = lambda _project: _profiles(tmp_path)
    with TestClient(app) as client:
        prepared = _prepare(client)
        body = _build_body(prepared)
        started = client.post(f"/api/projects/{prepared['project_id']}/mural-builds", json=body)
        assert started.status_code == 202, started.text
        assert started.json()["cache_hit"] is False
        job = _wait(client, started.json()["job"]["id"], timeout=60)
        assert job["state"] == "succeeded", job.get("failure")

        result = client.get(
            f"/api/projects/{prepared['project_id']}/mural-builds/{job['id']}"
        ).json()
        assert result["freshness"] == "current"
        assert result["download_ready"] is True
        assert result["validation"]["status"] == "validated"
        assert slicer.expected_colors == ("#CBC6B8", "#000000")
        assert {item["kind"] for item in result["artifacts"]} == {
            "bambu-mural-3mf",
            "mural-label-partition",
            "mural-topology-partition",
            "mural-seam-qa",
            "bambu-mural-validation-report",
            "bambu-mural-validation-log",
        }
        seam_qa = client.get(result["seam_qa"]["download_url"]).json()
        assert seam_qa["request_fingerprint"] == body["expected_request_fingerprint"]
        assert seam_qa["master_size_mm"] == {"width": 240.0, "height": 80.0}
        assert seam_qa["status"] == "pass"
        topology_artifact = next(
            item for item in result["artifacts"] if item["kind"] == "mural-topology-partition"
        )
        topology_payload = client.get(topology_artifact["download_url"]).json()
        topology_manifest = MuralTopologyPartitionManifest.model_validate(topology_payload)
        assert all(
            tile["raster_evidence_policy"] == "separate_nominal_label_recomposition"
            and "tile_raster_sha256" in tile
            and "nominal_label_crop_sha256" not in tile
            for tile in topology_payload["tiles"]
        )
        assert all(
            tile.nominal_label_crop_sha256 == tile.visible_labels_sha256
            for tile in topology_manifest.tiles
        )
        package_response = client.get(result["package"]["download_url"])
        assert package_response.status_code == 200
        package = read_bambu_mural_3mf(package_response.content)
        assert package.prime_tower_reserve.x_mm == 205
        assert package.prime_tower_reserve.tower_x_mm == 208
        assert package.prime_tower_reserve.tower_y_mm == 208

        repeated = client.post(f"/api/projects/{prepared['project_id']}/mural-builds", json=body)
        assert repeated.status_code == 202
        assert repeated.json()["cache_hit"] is True
        assert repeated.json()["job"]["id"] == job["id"]

        changed = client.put(
            f"/api/projects/{prepared['project_id']}/mural-plan",
            json={
                "expected_generation": 1,
                "processed_artifact_id": prepared["processed_artifact_id"],
                "layout": {
                    **prepared["plan"]["request"]["layout"],
                    "horizontal_gap_mm": 1,
                },
                "reserved_rectangles": prepared["plan"]["request"]["reserved_rectangles"],
            },
        )
        assert changed.status_code == 200
        stale_result = client.get(
            f"/api/projects/{prepared['project_id']}/mural-builds/{job['id']}"
        ).json()
        assert stale_result["freshness"] == "stale"
        assert stale_result["download_ready"] is False


def test_optional_assembly_aids_publish_printable_guide_without_changing_3mf_or_art(
    tmp_path,
) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace", bambu_resources_root=None))
    app.state.mural_build_service.validator = _ValidatedSlicer()
    app.state.mural_build_service.profile_provider = lambda _project: _profiles(tmp_path)
    with TestClient(app) as client:
        prepared = _prepare(client)
        body = {**_build_body(prepared), "name": "The Wager & arrival <guide>"}

        plain_start = client.post(f"/api/projects/{prepared['project_id']}/mural-builds", json=body)
        assert plain_start.status_code == 202, plain_start.text
        plain_job = _wait(client, plain_start.json()["job"]["id"], timeout=60)
        assert plain_job["state"] == "succeeded", plain_job.get("failure")
        plain = client.get(
            f"/api/projects/{prepared['project_id']}/mural-builds/{plain_job['id']}"
        ).json()
        plain_package = client.get(plain["package"]["download_url"]).content
        plain_labels = client.get(plain["label_partition"]["download_url"]).content
        plain_topology = client.get(plain["topology_partition"]["download_url"]).content
        assert plain["assembly_aids"] is None
        assert plain["assembly_sheet"] is None

        guided_body = {
            **body,
            "assembly_aids": {
                "enabled": True,
                "rear_identifiers": True,
                "edge_identifiers": True,
                "orientation_marks": True,
                "crop_marks": True,
                "alignment_jig_metadata": True,
            },
        }
        guided_start = client.post(
            f"/api/projects/{prepared['project_id']}/mural-builds", json=guided_body
        )
        assert guided_start.status_code == 202, guided_start.text
        guided_job = _wait(client, guided_start.json()["job"]["id"], timeout=60)
        assert guided_job["state"] == "succeeded", guided_job.get("failure")
        guided = client.get(
            f"/api/projects/{prepared['project_id']}/mural-builds/{guided_job['id']}"
        ).json()

        assert guided["download_ready"] is True
        assert guided["assembly_aids"]["media_type"] == "application/json"
        assert guided["assembly_sheet"]["media_type"] == "image/svg+xml"
        assert {item["kind"] for item in guided["artifacts"]} == {
            "bambu-mural-3mf",
            "mural-label-partition",
            "mural-topology-partition",
            "mural-seam-qa",
            "bambu-mural-validation-report",
            "bambu-mural-validation-log",
            "mural-assembly-aids",
            "mural-assembly-sheet",
        }
        aids_payload = client.get(guided["assembly_aids"]["download_url"]).json()
        aids = MuralAssemblyAidsManifest.model_validate(aids_payload)
        assert aids.title == "The Wager & arrival <guide>"
        assert aids.artwork_purity.visible_art_unchanged is True
        assert aids.artwork_purity.authoritative_master_sha256 == (
            aids.artwork_purity.recomposed_visible_art_sha256
        )
        assert len(aids.tiles) == 2
        assert aids.tiles[0].neighbours.right == aids.tiles[1].tile_id
        assert aids.tiles[1].neighbours.left == aids.tiles[0].tile_id
        assert aids.tiles[0].edge_identifiers.right == aids.tiles[1].edge_identifiers.left
        assert aids.tiles[0].edge_identifiers.right.startswith("JOIN · tile-r01-c01 ↔")
        assert all(item.back_identifier and item.orientation_mark == "TOP ↑" for item in aids.tiles)

        svg = client.get(guided["assembly_sheet"]["download_url"]).content
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg")
        assert b"The Wager &amp; arrival &lt;guide&gt;" in svg
        assert aids.manifest_sha256.encode() in svg

        # The guide is external: enabling it cannot change the printable package,
        # label partition, topology partition, or any recorded tile fingerprint.
        assert client.get(guided["package"]["download_url"]).content == plain_package
        assert client.get(guided["label_partition"]["download_url"]).content == plain_labels
        assert client.get(guided["topology_partition"]["download_url"]).content == plain_topology
        topology = MuralTopologyPartitionManifest.model_validate_json(plain_topology)
        assert {item.tile_geometry_fingerprint for item in aids.tiles} == {
            item.tile_geometry_fingerprint for item in topology.tiles
        }

        invalid = client.post(
            f"/api/projects/{prepared['project_id']}/mural-builds",
            json={
                **body,
                "assembly_aids": {
                    "enabled": True,
                    "rear_identifiers": False,
                    "edge_identifiers": False,
                    "orientation_marks": False,
                    "crop_marks": False,
                    "alignment_jig_metadata": False,
                },
            },
        )
        assert invalid.status_code == 422


def test_mural_build_rejects_stale_binding_and_requires_saved_tower_reserve(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace", bambu_resources_root=None))
    with TestClient(app) as client:
        prepared = _prepare(client)
        body = _build_body(prepared)
        stale = {**body, "expected_plan_generation": 99}
        response = client.post(f"/api/projects/{prepared['project_id']}/mural-builds", json=stale)
        assert response.status_code == 409
        assert response.json()["error"]["details"]["current_plan_generation"] == 1

        no_reserve = client.put(
            f"/api/projects/{prepared['project_id']}/mural-plan",
            json={
                "expected_generation": 1,
                "processed_artifact_id": prepared["processed_artifact_id"],
                "layout": prepared["plan"]["request"]["layout"],
                "reserved_rectangles": [],
            },
        ).json()
        no_reserve_body = {
            **body,
            "expected_plan_generation": no_reserve["generation"],
            "expected_request_fingerprint": no_reserve["plan"]["request_fingerprint"],
        }
        rejected = client.post(
            f"/api/projects/{prepared['project_id']}/mural-builds", json=no_reserve_body
        )
        assert rejected.status_code == 409
        assert "prime-tower reserve" in rejected.json()["error"]["details"]["reason"]


def test_mural_build_rejects_corrupt_geometry_before_creating_a_job(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    app = create_app(Settings(workspace=workspace, bambu_resources_root=None))
    with TestClient(app) as client:
        prepared = _prepare(client)
        connection = open_database(workspace / "image23mf.sqlite3")
        try:
            record = ArtifactRepository(connection).get(prepared["geometry"]["id"])
        finally:
            connection.close()
        app.state.mural_build_service.blob_store.path_for(record.relative_path).write_bytes(
            b"corrupt"
        )
        response = client.post(
            f"/api/projects/{prepared['project_id']}/mural-builds",
            json=_build_body(prepared),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "blob_unavailable"
        connection = open_database(workspace / "image23mf.sqlite3")
        try:
            count = connection.execute(
                "SELECT count(*) FROM jobs WHERE supersession_key = ?",
                (f"mural-build:{prepared['project_id']}",),
            ).fetchone()[0]
        finally:
            connection.close()
        assert count == 0


def test_mural_build_ignores_unused_palette_slots_for_slice_expectation(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace", bambu_resources_root=None))
    slicer = _ValidatedSlicer()
    app.state.mural_build_service.validator = slicer
    app.state.mural_build_service.profile_provider = lambda _project: _profiles(tmp_path)
    with TestClient(app) as client:
        prepared = _prepare(client, single_color=True)
        started = client.post(
            f"/api/projects/{prepared['project_id']}/mural-builds",
            json=_build_body(prepared),
        ).json()
        assert _wait(client, started["job"]["id"], timeout=60)["state"] == "succeeded"
        assert slicer.expected_colors == ("#CBC6B8",)


def test_active_mural_build_is_single_flight_and_cancelable(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace", bambu_resources_root=None))
    slicer = _BlockingSlicer()
    app.state.mural_build_service.validator = slicer
    app.state.mural_build_service.profile_provider = lambda _project: _profiles(tmp_path)
    with TestClient(app) as client:
        prepared = _prepare(client)
        endpoint = f"/api/projects/{prepared['project_id']}/mural-builds"
        body = _build_body(prepared)
        first = client.post(endpoint, json=body).json()
        assert slicer.entered.wait(timeout=30)
        active = client.post(endpoint, json=body).json()
        assert active["job"]["id"] == first["job"]["id"]
        assert active["cache_hit"] is False
        canceled = client.post(f"/api/jobs/{first['job']['id']}/cancel")
        assert canceled.status_code == 200
        assert _wait(client, first["job"]["id"])["state"] == "canceled"
        result = client.get(
            f"/api/projects/{prepared['project_id']}/mural-builds/{first['job']['id']}"
        ).json()
        assert result["download_ready"] is False
        assert result["artifacts"] == []


def test_unavailable_bambu_is_retained_honestly_but_not_download_ready(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace", bambu_resources_root=None))
    app.state.mural_build_service.validator = _UnavailableSlicer()
    app.state.mural_build_service.profile_provider = lambda _project: _profiles(tmp_path)
    with TestClient(app) as client:
        prepared = _prepare(client)
        started = client.post(
            f"/api/projects/{prepared['project_id']}/mural-builds",
            json=_build_body(prepared),
        ).json()
        job = _wait(client, started["job"]["id"], timeout=60)
        assert job["state"] == "succeeded", job.get("failure")
        result = client.get(
            f"/api/projects/{prepared['project_id']}/mural-builds/{job['id']}"
        ).json()
        assert result["validation"] == {
            "status": "unavailable",
            "attempted": True,
            "reason": "Bambu Studio is not installed",
            "executable": None,
            "version": None,
        }
        assert result["package"] is not None
        assert result["seam_qa"] is not None
        assert result["download_ready"] is False

        validated = _ValidatedSlicer()
        app.state.mural_build_service.validator = validated
        retried = client.post(
            f"/api/projects/{prepared['project_id']}/mural-builds",
            json=_build_body(prepared),
        )
        assert retried.status_code == 202
        assert retried.json()["cache_hit"] is False
        assert retried.json()["job"]["id"] != job["id"]
        retry_job = _wait(client, retried.json()["job"]["id"], timeout=60)
        assert retry_job["state"] == "succeeded", retry_job.get("failure")
        retry_result = client.get(
            f"/api/projects/{prepared['project_id']}/mural-builds/{retry_job['id']}"
        ).json()
        assert retry_result["validation"]["status"] == "validated"
        assert retry_result["download_ready"] is True
