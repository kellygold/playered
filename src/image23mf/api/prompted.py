"""HTTP surface for immutable prompted-edit alternatives."""

import sqlite3
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from image23mf.api.errors import ApiProblem
from image23mf.contracts.api import ApiErrorCode
from image23mf.contracts.prompted import (
    AcceptPromptedAlternativeRequest,
    AcceptPromptedAlternativeResponse,
    ExecutePromptedEditRequest,
    PreparePromptedEditRequest,
    PreparePromptedEditResponse,
    PromptedAlternativeCollection,
    PromptedAlternativeResource,
    PromptedExecutionStart,
    PromptedSessionResource,
    RejectPromptedEditRequest,
)
from image23mf.prompted_edits.lifecycle import PromptedEditLifecycle
from image23mf.prompted_edits.repository import (
    PromptedAlternativeRecord,
    PromptedEditConflictError,
    PromptedEditRepository,
    PromptedSessionRecord,
)
from image23mf.prompted_edits.service import PromptedEditError
from image23mf.storage import (
    ContentAddressedStore,
    InvalidPublicationError,
    RecordNotFoundError,
    RevisionPublisher,
    StaleDraftError,
)


def create_prompted_router(
    *,
    database: Callable[[], Iterator[sqlite3.Connection]],
    blob_store: Callable[[], ContentAddressedStore],
    lifecycle: Callable[[Request], PromptedEditLifecycle],
    error_responses: dict,
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/prompted-edit/providers",
        tags=["prompted-edits"],
        responses=error_responses,
    )
    def providers(request: Request):
        return {"items": lifecycle(request).prompted_edits.providers()}

    @router.post(
        "/api/projects/{project_id}/prompted-edits/prepare",
        tags=["prompted-edits"],
        response_model=PreparePromptedEditResponse,
        responses=error_responses,
    )
    def prepare(project_id: str, payload: PreparePromptedEditRequest, request: Request):
        try:
            session = lifecycle(request).prepare(project_id=project_id, payload=payload)
            return PreparePromptedEditResponse(
                session_id=session.id,
                request_sha256=session.request_sha256,
                provider=session.provider,
                disclosure=session.disclosure,
            )
        except RecordNotFoundError as error:
            raise _not_found(error) from error
        except (ValueError, PromptedEditError) as error:
            raise _invalid(str(error)) from error

    @router.post(
        "/api/prompted-edits/{session_id}/execute",
        tags=["prompted-edits"],
        response_model=PromptedExecutionStart,
        status_code=202,
        responses=error_responses,
    )
    def execute(
        session_id: str,
        payload: ExecutePromptedEditRequest,
        request: Request,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ):
        try:
            job = lifecycle(request).execute(
                session_id,
                request_sha256=payload.request_sha256,
                disclosure_sha256=payload.disclosure_sha256,
            )
            session = PromptedEditRepository(connection, store).get(session_id)
            return PromptedExecutionStart(session=_resource(connection, session), job=job)
        except RecordNotFoundError as error:
            raise _not_found(error) from error
        except PromptedEditConflictError as error:
            raise _conflict(str(error)) from error

    @router.post(
        "/api/prompted-edits/{session_id}/cancel",
        tags=["prompted-edits"],
        response_model=PromptedSessionResource,
        responses=error_responses,
    )
    def cancel(
        session_id: str,
        request: Request,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ):
        try:
            lifecycle(request).cancel(session_id)
            return _resource(connection, PromptedEditRepository(connection, store).get(session_id))
        except RecordNotFoundError as error:
            raise _not_found(error) from error
        except PromptedEditConflictError as error:
            raise _conflict(str(error)) from error

    @router.post(
        "/api/prompted-edits/{session_id}/retry",
        tags=["prompted-edits"],
        response_model=PreparePromptedEditResponse,
        responses=error_responses,
    )
    def retry(session_id: str, request: Request):
        try:
            session = lifecycle(request).retry(session_id)
            return PreparePromptedEditResponse(
                session_id=session.id,
                request_sha256=session.request_sha256,
                provider=session.provider,
                disclosure=session.disclosure,
            )
        except RecordNotFoundError as error:
            raise _not_found(error) from error
        except PromptedEditConflictError as error:
            raise _conflict(str(error)) from error

    @router.post(
        "/api/prompted-edits/{session_id}/reject",
        tags=["prompted-edits"],
        response_model=PromptedSessionResource,
        responses=error_responses,
    )
    def reject(
        session_id: str,
        payload: RejectPromptedEditRequest,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ):
        try:
            session = PromptedEditRepository(connection, store).reject(
                session_id,
                reason=payload.reason,
            )
            return _resource(connection, session)
        except RecordNotFoundError as error:
            raise _not_found(error) from error
        except PromptedEditConflictError as error:
            raise _conflict(str(error)) from error

    @router.post(
        "/api/projects/{project_id}/prompted-edits/{session_id}/alternatives/{alternative_id}/accept",
        tags=["prompted-edits"],
        response_model=AcceptPromptedAlternativeResponse,
        responses=error_responses,
    )
    def accept(
        project_id: str,
        session_id: str,
        alternative_id: str,
        payload: AcceptPromptedAlternativeRequest,
        request: Request,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ):
        try:
            result = RevisionPublisher(connection, store).accept_prompted_alternative(
                project_id=project_id,
                session_id=session_id,
                alternative_id=alternative_id,
                expected_generation=payload.expected_draft_generation,
                engine_version=request.app.version,
                label=payload.label,
                notes=payload.notes,
            )
            session = PromptedEditRepository(connection, store).get(session_id)
            return AcceptPromptedAlternativeResponse(
                session=_resource(connection, session),
                revision_id=result.revision.id,
                draft_generation=result.continuation_draft.generation,
            )
        except RecordNotFoundError as error:
            raise _not_found(error) from error
        except StaleDraftError as error:
            raise ApiProblem(
                status_code=409,
                code=ApiErrorCode.STALE_DRAFT,
                message=str(error),
            ) from error
        except (InvalidPublicationError, PromptedEditConflictError, ValueError) as error:
            raise _conflict(str(error)) from error

    @router.get(
        "/api/projects/{project_id}/prompted-edits",
        tags=["prompted-edits"],
        response_model=PromptedAlternativeCollection,
        responses=error_responses,
    )
    def list_sessions(
        project_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ):
        sessions = PromptedEditRepository(connection, store).list_for_project(project_id)
        return PromptedAlternativeCollection(
            items=tuple(_resource(connection, item) for item in sessions)
        )

    @router.get(
        "/api/prompted-edits/{session_id}",
        tags=["prompted-edits"],
        response_model=PromptedSessionResource,
        responses=error_responses,
    )
    def get_session(
        session_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ):
        try:
            return _resource(connection, PromptedEditRepository(connection, store).get(session_id))
        except RecordNotFoundError as error:
            raise _not_found(error) from error

    @router.get(
        "/api/prompted-edits/{session_id}/alternatives/{alternative_id}/{evidence}",
        tags=["prompted-edits"],
        response_class=FileResponse,
        responses=error_responses,
    )
    def alternative_evidence(
        session_id: str,
        alternative_id: str,
        evidence: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ):
        try:
            session = PromptedEditRepository(connection, store).get(session_id)
            alternative = next(item for item in session.alternatives if item.id == alternative_id)
        except (RecordNotFoundError, StopIteration) as error:
            raise _not_found(error) from error
        blob = (
            alternative.changed_mask
            if evidence == "changed-mask"
            else alternative.changed_mask_preview
            if evidence == "changed-mask-preview"
            else None
        )
        if blob is None:
            raise _not_found(ValueError("unknown prompted evidence"))
        return FileResponse(store.path_for(blob.relative_path), media_type=blob.media_type)

    return router


def _resource(connection, session: PromptedSessionRecord) -> PromptedSessionResource:
    alternatives = tuple(_alternative_resource(connection, item) for item in session.alternatives)
    return PromptedSessionResource(
        id=session.id,
        project_id=session.project_id,
        parent_revision_id=session.parent_revision_id,
        retry_of_session_id=session.retry_of_session_id,
        status=session.status,
        request_sha256=session.request_sha256,
        selection_sha256=session.selection_sha256,
        provider=session.provider,
        disclosure=session.disclosure,
        prompt=session.request["prompt"],
        options=session.request.get("options", {}),
        seed=session.request.get("seed"),
        requested_alternative_count=session.request["alternative_count"],
        alternatives=alternatives,
        failures=session.failures,
        accepted_alternative_id=session.accepted_alternative_id,
        accepted_revision_id=session.accepted_revision_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _alternative_resource(connection, item: PromptedAlternativeRecord):
    row = connection.execute(
        "SELECT media_type FROM assets WHERE id = ?", (item.output_asset_id,)
    ).fetchone()
    return PromptedAlternativeResource(
        id=item.id,
        index=item.index,
        status=item.status,
        output_sha256=item.output_sha256,
        output_media_type=str(row["media_type"]),
        output_url=f"/api/assets/{item.output_asset_id}",
        changed_mask_sha256=item.changed_mask.sha256,
        changed_mask_url=(
            f"/api/prompted-edits/{item.session_id}/alternatives/{item.id}/changed-mask"
        ),
        changed_mask_preview_url=(
            f"/api/prompted-edits/{item.session_id}/alternatives/{item.id}/changed-mask-preview"
        ),
        changed_pixel_count=item.changed_pixel_count,
        width_px=item.width_px,
        height_px=item.height_px,
        provenance=item.provenance,
    )


def _not_found(error) -> ApiProblem:
    return ApiProblem(
        status_code=404,
        code=ApiErrorCode.NOT_FOUND,
        message=str(error),
    )


def _invalid(message: str) -> ApiProblem:
    return ApiProblem(status_code=422, code=ApiErrorCode.VALIDATION_ERROR, message=message)


def _conflict(message: str) -> ApiProblem:
    return ApiProblem(status_code=409, code=ApiErrorCode.CONFLICT, message=message)
