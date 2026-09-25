from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import image23mf.exporting as exporting_module
from image23mf.api.app import create_app
from image23mf.bambu import read_bambu_3mf
from image23mf.bambu.cli import (
    BambuSliceProfiles,
    BambuStudioCanceledError,
    BambuStudioSliceError,
    BambuValidationResult,
    BambuValidationStatus,
    PinnedBambuProfile,
    SlicedArtifactEvidence,
)
from image23mf.bambu.report import (
    SlicerPlateReport,
    SlicerValidationExpectation,
    build_slicer_validation_report,
)
from image23mf.contracts.exporting import ExportMaterialMapping, StartExportRequest
from image23mf.contracts.jobs import JobStage, JobState, JobType
from image23mf.engine.labels import LabelField
from image23mf.exporting import ExportArtifactUnavailableError, ExportService
from image23mf.geometry import (
    BaseBuildBounds,
    BaseMeshOptions,
    CapabilityStatus,
    FlushInlayStrategy,
    GeometryDocument,
    LineSegment,
    Material,
    Path2D,
    Point2,
    RectangleBase,
    SourceLabel,
    assemble_mesh_document,
    build_shared_boundary_topology,
    dump_geometry_ir,
    extrude_artwork_regions,
    generate_structural_base,
)
from image23mf.settings import Settings
from image23mf.storage import (
    ArtifactRepository,
    ContentAddressedStore,
    ProjectRepository,
    open_database,
)
from image23mf.workers import LocalWorkerManager, WorkerArtifact, WorkerResult


def printable_document() -> GeometryDocument:
    labels = LabelField(width=2, height=1, label_values=(0, 1), pixels=b"\x00\x01")
    digest = hashlib.sha256(labels.pixels).hexdigest()
    blue = Material.create(name="Marine Blue", color_hex="#0078BF", palette_color_id="blue")
    orange = Material.create(name="Mandarin Orange", color_hex="#F99963", palette_color_id="orange")
    materials = tuple(sorted((blue, orange), key=lambda item: item.id))
    source_labels = tuple(
        sorted(
            (
                SourceLabel.create(
                    source_asset_id="source-1",
                    processed_labels_sha256=digest,
                    label_index=0,
                    name="Ocean",
                    color_hex="#0078BF",
                    palette_color_id="blue",
                    material_id=blue.id,
                    classification="background",
                ),
                SourceLabel.create(
                    source_asset_id="source-1",
                    processed_labels_sha256=digest,
                    label_index=1,
                    name="Sun",
                    color_hex="#F99963",
                    palette_color_id="orange",
                    material_id=orange.id,
                    classification="artwork",
                ),
            ),
            key=lambda item: item.id,
        )
    )
    construction = Path2D.create(
        purpose="construction",
        start=Point2(x_mm=0, y_mm=0),
        segments=(
            LineSegment(end=Point2(x_mm=4, y_mm=0)),
            LineSegment(end=Point2(x_mm=4, y_mm=2)),
            LineSegment(end=Point2(x_mm=0, y_mm=2)),
            LineSegment(end=Point2(x_mm=0, y_mm=0)),
        ),
        closed=True,
    )
    shape = RectangleBase.create(center=Point2(x_mm=2, y_mm=1), width_mm=4, height_mm=2)
    topology = build_shared_boundary_topology(
        labels,
        source_asset_sha256="a" * 64,
        source_labels=source_labels,
        materials=materials,
        base=shape,
        canvas_width_mm=4,
        canvas_height_mm=2,
        vector_paths=(construction,),
        vector_artifact_sha256="b" * 64,
    ).document
    base = generate_structural_base(
        shape,
        material=blue,
        options=BaseMeshOptions(thickness_mm=1.2, layer_height_mm=0.2),
        build_bounds=BaseBuildBounds(width_mm=256, depth_mm=256),
    )
    artwork = extrude_artwork_regions(
        topology,
        FlushInlayStrategy(base_top_z_mm=1.2, layer_height_mm=0.2),
    )
    document = assemble_mesh_document(
        topology,
        base_mesh=base.mesh,
        base_part=base.part,
        artwork=artwork,
    ).document
    assert document.capabilities.mesh.status == CapabilityStatus.AVAILABLE
    return document


def document_with_absent_material() -> GeometryDocument:
    document = printable_document()
    absent = Material.create(
        name="Absent Violet",
        color_hex="#7B4AB5",
        palette_color_id="violet",
        filament_id="filament-violet",
    )
    payload = document.model_dump(mode="python")
    payload.update(
        {
            "materials": tuple(sorted((*document.materials, absent), key=lambda item: item.id)),
            "palette_color_order": ("blue", "orange", "violet"),
        }
    )
    return GeometryDocument.model_validate(payload)


def pinned_profiles(tmp_path: Path, count: int = 2) -> BambuSliceProfiles:
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in ("machine", "process", *(f"filament-{index}" for index in range(count))):
        path = tmp_path / f"{name}.json"
        path.write_text('{"name":"fixture"}', encoding="utf-8")
        paths.append(PinnedBambuProfile.pin(path))
    return BambuSliceProfiles(machine=paths[0], process=paths[1], filaments=tuple(paths[2:]))


class FakeValidator:
    def __init__(self) -> None:
        self.calls = 0
        self.expectations = []

    def validate(
        self,
        source,
        *,
        profiles,
        expectation=None,
        cancellation=None,
        project_output=None,
        sliced_output=None,
    ):
        self.calls += 1
        project = read_bambu_3mf(source)
        assert len(profiles.filaments) == len(project.materials)
        assert isinstance(expectation, SlicerValidationExpectation)
        self.expectations.append(expectation)
        used_material_indices = tuple(sorted({mesh.material_index for mesh in project.meshes}))
        used_materials = tuple(project.materials[index] for index in used_material_indices)
        payload = Path(source).read_bytes()
        if project_output is not None:
            project_output.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        report = build_slicer_validation_report(
            (
                SlicerPlateReport(
                    member="Metadata/plate_1.gcode",
                    declared_layer_count=3,
                    observed_layer_count=3,
                    max_z_height_mm=0.6,
                    used_extruders=tuple(item.extruder for item in used_materials),
                    observed_colors=tuple(item.color.upper() for item in used_materials),
                    filament_usage=(),
                    tool_changes=(),
                    model_extrusion_bounds=None,
                    has_extrusion=True,
                ),
            ),
            expectation=expectation,
            stdout="sliced fixture",
        )
        return BambuValidationResult(
            status=BambuValidationStatus.VALIDATED,
            source_sha256=digest,
            executable=Path("/fixture/BambuStudio"),
            version="02.07.01.62",
            command=("BambuStudio", "--slice", "0", "project.3mf"),
            return_code=0,
            stdout="sliced fixture",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
            canceled=False,
            warnings=(),
            profile_set_sha256="c" * 64,
            cache_key=f"fixture:{digest}",
            artifact=SlicedArtifactEvidence(
                name="fixture.gcode.3mf",
                sha256="d" * 64,
                size_bytes=100,
                gcode_members=("Metadata/plate_1.gcode",),
                uncompressed_gcode_bytes=1000,
            ),
            report=report,
        )


class FailingValidator(FakeValidator):
    def validate(
        self,
        source,
        *,
        profiles,
        expectation=None,
        cancellation=None,
        project_output=None,
        sliced_output=None,
    ):
        result = super().validate(
            source,
            profiles=profiles,
            expectation=expectation,
            cancellation=cancellation,
            project_output=project_output,
        )
        failed = dataclass_replace(
            result,
            status=BambuValidationStatus.FAILED,
            return_code=7,
            stderr="fixture slicing failed",
        )
        raise BambuStudioSliceError("fixture failure", failed)


class BlockingValidator(FakeValidator):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def validate(
        self,
        source,
        *,
        profiles,
        expectation=None,
        cancellation=None,
        project_output=None,
        sliced_output=None,
    ):
        del source, profiles, expectation
        self.calls += 1
        self.started.set()
        while cancellation is None or not cancellation.canceled:
            time.sleep(0.01)
        raise BambuStudioCanceledError(
            "canceled",
            BambuValidationResult(
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
            ),
        )


def dataclass_replace(value, **changes):
    import dataclasses

    return dataclasses.replace(value, **changes)


def seeded_workspace(tmp_path, *, validator=None, document=None):
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    store = ContentAddressedStore(workspace)
    connection = open_database(database_path)
    try:
        project = ProjectRepository(connection).create("Export fixture")
    finally:
        connection.close()
    manager = LocalWorkerManager(database_path=database_path, blob_store=store)
    source_document = document or printable_document()
    payload = dump_geometry_ir(source_document)

    def geometry_work(context):
        blob = context.blob_store.put_bytes(
            payload,
            namespace="artifacts",
            extension=".json",
            media_type="application/json",
        )
        return WorkerResult(
            artifacts=(
                WorkerArtifact(
                    kind="geometry-ir",
                    derivation_key=hashlib.sha256(payload).hexdigest(),
                    blob=blob,
                ),
            )
        )

    geometry_job = manager.submit(
        project_id=project.id,
        job_type=JobType.GEOMETRY,
        initial_stage=JobStage.MESHING,
        work=geometry_work,
    )
    geometry_job = manager.wait_for_terminal(geometry_job.id)
    connection = open_database(database_path)
    try:
        artifact = ArtifactRepository(connection).get(geometry_job.artifact_ids[0])
    finally:
        connection.close()
    selected_validator = validator or FakeValidator()
    profiles = pinned_profiles(tmp_path / "profiles", count=len(source_document.materials))
    service = ExportService(
        database_path=database_path,
        blob_store=store,
        validator=selected_validator,
        profile_provider=lambda _project: profiles,
    )
    request = StartExportRequest(
        geometry_artifact_id=artifact.id,
        geometry_sha256=artifact.sha256,
        name="Current draft export",
        profile={"nozzle_diameter_mm": 0.4, "layer_height_mm": 0.2},
        minimum_part_thickness_mm=0.2,
    )
    return manager, service, project, artifact, request, store, selected_validator


@pytest.mark.parametrize("nozzle,layer", ((0.2, 0.1), (0.4, 0.2)))
def test_success_publishes_complete_atomic_bundle_and_downloadable_valid_3mf(
    tmp_path, nozzle, layer
):
    manager, service, project, _source, request, store, validator = seeded_workspace(tmp_path)
    request = request.model_copy(
        update={
            "profile": request.profile.model_copy(
                update={"nozzle_diameter_mm": nozzle, "layer_height_mm": layer}
            )
        }
    )
    try:
        started = service.start_export(project_id=project.id, request=request, manager=manager)
        terminal = manager.wait_for_terminal(started.job.id)
        result = service.get_result(project_id=project.id, job_id=terminal.id)

        assert terminal.state == JobState.SUCCEEDED
        assert result.download_ready
        assert {item.kind for item in result.artifacts} == {
            "geometry-quality-report",
            "bambu-3mf",
            "bambu-validation-report",
            "bambu-validation-log",
        }
        package = result.package
        assert package is not None
        connection = open_database(service.database_path)
        try:
            records = ArtifactRepository(connection).list_for_job(terminal.id)
            package_record = next(item.relative_path for item in records if item.id == package.id)
            report_record = next(
                item.relative_path for item in records if item.kind == "bambu-validation-report"
            )
        finally:
            connection.close()
        package_path = store.path_for(package_record)
        parsed = read_bambu_3mf(package_path)
        persisted_report = json.loads(store.path_for(report_record).read_text(encoding="utf-8"))
        assert parsed.settings.nozzle_diameter == nozzle
        assert parsed.settings.layer_height == layer
        assert len(parsed.materials) == 2
        assert validator.calls == 1
        assert validator.expectations[0].expected_colors == tuple(
            item.color.upper() for item in parsed.materials
        )
        assert persisted_report["slicer_report"]["expected_colors_present"] is True
        assert persisted_report["slicer_report"]["observed_colors"] == list(
            validator.expectations[0].expected_colors
        )
        assert persisted_report["result"]["stdout"] == "sliced fixture"
    finally:
        manager.shutdown()


def test_absent_palette_slot_is_packaged_but_not_required_from_slicer_output(tmp_path):
    document = document_with_absent_material()
    manager, service, project, _source, request, store, validator = seeded_workspace(
        tmp_path, document=document
    )
    try:
        terminal = manager.wait_for_terminal(
            service.start_export(project_id=project.id, request=request, manager=manager).job.id
        )
        assert terminal.state == JobState.SUCCEEDED
        connection = open_database(service.database_path)
        try:
            package_record = next(
                item.relative_path
                for item in ArtifactRepository(connection).list_for_job(terminal.id)
                if item.kind == "bambu-3mf"
            )
        finally:
            connection.close()
        parsed = read_bambu_3mf(store.path_for(package_record))

        assert [item.palette_color_id for item in parsed.materials] == [
            "blue",
            "orange",
            "violet",
        ]
        used_slots = {mesh.material_index for mesh in parsed.meshes}
        assert {parsed.materials[index].palette_color_id for index in used_slots} == {
            "blue",
            "orange",
        }
        assert validator.expectations[0].expected_colors == ("#0078BF", "#F99963")
        assert "#7B4AB5" not in validator.expectations[0].expected_colors
    finally:
        manager.shutdown()


def test_explicit_reordered_palette_controls_slots_without_losing_names_or_colors(tmp_path):
    document = document_with_absent_material()
    by_palette = {item.palette_color_id: item for item in document.materials}
    desired_order = ("violet", "orange", "blue")
    manager, service, project, _source, request, store, validator = seeded_workspace(
        tmp_path, document=document
    )
    request = request.model_copy(
        update={
            "material_mapping": tuple(
                ExportMaterialMapping(
                    material_id=by_palette[palette_id].id,
                    extruder=index,
                    preset="Bambu PLA Matte",
                )
                for index, palette_id in enumerate(desired_order, start=1)
            )
        }
    )
    try:
        terminal = manager.wait_for_terminal(
            service.start_export(project_id=project.id, request=request, manager=manager).job.id
        )
        assert terminal.state == JobState.SUCCEEDED
        connection = open_database(service.database_path)
        try:
            package_record = next(
                item.relative_path
                for item in ArtifactRepository(connection).list_for_job(terminal.id)
                if item.kind == "bambu-3mf"
            )
        finally:
            connection.close()
        parsed = read_bambu_3mf(store.path_for(package_record))

        assert tuple(item.palette_color_id for item in parsed.materials) == desired_order
        assert tuple(item.name for item in parsed.materials) == tuple(
            by_palette[palette_id].name for palette_id in desired_order
        )
        assert tuple(item.color for item in parsed.materials) == tuple(
            by_palette[palette_id].color_hex for palette_id in desired_order
        )
        assert validator.expectations[0].expected_colors == ("#F99963", "#0078BF")
    finally:
        manager.shutdown()


def test_identical_success_is_cached_without_reslicing_and_corruption_forces_retry(tmp_path):
    manager, service, project, _source, request, store, validator = seeded_workspace(tmp_path)
    try:
        first = service.start_export(project_id=project.id, request=request, manager=manager).job
        first = manager.wait_for_terminal(first.id)
        cached = service.start_export(project_id=project.id, request=request, manager=manager).job
        assert cached.id == first.id
        assert validator.calls == 1

        connection = open_database(service.database_path)
        try:
            package = next(
                item
                for item in ArtifactRepository(connection).list_for_job(first.id)
                if item.kind == "bambu-3mf"
            )
        finally:
            connection.close()
        store.path_for(package.relative_path).write_bytes(b"corrupt")
        with pytest.raises(ExportArtifactUnavailableError, match="corrupt"):
            service.get_result(project_id=project.id, job_id=first.id)
        retried = service.start_export(project_id=project.id, request=request, manager=manager).job
        assert retried.id != first.id
        assert manager.wait_for_terminal(retried.id).state == JobState.SUCCEEDED
        assert validator.calls == 2
    finally:
        manager.shutdown()


def test_concurrent_identical_exports_share_one_job_and_one_slice(tmp_path):
    manager, service, project, _source, request, _store, validator = seeded_workspace(tmp_path)
    barrier = threading.Barrier(3)
    job_ids = []
    errors = []

    def start() -> None:
        try:
            barrier.wait(timeout=2)
            job_ids.append(
                service.start_export(project_id=project.id, request=request, manager=manager).job.id
            )
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=start), threading.Thread(target=start)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=5)
        assert errors == []
        assert len(job_ids) == 2
        assert len(set(job_ids)) == 1
        assert manager.wait_for_terminal(job_ids[0]).state == JobState.SUCCEEDED
        assert validator.calls == 1
    finally:
        manager.shutdown()


def test_engine_or_pinned_profile_upgrade_invalidates_export_cache(tmp_path, monkeypatch):
    manager, service, project, _source, request, _store, validator = seeded_workspace(tmp_path)
    try:
        first = service.start_export(project_id=project.id, request=request, manager=manager).job
        assert manager.wait_for_terminal(first.id).state == JobState.SUCCEEDED

        monkeypatch.setattr(exporting_module, "__version__", "99.0.0-upgrade-proof")
        upgraded = service.start_export(project_id=project.id, request=request, manager=manager).job
        assert upgraded.id != first.id
        assert manager.wait_for_terminal(upgraded.id).state == JobState.SUCCEEDED

        replacement_profiles = pinned_profiles(tmp_path / "replacement-profiles")
        replacement_profiles.machine.path.write_text(
            '{"name":"upgraded fixture"}', encoding="utf-8"
        )
        replacement_profiles = BambuSliceProfiles(
            machine=PinnedBambuProfile.pin(replacement_profiles.machine.path),
            process=replacement_profiles.process,
            filaments=replacement_profiles.filaments,
        )
        service.profile_provider = lambda _project: replacement_profiles
        repinned = service.start_export(project_id=project.id, request=request, manager=manager).job
        assert repinned.id != upgraded.id
        assert manager.wait_for_terminal(repinned.id).state == JobState.SUCCEEDED
        assert validator.calls == 3
    finally:
        manager.shutdown()


def test_slicer_failure_and_cancellation_publish_no_partial_artifacts(tmp_path):
    manager, service, project, _source, request, _store, _validator = seeded_workspace(
        tmp_path / "failure", validator=FailingValidator()
    )
    try:
        failed = service.start_export(project_id=project.id, request=request, manager=manager).job
        failed = manager.wait_for_terminal(failed.id)
        assert failed.state == JobState.FAILED
        assert failed.failure and failed.failure.code == "bambu_slice_failed"
        assert failed.artifact_ids == ()
    finally:
        manager.shutdown()

    blocker = BlockingValidator()
    manager, service, project, _source, request, _store, _validator = seeded_workspace(
        tmp_path / "cancel", validator=blocker
    )
    try:
        running = service.start_export(project_id=project.id, request=request, manager=manager).job
        assert blocker.started.wait(2)
        manager.cancel(running.id)
        manager.wait_until_idle(running.id)
        canceled = manager.get(running.id)
        assert canceled.state == JobState.CANCELED
        assert canceled.artifact_ids == ()
    finally:
        manager.shutdown()


def test_cross_project_and_corrupt_source_are_rejected_before_queueing(tmp_path):
    manager, service, project, source, request, store, _validator = seeded_workspace(tmp_path)
    connection = open_database(service.database_path)
    try:
        other = ProjectRepository(connection).create("Other project")
    finally:
        connection.close()
    try:
        try:
            service.start_export(project_id=other.id, request=request, manager=manager)
        except Exception as error:
            assert type(error).__name__ == "RecordNotFoundError"
        else:  # pragma: no cover
            raise AssertionError("cross-project artifact was accepted")

        store.path_for(source.relative_path).write_bytes(b"corrupt")
        try:
            service.start_export(project_id=project.id, request=request, manager=manager)
        except ExportArtifactUnavailableError:
            pass
        else:  # pragma: no cover
            raise AssertionError("corrupt Geometry IR was accepted")
    finally:
        manager.shutdown()


def test_quality_failure_and_supersession_are_actionable_and_publish_atomically(tmp_path):
    manager, service, project, _source, request, _store, _validator = seeded_workspace(
        tmp_path / "quality"
    )
    try:
        unsafe = request.model_copy(update={"minimum_part_thickness_mm": 0.8})
        failed = service.start_export(project_id=project.id, request=unsafe, manager=manager).job
        failed = manager.wait_for_terminal(failed.id)
        assert failed.state == JobState.FAILED
        assert failed.failure and failed.failure.code == "geometry_quality_failed"
        assert failed.failure.details["finding_codes"] == ["part-too-thin"]
        assert failed.artifact_ids == ()
    finally:
        manager.shutdown()

    blocker = BlockingValidator()
    manager, service, project, _source, request, _store, _validator = seeded_workspace(
        tmp_path / "supersession", validator=blocker
    )
    try:
        old = service.start_export(project_id=project.id, request=request, manager=manager).job
        assert blocker.started.wait(2)
        replacement_validator = FakeValidator()
        service.validator = replacement_validator
        newer_request = request.model_copy(update={"name": "Newer export intent"})
        newer = service.start_export(
            project_id=project.id, request=newer_request, manager=manager
        ).job
        newer = manager.wait_for_terminal(newer.id)
        manager.wait_until_idle(old.id)
        assert manager.get(old.id).state == JobState.SUPERSEDED
        assert manager.get(old.id).artifact_ids == ()
        assert newer.state == JobState.SUCCEEDED
        assert replacement_validator.calls == 1
    finally:
        manager.shutdown()


def test_export_api_returns_result_and_project_scoped_download(tmp_path):
    manager, service, project, _source, request, _store, _validator = seeded_workspace(
        tmp_path / "seed"
    )
    manager.shutdown()
    app = create_app(Settings(workspace=service.blob_store.root, bambu_resources_root=None))
    app.state.export_service = service
    service.validator = FakeValidator()
    service.profile_provider = lambda _project: pinned_profiles(tmp_path / "api-profiles")
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project.id}/exports",
            json=request.model_dump(mode="json"),
        )
        assert response.status_code == 202
        job_id = response.json()["job"]["id"]
        terminal = app.state.worker_manager.wait_for_terminal(job_id)
        assert terminal.state == JobState.SUCCEEDED
        result = client.get(f"/api/projects/{project.id}/exports/{job_id}")
        assert result.status_code == 200
        package = result.json()["package"]
        downloaded = client.get(package["download_url"])
        assert downloaded.status_code == 200
        assert read_bambu_3mf(downloaded.content).name == "Current draft export"

        connection = open_database(service.database_path)
        try:
            other = ProjectRepository(connection).create("API other")
        finally:
            connection.close()
        assert client.get(f"/api/projects/{other.id}/exports/{job_id}").status_code == 404
        assert client.get(f"/api/projects/{other.id}/artifacts/{package['id']}").status_code == 404


def test_export_publishes_prepared_bytes_instead_of_intermediate(tmp_path):
    class PreparedValidator(FakeValidator):
        def validate(self, source, **kwargs):
            result = super().validate(source, **kwargs)
            output = kwargs["project_output"]
            # ZIP comments keep the fixture structurally readable but change its bytes.
            import zipfile

            with zipfile.ZipFile(output, "a") as archive:
                archive.comment = b"Bambu serialized project fixture"
            self.expected_bytes = output.read_bytes()
            return dataclass_replace(
                result, source_sha256=hashlib.sha256(self.expected_bytes).hexdigest()
            )

    validator = PreparedValidator()
    manager, service, project, _, request, store, _ = seeded_workspace(
        tmp_path, validator=validator
    )
    try:
        terminal = manager.wait_for_terminal(
            service.start_export(project_id=project.id, request=request, manager=manager).job.id
        )
        assert terminal.state == JobState.SUCCEEDED
        connection = open_database(service.database_path)
        try:
            record = next(
                r
                for r in ArtifactRepository(connection).list_for_job(terminal.id)
                if r.kind == "bambu-3mf"
            )
        finally:
            connection.close()
        assert store.path_for(record.relative_path).read_bytes() == validator.expected_bytes
        assert record.sha256 == hashlib.sha256(validator.expected_bytes).hexdigest()
    finally:
        manager.shutdown()


@pytest.mark.parametrize("error", [OverflowError("coordinate overflow"), AssertionError()])
def test_optional_preview_error_preserves_validated_download(tmp_path, monkeypatch, error):
    class RetainedSliceValidator(FakeValidator):
        def validate(self, source, **kwargs):
            result = super().validate(source, **kwargs)
            payload = b"retained test slice"
            kwargs["sliced_output"].write_bytes(payload)
            return dataclass_replace(
                result,
                artifact=dataclass_replace(
                    result.artifact,
                    sha256=hashlib.sha256(payload).hexdigest(),
                ),
            )

    def fail_preview(*args, **kwargs):
        raise error

    monkeypatch.setattr(exporting_module, "render_sliced_preview", fail_preview)
    manager, service, project, _source, request, _store, _validator = seeded_workspace(
        tmp_path,
        validator=RetainedSliceValidator(),
    )
    try:
        job = service.start_export(project_id=project.id, request=request, manager=manager).job
        assert manager.wait_for_terminal(job.id).state == JobState.SUCCEEDED
        result = service.get_result(project_id=project.id, job_id=job.id)
        assert result.download_ready
        assert result.validation_report.metadata["sliced_preview_unavailable_reason"]
        assert not any(a.kind == "bambu-slicer-preview-image" for a in result.artifacts)
    finally:
        manager.shutdown()
