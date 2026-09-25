import { useId } from 'react'
import { JobActivity } from '../JobActivity'
import { previewCheckpointLabel } from '../preview/previewJobPresentation'
import {
  type PreviewCoordinatorPhase,
  type PreviewCoordinatorState,
  type PreviewFailureDiagnostic,
} from '../preview'
import './PreviewJobStatus.css'

export type PreviewJobStatusProps = {
  state: PreviewCoordinatorState
  onCancel?: () => void
  onRetry?: () => void
  disabled?: boolean
}

function isActive(phase: PreviewCoordinatorPhase): boolean {
  return phase === 'submitting' || phase === 'queued' || phase === 'running' || phase === 'canceling'
}

function phaseTitle(state: PreviewCoordinatorState): string {
  if (state.phase === 'idle') return 'Preview waiting'
  if (state.phase === 'submitting') return 'Submitting preview'
  if (state.phase === 'canceling') return 'Canceling preview'
  if (state.phase === 'failed') return state.failure?.title ?? 'Preview failed'
  if (state.phase === 'canceled') return 'Preview canceled'
  if (state.phase === 'superseded') return 'Preview superseded'
  if (state.job?.state === 'succeeded' && state.phase !== 'succeeded') return 'Loading preview files'
  if (state.job) return previewCheckpointLabel(state.job.stage, state.job.progress)
  return state.phase === 'succeeded' ? 'Preview complete' : 'Preview processing'
}

function freshnessLabel(state: PreviewCoordinatorState): string {
  if (state.resultFreshness === 'current') return 'Current preview'
  if (state.resultFreshness === 'stale') {
    return isActive(state.phase) ? 'Stale preview visible' : 'Stale preview'
  }
  return 'No preview yet'
}

function diagnosticEntries(details: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(details)
    .sort(([first], [second]) => first.localeCompare(second))
    .map(([key, value]) => [
      key.replaceAll('_', ' '),
      typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
        ? String(value)
        : JSON.stringify(value),
    ])
}

function FailureDetails({ failure }: { failure: PreviewFailureDiagnostic }) {
  const entries = diagnosticEntries(failure.details)
  return (
    <details className="preview-job-diagnostics">
      <summary>Technical details</summary>
      <dl>
        <div>
          <dt>Error code</dt>
          <dd><code>{failure.code}</code></dd>
        </div>
        {failure.requestId ? (
          <div>
            <dt>Request ID</dt>
            <dd><code>{failure.requestId}</code></dd>
          </div>
        ) : null}
        {entries.map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </details>
  )
}

export function PreviewJobStatus({
  state,
  onCancel,
  onRetry,
  disabled = false,
}: PreviewJobStatusProps) {
  const headingId = useId()
  const active = isActive(state.phase)
  const cancelable = active && state.phase !== 'canceling' && !!onCancel
  const failure = state.failure

  return (
    <section
      className="preview-job-status"
      data-phase={state.phase}
      aria-labelledby={headingId}
      aria-busy={active}
    >
      <div className="preview-job-heading">
        <div>
          <span className="preview-job-eyebrow">Preview job</span>
          <h3 id={headingId}>{phaseTitle(state)}</h3>
        </div>
        <span className={`preview-job-freshness is-${state.resultFreshness}`}>
          {freshnessLabel(state)}
        </span>
      </div>

      <p className="preview-job-announcement visually-hidden" role="status" aria-live="polite" aria-atomic="true">
        {state.announcement}
      </p>

      {active ? (
        <div className="preview-job-progress">
          <JobActivity job={state.job} label="Preview rendering progress" />
          {state.resultFreshness === 'stale' ? (
            <p>The previous preview remains visible until this request completes.</p>
          ) : null}
        </div>
      ) : null}

      {failure ? (
        <div className="preview-job-failure" role="alert">
          <strong>{failure.message}</strong>
          {failure.suggestion ? <p>{failure.suggestion}</p> : null}
          <FailureDetails failure={failure} />
        </div>
      ) : null}

      {state.phase === 'succeeded' && state.result?.reprocessing_plan ? (
        <details className="preview-job-reprocessing" data-mode={state.result.reprocessing_plan.mode}>
          <summary>Processing details</summary>
          <strong>
            {state.result.reprocessing_plan.mode === 'scoped'
              ? 'Scoped reprocessing'
              : 'Full reprocessing'}
          </strong>
          <p>{state.result.reprocessing_plan.message}</p>
          {state.result.reprocessing_plan.mode === 'scoped' ? (
            <small>
              Reused {state.result.reprocessing_plan.reused_stages.join(' + ')} · affected{' '}
              {state.result.reprocessing_plan.affected_pixel_count.toLocaleString()} pixels
            </small>
          ) : null}
        </details>
      ) : null}

      <div className="preview-job-actions">
        {cancelable ? (
          <button type="button" disabled={disabled} onClick={onCancel}>
            Cancel preview
          </button>
        ) : null}
        {failure?.retryable && onRetry ? (
          <button type="button" disabled={disabled} data-primary="true" onClick={onRetry}>
            Retry preview
          </button>
        ) : null}
      </div>
    </section>
  )
}
