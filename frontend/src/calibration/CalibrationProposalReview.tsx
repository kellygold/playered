import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from 'react'
import './CalibrationProposalReview.css'

const KEYBOARD_SCROLL_REGION = { role: 'region' as const, tabIndex: 0 }

export type CalibrationProposalState = 'pending' | 'accepted' | 'rejected'
export type CalibrationObservationOutcome = 'untested' | 'pass' | 'fail' | 'uncertain'

export type CalibrationRecommendation = {
  value: number
  unit: 'mm' | 'mm2'
  basis: string
  confidence: string
  rationale: string
  evidence_feature_kinds: string[]
  evidence_run_ids: string[]
}

export type CalibrationRecommendationChange = {
  name: string
  before: CalibrationRecommendation
  after: CalibrationRecommendation
}

export type CalibrationProposalContribution = {
  run_id: string
  feature_id: string
  feature_kind: string
  nominal_dimension_mm: number
  rasterized_dimension_mm: number
  rasterized_width_mm: number
  rasterized_height_mm: number
  outcome: CalibrationObservationOutcome
  measured_dimension_mm: number | null
  notes: string
  included: boolean
  exclusion_reason: string | null
}

export type CalibrationTransitionAnalysis = {
  feature_kind: string
  recommendation_names: string[]
  status: 'proposed' | 'blocked'
  proposed_dimension_mm: number | null
  largest_failed_dimension_mm: number | null
  smallest_passed_dimension_mm: number | null
  reason: string
  contributing_run_ids: string[]
}

export type CalibrationProposal = {
  id: string
  profile_id: string
  process_fingerprint: string
  base_catalog_fingerprint: string
  proposed_catalog_fingerprint: string
  algorithm_version: string
  run_ids: string[]
  analyses: CalibrationTransitionAnalysis[]
  contributions: CalibrationProposalContribution[]
  base_evidence_status: string
  proposed_evidence_status: string
  recommendation_changes: CalibrationRecommendationChange[]
  proposal_sha256: string
}

export type CalibrationProposalResource = {
  proposal: CalibrationProposal
  state: CalibrationProposalState
  created_at: string
  reviewed_at: string | null
  reviewer: string | null
  review_reason: string | null
}

export type CalibrationCatalogReviewContext = {
  active_fingerprint: string
  catalog_id?: string
  catalog_version?: string
}

export type CalibrationDispositionRequest = {
  proposal_id: string
  expected_catalog_fingerprint: string
  reviewer: string
  reason: string
}

export type CalibrationProposalReviewProps = {
  resource: CalibrationProposalResource
  catalog: CalibrationCatalogReviewContext
  onAccept: (request: CalibrationDispositionRequest) => Promise<void> | void
  onReject: (request: CalibrationDispositionRequest) => Promise<void> | void
  onOpenRun?: (runId: string) => void
}

type Decision = 'accept' | 'reject'

function words(value: string): string {
  return value.replaceAll('_', ' ').replaceAll('-', ' ')
}

function title(value: string): string {
  const normalized = words(value)
  return normalized.charAt(0).toUpperCase() + normalized.slice(1)
}

function statusLabel(value: CalibrationProposalState): string {
  if (value === 'pending') return 'Pending review'
  return value === 'accepted' ? 'Accepted' : 'Rejected'
}

function shortHash(value: string): string {
  return `${value.slice(0, 10)}…${value.slice(-8)}`
}

function exactNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(9)))
}

function dimension(value: number | null): string {
  return value === null ? '—' : `${exactNumber(value)} mm`
}

function recommendationValue(value: CalibrationRecommendation): string {
  return `${exactNumber(value.value)} ${value.unit === 'mm2' ? 'mm²' : 'mm'}`
}

function outcomeLabel(value: CalibrationObservationOutcome): string {
  if (value === 'pass') return 'Passed'
  if (value === 'fail') return 'Failed'
  return value === 'uncertain' ? 'Uncertain' : 'Untested'
}

function failureMessage(error: unknown, decision: Decision): string {
  if (error instanceof Error && error.message) return error.message
  return `${decision === 'accept' ? 'Acceptance' : 'Rejection'} could not be recorded.`
}

function RunReference({
  runId,
  onOpenRun,
}: {
  runId: string
  onOpenRun?: (runId: string) => void
}) {
  if (!onOpenRun) return <code>{runId}</code>
  return (
    <button type="button" className="calibration-proposal-review__run" onClick={() => onOpenRun(runId)}>
      {runId}
    </button>
  )
}

export function CalibrationProposalReview({
  resource,
  catalog,
  onAccept,
  onReject,
  onOpenRun,
}: CalibrationProposalReviewProps) {
  const proposal = resource.proposal
  const headingId = useId()
  const decisionHeadingId = useId()
  const staleAlertId = useId()
  const [decision, setDecision] = useState<Decision | null>(null)
  const [reviewer, setReviewer] = useState('')
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const reviewerRef = useRef<HTMLInputElement>(null)
  const errorRef = useRef<HTMLDivElement>(null)
  const acceptTriggerRef = useRef<HTMLButtonElement>(null)
  const rejectTriggerRef = useRef<HTMLButtonElement>(null)
  const returnDecisionRef = useRef<Decision | null>(null)

  const baseCatalogIsActive = catalog.active_fingerprint === proposal.base_catalog_fingerprint
  const acceptedCatalogIsActive = resource.state === 'accepted'
    && catalog.active_fingerprint === proposal.proposed_catalog_fingerprint
  const catalogIdentityMatchesReview = baseCatalogIsActive || acceptedCatalogIsActive
  const stale = resource.state === 'pending' && !baseCatalogIsActive
  const canSubmit = Boolean(reviewer.trim() && reason.trim() && confirmed && !submitting)
  const includedCount = useMemo(
    () => proposal.contributions.filter((item) => item.included).length,
    [proposal.contributions],
  )
  const excludedCount = proposal.contributions.length - includedCount

  useEffect(() => {
    if (decision) reviewerRef.current?.focus()
    else if (returnDecisionRef.current === 'accept') acceptTriggerRef.current?.focus()
    else if (returnDecisionRef.current === 'reject') rejectTriggerRef.current?.focus()
  }, [decision])

  useEffect(() => {
    if (error) errorRef.current?.focus()
  }, [error])

  const beginDecision = (next: Decision) => {
    returnDecisionRef.current = next
    setDecision(next)
    setReviewer(resource.reviewer ?? '')
    setReason('')
    setConfirmed(false)
    setError(null)
    setAnnouncement('')
  }

  const cancelDecision = () => {
    if (submitting) return
    setDecision(null)
    setConfirmed(false)
    setError(null)
  }

  const handleDecisionKeyDown = (event: KeyboardEvent<HTMLFormElement>) => {
    if (event.key !== 'Escape' || submitting) return
    event.preventDefault()
    cancelDecision()
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!decision || !canSubmit || (decision === 'accept' && stale)) return
    const selectedDecision = decision
    const request = {
      proposal_id: proposal.id,
      expected_catalog_fingerprint: catalog.active_fingerprint,
      reviewer: reviewer.trim(),
      reason: reason.trim(),
    }
    setSubmitting(true)
    setError(null)
    setAnnouncement(
      `${selectedDecision === 'accept' ? 'Accepting' : 'Rejecting'} proposal…`,
    )
    try {
      await (selectedDecision === 'accept' ? onAccept(request) : onReject(request))
      setAnnouncement(
        `${selectedDecision === 'accept' ? 'Acceptance' : 'Rejection'} recorded.`,
      )
      setDecision(null)
    } catch (caught) {
      setError(failureMessage(caught, selectedDecision))
      setAnnouncement('')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="calibration-proposal-review" aria-labelledby={headingId}>
      <header className="calibration-proposal-review__heading">
        <div>
          <span className="eyebrow">Physical calibration authority</span>
          <h2 id={headingId}>Proposal review</h2>
          <p>
            Review every changed recommendation and its exact source observations before moving
            the active catalog head.
          </p>
        </div>
        <span className="calibration-proposal-review__state" data-state={resource.state}>
          {statusLabel(resource.state)}
        </span>
      </header>

      <dl className="calibration-proposal-review__identity" aria-label="Proposal identity">
        <div>
          <dt>Profile</dt>
          <dd>{proposal.profile_id}</dd>
        </div>
        <div>
          <dt>Algorithm</dt>
          <dd>{proposal.algorithm_version}</dd>
        </div>
        <div>
          <dt>Evidence state</dt>
          <dd>{title(proposal.base_evidence_status)} → {title(proposal.proposed_evidence_status)}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{resource.created_at}</dd>
        </div>
      </dl>

      <section className="calibration-proposal-review__guard" aria-labelledby={`${headingId}-guard`}>
        <header>
          <div>
            <span className="eyebrow">Catalog guard</span>
            <h3 id={`${headingId}-guard`}>Exact immutable identities</h3>
          </div>
          <span data-match={catalogIdentityMatchesReview}>
            {acceptedCatalogIsActive
              ? 'Accepted catalog active'
              : baseCatalogIsActive
                ? 'Current head matches'
                : 'Head changed'}
          </span>
        </header>
        <dl>
          <div>
            <dt>Expected active</dt>
            <dd title={proposal.base_catalog_fingerprint}>{shortHash(proposal.base_catalog_fingerprint)}</dd>
          </div>
          <div>
            <dt>Current active</dt>
            <dd title={catalog.active_fingerprint}>{shortHash(catalog.active_fingerprint)}</dd>
          </div>
          <div>
            <dt>Proposed catalog</dt>
            <dd title={proposal.proposed_catalog_fingerprint}>{shortHash(proposal.proposed_catalog_fingerprint)}</dd>
          </div>
          <div>
            <dt>Proposal</dt>
            <dd title={proposal.proposal_sha256}>{shortHash(proposal.proposal_sha256)}</dd>
          </div>
          <div>
            <dt>Process</dt>
            <dd title={proposal.process_fingerprint}>{shortHash(proposal.process_fingerprint)}</dd>
          </div>
        </dl>
        {stale ? (
          <p className="calibration-proposal-review__stale" id={staleAlertId} role="alert">
            Acceptance is blocked because the active catalog no longer matches this proposal's
            base. Reject it or review a new proposal derived from the current head.
          </p>
        ) : null}
      </section>

      <section className="calibration-proposal-review__changes" aria-labelledby={`${headingId}-changes`}>
        <header>
          <div>
            <span className="eyebrow">Recommendation delta</span>
            <h3 id={`${headingId}-changes`}>Before and after</h3>
          </div>
          <span>{proposal.recommendation_changes.length} changed</span>
        </header>
        {proposal.recommendation_changes.length ? (
          <div
            {...KEYBOARD_SCROLL_REGION}
            className="calibration-proposal-review__table-scroll"
            aria-label="Scrollable recommendation changes"
          >
            <table>
              <caption className="sr-only">Exact recommendation values before and after promotion</caption>
              <thead>
                <tr>
                  <th scope="col">Recommendation</th>
                  <th scope="col">Before</th>
                  <th scope="col">After</th>
                </tr>
              </thead>
              <tbody>
                {proposal.recommendation_changes.map((change) => (
                  <tr key={change.name}>
                    <th scope="row">{title(change.name)}</th>
                    <td>
                      <strong>{recommendationValue(change.before)}</strong>
                      <span>{title(change.before.basis)} · {title(change.before.confidence)}</span>
                      <p>{change.before.rationale}</p>
                    </td>
                    <td>
                      <strong>{recommendationValue(change.after)}</strong>
                      <span>{title(change.after.basis)} · {title(change.after.confidence)}</span>
                      <p>{change.after.rationale}</p>
                      <small>
                        Evidence: {change.after.evidence_run_ids.length
                          ? change.after.evidence_run_ids.join(', ')
                          : 'No physical run IDs'}
                      </small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="calibration-proposal-review__empty">This proposal contains no recommendation changes.</p>
        )}
      </section>

      <section className="calibration-proposal-review__analyses" aria-labelledby={`${headingId}-analyses`}>
        <header>
          <div>
            <span className="eyebrow">Transition analysis</span>
            <h3 id={`${headingId}-analyses`}>Included and blocked evidence</h3>
          </div>
          <span>{includedCount} included · {excludedCount} excluded</span>
        </header>
        <ul>
          {proposal.analyses.map((analysis) => (
            <li key={analysis.feature_kind} data-status={analysis.status}>
              <div>
                <strong>{title(analysis.feature_kind)}</strong>
                <span>{analysis.status === 'proposed' ? 'Transition proposed' : 'Blocked'}</span>
              </div>
              <dl>
                <div><dt>Largest fail</dt><dd>{dimension(analysis.largest_failed_dimension_mm)}</dd></div>
                <div><dt>Smallest pass</dt><dd>{dimension(analysis.smallest_passed_dimension_mm)}</dd></div>
                <div><dt>Proposed</dt><dd>{dimension(analysis.proposed_dimension_mm)}</dd></div>
              </dl>
              <p>{analysis.reason}</p>
              <small>{analysis.recommendation_names.map(title).join(', ')}</small>
            </li>
          ))}
        </ul>
      </section>

      <section className="calibration-proposal-review__observations" aria-labelledby={`${headingId}-observations`}>
        <header>
          <div>
            <span className="eyebrow">Source observations</span>
            <h3 id={`${headingId}-observations`}>Exact physical record</h3>
          </div>
          <span>{proposal.contributions.length} observations</span>
        </header>
        <div
          {...KEYBOARD_SCROLL_REGION}
          className="calibration-proposal-review__table-scroll"
          aria-label="Scrollable source observations"
        >
          <table>
            <caption className="sr-only">Every source calibration observation and exclusion reason</caption>
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Dimensions</th>
                <th scope="col">Observation</th>
                <th scope="col">Use</th>
              </tr>
            </thead>
            <tbody>
              {proposal.contributions.map((item) => (
                <tr key={`${item.run_id}:${item.feature_id}`} data-included={item.included}>
                  <th scope="row">
                    <RunReference runId={item.run_id} onOpenRun={onOpenRun} />
                    <code>{item.feature_id}</code>
                    <span>{title(item.feature_kind)}</span>
                  </th>
                  <td>
                    <span>Nominal {dimension(item.nominal_dimension_mm)}</span>
                    <span>Raster {dimension(item.rasterized_dimension_mm)}</span>
                    <span>
                      X/Y {exactNumber(item.rasterized_width_mm)} × {exactNumber(item.rasterized_height_mm)} mm
                    </span>
                  </td>
                  <td>
                    <strong data-outcome={item.outcome}>{outcomeLabel(item.outcome)}</strong>
                    <span>
                      Measured {item.measured_dimension_mm === null
                        ? 'not recorded'
                        : dimension(item.measured_dimension_mm)}
                    </span>
                    <span>{item.notes || 'No observation notes.'}</span>
                  </td>
                  <td>
                    <strong>{item.included ? 'Included' : 'Excluded'}</strong>
                    <span>{item.exclusion_reason ?? 'Used by the transition analysis.'}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {resource.state !== 'pending' ? (
        <section className="calibration-proposal-review__reviewed" aria-label="Completed review">
          <strong>{statusLabel(resource.state)} by {resource.reviewer}</strong>
          <span>{resource.reviewed_at}</span>
          <p>{resource.review_reason}</p>
        </section>
      ) : (
        <section className="calibration-proposal-review__decision" aria-labelledby={`${headingId}-decision`}>
          <header>
            <div>
              <span className="eyebrow">Explicit disposition</span>
              <h3 id={`${headingId}-decision`}>Accept or reject</h3>
            </div>
          </header>
          <div className="calibration-proposal-review__decision-actions" hidden={decision !== null}>
              <button
                ref={rejectTriggerRef}
                type="button"
                className="calibration-proposal-review__reject"
                onClick={() => beginDecision('reject')}
              >
                Review rejection
              </button>
              <button
                ref={acceptTriggerRef}
                type="button"
                className="calibration-proposal-review__accept"
                disabled={stale}
                aria-describedby={stale ? staleAlertId : undefined}
                onClick={() => beginDecision('accept')}
              >
                Review acceptance
              </button>
          </div>
          {decision ? (
            // Escape is an explicit cancel path for this inline confirmation form.
            // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
            <form
              aria-labelledby={decisionHeadingId}
              onSubmit={submit}
              onKeyDown={handleDecisionKeyDown}
            >
              <div className="calibration-proposal-review__form-heading">
                <div>
                  <span>{decision === 'accept' ? 'Catalog promotion' : 'Terminal rejection'}</span>
                  <h4 id={decisionHeadingId}>
                    Confirm {decision === 'accept' ? 'acceptance' : 'rejection'}
                  </h4>
                </div>
                <button type="button" onClick={cancelDecision} disabled={submitting}>
                  Cancel
                </button>
              </div>
              <p>
                {decision === 'accept'
                  ? 'Acceptance atomically promotes the proposed catalog from the exact active fingerprint below.'
                  : 'Rejection is terminal for this proposal and records the reviewer and reason.'}
              </p>
              <code className="calibration-proposal-review__expected">
                Expected active: {catalog.active_fingerprint}
              </code>
              <label>
                <span>Reviewer</span>
                <input
                  ref={reviewerRef}
                  value={reviewer}
                  maxLength={200}
                  required
                  autoComplete="name"
                  onChange={(event) => setReviewer(event.target.value)}
                />
              </label>
              <label>
                <span>Review reason</span>
                <textarea
                  value={reason}
                  maxLength={2000}
                  rows={4}
                  required
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
              <label className="calibration-proposal-review__confirmation">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                <span>
                  I reviewed the exact catalog fingerprints, recommendation delta, and source
                  observations for this disposition.
                </span>
              </label>
              {error ? (
                <div
                  ref={errorRef}
                  className="calibration-proposal-review__error"
                  role="alert"
                  tabIndex={-1}
                >
                  <strong>Review was not saved</strong>
                  <span>{error}</span>
                </div>
              ) : null}
              <button
                type="submit"
                className={decision === 'accept'
                  ? 'calibration-proposal-review__accept'
                  : 'calibration-proposal-review__reject'}
                disabled={!canSubmit || (decision === 'accept' && stale)}
              >
                {submitting
                  ? 'Recording…'
                  : `Confirm ${decision === 'accept' ? 'acceptance' : 'rejection'}`}
              </button>
            </form>
          ) : null}
        </section>
      )}

      <p className="calibration-proposal-review__announcement" role="status" aria-live="polite">
        {announcement}
      </p>
    </section>
  )
}
