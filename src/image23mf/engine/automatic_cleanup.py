"""Deterministic preview cleanup composed from the explicit engine primitives."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.contours import (
    ContourCleanupRecord,
    ContourCleanupRequest,
    ContourOperation,
    ContourOperationKind,
    apply_contour_cleanup,
)
from image23mf.engine.islands import (
    IslandAnalysis,
    IslandAnalysisOptions,
    IslandCandidateStatus,
    IslandMergePolicy,
    IslandMergeRequest,
    IslandPolicyRecord,
    apply_island_policy,
    classify_small_islands,
)
from image23mf.engine.labels import LabelField
from image23mf.engine.regions import RegionAnalysis, analyze_regions

AUTOMATIC_CLEANUP_SCHEMA_VERSION = 1


class AutomaticCleanupModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class AutomaticCleanupRecord(AutomaticCleanupModel):
    """Exact audit record for the automatic portion of preview cleanup."""

    schema_version: Literal[1] = AUTOMATIC_CLEANUP_SCHEMA_VERSION
    island_policy: IslandMergePolicy
    island_analysis_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    island_record: Optional[IslandPolicyRecord] = None
    smoothing_radius_mm: float = Field(ge=0, le=5)
    smoothing_majority_ratio: float = Field(gt=0.5, le=1)
    contour_record: Optional[ContourCleanupRecord] = None
    before_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_label_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_label_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_pixel_count: int = Field(ge=0)

    @model_validator(mode="after")
    def record_is_consistent(self) -> AutomaticCleanupRecord:
        if (
            self.island_record is not None
            and self.island_record.before_graph_fingerprint != self.before_graph_fingerprint
        ):
            raise ValueError("automatic island record does not start at the source graph")
        if self.contour_record is not None:
            expected_before = (
                self.island_record.after_graph_fingerprint
                if self.island_record is not None
                else self.before_graph_fingerprint
            )
            if self.contour_record.before_graph_fingerprint != expected_before:
                raise ValueError("automatic contour record does not follow the island record")
            if self.contour_record.after_graph_fingerprint != self.after_graph_fingerprint:
                raise ValueError("automatic contour record does not end at the final graph")
        elif self.island_record is not None:
            if self.island_record.after_graph_fingerprint != self.after_graph_fingerprint:
                raise ValueError("automatic island record does not end at the final graph")
        elif self.before_graph_fingerprint != self.after_graph_fingerprint:
            raise ValueError("an empty automatic cleanup cannot change the region graph")
        if self.changed_pixel_count == 0 and self.before_label_sha256 != self.after_label_sha256:
            raise ValueError("zero automatic changes require identical label fingerprints")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AutomaticCleanupResult:
    record: AutomaticCleanupRecord
    labels: LabelField
    active: bytes
    changed_mask: bytes
    analysis: RegionAnalysis
    initial_island_analysis: IslandAnalysis

    def __post_init__(self) -> None:
        size = self.labels.width * self.labels.height
        if len(self.active) != size or len(self.changed_mask) != size:
            raise ValueError("automatic cleanup planes must match the label field")
        if sum(1 for value in self.changed_mask if value) != self.record.changed_pixel_count:
            raise ValueError("automatic cleanup mask does not match its exact change count")
        if self.analysis.graph.fingerprint() != self.record.after_graph_fingerprint:
            raise ValueError("automatic cleanup analysis does not match its record")


def apply_automatic_cleanup(
    labels: LabelField,
    *,
    active: bytes,
    colors: Mapping[int, str],
    width_mm: float,
    height_mm: float,
    island_options: IslandAnalysisOptions,
    island_policy: IslandMergePolicy,
    smoothing_radius_mm: float,
    smoothing_majority_ratio: float = 0.6,
) -> AutomaticCleanupResult:
    """Apply configured cleanup before manual replay and retain an exact audit trail.

    Review and keep are intentionally non-destructive. Hole thresholds remain diagnostic until an
    explicit hole correction command exists; this function never silently fills artwork.
    """

    source_analysis = analyze_regions(
        labels,
        colors=colors,
        width_mm=width_mm,
        height_mm=height_mm,
        active=active,
    )
    initial_islands = classify_small_islands(source_analysis, options=island_options)
    working_labels = labels
    working_analysis = source_analysis
    island_record: Optional[IslandPolicyRecord] = None
    region_ids = tuple(
        item.region_id
        for item in initial_islands.candidates
        if item.status == IslandCandidateStatus.RISK and not item.touches_canvas_border
    )
    if region_ids:
        applied_islands = apply_island_policy(
            working_labels,
            working_analysis,
            initial_islands,
            IslandMergeRequest(policy=island_policy, region_ids=region_ids),
        )
        working_labels = applied_islands.labels
        working_analysis = applied_islands.analysis
        island_record = applied_islands.record

    contour_record: Optional[ContourCleanupRecord] = None
    if smoothing_radius_mm > 0:
        applied_contour = apply_contour_cleanup(
            working_labels,
            working_analysis,
            ContourCleanupRequest(
                operations=(
                    ContourOperation(
                        kind=ContourOperationKind.BOUNDARY_SIMPLIFY,
                        radius_mm=smoothing_radius_mm,
                        editable_labels=working_labels.label_values,
                        minimum_majority_ratio=smoothing_majority_ratio,
                    ),
                )
            ),
        )
        working_labels = applied_contour.labels
        working_analysis = applied_contour.analysis
        contour_record = applied_contour.record

    changed_mask = bytes(
        1 if before != after else 0 for before, after in zip(labels.pixels, working_labels.pixels)
    )
    record = AutomaticCleanupRecord(
        island_policy=island_policy,
        island_analysis_fingerprint=initial_islands.fingerprint(),
        island_record=island_record,
        smoothing_radius_mm=smoothing_radius_mm,
        smoothing_majority_ratio=smoothing_majority_ratio,
        contour_record=contour_record,
        before_graph_fingerprint=source_analysis.graph.fingerprint(),
        after_graph_fingerprint=working_analysis.graph.fingerprint(),
        before_label_sha256=hashlib.sha256(labels.pixels).hexdigest(),
        after_label_sha256=hashlib.sha256(working_labels.pixels).hexdigest(),
        active_sha256=hashlib.sha256(active).hexdigest(),
        changed_mask_sha256=hashlib.sha256(changed_mask).hexdigest(),
        changed_pixel_count=sum(changed_mask),
    )
    return AutomaticCleanupResult(
        record=record,
        labels=working_labels,
        active=active,
        changed_mask=changed_mask,
        analysis=working_analysis,
        initial_island_analysis=initial_islands,
    )
