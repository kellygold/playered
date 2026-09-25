"""Application lifecycle for consented, asynchronous prompted-edit alternatives."""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from image23mf.contracts.editor import SourceRegionSelection, canonical_fingerprint
from image23mf.contracts.job import CropMode, load_job_config
from image23mf.contracts.jobs import JobFailure, JobStage, JobType
from image23mf.contracts.prompted import PreparePromptedEditRequest, PromptedSessionStatus
from image23mf.editor.selection import rasterize_canvas_selection
from image23mf.engine.transform import (
    CanonicalTransform,
    MillimetreSize,
    PixelSize,
    Rect,
)
from image23mf.prompted_edits.evidence import build_prompted_alternative_evidence
from image23mf.prompted_edits.models import (
    ConsentDecision,
    EgressDisclosure,
    PromptedEditAsset,
    PromptedEditRequest,
)
from image23mf.prompted_edits.repository import (
    PromptedEditConflictError,
    PromptedEditRepository,
    PromptedSessionRecord,
    StoredPromptedAlternative,
)
from image23mf.prompted_edits.service import (
    PromptedEditCanceledError,
    PromptedEditError,
    PromptedEditService,
)
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    RevisionRepository,
    open_database,
)
from image23mf.storage.repositories import canonical_json
from image23mf.workers import (
    JobContext,
    LocalWorkerManager,
    WorkerCanceledError,
    WorkerJobError,
    WorkerResult,
)


class GrantedConsent:
    def __init__(self, decision: ConsentDecision) -> None:
        self.decision = decision

    def authorize(self, disclosure: EgressDisclosure) -> ConsentDecision:
        return self.decision


class PromptedEditLifecycle:
    def __init__(
        self,
        *,
        database_path: Path,
        blob_store: ContentAddressedStore,
        prompted_edits: PromptedEditService,
        workers: LocalWorkerManager,
    ) -> None:
        self.database_path = database_path
        self.blob_store = blob_store
        self.prompted_edits = prompted_edits
        self.workers = workers

    def prepare(
        self,
        *,
        project_id: str,
        payload: PreparePromptedEditRequest,
    ) -> PromptedSessionRecord:
        connection = open_database(self.database_path)
        try:
            revision = RevisionRepository(connection).get(payload.parent_revision_id)
            if revision.project_id != project_id:
                raise ValueError("prompted-edit parent must belong to the project")
            preview = connection.execute(
                "SELECT project_id, job_type, state FROM jobs WHERE id = ?",
                (payload.preview_job_id,),
            ).fetchone()
            if (
                preview is None
                or preview["project_id"] != project_id
                or preview["job_type"] != "preview"
                or preview["state"] != "succeeded"
            ):
                raise ValueError("prompted edits require a successful project preview")
            source = AssetRepository(connection, self.blob_store).get(revision.source_asset_id)
            if source.width_px is None or source.height_px is None:
                raise ValueError("prompted-edit source dimensions are unavailable")
            if (
                payload.selection.source_width_px != source.width_px
                or payload.selection.source_height_px != source.height_px
            ):
                raise ValueError("prompted-edit selection dimensions do not match the source")
            if any(
                isinstance(item, SourceRegionSelection) for item in payload.selection.primitives
            ):
                raise ValueError("region-list prompted edits require exact preview masks")
            config = load_job_config(dict(revision.config))
            transform = CanonicalTransform(
                original_size=PixelSize(width=source.width_px, height=source.height_px),
                normalized_size=PixelSize(width=source.width_px, height=source.height_px),
                crop_rect=Rect(x=0, y=0, width=source.width_px, height=source.height_px),
                fit_mode=CropMode.STRETCH,
                working_size=PixelSize(width=source.width_px, height=source.height_px),
                canvas_size=MillimetreSize(
                    width=config.canvas.width_mm,
                    height=config.canvas.height_mm,
                ),
            )
            raster = rasterize_canvas_selection(payload.selection, transform)
            if raster.selected_pixel_count == 0:
                raise ValueError("prompted-edit selection cannot be empty")
            mask_bytes = _mask_png(raster.mask, raster.width, raster.height)
            mask_blob = self.blob_store.put_bytes(
                mask_bytes,
                namespace="artifacts",
                extension=".png",
                media_type="image/png",
            )
            source_asset = _prompted_asset(self.blob_store, source)
            references = tuple(
                _prompted_asset(
                    self.blob_store,
                    AssetRepository(connection, self.blob_store).get(asset_id),
                )
                for asset_id in payload.reference_asset_ids
            )
            request = PromptedEditRequest(
                project_id=project_id,
                parent_revision_id=revision.id,
                selection_sha256=canonical_fingerprint(payload.selection),
                source=source_asset,
                mask=PromptedEditAsset.from_bytes(mask_bytes, "image/png"),
                prompt=payload.prompt,
                references=references,
                options=payload.options,
                seed=payload.seed,
                alternative_count=payload.alternative_count,
            )
            disclosure = self.prompted_edits.prepare_disclosure(
                provider_id=payload.provider_id,
                request=request,
            )
            stored_request = {
                "schema_version": 1,
                "preview_job_id": payload.preview_job_id,
                "selection": payload.selection.model_dump(mode="json"),
                "prompt": payload.prompt,
                "reference_asset_ids": list(payload.reference_asset_ids),
                "options": payload.options,
                "seed": payload.seed,
                "alternative_count": payload.alternative_count,
            }
            request_sha256 = hashlib.sha256(
                canonical_json(
                    {
                        **stored_request,
                        "project_id": project_id,
                        "parent_revision_id": revision.id,
                        "provider_id": payload.provider_id,
                        "source_sha256": source.sha256,
                        "mask_sha256": mask_blob.sha256,
                        "reference_sha256": [item.sha256 for item in references],
                    }
                ).encode("utf-8")
            ).hexdigest()
            return PromptedEditRepository(connection, self.blob_store).create_prepared(
                project_id=project_id,
                parent_revision_id=revision.id,
                request_sha256=request_sha256,
                selection_sha256=request.selection_sha256,
                source_asset_id=source.id,
                mask=mask_blob,
                provider=disclosure.provider,
                disclosure=disclosure,
                request=stored_request,
            )
        finally:
            connection.close()

    def retry(self, session_id: str) -> PromptedSessionRecord:
        connection = open_database(self.database_path)
        try:
            repository = PromptedEditRepository(connection, self.blob_store)
            prior = repository.get(session_id)
            if prior.status not in {
                PromptedSessionStatus.FAILED,
                PromptedSessionStatus.CANCELED,
                PromptedSessionStatus.REJECTED,
                PromptedSessionStatus.COMPLETE,
                PromptedSessionStatus.PARTIAL,
            }:
                raise PromptedEditConflictError("prompted edit is not retryable yet")
            return repository.create_prepared(
                project_id=prior.project_id,
                parent_revision_id=prior.parent_revision_id,
                request_sha256=prior.request_sha256,
                selection_sha256=prior.selection_sha256,
                source_asset_id=prior.source_asset_id,
                mask=prior.mask,
                provider=prior.provider,
                disclosure=prior.disclosure,
                request=prior.request,
                retry_of_session_id=prior.id,
            )
        finally:
            connection.close()

    def execute(
        self,
        session_id: str,
        *,
        request_sha256: str,
        disclosure_sha256: str,
    ):
        connection = open_database(self.database_path)
        try:
            repository = PromptedEditRepository(connection, self.blob_store)
            session = repository.get(session_id)
            if session.status != PromptedSessionStatus.PREPARED:
                raise PromptedEditConflictError("only a prepared prompted edit can execute")
            if session.request_sha256 != request_sha256:
                raise PromptedEditConflictError("prompted-edit request changed before consent")
            if session.disclosure.disclosure_sha256 != disclosure_sha256:
                raise PromptedEditConflictError("prompted-edit disclosure changed before consent")
            repository.transition(session_id, PromptedSessionStatus.QUEUED)
        finally:
            connection.close()

        try:
            return self.workers.submit(
                project_id=session.project_id,
                revision_id=session.parent_revision_id,
                job_type=JobType.PROMPTED_EDIT,
                initial_stage=JobStage.GENERATING,
                request_key=session.id,
                work=self._work(session.id),
            )
        except Exception:
            self._transition_if_active(session_id, PromptedSessionStatus.FAILED)
            raise

    def cancel(self, session_id: str):
        connection = open_database(self.database_path)
        try:
            PromptedEditRepository(connection, self.blob_store).get(session_id)
            row = connection.execute(
                """
                SELECT id FROM jobs WHERE request_key = ? AND job_type = 'prompted_edit'
                    AND state IN ('queued', 'running')
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise PromptedEditConflictError("prompted edit has no active job")
            job_id = str(row["id"])
        finally:
            connection.close()
        job = self.workers.cancel(job_id)
        self._transition_if_active(session_id, PromptedSessionStatus.CANCELED)
        return job

    def _work(self, session_id: str):
        def run(context: JobContext) -> WorkerResult:
            connection = open_database(self.database_path)
            try:
                repository = PromptedEditRepository(connection, self.blob_store)
                session = repository.transition(session_id, PromptedSessionStatus.RUNNING)
                request = self._request(connection, session)
            finally:
                connection.close()
            try:
                context.report(stage=JobStage.GENERATING, progress=0.05)
                execution = self.prompted_edits.execute(
                    provider_id=session.provider.provider_id,
                    request=request,
                    consent=GrantedConsent(
                        ConsentDecision(
                            granted=True,
                            event_id=f"consent_{session.id.replace('-', '_')[-40:]}",
                            recorded_at=datetime.now(timezone.utc),
                        )
                    ),
                    canceled=lambda: context.cancellation.canceled,
                )
                context.check_canceled()
                stored = self._store_alternatives(session, request, execution)
                context.check_canceled()
                connection = open_database(self.database_path)
                try:
                    PromptedEditRepository(connection, self.blob_store).complete(
                        session_id,
                        execution=execution,
                        alternatives=stored,
                    )
                finally:
                    connection.close()
                return WorkerResult()
            except WorkerCanceledError:
                self._transition_if_active(session_id, PromptedSessionStatus.CANCELED)
                raise
            except PromptedEditCanceledError as error:
                self._transition_if_active(session_id, PromptedSessionStatus.CANCELED)
                raise WorkerCanceledError(str(error)) from error
            except PromptedEditError as error:
                self._transition_if_active(session_id, PromptedSessionStatus.FAILED)
                raise WorkerJobError(
                    JobFailure(
                        code=error.code,
                        message=str(error),
                        retryable=error.retryable,
                    )
                ) from error
            except Exception as error:
                self._transition_if_active(session_id, PromptedSessionStatus.FAILED)
                raise WorkerJobError(
                    JobFailure(
                        code="prompted_edit_processing_failed",
                        message="The prompted alternative could not be validated locally.",
                        retryable=True,
                        details={"exception_type": type(error).__name__},
                    )
                ) from error

        return run

    def _request(
        self,
        connection,
        session: PromptedSessionRecord,
    ) -> PromptedEditRequest:
        assets = AssetRepository(connection, self.blob_store)
        source = _prompted_asset(self.blob_store, assets.get(session.source_asset_id))
        mask_bytes = self.blob_store.path_for(session.mask.relative_path).read_bytes()
        references = tuple(
            _prompted_asset(self.blob_store, assets.get(asset_id))
            for asset_id in session.request.get("reference_asset_ids", ())
        )
        return PromptedEditRequest(
            project_id=session.project_id,
            parent_revision_id=session.parent_revision_id,
            selection_sha256=session.selection_sha256,
            source=source,
            mask=PromptedEditAsset.from_bytes(mask_bytes, "image/png"),
            prompt=session.request["prompt"],
            references=references,
            options=session.request.get("options", {}),
            seed=session.request.get("seed"),
            alternative_count=session.request["alternative_count"],
        )

    def _store_alternatives(self, session, request, execution):
        stored = []
        connection = open_database(self.database_path)
        try:
            assets = AssetRepository(connection, self.blob_store)
            for alternative in execution.alternatives:
                evidence = build_prompted_alternative_evidence(
                    request.source.data,
                    alternative.output.data,
                )
                extension = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                }[alternative.output.media_type]
                output_blob = self.blob_store.put_bytes(
                    alternative.output.data,
                    namespace="assets",
                    extension=extension,
                    media_type=alternative.output.media_type,
                )
                output_asset = assets.register(
                    output_blob,
                    original_filename=f"prompted-{session.id}-{alternative.index}{extension}",
                    width_px=evidence.width_px,
                    height_px=evidence.height_px,
                    metadata={
                        "prompted_session_id": session.id,
                        "alternative_index": alternative.index,
                    },
                )
                mask_blob = self.blob_store.put_bytes(
                    evidence.changed_mask,
                    namespace="artifacts",
                    extension=".mask",
                    media_type="application/octet-stream",
                )
                preview_blob = self.blob_store.put_bytes(
                    evidence.changed_mask_preview_png,
                    namespace="artifacts",
                    extension=".png",
                    media_type="image/png",
                )
                stored.append(
                    StoredPromptedAlternative(
                        index=alternative.index,
                        output_asset=output_asset,
                        changed_mask=mask_blob,
                        changed_mask_preview=preview_blob,
                        changed_pixel_count=evidence.changed_pixel_count,
                        width_px=evidence.width_px,
                        height_px=evidence.height_px,
                        provenance=alternative.provenance,
                    )
                )
        finally:
            connection.close()
        return tuple(stored)

    def _transition_if_active(
        self,
        session_id: str,
        target: PromptedSessionStatus,
    ) -> None:
        connection = open_database(self.database_path)
        try:
            repository = PromptedEditRepository(connection, self.blob_store)
            current = repository.get(session_id)
            if current.status in {PromptedSessionStatus.QUEUED, PromptedSessionStatus.RUNNING}:
                repository.transition(session_id, target)
        finally:
            connection.close()


def _prompted_asset(store, asset) -> PromptedEditAsset:
    return PromptedEditAsset.from_bytes(
        store.path_for(asset.relative_path).read_bytes(),
        asset.media_type,
    )


def _mask_png(mask: bytes, width: int, height: int) -> bytes:
    image = Image.frombytes("L", (width, height), bytes(255 if value else 0 for value in mask))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
