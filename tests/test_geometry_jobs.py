import json
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from image23mf.contracts.jobs import JobState, JobType
from image23mf.geometry.extrusion import ArtworkExtrusionError
from image23mf.geometry.jobs import (
    GEOMETRY_IR_KIND,
    GEOMETRY_MESH_KIND,
    GEOMETRY_PREVIEW_KIND,
    GEOMETRY_REPORT_KIND,
    GEOMETRY_SVG_KIND,
    BinaryArtifact,
    ExtrusionOutput,
    GeometryJobPipeline,
    GeometryJobRequest,
    GeometryPhase,
    GeometryPipelineAdapters,
    ValidationOutput,
    VectorizationOutput,
    geometry_derivation_key,
)
from image23mf.storage import (
    ArtifactRepository,
    ContentAddressedStore,
    ProjectRepository,
    open_database,
)
from image23mf.workers import LocalWorkerManager


@dataclass
class FakeGeometry:
    calls: list[str] = field(default_factory=list)
    workspaces: list = field(default_factory=list)
    vectorize_started: threading.Event = field(default_factory=threading.Event)
    release_vectorize: threading.Event = field(default_factory=threading.Event)
    block_vectorize: bool = False
    crash_phase: Optional[str] = None
    cancel_phase: Optional[str] = None
    bad_preview_extension: bool = False
    extrusion_failure: Optional[str] = None

    def adapters(self) -> GeometryPipelineAdapters:
        return GeometryPipelineAdapters(
            vectorize=self.vectorize,
            topology=self.topology,
            extrude=self.extrude,
            validate=self.validate,
            preview=self.preview,
        )

    def enter(self, name, context):
        self.calls.append(name)
        self.workspaces.append(context.workspace)
        assert context.workspace.is_dir()
        assert context.phase.value == name
        if self.crash_phase == name:
            raise RuntimeError(f"crashed in {name}")
        if self.cancel_phase == name:
            while True:
                context.check_canceled()
                time.sleep(0.005)

    def vectorize(self, _request, context):
        self.enter("vectorize", context)
        self.vectorize_started.set()
        if self.block_vectorize:
            while not self.release_vectorize.wait(0.005):
                context.check_canceled()
        return VectorizationOutput(
            geometry={"paths": 2},
            svg=BinaryArtifact(b"<svg/>", ".svg", "image/svg+xml"),
        )

    def topology(self, _request, vectorized, context):
        self.enter("topology", context)
        assert vectorized.geometry == {"paths": 2}
        return {"islands": 2}

    def extrude(self, _request, topology, context):
        self.enter("extrude", context)
        assert topology == {"islands": 2}
        if self.extrusion_failure is not None:
            raise ArtworkExtrusionError(self.extrusion_failure)
        return ExtrusionOutput(
            geometry={"meshes": 3},
            geometry_ir=BinaryArtifact(
                json.dumps({"schema_version": 1, "meshes": ["mesh-1"]}).encode(),
                ".json",
                "application/vnd.image23mf.geometry+json",
            ),
            mesh=BinaryArtifact(b"solid mesh", ".stl", "model/stl"),
        )

    def validate(self, _request, extruded, context):
        self.enter("validate", context)
        assert extruded.geometry == {"meshes": 3}
        return ValidationOutput(
            accepted=True,
            report=BinaryArtifact(
                json.dumps({"accepted": True}).encode(),
                ".json",
                "application/json",
            ),
        )

    def preview(self, _request, _extruded, _validation, context):
        self.enter("preview", context)
        return BinaryArtifact(
            b"preview",
            ".not-valid-extension" if self.bad_preview_extension else ".png",
            "image/png",
        )


def setup_manager(tmp_path, *, max_workers=2):
    workspace = tmp_path / "workspace"
    database_path = workspace / "image23mf.sqlite3"
    connection = open_database(database_path)
    try:
        project = ProjectRepository(connection).create("Geometry pipeline")
    finally:
        connection.close()
    manager = LocalWorkerManager(
        database_path=database_path,
        blob_store=ContentAddressedStore(workspace),
        max_workers=max_workers,
    )
    return manager, project, database_path


def request(key="a" * 64):
    return GeometryJobRequest(
        derivation_key=key,
        payload={"mask": "fixture"},
        metadata={"source": "test"},
    )


def artifacts_for(database_path, job_id):
    connection = open_database(database_path)
    try:
        return ArtifactRepository(connection).list_for_job(job_id)
    finally:
        connection.close()


def test_geometry_pipeline_reports_all_phases_and_atomically_publishes_artifacts(tmp_path):
    manager, project, database_path = setup_manager(tmp_path)
    fake = FakeGeometry()
    phases = []
    pipeline = GeometryJobPipeline(adapters=fake.adapters(), phase_observer=phases.append)
    try:
        job = pipeline.submit(manager, project_id=project.id, request=request())
        result = manager.wait_for_terminal(job.id)

        assert result.state == JobState.SUCCEEDED
        assert fake.calls == ["vectorize", "topology", "extrude", "validate", "preview"]
        assert [item.phase for item in phases] == list(GeometryPhase)
        assert [item.progress for item in phases] == sorted(item.progress for item in phases)
        artifacts = artifacts_for(database_path, result.id)
        assert {item.kind for item in artifacts} == {
            GEOMETRY_SVG_KIND,
            GEOMETRY_IR_KIND,
            GEOMETRY_MESH_KIND,
            GEOMETRY_REPORT_KIND,
            GEOMETRY_PREVIEW_KIND,
        }
        assert {item.derivation_key for item in artifacts} == {"a" * 64}
        assert all(item.metadata["geometry_pipeline_schema_version"] == 1 for item in artifacts)
    finally:
        manager.shutdown()


def test_geometry_cancellation_stops_stage_and_cleans_workspace_without_publication(tmp_path):
    manager, project, database_path = setup_manager(tmp_path)
    fake = FakeGeometry(cancel_phase="topology")
    pipeline = GeometryJobPipeline(adapters=fake.adapters())
    try:
        job = pipeline.submit(manager, project_id=project.id, request=request())
        deadline = time.monotonic() + 2
        while "topology" not in fake.calls and time.monotonic() < deadline:
            time.sleep(0.005)
        assert "topology" in fake.calls
        manager.cancel(job.id)
        manager.wait_until_idle(job.id)

        assert manager.get(job.id).state == JobState.CANCELED
        assert artifacts_for(database_path, job.id) == ()
        assert fake.workspaces and all(not path.exists() for path in fake.workspaces)
    finally:
        manager.shutdown()


def test_completed_derivation_is_reused_after_pipeline_restart(tmp_path):
    manager, project, database_path = setup_manager(tmp_path)
    first_fake = FakeGeometry()
    first = GeometryJobPipeline(adapters=first_fake.adapters(), memory_cache_entries=0)
    try:
        first_job = first.submit(manager, project_id=project.id, request=request())
        assert manager.wait_for_terminal(first_job.id).state == JobState.SUCCEEDED

        second_fake = FakeGeometry(crash_phase="vectorize")
        observed = []
        second = GeometryJobPipeline(
            adapters=second_fake.adapters(),
            phase_observer=observed.append,
            memory_cache_entries=0,
        )
        second_job = second.submit(
            manager,
            project_id=project.id,
            request=request(),
            job_type=JobType.EXPORT,
        )
        result = manager.wait_for_terminal(second_job.id)

        assert result.state == JobState.SUCCEEDED
        assert second_fake.calls == []
        assert [(item.phase, item.cache_hit) for item in observed] == [
            (GeometryPhase.PUBLISH, True)
        ]
        cached = artifacts_for(database_path, second_job.id)
        assert len(cached) == 5
        geometry_ir = next(item for item in cached if item.kind == GEOMETRY_IR_KIND)
        assert geometry_ir.media_type == "application/vnd.image23mf.geometry+json"
        assert all(item.metadata["cache_hit"] is True for item in cached)
    finally:
        manager.shutdown()


def test_concurrent_exports_single_flight_one_derivation(tmp_path):
    manager, project, database_path = setup_manager(tmp_path, max_workers=2)
    fake = FakeGeometry(block_vectorize=True)
    phases = []
    pipeline = GeometryJobPipeline(adapters=fake.adapters(), phase_observer=phases.append)
    try:
        first = pipeline.submit(
            manager,
            project_id=project.id,
            request=request(),
            job_type=JobType.EXPORT,
        )
        assert fake.vectorize_started.wait(2)
        second = pipeline.submit(
            manager,
            project_id=project.id,
            request=request(),
            job_type=JobType.EXPORT,
        )
        fake.release_vectorize.set()

        assert manager.wait_for_terminal(first.id).state == JobState.SUCCEEDED
        assert manager.wait_for_terminal(second.id).state == JobState.SUCCEEDED
        assert fake.calls.count("vectorize") == 1
        assert len(artifacts_for(database_path, first.id)) == 5
        assert len(artifacts_for(database_path, second.id)) == 5
        assert any(item.phase == GeometryPhase.PUBLISH and item.cache_hit for item in phases)
    finally:
        fake.release_vectorize.set()
        manager.shutdown()


def test_stage_crash_cleans_workspace_and_publishes_nothing(tmp_path):
    manager, project, database_path = setup_manager(tmp_path)
    fake = FakeGeometry(crash_phase="extrude")
    pipeline = GeometryJobPipeline(adapters=fake.adapters())
    try:
        job = pipeline.submit(manager, project_id=project.id, request=request())
        result = manager.wait_for_terminal(job.id)

        assert result.state == JobState.FAILED
        assert result.failure is not None and result.failure.code == "worker_crash"
        assert artifacts_for(database_path, job.id) == ()
        assert fake.workspaces and all(not path.exists() for path in fake.workspaces)
    finally:
        manager.shutdown()


def test_artwork_extrusion_failure_preserves_actionable_geometry_diagnostic(tmp_path):
    manager, project, database_path = setup_manager(tmp_path)
    diagnostic = (
        "island island_example could not be extruded: polygon triangulation ended with a face "
        "below mesh edge or altitude tolerance"
    )
    fake = FakeGeometry(extrusion_failure=diagnostic)
    pipeline = GeometryJobPipeline(adapters=fake.adapters())
    try:
        job = pipeline.submit(manager, project_id=project.id, request=request())
        result = manager.wait_for_terminal(job.id)

        assert result.state == JobState.FAILED
        assert result.failure is not None
        assert result.failure.code == "geometry_extrusion_failed"
        assert result.failure.retryable is False
        assert diagnostic in result.failure.message
        assert result.failure.details == {
            "exception_type": "ArtworkExtrusionError",
            "phase": "extrude",
        }
        assert artifacts_for(database_path, job.id) == ()
    finally:
        manager.shutdown()


def test_blob_failure_after_other_payloads_still_publishes_no_artifact_records(tmp_path):
    manager, project, database_path = setup_manager(tmp_path)
    fake = FakeGeometry(bad_preview_extension=True)
    pipeline = GeometryJobPipeline(adapters=fake.adapters())
    try:
        job = pipeline.submit(manager, project_id=project.id, request=request())
        result = manager.wait_for_terminal(job.id)

        assert result.state == JobState.FAILED
        assert artifacts_for(database_path, job.id) == ()
    finally:
        manager.shutdown()


def test_geometry_derivation_key_is_canonical_and_covers_stage_versions():
    values = dict(
        source_sha256="1" * 64,
        processed_labels_sha256="2" * 64,
        settings={"nozzle": 0.4, "colors": ["black", "white"]},
        stage_versions={"vectorize": "potrace-1.16", "topology": "1"},
    )
    first = geometry_derivation_key(**values)
    reordered = geometry_derivation_key(
        **{
            **values,
            "settings": {"colors": ["black", "white"], "nozzle": 0.4},
            "stage_versions": {"topology": "1", "vectorize": "potrace-1.16"},
        }
    )
    changed = geometry_derivation_key(
        **{**values, "stage_versions": {"vectorize": "potrace-1.16", "topology": "2"}}
    )

    assert first == reordered
    assert first != changed
    assert len(first) == 64
