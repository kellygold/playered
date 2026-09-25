"""HTTP surface for immutable physical calibration evidence."""

# ruff: noqa: UP045 -- FastAPI evaluates this dependency annotation on Python 3.9.
import sqlite3
from collections.abc import Callable, Iterator
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse

from image23mf.api.errors import ApiProblem
from image23mf.calibration.catalogs import (
    CalibrationCatalogCollection,
    CalibrationCatalogConflictError,
    CalibrationCatalogError,
    CalibrationCatalogRepository,
    CalibrationCatalogVersionResource,
)
from image23mf.calibration.drafts import (
    CalibrationDraftAttestRequest,
    CalibrationDraftCollection,
    CalibrationDraftConflictError,
    CalibrationDraftCorruptError,
    CalibrationDraftCreateRequest,
    CalibrationDraftInvalidError,
    CalibrationDraftMutationRequest,
    CalibrationDraftNotFoundError,
    CalibrationDraftResource,
    CalibrationDraftService,
    CalibrationDraftUpdate,
    CalibrationSpecimenResource,
)
from image23mf.calibration.evidence import (
    MAX_BUNDLE_BYTES,
    MAX_MEMBER_BYTES,
    CalibrationEvidenceError,
    CalibrationEvidenceRole,
    parse_calibration_evidence_bundle,
)
from image23mf.calibration.models import PrintabilityProfileCatalog
from image23mf.calibration.promotion import (
    CalibrationPromotionError,
    CalibrationPromotionResult,
    CalibrationPromotionService,
    CalibrationProposalCollection,
    CalibrationProposalConflictError,
    CalibrationProposalNotFoundError,
    CalibrationProposalResource,
)
from image23mf.calibration.registry import (
    CalibrationMemberResource,
    CalibrationRegistry,
    CalibrationRegistryConflictError,
    CalibrationRegistryCorruptError,
    CalibrationRegistryError,
    CalibrationRegistryNotFoundError,
    CalibrationRunResource,
)
from image23mf.contracts.api import ApiErrorCode
from image23mf.contracts.calibration import (
    AcceptCalibrationProposalRequest,
    CalibrationArtifactApiResource,
    CalibrationDraftFinalizeApiResource,
    CalibrationImportApiResource,
    CalibrationMemberApiResource,
    CalibrationRunApiResource,
    CalibrationRunCollection,
    CalibrationSourceBundleApiResource,
    CreateCalibrationProposalRequest,
    RejectCalibrationProposalRequest,
)
from image23mf.storage import ContentAddressedStore

_ZIP_MEDIA_TYPES = frozenset(
    {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
)


def create_calibration_router(
    *,
    database: Callable[[], Iterator[sqlite3.Connection]],
    blob_store: Callable[[], ContentAddressedStore],
    catalog: Callable[[], PrintabilityProfileCatalog],
    error_responses: dict,
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/calibration/specimens/{profile_id}",
        tags=["calibration"],
        response_model=CalibrationSpecimenResource,
        responses=error_responses,
    )
    def get_specimen(
        profile_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationSpecimenResource:
        try:
            return CalibrationDraftService(connection, store).specimen(profile_id)
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error

    @router.get(
        "/api/calibration/specimens/{profile_id}/coupon.svg",
        tags=["calibration"],
        responses=error_responses,
    )
    def get_specimen_svg(
        profile_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> Response:
        return _specimen_response(connection, store, profile_id, "svg")

    @router.get(
        "/api/calibration/specimens/{profile_id}/coupon.png",
        tags=["calibration"],
        responses=error_responses,
    )
    def get_specimen_png(
        profile_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> Response:
        return _specimen_response(connection, store, profile_id, "png")

    @router.get(
        "/api/calibration/specimens/{profile_id}/bundle",
        tags=["calibration"],
        responses=error_responses,
    )
    def get_specimen_bundle(
        profile_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> Response:
        return _specimen_response(connection, store, profile_id, "bundle")

    @router.post(
        "/api/calibration/drafts",
        tags=["calibration"],
        status_code=201,
        response_model=CalibrationDraftResource,
        responses=error_responses,
    )
    def create_draft(
        payload: CalibrationDraftCreateRequest,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationDraftResource:
        try:
            return CalibrationDraftService(connection, store).create(payload)
        except CalibrationDraftConflictError as error:
            raise _conflict(str(error)) from error
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error

    @router.get(
        "/api/calibration/drafts",
        tags=["calibration"],
        response_model=CalibrationDraftCollection,
        responses=error_responses,
    )
    def list_drafts(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationDraftCollection:
        try:
            return CalibrationDraftService(connection, store).list()
        except CalibrationDraftCorruptError as error:
            raise _conflict(str(error)) from error

    @router.get(
        "/api/calibration/drafts/{draft_id}",
        tags=["calibration"],
        response_model=CalibrationDraftResource,
        responses=error_responses,
    )
    def get_draft(
        draft_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationDraftResource:
        try:
            return CalibrationDraftService(connection, store).get(draft_id)
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationDraftCorruptError as error:
            raise _conflict(str(error)) from error

    @router.put(
        "/api/calibration/drafts/{draft_id}",
        tags=["calibration"],
        response_model=CalibrationDraftResource,
        responses=error_responses,
    )
    def update_draft(
        draft_id: str,
        payload: CalibrationDraftUpdate,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationDraftResource:
        try:
            return CalibrationDraftService(connection, store).update(draft_id, payload)
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationDraftConflictError as error:
            raise _conflict(str(error)) from error
        except CalibrationDraftInvalidError as error:
            raise _invalid(str(error)) from error

    @router.put(
        "/api/calibration/drafts/{draft_id}/members/{role}/{ordinal}",
        tags=["calibration"],
        response_model=CalibrationDraftResource,
        responses=error_responses,
    )
    async def upload_draft_member(
        draft_id: str,
        role: CalibrationEvidenceRole,
        ordinal: int,
        request: Request,
        expected_generation: Annotated[int, Query(gt=0)],
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationDraftResource:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) <= 0 or int(declared) > MAX_MEMBER_BYTES:
                    raise _member_too_large()
            except ValueError as error:
                raise _invalid("Calibration attachment has an invalid content length.") from error
        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_MEMBER_BYTES:
                raise _member_too_large()
            chunks.append(chunk)
        try:
            return CalibrationDraftService(connection, store).upload(
                draft_id,
                role=role,
                ordinal=ordinal,
                media_type=media_type,
                payload=b"".join(chunks),
                expected_generation=expected_generation,
            )
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationDraftConflictError as error:
            raise _conflict(str(error)) from error
        except CalibrationDraftInvalidError as error:
            raise _invalid(str(error)) from error

    @router.delete(
        "/api/calibration/drafts/{draft_id}/members/{role}/{ordinal}",
        tags=["calibration"],
        response_model=CalibrationDraftResource,
        responses=error_responses,
    )
    def remove_draft_member(
        draft_id: str,
        role: CalibrationEvidenceRole,
        ordinal: int,
        expected_generation: Annotated[int, Query(gt=0)],
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationDraftResource:
        try:
            return CalibrationDraftService(connection, store).remove(
                draft_id,
                role=role,
                ordinal=ordinal,
                expected_generation=expected_generation,
            )
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationDraftConflictError as error:
            raise _conflict(str(error)) from error
        except CalibrationDraftInvalidError as error:
            raise _invalid(str(error)) from error

    @router.get(
        "/api/calibration/drafts/{draft_id}/members/{member_id}",
        tags=["calibration"],
        responses=error_responses,
    )
    def download_draft_member(
        draft_id: str,
        member_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> FileResponse:
        try:
            member, relative_path = CalibrationDraftService(connection, store).member(
                draft_id, member_id
            )
            return FileResponse(
                store.path_for(relative_path),
                media_type=member.media_type,
                filename=member.filename,
            )
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error
        except (CalibrationDraftCorruptError, FileNotFoundError) as error:
            raise _conflict("The retained calibration draft attachment is unavailable.") from error

    @router.post(
        "/api/calibration/drafts/{draft_id}/attest",
        tags=["calibration"],
        response_model=CalibrationDraftResource,
        responses=error_responses,
    )
    def attest_draft(
        draft_id: str,
        payload: CalibrationDraftAttestRequest,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationDraftResource:
        try:
            return CalibrationDraftService(connection, store).attest(draft_id, payload)
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationDraftConflictError as error:
            raise _conflict(str(error)) from error
        except CalibrationDraftInvalidError as error:
            raise _invalid(str(error)) from error

    @router.post(
        "/api/calibration/drafts/{draft_id}/finalize",
        tags=["calibration"],
        response_model=CalibrationDraftFinalizeApiResource,
        responses=error_responses,
    )
    def finalize_draft(
        draft_id: str,
        payload: CalibrationDraftMutationRequest,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationDraftFinalizeApiResource:
        try:
            result = CalibrationDraftService(connection, store).finalize(draft_id, payload)
            return CalibrationDraftFinalizeApiResource(
                draft=result.draft,
                run=_run_resource(result.imported.run),
                duplicate=result.imported.duplicate,
            )
        except CalibrationDraftNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationDraftConflictError as error:
            raise _conflict(str(error)) from error
        except (
            CalibrationDraftInvalidError,
            CalibrationEvidenceError,
            CalibrationRegistryError,
        ) as error:
            raise _invalid(str(error)) from error

    @router.post(
        "/api/calibration/runs/import",
        tags=["calibration"],
        status_code=201,
        response_model=CalibrationImportApiResource,
        responses=error_responses,
    )
    async def import_run(
        request: Request,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationImportApiResource:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in _ZIP_MEDIA_TYPES:
            raise ApiProblem(
                status_code=415,
                code=ApiErrorCode.UNSUPPORTED_MEDIA,
                message="Calibration evidence must be uploaded as a ZIP bundle.",
                details={"accepted_media_types": sorted(_ZIP_MEDIA_TYPES)},
            )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise _invalid("Calibration evidence has an invalid content length.") from error
            if declared_size < 0:
                raise _invalid("Calibration evidence has an invalid content length.")
            if declared_size > MAX_BUNDLE_BYTES:
                raise _too_large()
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BUNDLE_BYTES:
                raise _too_large()
            chunks.append(chunk)
        try:
            bundle = b"".join(chunks)
            parsed = parse_calibration_evidence_bundle(bundle)
            retained = CalibrationCatalogRepository(connection).get(
                parsed.manifest.catalog_fingerprint
            )
            result = CalibrationRegistry(connection, store, retained.catalog).import_bundle(bundle)
            return CalibrationImportApiResource(
                run=_run_resource(result.run), duplicate=result.duplicate
            )
        except CalibrationRegistryConflictError as error:
            raise _conflict(str(error)) from error
        except (CalibrationEvidenceError, CalibrationRegistryError, ValueError) as error:
            raise _invalid(str(error)) from error

    @router.get(
        "/api/printability-profile-catalogs",
        tags=["calibration"],
        response_model=CalibrationCatalogCollection,
        responses=error_responses,
    )
    def list_catalogs(
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> CalibrationCatalogCollection:
        try:
            return CalibrationCatalogRepository(connection).list()
        except CalibrationCatalogError as error:
            raise _conflict(str(error)) from error

    @router.get(
        "/api/printability-profile-catalogs/{fingerprint}",
        tags=["calibration"],
        response_model=CalibrationCatalogVersionResource,
        responses=error_responses,
    )
    def get_catalog(
        fingerprint: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
    ) -> CalibrationCatalogVersionResource:
        try:
            return CalibrationCatalogRepository(connection).get(fingerprint)
        except ValueError as error:
            raise _not_found(str(error)) from error
        except CalibrationCatalogError as error:
            raise _conflict(str(error)) from error

    @router.post(
        "/api/calibration/proposals",
        tags=["calibration"],
        status_code=201,
        response_model=CalibrationProposalResource,
        responses=error_responses,
    )
    def create_proposal(
        payload: CreateCalibrationProposalRequest,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationProposalResource:
        try:
            return CalibrationPromotionService(connection, store).derive(
                profile_id=payload.profile_id,
                run_ids=payload.run_ids,
                expected_catalog_fingerprint=payload.expected_catalog_fingerprint,
            )
        except CalibrationProposalConflictError as error:
            raise _conflict(str(error)) from error
        except (CalibrationPromotionError, CalibrationRegistryError, ValueError) as error:
            raise _invalid(str(error)) from error

    @router.get(
        "/api/calibration/proposals",
        tags=["calibration"],
        response_model=CalibrationProposalCollection,
        responses=error_responses,
    )
    def list_proposals(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
        profile_id: Annotated[Optional[str], Query(max_length=120)] = None,
    ) -> CalibrationProposalCollection:
        try:
            return CalibrationPromotionService(connection, store).list(profile_id=profile_id)
        except CalibrationPromotionError as error:
            raise _conflict(str(error)) from error

    @router.get(
        "/api/calibration/proposals/{proposal_id}",
        tags=["calibration"],
        response_model=CalibrationProposalResource,
        responses=error_responses,
    )
    def get_proposal(
        proposal_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationProposalResource:
        try:
            return CalibrationPromotionService(connection, store).get(proposal_id)
        except CalibrationProposalNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationPromotionError as error:
            raise _conflict(str(error)) from error

    @router.post(
        "/api/calibration/proposals/{proposal_id}/accept",
        tags=["calibration"],
        response_model=CalibrationPromotionResult,
        responses=error_responses,
    )
    def accept_proposal(
        proposal_id: str,
        payload: AcceptCalibrationProposalRequest,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationPromotionResult:
        try:
            return CalibrationPromotionService(connection, store).accept(
                proposal_id,
                expected_catalog_fingerprint=payload.expected_catalog_fingerprint,
                reviewer=payload.reviewer,
                reason=payload.reason,
            )
        except CalibrationProposalNotFoundError as error:
            raise _not_found(str(error)) from error
        except (CalibrationProposalConflictError, CalibrationCatalogConflictError) as error:
            raise _conflict(str(error)) from error
        except CalibrationPromotionError as error:
            raise _invalid(str(error)) from error

    @router.post(
        "/api/calibration/proposals/{proposal_id}/reject",
        tags=["calibration"],
        response_model=CalibrationProposalResource,
        responses=error_responses,
    )
    def reject_proposal(
        proposal_id: str,
        payload: RejectCalibrationProposalRequest,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationProposalResource:
        try:
            return CalibrationPromotionService(connection, store).reject(
                proposal_id, reviewer=payload.reviewer, reason=payload.reason
            )
        except CalibrationProposalNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationProposalConflictError as error:
            raise _conflict(str(error)) from error
        except CalibrationPromotionError as error:
            raise _invalid(str(error)) from error

    @router.get(
        "/api/calibration/runs",
        tags=["calibration"],
        response_model=CalibrationRunCollection,
        responses=error_responses,
    )
    def list_runs(
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
        profile_id: Annotated[Optional[str], Query(max_length=120)] = None,
    ) -> CalibrationRunCollection:
        try:
            items = CalibrationRegistry(connection, store, catalog()).list(profile_id=profile_id)
            resources = tuple(_run_resource(item) for item in items)
            return CalibrationRunCollection(items=resources, total=len(resources))
        except CalibrationRegistryCorruptError as error:
            raise _conflict(str(error)) from error

    @router.get(
        "/api/calibration/runs/{run_id}",
        tags=["calibration"],
        response_model=CalibrationRunApiResource,
        responses=error_responses,
    )
    def get_run(
        run_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> CalibrationRunApiResource:
        try:
            return _run_resource(CalibrationRegistry(connection, store, catalog()).get(run_id))
        except CalibrationRegistryNotFoundError as error:
            raise _not_found(str(error)) from error
        except CalibrationRegistryCorruptError as error:
            raise _conflict(str(error)) from error

    @router.get(
        "/api/calibration/runs/{run_id}/bundle",
        tags=["calibration"],
        responses=error_responses,
    )
    def download_bundle(
        run_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> FileResponse:
        try:
            run = CalibrationRegistry(connection, store, catalog()).get(run_id)
            return FileResponse(
                store.path_for(run.source_bundle.relative_path),
                media_type=run.source_bundle.media_type,
                filename=f"{run.id}-evidence.zip",
            )
        except CalibrationRegistryNotFoundError as error:
            raise _not_found(str(error)) from error
        except (CalibrationRegistryCorruptError, FileNotFoundError) as error:
            raise _conflict("The retained calibration evidence bundle is unavailable.") from error

    @router.get(
        "/api/calibration/runs/{run_id}/members/{member_id}",
        tags=["calibration"],
        responses=error_responses,
    )
    def download_member(
        run_id: str,
        member_id: str,
        connection: Annotated[sqlite3.Connection, Depends(database)],
        store: Annotated[ContentAddressedStore, Depends(blob_store)],
    ) -> FileResponse:
        registry = CalibrationRegistry(connection, store, catalog())
        try:
            member = registry.member(run_id, member_id)
            return FileResponse(
                store.path_for(member.relative_path),
                media_type=member.media_type,
                filename=member.filename,
            )
        except CalibrationRegistryNotFoundError as error:
            raise _not_found(str(error)) from error
        except (CalibrationRegistryCorruptError, FileNotFoundError) as error:
            raise _conflict("The retained calibration evidence member is unavailable.") from error

    return router


def _run_resource(run: CalibrationRunResource) -> CalibrationRunApiResource:
    return CalibrationRunApiResource(
        id=run.id,
        artifact=CalibrationArtifactApiResource(
            id=run.artifact.id,
            catalog_id=run.artifact.catalog_id,
            catalog_version=run.artifact.catalog_version,
            catalog_fingerprint=run.artifact.catalog_fingerprint,
            profile_id=run.artifact.profile_id,
            profile_fingerprint=run.artifact.profile_fingerprint,
            artifact_fingerprint=run.artifact.artifact_fingerprint,
            manifest=run.artifact.manifest,
            members=tuple(_member_resource(run.id, item) for item in run.artifact.members),
            created_at=run.artifact.created_at,
        ),
        catalog_id=run.catalog_id,
        catalog_version=run.catalog_version,
        catalog_fingerprint=run.catalog_fingerprint,
        profile_id=run.profile_id,
        profile_fingerprint=run.profile_fingerprint,
        printer_id=run.printer_id,
        nozzle_id=run.nozzle_id,
        material_class=run.material_class,
        process_fingerprint=run.process_fingerprint,
        record_sha256=run.record_sha256,
        evidence_sha256=run.evidence_sha256,
        evidence=run.evidence,
        members=tuple(_member_resource(run.id, item) for item in run.members),
        source_bundle=CalibrationSourceBundleApiResource(
            filename=f"{run.id}-evidence.zip",
            sha256=run.source_bundle.sha256,
            byte_size=run.source_bundle.byte_size,
            media_type=run.source_bundle.media_type,
            download_url=f"/api/calibration/runs/{run.id}/bundle",
        ),
        imported_at=run.imported_at,
        integrity=run.integrity,
    )


def _member_resource(run_id: str, item: CalibrationMemberResource) -> CalibrationMemberApiResource:
    return CalibrationMemberApiResource(
        id=item.id,
        role=item.role,
        ordinal=item.ordinal,
        filename=item.filename,
        sha256=item.sha256,
        byte_size=item.byte_size,
        media_type=item.media_type,
        download_url=f"/api/calibration/runs/{run_id}/members/{item.id}",
    )


def _invalid(message: str) -> ApiProblem:
    return ApiProblem(
        status_code=422,
        code=ApiErrorCode.VALIDATION_ERROR,
        message=message or "Calibration evidence is invalid.",
    )


def _conflict(message: str) -> ApiProblem:
    return ApiProblem(status_code=409, code=ApiErrorCode.CONFLICT, message=message)


def _not_found(message: str) -> ApiProblem:
    return ApiProblem(status_code=404, code=ApiErrorCode.NOT_FOUND, message=message)


def _too_large() -> ApiProblem:
    return ApiProblem(
        status_code=413,
        code=ApiErrorCode.RESOURCE_LIMIT,
        message="Calibration evidence exceeds the upload limit.",
        details={"maximum_bytes": MAX_BUNDLE_BYTES},
    )


def _member_too_large() -> ApiProblem:
    return ApiProblem(
        status_code=413,
        code=ApiErrorCode.RESOURCE_LIMIT,
        message="Calibration attachment exceeds the upload limit.",
        details={"maximum_bytes": MAX_MEMBER_BYTES},
    )


def _specimen_response(
    connection: sqlite3.Connection,
    store: ContentAddressedStore,
    profile_id: str,
    kind: str,
) -> Response:
    try:
        payload, media_type, filename = CalibrationDraftService(connection, store).specimen_bytes(
            profile_id, kind
        )
    except CalibrationDraftNotFoundError as error:
        raise _not_found(str(error)) from error
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
