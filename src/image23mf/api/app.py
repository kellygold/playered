import asyncio
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastapi import Body, Depends, FastAPI, Header, Query, Request, Response
from fastapi import Path as ApiPath
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from image23mf import __version__
from image23mf.api.calibration import create_calibration_router
from image23mf.api.capabilities import discover_capabilities
from image23mf.api.errors import ApiProblem, install_error_handlers, problem_response
from image23mf.api.json_response import preview_json_response
from image23mf.api.mural import create_mural_router
from image23mf.api.prompted import create_prompted_router
from image23mf.artifact_lifecycle import (
    ArtifactNotRegenerableError,
    ImmutableArtifactError,
    delete_regenerable_artifact,
    inspect_artifact,
)
from image23mf.artifact_reveal import (
    ArtifactBlobUnavailableError,
    ArtifactNotRevealableError,
    FinderRevealFailedError,
    reveal_artifact_in_finder,
)
from image23mf.bundles import BundleError, ProjectBundleService
from image23mf.calibration import (
    PrintabilityProfileCatalog,
    ResolvedPrintabilitySettings,
    ResolvePrintabilityRequest,
    UnknownPrintabilityCatalogError,
    UnknownPrintabilityProfileError,
)
from image23mf.calibration.catalogs import PersistentPrintabilityProfileService
from image23mf.contracts.api import (
    ApiErrorCode,
    ApiErrorEnvelope,
    CapabilitiesResponse,
    HealthResponse,
    ToolCapability,
)
from image23mf.contracts.artifacts import (
    ArtifactDeletionResource,
    ArtifactInspectionResource,
    ArtifactRevealResponse,
)
from image23mf.contracts.bundles import ProjectBundleImportResource
from image23mf.contracts.exporting import (
    ExportJobResult,
    ExportStartResponse,
    StartExportRequest,
)
from image23mf.contracts.filaments import (
    CreateFilamentRequest,
    FilamentCatalogImportResource,
    FilamentCatalogResource,
    FilamentCollection,
    FilamentResource,
    ImportFilamentCatalogRequest,
    UpdateFilamentRequest,
)
from image23mf.contracts.geometry import (
    GeometryJobResult,
    GeometryStartResponse,
    StartGeometryRequest,
)
from image23mf.contracts.jobs import TERMINAL_JOB_STATES, InvalidJobTransitionError, JobResource
from image23mf.contracts.processing import (
    AutoPaletteRequest,
    AutoPaletteResource,
    DraftHistoryMutationRequest,
    DraftResource,
    PreviewJobResult,
    PreviewStartResponse,
    ProjectSummaryCollection,
    ProjectSummaryResource,
    ProjectWorkspaceResource,
    SaveDraftRequest,
    StartPreviewRequest,
)
from image23mf.contracts.revisions import (
    BranchRevisionRequest,
    BranchRevisionResponse,
    PublishRevisionRequest,
    PublishRevisionResponse,
    RevisionCollection,
    RevisionResource,
)
from image23mf.editor import EditorReplayError, IncompatibleSelectorError
from image23mf.engine.ingestion import (
    ImageIngestionError,
    ImageIngestionErrorCode,
    ImageIngestionLimits,
)
from image23mf.engine.palette import PaletteQuantizationError
from image23mf.exporting import (
    ExportArtifactUnavailableError,
    ExportService,
    InvalidExportRequestError,
)
from image23mf.filaments import FilamentCatalogService, UnknownCatalogEntryError
from image23mf.geometry.service import GeometryRequestError, GeometryService
from image23mf.mural.builds import MuralBuildService
from image23mf.processing import (
    InvalidDraftHistoryTargetError,
    InvalidPreviewJobError,
    InvalidRevisionCursorError,
    ProcessingService,
    make_preview_work,
)
from image23mf.profiles import (
    PrintSetupRequest,
    ProfileCatalog,
    ProfileCatalogService,
    ProfileValidationError,
    ValidatedPrintSetup,
)
from image23mf.prompted_edits.lifecycle import PromptedEditLifecycle
from image23mf.prompted_edits.service import PromptedEditService
from image23mf.recovery import (
    ReconciliationHistory,
    StartupReconciliationReport,
    StartupRecoveryService,
)
from image23mf.settings import Settings, settings
from image23mf.storage import (
    ArtifactRepository,
    AssetRepository,
    ContentAddressedStore,
    DraftHistoryBoundaryReachedError,
    DuplicateFilamentError,
    FilamentInUseError,
    FilamentRepository,
    InvalidDraftHistoryError,
    InvalidFilamentReferenceError,
    InvalidPublicationError,
    JobRepository,
    MissingBlobError,
    PreviewEvidenceConflictError,
    RecordNotFoundError,
    RegionOperation,
    StaleDraftError,
    StoredBlob,
    open_database,
)
from image23mf.workers import LocalWorkerManager
from image23mf.workspace_health import (
    DEFAULT_MINIMUM_AGE_SECONDS,
    MAX_MINIMUM_AGE_SECONDS,
    GCApplyRequest,
    GCExecution,
    GCPlan,
    InvalidGCPlanError,
    StaleGCPlanError,
    WorkspaceHealthReport,
    WorkspaceHealthService,
)

ERROR_RESPONSES = {
    404: {"model": ApiErrorEnvelope, "description": "Resource not found"},
    409: {"model": ApiErrorEnvelope, "description": "Resource state conflict"},
    422: {"model": ApiErrorEnvelope, "description": "Contract validation failure"},
    500: {"model": ApiErrorEnvelope, "description": "Unexpected local error"},
}

# Archive validation has a separate 10 GiB decompressed ceiling. This lower wire
# limit bounds temporary disk use before an untrusted archive can be inspected.
MAX_PROJECT_BUNDLE_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

ResourceId = Annotated[
    str,
    ApiPath(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]


def _project_bundle_filename(project_name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", project_name).strip("-").lower()[:80]
    return f"{stem or 'project'}.image23mf"


def create_app(app_settings: Optional[Settings] = None) -> FastAPI:
    resolved_settings = app_settings or settings
    app = FastAPI(
        title="Image23MF Studio",
        summary="Local nozzle-aware image-to-3MF workspace",
        version=__version__,
    )
    app.state.settings = resolved_settings
    app.state.profile_catalog_service = ProfileCatalogService.bundled()
    database_path = resolved_settings.workspace / "image23mf.sqlite3"
    app.state.printability_profile_service = PersistentPrintabilityProfileService(database_path)
    app.state.filament_catalog_service = FilamentCatalogService.bundled()
    store = ContentAddressedStore(resolved_settings.workspace)
    app.state.processing_service = ProcessingService(
        database_path=database_path,
        blob_store=store,
        profiles=app.state.profile_catalog_service,
        printability_profiles=app.state.printability_profile_service,
        ingestion_limits=ImageIngestionLimits(
            max_input_bytes=resolved_settings.max_image_input_bytes
        ),
    )
    app.state.preview_work_factory = make_preview_work
    app.state.worker_manager = LocalWorkerManager(
        database_path=database_path,
        blob_store=store,
    )
    app.state.prompted_edit_service = PromptedEditService(())
    app.state.geometry_service = GeometryService(
        database_path=database_path,
        blob_store=store,
    )
    app.state.export_service = ExportService(
        database_path=database_path,
        blob_store=store,
        bambu_resources_root=resolved_settings.bambu_resources_root,
    )
    app.state.mural_build_service = MuralBuildService(
        database_path=database_path,
        blob_store=store,
        profiles=app.state.profile_catalog_service,
        bambu_resources_root=resolved_settings.bambu_resources_root,
    )

    def reconcile_startup() -> None:
        connection = open_database(database_path)
        try:
            app.state.startup_reconciliation = StartupRecoveryService(connection, store).reconcile()
        finally:
            connection.close()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            reconcile_startup()
            yield
        finally:
            application.state.worker_manager.shutdown()

    app.router.lifespan_context = lifespan
    install_error_handlers(app)

    @app.exception_handler(UnknownPrintabilityCatalogError)
    async def unknown_printability_catalog(
        request: Request, error: UnknownPrintabilityCatalogError
    ):
        return problem_response(
            request,
            status_code=409,
            code=ApiErrorCode.CONFLICT,
            message=(
                "This project is pinned to a printability catalog that is not retained in the "
                "current workspace. Restore the source workspace backup or explicitly migrate "
                "the project after reviewing current defaults."
            ),
            details={"catalog_fingerprint": error.fingerprint},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "").strip()
        request.state.request_id = incoming[:128] if incoming else uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def database() -> Iterator[sqlite3.Connection]:
        connection = open_database(resolved_settings.workspace / "image23mf.sqlite3")
        try:
            yield connection
        finally:
            connection.close()

    def blob_store() -> ContentAddressedStore:
        return ContentAddressedStore(resolved_settings.workspace)

    def profile_catalog() -> ProfileCatalogService:
        return app.state.profile_catalog_service

    def mural_builds() -> MuralBuildService:
        return app.state.mural_build_service

    def printability_catalog() -> PrintabilityProfileCatalog:
        return app.state.printability_profile_service.catalog

    app.include_router(
        create_calibration_router(
            database=database,
            blob_store=blob_store,
            catalog=printability_catalog,
            error_responses=ERROR_RESPONSES,
        )
    )

    app.include_router(
        create_mural_router(
            database=database,
            blob_store=blob_store,
            profiles=profile_catalog,
            mural_builds=mural_builds,
            error_responses=ERROR_RESPONSES,
        )
    )
    app.include_router(
        create_prompted_router(
            database=database,
            blob_store=blob_store,
            lifecycle=lambda request: PromptedEditLifecycle(
                database_path=database_path,
                blob_store=store,
                prompted_edits=request.app.state.prompted_edit_service,
                workers=request.app.state.worker_manager,
            ),
            error_responses=ERROR_RESPONSES,
        )
    )

    @app.get("/api/health", tags=["system"], response_model=HealthResponse)
    def health() -> HealthResponse:
        tools = tuple(ToolCapability.model_validate(item) for item in discover_capabilities())
        return HealthResponse(
            status="ok",
            version=__version__,
            workspace=str(resolved_settings.workspace),
            capabilities=tools,
        )

    @app.get("/api/capabilities", tags=["system"], response_model=CapabilitiesResponse)
    def capabilities() -> CapabilitiesResponse:
        tools = tuple(ToolCapability.model_validate(item) for item in discover_capabilities())
        by_id = {tool.id: tool for tool in tools}
        return CapabilitiesResponse(
            version=__version__,
            tools=tools,
            features={
                "discrete_color_processing": True,
                "vectorization": by_id["potrace"].available,
                "reference_geometry": by_id["openscad"].available,
                "bambu_validation": by_id["bambu-studio"].available,
                "prompted_editing": bool(app.state.prompted_edit_service.providers()),
                "artifact_lifecycle": True,
                "finder_reveal": sys.platform == "darwin",
            },
        )

    @app.get("/api/profiles", tags=["profiles"], response_model=ProfileCatalog)
    def profiles(request: Request) -> ProfileCatalog:
        return request.app.state.profile_catalog_service.catalog

    @app.post(
        "/api/profiles/validate",
        tags=["profiles"],
        response_model=ValidatedPrintSetup,
        responses=ERROR_RESPONSES,
    )
    def validate_profile_setup(
        request_body: Annotated[PrintSetupRequest, Body()], request: Request
    ) -> ValidatedPrintSetup:
        try:
            return request.app.state.profile_catalog_service.validate(request_body)
        except ProfileValidationError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The print setup is incompatible with the selected profiles.",
                details={"issues": [issue.model_dump(mode="json") for issue in error.issues]},
            ) from error

    @app.get(
        "/api/printability-profiles",
        tags=["profiles"],
        response_model=PrintabilityProfileCatalog,
    )
    def printability_profiles(request: Request) -> PrintabilityProfileCatalog:
        return request.app.state.printability_profile_service.catalog

    @app.post(
        "/api/printability-profiles/resolve",
        tags=["profiles"],
        response_model=ResolvedPrintabilitySettings,
        responses=ERROR_RESPONSES,
    )
    def resolve_printability_profile(
        request_body: Annotated[ResolvePrintabilityRequest, Body()], request: Request
    ) -> ResolvedPrintabilitySettings:
        try:
            return request.app.state.printability_profile_service.resolve(request_body)
        except UnknownPrintabilityProfileError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message=str(error),
                details={"available_profile_ids": list(error.available_profile_ids)},
            ) from error

    @app.get(
        "/api/filaments",
        tags=["filaments"],
        response_model=FilamentCollection,
        responses=ERROR_RESPONSES,
    )
    def list_filaments(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        owned: Annotated[Optional[bool], Query()] = None,
        query: Annotated[str, Query(max_length=160)] = "",
        manufacturer: Annotated[str, Query(max_length=120)] = "",
        material: Annotated[str, Query(max_length=80)] = "",
    ) -> FilamentCollection:
        records = FilamentRepository(connection).list(
            owned=owned,
            query=query,
            manufacturer=manufacturer,
            material=material,
        )
        items = tuple(FilamentResource.from_record(record) for record in records)
        return FilamentCollection(items=items, total=len(items))

    @app.post(
        "/api/filaments",
        tags=["filaments"],
        status_code=201,
        response_model=FilamentResource,
        responses=ERROR_RESPONSES,
    )
    def create_filament(
        request_body: Annotated[CreateFilamentRequest, Body()],
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> FilamentResource:
        try:
            record = FilamentRepository(connection).create(**request_body.model_dump(mode="python"))
        except DuplicateFilamentError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="That filament color is already in the library.",
            ) from error
        except ValueError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message=str(error),
            ) from error
        return FilamentResource.from_record(record)

    @app.get(
        "/api/filaments/{filament_id}",
        tags=["filaments"],
        response_model=FilamentResource,
        responses=ERROR_RESPONSES,
    )
    def get_filament(
        filament_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> FilamentResource:
        try:
            return FilamentResource.from_record(FilamentRepository(connection).get(filament_id))
        except RecordNotFoundError as error:
            raise _filament_not_found(filament_id) from error

    @app.patch(
        "/api/filaments/{filament_id}",
        tags=["filaments"],
        response_model=FilamentResource,
        responses=ERROR_RESPONSES,
    )
    def update_filament(
        filament_id: ResourceId,
        request_body: Annotated[UpdateFilamentRequest, Body()],
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> FilamentResource:
        try:
            record = FilamentRepository(connection).update(
                filament_id,
                request_body.model_dump(mode="python", exclude_unset=True),
            )
        except RecordNotFoundError as error:
            raise _filament_not_found(filament_id) from error
        except DuplicateFilamentError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="That filament color is already in the library.",
            ) from error
        except ValueError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message=str(error),
            ) from error
        return FilamentResource.from_record(record)

    @app.delete(
        "/api/filaments/{filament_id}",
        tags=["filaments"],
        status_code=204,
        response_class=Response,
        responses=ERROR_RESPONSES,
    )
    def delete_filament(
        filament_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> Response:
        try:
            FilamentRepository(connection).delete(filament_id)
        except RecordNotFoundError as error:
            raise _filament_not_found(filament_id) from error
        except FilamentInUseError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="This filament is used by a saved palette and cannot be deleted.",
                details={"filament_id": filament_id},
            ) from error
        return Response(status_code=204)

    @app.get(
        "/api/filament-catalogs/bambu-lab-starter",
        tags=["filaments"],
        response_model=FilamentCatalogResource,
    )
    def filament_catalog(request: Request) -> FilamentCatalogResource:
        catalog = request.app.state.filament_catalog_service.catalog
        return FilamentCatalogResource(catalog=catalog, fingerprint=catalog.fingerprint())

    @app.post(
        "/api/filament-catalogs/bambu-lab-starter/import",
        tags=["filaments"],
        response_model=FilamentCatalogImportResource,
        responses=ERROR_RESPONSES,
    )
    def import_filament_catalog(
        request_body: Annotated[ImportFilamentCatalogRequest, Body()],
        request: Request,
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> FilamentCatalogImportResource:
        try:
            result = request.app.state.filament_catalog_service.import_entries(
                FilamentRepository(connection),
                request_body.entry_ids,
                owned=request_body.owned,
            )
        except UnknownCatalogEntryError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="One or more catalog colors do not exist.",
                details={"entry_ids": list(error.entry_ids)},
            ) from error
        return FilamentCatalogImportResource(
            catalog_id=result.catalog_id,
            catalog_version=result.catalog_version,
            catalog_fingerprint=result.catalog_fingerprint,
            filaments=tuple(FilamentResource.from_record(item) for item in result.filaments),
        )

    @app.post(
        "/api/projects/import",
        tags=["projects"],
        status_code=201,
        response_model=ProjectWorkspaceResource,
        responses=ERROR_RESPONSES,
    )
    async def import_project(
        request: Request,
        filename: Annotated[
            str,
            Header(alias="X-Filename", min_length=1, max_length=255),
        ],
        project_name: Annotated[Optional[str], Query(max_length=160)] = None,
    ) -> ProjectWorkspaceResource:
        declared_size = request.headers.get("content-length")
        if declared_size is not None:
            try:
                too_large = (
                    int(declared_size)
                    > request.app.state.processing_service.ingestion_limits.max_input_bytes
                )
            except ValueError:
                too_large = False
            if too_large:
                raise ApiProblem(
                    status_code=413,
                    code=ApiErrorCode.RESOURCE_LIMIT,
                    message="The image file exceeds the configured byte limit.",
                    details={
                        "maximum_bytes": (
                            request.app.state.processing_service.ingestion_limits.max_input_bytes
                        ),
                        "suggestion": "Export a smaller PNG, JPEG, or WebP image.",
                    },
                )
        maximum_bytes = request.app.state.processing_service.ingestion_limits.max_input_bytes
        incoming = bytearray()
        async for chunk in request.stream():
            if len(incoming) + len(chunk) > maximum_bytes:
                raise ApiProblem(
                    status_code=413,
                    code=ApiErrorCode.RESOURCE_LIMIT,
                    message="The image file exceeds the configured byte limit.",
                    details={
                        "maximum_bytes": maximum_bytes,
                        "suggestion": "Export a smaller PNG, JPEG, or WebP image.",
                    },
                )
            incoming.extend(chunk)
        payload = bytes(incoming)
        try:
            return request.app.state.processing_service.import_project(
                payload,
                filename=filename,
                project_name=project_name,
            )
        except ImageIngestionError as error:
            resource_codes = {
                ImageIngestionErrorCode.INPUT_TOO_LARGE,
                ImageIngestionErrorCode.DIMENSIONS_TOO_LARGE,
                ImageIngestionErrorCode.PIXEL_LIMIT_EXCEEDED,
                ImageIngestionErrorCode.COLOR_PROFILE_TOO_LARGE,
                ImageIngestionErrorCode.DECODE_RESOURCE_EXHAUSTED,
            }
            is_unsupported = error.code == ImageIngestionErrorCode.UNSUPPORTED_FORMAT
            raise ApiProblem(
                status_code=415 if is_unsupported else 413 if error.code in resource_codes else 422,
                code=(
                    ApiErrorCode.UNSUPPORTED_MEDIA
                    if is_unsupported
                    else ApiErrorCode.RESOURCE_LIMIT
                    if error.code in resource_codes
                    else ApiErrorCode.VALIDATION_ERROR
                ),
                message=error.message,
                details={
                    "ingestion_code": error.code.value,
                    "suggestion": error.suggestion,
                    **error.details,
                },
            ) from error

    @app.get(
        "/api/projects",
        tags=["projects"],
        response_model=ProjectSummaryCollection,
        responses=ERROR_RESPONSES,
    )
    def list_projects(
        request: Request,
        include_archived: Annotated[bool, Query()] = False,
    ) -> ProjectSummaryCollection:
        return request.app.state.processing_service.list_projects(include_archived=include_archived)

    @app.post(
        "/api/projects/{project_id}/archive",
        tags=["projects"],
        response_model=ProjectSummaryResource,
        responses=ERROR_RESPONSES,
    )
    def archive_project(project_id: ResourceId, request: Request) -> ProjectSummaryResource:
        try:
            return request.app.state.processing_service.set_project_archived(
                project_id, archived=True
            )
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested project does not exist.",
                details={"resource": "project", "id": project_id},
            ) from error

    @app.post(
        "/api/projects/{project_id}/restore",
        tags=["projects"],
        response_model=ProjectSummaryResource,
        responses=ERROR_RESPONSES,
    )
    def restore_project(project_id: ResourceId, request: Request) -> ProjectSummaryResource:
        try:
            return request.app.state.processing_service.set_project_archived(
                project_id, archived=False
            )
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested project does not exist.",
                details={"resource": "project", "id": project_id},
            ) from error

    @app.get(
        "/api/projects/{project_id}",
        tags=["projects"],
        response_model=ProjectWorkspaceResource,
        responses=ERROR_RESPONSES,
    )
    def get_project(project_id: ResourceId, request: Request) -> ProjectWorkspaceResource:
        try:
            return request.app.state.processing_service.get_workspace(project_id)
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested project does not exist.",
                details={"resource": "project", "id": project_id},
            ) from error

    @app.get(
        "/api/projects/{project_id}/bundle",
        tags=["projects"],
        response_class=FileResponse,
        responses=ERROR_RESPONSES,
    )
    def export_project_bundle(
        project_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        history_mode: Annotated[
            Literal["full_history", "current_state_only"], Query()
        ] = "full_history",
    ) -> FileResponse:
        temporary_dir = resolved_settings.workspace / "temp" / "project-bundles"
        temporary_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="export-", suffix=".image23mf", dir=temporary_dir
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.chmod(0o600)
        try:
            result = ProjectBundleService(connection, blob_store()).export(
                project_id,
                temporary,
                history_mode=history_mode,
            )
        except RecordNotFoundError as error:
            temporary.unlink(missing_ok=True)
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested project does not exist.",
                details={"resource": "project", "id": project_id},
            ) from error
        except BundleError as error:
            temporary.unlink(missing_ok=True)
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The project bundle could not be created safely.",
                details={"reason": str(error)},
            ) from error
        return FileResponse(
            result.path,
            media_type="application/vnd.image23mf.project+zip",
            filename=_project_bundle_filename(result.manifest.project.name),
            headers={
                "X-Image23MF-Bundle-SHA256": result.sha256,
                "X-Image23MF-History-Mode": result.manifest.history_mode,
            },
            background=BackgroundTask(result.path.unlink, missing_ok=True),
        )

    @app.post(
        "/api/project-bundles/import",
        tags=["projects"],
        status_code=201,
        response_model=ProjectBundleImportResource,
        responses=ERROR_RESPONSES,
    )
    async def import_project_bundle(
        request: Request,
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> ProjectBundleImportResource:
        raw_content_length = request.headers.get("content-length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError as error:
                raise ApiProblem(
                    status_code=400,
                    code=ApiErrorCode.VALIDATION_ERROR,
                    message="The project bundle Content-Length is invalid.",
                ) from error
            if content_length < 0:
                raise ApiProblem(
                    status_code=400,
                    code=ApiErrorCode.VALIDATION_ERROR,
                    message="The project bundle Content-Length is invalid.",
                )
            if content_length > MAX_PROJECT_BUNDLE_UPLOAD_BYTES:
                raise ApiProblem(
                    status_code=413,
                    code=ApiErrorCode.RESOURCE_LIMIT,
                    message="The project bundle exceeds the upload byte limit.",
                    details={"maximum_bytes": MAX_PROJECT_BUNDLE_UPLOAD_BYTES},
                )
        temporary_dir = resolved_settings.workspace / "temp" / "project-bundles"
        temporary_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="import-", suffix=".image23mf", dir=temporary_dir
        )
        temporary = Path(temporary_name)
        total = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.chmod(temporary, 0o600)
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_PROJECT_BUNDLE_UPLOAD_BYTES:
                        raise ApiProblem(
                            status_code=413,
                            code=ApiErrorCode.RESOURCE_LIMIT,
                            message="The project bundle exceeds the upload byte limit.",
                            details={"maximum_bytes": MAX_PROJECT_BUNDLE_UPLOAD_BYTES},
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            result = ProjectBundleService(connection, blob_store()).restore(temporary)
            return ProjectBundleImportResource.from_result(result)
        except ApiProblem:
            raise
        except BundleError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The project bundle is invalid or could not be restored safely.",
                details={"reason": str(error)},
            ) from error
        finally:
            temporary.unlink(missing_ok=True)

    @app.put(
        "/api/projects/{project_id}/draft",
        tags=["projects"],
        response_model=DraftResource,
        responses=ERROR_RESPONSES,
    )
    def save_project_draft(
        project_id: ResourceId,
        request_body: Annotated[SaveDraftRequest, Body()],
        request: Request,
    ) -> DraftResource:
        operations = tuple(
            RegionOperation(
                operation_type=item.operation_type,
                selection=item.selection,
                parameters=item.parameters,
                source=item.source,
                provenance=item.provenance,
            )
            for item in request_body.operations
        )
        try:
            return request.app.state.processing_service.save_draft(
                project_id=project_id,
                config=request_body.config,
                operations=operations,
                expected_draft_generation=request_body.expected_draft_generation,
                history_command=request_body.history_command,
            )
        except ProfileValidationError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The draft configuration is incompatible with the selected profiles.",
                details={"issues": [issue.model_dump(mode="json") for issue in error.issues]},
            ) from error
        except InvalidFilamentReferenceError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The palette references filament colors that are not in the library.",
                details={"filament_ids": list(error.missing_ids)},
            ) from error
        except IncompatibleSelectorError as error:
            raise _incompatible_editor_history(error, project_id=project_id) from error
        except EditorReplayError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The saved editor history cannot be replayed safely.",
                details={"reason": str(error), "action": "Review or remove the stale edit."},
            ) from error
        except StaleDraftError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.STALE_DRAFT,
                message="A newer project draft already exists.",
                details={"action": "Reload the project and retry your edit."},
            ) from error
        except InvalidDraftHistoryError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.STALE_DRAFT,
                message="The editor history changed before this command was saved.",
                details={"action": "Reload the project before making another edit."},
            ) from error
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The project or source image no longer exists.",
                details={"project_id": project_id},
            ) from error

    def move_project_draft_history(
        project_id: str,
        request_body: DraftHistoryMutationRequest,
        request: Request,
        *,
        direction: str,
    ) -> DraftResource:
        try:
            return request.app.state.processing_service.move_draft_history(
                project_id=project_id,
                direction=direction,
                request_id=request_body.request_id,
                expected_draft_generation=request_body.expected_draft_generation,
                expected_cursor_node_id=request_body.expected_cursor_node_id,
            )
        except (StaleDraftError, InvalidDraftHistoryError) as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.STALE_DRAFT,
                message="The editor history changed before this command could be applied.",
                details={"action": "Reload the project before retrying."},
            ) from error
        except DraftHistoryBoundaryReachedError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.STALE_DRAFT,
                message="There is no command available in that history direction.",
                details={"direction": direction},
            ) from error
        except (IncompatibleSelectorError, EditorReplayError) as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.INCOMPATIBLE_EDITOR_HISTORY,
                message="The stored editor state cannot be replayed safely.",
                details={"reason": str(error)},
            ) from error
        except InvalidDraftHistoryTargetError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.INCOMPATIBLE_EDITOR_HISTORY,
                message="The stored editor state is no longer compatible with current profiles.",
                details={"reason": str(error), "action": "Keep the current draft state."},
            ) from error
        except ProfileValidationError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The stored editor state is incompatible with current printer profiles.",
                details={"issues": [issue.model_dump(mode="json") for issue in error.issues]},
            ) from error
        except InvalidFilamentReferenceError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The stored editor state references unavailable filament colors.",
                details={
                    "filament_ids": list(error.missing_ids),
                    "action": "Restore the missing filament before undoing or redoing.",
                },
            ) from error
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The project draft does not exist.",
                details={"project_id": project_id},
            ) from error

    @app.post(
        "/api/projects/{project_id}/draft/history/undo",
        tags=["projects"],
        response_model=DraftResource,
        responses=ERROR_RESPONSES,
    )
    def undo_project_draft(
        project_id: ResourceId,
        request_body: Annotated[DraftHistoryMutationRequest, Body()],
        request: Request,
    ) -> DraftResource:
        return move_project_draft_history(project_id, request_body, request, direction="undo")

    @app.post(
        "/api/projects/{project_id}/draft/history/redo",
        tags=["projects"],
        response_model=DraftResource,
        responses=ERROR_RESPONSES,
    )
    def redo_project_draft(
        project_id: ResourceId,
        request_body: Annotated[DraftHistoryMutationRequest, Body()],
        request: Request,
    ) -> DraftResource:
        return move_project_draft_history(project_id, request_body, request, direction="redo")

    @app.get(
        "/api/projects/{project_id}/revisions",
        tags=["revisions"],
        response_model=RevisionCollection,
        responses=ERROR_RESPONSES,
    )
    def list_project_revisions(
        project_id: ResourceId,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[Optional[str], Query(max_length=512)] = None,
    ) -> RevisionCollection:
        try:
            return request.app.state.processing_service.list_revisions(
                project_id, limit=limit, cursor=cursor
            )
        except InvalidRevisionCursorError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The revision history cursor is invalid.",
                details={"action": "Reload revision history from the first page."},
            ) from error
        except RecordNotFoundError as error:
            raise _revision_not_found(project_id=project_id) from error

    @app.get(
        "/api/projects/{project_id}/revisions/{revision_id}",
        tags=["revisions"],
        response_model=RevisionResource,
        responses=ERROR_RESPONSES,
    )
    def get_project_revision(
        project_id: ResourceId,
        revision_id: ResourceId,
        request: Request,
    ) -> RevisionResource:
        try:
            return request.app.state.processing_service.get_revision(
                project_id=project_id, revision_id=revision_id
            )
        except RecordNotFoundError as error:
            raise _revision_not_found(project_id=project_id, revision_id=revision_id) from error

    @app.post(
        "/api/projects/{project_id}/revisions",
        tags=["revisions"],
        status_code=201,
        response_model=PublishRevisionResponse,
        responses=ERROR_RESPONSES,
    )
    def publish_project_revision(
        project_id: ResourceId,
        request_body: Annotated[PublishRevisionRequest, Body()],
        request: Request,
    ) -> PublishRevisionResponse:
        try:
            return request.app.state.processing_service.publish_revision(
                project_id=project_id,
                expected_draft_generation=request_body.expected_draft_generation,
                label=request_body.label,
                notes=request_body.notes,
                preview_job_id=request_body.preview_job_id,
            )
        except StaleDraftError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.STALE_DRAFT,
                message="A newer project draft already exists.",
                details={
                    "project_id": project_id,
                    "action": "Reload the project before publishing this revision.",
                },
            ) from error
        except PreviewEvidenceConflictError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="The selected preview is not current publication evidence.",
                details={
                    "project_id": project_id,
                    "preview_job_id": error.preview_job_id,
                    "reason": error.reason,
                    "action": "Generate a new preview or publish without a preview guard.",
                },
            ) from error
        except ProfileValidationError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The revision configuration is incompatible with its profiles.",
                details={"issues": [issue.model_dump(mode="json") for issue in error.issues]},
            ) from error
        except InvalidFilamentReferenceError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The revision references filament colors that are not in the library.",
                details={"filament_ids": list(error.missing_ids)},
            ) from error
        except IncompatibleSelectorError as error:
            raise _incompatible_editor_history(error, project_id=project_id) from error
        except EditorReplayError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The editor history cannot be published safely.",
                details={"reason": str(error)},
            ) from error
        except RecordNotFoundError as error:
            raise _revision_not_found(project_id=project_id) from error
        except (InvalidPublicationError, MissingBlobError) as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="The revision could not be published atomically.",
                details={"project_id": project_id, "reason": str(error)},
            ) from error

    @app.post(
        "/api/projects/{project_id}/revisions/{revision_id}/branch",
        tags=["revisions"],
        response_model=BranchRevisionResponse,
        responses=ERROR_RESPONSES,
    )
    def branch_project_revision(
        project_id: ResourceId,
        revision_id: ResourceId,
        request_body: Annotated[BranchRevisionRequest, Body()],
        request: Request,
    ) -> BranchRevisionResponse:
        try:
            return request.app.state.processing_service.branch_revision(
                project_id=project_id,
                revision_id=revision_id,
                expected_draft_generation=request_body.expected_draft_generation,
            )
        except StaleDraftError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.STALE_DRAFT,
                message="A newer project draft already exists.",
                details={
                    "project_id": project_id,
                    "revision_id": revision_id,
                    "action": "Reload the project before opening this revision.",
                },
            ) from error
        except InvalidFilamentReferenceError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The historical revision references missing filament colors.",
                details={"filament_ids": list(error.missing_ids)},
            ) from error
        except IncompatibleSelectorError as error:
            raise _incompatible_editor_history(error, project_id=project_id) from error
        except EditorReplayError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="This historical editor sequence cannot be reopened safely.",
                details={
                    "project_id": project_id,
                    "revision_id": revision_id,
                    "reason": str(error),
                    "action": "Keep this revision as read-only history.",
                },
            ) from error
        except RecordNotFoundError as error:
            raise _revision_not_found(project_id=project_id, revision_id=revision_id) from error
        except InvalidPublicationError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="The historical revision could not be reopened safely.",
                details={"project_id": project_id, "revision_id": revision_id},
            ) from error

    @app.post(
        "/api/projects/{project_id}/palette/auto",
        tags=["palettes"],
        response_model=AutoPaletteResource,
        responses=ERROR_RESPONSES,
    )
    def auto_palette(
        project_id: ResourceId,
        request_body: Annotated[AutoPaletteRequest, Body()],
        request: Request,
    ) -> AutoPaletteResource:
        try:
            return request.app.state.processing_service.auto_palette(
                project_id=project_id,
                config=request_body.config,
                color_count=request_body.color_count,
            )
        except ProfileValidationError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The palette configuration is incompatible with the selected profiles.",
                details={"issues": [issue.model_dump(mode="json") for issue in error.issues]},
            ) from error
        except PaletteQuantizationError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message=str(error),
                details={"palette_code": error.code.value},
            ) from error
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The project or source image no longer exists.",
                details={"project_id": project_id},
            ) from error
        except (FileNotFoundError, ValueError) as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.BLOB_UNAVAILABLE,
                message="The source image is missing or corrupt.",
                details={"project_id": project_id},
            ) from error

    @app.post(
        "/api/projects/{project_id}/previews",
        tags=["previews"],
        status_code=202,
        response_model=PreviewStartResponse,
        responses=ERROR_RESPONSES,
    )
    def start_preview(
        project_id: ResourceId,
        request_body: Annotated[StartPreviewRequest, Body()],
        request: Request,
    ) -> PreviewStartResponse:
        try:
            return request.app.state.processing_service.start_preview(
                project_id=project_id,
                config=request_body.config,
                expected_draft_generation=request_body.expected_draft_generation,
                manager=request.app.state.worker_manager,
                work_factory=request.app.state.preview_work_factory,
            )
        except ProfileValidationError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The preview configuration is incompatible with the selected profiles.",
                details={"issues": [issue.model_dump(mode="json") for issue in error.issues]},
            ) from error
        except InvalidFilamentReferenceError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The palette references filament colors that are not in the library.",
                details={"filament_ids": list(error.missing_ids)},
            ) from error
        except IncompatibleSelectorError as error:
            raise _incompatible_editor_history(error, project_id=project_id) from error
        except EditorReplayError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The saved editor history cannot be replayed safely.",
                details={"reason": str(error), "action": "Review or remove the stale edit."},
            ) from error
        except StaleDraftError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.STALE_DRAFT,
                message="A newer project draft already exists.",
                details={"action": "Reload the project and retry your edit."},
            ) from error
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The project or source image no longer exists.",
                details={"project_id": project_id},
            ) from error

    @app.post(
        "/api/projects/{project_id}/geometry",
        tags=["geometry"],
        status_code=202,
        response_model=GeometryStartResponse,
        responses=ERROR_RESPONSES,
    )
    def start_geometry(
        project_id: ResourceId,
        request_body: Annotated[StartGeometryRequest, Body()],
        request: Request,
    ) -> GeometryStartResponse:
        try:
            return request.app.state.geometry_service.start(
                project_id=project_id,
                preview_job_id=request_body.preview_job_id,
                expected_draft_generation=request_body.expected_draft_generation,
                manager=request.app.state.worker_manager,
            )
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The project or preview does not exist.",
                details={"project_id": project_id},
            ) from error
        except GeometryRequestError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="Geometry requires a current, verified preview.",
                details={"project_id": project_id, "reason": str(error)},
            ) from error

    @app.get(
        "/api/projects/{project_id}/geometry/{job_id}",
        tags=["geometry"],
        response_model=GeometryJobResult,
        responses=ERROR_RESPONSES,
    )
    def geometry_result(
        project_id: ResourceId,
        job_id: ResourceId,
        request: Request,
    ) -> GeometryJobResult:
        try:
            return request.app.state.geometry_service.result(
                project_id=project_id,
                job_id=job_id,
            )
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested geometry does not exist for this project.",
                details={"project_id": project_id, "job_id": job_id},
            ) from error
        except GeometryRequestError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.BLOB_UNAVAILABLE,
                message="The geometry result is incomplete, missing, or corrupt.",
                details={"project_id": project_id, "job_id": job_id, "reason": str(error)},
            ) from error

    @app.post(
        "/api/projects/{project_id}/exports",
        tags=["exports"],
        status_code=202,
        response_model=ExportStartResponse,
        responses=ERROR_RESPONSES,
    )
    def start_export(
        project_id: ResourceId,
        request_body: Annotated[StartExportRequest, Body()],
        request: Request,
    ) -> ExportStartResponse:
        try:
            return request.app.state.export_service.start_export(
                project_id=project_id,
                request=request_body,
                manager=request.app.state.worker_manager,
            )
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The project, revision, or geometry artifact does not exist.",
                details={"project_id": project_id},
            ) from error
        except InvalidExportRequestError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="The selected geometry cannot be exported.",
                details={"project_id": project_id, "reason": str(error)},
            ) from error
        except ExportArtifactUnavailableError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.BLOB_UNAVAILABLE,
                message="The selected geometry artifact is missing or corrupt.",
                details={"project_id": project_id, "reason": str(error)},
            ) from error

    @app.get(
        "/api/projects/{project_id}/exports/{job_id}",
        tags=["exports"],
        response_model=ExportJobResult,
        responses=ERROR_RESPONSES,
    )
    def export_result(
        project_id: ResourceId,
        job_id: ResourceId,
        request: Request,
    ) -> ExportJobResult:
        try:
            return request.app.state.export_service.get_result(
                project_id=project_id,
                job_id=job_id,
            )
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested export does not exist for this project.",
                details={"project_id": project_id, "job_id": job_id},
            ) from error
        except ExportArtifactUnavailableError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.BLOB_UNAVAILABLE,
                message="The export result is incomplete, missing, or corrupt.",
                details={"project_id": project_id, "job_id": job_id, "reason": str(error)},
            ) from error

    @app.get(
        "/api/jobs/{job_id}/result",
        tags=["previews"],
        response_model=PreviewJobResult,
        responses=ERROR_RESPONSES,
    )
    def preview_result(job_id: ResourceId, request: Request) -> Response:
        try:
            result = request.app.state.processing_service.get_preview_result(job_id)
            return preview_json_response(result, request.headers.get("accept-encoding", ""))
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested job does not exist.",
                details={"resource": "job", "id": job_id},
            ) from error
        except InvalidPreviewJobError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="This result resource only supports preview jobs.",
                details={"job_id": job_id},
            ) from error
        except (FileNotFoundError, ValueError) as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.BLOB_UNAVAILABLE,
                message="The preview result is missing or corrupt.",
                details={"job_id": job_id},
            ) from error

    @app.get(
        "/api/jobs/{job_id}/events",
        tags=["jobs"],
        response_class=StreamingResponse,
        responses=ERROR_RESPONSES,
    )
    async def job_events(job_id: ResourceId, request: Request) -> StreamingResponse:
        try:
            initial = request.app.state.worker_manager.get(job_id)
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested job does not exist.",
                details={"resource": "job", "id": job_id},
            ) from error

        async def stream():
            current = initial
            previous = None
            while True:
                payload = current.model_dump_json()
                if payload != previous:
                    yield f"event: job\ndata: {payload}\n\n"
                    previous = payload
                if current.state in TERMINAL_JOB_STATES or await request.is_disconnected():
                    break
                await asyncio.sleep(0.1)
                current = request.app.state.worker_manager.get(job_id)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/jobs/{job_id}",
        tags=["jobs"],
        response_model=JobResource,
        responses=ERROR_RESPONSES,
    )
    def get_job(
        job_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> JobResource:
        try:
            return JobRepository(connection).get(job_id)
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested job does not exist.",
                details={"resource": "job", "id": job_id},
            ) from error

    @app.post(
        "/api/jobs/{job_id}/cancel",
        tags=["jobs"],
        response_model=JobResource,
        responses=ERROR_RESPONSES,
    )
    def cancel_job(
        job_id: ResourceId,
        request: Request,
    ) -> JobResource:
        try:
            return request.app.state.worker_manager.cancel(job_id)
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested job does not exist.",
                details={"resource": "job", "id": job_id},
            ) from error
        except InvalidJobTransitionError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.INVALID_JOB_STATE,
                message="This job can no longer be canceled.",
                details={"job_id": job_id},
            ) from error

    @app.get(
        "/api/assets/{asset_id}",
        tags=["assets"],
        response_class=FileResponse,
        responses=ERROR_RESPONSES,
    )
    def download_asset(
        asset_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> FileResponse:
        try:
            asset = AssetRepository(connection, store).get(asset_id)
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested source image does not exist.",
                details={"resource": "asset", "id": asset_id},
            ) from error
        blob = asset.stored_blob()
        try:
            valid = store.verify(blob)
        except FileNotFoundError:
            valid = False
        if not valid:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.BLOB_UNAVAILABLE,
                message="The source image record exists but its file is missing or corrupt.",
                details={"asset_id": asset_id},
            )
        return FileResponse(
            store.path_for(asset.relative_path),
            media_type=asset.media_type,
            filename=asset.original_filename,
        )

    @app.get(
        "/api/projects/{project_id}/artifacts/{artifact_id}",
        tags=["artifacts"],
        response_class=FileResponse,
        responses=ERROR_RESPONSES,
    )
    def download_artifact(
        project_id: ResourceId,
        artifact_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> FileResponse:
        try:
            artifacts = ArtifactRepository(connection)
            artifact = artifacts.get(artifact_id)
            if artifacts.owner_project_id(artifact_id) != project_id:
                raise RecordNotFoundError(f"artifact not found: {artifact_id}")
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The requested artifact does not exist.",
                details={"resource": "artifact", "project_id": project_id, "id": artifact_id},
            ) from error
        extension = Path(artifact.relative_path).suffix
        blob = StoredBlob(
            sha256=artifact.sha256,
            relative_path=artifact.relative_path,
            byte_size=artifact.byte_size,
            media_type=artifact.media_type,
            extension=extension,
        )
        try:
            valid = store.verify(blob)
        except FileNotFoundError:
            valid = False
        if not valid:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.BLOB_UNAVAILABLE,
                message="The artifact record exists but its file is missing or corrupt.",
                details={"artifact_id": artifact_id},
            )
        return FileResponse(
            store.path_for(artifact.relative_path),
            media_type=artifact.media_type,
            filename=f"{artifact.kind}-{artifact.id}{extension}",
        )

    @app.get(
        "/api/projects/{project_id}/artifacts/{artifact_id}/inspection",
        tags=["artifacts"],
        response_model=ArtifactInspectionResource,
        responses=ERROR_RESPONSES,
    )
    def inspect_project_artifact(
        project_id: ResourceId,
        artifact_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> ArtifactInspectionResource:
        try:
            return inspect_artifact(
                connection=connection,
                store=store,
                project_id=project_id,
                artifact_id=artifact_id,
            )
        except RecordNotFoundError as error:
            raise _artifact_not_found(project_id, artifact_id) from error

    @app.post(
        "/api/projects/{project_id}/artifacts/{artifact_id}/reveal",
        tags=["artifacts"],
        response_model=ArtifactRevealResponse,
        responses=ERROR_RESPONSES,
    )
    def reveal_project_artifact(
        project_id: ResourceId,
        artifact_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> ArtifactRevealResponse:
        try:
            result = reveal_artifact_in_finder(
                connection=connection,
                store=store,
                project_id=project_id,
                artifact_id=artifact_id,
            )
            return ArtifactRevealResponse(
                artifact_id=result.artifact_id,
                supported=result.supported,
                revealed=result.revealed,
                reason=result.reason,
            )
        except RecordNotFoundError as error:
            raise _artifact_not_found(project_id, artifact_id) from error
        except ArtifactNotRevealableError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="This artifact cannot be revealed in Finder.",
                details={"artifact_id": artifact_id, "reason": str(error)},
            ) from error
        except ArtifactBlobUnavailableError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.BLOB_UNAVAILABLE,
                message="The retained artifact is missing or corrupt.",
                details={"artifact_id": artifact_id, "reason": str(error)},
            ) from error
        except FinderRevealFailedError as error:
            raise ApiProblem(
                status_code=503,
                code=ApiErrorCode.CAPABILITY_UNAVAILABLE,
                message="Finder is temporarily unable to reveal the artifact.",
                retryable=True,
                details={
                    "artifact_id": artifact_id,
                    "reason": str(error),
                    "action": "Retry reveal, or download the retained artifact instead.",
                },
            ) from error

    @app.delete(
        "/api/projects/{project_id}/artifacts/{artifact_id}",
        tags=["artifacts"],
        response_model=ArtifactDeletionResource,
        responses=ERROR_RESPONSES,
    )
    def delete_project_artifact(
        project_id: ResourceId,
        artifact_id: ResourceId,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> ArtifactDeletionResource:
        try:
            return delete_regenerable_artifact(
                connection=connection,
                store=store,
                project_id=project_id,
                artifact_id=artifact_id,
            )
        except RecordNotFoundError as error:
            raise _artifact_not_found(project_id, artifact_id) from error
        except (ImmutableArtifactError, ArtifactNotRegenerableError) as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="This artifact cannot be deleted safely.",
                details={"artifact_id": artifact_id, "reason": str(error)},
            ) from error

    @app.get(
        "/api/workspace/health",
        tags=["workspace"],
        response_model=WorkspaceHealthReport,
        responses=ERROR_RESPONSES,
    )
    def workspace_health(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> WorkspaceHealthReport:
        return WorkspaceHealthService(connection, store).inspect(
            tools=tuple(discover_capabilities())
        )

    @app.get(
        "/api/workspace/recovery/latest",
        tags=["workspace"],
        response_model=StartupReconciliationReport,
        responses=ERROR_RESPONSES,
    )
    def latest_startup_reconciliation(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> StartupReconciliationReport:
        report = StartupRecoveryService(connection, store).latest()
        if report is None:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="No startup reconciliation evidence has been recorded yet.",
                details={"resource": "startup_reconciliation"},
            )
        return report

    @app.get(
        "/api/workspace/recovery/history",
        tags=["workspace"],
        response_model=ReconciliationHistory,
        responses=ERROR_RESPONSES,
    )
    def startup_reconciliation_history(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> ReconciliationHistory:
        return StartupRecoveryService(connection, store).history(limit=limit)

    @app.post(
        "/api/workspace/recovery/reconcile",
        tags=["workspace"],
        response_model=StartupReconciliationReport,
        responses=ERROR_RESPONSES,
    )
    def reconcile_workspace_now(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> StartupReconciliationReport:
        # This repair is intentionally non-destructive: it may fail abandoned active jobs,
        # but it never deletes files or claims that an interrupted worker resumed.
        return StartupRecoveryService(connection, store).reconcile()

    @app.post(
        "/api/workspace/gc/plan",
        tags=["workspace"],
        response_model=GCPlan,
        responses=ERROR_RESPONSES,
    )
    def garbage_collection_plan(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
        minimum_age_seconds: Annotated[
            int,
            Query(ge=0, le=MAX_MINIMUM_AGE_SECONDS),
        ] = DEFAULT_MINIMUM_AGE_SECONDS,
    ) -> GCPlan:
        return WorkspaceHealthService(connection, store).plan_garbage_collection(
            minimum_age_seconds=minimum_age_seconds
        )

    @app.post(
        "/api/workspace/gc/apply",
        tags=["workspace"],
        response_model=GCExecution,
        responses=ERROR_RESPONSES,
    )
    def apply_garbage_collection(
        request_body: Annotated[GCApplyRequest, Body()],
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> GCExecution:
        try:
            return WorkspaceHealthService(connection, store).apply_current_plan(
                plan_sha256=request_body.plan_sha256,
                minimum_age_seconds=request_body.minimum_age_seconds,
            )
        except StaleGCPlanError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="The workspace changed after this cleanup plan was reviewed.",
                details={"action": "Generate and review a fresh dry-run plan."},
            ) from error
        except InvalidGCPlanError as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The cleanup plan fingerprint is invalid.",
            ) from error

    return app


def _filament_not_found(filament_id: str) -> ApiProblem:
    return ApiProblem(
        status_code=404,
        code=ApiErrorCode.NOT_FOUND,
        message="The requested filament does not exist.",
        details={"resource": "filament", "id": filament_id},
    )


def _artifact_not_found(project_id: str, artifact_id: str) -> ApiProblem:
    return ApiProblem(
        status_code=404,
        code=ApiErrorCode.NOT_FOUND,
        message="The requested artifact does not exist in this project.",
        details={"resource": "artifact", "project_id": project_id, "id": artifact_id},
    )


def _revision_not_found(*, project_id: str, revision_id: Optional[str] = None) -> ApiProblem:
    details = {"resource": "revision" if revision_id is not None else "project"}
    details["project_id"] = project_id
    if revision_id is not None:
        details["id"] = revision_id
    return ApiProblem(
        status_code=404,
        code=ApiErrorCode.NOT_FOUND,
        message=(
            "The requested revision does not exist in this project."
            if revision_id is not None
            else "The requested project does not exist."
        ),
        details=details,
    )


def _incompatible_editor_history(
    error: IncompatibleSelectorError,
    *,
    project_id: str,
) -> ApiProblem:
    return ApiProblem(
        status_code=422,
        code=ApiErrorCode.INCOMPATIBLE_EDITOR_HISTORY,
        message="The saved editor history belongs to a different image configuration.",
        details={
            "project_id": project_id,
            "reason": "selector_config_mismatch",
            "operation_count": error.operation_count,
            "conflict_count": len(error.conflicts),
            "action": "clear_or_restore_config",
        },
    )


app = create_app()
