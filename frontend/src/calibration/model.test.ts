import { describe, expect, it } from 'vitest'
import type {
  CalibrationDraft,
  CalibrationProposal,
  CalibrationRun,
  CalibrationRunRecord,
  PrintabilityProfile,
  PrintabilityRecommendations,
} from './contracts'
import {
  canonicalProposalRunIds,
  draftEvidencePresentation,
  draftProgress,
  evidenceStatusPresentation,
  profileValidationBlockers,
  proposalChangeRows,
  proposalContributionRows,
  runEvidencePresentation,
  updateObservation,
} from './model'

const recommendation = (basis: 'engineering_baseline' | 'printed_calibration') => ({
  value: 0.4,
  unit: 'mm' as const,
  basis,
  confidence: basis === 'printed_calibration' ? 'high' as const : 'provisional' as const,
  rationale: `${basis} rationale`,
  evidence_feature_kinds: ['line' as const],
  evidence_run_ids: basis === 'printed_calibration' ? ['run_1'] : [],
})

function recommendations(): PrintabilityRecommendations {
  return {
    minimum_island_area_mm2: { ...recommendation('printed_calibration'), unit: 'mm2' },
    minimum_island_diameter_mm: recommendation('printed_calibration'),
    maximum_tiny_hole_area_mm2: { ...recommendation('printed_calibration'), unit: 'mm2' },
    maximum_tiny_hole_diameter_mm: recommendation('printed_calibration'),
    minimum_ring_width_mm: recommendation('engineering_baseline'),
    minimum_line_width_mm: recommendation('printed_calibration'),
    minimum_neck_width_mm: recommendation('printed_calibration'),
    minimum_gap_width_mm: recommendation('printed_calibration'),
    long_line_minimum_length_mm: recommendation('engineering_baseline'),
    smoothing_radius_mm: recommendation('engineering_baseline'),
  }
}

describe('calibration presentation model', () => {
  it('distinguishes generated, partially validated, and fully validated profile states', () => {
    expect(evidenceStatusPresentation('pending_print_calibration')).toMatchObject({
      label: 'Pending print calibration', physicallyObserved: false,
    })
    expect(evidenceStatusPresentation('partially_validated')).toMatchObject({
      label: 'Partially print-validated', physicallyObserved: true,
    })
    expect(evidenceStatusPresentation('print_validated')).toMatchObject({
      label: 'Fully print-validated', physicallyObserved: true,
    })
  })

  it('does not call preparation, unverified, or merely attested drafts sealed', () => {
    const fixture = (evidenceState: CalibrationDraft['readiness']['evidence_state']) => ({
      readiness: { evidence_state: evidenceState },
    }) as CalibrationDraft

    expect(draftEvidencePresentation(fixture('preparation'))).toMatchObject({
      label: 'Generated preparation', physicallyObserved: false,
    })
    expect(draftEvidencePresentation(fixture('unverified_draft'))).toMatchObject({
      label: 'Unverified run draft', physicallyObserved: false,
    })
    expect(draftEvidencePresentation(fixture('attested_draft'))).toMatchObject({
      label: 'Attested physical observation', physicallyObserved: true,
    })
    expect(draftEvidencePresentation(fixture('sealed_physical_evidence'))).toMatchObject({
      label: 'Sealed physical evidence', physicallyObserved: true,
    })
  })

  it('calls only a completed, integrity-verified imported run physical evidence', () => {
    const verified = {
      integrity: 'verified', evidence: { record: { status: 'completed' } },
    } as CalibrationRun
    const invalid = {
      integrity: 'verified', evidence: { record: { status: 'template' } },
    } as unknown as CalibrationRun

    expect(runEvidencePresentation(verified).physicallyObserved).toBe(true)
    expect(runEvidencePresentation(invalid)).toMatchObject({
      label: 'Evidence verification failed', physicallyObserved: false,
    })
  })

  it('names every recommendation that still blocks full validation', () => {
    const profile = { recommendations: recommendations() } as PrintabilityProfile

    expect(profileValidationBlockers(profile).map((blocker) => blocker.recommendation)).toEqual([
      'minimum_ring_width_mm',
      'long_line_minimum_length_mm',
      'smoothing_radius_mm',
    ])
  })

  it('reports exact scoring and readiness progress including uncertain outcomes', () => {
    const draft = {
      record: {
        observations: [
          { feature_id: 'dot-1', outcome: 'pass' },
          { feature_id: 'dot-2', outcome: 'fail' },
          { feature_id: 'dot-3', outcome: 'uncertain' },
          { feature_id: 'dot-4', outcome: 'untested' },
        ],
      },
      members: [{ role: 'coupon_svg' }, { role: 'photo' }, { role: 'photo' }],
      readiness: {
        blockers: [{ code: 'missing_gcode' }, { code: 'missing_settings' }],
        ready_to_attest: false,
        ready_to_finalize: false,
      },
    } as CalibrationDraft

    expect(draftProgress(draft)).toEqual({
      scored: 3,
      total: 4,
      passed: 1,
      failed: 1,
      uncertain: 1,
      untested: 1,
      attachmentCount: 3,
      photoCount: 2,
      blockerCount: 2,
      readyToAttest: false,
      readyToFinalize: false,
    })
  })

  it('retains a measured physical zero and an uncertain outcome when editing', () => {
    const record = {
      observations: [{
        feature_id: 'hole-00-0p2', outcome: 'untested', measured_dimension_mm: null, notes: '',
      }],
    } as CalibrationRunRecord

    const updated = updateObservation(record, 'hole-00-0p2', {
      outcome: 'uncertain', measured_dimension_mm: 0, notes: 'Closed completely.',
    })

    expect(updated.observations[0]).toEqual({
      feature_id: 'hole-00-0p2',
      outcome: 'uncertain',
      measured_dimension_mm: 0,
      notes: 'Closed completely.',
    })
  })

  it('canonicalizes run selection without mutating evidence identity', () => {
    expect(canonicalProposalRunIds([' run_b ', 'run_a', 'run_b', ''])).toEqual([
      'run_a', 'run_b',
    ])
  })

  it('builds explicit before/after and source-observation rows without dropping zero', () => {
    const before = recommendation('engineering_baseline')
    const after = { ...recommendation('printed_calibration'), value: 0.5 }
    const proposal = {
      proposal: {
        recommendation_changes: [{ name: 'minimum_line_width_mm', before, after }],
        contributions: [
          {
            run_id: 'run_1',
            feature_id: 'line-00-0p2',
            feature_kind: 'line',
            nominal_dimension_mm: 0.2,
            rasterized_dimension_mm: 0.2,
            rasterized_width_mm: 0.2,
            rasterized_height_mm: 10,
            outcome: 'fail',
            measured_dimension_mm: 0,
            notes: 'No material remained.',
            included: true,
            exclusion_reason: null,
          },
        ],
      },
    } as CalibrationProposal

    expect(proposalChangeRows(proposal)[0]).toMatchObject({
      label: 'Minimum line width', beforeValue: 0.4, afterValue: 0.5,
    })
    expect(proposalContributionRows(proposal)[0]).toMatchObject({
      disposition: 'included', measuredLabel: '0 mm', measured_dimension_mm: 0,
    })
  })
})
