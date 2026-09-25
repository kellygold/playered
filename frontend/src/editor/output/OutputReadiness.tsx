import { useId } from 'react'
import type { JobResource } from '../../contracts'
import { JobActivity } from '../JobActivity'
import type { OutputActionId, OutputReadinessModel } from './outputReadinessModel'
import './OutputReadiness.css'

export type OutputReadinessProps = {
  model: OutputReadinessModel
  activeJob?: JobResource | null
  showAction?: boolean
  onAction?: (action: OutputActionId) => void
}

export function OutputReadiness({ model, onAction, activeJob, showAction = true }: OutputReadinessProps) {
  const generatedId = useId().replaceAll(':', '')
  const headingId = `output-readiness-${generatedId}`
  const actionReasonId = `output-action-reason-${generatedId}`
  const primaryAction = model.primaryAction

  return (
    <section className="output-readiness" aria-labelledby={headingId}>
      <div className="output-readiness__summary">
        <div>
          <span className="output-readiness__eyebrow">Output readiness</span>
          <h2 id={headingId}>{model.headline}</h2>
          <p role="status" aria-live="polite" aria-atomic="true">{model.summary}</p>
        </div>
        {primaryAction && showAction ? (
          <div className="output-readiness__action">
            <button
              type="button"
              data-primary="true"
              aria-disabled={primaryAction.disabled || undefined}
              aria-describedby={primaryAction.disabledReason ? actionReasonId : undefined}
              onClick={() => {
                if (!primaryAction.disabled) onAction?.(primaryAction.id)
              }}
            >
              {primaryAction.label}
            </button>
            {primaryAction.disabledReason ? (
              <small id={actionReasonId}>{primaryAction.disabledReason}</small>
            ) : null}
          </div>
        ) : null}
      </div>

      <ol className="output-readiness__stages" aria-label="Output stages">
        {model.stages.map((stage, index) => (
          <li
            key={stage.id}
            data-state={stage.state}
            aria-current={stage.current ? 'step' : undefined}
          >
            <span className="output-readiness__index" aria-hidden="true">
              {stage.state === 'complete' ? '✓' : index + 1}
            </span>
            <div>
              <strong>{stage.label}</strong>
              <span className="output-readiness__stage-status">{stage.status}</span>
            </div>
          </li>
        ))}
      </ol>
      {activeJob && model.stages.some((stage) => stage.state === 'working') ? <JobActivity job={activeJob} label="3MF build progress" /> : null}
      <details className="output-readiness__details">
        <summary>Build details</summary>
        <dl>
          {model.stages.map((stage) => (
            <div key={stage.id}><dt>{stage.label}</dt><dd>{stage.detail}</dd></div>
          ))}
        </dl>
      </details>
    </section>
  )
}
