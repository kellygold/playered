"""Conservative, evidence-backed planning for incremental preview reprocessing."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReprocessingReason(str, Enum):
    NO_BASELINE = "no_baseline"
    LOCAL_INTERIOR_EDIT = "local_interior_edit"
    PALETTE_CHANGED = "palette_changed"
    CROP_CHANGED = "crop_changed"
    CLEANUP_CHANGED = "cleanup_changed"
    BOUNDARY_EDIT = "boundary_edit"
    ENGINE_CHANGED = "engine_changed"
    OPERATIONS_CHANGED = "operations_changed"
    CACHE_INCOMPLETE = "cache_incomplete"


class ReprocessingPlan(BaseModel):
    """Persisted explanation of what was reused and why the decision was safe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    mode: Literal["scoped", "full"]
    reason: ReprocessingReason
    message: str = Field(min_length=1)
    baseline_job_id: Optional[str] = None
    reused_stages: tuple[str, ...] = ()
    recomputed_stages: tuple[str, ...]
    affected_pixel_count: int = Field(ge=0)
    affected_mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_equivalence: Literal["by_construction", "full_recompute"]

    @model_validator(mode="after")
    def scoped_work_has_a_baseline(self) -> ReprocessingPlan:
        if self.mode == "scoped" and (not self.baseline_job_id or not self.reused_stages):
            raise ValueError("scoped reprocessing requires a baseline and reused stages")
        if self.mode == "full" and self.reused_stages:
            raise ValueError("full reprocessing cannot claim reused stages")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


def mask_sha256(mask: bytes) -> str:
    return hashlib.sha256(mask).hexdigest()


def empty_mask(width: int, height: int) -> bytes:
    if width < 1 or height < 1:
        raise ValueError("mask dimensions must be positive")
    return bytes(width * height)


def is_strictly_interior_selection(
    mask: bytes,
    *,
    labels: bytes,
    active: bytes,
    width: int,
    height: int,
) -> bool:
    """Return true only when selected pixels lie inside one region, one pixel from its edge.

    This intentionally rejects canvas edges, inactive pixels, multi-label selections and every
    selection adjacent (including diagonally) to another label or transparency. False negatives
    cost performance only; false positives could make cache reuse incorrect.
    """

    size = width * height
    if width < 3 or height < 3 or any(len(plane) != size for plane in (mask, labels, active)):
        return False
    selected = np.frombuffer(mask, dtype=np.uint8).reshape((height, width)) > 0
    if not np.any(selected):
        return False
    activity = np.frombuffer(active, dtype=np.uint8).reshape((height, width)) > 0
    label_plane = np.frombuffer(labels, dtype=np.uint8).reshape((height, width))
    if not np.all(activity[selected]):
        return False
    selected_labels = np.unique(label_plane[selected])
    if selected_labels.size != 1:
        return False
    y, x = np.nonzero(selected)
    if np.any(x == 0) or np.any(y == 0) or np.any(x == width - 1) or np.any(y == height - 1):
        return False
    target = int(selected_labels[0])
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            neighbors_active = activity[y + dy, x + dx]
            neighbors_labels = label_plane[y + dy, x + dx]
            if not np.all(neighbors_active) or not np.all(neighbors_labels == target):
                return False
    return True
