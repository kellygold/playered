"""Composable HTTP routes for master-canvas mural plans."""

# ruff: noqa: UP045 -- Python 3.9 is supported by the application.

import sqlite3
from collections.abc import Callable, Iterator
from typing import Annotated, Any, Optional, Union

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from image23mf.api.errors import ApiProblem
from image23mf.contracts.api import ApiErrorCode
from image23mf.contracts.mural import (
    MuralBuildResult,
    MuralBuildStartResponse,
    MuralPlanPreviewResource,
    MuralPlanResource,
    MuralPlanSettings,
    SaveMuralPlanRequest,
    StartMuralBuildRequest,
)
from image23mf.mural.builds import (
    InvalidMuralBuildRequestError,
    MuralBuildArtifactUnavailableError,
    MuralBuildService,
)
from image23mf.mural.provenance import InvalidMuralPlanSourceError
from image23mf.mural.repository import StaleMuralPlanError
from image23mf.mural.service import MuralPlanningError, MuralPlanningService
from image23mf.profiles import ProfileCatalogService
from image23mf.storage import ContentAddressedStore, RecordNotFoundError

DatabaseDependency = Callable[[], Iterator[sqlite3.Connection]]
BlobStoreDependency = Callable[[], ContentAddressedStore]
ProfileDependency = Callable[[], ProfileCatalogService]
MuralBuildDependency = Callable[[], MuralBuildService]
ResourceId = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]


def create_mural_router(
    *,
    database: DatabaseDependency,
    blob_store: BlobStoreDependency,
    profiles: ProfileDependency,
    mural_builds: Optional[MuralBuildDependency] = None,
    error_responses: Optional[dict[Union[int, str], dict[str, Any]]] = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}", tags=["murals"])
    responses = error_responses or {}

    def service(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
        profile_service: Annotated[ProfileCatalogService, Depends(profiles)],
    ) -> MuralPlanningService:
        return MuralPlanningService(connection, store, profile_service)

    @router.get("/mural-plan", response_model=MuralPlanResource, responses=responses)
    def get_mural_plan(
        project_id: ResourceId,
        planner: Annotated[MuralPlanningService, Depends(service)],
    ) -> MuralPlanResource:
        record = planner.get(project_id)
        if record is None:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="This project does not have a saved mural plan.",
                details={"resource": "mural_plan", "project_id": project_id},
            )
        return record

    @router.put("/mural-plan", response_model=MuralPlanResource, responses=responses)
    def save_mural_plan(
        project_id: ResourceId,
        body: SaveMuralPlanRequest,
        planner: Annotated[MuralPlanningService, Depends(service)],
    ) -> MuralPlanResource:
        try:
            return planner.save(project_id, body)
        except StaleMuralPlanError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="The mural plan changed in another editor session.",
                details={
                    "resource": "mural_plan",
                    "project_id": project_id,
                    "expected_generation": error.expected_generation,
                    "current_generation": error.current_generation,
                },
            ) from error
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The project or processed preview artifact no longer exists.",
                details={"resource": "mural_plan", "project_id": project_id},
            ) from error
        except (MuralPlanningError, InvalidMuralPlanSourceError) as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The current processed master cannot produce this mural plan.",
                details={"reason": str(error)},
            ) from error

    @router.post(
        "/mural-plan/preview", response_model=MuralPlanPreviewResource, responses=responses
    )
    def preview_mural_plan(
        project_id: ResourceId,
        body: MuralPlanSettings,
        planner: Annotated[MuralPlanningService, Depends(service)],
    ) -> MuralPlanPreviewResource:
        try:
            return planner.preview(project_id, body)
        except RecordNotFoundError as error:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="The project or processed preview artifact no longer exists.",
                details={"resource": "mural_plan", "project_id": project_id},
            ) from error
        except (MuralPlanningError, InvalidMuralPlanSourceError) as error:
            raise ApiProblem(
                status_code=422,
                code=ApiErrorCode.VALIDATION_ERROR,
                message="The current processed master cannot preview this mural plan.",
                details={"reason": str(error)},
            ) from error

    @router.delete("/mural-plan", status_code=204, responses=responses)
    def delete_mural_plan(
        project_id: ResourceId,
        expected_generation: Annotated[int, Query(ge=1)],
        planner: Annotated[MuralPlanningService, Depends(service)],
    ) -> Response:
        try:
            deleted = planner.delete(project_id, expected_generation=expected_generation)
        except StaleMuralPlanError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.CONFLICT,
                message="The mural plan changed in another editor session.",
                details={
                    "resource": "mural_plan",
                    "project_id": project_id,
                    "expected_generation": error.expected_generation,
                    "current_generation": error.current_generation,
                },
            ) from error
        if not deleted:
            raise ApiProblem(
                status_code=404,
                code=ApiErrorCode.NOT_FOUND,
                message="This project does not have a saved mural plan.",
                details={"resource": "mural_plan", "project_id": project_id},
            )
        return Response(status_code=204)

    if mural_builds is not None:

        @router.post(
            "/mural-builds",
            status_code=202,
            response_model=MuralBuildStartResponse,
            responses=responses,
        )
        def start_mural_build(
            project_id: ResourceId,
            body: StartMuralBuildRequest,
            request: Request,
            builds: Annotated[MuralBuildService, Depends(mural_builds)],
        ) -> MuralBuildStartResponse:
            try:
                return builds.start(
                    project_id=project_id,
                    request=body,
                    manager=request.app.state.worker_manager,
                )
            except RecordNotFoundError as error:
                raise ApiProblem(
                    status_code=404,
                    code=ApiErrorCode.NOT_FOUND,
                    message="The mural plan, project, or geometry artifact does not exist.",
                    details={"project_id": project_id},
                ) from error
            except InvalidMuralBuildRequestError as error:
                raise ApiProblem(
                    status_code=409,
                    code=ApiErrorCode.CONFLICT,
                    message="Mural build requires the current saved plan and exact geometry.",
                    details={
                        "project_id": project_id,
                        "expected_plan_generation": body.expected_plan_generation,
                        "current_plan_generation": error.current_plan_generation,
                        "expected_request_fingerprint": body.expected_request_fingerprint,
                        "current_request_fingerprint": error.current_request_fingerprint,
                        "reason": str(error),
                        "action": "Reload the plan and run geometry preflight again.",
                    },
                ) from error
            except MuralBuildArtifactUnavailableError as error:
                raise ApiProblem(
                    status_code=409,
                    code=ApiErrorCode.BLOB_UNAVAILABLE,
                    message="A required mural source artifact is missing or corrupt.",
                    details={
                        "project_id": project_id,
                        "reason": str(error),
                        "action": "Regenerate geometry from the current preview.",
                    },
                ) from error

        @router.get(
            "/mural-builds/{job_id}",
            response_model=MuralBuildResult,
            responses=responses,
        )
        def mural_build_result(
            project_id: ResourceId,
            job_id: ResourceId,
            builds: Annotated[MuralBuildService, Depends(mural_builds)],
        ) -> MuralBuildResult:
            try:
                return builds.result(project_id=project_id, job_id=job_id)
            except RecordNotFoundError as error:
                raise ApiProblem(
                    status_code=404,
                    code=ApiErrorCode.NOT_FOUND,
                    message="The requested mural build does not exist for this project.",
                    details={"project_id": project_id, "job_id": job_id},
                ) from error
            except MuralBuildArtifactUnavailableError as error:
                raise ApiProblem(
                    status_code=409,
                    code=ApiErrorCode.BLOB_UNAVAILABLE,
                    message="The mural build result is incomplete, missing, or corrupt.",
                    details={
                        "project_id": project_id,
                        "job_id": job_id,
                        "reason": str(error),
                    },
                ) from error

    return router
