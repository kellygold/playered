"""Deterministic physical-evidence proposals and explicit catalog promotion."""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import json
import math
import sqlite3
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.calibration.artifact import CalibrationOutcome
from image23mf.calibration.catalogs import (
    CalibrationCatalogRepository,
    CalibrationCatalogVersionResource,
)
from image23mf.calibration.models import (
    RECOMMENDATION_ORDER,
    CalibrationBasis,
    CalibrationConfidence,
    CalibrationEvidenceStatus,
    CalibrationFeatureKind,
    CalibrationRecommendation,
    PrintabilityProfile,
    PrintabilityProfileCatalog,
    RecommendationUnit,
)
from image23mf.calibration.registry import CalibrationRegistry, CalibrationRunResource
from image23mf.storage import ContentAddressedStore
from image23mf.storage.repositories import canonical_json, immediate_transaction, new_id

CALIBRATION_PROPOSAL_ALGORITHM_VERSION = "transition-bracket-v1"
_DERIVED_RECOMMENDATIONS = {
    CalibrationFeatureKind.DOT: (
        "minimum_island_diameter_mm",
        "minimum_island_area_mm2",
    ),
    CalibrationFeatureKind.HOLE: (
        "maximum_tiny_hole_diameter_mm",
        "maximum_tiny_hole_area_mm2",
    ),
    CalibrationFeatureKind.LINE: ("minimum_line_width_mm",),
    CalibrationFeatureKind.NECK: ("minimum_neck_width_mm",),
    CalibrationFeatureKind.GAP: ("minimum_gap_width_mm",),
}
_POLICY_ONLY_RECOMMENDATIONS = frozenset(
    {"minimum_ring_width_mm", "long_line_minimum_length_mm", "smoothing_radius_mm"}
)


class CalibrationPromotionError(RuntimeError):
    """A proposal cannot be derived or reviewed safely."""


class CalibrationProposalConflictError(CalibrationPromotionError):
    """Proposal inputs or active catalog state are stale or conflicting."""


class CalibrationProposalNotFoundError(CalibrationPromotionError):
    """A requested proposal does not exist."""


class CalibrationProposalState(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CalibrationProposalContribution(ProposalModel):
    run_id: str
    feature_id: str
    feature_kind: CalibrationFeatureKind
    nominal_dimension_mm: float = Field(gt=0)
    rasterized_dimension_mm: float = Field(gt=0)
    rasterized_width_mm: float = Field(gt=0)
    rasterized_height_mm: float = Field(gt=0)
    outcome: CalibrationOutcome
    measured_dimension_mm: Optional[float] = Field(default=None, ge=0)
    notes: str
    included: bool
    exclusion_reason: Optional[str]


class CalibrationTransitionAnalysis(ProposalModel):
    feature_kind: CalibrationFeatureKind
    recommendation_names: tuple[str, ...]
    status: Literal["proposed", "blocked"]
    proposed_dimension_mm: Optional[float] = Field(default=None, ge=0)
    largest_failed_dimension_mm: Optional[float] = Field(default=None, ge=0)
    smallest_passed_dimension_mm: Optional[float] = Field(default=None, ge=0)
    reason: str
    contributing_run_ids: tuple[str, ...]


class CalibrationRecommendationChange(ProposalModel):
    name: str
    before: CalibrationRecommendation
    after: CalibrationRecommendation

    @model_validator(mode="after")
    def actually_changes(self) -> CalibrationRecommendationChange:
        if self.name not in RECOMMENDATION_ORDER:
            raise ValueError(f"unknown recommendation change: {self.name}")
        if self.before == self.after:
            raise ValueError("recommendation change must contain different values")
        return self


class CalibrationProfileProposal(ProposalModel):
    schema_version: Literal[1] = 1
    id: str
    profile_id: str
    process_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    algorithm_version: Literal["transition-bracket-v1"] = CALIBRATION_PROPOSAL_ALGORITHM_VERSION
    run_ids: tuple[str, ...] = Field(min_length=1)
    analyses: tuple[CalibrationTransitionAnalysis, ...]
    contributions: tuple[CalibrationProposalContribution, ...]
    base_evidence_status: CalibrationEvidenceStatus
    proposed_evidence_status: CalibrationEvidenceStatus
    recommendation_changes: tuple[CalibrationRecommendationChange, ...] = Field(min_length=1)
    proposed_catalog: PrintabilityProfileCatalog
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def identity_is_canonical(self) -> CalibrationProfileProposal:
        if self.run_ids != tuple(sorted(set(self.run_ids))):
            raise ValueError("proposal run IDs must be unique and canonical")
        if self.analyses != tuple(sorted(self.analyses, key=lambda item: item.feature_kind.value)):
            raise ValueError("proposal analyses must use canonical feature-kind order")
        expected_changes = tuple(
            sorted(
                self.recommendation_changes,
                key=lambda item: RECOMMENDATION_ORDER.index(item.name),
            )
        )
        if self.recommendation_changes != expected_changes or len(
            {item.name for item in self.recommendation_changes}
        ) != len(self.recommendation_changes):
            raise ValueError("recommendation changes must be unique and canonical")
        contribution_positions = tuple(
            (item.run_id, item.feature_id) for item in self.contributions
        )
        if contribution_positions != tuple(sorted(set(contribution_positions))):
            raise ValueError("proposal contributions must be unique and canonical")
        if self.proposed_catalog.fingerprint() != self.proposed_catalog_fingerprint:
            raise ValueError("proposed catalog fingerprint is invalid")
        if self.fingerprint() != self.proposal_sha256:
            raise ValueError("proposal fingerprint is invalid")
        return self

    def canonical_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"proposal_sha256"})

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical_payload()).encode("utf-8")).hexdigest()


class CalibrationProposalResource(ProposalModel):
    proposal: CalibrationProfileProposal
    state: CalibrationProposalState
    created_at: str
    reviewed_at: Optional[str]
    reviewer: Optional[str]
    review_reason: Optional[str]


class CalibrationProposalCollection(ProposalModel):
    items: tuple[CalibrationProposalResource, ...]


class CalibrationPromotionResult(ProposalModel):
    proposal: CalibrationProposalResource
    active_catalog: CalibrationCatalogVersionResource


class CalibrationPromotionService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        blob_store: ContentAddressedStore,
    ) -> None:
        self.connection = connection
        self.blob_store = blob_store
        self.catalogs = CalibrationCatalogRepository(connection)

    def derive(
        self,
        *,
        profile_id: str,
        run_ids: tuple[str, ...],
        expected_catalog_fingerprint: str,
    ) -> CalibrationProposalResource:
        canonical_run_ids = tuple(sorted(set(run_ids)))
        if not canonical_run_ids or canonical_run_ids != run_ids:
            raise CalibrationPromotionError("proposal run IDs must be non-empty and canonical")
        active = self.catalogs.active()
        if active.fingerprint != expected_catalog_fingerprint:
            raise CalibrationProposalConflictError(
                "active calibration catalog changed before proposal derivation"
            )
        profile = active.catalog.profile(profile_id)
        if profile is None:
            raise CalibrationPromotionError(f"unknown profile in active catalog: {profile_id}")
        registry = CalibrationRegistry(self.connection, self.blob_store, active.catalog)
        runs = tuple(registry.get(run_id) for run_id in canonical_run_ids)
        self._validate_run_scope(runs, active, profile)
        process_fingerprint = runs[0].process_fingerprint
        analyses, contributions = _analyze_transitions(runs)
        if not any(item.status == "proposed" for item in analyses):
            raise CalibrationPromotionError(
                "evidence does not contain a monotonic fail-to-pass transition"
            )
        reviewed_on = max(run.evidence.record.printed_on or "" for run in runs)
        proposed_profile = _proposed_profile(
            profile,
            analyses=analyses,
            run_ids=canonical_run_ids,
            reviewed_on=reviewed_on,
        )
        changes = tuple(
            CalibrationRecommendationChange(
                name=name,
                before=getattr(profile.recommendations, name),
                after=getattr(proposed_profile.recommendations, name),
            )
            for name in RECOMMENDATION_ORDER
            if getattr(profile.recommendations, name)
            != getattr(proposed_profile.recommendations, name)
        )
        seed = canonical_json(
            {
                "algorithm_version": CALIBRATION_PROPOSAL_ALGORITHM_VERSION,
                "base_catalog_fingerprint": active.fingerprint,
                "profile_id": profile_id,
                "process_fingerprint": process_fingerprint,
                "run_ids": canonical_run_ids,
                "analyses": [item.model_dump(mode="json") for item in analyses],
            }
        )
        suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        physical_version = f"physical-{reviewed_on.replace('-', '')}-{suffix}"
        source = active.catalog.source.model_copy(
            update={
                "version": physical_version,
                "captured_on": reviewed_on,
                "method": (
                    "Explicitly reviewed local physical transition coupon evidence. "
                    "Policy-only recommendations remain provisional."
                ),
                "references": tuple(
                    sorted(
                        {
                            *active.catalog.source.references,
                            *(f"calibration-run:{run_id}" for run_id in canonical_run_ids),
                        }
                    )
                ),
            }
        )
        proposed_catalog = active.catalog.model_copy(
            update={
                "catalog_version": physical_version,
                "source": source,
                "profiles": tuple(
                    proposed_profile if item.id == profile_id else item
                    for item in active.catalog.profiles
                ),
            }
        )
        draft = {
            "schema_version": 1,
            "id": "pending",
            "profile_id": profile_id,
            "process_fingerprint": process_fingerprint,
            "base_catalog_fingerprint": active.fingerprint,
            "proposed_catalog_fingerprint": proposed_catalog.fingerprint(),
            "algorithm_version": CALIBRATION_PROPOSAL_ALGORITHM_VERSION,
            "run_ids": canonical_run_ids,
            "analyses": analyses,
            "contributions": contributions,
            "base_evidence_status": profile.evidence_status,
            "proposed_evidence_status": proposed_profile.evidence_status,
            "recommendation_changes": changes,
            "proposed_catalog": proposed_catalog,
        }
        identity = hashlib.sha256(
            canonical_json(
                {
                    key: (
                        [item.model_dump(mode="json") for item in value]
                        if key in {"analyses", "contributions", "recommendation_changes"}
                        else value.model_dump(mode="json")
                        if key == "proposed_catalog"
                        else value
                    )
                    for key, value in draft.items()
                    if key != "id"
                }
            ).encode("utf-8")
        ).hexdigest()
        draft["id"] = f"calibration-proposal-{identity[:24]}"
        unsigned = CalibrationProfileProposal.model_construct(**draft, proposal_sha256="0" * 64)
        draft["proposal_sha256"] = unsigned.fingerprint()
        proposal = CalibrationProfileProposal.model_validate(draft)
        return self._store(proposal)

    def get(self, proposal_id: str) -> CalibrationProposalResource:
        row = self.connection.execute(
            "SELECT * FROM calibration_profile_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise CalibrationProposalNotFoundError(f"calibration proposal not found: {proposal_id}")
        return _proposal_resource(self.connection, row)

    def list(self, *, profile_id: Optional[str] = None) -> CalibrationProposalCollection:
        if profile_id is None:
            rows = self.connection.execute(
                "SELECT * FROM calibration_profile_proposals ORDER BY created_at DESC, id DESC"
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM calibration_profile_proposals
                WHERE profile_id = ? ORDER BY created_at DESC, id DESC
                """,
                (profile_id,),
            ).fetchall()
        return CalibrationProposalCollection(
            items=tuple(_proposal_resource(self.connection, row) for row in rows)
        )

    def accept(
        self,
        proposal_id: str,
        *,
        expected_catalog_fingerprint: str,
        reviewer: str,
        reason: str,
    ) -> CalibrationPromotionResult:
        if not reviewer.strip() or not reason.strip():
            raise CalibrationPromotionError("proposal acceptance requires reviewer and reason")
        current = self.get(proposal_id)
        if current.state != CalibrationProposalState.PENDING:
            raise CalibrationProposalConflictError("calibration proposal was already reviewed")
        proposal = current.proposal
        if proposal.base_catalog_fingerprint != expected_catalog_fingerprint:
            raise CalibrationProposalConflictError(
                "proposal base does not match review expectation"
            )
        base = self.catalogs.get(proposal.base_catalog_fingerprint)
        self._revalidate_printed_evidence(proposal, base)
        with immediate_transaction(self.connection):
            head = self.connection.execute(
                "SELECT fingerprint FROM calibration_catalog_head WHERE singleton = 'active'"
            ).fetchone()
            if head is None or head["fingerprint"] != expected_catalog_fingerprint:
                raise CalibrationProposalConflictError(
                    "active calibration catalog changed during review"
                )
            existing = self.connection.execute(
                "SELECT catalog_json FROM calibration_catalog_versions WHERE fingerprint = ?",
                (proposal.proposed_catalog_fingerprint,),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    """
                    INSERT INTO calibration_catalog_versions(
                        fingerprint, catalog_id, catalog_version, catalog_json,
                        parent_fingerprint
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.proposed_catalog_fingerprint,
                        proposal.proposed_catalog.catalog_id,
                        proposal.proposed_catalog.catalog_version,
                        proposal.proposed_catalog.canonical_json(),
                        proposal.base_catalog_fingerprint,
                    ),
                )
            elif existing["catalog_json"] != proposal.proposed_catalog.canonical_json():
                raise CalibrationProposalConflictError(
                    "proposed catalog fingerprint conflicts with retained content"
                )
            cursor = self.connection.execute(
                """
                UPDATE calibration_profile_proposals
                SET state = 'accepted',
                    reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    reviewer = ?, review_reason = ?
                WHERE id = ? AND state = 'pending'
                """,
                (reviewer.strip(), reason.strip(), proposal_id),
            )
            if cursor.rowcount != 1:
                raise CalibrationProposalConflictError("calibration proposal changed during review")
            self.connection.execute(
                """
                INSERT INTO calibration_catalog_promotions(
                    id, proposal_id, previous_fingerprint, activated_fingerprint, reviewer
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    new_id("calibration_promotion"),
                    proposal_id,
                    proposal.base_catalog_fingerprint,
                    proposal.proposed_catalog_fingerprint,
                    reviewer.strip(),
                ),
            )
            head_cursor = self.connection.execute(
                """
                UPDATE calibration_catalog_head
                SET fingerprint = ?, activated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE singleton = 'active' AND fingerprint = ?
                """,
                (
                    proposal.proposed_catalog_fingerprint,
                    proposal.base_catalog_fingerprint,
                ),
            )
            if head_cursor.rowcount != 1:
                raise CalibrationProposalConflictError(
                    "active calibration catalog changed during promotion"
                )
        return CalibrationPromotionResult(
            proposal=self.get(proposal_id),
            active_catalog=self.catalogs.active(),
        )

    def reject(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        reason: str,
    ) -> CalibrationProposalResource:
        if not reviewer.strip() or not reason.strip():
            raise CalibrationPromotionError("proposal rejection requires reviewer and reason")
        with immediate_transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE calibration_profile_proposals
                SET state = 'rejected',
                    reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    reviewer = ?, review_reason = ?
                WHERE id = ? AND state = 'pending'
                """,
                (reviewer.strip(), reason.strip(), proposal_id),
            )
            if cursor.rowcount != 1:
                if (
                    self.connection.execute(
                        "SELECT 1 FROM calibration_profile_proposals WHERE id = ?", (proposal_id,)
                    ).fetchone()
                    is None
                ):
                    raise CalibrationProposalNotFoundError(
                        f"calibration proposal not found: {proposal_id}"
                    )
                raise CalibrationProposalConflictError("calibration proposal was already reviewed")
        return self.get(proposal_id)

    def _store(self, proposal: CalibrationProfileProposal) -> CalibrationProposalResource:
        existing = self.connection.execute(
            "SELECT * FROM calibration_profile_proposals WHERE proposal_sha256 = ?",
            (proposal.proposal_sha256,),
        ).fetchone()
        if existing is not None:
            return _proposal_resource(self.connection, existing)
        with immediate_transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO calibration_profile_proposals(
                    id, profile_id, process_fingerprint, base_catalog_fingerprint,
                    proposed_catalog_fingerprint, algorithm_version, proposal_sha256,
                    proposal_json, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    proposal.id,
                    proposal.profile_id,
                    proposal.process_fingerprint,
                    proposal.base_catalog_fingerprint,
                    proposal.proposed_catalog_fingerprint,
                    proposal.algorithm_version,
                    proposal.proposal_sha256,
                    canonical_json(proposal.model_dump(mode="json")),
                ),
            )
            for contribution in proposal.contributions:
                self.connection.execute(
                    """
                    INSERT INTO calibration_proposal_contributions(
                        id, proposal_id, run_id, feature_id, feature_kind, included,
                        exclusion_reason, observation_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("calibration_contribution"),
                        proposal.id,
                        contribution.run_id,
                        contribution.feature_id,
                        contribution.feature_kind.value,
                        int(contribution.included),
                        contribution.exclusion_reason,
                        canonical_json(contribution.model_dump(mode="json")),
                    ),
                )
        return self.get(proposal.id)

    def _validate_run_scope(
        self,
        runs: tuple[CalibrationRunResource, ...],
        active: CalibrationCatalogVersionResource,
        profile: PrintabilityProfile,
    ) -> None:
        process_fingerprints = {run.process_fingerprint for run in runs}
        if len(process_fingerprints) != 1:
            raise CalibrationPromotionError(
                "proposal runs must share one exact physical process fingerprint"
            )
        for run in runs:
            if (
                run.catalog_fingerprint != active.fingerprint
                or run.profile_id != profile.id
                or run.profile_fingerprint != _profile_fingerprint(profile)
            ):
                raise CalibrationPromotionError(
                    "proposal evidence does not match the active catalog/profile"
                )

    def _revalidate_printed_evidence(
        self,
        proposal: CalibrationProfileProposal,
        base: CalibrationCatalogVersionResource,
    ) -> None:
        registry = CalibrationRegistry(self.connection, self.blob_store, base.catalog)
        runs = tuple(registry.get(run_id) for run_id in proposal.run_ids)
        profile = base.catalog.profile(proposal.profile_id)
        if profile is None:
            raise CalibrationPromotionError("proposal base profile disappeared")
        self._validate_run_scope(runs, base, profile)
        if {run.process_fingerprint for run in runs} != {proposal.process_fingerprint}:
            raise CalibrationPromotionError("proposal process fingerprint is not reproducible")
        proposed_profile = proposal.proposed_catalog.profile(proposal.profile_id)
        if proposed_profile is None:
            raise CalibrationPromotionError("proposal output profile disappeared")
        referenced_run_ids = {
            run_id
            for _, recommendation in proposed_profile.recommendations
            for run_id in recommendation.evidence_run_ids
        }
        referenced_runs = {run_id: registry.get(run_id) for run_id in sorted(referenced_run_ids)}
        for name, recommendation in proposed_profile.recommendations:
            if name in _POLICY_ONLY_RECOMMENDATIONS:
                if recommendation.basis != CalibrationBasis.ENGINEERING_BASELINE:
                    raise CalibrationPromotionError(
                        f"policy-only recommendation cannot claim printed evidence: {name}"
                    )
                continue
            if recommendation.basis != CalibrationBasis.PRINTED_CALIBRATION:
                continue
            if not recommendation.evidence_run_ids:
                raise CalibrationPromotionError(f"printed recommendation lacks evidence: {name}")
            for run_id in recommendation.evidence_run_ids:
                run = referenced_runs[run_id]
                if (
                    run.profile_id != proposed_profile.id
                    or run.process_fingerprint != proposal.process_fingerprint
                    or run.printer_id != proposed_profile.printer_id
                    or run.nozzle_id != proposed_profile.nozzle_id
                    or run.material_class != proposed_profile.material_class
                ):
                    raise CalibrationPromotionError(
                        f"printed recommendation evidence has incompatible scope: {name}"
                    )
                available_kinds = {item.kind for item in run.evidence.artifact.features}
                if not set(recommendation.evidence_feature_kinds).issubset(available_kinds):
                    raise CalibrationPromotionError(
                        f"printed recommendation evidence lacks its feature kind: {name}"
                    )


def _analyze_transitions(
    runs: tuple[CalibrationRunResource, ...],
) -> tuple[
    tuple[CalibrationTransitionAnalysis, ...],
    tuple[CalibrationProposalContribution, ...],
]:
    raw = []
    for run in runs:
        features = {item.id: item for item in run.evidence.artifact.features}
        for observation in run.evidence.record.observations:
            feature = features[observation.feature_id]
            raw.append((run.id, feature, observation))
    analyses = []
    decisions: dict[CalibrationFeatureKind, tuple[bool, str]] = {}
    for kind in CalibrationFeatureKind:
        selected = [item for item in raw if item[1].kind == kind]
        analysis = _transition_analysis(kind, selected)
        analyses.append(analysis)
        decisions[kind] = (analysis.status == "proposed", analysis.reason)
    contributions = tuple(
        sorted(
            (
                CalibrationProposalContribution(
                    run_id=run_id,
                    feature_id=feature.id,
                    feature_kind=feature.kind,
                    nominal_dimension_mm=feature.nominal_dimension_mm,
                    rasterized_dimension_mm=feature.rasterized_dimension_mm,
                    rasterized_width_mm=feature.rasterized_width_mm,
                    rasterized_height_mm=feature.rasterized_height_mm,
                    outcome=observation.outcome,
                    measured_dimension_mm=observation.measured_dimension_mm,
                    notes=observation.notes,
                    included=decisions[feature.kind][0],
                    exclusion_reason=(
                        None if decisions[feature.kind][0] else decisions[feature.kind][1]
                    ),
                )
                for run_id, feature, observation in raw
            ),
            key=lambda item: (item.run_id, item.feature_id),
        )
    )
    return tuple(sorted(analyses, key=lambda item: item.feature_kind.value)), contributions


def _transition_analysis(kind, observations) -> CalibrationTransitionAnalysis:
    run_ids = tuple(sorted({item[0] for item in observations}))
    recommendation_names = _DERIVED_RECOMMENDATIONS[kind]
    outcomes_by_dimension: dict[float, set[CalibrationOutcome]] = {}
    for _, feature, observation in observations:
        outcomes_by_dimension.setdefault(feature.rasterized_dimension_mm, set()).add(
            observation.outcome
        )
    if any(CalibrationOutcome.UNCERTAIN in outcomes for outcomes in outcomes_by_dimension.values()):
        return _blocked(
            kind, recommendation_names, run_ids, "uncertain observations require review"
        )
    if any(len(outcomes) != 1 for outcomes in outcomes_by_dimension.values()):
        return _blocked(kind, recommendation_names, run_ids, "contradictory outcomes at one size")
    failed = sorted(
        dimension
        for dimension, outcomes in outcomes_by_dimension.items()
        if outcomes == {CalibrationOutcome.FAIL}
    )
    passed = sorted(
        dimension
        for dimension, outcomes in outcomes_by_dimension.items()
        if outcomes == {CalibrationOutcome.PASS}
    )
    if not failed or not passed:
        return _blocked(kind, recommendation_names, run_ids, "transition is not bracketed")
    largest_failed = max(failed)
    smallest_passed = min(passed)
    if largest_failed >= smallest_passed:
        return _blocked(kind, recommendation_names, run_ids, "outcomes are non-monotonic")
    proposed = largest_failed if kind == CalibrationFeatureKind.HOLE else smallest_passed
    return CalibrationTransitionAnalysis(
        feature_kind=kind,
        recommendation_names=recommendation_names,
        status="proposed",
        proposed_dimension_mm=proposed,
        largest_failed_dimension_mm=largest_failed,
        smallest_passed_dimension_mm=smallest_passed,
        reason=(
            "largest failed hole diameter bounds the tiny-hole warning"
            if kind == CalibrationFeatureKind.HOLE
            else "smallest consistently passing feature defines the conservative threshold"
        ),
        contributing_run_ids=run_ids,
    )


def _blocked(kind, names, run_ids, reason) -> CalibrationTransitionAnalysis:
    return CalibrationTransitionAnalysis(
        feature_kind=kind,
        recommendation_names=names,
        status="blocked",
        reason=reason,
        contributing_run_ids=run_ids,
    )


def _proposed_profile(
    profile: PrintabilityProfile,
    *,
    analyses: tuple[CalibrationTransitionAnalysis, ...],
    run_ids: tuple[str, ...],
    reviewed_on: str,
) -> PrintabilityProfile:
    updates = {}
    for analysis in analyses:
        if analysis.status != "proposed" or analysis.proposed_dimension_mm is None:
            continue
        dimension = analysis.proposed_dimension_mm
        for name in analysis.recommendation_names:
            value = math.pi * (dimension / 2) ** 2 if name.endswith("area_mm2") else dimension
            updates[name] = CalibrationRecommendation(
                value=value,
                unit=(
                    RecommendationUnit.SQUARE_MILLIMETRES
                    if name.endswith("area_mm2")
                    else RecommendationUnit.MILLIMETRES
                ),
                basis=CalibrationBasis.PRINTED_CALIBRATION,
                confidence=CalibrationConfidence.MODERATE,
                rationale=(
                    f"Explicitly reviewed {analysis.feature_kind.value} transition: "
                    f"largest fail {analysis.largest_failed_dimension_mm:g} mm, "
                    f"smallest pass {analysis.smallest_passed_dimension_mm:g} mm."
                ),
                evidence_feature_kinds=(analysis.feature_kind,),
                evidence_run_ids=run_ids,
            )
    recommendations = profile.recommendations.model_copy(update=updates)
    return profile.model_copy(
        update={
            "evidence_status": CalibrationEvidenceStatus.PARTIALLY_VALIDATED,
            "reviewed_on": reviewed_on,
            "recommendations": recommendations,
        }
    )


def _proposal_resource(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> CalibrationProposalResource:
    try:
        proposal = CalibrationProfileProposal.model_validate_json(row["proposal_json"])
    except Exception as error:
        raise CalibrationPromotionError("retained calibration proposal is invalid") from error
    if (
        proposal.id != row["id"]
        or proposal.proposal_sha256 != row["proposal_sha256"]
        or proposal.base_catalog_fingerprint != row["base_catalog_fingerprint"]
        or proposal.proposed_catalog_fingerprint != row["proposed_catalog_fingerprint"]
        or proposal.process_fingerprint != row["process_fingerprint"]
        or proposal.profile_id != row["profile_id"]
        or proposal.algorithm_version != row["algorithm_version"]
    ):
        raise CalibrationPromotionError("retained calibration proposal identity is invalid")
    contribution_rows = connection.execute(
        """
        SELECT * FROM calibration_proposal_contributions
        WHERE proposal_id = ? ORDER BY run_id, feature_id
        """,
        (proposal.id,),
    ).fetchall()
    retained_contributions = []
    for contribution_row in contribution_rows:
        try:
            contribution = CalibrationProposalContribution.model_validate_json(
                contribution_row["observation_json"]
            )
        except Exception as error:
            raise CalibrationPromotionError(
                "retained calibration proposal contribution is invalid"
            ) from error
        if (
            contribution.run_id != contribution_row["run_id"]
            or contribution.feature_id != contribution_row["feature_id"]
            or contribution.feature_kind.value != contribution_row["feature_kind"]
            or contribution.included != bool(contribution_row["included"])
            or contribution.exclusion_reason != contribution_row["exclusion_reason"]
        ):
            raise CalibrationPromotionError(
                "retained calibration proposal contribution identity is invalid"
            )
        retained_contributions.append(contribution)
    if tuple(retained_contributions) != proposal.contributions:
        raise CalibrationPromotionError(
            "retained calibration proposal contribution set is not closed"
        )
    return CalibrationProposalResource(
        proposal=proposal,
        state=CalibrationProposalState(row["state"]),
        created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
        reviewer=row["reviewer"],
        review_reason=row["review_reason"],
    )


def _profile_fingerprint(profile: PrintabilityProfile) -> str:
    return hashlib.sha256(
        json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
