import io
import json
import struct
import threading
from copy import deepcopy

from fastapi.testclient import TestClient
from PIL import Image

import image23mf.processing as processing_module
from image23mf.api.app import create_app
from image23mf.calibration import PrintabilityProfileCatalog, PrintabilityProfileService
from image23mf.engine import RegionGraph
from image23mf.processing import make_preview_work
from image23mf.settings import Settings
from image23mf.workers import JobContext


def encoded_image(*, size: tuple[int, int] = (80, 50), image_format: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, (20, 80, 140, 255)).save(output, format=image_format)
    return output.getvalue()


def encoded_dot_image() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGBA", (200, 200), (242, 226, 194, 255))
    try:
        for y in range(98, 102):
            for x in range(98, 102):
                image.putpixel((x, y), (27, 29, 31, 255))
        image.save(output, format="PNG")
    finally:
        image.close()
    return output.getvalue()


def import_project(client: TestClient, *, filename: str = "art.png") -> dict:
    response = client.post(
        "/api/projects/import?project_name=Processing%20proof",
        content=encoded_image(),
        headers={"X-Filename": filename, "Content-Type": "application/octet-stream"},
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


def exact_keep_operation(graph: dict, *, config_sha256: str, command_id: str) -> dict:
    command = {
        "schema_version": 1,
        "command_type": "keep_regions",
        "command_id": command_id,
        "source": "manual",
        "created_at": "2026-07-16T03:30:00Z",
        "provenance": {"ui": "processing-api-test"},
        "selector": {
            "graph_fingerprint": RegionGraph.model_validate(graph).fingerprint(),
            "config_fingerprint": config_sha256,
            "region_ids": [graph["regions"][0]["id"]],
        },
        "reason": "Preserve this intentional region through subsequent edits.",
    }
    return {
        "operation_type": "editor_command_v1",
        "selection": command["selector"],
        "parameters": {"command": command},
        "source": "manual",
        "provenance": {"editor_schema_version": 1},
    }


def canvas_selection_operation(result: dict, *, config_sha256: str) -> dict:
    graph_fingerprint = RegionGraph.model_validate(result["region_graph"]).fingerprint()
    transform = result["statistics"]["transform"]
    command = {
        "schema_version": 1,
        "command_type": "canvas_selection",
        "command_id": "cmd_canvas_selection_api",
        "source": "manual",
        "created_at": "2026-07-17T07:00:00Z",
        "provenance": {"ui": "processing-api-test"},
        "selector": {
            "graph_fingerprint": graph_fingerprint,
            "config_fingerprint": config_sha256,
            "selection": {
                "schema_version": 1,
                "coordinate_space": "normalized_source",
                "source_width_px": transform["normalized_size"]["width"],
                "source_height_px": transform["normalized_size"]["height"],
                "primitives": [
                    {
                        "kind": "rectangle",
                        "primitive_id": "selection_api_rectangle",
                        "combine": "add",
                        "x": 1,
                        "y": 1,
                        "width": 10,
                        "height": 10,
                    }
                ],
                "expand_mm": 0,
                "feather_mm": 0,
            },
        },
    }
    return {
        "operation_type": "canvas_selection_v1",
        "selection": command["selector"],
        "parameters": {"command": command},
        "source": "manual",
        "provenance": {"editor_schema_version": 1},
    }


def local_fill_operation(result: dict, *, config_sha256: str) -> dict:
    graph_fingerprint = RegionGraph.model_validate(result["region_graph"]).fingerprint()
    source_label = result["region_graph"]["regions"][0]["label"]
    target_label = next(
        item["label"] for item in result["region_graph"]["palette"] if item["label"] != source_label
    )
    transform = result["statistics"]["transform"]
    command = {
        "schema_version": 1,
        "command_type": "local_raster_edit",
        "command_id": "cmd_local_fill_api",
        "source": "manual",
        "created_at": "2026-07-17T08:00:00Z",
        "provenance": {"ui": "processing-api-test", "selection_snapshot": True},
        "selector": {
            "graph_fingerprint": graph_fingerprint,
            "config_fingerprint": config_sha256,
            "selection": {
                "schema_version": 1,
                "coordinate_space": "normalized_source",
                "source_width_px": transform["normalized_size"]["width"],
                "source_height_px": transform["normalized_size"]["height"],
                "primitives": [
                    {
                        "kind": "rectangle",
                        "primitive_id": "selection_api_local_fill",
                        "combine": "add",
                        "x": 30,
                        "y": 15,
                        "width": 10,
                        "height": 10,
                    }
                ],
                "expand_mm": 0,
                "feather_mm": 0,
            },
        },
        "edit": {
            "kind": "fill",
            "target_label": target_label,
            "activate_transparent": True,
        },
    }
    return {
        "operation_type": "editor_command_v1",
        "selection": command["selector"],
        "parameters": {"command": command},
        "source": "manual",
        "provenance": {"editor_schema_version": 1},
    }


def test_import_persists_normalized_image_project_and_reopenable_draft(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        imported = import_project(client, filename="camera.WRONG")
        source = client.get(f"/api/assets/{imported['source_asset']['id']}")
        reopened = client.get(f"/api/projects/{imported['project']['id']}")

    assert imported["project"]["name"] == "Processing proof"
    assert imported["source_asset"]["media_type"] == "image/png"
    assert imported["source_asset"]["original_filename"] == "camera.WRONG"
    assert imported["source_asset"]["metadata"]["detected_format"] == "png"
    assert imported["draft"]["generation"] == 1
    assert imported["latest_preview_job"] is None
    assert imported["draft"]["config"]["source_asset_id"] == imported["source_asset"]["id"]
    assert imported["draft"]["config"]["cleanup"] == {
        "min_island_mm2": 0.282743,
        "minimum_island_diameter_mm": 0.6,
        "max_hole_mm2": 0.502655,
        "maximum_tiny_hole_diameter_mm": 0.8,
        "minimum_ring_width_mm": 0.6,
        "minimum_line_width_mm": 0.48,
        "minimum_neck_width_mm": 0.6,
        "minimum_gap_width_mm": 0.5,
        "long_line_minimum_length_mm": 1.6,
        "smoothing_radius_mm": 0,
        "merge_policy": "review",
        "preserve_long_lines": True,
        "printability_profile_id": "bambu-p2s-0.4-hardened-steel-pla-v1",
        "printability_profile_catalog_fingerprint": (
            "215157c562737bc0d97391257aca309a49ca65415bfc0935fc3c333b74a5c08c"
        ),
        "override_fields": [],
    }
    assert source.status_code == 200
    assert source.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(source.content)) as restored_source:
        assert restored_source.mode == "RGBA"
        assert restored_source.size == (80, 50)
    assert reopened.status_code == 200
    assert reopened.json() == imported


def test_project_browser_persists_thumbnail_and_archive_restore_across_restart(tmp_path) -> None:
    workspace_path = tmp_path / "workspace"
    app = create_app(Settings(workspace=workspace_path))
    with TestClient(app) as client:
        workspace = import_project(client, filename="wager.png")
        project_id = workspace["project"]["id"]
        started = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(started["job"]["id"])
        preview_result = client.get(f"/api/jobs/{started['job']['id']}/result")
        assert preview_result.status_code == 200, preview_result.text

    restarted = create_app(Settings(workspace=workspace_path))
    with TestClient(restarted) as client:
        listed = client.get("/api/projects")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["total"] == 1
        summary = body["items"][0]
        assert summary["project"]["id"] == project_id
        assert summary["thumbnail_kind"] == "preview"
        assert summary["thumbnail_url"].startswith(f"/api/projects/{project_id}/artifacts/")
        assert summary["source_width_px"] == 80
        assert summary["source_height_px"] == 50
        assert summary["canvas_width_mm"] == 200
        assert summary["canvas_height_mm"] == 200
        assert summary["color_count"] == 2
        assert summary["status"] == "preview_ready"
        assert summary["validation"] == "not_requested"
        thumbnail = client.get(summary["thumbnail_url"])
        assert thumbnail.status_code == 200
        assert thumbnail.headers["content-type"] == "image/png"

        archived = client.post(f"/api/projects/{project_id}/archive")
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"
        assert client.get("/api/projects").json() == {"items": [], "total": 0}
        archived_list = client.get("/api/projects?include_archived=true").json()
        assert archived_list["total"] == 1
        assert archived_list["items"][0]["project"]["archived_at"] is not None

        restored = client.post(f"/api/projects/{project_id}/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["project"]["archived_at"] is None
        assert restored.json()["thumbnail_url"] == summary["thumbnail_url"]
        assert client.get("/api/projects").json()["total"] == 1


def test_cleanup_defaults_re_resolve_by_nozzle_while_explicit_overrides_survive(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        project_id = workspace["project"]["id"]
        config = workspace["draft"]["config"]
        config["printer"].update(
            {
                "nozzle_id": "nozzle-0.2-hardened-steel",
                "nozzle_mm": 0.2,
                "layer_height_mm": 0.12,
            }
        )
        fine = client.put(
            f"/api/projects/{project_id}/draft",
            json={
                "config": config,
                "operations": [],
                "expected_draft_generation": workspace["draft"]["generation"],
            },
        )
        assert fine.status_code == 200, fine.text
        fine_config = fine.json()["config"]
        assert fine_config["cleanup"]["printability_profile_id"].startswith("bambu-p2s-0.2")
        assert fine_config["cleanup"]["min_island_mm2"] == 0.070686
        assert fine_config["cleanup"]["minimum_line_width_mm"] == 0.24
        assert fine_config["cleanup"]["minimum_neck_width_mm"] == 0.3
        assert fine_config["cleanup"]["minimum_gap_width_mm"] == 0.25
        assert fine_config["cleanup"]["max_hole_mm2"] == 0.125664
        assert fine_config["cleanup"]["override_fields"] == []

        fine_config["cleanup"]["min_island_mm2"] = 0.123
        fine_config["cleanup"]["minimum_line_width_mm"] = 0.31
        fine_config["cleanup"]["override_fields"] = [
            "min_island_mm2",
            "minimum_line_width_mm",
        ]
        fine_config["printer"].update(
            {
                "nozzle_id": "nozzle-0.4-hardened-steel",
                "nozzle_mm": 0.4,
                "layer_height_mm": 0.2,
            }
        )
        standard = client.put(
            f"/api/projects/{project_id}/draft",
            json={
                "config": fine_config,
                "operations": [],
                "expected_draft_generation": fine.json()["generation"],
            },
        )

    assert standard.status_code == 200, standard.text
    standard_cleanup = standard.json()["config"]["cleanup"]
    assert standard_cleanup["printability_profile_id"].startswith("bambu-p2s-0.4")
    assert standard_cleanup["min_island_mm2"] == 0.123
    assert standard_cleanup["minimum_line_width_mm"] == 0.31
    assert standard_cleanup["max_hole_mm2"] == 0.502655
    assert standard_cleanup["override_fields"] == [
        "min_island_mm2",
        "minimum_line_width_mm",
    ]


def test_preview_worker_uses_the_injected_printability_catalog(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    payload = app.state.printability_profile_service.catalog.model_dump(mode="json")
    standard = next(
        item for item in payload["profiles"] if item["nozzle_id"] == "nozzle-0.4-hardened-steel"
    )
    standard["recommendations"]["minimum_line_width_mm"]["value"] = 0.49
    standard["recommendations"]["minimum_neck_width_mm"]["value"] = 0.61
    standard["recommendations"]["minimum_gap_width_mm"]["value"] = 0.51
    injected = PrintabilityProfileService(PrintabilityProfileCatalog.model_validate(payload))
    app.state.printability_profile_service = injected
    app.state.processing_service.printability_profiles = injected

    with TestClient(app) as client:
        workspace = import_project(client)
        started = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(started["job"]["id"])
        result = client.get(f"/api/jobs/{started['job']['id']}/result")

    assert result.status_code == 200, result.text
    options = result.json()["clearance_analysis"]["options"]
    assert options["minimum_line_width_mm"] == 0.49
    assert options["minimum_neck_width_mm"] == 0.61
    assert options["minimum_gap_width_mm"] == 0.51
    assert (
        started["draft"]["config"]["cleanup"]["printability_profile_catalog_fingerprint"]
        == injected.catalog.fingerprint()
    )


def test_dominant_cleanup_changes_processed_preview_and_publishes_exact_evidence(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        imported_response = client.post(
            "/api/projects/import?project_name=Dot%20cleanup%20proof",
            content=encoded_dot_image(),
            headers={"X-Filename": "dot.png", "Content-Type": "application/octet-stream"},
        )
        assert imported_response.status_code == 201, imported_response.text
        workspace = imported_response.json()

        baseline = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(baseline["job"]["id"])
        baseline_result = client.get(f"/api/jobs/{baseline['job']['id']}/result").json()

        cleanup_config = baseline["draft"]["config"]
        cleanup_config["cleanup"].update(
            {
                "min_island_mm2": 25,
                "merge_policy": "dominant_neighbor",
                "override_fields": ["min_island_mm2"],
            }
        )
        cleaned_response = client.post(
            f"/api/projects/{workspace['project']['id']}/previews",
            json={
                "config": cleanup_config,
                "expected_draft_generation": baseline["draft"]["generation"],
            },
        )
        assert cleaned_response.status_code == 202, cleaned_response.text
        cleaned = cleaned_response.json()
        app.state.worker_manager.wait_for_terminal(cleaned["job"]["id"])
        cleaned_result = client.get(f"/api/jobs/{cleaned['job']['id']}/result").json()

        baseline_artifacts = {item["kind"]: item for item in baseline_result["artifacts"]}
        cleaned_artifacts = {item["kind"]: item for item in cleaned_result["artifacts"]}
        cleanup_record = client.get(cleaned_artifacts["automatic-cleanup"]["download_url"]).json()
        cleanup_mask = client.get(
            cleaned_artifacts["automatic-cleanup-changed-mask"]["download_url"]
        ).content

    assert (
        baseline_artifacts["palette-preview-image"]["sha256"]
        != cleaned_artifacts["palette-preview-image"]["sha256"]
    )
    assert cleanup_record["island_policy"] == "dominant_neighbor"
    assert cleanup_record["changed_pixel_count"] > 0
    assert sum(cleanup_mask) == cleanup_record["changed_pixel_count"]
    assert cleaned_artifacts["automatic-cleanup"]["metadata"]["changed_pixel_count"] == sum(
        cleanup_mask
    )
    assert (
        cleaned_result["risk_report"]["graph_fingerprint"]
        == (cleanup_record["after_graph_fingerprint"])
    )


def test_import_failures_use_stable_actionable_media_and_resource_errors(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace", max_image_input_bytes=20))
    with TestClient(app) as client:
        unsupported = client.post(
            "/api/projects/import",
            content=b"GIF89a-not-supported",
            headers={"X-Filename": "animation.gif"},
        )
        oversized = client.post(
            "/api/projects/import",
            content=b"small",
            headers={"X-Filename": "huge.png", "Content-Length": "21"},
        )

    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "unsupported_media"
    assert unsupported.json()["error"]["details"]["ingestion_code"] == "unsupported_format"
    assert unsupported.json()["error"]["details"]["suggestion"]
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "resource_limit"


def test_preview_poll_stream_result_and_artifact_download_are_coherent(
    tmp_path, monkeypatch
) -> None:
    last_stage = None
    operation_stages = {}
    original_report = JobContext.report
    original_cleanup = processing_module.apply_automatic_cleanup
    original_holes = processing_module.classify_holes

    def report(context, *, stage, progress):
        nonlocal last_stage
        result = original_report(context, stage=stage, progress=progress)
        last_stage = stage.value
        return result

    def cleanup(*args, **kwargs):
        operation_stages["cleanup"] = last_stage
        return original_cleanup(*args, **kwargs)

    def holes(*args, **kwargs):
        operation_stages["holes"] = last_stage
        return original_holes(*args, **kwargs)

    monkeypatch.setattr(JobContext, "report", report)
    monkeypatch.setattr(processing_module, "apply_automatic_cleanup", cleanup)
    monkeypatch.setattr(processing_module, "classify_holes", holes)
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        started = start_preview(client, workspace)
        job_id = started["job"]["id"]
        assert started["draft"]["generation"] == 2
        assert len(started["job"]["request_key"]) == 64

        with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]
        polled = client.get(f"/api/jobs/{job_id}")
        result = client.get(f"/api/jobs/{job_id}/result")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert events
        assert [event["progress"] for event in events] == sorted(
            event["progress"] for event in events
        )
        assert events[-1]["state"] == "succeeded"
        assert operation_stages == {"cleanup": "cleaning", "holes": "analyzing"}
        assert polled.json()["state"] == "succeeded"
        assert result.status_code == 200
        body = result.json()
        assert [artifact["kind"] for artifact in body["artifacts"]] == [
            "automatic-cleanup",
            "automatic-cleanup-active",
            "automatic-cleanup-changed-mask",
            "automatic-cleanup-labels",
            "automatic-cleanup-mask-preview-image",
            "clearance-analysis",
            "editor-changed-mask",
            "editor-protected-mask",
            "editor-replay",
            "hole-analysis",
            "island-analysis",
            "palette-metrics",
            "palette-preview-image",
            "preview-image",
            "preview-statistics",
            "printability-settings",
            "processed-active",
            "processed-labels",
            "quantized-active",
            "quantized-labels",
            "quantized-preview-image",
            "region-assignment",
            "region-graph",
            "reprocessing-affected-mask",
            "reprocessing-affected-mask-preview-image",
            "reprocessing-plan",
            "risk-mask",
            "risk-report",
        ]
        assert body["job"]["artifact_ids"] == [artifact["id"] for artifact in body["artifacts"]]
        assert body["statistics"]["schema_version"] == 1
        assert body["statistics"]["config_sha256"] == started["draft"]["config_sha256"]
        assert {item["derivation_key"] for item in body["artifacts"]} == {
            started["job"]["request_key"]
        }
        assert body["statistics"]["preview"] == {"width": 1024, "height": 1024}
        assert body["palette_metrics"]["schema_version"] == 1
        assert body["palette_metrics"]["config_sha256"] == started["draft"]["config_sha256"]
        assert body["palette_metrics"]["visible_pixel_count"] == 1024 * 1024
        assert sum(item["pixel_count"] for item in body["palette_metrics"]["colors"]) == 1024 * 1024
        assert len(body["palette_metrics"]["quantization_fingerprint"]) == 64
        assert body["region_graph"]["schema_version"] == 1
        assert body["region_graph"]["active_pixel_count"] == 1024 * 1024
        assert sum(region["pixel_count"] for region in body["region_graph"]["regions"]) == (
            1024 * 1024
        )
        assert body["risk_report"]["schema_version"] == 1
        assert (
            body["risk_report"]["graph_fingerprint"]
            == RegionGraph.model_validate(body["region_graph"]).fingerprint()
        )
        assert body["risk_report"]["evaluated_codes"] == [
            "small_island",
            "tiny_hole",
            "hollow_ring",
            "narrow_neck",
            "thin_line",
            "narrow_gap",
            "excess_fragmentation",
            "color_absent",
        ]
        assert body["risk_report"]["pending_codes"] == []
        by_kind = {artifact["kind"]: artifact for artifact in body["artifacts"]}
        assert by_kind["risk-report"]["metadata"]["printability_profile_id"] == (
            "bambu-p2s-0.4-hardened-steel-pla-v1"
        )
        assert by_kind["risk-report"]["metadata"]["printability_evidence_status"] == (
            "pending_print_calibration"
        )
        assert (
            len(by_kind["risk-report"]["metadata"]["printability_profile_catalog_fingerprint"])
            == 64
        )
        clearance_download = client.get(by_kind["clearance-analysis"]["download_url"])
        hole_download = client.get(by_kind["hole-analysis"]["download_url"])
        island_download = client.get(by_kind["island-analysis"]["download_url"])
        metrics_download = client.get(by_kind["palette-metrics"]["download_url"])
        graph_download = client.get(by_kind["region-graph"]["download_url"])
        assignment_download = client.get(by_kind["region-assignment"]["download_url"])
        risk_mask_download = client.get(by_kind["risk-mask"]["download_url"])
        risk_download = client.get(by_kind["risk-report"]["download_url"])
        palette_download = client.get(by_kind["palette-preview-image"]["download_url"])
        printability_download = client.get(by_kind["printability-settings"]["download_url"])
        assert metrics_download.json() == body["palette_metrics"]
        assert graph_download.json() == body["region_graph"]
        assignment_metadata = by_kind["region-assignment"]["metadata"]
        assert assignment_metadata == {
            "schema_version": 1,
            "width": body["region_graph"]["width_px"],
            "height": body["region_graph"]["height_px"],
            "encoding": "int32-region-index-row-major-little-endian",
            "inactive_index": -1,
            "region_count": len(body["region_graph"]["regions"]),
            "graph_fingerprint": body["risk_report"]["graph_fingerprint"],
        }
        assignment = tuple(
            value[0] for value in struct.iter_unpack("<i", assignment_download.content)
        )
        assert len(assignment) == (
            body["region_graph"]["width_px"] * body["region_graph"]["height_px"]
        )
        assert min(assignment) >= -1
        assert max(assignment) < len(body["region_graph"]["regions"])
        assert [
            assignment.count(index) for index in range(len(body["region_graph"]["regions"]))
        ] == [region["pixel_count"] for region in body["region_graph"]["regions"]]
        risk_mask_metadata = by_kind["risk-mask"]["metadata"]
        assert risk_mask_metadata["encoding"] == "uint8-risk-severity-bitset-row-major"
        assert risk_mask_metadata["severity_bits"] == {"info": 1, "warning": 2, "error": 4}
        assert risk_mask_metadata["width"] == body["region_graph"]["width_px"]
        assert risk_mask_metadata["height"] == body["region_graph"]["height_px"]
        assert risk_mask_metadata["assignment_sha256"] == by_kind["region-assignment"]["sha256"]
        assert risk_mask_metadata["graph_fingerprint"] == body["risk_report"]["graph_fingerprint"]
        assert (
            risk_mask_metadata["risk_report_fingerprint"]
            == by_kind["risk-report"]["metadata"]["report_fingerprint"]
        )
        assert risk_mask_metadata["source_sha256"] == workspace["source_asset"]["sha256"]
        assert risk_mask_metadata["config_fingerprint"] == started["draft"]["config_sha256"]
        assert (
            risk_mask_metadata["operations_fingerprint"]
            == started["draft"]["editor_sequence_sha256"]
        )
        assert len(risk_mask_download.content) == (
            body["region_graph"]["width_px"] * body["region_graph"]["height_px"]
        )
        assert set(risk_mask_download.content).issubset({0, 1, 2, 3, 4, 5, 6, 7})
        assert (
            risk_mask_metadata["exact_warning_count"]
            + risk_mask_metadata["approximate_warning_count"]
            == body["risk_report"]["summary"]["total"]
        )
        severity_bit = {"info": 1, "warning": 2, "error": 4}
        region_bits = [0] * len(body["region_graph"]["regions"])
        id_to_index = {
            region["id"]: index for index, region in enumerate(body["region_graph"]["regions"])
        }
        for warning in body["risk_report"]["warnings"]:
            affected = {
                id_to_index[region_id]
                for region_id in warning["affected_region_ids"]
                if region_id in id_to_index
            }
            affected.update(
                index
                for index, region in enumerate(body["region_graph"]["regions"])
                if region["label"] in warning["affected_labels"]
            )
            for index in affected:
                region_bits[index] |= severity_bit[warning["severity"]]
        assert risk_mask_download.content == bytes(
            0 if region_index < 0 else region_bits[region_index] for region_index in assignment
        )
        assert risk_download.json() == body["risk_report"]
        assert island_download.json() == body["island_analysis"]
        assert clearance_download.json() == body["clearance_analysis"]
        assert hole_download.json() == body["hole_analysis"]
        resolved_printability = printability_download.json()
        assert resolved_printability["profile_id"] == ("bambu-p2s-0.4-hardened-steel-pla-v1")
        assert len(resolved_printability["values"]) == 10
        assert {item["source"] for item in resolved_printability["values"]} == {"profile"}
        assert by_kind["printability-settings"]["metadata"]["resolved_values"] == [
            {
                "name": item["name"],
                "value": item["value"],
                "unit": item["unit"],
                "source": item["source"],
                "profile_value": item["profile_value"],
            }
            for item in resolved_printability["values"]
        ]
        assert body["hole_analysis"]["schema_version"] == 1
        assert body["clearance_analysis"]["schema_version"] == 1
        assert body["island_analysis"]["schema_version"] == 1
        assert body["island_analysis"]["options"]["minimum_area_mm2"] == 0.282743
        assert body["island_analysis"]["options"]["minimum_equivalent_diameter_mm"] == 0.6
        assert body["clearance_analysis"]["options"]["minimum_line_width_mm"] == 0.48
        assert body["clearance_analysis"]["options"]["minimum_neck_width_mm"] == 0.6
        assert body["clearance_analysis"]["options"]["minimum_gap_width_mm"] == 0.5
        assert body["hole_analysis"]["options"]["maximum_area_mm2"] == 0.502655
        assert body["hole_analysis"]["options"]["maximum_equivalent_diameter_mm"] == 0.8
        assert body["hole_analysis"]["options"]["minimum_surviving_ring_width_mm"] == 0.6
        assert (
            body["island_analysis"]["graph_fingerprint"] == body["risk_report"]["graph_fingerprint"]
        )
        with Image.open(io.BytesIO(palette_download.content)) as palette_image:
            visible_colors = {
                pixel[:3] for pixel in palette_image.convert("RGBA").getdata() if pixel[3] > 0
            }
        configured_colors = {
            tuple(bytes.fromhex(color["hex"].removeprefix("#")))
            for color in started["draft"]["config"]["palette"]["colors"]
        }
        assert visible_colors <= configured_colors
        for artifact in body["artifacts"]:
            download = client.get(artifact["download_url"])
            assert download.status_code == 200
            assert len(download.content) == artifact["byte_size"]


def test_identical_previews_publish_independent_artifact_records_across_projects(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        first_workspace = import_project(client, filename="first.png")
        second_workspace = import_project(client, filename="second.png")
        first = start_preview(client, first_workspace)
        second = start_preview(client, second_workspace)
        first_job = app.state.worker_manager.wait_for_terminal(first["job"]["id"])
        second_job = app.state.worker_manager.wait_for_terminal(second["job"]["id"])
        first_result = client.get(f"/api/jobs/{first_job.id}/result")
        second_result = client.get(f"/api/jobs/{second_job.id}/result")

    assert first_job.state.value == "succeeded"
    assert second_job.state.value == "succeeded"
    assert first_result.status_code == 200
    assert second_result.status_code == 200
    assert first["job"]["request_key"] == second["job"]["request_key"]
    assert first_result.json()["job"]["artifact_ids"] != second_result.json()["job"]["artifact_ids"]
    assert [item["sha256"] for item in first_result.json()["artifacts"]] == [
        item["sha256"] for item in second_result.json()["artifacts"]
    ]


def test_engine_upgrade_explains_stale_preview_and_regeneration_publishes(
    tmp_path, monkeypatch
) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        original = start_preview(client, workspace)
        assert app.state.worker_manager.wait_for_terminal(original["job"]["id"]).state.value == (
            "succeeded"
        )

        monkeypatch.setattr(processing_module, "__version__", "99.0.0-upgrade-proof")
        stale = client.post(
            f"/api/projects/{workspace['project']['id']}/revisions",
            json={
                "label": "Must not publish stale evidence",
                "expected_draft_generation": original["draft"]["generation"],
                "preview_job_id": original["job"]["id"],
            },
        )
        assert stale.status_code == 409, stale.text
        assert "processing engine was upgraded" in stale.json()["error"]["details"]["reason"]

        reopened = client.get(f"/api/projects/{workspace['project']['id']}").json()
        regenerated = start_preview(client, reopened)
        assert regenerated["job"]["request_key"] != original["job"]["request_key"]
        assert (
            app.state.worker_manager.wait_for_terminal(regenerated["job"]["id"]).state.value
            == "succeeded"
        )
        published = client.post(
            f"/api/projects/{workspace['project']['id']}/revisions",
            json={
                "label": "Regenerated after upgrade",
                "expected_draft_generation": regenerated["draft"]["generation"],
                "preview_job_id": regenerated["job"]["id"],
            },
        )

    assert published.status_code == 201, published.text
    assert published.json()["revision"]["preview_evidence"]["status"] == "fresh"


def test_typed_editor_history_changes_derivation_and_replays_into_canonical_artifacts(
    tmp_path,
) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        baseline = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(baseline["job"]["id"])
        baseline_result = client.get(f"/api/jobs/{baseline['job']['id']}/result").json()
        graph = baseline_result["region_graph"]
        command = {
            "schema_version": 1,
            "command_type": "keep_regions",
            "command_id": "cmd_integration_keep",
            "source": "manual",
            "created_at": "2026-07-16T03:30:00Z",
            "provenance": {"ui": "processing-api-test", "finding_id": "region-proof"},
            "selector": {
                "graph_fingerprint": RegionGraph.model_validate(graph).fingerprint(),
                "config_fingerprint": baseline["draft"]["config_sha256"],
                "region_ids": [graph["regions"][0]["id"]],
            },
            "reason": "Preserve this intentional region through subsequent edits.",
        }
        operation = {
            "operation_type": "editor_command_v1",
            "selection": command["selector"],
            "parameters": {"command": command},
            "source": "manual",
            "provenance": {"editor_schema_version": 1},
        }
        saved = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": baseline["draft"]["config"],
                "operations": [operation],
                "expected_draft_generation": baseline["draft"]["generation"],
            },
        )
        assert saved.status_code == 200, saved.text
        replayed = start_preview(client, {**workspace, "draft": saved.json()})
        app.state.worker_manager.wait_for_terminal(replayed["job"]["id"])
        replayed_result = client.get(f"/api/jobs/{replayed['job']['id']}/result").json()
        artifacts = {item["kind"]: item for item in replayed_result["artifacts"]}
        replay_record = client.get(artifacts["editor-replay"]["download_url"]).json()
        labels = client.get(artifacts["processed-labels"]["download_url"]).content
        protected = client.get(artifacts["editor-protected-mask"]["download_url"]).content

    assert replayed["job"]["state"] in {"queued", "running"}
    assert replayed["job"]["request_key"] != baseline["job"]["request_key"]
    assert replay_record["command_count"] == 1
    assert replay_record["steps"][0]["command_id"] == "cmd_integration_keep"
    assert replay_record["changed_pixel_count"] == 0
    assert replay_record["protected_pixel_count"] == sum(bool(value) for value in protected)
    assert replay_record["protected_pixel_count"] > 0
    assert len(labels) == graph["width_px"] * graph["height_px"]
    assert (
        artifacts["palette-preview-image"]["metadata"]["editor_replay_fingerprint"]
        == (artifacts["editor-replay"]["metadata"]["replay_fingerprint"])
    )


def test_local_selection_edit_replays_through_preview_and_reprocesses_labels(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        baseline = start_preview(client, workspace)
        assert app.state.worker_manager.wait_for_terminal(baseline["job"]["id"]).state.value == (
            "succeeded"
        )
        baseline_result = client.get(f"/api/jobs/{baseline['job']['id']}/result").json()
        baseline_artifacts = {item["kind"]: item for item in baseline_result["artifacts"]}
        operation = local_fill_operation(
            baseline_result,
            config_sha256=baseline["draft"]["config_sha256"],
        )
        saved = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": baseline["draft"]["config"],
                "operations": [operation],
                "expected_draft_generation": baseline["draft"]["generation"],
            },
        )
        assert saved.status_code == 200, saved.text
        replayed = start_preview(client, {**workspace, "draft": saved.json()})
        assert app.state.worker_manager.wait_for_terminal(replayed["job"]["id"]).state.value == (
            "succeeded"
        )
        result = client.get(f"/api/jobs/{replayed['job']['id']}/result").json()
        artifacts = {item["kind"]: item for item in result["artifacts"]}
        replay_record = client.get(artifacts["editor-replay"]["download_url"]).json()
        reprocessing_plan = client.get(artifacts["reprocessing-plan"]["download_url"]).json()
        processed_labels = client.get(artifacts["processed-labels"]["download_url"]).content

    assert replayed["job"]["request_key"] != baseline["job"]["request_key"]
    assert replay_record["command_count"] == 1
    assert replay_record["steps"][0]["command_type"] == "local_raster_edit"
    assert replay_record["steps"][0]["changed_pixel_count"] > 0
    assert replay_record["steps"][0]["engine_record_fingerprint"]
    assert operation["parameters"]["command"]["edit"]["target_label"] in processed_labels
    assert reprocessing_plan["mode"] == "scoped", reprocessing_plan
    assert reprocessing_plan["reason"] == "local_interior_edit"
    assert reprocessing_plan["reused_stages"] == ["quantization", "automatic_cleanup"]
    assert reprocessing_plan["affected_pixel_count"] > 0
    assert (
        artifacts["quantized-labels"]["sha256"] == baseline_artifacts["quantized-labels"]["sha256"]
    )
    assert (
        artifacts["automatic-cleanup-labels"]["sha256"]
        == baseline_artifacts["automatic-cleanup-labels"]["sha256"]
    )


def test_local_edit_touching_canvas_boundary_forces_full_reprocessing(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        baseline = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(baseline["job"]["id"])
        baseline_result = client.get(f"/api/jobs/{baseline['job']['id']}/result").json()
        operation = local_fill_operation(
            baseline_result,
            config_sha256=baseline["draft"]["config_sha256"],
        )
        rectangle = operation["parameters"]["command"]["selector"]["selection"]["primitives"][0]
        rectangle.update({"x": 15, "y": 10, "width": 10, "height": 10})
        saved = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": baseline["draft"]["config"],
                "operations": [operation],
                "expected_draft_generation": baseline["draft"]["generation"],
            },
        )
        assert saved.status_code == 200, saved.text
        replayed = start_preview(client, {**workspace, "draft": saved.json()})
        app.state.worker_manager.wait_for_terminal(replayed["job"]["id"])
        result = client.get(f"/api/jobs/{replayed['job']['id']}/result").json()
        artifacts = {item["kind"]: item for item in result["artifacts"]}
        plan = client.get(artifacts["reprocessing-plan"]["download_url"]).json()

    assert plan["mode"] == "full"
    assert plan["reason"] == "boundary_edit"
    assert plan["reused_stages"] == []


def test_canvas_selection_is_revisioned_without_invalidating_render_evidence(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        baseline = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(baseline["job"]["id"])
        result = client.get(f"/api/jobs/{baseline['job']['id']}/result").json()
        operation = canvas_selection_operation(
            result,
            config_sha256=baseline["draft"]["config_sha256"],
        )
        saved_response = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": baseline["draft"]["config"],
                "operations": [operation],
                "expected_draft_generation": baseline["draft"]["generation"],
            },
        )
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()
        published = client.post(
            f"/api/projects/{workspace['project']['id']}/revisions",
            json={
                "label": "Selection snapshot",
                "expected_draft_generation": saved["generation"],
                "preview_job_id": baseline["job"]["id"],
            },
        )

    assert saved["editor_sequence_sha256"] == baseline["draft"]["editor_sequence_sha256"]
    assert saved["operations"] == [operation]
    assert published.status_code == 201, published.text
    assert published.json()["revision"]["operations"] == [operation]
    assert published.json()["revision"]["preview_evidence"]["status"] == "fresh"


def test_config_change_after_exact_manual_command_is_rejected_without_poisoning_draft(
    tmp_path,
) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        baseline = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(baseline["job"]["id"])
        result = client.get(f"/api/jobs/{baseline['job']['id']}/result").json()
        operation = exact_keep_operation(
            result["region_graph"],
            config_sha256=baseline["draft"]["config_sha256"],
            command_id="cmd_config_guard",
        )
        exact = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": baseline["draft"]["config"],
                "operations": [operation],
                "expected_draft_generation": baseline["draft"]["generation"],
            },
        )
        assert exact.status_code == 200, exact.text
        saved = exact.json()

        variants = []
        canvas = deepcopy(saved["config"])
        canvas["canvas"]["width_mm"] -= 1
        variants.append(canvas)
        crop = deepcopy(saved["config"])
        crop["crop"].update({"x": 0.01, "width": 0.99})
        variants.append(crop)
        palette = deepcopy(saved["config"])
        palette["palette"]["colors"][0]["name"] = "Renamed exact color"
        variants.append(palette)
        cleanup = deepcopy(saved["config"])
        cleanup["cleanup"]["smoothing_radius_mm"] = 0.1
        cleanup["cleanup"]["override_fields"] = ["smoothing_radius_mm"]
        variants.append(cleanup)
        printer = deepcopy(saved["config"])
        printer["printer"]["layer_height_mm"] = 0.1
        variants.append(printer)
        geometry = deepcopy(saved["config"])
        geometry["geometry"]["corner_radius_mm"] = 1
        variants.append(geometry)

        for candidate in variants:
            rejected = client.put(
                f"/api/projects/{workspace['project']['id']}/draft",
                json={
                    "config": candidate,
                    "operations": [operation],
                    "expected_draft_generation": saved["generation"],
                },
            )
            assert rejected.status_code == 422, rejected.text
            error = rejected.json()["error"]
            assert error["code"] == "incompatible_editor_history", rejected.text
            assert error["message"] == (
                "The saved editor history belongs to a different image configuration."
            )
            assert error["details"] == {
                "project_id": workspace["project"]["id"],
                "reason": "selector_config_mismatch",
                "operation_count": 1,
                "conflict_count": 1,
                "action": "clear_or_restore_config",
            }
            assert baseline["draft"]["config_sha256"] not in rejected.text
            reopened = client.get(f"/api/projects/{workspace['project']['id']}")
            assert reopened.status_code == 200, reopened.text
            assert reopened.json()["draft"] == saved

        stale_candidate = deepcopy(saved["config"])
        stale_candidate["canvas"]["height_mm"] -= 1
        stale = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": stale_candidate,
                "operations": [operation],
                "expected_draft_generation": saved["generation"] - 1,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_draft"

        preview_candidate = deepcopy(saved["config"])
        preview_candidate["canvas"]["height_mm"] -= 1
        preview = client.post(
            f"/api/projects/{workspace['project']['id']}/previews",
            json={
                "config": preview_candidate,
                "expected_draft_generation": saved["generation"],
            },
        )
        assert preview.status_code == 422, preview.text
        assert preview.json()["error"]["code"] == "incompatible_editor_history"
        final = client.get(f"/api/projects/{workspace['project']['id']}")
        assert final.status_code == 200, final.text
        assert final.json()["draft"] == saved

        non_override = deepcopy(saved["config"])
        non_override["cleanup"]["min_island_mm2"] = 10
        normalized = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": non_override,
                "operations": [operation],
                "expected_draft_generation": saved["generation"],
            },
        )
        assert normalized.status_code == 200, normalized.text
        assert normalized.json()["config"] == saved["config"]
        assert normalized.json()["generation"] == saved["generation"] + 1


def test_ambiguous_legacy_raster_history_is_rejected_before_preview(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        before = workspace["draft"]
        response = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": workspace["draft"]["config"],
                "operations": [
                    {
                        "operation_type": "replace_label",
                        "selection": {"x": 0, "y": 0},
                        "parameters": {"target": 1},
                        "source": "manual",
                        "provenance": {},
                    }
                ],
                "expected_draft_generation": workspace["draft"]["generation"],
            },
        )
        reopened = client.get(f"/api/projects/{workspace['project']['id']}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "cannot be replayed safely" in response.json()["error"]["message"]
    assert reopened.status_code == 200
    assert reopened.json()["draft"] == before


def test_preview_rejects_stale_draft_and_mismatched_pinned_nozzle(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        first = start_preview(client, workspace)
        app.state.worker_manager.wait_for_terminal(first["job"]["id"])

        stale = client.post(
            f"/api/projects/{workspace['project']['id']}/previews",
            json={
                "config": first["draft"]["config"],
                "expected_draft_generation": workspace["draft"]["generation"],
            },
        )
        mismatched_config = first["draft"]["config"]
        mismatched_config["printer"]["nozzle_mm"] = 0.2
        mismatch = client.post(
            f"/api/projects/{workspace['project']['id']}/previews",
            json={
                "config": mismatched_config,
                "expected_draft_generation": first["draft"]["generation"],
            },
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_draft"
    assert mismatch.status_code == 422
    issues = mismatch.json()["error"]["details"]["issues"]
    assert [issue["code"] for issue in issues] == ["nozzle_diameter_mismatch"]
    assert issues[0]["details"]["suggested_mm"] == 0.4


def test_new_preview_supersedes_slow_old_work_and_only_current_artifacts_publish(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    old_started = threading.Event()
    release_old = threading.Event()
    calls = 0

    def controlled_factory(
        config,
        fingerprint,
        printability_profiles,
        operations=(),
        baseline_job_id=None,
    ):
        nonlocal calls
        calls += 1
        actual = make_preview_work(
            config,
            fingerprint,
            printability_profiles,
            operations,
            baseline_job_id,
        )
        if calls != 1:
            return actual

        def slow(context):
            old_started.set()
            release_old.wait(timeout=3)
            return actual(context)

        return slow

    app.state.preview_work_factory = controlled_factory
    try:
        with TestClient(app) as client:
            workspace = import_project(client)
            old = start_preview(client, workspace)
            assert old_started.wait(2)
            current_workspace = client.get(f"/api/projects/{workspace['project']['id']}").json()
            new = start_preview(client, current_workspace)
            current = app.state.worker_manager.wait_for_terminal(new["job"]["id"])
            release_old.set()
            app.state.worker_manager.wait_until_idle(old["job"]["id"])

            old_result = client.get(f"/api/jobs/{old['job']['id']}/result").json()
            new_result = client.get(f"/api/jobs/{new['job']['id']}/result").json()

        assert old_result["job"]["state"] == "superseded"
        assert old_result["artifacts"] == []
        assert current.state.value == "succeeded"
        assert [item["kind"] for item in new_result["artifacts"]] == [
            "automatic-cleanup",
            "automatic-cleanup-active",
            "automatic-cleanup-changed-mask",
            "automatic-cleanup-labels",
            "automatic-cleanup-mask-preview-image",
            "clearance-analysis",
            "editor-changed-mask",
            "editor-protected-mask",
            "editor-replay",
            "hole-analysis",
            "island-analysis",
            "palette-metrics",
            "palette-preview-image",
            "preview-image",
            "preview-statistics",
            "printability-settings",
            "processed-active",
            "processed-labels",
            "quantized-active",
            "quantized-labels",
            "quantized-preview-image",
            "region-assignment",
            "region-graph",
            "reprocessing-affected-mask",
            "reprocessing-affected-mask-preview-image",
            "reprocessing-plan",
            "risk-mask",
            "risk-report",
        ]
    finally:
        release_old.set()


def test_completed_preview_and_statistics_survive_application_restart(tmp_path) -> None:
    workspace_path = tmp_path / "workspace"
    first_app = create_app(Settings(workspace=workspace_path))
    with TestClient(first_app) as client:
        workspace = import_project(client)
        started = start_preview(client, workspace)
        first_app.state.worker_manager.wait_for_terminal(started["job"]["id"])
        expected = client.get(f"/api/jobs/{started['job']['id']}/result").json()

    second_app = create_app(Settings(workspace=workspace_path))
    with TestClient(second_app) as client:
        reopened = client.get(f"/api/projects/{workspace['project']['id']}")
        restored = client.get(f"/api/jobs/{started['job']['id']}/result")

    assert reopened.status_code == 200
    assert reopened.json()["draft"]["generation"] == 2
    assert reopened.json()["latest_preview_job"]["id"] == started["job"]["id"]
    assert reopened.json()["latest_preview_job"]["state"] == "succeeded"
    assert restored.status_code == 200
    assert restored.json() == expected


def test_palette_draft_operations_are_generation_guarded_and_survive_preview(tmp_path) -> None:
    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        workspace = import_project(client)
        config = workspace["draft"]["config"]
        before = config["palette"]["colors"]
        after = [
            {**before[0], "name": "Warm light", "hex": "#F4E2C1"},
            before[1],
        ]
        config["palette"]["colors"] = after
        operations = [
            {
                "operation_type": "palette-edit",
                "selection": {"color_id": before[0]["id"]},
                "parameters": {
                    "action": "replace",
                    "before_palette": before,
                    "after_palette": after,
                },
                "source": "manual",
                "provenance": {"ui": "palette-editor-v1"},
            }
        ]
        saved = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": config,
                "operations": operations,
                "expected_draft_generation": workspace["draft"]["generation"],
            },
        )
        stale = client.put(
            f"/api/projects/{workspace['project']['id']}/draft",
            json={
                "config": config,
                "operations": operations,
                "expected_draft_generation": workspace["draft"]["generation"],
            },
        )
        started = start_preview(
            client,
            {**workspace, "draft": saved.json()},
        )
        reopened = client.get(f"/api/projects/{workspace['project']['id']}")

    assert saved.status_code == 200
    assert saved.json()["generation"] == 2
    assert saved.json()["operations"] == operations
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_draft"
    assert started["draft"]["operations"] == operations
    assert reopened.json()["draft"]["operations"] == operations
    assert reopened.json()["draft"]["config"]["palette"]["colors"] == after


def test_auto_palette_uses_rendered_crop_and_preserves_locked_indices(tmp_path) -> None:
    output = io.BytesIO()
    image = Image.new("RGBA", (120, 60), (230, 30, 20, 255))
    for x in range(60, 120):
        for y in range(60):
            image.putpixel((x, y), (20, 70, 225, 255))
    image.save(output, format="PNG")
    image.close()

    app = create_app(Settings(workspace=tmp_path / "workspace"))
    with TestClient(app) as client:
        imported = client.post(
            "/api/projects/import",
            content=output.getvalue(),
            headers={"X-Filename": "split.png", "Content-Type": "application/octet-stream"},
        ).json()
        config = imported["draft"]["config"]
        config["palette"]["colors"][0].update({"hex": "#123456", "locked": True})
        full = client.post(
            f"/api/projects/{imported['project']['id']}/palette/auto",
            json={"config": config, "color_count": 2},
        )
        config["crop"].update({"x": 0.5, "width": 0.5})
        cropped = client.post(
            f"/api/projects/{imported['project']['id']}/palette/auto",
            json={"config": config, "color_count": 2},
        )
        config["palette"]["colors"].append(
            {
                "id": "locked-third",
                "name": "Locked third",
                "hex": "#ABCDEF",
                "locked": True,
                "filament_id": None,
            }
        )
        invalid_reduction = client.post(
            f"/api/projects/{imported['project']['id']}/palette/auto",
            json={"config": config, "color_count": 2},
        )

    assert full.status_code == 200, full.text
    assert cropped.status_code == 200, cropped.text
    assert full.json()["colors"][0] == "#123456"
    assert cropped.json()["colors"][0] == "#123456"
    assert full.json()["options_fingerprint"] == cropped.json()["options_fingerprint"]
    assert full.json()["visible_pixel_count"] == 1024 * 1024
    assert cropped.json()["visible_pixel_count"] == 1024 * 1024
    assert full.json()["colors"][1] != cropped.json()["colors"][1]
    assert invalid_reduction.status_code == 422
    assert invalid_reduction.json()["error"]["details"]["palette_code"] == "invalid_lock"
