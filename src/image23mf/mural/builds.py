"""Durable asynchronous master-canvas mural packaging.

The build binds one saved plan generation to the exact preview labels and Geometry IR that
produced it.  It partitions the authoritative master once, retains seam/topology proof as
first-class artifacts, and writes one deterministic multi-plate Bambu project.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- Python 3.9 remains a supported runtime.
import dataclasses
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from image23mf import __version__
from image23mf.bambu import (
    BambuProfileError,
    BambuProject,
    BambuProjectSettings,
    profile_plan_for_project,
    resolve_p2s_profile_set,
)
from image23mf.bambu import (
    Material as BambuMaterial,
)
from image23mf.bambu import (
    Mesh as BambuMesh,
)
from image23mf.bambu.cli import (
    BambuSliceProfiles,
    BambuStudioCanceledError,
    BambuStudioCliValidator,
    BambuStudioProfileError,
    BambuStudioSliceError,
    BambuStudioTimeoutError,
    BambuStudioUnavailableError,
    BambuValidationResult,
    BambuValidationStatus,
    InvalidSlicedArtifactError,
    PinnedBambuProfile,
)
from image23mf.bambu.mural_package import (
    MuralBuildPlate,
    PrimeTowerReserve,
    build_bambu_mural_3mf,
    read_bambu_mural_3mf,
)
from image23mf.bambu.report import SlicerValidationExpectation
from image23mf.contracts.jobs import JobFailure, JobStage, JobState, JobType
from image23mf.contracts.mural import (
    MuralBuildResult,
    MuralBuildStartResponse,
    MuralValidationResource,
    StartMuralBuildRequest,
)
from image23mf.contracts.processing import ArtifactResource
from image23mf.engine.labels import LabelField
from image23mf.geometry import (
    BaseBuildBounds,
    BaseMeshOptions,
    FlushInlayStrategy,
    assemble_mesh_document,
    extrude_artwork_regions,
    generate_structural_base,
    load_geometry_ir,
)
from image23mf.geometry.model import (
    CapabilityStatus,
    GeometryDocument,
    LineSegment,
    Path2D,
    Point2,
    RectangleBase,
)
from image23mf.geometry.serialization import GeometryIRDecodeError
from image23mf.geometry.topology import build_shared_boundary_topology
from image23mf.mural.assembly_aids import build_mural_assembly_aids
from image23mf.mural.partition import partition_master_labels
from image23mf.mural.provenance import InvalidMuralPlanSourceError
from image23mf.mural.repository import MuralPlanRecord, MuralPlanRepository
from image23mf.mural.seam_qa import QaStatus, build_mural_seam_qa
from image23mf.mural.topology_partition import partition_master_topology
from image23mf.profiles import ProfileCatalogService
from image23mf.storage import (
    ArtifactRecord,
    ArtifactRepository,
    ContentAddressedStore,
    DraftRepository,
    JobRepository,
    ProjectRepository,
    RecordNotFoundError,
    RevisionRepository,
    StoredBlob,
    open_database,
)
from image23mf.workers import (
    JobContext,
    LocalWorkerManager,
    WorkerArtifact,
    WorkerCanceledError,
    WorkerJobError,
    WorkerResult,
)

# Normal outer-wall spacing also changes the embedded mural process settings.
MURAL_BUILD_ADAPTER_VERSION = 3
PRIME_TOWER_EDGE_MARGIN_MM = 3.0
MINIMUM_PRIME_TOWER_WIDTH_MM = 10.0
MURAL_BUILD_REQUIRED_ARTIFACT_KINDS = (
    "bambu-mural-3mf",
    "mural-label-partition",
    "mural-topology-partition",
    "mural-seam-qa",
    "bambu-mural-validation-report",
    "bambu-mural-validation-log",
)
MURAL_BUILD_ASSEMBLY_ARTIFACT_KINDS = (
    "mural-assembly-aids",
    "mural-assembly-sheet",
)


def _valid_artifact_kind_set(kinds: set[str]) -> bool:
    required = set(MURAL_BUILD_REQUIRED_ARTIFACT_KINDS)
    return kinds in (
        required,
        required | set(MURAL_BUILD_ASSEMBLY_ARTIFACT_KINDS),
    )


class InvalidMuralBuildRequestError(ValueError):
    """The request is stale or does not bind to the saved master and geometry."""

    def __init__(
        self,
        message: str,
        *,
        current_plan_generation: Optional[int] = None,
        current_request_fingerprint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.current_plan_generation = current_plan_generation
        self.current_request_fingerprint = current_request_fingerprint


class MuralBuildArtifactUnavailableError(RuntimeError):
    """A required immutable input or published output is missing or corrupt."""


ProfileProvider = Callable[[BambuProject], BambuSliceProfiles]


@dataclasses.dataclass(frozen=True)
class _BuildInputs:
    plan: MuralPlanRecord
    document: GeometryDocument
    geometry: ArtifactRecord
    labels: LabelField
    base_thickness_mm: float
    art_thickness_mm: float


class MuralBuildService:
    """Validate, schedule, cache, and read exact mural build jobs."""

    def __init__(
        self,
        *,
        database_path: Path,
        blob_store: ContentAddressedStore,
        profiles: ProfileCatalogService,
        bambu_resources_root: Optional[Path] = None,
        validator: Optional[BambuStudioCliValidator] = None,
        profile_provider: Optional[ProfileProvider] = None,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.blob_store = blob_store
        self.profiles = profiles
        self.bambu_resources_root = (
            None if bambu_resources_root is None else bambu_resources_root.expanduser().resolve()
        )
        self.validator = validator or BambuStudioCliValidator()
        self.profile_provider = profile_provider or self._installed_profiles
        self._request_guard = threading.RLock()

    def start(
        self,
        *,
        project_id: str,
        request: StartMuralBuildRequest,
        manager: LocalWorkerManager,
    ) -> MuralBuildStartResponse:
        inputs = self._load_inputs(project_id, request)
        materials = _bambu_materials(inputs.document, request)
        settings = _settings(request)
        request_key = _request_key(project_id, request, inputs.plan)
        with self._request_guard:
            cached = self._cached_or_active(project_id, request_key)
            if cached is not None:
                return MuralBuildStartResponse(job=cached[0], cache_hit=cached[1])
            job = manager.submit(
                project_id=project_id,
                revision_id=inputs.plan.request.source.revision_id,
                job_type=JobType.EXPORT,
                initial_stage=JobStage.ANALYZING,
                request_key=request_key,
                supersession_key=f"mural-build:{project_id}",
                work=self.make_work(
                    project_id=project_id,
                    request=request,
                    request_key=request_key,
                    materials=materials,
                    settings=settings,
                ),
            )
        return MuralBuildStartResponse(job=job, cache_hit=False)

    def make_work(
        self,
        *,
        project_id: str,
        request: StartMuralBuildRequest,
        request_key: str,
        materials: tuple[BambuMaterial, ...],
        settings: BambuProjectSettings,
    ) -> Callable[[JobContext], WorkerResult]:
        def work(context: JobContext) -> WorkerResult:
            context.report(stage=JobStage.ANALYZING, progress=0.06)
            try:
                inputs = self._load_inputs(project_id, request)
            except (InvalidMuralBuildRequestError, MuralBuildArtifactUnavailableError) as error:
                raise _job_error("mural_source_stale", str(error), retryable=False) from error

            plan = inputs.plan.plan
            source = inputs.plan.request.source
            vector_sha = inputs.document.capabilities.vector_geometry.artifact_sha256
            if vector_sha is None:
                raise _job_error(
                    "mural_geometry_incomplete",
                    "Geometry IR lacks exact vector or topology provenance.",
                    retryable=False,
                )
            context.check_canceled()
            master_width = plan.master_size_mm.width
            master_height = plan.master_size_mm.height
            construction_paths = (_construction_rectangle(master_width, master_height),)
            master_topology = build_shared_boundary_topology(
                inputs.labels,
                source_asset_sha256=source.source_asset_sha256,
                source_labels=inputs.document.source_labels,
                materials=inputs.document.materials,
                base=RectangleBase.create(
                    center=Point2(x_mm=master_width / 2, y_mm=master_height / 2),
                    width_mm=master_width,
                    height_mm=master_height,
                ),
                canvas_width_mm=master_width,
                canvas_height_mm=master_height,
                vector_paths=construction_paths,
                vector_artifact_sha256=vector_sha,
                palette_color_order=inputs.document.palette_color_order,
            )
            label_partition = partition_master_labels(
                inputs.labels,
                inputs.plan.request,
                plan,
                authoritative_vector_sha256=vector_sha,
                authoritative_topology_sha256=master_topology.topology_artifact_sha256,
            )
            topology_partition = partition_master_topology(label_partition, master_topology)
            seam_qa = build_mural_seam_qa(label_partition, topology_partition, plan)
            if seam_qa.status == QaStatus.FAIL:
                raise _job_error(
                    "mural_seam_qa_failed",
                    "Exact mural seam/topology QA failed.",
                    retryable=False,
                )

            assembly_manifest = None
            assembly_sheet_payload = None
            if request.assembly_aids.enabled:
                assembly_manifest, assembly_sheet_payload = build_mural_assembly_aids(
                    title=request.name,
                    request=inputs.plan.request,
                    plan=plan,
                    seam_qa=seam_qa,
                    topology=topology_partition,
                    settings=request.assembly_aids,
                    total_panel_thickness_mm=(inputs.base_thickness_mm + inputs.art_thickness_mm),
                )

            context.check_canceled()
            context.report(stage=JobStage.MESHING, progress=0.34)
            plates = _build_plates(
                topology_partition,
                inputs,
                materials=materials,
                settings=settings,
                cancellation=context.check_canceled,
            )
            reserve = _prime_tower_reserve(inputs.plan)
            context.report(stage=JobStage.PACKAGING, progress=0.58)
            package_payload = build_bambu_mural_3mf(
                name=request.name,
                plates=plates,
                materials=materials,
                settings=settings,
                prime_tower_reserve=reserve,
            )
            package = read_bambu_mural_3mf(
                package_payload,
                expected_plate_provenance={
                    item.tile_id: item.source_geometry_fingerprint for item in plates
                },
            )
            package_blob = context.blob_store.put_bytes(
                package_payload,
                namespace="artifacts",
                extension=".3mf",
                media_type="model/3mf",
            )

            context.check_canceled()
            context.report(stage=JobStage.SLICING, progress=0.76)
            validation, validation_reason, validation_attempted = self._validate(
                context,
                package_blob,
                plates=plates,
                materials=materials,
                settings=settings,
                name=request.name,
            )
            context.report(stage=JobStage.VALIDATING, progress=0.92)
            validation_payload = _json_bytes(
                {
                    "schema_version": 1,
                    "status": validation.status.value,
                    "attempted": validation_attempted,
                    "reason": validation_reason,
                    "source_geometry_artifact_id": inputs.geometry.id,
                    "source_geometry_sha256": inputs.geometry.sha256,
                    "plan_generation": inputs.plan.generation,
                    "request_fingerprint": plan.request_fingerprint,
                    "source_fingerprint": plan.source_fingerprint,
                    "manifest_sha256": package.manifest_sha256,
                    "result": _jsonable(validation),
                    "slicer_report": (
                        validation.report.as_contract() if validation.report is not None else None
                    ),
                }
            )
            log_payload = _validation_log(validation, validation_reason).encode("utf-8")
            provenance = {
                "mural_build_schema_version": 1,
                "adapter_version": MURAL_BUILD_ADAPTER_VERSION,
                "project_id": project_id,
                "plan_generation": inputs.plan.generation,
                "request_fingerprint": plan.request_fingerprint,
                "source_fingerprint": plan.source_fingerprint,
                "source_asset_id": source.source_asset_id,
                "source_asset_sha256": source.source_asset_sha256,
                "processed_artifact_id": source.processed_artifact_id,
                "processed_artifact_sha256": source.processed_artifact_sha256,
                "geometry_artifact_id": inputs.geometry.id,
                "geometry_sha256": inputs.geometry.sha256,
                "geometry_fingerprint": inputs.document.fingerprint(),
                "label_partition_sha256": label_partition.manifest.partition_sha256,
                "topology_partition_sha256": topology_partition.manifest.partition_sha256,
                "manifest_sha256": package.manifest_sha256,
                "validation_status": validation.status.value,
                "assembly_aids_enabled": request.assembly_aids.enabled,
                "assembly_aids_manifest_sha256": (
                    assembly_manifest.manifest_sha256 if assembly_manifest is not None else None
                ),
            }
            artifacts = [
                WorkerArtifact(
                    kind="bambu-mural-3mf",
                    derivation_key=request_key,
                    blob=package_blob,
                    metadata=provenance,
                ),
                _json_artifact(
                    context,
                    kind="mural-label-partition",
                    derivation_key=request_key,
                    payload=label_partition.manifest.model_dump(mode="json"),
                    metadata=provenance,
                ),
                _json_artifact(
                    context,
                    kind="mural-topology-partition",
                    derivation_key=request_key,
                    payload=topology_partition.manifest.model_dump(mode="json"),
                    metadata=provenance,
                ),
                _json_artifact(
                    context,
                    kind="mural-seam-qa",
                    derivation_key=request_key,
                    payload=seam_qa.model_dump(mode="json"),
                    metadata={**provenance, "qa_status": seam_qa.status.value},
                ),
                _bytes_artifact(
                    context,
                    kind="bambu-mural-validation-report",
                    derivation_key=request_key,
                    payload=validation_payload,
                    extension=".json",
                    media_type="application/json",
                    metadata=provenance,
                ),
                _bytes_artifact(
                    context,
                    kind="bambu-mural-validation-log",
                    derivation_key=request_key,
                    payload=log_payload,
                    extension=".log",
                    media_type="text/plain",
                    metadata=provenance,
                ),
            ]
            if assembly_manifest is not None and assembly_sheet_payload is not None:
                artifacts.extend(
                    (
                        _json_artifact(
                            context,
                            kind="mural-assembly-aids",
                            derivation_key=request_key,
                            payload=assembly_manifest.model_dump(mode="json"),
                            metadata=provenance,
                        ),
                        _bytes_artifact(
                            context,
                            kind="mural-assembly-sheet",
                            derivation_key=request_key,
                            payload=assembly_sheet_payload,
                            extension=".svg",
                            media_type="image/svg+xml",
                            metadata=provenance,
                        ),
                    )
                )
            context.check_canceled()
            return WorkerResult(artifacts=tuple(artifacts))

        return work

    def result(self, *, project_id: str, job_id: str) -> MuralBuildResult:
        connection = open_database(self.database_path)
        try:
            ProjectRepository(connection).get(project_id)
            job = JobRepository(connection).get(job_id)
            if (
                job.project_id != project_id
                or job.type != JobType.EXPORT
                or job.supersession_key != f"mural-build:{project_id}"
            ):
                raise RecordNotFoundError(f"mural build job not found: {job_id}")
            records = ArtifactRepository(connection).list_for_job(job_id)
            plan_repository = MuralPlanRepository(connection, self.blob_store)
            plan = plan_repository.get(project_id)
            source_stale_reason = None
            if plan is not None:
                try:
                    plan_repository.provenance.validate(project_id, plan.request)
                except InvalidMuralPlanSourceError as error:
                    source_stale_reason = str(error)
        finally:
            connection.close()
        resources = tuple(self._resource(item, project_id=project_id) for item in records)
        by_kind = {item.kind: item for item in resources}
        if job.state == JobState.SUCCEEDED and not _valid_artifact_kind_set(set(by_kind)):
            raise MuralBuildArtifactUnavailableError(
                "Successful mural build artifacts are incomplete or unexpected."
            )
        package = by_kind.get("bambu-mural-3mf")
        stale_reason = source_stale_reason or _stale_reason(plan, package)
        validation = self._validation_resource(by_kind.get("bambu-mural-validation-report"))
        validation_acceptable = validation.status == "validated"
        return MuralBuildResult(
            job=job,
            freshness="stale" if stale_reason else "current",
            stale_reason=stale_reason,
            cache_hit=bool(package and package.metadata.get("cache_hit", False)),
            artifacts=resources,
            package=package,
            label_partition=by_kind.get("mural-label-partition"),
            topology_partition=by_kind.get("mural-topology-partition"),
            seam_qa=by_kind.get("mural-seam-qa"),
            validation_report=by_kind.get("bambu-mural-validation-report"),
            validation_log=by_kind.get("bambu-mural-validation-log"),
            assembly_aids=by_kind.get("mural-assembly-aids"),
            assembly_sheet=by_kind.get("mural-assembly-sheet"),
            validation=validation,
            download_ready=(
                job.state == JobState.SUCCEEDED
                and stale_reason is None
                and _valid_artifact_kind_set(set(by_kind))
                and validation_acceptable
            ),
        )

    def _load_inputs(self, project_id: str, request: StartMuralBuildRequest) -> _BuildInputs:
        connection = open_database(self.database_path)
        try:
            ProjectRepository(connection).get(project_id)
            plans = MuralPlanRepository(connection, self.blob_store)
            plan = plans.get(project_id)
            if plan is None:
                raise RecordNotFoundError(f"mural plan not found: {project_id}")
            if (
                plan.generation != request.expected_plan_generation
                or plan.plan.request_fingerprint != request.expected_request_fingerprint
            ):
                raise InvalidMuralBuildRequestError(
                    "The saved mural plan changed; reload it before building.",
                    current_plan_generation=plan.generation,
                    current_request_fingerprint=plan.plan.request_fingerprint,
                )
            plans.provenance.validate(project_id, plan.request)
            if not plan.plan.all_tiles_fit:
                raise InvalidMuralBuildRequestError(
                    "Every mural tile must fit the selected bed before packaging.",
                    current_plan_generation=plan.generation,
                    current_request_fingerprint=plan.plan.request_fingerprint,
                )
            _prime_tower_reserve(plan)
            artifact_repo = ArtifactRepository(connection)
            geometry = artifact_repo.get(request.geometry_artifact_id)
            if artifact_repo.owner_project_id(geometry.id) != project_id:
                raise RecordNotFoundError(f"geometry artifact not found: {geometry.id}")
            if geometry.kind != "geometry-ir" or geometry.sha256 != request.geometry_sha256:
                raise InvalidMuralBuildRequestError(
                    "The selected artifact is not the exact requested Geometry IR.",
                    current_plan_generation=plan.generation,
                    current_request_fingerprint=plan.plan.request_fingerprint,
                )
            labels_record = _matching_labels(connection, geometry, plan)
            config = _source_config(connection, project_id, plan)
        finally:
            connection.close()
        geometry_payload = self._verified_bytes(geometry)
        labels_payload = self._verified_bytes(labels_record)
        try:
            document = load_geometry_ir(geometry_payload)
        except GeometryIRDecodeError as error:
            raise InvalidMuralBuildRequestError(f"Geometry IR is invalid: {error}") from error
        source = plan.request.source
        if (
            document.source_asset_sha256 != source.source_asset_sha256
            or document.processed_labels_sha256 != labels_record.sha256
            or document.source_width_px != source.processed_size.width
            or document.source_height_px != source.processed_size.height
        ):
            raise InvalidMuralBuildRequestError(
                "Geometry IR does not describe the saved mural master.",
                current_plan_generation=plan.generation,
                current_request_fingerprint=plan.plan.request_fingerprint,
            )
        if geometry.metadata.get("config_fingerprint") != source.config_sha256:
            raise InvalidMuralBuildRequestError(
                "Geometry IR configuration does not match the saved mural plan.",
                current_plan_generation=plan.generation,
                current_request_fingerprint=plan.plan.request_fingerprint,
            )
        if (
            document.capabilities.mesh.status != CapabilityStatus.AVAILABLE
            or document.capabilities.vector_geometry.status != CapabilityStatus.AVAILABLE
            or document.capabilities.topology.status != CapabilityStatus.AVAILABLE
        ):
            raise InvalidMuralBuildRequestError("Geometry IR is not complete enough for a mural.")
        width = int(labels_record.metadata.get("width", 0))
        height = int(labels_record.metadata.get("height", 0))
        if (width, height) != (source.processed_size.width, source.processed_size.height):
            raise InvalidMuralBuildRequestError("Processed labels have stale dimensions.")
        labels = LabelField(
            width=width,
            height=height,
            label_values=tuple(range(len(config.palette.colors))),
            pixels=labels_payload,
        )
        if hashlib.sha256(labels.pixels).hexdigest() != document.processed_labels_sha256:
            raise InvalidMuralBuildRequestError("Processed labels do not match Geometry IR.")
        if (
            request.profile.nozzle_diameter_mm != config.printer.nozzle_mm
            or request.profile.layer_height_mm != config.printer.layer_height_mm
        ):
            raise InvalidMuralBuildRequestError(
                "Mural build profile must match the master geometry nozzle and layer height."
            )
        return _BuildInputs(
            plan=plan,
            document=document,
            geometry=geometry,
            labels=labels,
            base_thickness_mm=config.geometry.base_thickness_mm,
            art_thickness_mm=config.geometry.art_thickness_mm,
        )

    def _verified_bytes(self, artifact: ArtifactRecord) -> bytes:
        blob = _stored_blob(artifact)
        try:
            if not self.blob_store.verify(blob):
                raise MuralBuildArtifactUnavailableError(
                    f"{artifact.kind} artifact failed content verification"
                )
            payload = self.blob_store.path_for(artifact.relative_path).read_bytes()
        except (FileNotFoundError, OSError) as error:
            raise MuralBuildArtifactUnavailableError(
                f"{artifact.kind} artifact is missing or unreadable"
            ) from error
        if hashlib.sha256(payload).hexdigest() != artifact.sha256:
            raise MuralBuildArtifactUnavailableError(
                f"{artifact.kind} artifact digest changed while reading"
            )
        return payload

    def _validate(
        self,
        context: JobContext,
        package_blob: StoredBlob,
        *,
        plates: tuple[MuralBuildPlate, ...],
        materials: tuple[BambuMaterial, ...],
        settings: BambuProjectSettings,
        name: str,
    ) -> tuple[BambuValidationResult, Optional[str], bool]:
        project = BambuProject(
            name=name,
            meshes=tuple(mesh for plate in plates for mesh in plate.meshes),
            materials=materials,
            settings=settings,
        )
        try:
            profiles = self.profile_provider(project)
            result = self.validator.validate(
                context.blob_store.path_for(package_blob.relative_path),
                profiles=profiles,
                expectation=SlicerValidationExpectation(
                    expected_colors=_used_material_colors(plates, materials)
                ),
                cancellation=context.cancellation,
            )
            return result, None, True
        except BambuStudioCanceledError as error:
            raise WorkerCanceledError("Bambu Studio mural validation was canceled") from error
        except BambuStudioUnavailableError as error:
            return error.result, str(error), True
        except BambuStudioTimeoutError as error:
            return error.result, str(error), True
        except BambuStudioProfileError as error:
            return error.result, str(error), True
        except BambuStudioSliceError as error:
            return error.result, str(error), True
        except InvalidSlicedArtifactError as error:
            return error.result, str(error), True
        except (BambuProfileError, FileNotFoundError, OSError, ValueError) as error:
            return _validation_placeholder(BambuValidationStatus.PROFILE_ERROR), str(error), False

    def _cached_or_active(self, project_id: str, request_key: str):
        connection = open_database(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE project_id = ? AND job_type = 'export' AND request_key = ?
                  AND supersession_key = ? AND state IN ('queued', 'running', 'succeeded')
                ORDER BY CASE state WHEN 'succeeded' THEN 0 ELSE 1 END, created_at DESC, id DESC
                """,
                (project_id, request_key, f"mural-build:{project_id}"),
            ).fetchall()
            jobs = JobRepository(connection)
            artifacts = ArtifactRepository(connection)
            for row in rows:
                job = jobs.get(row["id"])
                if job.state in {JobState.QUEUED, JobState.RUNNING}:
                    return job, False
                records = artifacts.list_for_job(job.id)
                if not _valid_artifact_kind_set({item.kind for item in records}):
                    continue
                if all(
                    item.derivation_key == request_key and self._record_valid(item)
                    for item in records
                ) and self._cached_validation_passed(records):
                    return job, True
        finally:
            connection.close()
        return None

    def _record_valid(self, artifact: ArtifactRecord) -> bool:
        try:
            return self.blob_store.verify(_stored_blob(artifact))
        except (FileNotFoundError, OSError):
            return False

    def _cached_validation_passed(self, artifacts: tuple[ArtifactRecord, ...]) -> bool:
        reports = tuple(item for item in artifacts if item.kind == "bambu-mural-validation-report")
        if len(reports) != 1:
            return False
        try:
            payload = self._verified_bytes(reports[0])
            report = json.loads(payload)
        except (MuralBuildArtifactUnavailableError, json.JSONDecodeError, TypeError):
            return False
        return isinstance(report, dict) and report.get("status") == "validated"

    def _resource(self, artifact: ArtifactRecord, *, project_id: str) -> ArtifactResource:
        if not self._record_valid(artifact):
            raise MuralBuildArtifactUnavailableError(
                f"Published {artifact.kind} artifact is missing or corrupt."
            )
        return ArtifactResource(
            id=artifact.id,
            job_id=artifact.job_id,
            revision_id=artifact.revision_id,
            kind=artifact.kind,
            sha256=artifact.sha256,
            derivation_key=artifact.derivation_key,
            media_type=artifact.media_type,
            byte_size=artifact.byte_size,
            metadata=dict(artifact.metadata),
            download_url=f"/api/projects/{project_id}/artifacts/{artifact.id}",
            created_at=artifact.created_at,
        )

    def _validation_resource(self, artifact: Optional[ArtifactResource]) -> MuralValidationResource:
        if artifact is None:
            return MuralValidationResource(status="not_run", attempted=False)
        record = json.loads(self.blob_store.path_for(_artifact_path(artifact, self)).read_text())
        result = record.get("result") or {}
        return MuralValidationResource(
            status=record.get("status", "not_run"),
            attempted=bool(record.get("attempted", False)),
            reason=record.get("reason"),
            executable=result.get("executable"),
            version=result.get("version"),
        )

    def _installed_profiles(self, project: BambuProject) -> BambuSliceProfiles:
        if self.bambu_resources_root is None:
            raise BambuProfileError("Bambu Studio Resources root is not configured")
        resolved = resolve_p2s_profile_set(
            profile_plan_for_project(project), self.bambu_resources_root
        )

        def pin(profile) -> PinnedBambuProfile:
            blob = self.blob_store.put_bytes(
                profile.payload,
                namespace="cache",
                extension=".json",
                media_type="application/json",
            )
            return PinnedBambuProfile(
                path=self.blob_store.path_for(blob.relative_path), sha256=profile.sha256
            )

        return BambuSliceProfiles(
            machine=pin(resolved.machine),
            process=pin(resolved.process),
            filaments=tuple(pin(item) for item in resolved.filaments),
        )


def _matching_labels(connection, geometry: ArtifactRecord, plan: MuralPlanRecord) -> ArtifactRecord:
    preview_job_id = geometry.metadata.get("preview_job_id")
    source = plan.request.source
    processed = ArtifactRepository(connection).get(source.processed_artifact_id)
    if processed.job_id is not None:
        if not isinstance(preview_job_id, str) or preview_job_id != processed.job_id:
            raise InvalidMuralBuildRequestError(
                "Geometry IR was not produced from the mural plan's exact preview job."
            )
        candidates = ArtifactRepository(connection).list_for_job(processed.job_id)
    elif processed.revision_id is not None:
        if geometry.revision_id != processed.revision_id:
            raise InvalidMuralBuildRequestError(
                "Geometry IR was not published with the mural plan's exact revision."
            )
        candidates = ArtifactRepository(connection).list_for_revision(processed.revision_id)
    else:  # pragma: no cover - mural provenance rejects ownerless artifacts first
        candidates = ()
    matches = tuple(
        item
        for item in candidates
        if item.kind == "processed-labels" and item.derivation_key == processed.derivation_key
    )
    if len(matches) != 1:
        raise InvalidMuralBuildRequestError(
            "The mural preview does not retain exactly one processed-label artifact."
        )
    return matches[0]


def _source_config(connection, project_id: str, plan: MuralPlanRecord):
    source = plan.request.source
    if source.revision_id is not None:
        revision = RevisionRepository(connection).get(source.revision_id)
        if revision.project_id != project_id:
            raise RecordNotFoundError(f"revision not found: {source.revision_id}")
        from image23mf.contracts.job import load_job_config

        return load_job_config(dict(revision.config))
    draft = DraftRepository(connection).get(project_id)
    if draft is None or draft.generation != source.draft_generation:
        raise InvalidMuralBuildRequestError("The mural draft changed before packaging.")
    return DraftRepository(connection).load_config(project_id)


def _bambu_materials(
    document: GeometryDocument, request: StartMuralBuildRequest
) -> tuple[BambuMaterial, ...]:
    by_id = {item.id: item for item in document.materials}
    if request.material_mapping:
        mapping = request.material_mapping
        if {item.material_id for item in mapping} != set(by_id):
            raise InvalidMuralBuildRequestError(
                "Material mapping must exactly cover every Geometry IR material."
            )
    else:
        by_palette = {item.palette_color_id: item for item in document.materials}
        ordered = tuple(by_palette[item] for item in document.palette_color_order)
        from image23mf.contracts.exporting import ExportMaterialMapping

        mapping = tuple(
            ExportMaterialMapping(
                material_id=item.id,
                extruder=index,
                preset="Bambu PLA Matte",
            )
            for index, item in enumerate(ordered, start=1)
        )
    return tuple(
        BambuMaterial(
            name=by_id[item.material_id].name,
            color=by_id[item.material_id].color_hex,
            extruder=item.extruder,
            filament_type=item.filament_type,
            preset=item.preset,
            material_id=item.material_id,
            palette_color_id=by_id[item.material_id].palette_color_id,
            filament_id=by_id[item.material_id].filament_id,
        )
        for item in mapping
    )


def _settings(request: StartMuralBuildRequest) -> BambuProjectSettings:
    return BambuProjectSettings(
        printer_model=request.profile.printer_model,
        nozzle_diameter=request.profile.nozzle_diameter_mm,
        layer_height=request.profile.layer_height_mm,
        bed_type=request.profile.bed_type,
    )


def _build_plates(
    topology_partition,
    inputs: _BuildInputs,
    *,
    materials: tuple[BambuMaterial, ...],
    settings: BambuProjectSettings,
    cancellation: Callable[[], None],
) -> tuple[MuralBuildPlate, ...]:
    material_index = {item.material_id: index for index, item in enumerate(materials)}
    plan_tiles = {item.id: item for item in inputs.plan.plan.tiles}
    result = []
    for tile in topology_partition.tiles:
        cancellation()
        document = tile.topology.document
        base_material = next(
            item
            for item in document.materials
            if item.palette_color_id == inputs.document.palette_color_order[0]
        )
        base_layers = round(inputs.base_thickness_mm / settings.layer_height)
        base = generate_structural_base(
            document.base,
            material=base_material,
            options=BaseMeshOptions(
                thickness_mm=inputs.base_thickness_mm,
                layer_height_mm=settings.layer_height,
                minimum_layers=base_layers,
            ),
            build_bounds=BaseBuildBounds(width_mm=256, depth_mm=256),
        )
        art_layers = round(inputs.art_thickness_mm / settings.layer_height)
        artwork = extrude_artwork_regions(
            document,
            FlushInlayStrategy(
                base_top_z_mm=base.thickness_mm,
                layer_height_mm=settings.layer_height,
                artwork_layers=art_layers,
            ),
        )
        meshed = assemble_mesh_document(
            document,
            base_mesh=base.mesh,
            base_part=base.part,
            artwork=artwork,
        ).document
        mesh_by_id = {item.id: item for item in meshed.meshes}
        plan_tile = plan_tiles[tile.evidence.tile_id]
        bambu_meshes = tuple(
            _bambu_mesh(
                f"{tile.evidence.tile_id} - {part.name}",
                mesh_by_id[part.mesh_id],
                material_index[part.material_id],
                rotation_degrees=plan_tile.bed_fit.rotation_degrees,
                source_width_mm=document.canvas_width_mm,
                source_height_mm=document.canvas_height_mm,
            )
            for part in meshed.parts
        )
        result.append(
            MuralBuildPlate(
                tile_id=tile.evidence.tile_id,
                row=plan_tile.row,
                column=plan_tile.column,
                build_plate_index=plan_tile.build_plate_index,
                name=(
                    f"{plan_tile.build_plate_index:02d} - Row {plan_tile.row}, "
                    f"Column {plan_tile.column}"
                ),
                meshes=bambu_meshes,
                origin_x_mm=plan_tile.bed_fit.origin_x_mm or 0,
                origin_y_mm=plan_tile.bed_fit.origin_y_mm or 0,
                source_geometry_fingerprint=tile.evidence.tile_geometry_fingerprint,
            )
        )
    return tuple(result)


def _bambu_mesh(
    name,
    mesh,
    material_index: int,
    *,
    rotation_degrees: int,
    source_width_mm: float,
    source_height_mm: float,
) -> BambuMesh:
    del source_width_mm
    if rotation_degrees == 90:
        vertices = tuple(
            (source_height_mm - item.y_mm, item.x_mm, item.z_mm) for item in mesh.vertices
        )
    else:
        vertices = tuple((item.x_mm, item.y_mm, item.z_mm) for item in mesh.vertices)
    return BambuMesh(
        name=name,
        vertices=vertices,
        triangles=tuple(item.vertices for item in mesh.triangles),
        material_index=material_index,
    )


def _construction_rectangle(width_mm: float, height_mm: float) -> Path2D:
    origin = Point2(x_mm=0, y_mm=0)
    return Path2D.create(
        purpose="construction",
        start=origin,
        segments=(
            LineSegment(end=Point2(x_mm=width_mm, y_mm=0)),
            LineSegment(end=Point2(x_mm=width_mm, y_mm=height_mm)),
            LineSegment(end=Point2(x_mm=0, y_mm=height_mm)),
            LineSegment(end=origin),
        ),
        closed=True,
    )


def _used_material_colors(
    plates: tuple[MuralBuildPlate, ...], materials: tuple[BambuMaterial, ...]
) -> tuple[str, ...]:
    used = {mesh.material_index for plate in plates for mesh in plate.meshes}
    return tuple(material.color for index, material in enumerate(materials) if index in used)


def _prime_tower_reserve(plan: MuralPlanRecord) -> PrimeTowerReserve:
    rectangles = plan.request.reserved_rectangles
    if len(rectangles) != 1:
        raise InvalidMuralBuildRequestError(
            "Mural packaging requires exactly one saved prime-tower reserve rectangle."
        )
    item = rectangles[0]
    inner_width = item.width_mm - 2 * PRIME_TOWER_EDGE_MARGIN_MM
    inner_height = item.height_mm - 2 * PRIME_TOWER_EDGE_MARGIN_MM
    tower_width = min(35.0, inner_width, inner_height)
    if tower_width < MINIMUM_PRIME_TOWER_WIDTH_MM:
        raise InvalidMuralBuildRequestError(
            "Prime-tower reserve must leave room for the tower and its 3 mm edge margin."
        )
    return PrimeTowerReserve(
        x_mm=item.x_mm,
        y_mm=item.y_mm,
        width_mm=item.width_mm,
        height_mm=item.height_mm,
        tower_width_mm=tower_width,
        tower_x_mm=item.x_mm + PRIME_TOWER_EDGE_MARGIN_MM,
        tower_y_mm=item.y_mm + PRIME_TOWER_EDGE_MARGIN_MM,
    )


def _request_key(project_id: str, request: StartMuralBuildRequest, plan: MuralPlanRecord) -> str:
    return hashlib.sha256(
        _json_bytes(
            {
                "adapter_version": MURAL_BUILD_ADAPTER_VERSION,
                "engine_version": __version__,
                "pipeline": "mural-build",
                "project_id": project_id,
                "plan_generation": plan.generation,
                "request_fingerprint": plan.plan.request_fingerprint,
                "source_fingerprint": plan.plan.source_fingerprint,
                "request": request.model_dump(mode="json"),
            }
        )
    ).hexdigest()


def _stale_reason(
    plan: Optional[MuralPlanRecord], package: Optional[ArtifactResource]
) -> Optional[str]:
    if package is None:
        return None
    if plan is None:
        return "The saved mural plan was deleted after this build."
    metadata = package.metadata
    if metadata.get("plan_generation") != plan.generation:
        return "The saved mural plan generation changed after this build."
    if metadata.get("request_fingerprint") != plan.plan.request_fingerprint:
        return "The saved mural layout changed after this build."
    if metadata.get("source_fingerprint") != plan.plan.source_fingerprint:
        return "The processed mural master changed after this build."
    return None


def _stored_blob(artifact: ArtifactRecord) -> StoredBlob:
    return StoredBlob(
        sha256=artifact.sha256,
        relative_path=artifact.relative_path,
        byte_size=artifact.byte_size,
        media_type=artifact.media_type,
        extension=Path(artifact.relative_path).suffix,
    )


def _artifact_path(artifact: ArtifactResource, service: MuralBuildService) -> str:
    connection = open_database(service.database_path)
    try:
        return ArtifactRepository(connection).get(artifact.id).relative_path
    finally:
        connection.close()


def _json_artifact(
    context: JobContext,
    *,
    kind: str,
    derivation_key: str,
    payload: Any,
    metadata: dict[str, Any],
) -> WorkerArtifact:
    return _bytes_artifact(
        context,
        kind=kind,
        derivation_key=derivation_key,
        payload=_json_bytes(payload),
        extension=".json",
        media_type="application/json",
        metadata=metadata,
    )


def _bytes_artifact(
    context: JobContext,
    *,
    kind: str,
    derivation_key: str,
    payload: bytes,
    extension: str,
    media_type: str,
    metadata: dict[str, Any],
) -> WorkerArtifact:
    return WorkerArtifact(
        kind=kind,
        derivation_key=derivation_key,
        blob=context.blob_store.put_bytes(
            payload,
            namespace="artifacts",
            extension=extension,
            media_type=media_type,
        ),
        metadata=metadata,
    )


def _validation_placeholder(status: BambuValidationStatus) -> BambuValidationResult:
    return BambuValidationResult(
        status=status,
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


def _validation_log(result: BambuValidationResult, reason: Optional[str]) -> str:
    return "\n".join(
        (
            f"status={result.status.value}",
            f"reason={reason or ''}",
            f"executable={result.executable or ''}",
            f"version={result.version or ''}",
            f"command={' '.join(result.command)}",
            "stdout:",
            result.stdout,
            "stderr:",
            result.stderr,
        )
    )


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _job_error(code: str, message: str, *, retryable: bool) -> WorkerJobError:
    return WorkerJobError(
        JobFailure(
            code=code,
            message=message,
            retryable=retryable,
            details={"action": "Reload the mural plan and rebuild from current geometry."},
        )
    )
