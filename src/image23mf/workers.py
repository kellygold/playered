"""Cancelable local job execution and current-result publication."""

import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from image23mf.contracts.jobs import (
    TERMINAL_JOB_STATES,
    InvalidJobTransitionError,
    JobFailure,
    JobResource,
    JobStage,
    JobState,
    JobType,
)
from image23mf.external import CancellationToken
from image23mf.storage import ContentAddressedStore, JobRepository, StoredBlob, open_database
from image23mf.storage.repositories import canonical_json, immediate_transaction, new_id

logger = logging.getLogger(__name__)


class WorkerCanceledError(RuntimeError):
    """Cooperative task cancellation signal."""


class WorkerJobError(RuntimeError):
    """Actionable domain failure that should be retained on the job resource."""

    def __init__(self, failure: JobFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class PublicationCheckpoint(str, Enum):
    BLOBS_VERIFIED = "blobs_verified"
    TRANSACTION_OPENED = "transaction_opened"
    ARTIFACT_ROWS_WRITTEN = "artifact_rows_written"
    DATABASE_COMMITTED = "database_committed"


@dataclass(frozen=True)
class WorkerArtifact:
    kind: str
    derivation_key: str
    blob: StoredBlob
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerResult:
    artifacts: tuple[WorkerArtifact, ...] = ()


class JobContext:
    def __init__(
        self,
        *,
        job_id: str,
        database_path: Path,
        blob_store: ContentAddressedStore,
        cancellation: CancellationToken,
    ) -> None:
        self.job_id = job_id
        self.database_path = database_path
        self.blob_store = blob_store
        self.cancellation = cancellation

    def check_canceled(self) -> None:
        if self.cancellation.canceled:
            raise WorkerCanceledError("job was canceled")

    def report(self, *, stage: JobStage, progress: float) -> JobResource:
        self.check_canceled()
        connection = open_database(self.database_path)
        try:
            try:
                return JobRepository(connection).report_progress(
                    self.job_id, stage=stage, progress=progress
                )
            except InvalidJobTransitionError as error:
                self.cancellation.cancel()
                raise WorkerCanceledError("job is no longer active") from error
        finally:
            connection.close()


JobWork = Callable[[JobContext], WorkerResult]
PublicationHook = Callable[[PublicationCheckpoint, str], None]


class LocalWorkerManager:
    def __init__(
        self,
        *,
        database_path: Path,
        blob_store: ContentAddressedStore,
        max_workers: int = 2,
        publication_hook: Optional[PublicationHook] = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.database_path = database_path.expanduser().resolve()
        self.blob_store = blob_store
        self._publication_hook = publication_hook
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="image23mf-worker"
        )
        self._lock = threading.RLock()
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Future[None]] = {}
        self._closed = False

    def recover_interrupted(self) -> tuple[JobResource, ...]:
        connection = open_database(self.database_path)
        try:
            return JobRepository(connection).recover_interrupted()
        finally:
            connection.close()

    def submit(
        self,
        *,
        project_id: str,
        job_type: JobType,
        initial_stage: JobStage,
        work: JobWork,
        revision_id: Optional[str] = None,
        request_key: Optional[str] = None,
        supersession_key: Optional[str] = None,
    ) -> JobResource:
        with self._lock:
            if self._closed:
                raise RuntimeError("worker manager is closed")
        connection = open_database(self.database_path)
        try:
            enqueued = JobRepository(connection).enqueue(
                project_id=project_id,
                job_type=job_type,
                revision_id=revision_id,
                request_key=request_key,
                supersession_key=supersession_key,
            )
        finally:
            connection.close()

        token = CancellationToken()
        with self._lock:
            if self._closed:
                token.cancel()
                self._cancel_persisted(enqueued.job.id)
                raise RuntimeError("worker manager closed while submitting work")
            if enqueued.superseded_job_id is not None:
                previous = self._tokens.get(enqueued.superseded_job_id)
                if previous is not None:
                    previous.cancel()
            self._tokens[enqueued.job.id] = token
            future = self._executor.submit(
                self._execute,
                enqueued.job.id,
                initial_stage,
                work,
                token,
            )
            self._futures[enqueued.job.id] = future
            future.add_done_callback(lambda _future, job_id=enqueued.job.id: self._forget(job_id))
        return enqueued.job

    def cancel(self, job_id: str) -> JobResource:
        with self._lock:
            token = self._tokens.get(job_id)
            if token is not None:
                token.cancel()
        connection = open_database(self.database_path)
        try:
            return JobRepository(connection).cancel(job_id)
        finally:
            connection.close()

    def get(self, job_id: str) -> JobResource:
        connection = open_database(self.database_path)
        try:
            return JobRepository(connection).get(job_id)
        finally:
            connection.close()

    def wait_for_terminal(self, job_id: str, *, timeout_seconds: float = 10) -> JobResource:
        deadline = time.monotonic() + timeout_seconds
        while True:
            job = self.get(job_id)
            if job.state in TERMINAL_JOB_STATES:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"job did not finish within {timeout_seconds:g} seconds")
            time.sleep(0.02)

    def wait_until_idle(self, job_id: str, *, timeout_seconds: float = 10) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._lock:
                active = job_id in self._futures
            if not active:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"worker did not become idle within {timeout_seconds:g} seconds")
            time.sleep(0.02)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tokens = tuple(self._tokens.values())
        for token in tokens:
            token.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _execute(
        self,
        job_id: str,
        initial_stage: JobStage,
        work: JobWork,
        token: CancellationToken,
    ) -> None:
        try:
            connection = open_database(self.database_path)
            try:
                JobRepository(connection).start(job_id, stage=initial_stage)
            finally:
                connection.close()
            context = JobContext(
                job_id=job_id,
                database_path=self.database_path,
                blob_store=self.blob_store,
                cancellation=token,
            )
            context.check_canceled()
            result = work(context)
            context.check_canceled()
            self._publish_current_result(job_id, result)
        except WorkerCanceledError:
            self._cancel_if_active(job_id)
        except InvalidJobTransitionError:
            # The job was canceled or superseded before its thread began or while it ran.
            token.cancel()
        except WorkerJobError as error:
            self._fail_if_active(job_id, error.failure)
        except Exception as error:
            logger.exception("Local worker job crashed job_id=%s", job_id)
            self._fail_if_active(
                job_id,
                JobFailure(
                    code="worker_crash",
                    message=f"Worker task raised {type(error).__name__}.",
                    retryable=True,
                    details={"exception_type": type(error).__name__},
                ),
            )

    def _publish_current_result(self, job_id: str, result: WorkerResult) -> bool:
        for artifact in result.artifacts:
            if not artifact.kind.strip() or not artifact.derivation_key.strip():
                raise ValueError("worker artifacts require kind and derivation_key")
            if not artifact.blob.relative_path.startswith("artifacts/"):
                raise ValueError("worker artifacts must use the artifacts namespace")
            if not self.blob_store.verify(artifact.blob):
                raise ValueError("worker artifact failed content verification")
        self._publication_checkpoint(PublicationCheckpoint.BLOBS_VERIFIED, job_id)

        connection = open_database(self.database_path)
        try:
            with immediate_transaction(connection):
                self._publication_checkpoint(PublicationCheckpoint.TRANSACTION_OPENED, job_id)
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None or row["state"] != JobState.RUNNING.value:
                    return False
                if row["supersession_key"] is not None:
                    head = connection.execute(
                        """
                        SELECT job_id, generation FROM job_heads
                        WHERE project_id = ? AND supersession_key = ?
                        """,
                        (row["project_id"], row["supersession_key"]),
                    ).fetchone()
                    if (
                        head is None
                        or head["job_id"] != job_id
                        or head["generation"] != row["generation"]
                    ):
                        return False

                for artifact in result.artifacts:
                    connection.execute(
                        """
                        INSERT INTO artifacts(
                            id, revision_id, job_id, kind, sha256, derivation_key, media_type,
                            relative_path, byte_size, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("artifact"),
                            row["revision_id"],
                            job_id,
                            artifact.kind,
                            artifact.blob.sha256,
                            artifact.derivation_key,
                            artifact.blob.media_type,
                            artifact.blob.relative_path,
                            artifact.blob.byte_size,
                            canonical_json(artifact.metadata),
                        ),
                    )
                self._publication_checkpoint(PublicationCheckpoint.ARTIFACT_ROWS_WRITTEN, job_id)
                changed = connection.execute(
                    """
                    UPDATE jobs
                    SET state = 'succeeded', stage = 'complete', progress = 1,
                        finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        canceled_at = NULL, error_code = NULL, error_message = NULL,
                        error_details_json = NULL
                    WHERE id = ? AND state = 'running'
                    """,
                    (job_id,),
                ).rowcount
                published = changed == 1
            self._publication_checkpoint(PublicationCheckpoint.DATABASE_COMMITTED, job_id)
            return published
        finally:
            connection.close()

    def _publication_checkpoint(self, checkpoint: PublicationCheckpoint, job_id: str) -> None:
        if self._publication_hook is not None:
            self._publication_hook(checkpoint, job_id)

    def _cancel_if_active(self, job_id: str) -> None:
        with suppress(InvalidJobTransitionError):
            self._cancel_persisted(job_id)

    def _cancel_persisted(self, job_id: str) -> None:
        connection = open_database(self.database_path)
        try:
            job = JobRepository(connection).get(job_id)
            if job.state in {JobState.QUEUED, JobState.RUNNING, JobState.CANCELED}:
                JobRepository(connection).cancel(job_id)
        finally:
            connection.close()

    def _fail_if_active(self, job_id: str, failure: JobFailure) -> None:
        connection = open_database(self.database_path)
        try:
            repository = JobRepository(connection)
            job = repository.get(job_id)
            if job.state in {JobState.QUEUED, JobState.RUNNING}:
                repository.fail(job_id, failure)
        finally:
            connection.close()

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._tokens.pop(job_id, None)
            self._futures.pop(job_id, None)
