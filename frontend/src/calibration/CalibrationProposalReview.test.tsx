import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  CalibrationProposalReview,
  type CalibrationCatalogReviewContext,
  type CalibrationProposalResource,
} from './CalibrationProposalReview'

const BASE = 'a'.repeat(64)
const ACTIVE = BASE
const PROPOSED = 'b'.repeat(64)
const PROCESS = 'c'.repeat(64)
const PROPOSAL_HASH = 'd'.repeat(64)

function recommendation(
  value: number,
  basis: string,
  confidence: string,
  evidenceRunIds: string[] = [],
) {
  return {
    value,
    unit: 'mm' as const,
    basis,
    confidence,
    rationale: basis === 'printed_calibration'
      ? 'Smallest consistently passing physical feature.'
      : 'Engineering baseline pending physical evidence.',
    evidence_feature_kinds: ['dot'],
    evidence_run_ids: evidenceRunIds,
  }
}

function pendingResource(): CalibrationProposalResource {
  return {
    state: 'pending',
    created_at: '2026-07-18T01:02:03Z',
    reviewed_at: null,
    reviewer: null,
    review_reason: null,
    proposal: {
      id: 'calibration-proposal-physical-0001',
      profile_id: 'bambu-p2s-0.4-hardened-steel-pla-v1',
      process_fingerprint: PROCESS,
      base_catalog_fingerprint: BASE,
      proposed_catalog_fingerprint: PROPOSED,
      algorithm_version: 'transition-bracket-v1',
      run_ids: ['calibration-run-0001', 'calibration-run-0002'],
      base_evidence_status: 'pending_print_calibration',
      proposed_evidence_status: 'partially_validated',
      proposal_sha256: PROPOSAL_HASH,
      recommendation_changes: [{
        name: 'minimum_island_diameter_mm',
        before: recommendation(0.6, 'engineering_baseline', 'provisional'),
        after: recommendation(
          0.72,
          'printed_calibration',
          'moderate',
          ['calibration-run-0001', 'calibration-run-0002'],
        ),
      }],
      analyses: [
        {
          feature_kind: 'dot',
          recommendation_names: ['minimum_island_diameter_mm'],
          status: 'proposed',
          proposed_dimension_mm: 0.72,
          largest_failed_dimension_mm: 0.6,
          smallest_passed_dimension_mm: 0.72,
          reason: 'A monotonic physical fail-to-pass transition was recorded.',
          contributing_run_ids: ['calibration-run-0001', 'calibration-run-0002'],
        },
        {
          feature_kind: 'gap',
          recommendation_names: ['minimum_gap_width_mm'],
          status: 'blocked',
          proposed_dimension_mm: null,
          largest_failed_dimension_mm: 0.4,
          smallest_passed_dimension_mm: null,
          reason: 'An uncertain result prevents a defensible transition.',
          contributing_run_ids: ['calibration-run-0001'],
        },
      ],
      contributions: [
        {
          run_id: 'calibration-run-0001',
          feature_id: 'dot-00-0p6',
          feature_kind: 'dot',
          nominal_dimension_mm: 0.6,
          rasterized_dimension_mm: 0.6,
          rasterized_width_mm: 0.56,
          rasterized_height_mm: 0.63,
          outcome: 'fail',
          measured_dimension_mm: 0,
          notes: 'Feature vanished completely.',
          included: true,
          exclusion_reason: null,
        },
        {
          run_id: 'calibration-run-0002',
          feature_id: 'dot-01-0p72',
          feature_kind: 'dot',
          nominal_dimension_mm: 0.72,
          rasterized_dimension_mm: 0.7,
          rasterized_width_mm: 0.7,
          rasterized_height_mm: 0.77,
          outcome: 'pass',
          measured_dimension_mm: 0.69,
          notes: '',
          included: true,
          exclusion_reason: null,
        },
        {
          run_id: 'calibration-run-0001',
          feature_id: 'gap-01-0p4',
          feature_kind: 'gap',
          nominal_dimension_mm: 0.4,
          rasterized_dimension_mm: 0.42,
          rasterized_width_mm: 0.42,
          rasterized_height_mm: 6.02,
          outcome: 'uncertain',
          measured_dimension_mm: null,
          notes: 'Edge was difficult to classify.',
          included: false,
          exclusion_reason: 'Uncertain observations are retained but excluded from the bracket.',
        },
      ],
    },
  }
}

function catalog(activeFingerprint = ACTIVE): CalibrationCatalogReviewContext {
  return {
    active_fingerprint: activeFingerprint,
    catalog_id: 'image23mf-local-printability',
    catalog_version: 'v1',
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('CalibrationProposalReview', () => {
  it('shows exact recommendation deltas, transition blockers, and every source observation', () => {
    const openRun = vi.fn()
    render(
      <CalibrationProposalReview
        resource={pendingResource()}
        catalog={catalog()}
        onAccept={vi.fn()}
        onReject={vi.fn()}
        onOpenRun={openRun}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Proposal review' })).toBeInTheDocument()
    expect(screen.getByText('Pending print calibration → Partially validated')).toBeInTheDocument()
    expect(screen.getByText('Current head matches')).toBeInTheDocument()

    const delta = screen.getByRole('table', {
      name: 'Exact recommendation values before and after promotion',
    })
    expect(within(delta).getByText('0.6 mm')).toBeInTheDocument()
    expect(within(delta).getByText('0.72 mm')).toBeInTheDocument()
    expect(within(delta).getByText('Engineering baseline · Provisional')).toBeInTheDocument()
    expect(within(delta).getByText('Printed calibration · Moderate')).toBeInTheDocument()
    expect(screen.getByRole('region', {
      name: 'Scrollable recommendation changes',
    })).toHaveAttribute('tabindex', '0')

    expect(screen.getByText('An uncertain result prevents a defensible transition.')).toBeInTheDocument()
    expect(screen.getByText('2 included · 1 excluded')).toBeInTheDocument()

    const observations = screen.getByRole('table', {
      name: 'Every source calibration observation and exclusion reason',
    })
    expect(screen.getByRole('region', {
      name: 'Scrollable source observations',
    })).toHaveAttribute('tabindex', '0')
    expect(within(observations).getByText('Measured 0 mm')).toBeInTheDocument()
    expect(within(observations).getByText('X/Y 0.56 × 0.63 mm')).toBeInTheDocument()
    expect(within(observations).getByText('Uncertain')).toBeInTheDocument()
    expect(within(observations).getByText(
      'Uncertain observations are retained but excluded from the bracket.',
    )).toBeInTheDocument()

    fireEvent.click(within(observations).getAllByRole('button', {
      name: 'calibration-run-0001',
    })[0])
    expect(openRun).toHaveBeenCalledWith('calibration-run-0001')
  })

  it('requires explicit confirmation and sends the exact active fingerprint on acceptance', async () => {
    const accept = vi.fn().mockResolvedValue(undefined)
    render(
      <CalibrationProposalReview
        resource={pendingResource()}
        catalog={catalog()}
        onAccept={accept}
        onReject={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Review acceptance' }))
    const reviewer = screen.getByRole('textbox', { name: 'Reviewer' })
    const reason = screen.getByRole('textbox', { name: 'Review reason' })
    const confirm = screen.getByRole('button', { name: 'Confirm acceptance' })
    expect(reviewer).toHaveFocus()
    expect(confirm).toBeDisabled()

    fireEvent.change(reviewer, { target: { value: '  Kelly  ' } })
    fireEvent.change(reason, { target: { value: '  Reviewed both physical transition runs.  ' } })
    fireEvent.click(screen.getByRole('checkbox', {
      name: /I reviewed the exact catalog fingerprints/,
    }))
    expect(confirm).toBeEnabled()
    fireEvent.click(confirm)

    await waitFor(() => expect(accept).toHaveBeenCalledWith({
      proposal_id: 'calibration-proposal-physical-0001',
      expected_catalog_fingerprint: BASE,
      reviewer: 'Kelly',
      reason: 'Reviewed both physical transition runs.',
    }))
    expect(await screen.findByRole('status')).toHaveTextContent('Acceptance recorded.')
  })

  it('renders an unexpected untested source defensively instead of implying physical evidence', () => {
    const resource = pendingResource()
    resource.proposal.contributions[0] = {
      ...resource.proposal.contributions[0],
      outcome: 'untested',
      measured_dimension_mm: null,
      included: false,
      exclusion_reason: 'Untested observations cannot contribute physical evidence.',
    }
    render(
      <CalibrationProposalReview
        resource={resource}
        catalog={catalog()}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    )

    expect(screen.getByText('Untested')).toHaveAttribute('data-outcome', 'untested')
    expect(screen.getByText(
      'Untested observations cannot contribute physical evidence.',
    )).toBeInTheDocument()
  })

  it('blocks stale acceptance but permits an explicit terminal rejection', async () => {
    const changedHead = 'e'.repeat(64)
    const reject = vi.fn().mockResolvedValue(undefined)
    render(
      <CalibrationProposalReview
        resource={pendingResource()}
        catalog={catalog(changedHead)}
        onAccept={vi.fn()}
        onReject={reject}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Acceptance is blocked because the active catalog no longer matches',
    )
    expect(screen.getByRole('button', { name: 'Review acceptance' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Review rejection' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Reviewer' }), {
      target: { value: 'Kelly' },
    })
    fireEvent.change(screen.getByRole('textbox', { name: 'Review reason' }), {
      target: { value: 'Superseded by evidence against the new catalog.' },
    })
    fireEvent.click(screen.getByRole('checkbox', {
      name: /I reviewed the exact catalog fingerprints/,
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm rejection' }))

    await waitFor(() => expect(reject).toHaveBeenCalledWith(expect.objectContaining({
      expected_catalog_fingerprint: changedHead,
      reviewer: 'Kelly',
    })))
  })

  it('returns focus on Escape and focuses an actionable asynchronous error', async () => {
    const failure = vi.fn().mockRejectedValue(new Error('The active catalog changed during review.'))
    render(
      <CalibrationProposalReview
        resource={pendingResource()}
        catalog={catalog()}
        onAccept={failure}
        onReject={vi.fn()}
      />,
    )

    const trigger = screen.getByRole('button', { name: 'Review acceptance' })
    trigger.focus()
    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('form', { name: 'Confirm acceptance' }), { key: 'Escape' })
    expect(trigger).toHaveFocus()

    fireEvent.click(trigger)
    fireEvent.change(screen.getByRole('textbox', { name: 'Reviewer' }), {
      target: { value: 'Kelly' },
    })
    fireEvent.change(screen.getByRole('textbox', { name: 'Review reason' }), {
      target: { value: 'Reviewed exact evidence.' },
    })
    fireEvent.click(screen.getByRole('checkbox', {
      name: /I reviewed the exact catalog fingerprints/,
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm acceptance' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The active catalog changed during review.')
    expect(alert).toHaveFocus()
    expect(screen.getByRole('button', { name: 'Confirm acceptance' })).toBeEnabled()
  })

  it('renders terminal reviewer evidence without mutable disposition controls', () => {
    const reviewed = {
      ...pendingResource(),
      state: 'accepted' as const,
      reviewer: 'Kelly',
      reviewed_at: '2026-07-18T02:03:04Z',
      review_reason: 'Exact physical evidence reviewed and accepted.',
    }
    render(
      <CalibrationProposalReview
        resource={reviewed}
        catalog={catalog(PROPOSED)}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('Completed review')).toHaveTextContent('Accepted by Kelly')
    expect(screen.getByLabelText('Completed review')).toHaveTextContent(
      'Exact physical evidence reviewed and accepted.',
    )
    expect(screen.getByText('Accepted catalog active')).toBeInTheDocument()
    expect(screen.queryByText(/Acceptance is blocked/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Review acceptance' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Review rejection' })).not.toBeInTheDocument()
  })

  it('keeps the review rail-owned and responsive without fixed-position confirmation UI', () => {
    const css = readFileSync(
      resolve(process.cwd(), 'src/calibration/CalibrationProposalReview.css'),
      'utf8',
    )
    expect(css).toMatch(/@media \(max-width: 520px\), \(max-height: 560px\)/)
    expect(css).not.toMatch(/position:\s*fixed/)
  })
})
