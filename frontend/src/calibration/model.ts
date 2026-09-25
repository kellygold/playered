import type {
  CalibrationDraft,
  CalibrationEvidenceStatus,
  CalibrationObservation,
  CalibrationProposal,
  CalibrationProposalContribution,
  CalibrationRun,
  CalibrationRunRecord,
  PrintabilityProfile,
  PrintabilityRecommendations,
} from './contracts'

export type EvidencePresentation = {
  label: string
  description: string
  tone: 'neutral' | 'warning' | 'positive'
  physicallyObserved: boolean
}

export function evidenceStatusPresentation(
  status: CalibrationEvidenceStatus,
): EvidencePresentation {
  if (status === 'print_validated') {
    return {
      label: 'Fully print-validated',
      description: 'Every recommendation is backed by accepted physical calibration evidence.',
      tone: 'positive',
      physicallyObserved: true,
    }
  }
  if (status === 'partially_validated') {
    return {
      label: 'Partially print-validated',
      description: 'Some recommendations use accepted physical evidence; others remain baselines.',
      tone: 'warning',
      physicallyObserved: true,
    }
  }
  return {
    label: 'Pending print calibration',
    description: 'Recommendations are generated baselines, not physically observed evidence.',
    tone: 'neutral',
    physicallyObserved: false,
  }
}

export function draftEvidencePresentation(
  draft: CalibrationDraft | CalibrationDraft['readiness']['evidence_state'],
): EvidencePresentation {
  const evidenceState = typeof draft === 'string' ? draft : draft.readiness.evidence_state
  switch (evidenceState) {
    case 'sealed_physical_evidence':
      return {
        label: 'Sealed physical evidence',
        description: 'The observed run was finalized and retained with verified hashes.',
        tone: 'positive',
        physicallyObserved: true,
      }
    case 'attested_draft':
      return {
        label: 'Attested physical observation',
        description: 'The observation is attested but is not sealed until finalization succeeds.',
        tone: 'warning',
        physicallyObserved: true,
      }
    case 'unverified_draft':
      return {
        label: 'Unverified run draft',
        description: 'Print details are being recorded and have not been attested or sealed.',
        tone: 'warning',
        physicallyObserved: false,
      }
    case 'preparation':
      return {
        label: 'Generated preparation',
        description: 'Coupon files and templates are generated, not physically observed evidence.',
        tone: 'neutral',
        physicallyObserved: false,
      }
  }
}

export function runEvidencePresentation(run: CalibrationRun): EvidencePresentation {
  const sealed = run.integrity === 'verified' && run.evidence.record.status === 'completed'
  return sealed
    ? {
        label: 'Verified physical run',
        description: 'Completed, attested physical evidence with verified retained content.',
        tone: 'positive',
        physicallyObserved: true,
      }
    : {
        label: 'Evidence verification failed',
        description: 'This resource must not be treated as physical evidence.',
        tone: 'warning',
        physicallyObserved: false,
      }
}

export type ProfileValidationBlocker = {
  recommendation: keyof PrintabilityRecommendations
  label: string
  rationale: string
}

const recommendationLabels: Record<keyof PrintabilityRecommendations, string> = {
  minimum_island_area_mm2: 'Minimum island area',
  minimum_island_diameter_mm: 'Minimum island diameter',
  maximum_tiny_hole_area_mm2: 'Maximum tiny-hole area',
  maximum_tiny_hole_diameter_mm: 'Maximum tiny-hole diameter',
  minimum_ring_width_mm: 'Minimum ring width',
  minimum_line_width_mm: 'Minimum line width',
  minimum_neck_width_mm: 'Minimum neck width',
  minimum_gap_width_mm: 'Minimum gap width',
  long_line_minimum_length_mm: 'Long-line minimum length',
  smoothing_radius_mm: 'Smoothing radius',
}

export const recommendationOrder = Object.keys(
  recommendationLabels,
) as (keyof PrintabilityRecommendations)[]

export function profileValidationBlockers(profile: PrintabilityProfile): ProfileValidationBlocker[] {
  return recommendationOrder.flatMap((name) => {
    const recommendation = profile.recommendations[name]
    return recommendation.basis === 'printed_calibration'
      ? []
      : [{ recommendation: name, label: recommendationLabels[name], rationale: recommendation.rationale }]
  })
}

export type CalibrationDraftProgress = {
  scored: number
  total: number
  passed: number
  failed: number
  uncertain: number
  untested: number
  attachmentCount: number
  photoCount: number
  blockerCount: number
  readyToAttest: boolean
  readyToFinalize: boolean
}

export function draftProgress(draft: CalibrationDraft): CalibrationDraftProgress {
  const observations = draft.record.observations
  const count = (outcome: CalibrationObservation['outcome']) =>
    observations.filter((item) => item.outcome === outcome).length
  const untested = count('untested')
  return {
    scored: observations.length - untested,
    total: observations.length,
    passed: count('pass'),
    failed: count('fail'),
    uncertain: count('uncertain'),
    untested,
    attachmentCount: draft.members.length,
    photoCount: draft.members.filter((member) => member.role === 'photo').length,
    blockerCount: draft.readiness.blockers.length,
    readyToAttest: draft.readiness.ready_to_attest,
    readyToFinalize: draft.readiness.ready_to_finalize,
  }
}

export function updateObservation(
  record: CalibrationRunRecord,
  featureId: string,
  update: Partial<Pick<CalibrationObservation, 'outcome' | 'measured_dimension_mm' | 'notes'>>,
): CalibrationRunRecord {
  return {
    ...record,
    observations: record.observations.map((observation) =>
      observation.feature_id === featureId ? { ...observation, ...update } : observation,
    ),
  }
}

export function canonicalProposalRunIds(runIds: Iterable<string>): string[] {
  return [...new Set([...runIds].map((id) => id.trim()).filter(Boolean))].sort()
}

export type ProposalChangeRow = {
  name: keyof PrintabilityRecommendations
  label: string
  beforeValue: number
  afterValue: number
  unit: 'mm' | 'mm2'
  beforeBasis: string
  afterBasis: string
  evidenceRunIds: string[]
}

export function proposalChangeRows(proposal: CalibrationProposal): ProposalChangeRow[] {
  return proposal.proposal.recommendation_changes.map((change) => ({
    name: change.name,
    label: recommendationLabels[change.name],
    beforeValue: change.before.value,
    afterValue: change.after.value,
    unit: change.after.unit,
    beforeBasis: change.before.basis,
    afterBasis: change.after.basis,
    evidenceRunIds: change.after.evidence_run_ids,
  }))
}

export type ProposalContributionRow = CalibrationProposalContribution & {
  disposition: 'included' | 'excluded'
  measuredLabel: string
}

export function proposalContributionRows(
  proposal: CalibrationProposal,
): ProposalContributionRow[] {
  return proposal.proposal.contributions.map((contribution) => ({
    ...contribution,
    disposition: contribution.included ? 'included' : 'excluded',
    measuredLabel:
      contribution.measured_dimension_mm === null
        ? 'Not measured'
        : `${contribution.measured_dimension_mm} mm`,
  }))
}
