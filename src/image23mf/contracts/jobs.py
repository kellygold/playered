"""Processing/export job resource and deterministic state machine."""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JobType(str, Enum):
    PREVIEW = "preview"
    GEOMETRY = "geometry"
    EXPORT = "export"
    VALIDATION = "validation"
    PROMPTED_EDIT = "prompted_edit"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


class JobStage(str, Enum):
    QUEUED = "queued"
    INGESTING = "ingesting"
    NORMALIZING = "normalizing"
    QUANTIZING = "quantizing"
    ANALYZING = "analyzing"
    CLEANING = "cleaning"
    VECTORIZING = "vectorizing"
    MESHING = "meshing"
    PACKAGING = "packaging"
    SLICING = "slicing"
    VALIDATING = "validating"
    GENERATING = "generating"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


TERMINAL_JOB_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED, JobState.SUPERSEDED}
)

TERMINAL_STAGE = {
    JobState.SUCCEEDED: JobStage.COMPLETE,
    JobState.FAILED: JobStage.FAILED,
    JobState.CANCELED: JobStage.CANCELED,
    JobState.SUPERSEDED: JobStage.SUPERSEDED,
}

ALLOWED_TRANSITIONS = {
    JobState.QUEUED: frozenset(
        {JobState.RUNNING, JobState.FAILED, JobState.CANCELED, JobState.SUPERSEDED}
    ),
    JobState.RUNNING: TERMINAL_JOB_STATES,
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELED: frozenset(),
    JobState.SUPERSEDED: frozenset(),
}


class InvalidJobTransitionError(RuntimeError):
    pass


class JobFailure(JobModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class JobResource(JobModel):
    id: str
    project_id: str
    revision_id: Optional[str] = None
    type: JobType
    state: JobState
    stage: JobStage
    progress: float = Field(ge=0, le=1)
    request_key: Optional[str] = None
    supersession_key: Optional[str] = None
    generation: int = Field(default=0, ge=0)
    failure: Optional[JobFailure] = None
    artifact_ids: tuple[str, ...] = ()
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    canceled_at: Optional[str] = None

    @model_validator(mode="after")
    def terminal_fields_are_consistent(self) -> "JobResource":
        if self.supersession_key is None and self.generation != 0:
            raise ValueError("non-superseding jobs require generation 0")
        if self.supersession_key is not None and self.generation < 1:
            raise ValueError("superseding jobs require a positive generation")
        if self.state in TERMINAL_JOB_STATES:
            if self.stage != TERMINAL_STAGE[self.state]:
                raise ValueError("terminal job state and stage must agree")
            if self.finished_at is None:
                raise ValueError("terminal jobs require finished_at")
        elif self.finished_at is not None:
            raise ValueError("non-terminal jobs cannot have finished_at")
        if self.state == JobState.SUCCEEDED and self.progress != 1:
            raise ValueError("successful jobs require progress 1")
        if self.state == JobState.FAILED and self.failure is None:
            raise ValueError("failed jobs require a failure resource")
        if self.state != JobState.FAILED and self.failure is not None:
            raise ValueError("only failed jobs may carry failure details")
        if self.state == JobState.CANCELED and self.canceled_at is None:
            raise ValueError("canceled jobs require canceled_at")
        return self


def assert_job_transition(current: JobState, target: JobState) -> None:
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidJobTransitionError(f"cannot transition job from {current} to {target}")
