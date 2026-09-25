"""Durable, cancelable geometry-to-Bambu export orchestration."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import dataclasses
import hashlib
import json
import tempfile
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from image23mf import __version__
from image23mf.bambu import (
    Bambu3MFError,
    BambuProfileError,
    BambuProject,
    BambuProjectSettings,
    build_bambu_3mf,
    profile_plan_for_project,
    read_bambu_3mf,
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
from image23mf.bambu.placement import single_plate_placement
from image23mf.bambu.preview import render_sliced_preview
from image23mf.bambu.report import SlicerValidationExpectation
from image23mf.contracts.exporting import (
    ExportJobResult,
    ExportMaterialMapping,
    ExportStartResponse,
    StartExportRequest,
)
from image23mf.contracts.jobs import JobFailure, JobStage, JobState, JobType
from image23mf.contracts.processing import ArtifactResource
from image23mf.derivation import DerivationIdentity
from image23mf.geometry import (
    BaseBuildBounds,
    CapabilityStatus,
    GeometryDocument,
    MeshQualityOptions,
    load_geometry_ir,
    validate_geometry_quality,
)
from image23mf.geometry.serialization import GeometryIRDecodeError
from image23mf.storage import (
    ArtifactRecord,
    ArtifactRepository,
    ContentAddressedStore,
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

EXPORT_ARTIFACT_KINDS = (
    "bambu-3mf",
    "bambu-validation-log",
    "bambu-validation-report",
    "geometry-quality-report",
)
# Versioned export settings and sliced-preview rendering invalidate cached exports.
EXPORT_ADAPTER_VERSION = 7
OPTIONAL_EXPORT_ARTIFACT_KINDS = {"bambu-slicer-preview-image"}


def _complete_export_bundle(kinds: set[str]) -> bool:
    return (
        set(EXPORT_ARTIFACT_KINDS)
        <= kinds
        <= (set(EXPORT_ARTIFACT_KINDS) | OPTIONAL_EXPORT_ARTIFACT_KINDS)
    )


class InvalidExportRequestError(ValueError):
    """A requested export does not match its immutable source evidence."""


class ExportArtifactUnavailableError(RuntimeError):
    """A referenced input or successful output artifact is missing or corrupt."""


ProfileProvider = Callable[[BambuProject], BambuSliceProfiles]


class ExportService:
    """Validate, schedule, cache, and read durable export jobs."""

    def __init__(
        self,
        *,
        database_path: Path,
        blob_store: ContentAddressedStore,
        bambu_resources_root: Optional[Path] = None,
        validator: Optional[BambuStudioCliValidator] = None,
        profile_provider: Optional[ProfileProvider] = None,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.blob_store = blob_store
        self.bambu_resources_root = (
            None if bambu_resources_root is None else bambu_resources_root.expanduser().resolve()
        )
        self.validator = validator or BambuStudioCliValidator()
        self.profile_provider = profile_provider or self._installed_profiles
        self._request_guard = threading.RLock()

    def start_export(
        self,
        *,
        project_id: str,
        request: StartExportRequest,
        manager: LocalWorkerManager,
    ) -> ExportStartResponse:
        # Synchronous validation provides a stable 404/409 before a durable job is created.
        document = self._load_source(project_id, request)
        try:
            project = _bambu_project(document, request)
            profiles = self.profile_provider(project)
        except (Bambu3MFError, BambuProfileError, ValueError) as error:
            raise InvalidExportRequestError(
                f"The export does not match a measured P2S profile: {error}"
            ) from error
        connection = open_database(self.database_path)
        try:
            source_record = ArtifactRepository(connection).get(request.geometry_artifact_id)
        finally:
            connection.close()
        derivation = export_derivation_identity(
            project_id,
            request,
            upstream_derivation_key=source_record.derivation_key,
            profile_fingerprints=_profile_fingerprints(profiles),
        )
        request_key = derivation.key()
        with self._request_guard:
            cached = self._cached_or_active(project_id, request, request_key)
            if cached is not None:
                return ExportStartResponse(job=cached)
            job = manager.submit(
                project_id=project_id,
                revision_id=request.revision_id,
                job_type=JobType.EXPORT,
                initial_stage=JobStage.VALIDATING,
                request_key=request_key,
                supersession_key=f"export:{project_id}",
                work=self.make_work(
                    project_id=project_id,
                    request=request,
                    request_key=request_key,
                    profiles=profiles,
                    derivation=derivation,
                ),
            )
        return ExportStartResponse(job=job)

    def make_work(
        self,
        *,
        project_id: str,
        request: StartExportRequest,
        request_key: str,
        profiles: Optional[BambuSliceProfiles] = None,
        derivation: Optional[DerivationIdentity] = None,
    ) -> Callable[[JobContext], WorkerResult]:
        def work(context: JobContext) -> WorkerResult:
            context.report(stage=JobStage.VALIDATING, progress=0.08)
            try:
                document = self._load_source(project_id, request)
            except RecordNotFoundError as error:
                raise _job_error(
                    "export_source_missing",
                    "The project, revision, or geometry artifact no longer exists.",
                    retryable=False,
                    action="Regenerate geometry from the current revision.",
                ) from error
            except (InvalidExportRequestError, ExportArtifactUnavailableError) as error:
                raise _job_error(
                    "export_source_invalid",
                    str(error),
                    retryable=False,
                    action="Regenerate and select the verified geometry artifact.",
                ) from error

            quality = validate_geometry_quality(
                document,
                MeshQualityOptions(
                    build_bounds=BaseBuildBounds(width_mm=256, depth_mm=256),
                    minimum_part_thickness_mm=request.minimum_part_thickness_mm,
                ),
            )
            if not quality.safe_for_export:
                raise WorkerJobError(
                    JobFailure(
                        code="geometry_quality_failed",
                        message="Geometry did not pass the printable mesh quality gate.",
                        retryable=False,
                        details={
                            "action": "Inspect the affected regions and regenerate geometry.",
                            "affected_source_geometry_ids": list(
                                quality.affected_source_geometry_ids()
                            ),
                            "finding_codes": sorted({item.code for item in quality.findings}),
                            "quality_fingerprint": quality.fingerprint,
                        },
                    )
                )
            context.check_canceled()
            context.report(stage=JobStage.PACKAGING, progress=0.34)
            try:
                project = _bambu_project(document, request)
                package_bytes = build_bambu_3mf(
                    name=project.name,
                    meshes=project.meshes,
                    materials=project.materials,
                    settings=project.settings,
                )
                # The package reader is an independent structural round-trip gate.
                read_bambu_3mf(package_bytes)
                resolved_profiles = profiles or self.profile_provider(project)
            except (Bambu3MFError, BambuProfileError, ValueError) as error:
                raise _job_error(
                    "slicer_profile_error",
                    f"The export does not match a measured P2S profile: {error}",
                    retryable=False,
                    action="Use the supported P2S nozzle, layer, plate, and PLA presets.",
                ) from error
            package_blob = context.blob_store.put_bytes(
                package_bytes,
                namespace="artifacts",
                extension=".3mf",
                media_type="model/3mf",
            )
            context.check_canceled()
            context.report(stage=JobStage.SLICING, progress=0.64)
            sliced_preview_payload = None
            sliced_preview_metadata = {}
            try:
                with tempfile.TemporaryDirectory(prefix="playered-export-") as directory:
                    prepared_path = Path(directory) / "project.3mf"
                    sliced_path = Path(directory) / "slice.gcode.3mf"
                    validation = self.validator.validate(
                        context.blob_store.path_for(package_blob.relative_path),
                        profiles=resolved_profiles,
                        expectation=SlicerValidationExpectation(
                            expected_colors=_used_material_colors(project)
                        ),
                        cancellation=context.cancellation,
                        project_output=prepared_path,
                        sliced_output=sliced_path,
                    )
                    prepared_bytes = prepared_path.read_bytes()
                    if hashlib.sha256(prepared_bytes).hexdigest() != validation.source_sha256:
                        raise ValueError("Prepared download differs from the validated project")
                    package_blob = context.blob_store.put_bytes(
                        prepared_bytes,
                        namespace="artifacts",
                        extension=".3mf",
                        media_type="model/3mf",
                    )
                    if sliced_path.exists():
                        try:
                            if (
                                validation.artifact is None
                                or hashlib.sha256(sliced_path.read_bytes()).hexdigest()
                                != validation.artifact.sha256
                            ):
                                raise ValueError(
                                    "Retained slice does not match validation evidence"
                                )
                            meshes = {mesh.id: mesh for mesh in document.meshes}
                            base_top = max(
                                (
                                    vertex.z_mm
                                    for part in document.parts
                                    if part.role == "base"
                                    for vertex in meshes[part.mesh_id].vertices
                                ),
                                default=0,
                            )
                            scale = min(
                                1, 1024 / max(document.source_width_px, document.source_height_px)
                            )
                            footprint = render_sliced_preview(
                                sliced_path,
                                prepared_path,
                                palette=tuple(material.color for material in project.materials),
                                width_mm=document.canvas_width_mm,
                                height_mm=document.canvas_height_mm,
                                width_px=max(1, round(document.source_width_px * scale)),
                                height_px=max(1, round(document.source_height_px * scale)),
                                base_top_mm=base_top,
                                check_canceled=context.check_canceled,
                            )
                            sliced_preview_payload = footprint.png
                            sliced_preview_metadata = {
                                "package_sha256": validation.source_sha256,
                                "sliced_sha256": validation.artifact.sha256,
                                "width_px": footprint.width_px,
                                "height_px": footprint.height_px,
                                "approximation": (
                                    "Top-down extrusion footprints with line widths and arcs; "
                                    "not a physical print simulation."
                                ),
                            }
                        except WorkerCanceledError:
                            raise
                        except Exception as error:
                            # The valid editable download remains usable when a future slicer
                            # emits a path dialect we cannot faithfully display.
                            # Never invent a preview.
                            sliced_preview_metadata = {
                                "unavailable_reason": str(error).strip() or type(error).__name__
                            }
            except BambuStudioCanceledError as error:
                raise WorkerCanceledError("Bambu Studio slicing was canceled") from error
            except BambuStudioUnavailableError as error:
                raise _validation_job_error(
                    "bambu_unavailable",
                    "Bambu Studio is unavailable for mandatory slice validation.",
                    error.result,
                    retryable=False,
                    action="Install a supported Bambu Studio version and retry.",
                ) from error
            except BambuStudioTimeoutError as error:
                raise _validation_job_error(
                    "bambu_timeout",
                    "Bambu Studio did not finish slicing before the validation timeout.",
                    error.result,
                    retryable=True,
                    action="Retry after closing other slicing jobs or simplify the model.",
                ) from error
            except BambuStudioProfileError as error:
                raise _validation_job_error(
                    "slicer_profile_error",
                    "Pinned installed P2S profiles failed validation.",
                    error.result,
                    retryable=False,
                    action="Repair or update the measured Bambu Studio profile installation.",
                ) from error
            except (BambuStudioSliceError, InvalidSlicedArtifactError) as error:
                raise _validation_job_error(
                    "bambu_slice_failed",
                    "Bambu Studio could not produce a verified extrusion-bearing slice.",
                    error.result,
                    retryable=True,
                    action="Review the retained slicer evidence, geometry, and profile mapping.",
                ) from error
            if validation.status != BambuValidationStatus.VALIDATED:
                raise _validation_job_error(
                    "bambu_slice_failed",
                    "Bambu Studio returned non-validated slice evidence.",
                    validation,
                    retryable=True,
                    action="Review the retained slicer evidence and retry.",
                )
            context.check_canceled()
            context.report(stage=JobStage.VALIDATING, progress=0.9)
            quality_payload = _json_bytes(
                {"schema_version": 1, **_jsonable(quality), "manual_print_gate": "not_observed"}
            )
            validation_payload = _json_bytes(
                {
                    "schema_version": 1,
                    "result": _jsonable(validation),
                    "slicer_report": (
                        validation.report.as_contract() if validation.report is not None else None
                    ),
                    "source_geometry_sha256": request.geometry_sha256,
                    "request_key": request_key,
                    "manual_print_gate": "not_observed",
                    "sliced_preview": sliced_preview_metadata,
                }
            )
            log_payload = _validation_log(validation).encode("utf-8")
            identity_metadata = {} if derivation is None else derivation.metadata()
            artifacts = (
                _worker_artifact(
                    context,
                    kind="geometry-quality-report",
                    derivation_key=request_key,
                    payload=quality_payload,
                    extension=".json",
                    media_type="application/json",
                    metadata={
                        "safe_for_export": True,
                        "fingerprint": quality.fingerprint,
                        **identity_metadata,
                    },
                ),
                WorkerArtifact(
                    kind="bambu-3mf",
                    derivation_key=request_key,
                    blob=package_blob,
                    metadata={
                        "geometry_sha256": request.geometry_sha256,
                        "nozzle_diameter_mm": request.profile.nozzle_diameter_mm,
                        "layer_height_mm": request.profile.layer_height_mm,
                        **identity_metadata,
                    },
                ),
                _worker_artifact(
                    context,
                    kind="bambu-validation-report",
                    derivation_key=request_key,
                    payload=validation_payload,
                    extension=".json",
                    media_type="application/json",
                    metadata={
                        "status": validation.status.value,
                        "cache_key": validation.cache_key,
                        "manual_print_gate": "not_observed",
                        "sliced_preview_unavailable_reason": sliced_preview_metadata.get(
                            "unavailable_reason"
                        ),
                        "report_schema_version": (
                            validation.report.schema_version
                            if validation.report is not None
                            else None
                        ),
                        **identity_metadata,
                    },
                ),
                _worker_artifact(
                    context,
                    kind="bambu-validation-log",
                    derivation_key=request_key,
                    payload=log_payload,
                    extension=".log",
                    media_type="text/plain",
                    metadata={"status": validation.status.value, **identity_metadata},
                ),
            )
            if sliced_preview_payload is not None:
                artifacts += (
                    _worker_artifact(
                        context,
                        kind="bambu-slicer-preview-image",
                        derivation_key=request_key,
                        payload=sliced_preview_payload,
                        extension=".png",
                        media_type="image/png",
                        metadata={**sliced_preview_metadata, **identity_metadata},
                    ),
                )
            return WorkerResult(artifacts=artifacts)

        return work

    def get_result(self, *, project_id: str, job_id: str) -> ExportJobResult:
        connection = open_database(self.database_path)
        try:
            ProjectRepository(connection).get(project_id)
            job = JobRepository(connection).get(job_id)
            if job.project_id != project_id or job.type != JobType.EXPORT:
                raise RecordNotFoundError(f"export job not found: {job_id}")
            records = ArtifactRepository(connection).list_for_job(job_id)
        finally:
            connection.close()
        resources = tuple(self._verified_resource(item, project_id=project_id) for item in records)
        by_kind = {item.kind: item for item in resources}
        if job.state == JobState.SUCCEEDED and not _complete_export_bundle(set(by_kind)):
            raise ExportArtifactUnavailableError(
                "The successful export bundle is incomplete or contains unexpected artifacts."
            )
        return ExportJobResult(
            job=job,
            artifacts=resources,
            quality_report=by_kind.get("geometry-quality-report"),
            validation_report=by_kind.get("bambu-validation-report"),
            validation_log=by_kind.get("bambu-validation-log"),
            package=by_kind.get("bambu-3mf"),
            download_ready=job.state == JobState.SUCCEEDED,
        )

    def _load_source(self, project_id: str, request: StartExportRequest) -> GeometryDocument:
        connection = open_database(self.database_path)
        try:
            ProjectRepository(connection).get(project_id)
            if request.revision_id is not None:
                revision = RevisionRepository(connection).get(request.revision_id)
                if revision.project_id != project_id:
                    raise RecordNotFoundError(f"revision not found: {request.revision_id}")
            artifacts = ArtifactRepository(connection)
            artifact = artifacts.get(request.geometry_artifact_id)
            if artifacts.owner_project_id(artifact.id) != project_id:
                raise RecordNotFoundError(f"artifact not found: {artifact.id}")
        finally:
            connection.close()
        if request.revision_id is not None and artifact.revision_id != request.revision_id:
            raise InvalidExportRequestError(
                "The selected artifact was not published with the requested revision."
            )
        if artifact.kind != "geometry-ir":
            raise InvalidExportRequestError(
                "The selected artifact is not Geometry IR published with this revision."
            )
        if artifact.sha256 != request.geometry_sha256:
            raise InvalidExportRequestError("The selected Geometry IR SHA-256 does not match.")
        blob = _stored_blob(artifact)
        try:
            valid = self.blob_store.verify(blob)
            payload = self.blob_store.path_for(blob.relative_path).read_bytes() if valid else b""
        except (FileNotFoundError, OSError):
            valid = False
            payload = b""
        if not valid or hashlib.sha256(payload).hexdigest() != request.geometry_sha256:
            raise ExportArtifactUnavailableError("The Geometry IR artifact is missing or corrupt.")
        try:
            document = load_geometry_ir(payload)
        except GeometryIRDecodeError as error:
            raise InvalidExportRequestError(f"Geometry IR is invalid: {error}") from error
        if document.capabilities.mesh.status != CapabilityStatus.AVAILABLE:
            raise InvalidExportRequestError("Geometry IR has no available printable mesh artifact.")
        expected_materials = {item.id for item in document.materials}
        mapped_materials = {item.material_id for item in request.material_mapping}
        if mapped_materials and mapped_materials != expected_materials:
            raise InvalidExportRequestError(
                "Material mapping must exactly cover every Geometry IR material."
            )
        return document

    def _cached_or_active(self, project_id: str, request: StartExportRequest, request_key: str):
        connection = open_database(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE project_id = ? AND revision_id IS ? AND job_type = 'export'
                    AND request_key = ? AND state IN ('queued', 'running', 'succeeded')
                ORDER BY CASE state WHEN 'succeeded' THEN 0 ELSE 1 END, created_at DESC, id DESC
                """,
                (project_id, request.revision_id, request_key),
            ).fetchall()
            jobs = JobRepository(connection)
            artifacts = ArtifactRepository(connection)
            for row in rows:
                job = jobs.get(row["id"])
                if job.state in {JobState.QUEUED, JobState.RUNNING}:
                    return job
                records = artifacts.list_for_job(job.id)
                if not _complete_export_bundle({item.kind for item in records}):
                    continue
                valid_records = tuple(
                    item
                    for item in records
                    if item.derivation_key == request_key and self._record_is_valid(item)
                )
                if len(valid_records) == len(records):
                    return job
                for item in records:
                    if item not in valid_records:
                        self._remove_corrupt_output_blob(item)
        finally:
            connection.close()
        return None

    def _record_is_valid(self, artifact: ArtifactRecord) -> bool:
        try:
            return self.blob_store.verify(_stored_blob(artifact))
        except FileNotFoundError:
            return False

    def _remove_corrupt_output_blob(self, artifact: ArtifactRecord) -> None:
        """Remove only a verified-bad generated output so deterministic retry can replace it."""

        try:
            path = self.blob_store.path_for(artifact.relative_path)
        except FileNotFoundError:
            return
        if not self._record_is_valid(artifact):
            path.unlink(missing_ok=True)

    def _verified_resource(self, artifact: ArtifactRecord, *, project_id: str) -> ArtifactResource:
        if not self._record_is_valid(artifact):
            raise ExportArtifactUnavailableError(
                f"Export artifact {artifact.id} is missing or corrupt."
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


def export_request_key(project_id: str, request: StartExportRequest) -> str:
    return export_derivation_identity(project_id, request).key()


def export_derivation_identity(
    project_id: str,
    request: StartExportRequest,
    *,
    upstream_derivation_key: Optional[str] = None,
    profile_fingerprints: Optional[dict[str, str]] = None,
) -> DerivationIdentity:
    request_fingerprint = hashlib.sha256(_json_bytes(request.model_dump(mode="json"))).hexdigest()
    return DerivationIdentity(
        pipeline="export",
        source_fingerprint=request.geometry_sha256,
        config_fingerprint=request_fingerprint,
        operations_fingerprint=upstream_derivation_key or request.geometry_sha256,
        engine_version=__version__,
        adapter_versions={
            "bambu_3mf_package": "1",
            "bambu_slicer_validation": "1",
            "export_pipeline": str(EXPORT_ADAPTER_VERSION),
        },
        dependencies={
            "project": project_id,
            **(profile_fingerprints or {}),
        },
    )


def _profile_fingerprints(profiles: BambuSliceProfiles) -> dict[str, str]:
    return {
        "machine_profile": profiles.machine.sha256,
        "process_profile": profiles.process.sha256,
        **{
            f"filament_profile_{index}": profile.sha256
            for index, profile in enumerate(profiles.filaments, start=1)
        },
    }


def _bambu_project(document: GeometryDocument, request: StartExportRequest) -> BambuProject:
    material_by_id = {item.id: item for item in document.materials}
    if request.material_mapping:
        resolved_mapping = request.material_mapping
    else:
        if document.palette_color_order:
            material_by_palette = {item.palette_color_id: item for item in document.materials}
            ordered_materials = tuple(
                material_by_palette[palette_color_id]
                for palette_color_id in document.palette_color_order
            )
        else:
            ordered_materials = document.materials
        resolved_mapping = tuple(
            ExportMaterialMapping(
                material_id=material.id,
                extruder=index,
                preset="Bambu PLA Matte",
            )
            for index, material in enumerate(ordered_materials, start=1)
        )
    mapping = {item.material_id: item for item in resolved_mapping}
    ordered_mapping = tuple(sorted(mapping.values(), key=lambda item: item.extruder))
    material_index = {item.material_id: index for index, item in enumerate(ordered_mapping)}
    materials = tuple(
        _bambu_material(item, material_by_id[item.material_id]) for item in ordered_mapping
    )
    meshes_by_id = {item.id: item for item in document.meshes}
    meshes = tuple(
        BambuMesh(
            name=f"{part.name} [{part.id[-8:]}]",
            vertices=tuple(
                (vertex.x_mm, vertex.y_mm, vertex.z_mm)
                for vertex in meshes_by_id[part.mesh_id].vertices
            ),
            triangles=tuple(triangle.vertices for triangle in meshes_by_id[part.mesh_id].triangles),
            material_index=material_index[part.material_id],
        )
        for part in document.parts
    )
    x, y, tower = single_plate_placement(
        document.canvas_width_mm,
        document.canvas_height_mm,
        color_count=len({mesh.material_index for mesh in meshes}),
        layer_height_mm=request.profile.layer_height_mm,
    )
    return BambuProject(
        name=request.name,
        meshes=meshes,
        materials=materials,
        settings=BambuProjectSettings(
            printer_model=request.profile.printer_model,
            nozzle_diameter=request.profile.nozzle_diameter_mm,
            layer_height=request.profile.layer_height_mm,
            bed_type=request.profile.bed_type,
            plate_center_x=x,
            plate_center_y=y,
            prime_tower_position=tower,
        ),
    )


def _bambu_material(mapping: ExportMaterialMapping, material) -> BambuMaterial:
    return BambuMaterial(
        name=material.name,
        color=material.color_hex,
        extruder=mapping.extruder,
        filament_type=mapping.filament_type,
        preset=mapping.preset,
        material_id=material.id,
        palette_color_id=material.palette_color_id,
        filament_id=material.filament_id,
    )


def _used_material_colors(project: BambuProject) -> tuple[str, ...]:
    """Return declared colors that own geometry, preserving material-slot order."""

    used_indices = {mesh.material_index for mesh in project.meshes}
    return tuple(
        dict.fromkeys(
            material.color
            for index, material in enumerate(project.materials)
            if index in used_indices
        )
    )


def _worker_artifact(
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


def _stored_blob(artifact: ArtifactRecord) -> StoredBlob:
    return StoredBlob(
        sha256=artifact.sha256,
        relative_path=artifact.relative_path,
        byte_size=artifact.byte_size,
        media_type=artifact.media_type,
        extension=Path(artifact.relative_path).suffix,
    )


def _job_error(code: str, message: str, *, retryable: bool, action: str) -> WorkerJobError:
    return WorkerJobError(
        JobFailure(
            code=code,
            message=message,
            retryable=retryable,
            details={"action": action},
        )
    )


def _validation_job_error(
    code: str,
    message: str,
    result: BambuValidationResult,
    *,
    retryable: bool,
    action: str,
) -> WorkerJobError:
    return WorkerJobError(
        JobFailure(
            code=code,
            message=message,
            retryable=retryable,
            details={
                "action": action,
                "status": result.status.value,
                "return_code": result.return_code,
                "warnings": list(result.warnings),
            },
        )
    )


def _validation_log(result: BambuValidationResult) -> str:
    command = " ".join(result.command)
    return (
        f"status={result.status.value}\n"
        f"version={result.version or ''}\n"
        f"return_code={'' if result.return_code is None else result.return_code}\n"
        f"command={command}\n"
        "--- stdout ---\n"
        f"{result.stdout}\n"
        "--- stderr ---\n"
        f"{result.stderr}\n"
    )


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
