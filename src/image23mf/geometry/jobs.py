"""Cancelable geometry pipeline orchestration and derivation-key reuse.

The pipeline deliberately owns orchestration rather than geometry policy.  Topology,
extrusion, validation, and preview implementations are injected so those stages can evolve
without weakening the worker/storage guarantees:

* every expensive stage observes the live worker cancellation token;
* concurrent jobs for one derivation execute the stages only once per process;
* successful artifacts are reusable after restart from completed job records; and
* the existing worker publishes the complete artifact set and terminal job state in one
  SQLite transaction.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import json
import re
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from image23mf import __version__
from image23mf.contracts.jobs import JobFailure, JobResource, JobStage, JobType
from image23mf.derivation import DerivationIdentity
from image23mf.external import CancellationToken
from image23mf.geometry.extrusion import ArtworkExtrusionError
from image23mf.storage import ArtifactRepository, StoredBlob, open_database
from image23mf.workers import (
    JobContext,
    LocalWorkerManager,
    WorkerArtifact,
    WorkerCanceledError,
    WorkerJobError,
    WorkerResult,
)

GEOMETRY_PIPELINE_SCHEMA_VERSION = 1
GEOMETRY_SVG_KIND = "geometry-svg"
GEOMETRY_IR_KIND = "geometry-ir"
GEOMETRY_MESH_KIND = "geometry-mesh"
GEOMETRY_REPORT_KIND = "geometry-report"
GEOMETRY_PREVIEW_KIND = "geometry-preview"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GeometryPhase(str, Enum):
    VECTORIZE = "vectorize"
    TOPOLOGY = "topology"
    EXTRUDE = "extrude"
    VALIDATE = "validate"
    PREVIEW = "preview"
    PUBLISH = "publish"


@dataclass(frozen=True)
class GeometryPhaseProgress:
    phase: GeometryPhase
    progress: float
    cache_hit: bool = False


@dataclass(frozen=True)
class GeometryJobRequest:
    """Immutable orchestration input.

    ``payload`` is intentionally opaque to this layer.  A production adapter may carry a
    binary mask and print settings while tests and future importers may use a different
    source representation.  Every payload-affecting choice must be represented by the
    externally computed ``derivation_key``.
    """

    derivation_key: str
    payload: Any
    expects_preview: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not SHA256.fullmatch(self.derivation_key):
            raise ValueError("geometry derivation_key must be a lowercase SHA-256 digest")
        _canonical_json_bytes(self.metadata)


@dataclass(frozen=True)
class BinaryArtifact:
    payload: bytes
    extension: str
    media_type: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes) or not self.payload:
            raise ValueError("geometry artifacts require non-empty bytes")
        if not self.extension.startswith("."):
            raise ValueError("geometry artifact extension must include a leading dot")
        if "/" not in self.media_type:
            raise ValueError("geometry artifact media_type must be a MIME type")
        _canonical_json_bytes(self.metadata)


@dataclass(frozen=True)
class VectorizationOutput:
    geometry: Any
    svg: BinaryArtifact


@dataclass(frozen=True)
class ExtrusionOutput:
    geometry: Any
    geometry_ir: BinaryArtifact
    mesh: BinaryArtifact


@dataclass(frozen=True)
class ValidationOutput:
    accepted: bool
    report: BinaryArtifact


@dataclass(frozen=True)
class GeometryStageContext:
    job_id: str
    phase: GeometryPhase
    workspace: Path
    cancellation: CancellationToken

    def check_canceled(self) -> None:
        if self.cancellation.canceled:
            raise WorkerCanceledError("geometry job was canceled")


VectorizeStage = Callable[[GeometryJobRequest, GeometryStageContext], VectorizationOutput]
TopologyStage = Callable[[GeometryJobRequest, VectorizationOutput, GeometryStageContext], Any]
ExtrudeStage = Callable[[GeometryJobRequest, Any, GeometryStageContext], ExtrusionOutput]
ValidateStage = Callable[
    [GeometryJobRequest, ExtrusionOutput, GeometryStageContext], ValidationOutput
]
PreviewStage = Callable[
    [GeometryJobRequest, ExtrusionOutput, ValidationOutput, GeometryStageContext],
    Optional[BinaryArtifact],
]
PhaseObserver = Callable[[GeometryPhaseProgress], None]


@dataclass(frozen=True)
class GeometryPipelineAdapters:
    vectorize: VectorizeStage
    topology: TopologyStage
    extrude: ExtrudeStage
    validate: ValidateStage
    preview: PreviewStage


class GeometryValidationRejectedError(RuntimeError):
    """Raised when validation evidence rejects the generated mesh."""


@dataclass
class _KeyLock:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


@dataclass(frozen=True)
class _PhaseSpec:
    phase: GeometryPhase
    job_stage: JobStage
    progress: float


_PHASES = {
    GeometryPhase.VECTORIZE: _PhaseSpec(GeometryPhase.VECTORIZE, JobStage.VECTORIZING, 0.05),
    GeometryPhase.TOPOLOGY: _PhaseSpec(GeometryPhase.TOPOLOGY, JobStage.MESHING, 0.25),
    GeometryPhase.EXTRUDE: _PhaseSpec(GeometryPhase.EXTRUDE, JobStage.MESHING, 0.45),
    GeometryPhase.VALIDATE: _PhaseSpec(GeometryPhase.VALIDATE, JobStage.VALIDATING, 0.65),
    GeometryPhase.PREVIEW: _PhaseSpec(GeometryPhase.PREVIEW, JobStage.PACKAGING, 0.82),
    GeometryPhase.PUBLISH: _PhaseSpec(GeometryPhase.PUBLISH, JobStage.PACKAGING, 0.95),
}


class GeometryJobPipeline:
    """Create ``LocalWorkerManager`` work items for the canonical geometry phases."""

    def __init__(
        self,
        *,
        adapters: GeometryPipelineAdapters,
        phase_observer: Optional[PhaseObserver] = None,
        memory_cache_entries: int = 32,
    ) -> None:
        if memory_cache_entries < 0:
            raise ValueError("memory_cache_entries cannot be negative")
        self.adapters = adapters
        self.phase_observer = phase_observer
        self.memory_cache_entries = memory_cache_entries
        self._guard = threading.RLock()
        self._key_locks: dict[str, _KeyLock] = {}
        self._memory_cache: OrderedDict[str, tuple[WorkerArtifact, ...]] = OrderedDict()

    def submit(
        self,
        manager: LocalWorkerManager,
        *,
        project_id: str,
        request: GeometryJobRequest,
        job_type: JobType = JobType.GEOMETRY,
        revision_id: Optional[str] = None,
        supersession_key: Optional[str] = None,
    ) -> JobResource:
        if job_type not in {JobType.GEOMETRY, JobType.EXPORT}:
            raise ValueError("geometry pipelines may only submit geometry or export jobs")
        return manager.submit(
            project_id=project_id,
            revision_id=revision_id,
            job_type=job_type,
            initial_stage=JobStage.VECTORIZING,
            request_key=request.derivation_key,
            supersession_key=supersession_key,
            work=self.work(request),
        )

    def work(self, request: GeometryJobRequest) -> Callable[[JobContext], WorkerResult]:
        def execute(context: JobContext) -> WorkerResult:
            context.check_canceled()
            cached = self._cached_artifacts(context, request)
            if cached is not None:
                self._report(context, GeometryPhase.PUBLISH, cache_hit=True)
                return WorkerResult(artifacts=self._for_cache_hit(cached))

            key_lock = self._register_key_lock(request.derivation_key)
            acquired = False
            try:
                while not acquired:
                    context.check_canceled()
                    acquired = key_lock.lock.acquire(timeout=0.05)
                # The leader may have completed while this job waited.
                cached = self._cached_artifacts(context, request)
                if cached is not None:
                    self._report(context, GeometryPhase.PUBLISH, cache_hit=True)
                    return WorkerResult(artifacts=self._for_cache_hit(cached))
                artifacts = self._compute(context, request)
                self._remember(request.derivation_key, artifacts)
                return WorkerResult(artifacts=artifacts)
            finally:
                if acquired:
                    key_lock.lock.release()
                self._unregister_key_lock(request.derivation_key, key_lock)

        return execute

    def _compute(
        self,
        context: JobContext,
        request: GeometryJobRequest,
    ) -> tuple[WorkerArtifact, ...]:
        context.blob_store.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"geometry-{context.job_id}-",
            dir=context.blob_store.temp_root,
        ) as temporary:
            workspace = Path(temporary)
            vectorized = self._run(
                context,
                request,
                workspace,
                GeometryPhase.VECTORIZE,
                lambda stage: self.adapters.vectorize(request, stage),
            )
            topology = self._run(
                context,
                request,
                workspace,
                GeometryPhase.TOPOLOGY,
                lambda stage: self.adapters.topology(request, vectorized, stage),
            )
            extruded = self._run(
                context,
                request,
                workspace,
                GeometryPhase.EXTRUDE,
                lambda stage: self.adapters.extrude(request, topology, stage),
            )
            validation = self._run(
                context,
                request,
                workspace,
                GeometryPhase.VALIDATE,
                lambda stage: self.adapters.validate(request, extruded, stage),
            )
            if not validation.accepted:
                raise GeometryValidationRejectedError("geometry validation rejected the mesh")
            preview = self._run(
                context,
                request,
                workspace,
                GeometryPhase.PREVIEW,
                lambda stage: self.adapters.preview(request, extruded, validation, stage),
            )
            if request.expects_preview and preview is None:
                raise ValueError("geometry request requires a preview artifact")
            self._report(context, GeometryPhase.PUBLISH)
            payloads = [
                (GEOMETRY_SVG_KIND, vectorized.svg),
                (GEOMETRY_IR_KIND, extruded.geometry_ir),
                (GEOMETRY_MESH_KIND, extruded.mesh),
                (GEOMETRY_REPORT_KIND, validation.report),
            ]
            if preview is not None:
                payloads.append((GEOMETRY_PREVIEW_KIND, preview))
            artifacts = tuple(
                self._store_artifact(context, request, kind=kind, artifact=artifact)
                for kind, artifact in payloads
            )
            context.check_canceled()
            return artifacts

    def _run(
        self,
        context: JobContext,
        request: GeometryJobRequest,
        workspace: Path,
        phase: GeometryPhase,
        operation: Callable[[GeometryStageContext], Any],
    ) -> Any:
        self._report(context, phase)
        stage = GeometryStageContext(
            job_id=context.job_id,
            phase=phase,
            workspace=workspace,
            cancellation=context.cancellation,
        )
        stage.check_canceled()
        try:
            result = operation(stage)
        except ArtworkExtrusionError as error:
            raise WorkerJobError(
                JobFailure(
                    code="geometry_extrusion_failed",
                    message=f"Geometry extrusion could not create a printable solid: {error}",
                    retryable=False,
                    details={
                        "exception_type": type(error).__name__,
                        "phase": phase.value,
                    },
                )
            ) from error
        stage.check_canceled()
        return result

    def _report(
        self,
        context: JobContext,
        phase: GeometryPhase,
        *,
        cache_hit: bool = False,
    ) -> None:
        spec = _PHASES[phase]
        context.check_canceled()
        context.report(stage=spec.job_stage, progress=spec.progress)
        if self.phase_observer is not None:
            self.phase_observer(
                GeometryPhaseProgress(phase=phase, progress=spec.progress, cache_hit=cache_hit)
            )

    @staticmethod
    def _store_artifact(
        context: JobContext,
        request: GeometryJobRequest,
        *,
        kind: str,
        artifact: BinaryArtifact,
    ) -> WorkerArtifact:
        blob = context.blob_store.put_bytes(
            artifact.payload,
            namespace="artifacts",
            extension=artifact.extension,
            media_type=artifact.media_type,
        )
        return WorkerArtifact(
            kind=kind,
            derivation_key=request.derivation_key,
            blob=blob,
            metadata={
                **dict(request.metadata),
                **dict(artifact.metadata),
                "geometry_pipeline_schema_version": GEOMETRY_PIPELINE_SCHEMA_VERSION,
            },
        )

    def _cached_artifacts(
        self,
        context: JobContext,
        request: GeometryJobRequest,
    ) -> Optional[tuple[WorkerArtifact, ...]]:
        context.check_canceled()
        with self._guard:
            memory = self._memory_cache.get(request.derivation_key)
            if memory is not None:
                selected = self._select_valid(context, request, memory)
                if selected is not None:
                    self._memory_cache.move_to_end(request.derivation_key)
                    return selected
                self._memory_cache.pop(request.derivation_key, None)

        connection = open_database(context.database_path)
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT jobs.id, jobs.finished_at
                FROM jobs
                JOIN artifacts ON artifacts.job_id = jobs.id
                WHERE jobs.state = 'succeeded' AND artifacts.derivation_key = ?
                ORDER BY jobs.finished_at DESC, jobs.id DESC
                """,
                (request.derivation_key,),
            ).fetchall()
            repository = ArtifactRepository(connection)
            for row in rows:
                records = repository.list_for_job(row["id"])
                artifacts = tuple(
                    WorkerArtifact(
                        kind=record.kind,
                        derivation_key=record.derivation_key,
                        blob=StoredBlob(
                            sha256=record.sha256,
                            relative_path=record.relative_path,
                            byte_size=record.byte_size,
                            media_type=record.media_type,
                            extension=Path(record.relative_path).suffix,
                        ),
                        metadata=dict(record.metadata),
                    )
                    for record in records
                    if record.derivation_key == request.derivation_key
                )
                selected = self._select_valid(context, request, artifacts)
                if selected is not None:
                    self._remember(request.derivation_key, selected)
                    return selected
        finally:
            connection.close()
        return None

    @staticmethod
    def _select_valid(
        context: JobContext,
        request: GeometryJobRequest,
        artifacts: tuple[WorkerArtifact, ...],
    ) -> Optional[tuple[WorkerArtifact, ...]]:
        expected = [
            GEOMETRY_SVG_KIND,
            GEOMETRY_IR_KIND,
            GEOMETRY_MESH_KIND,
            GEOMETRY_REPORT_KIND,
        ]
        if request.expects_preview:
            expected.append(GEOMETRY_PREVIEW_KIND)
        selected = {item.kind: item for item in artifacts if item.kind in set(expected)}
        if set(selected) != set(expected):
            return None
        try:
            if not all(context.blob_store.verify(selected[kind].blob) for kind in expected):
                return None
        except FileNotFoundError:
            return None
        return tuple(selected[kind] for kind in expected)

    @staticmethod
    def _for_cache_hit(artifacts: tuple[WorkerArtifact, ...]) -> tuple[WorkerArtifact, ...]:
        return tuple(
            WorkerArtifact(
                kind=item.kind,
                derivation_key=item.derivation_key,
                blob=item.blob,
                metadata={**dict(item.metadata), "cache_hit": True},
            )
            for item in artifacts
        )

    def _remember(self, key: str, artifacts: tuple[WorkerArtifact, ...]) -> None:
        if self.memory_cache_entries == 0:
            return
        with self._guard:
            self._memory_cache[key] = artifacts
            self._memory_cache.move_to_end(key)
            while len(self._memory_cache) > self.memory_cache_entries:
                self._memory_cache.popitem(last=False)

    def _register_key_lock(self, key: str) -> _KeyLock:
        with self._guard:
            entry = self._key_locks.setdefault(key, _KeyLock())
            entry.users += 1
            return entry

    def _unregister_key_lock(self, key: str, entry: _KeyLock) -> None:
        with self._guard:
            entry.users -= 1
            if entry.users == 0 and self._key_locks.get(key) is entry:
                self._key_locks.pop(key)


def geometry_derivation_key(
    *,
    source_sha256: str,
    processed_labels_sha256: str,
    settings: Mapping[str, Any],
    stage_versions: Mapping[str, str],
) -> str:
    """Hash every geometry-affecting input into one persistent cache key."""

    for name, digest in (
        ("source_sha256", source_sha256),
        ("processed_labels_sha256", processed_labels_sha256),
    ):
        if not SHA256.fullmatch(digest):
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    versions = dict(stage_versions)
    invalid_version = any(
        not str(key).strip() or not str(value).strip() for key, value in versions.items()
    )
    if not versions or invalid_version:
        raise ValueError("stage_versions require non-empty names and values")
    settings_fingerprint = hashlib.sha256(_canonical_json_bytes(dict(settings))).hexdigest()
    return DerivationIdentity(
        pipeline="geometry",
        source_fingerprint=source_sha256,
        config_fingerprint=settings_fingerprint,
        operations_fingerprint=processed_labels_sha256,
        engine_version=__version__,
        adapter_versions={
            "pipeline": str(GEOMETRY_PIPELINE_SCHEMA_VERSION),
            **{str(key): str(value) for key, value in versions.items()},
        },
        dependencies={"processed_labels": processed_labels_sha256},
    ).key()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
