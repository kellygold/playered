import { useEffect, useRef, useState } from 'react'
import './WorkspaceRecoveryPanel.css'

export type InterruptedJobEvidence = {
  job_id: string
  project_id: string
  type: 'preview' | 'geometry' | 'export' | 'validation'
  state_before_restart: 'queued' | 'running'
  stage_before_restart: string
  state_after_recovery: 'failed'
  disposition: 'retry_required'
  resumable: false
  retryable: true
  action: string
}

export type StartupReconciliation = {
  id: string
  status: 'healthy' | 'attention' | 'critical'
  started_at: string
  completed_at: string
  interrupted_jobs: InterruptedJobEvidence[]
  stale_temp_file_count: number
  orphan_file_count: number
  database_integrity: boolean
  foreign_key_integrity: boolean
  automatic_deletions: number
  recommended_actions: string[]
}

export type CleanupPlan = {
  generated_at: string
  minimum_age_seconds: number
  plan_sha256: string
  candidate_count: number
  reclaimable_bytes: number
  candidates: {
    relative_path: string
    namespace: string
    reason: string
    byte_size: number
    sha256: string
    mtime_ns: number
  }[]
}

type WorkspaceRecoveryPanelProps = {
  report: StartupReconciliation
  onReconcile: () => Promise<StartupReconciliation>
  onReviewCleanup: () => Promise<CleanupPlan>
  onClose: () => void
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'

function hasIntegrityProblem(report: StartupReconciliation): boolean {
  return report.status === 'critical' || !report.database_integrity || !report.foreign_key_integrity
}

function statusTitle(report: StartupReconciliation): string {
  if (hasIntegrityProblem(report)) return 'Your saved workspace needs attention'
  if (report.interrupted_jobs.length > 0) return 'Some work was interrupted'
  return 'Workspace status'
}

function timestamp(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

function bytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

export function WorkspaceRecoveryNotice({
  report,
  onOpen,
}: {
  report: StartupReconciliation
  onOpen: () => void
}) {
  const critical = hasIntegrityProblem(report)
  const affected = report.interrupted_jobs.length
  if (!critical && affected === 0) return null
  return (
    <section className={`workspace-recovery-notice is-${critical ? 'critical' : 'attention'}`} aria-label="Startup recovery status">
      <div>
        <strong>{statusTitle(report)}</strong>
        <span>
          {critical
            ? 'A workspace check found a problem. Open the details for next steps.'
            : `${affected} task${affected === 1 ? '' : 's'} did not finish. Open the details to see what to retry.`}
        </span>
      </div>
      <button type="button" onClick={onOpen}>View details</button>
    </section>
  )
}

export function WorkspaceRecoveryPanel({
  report,
  onReconcile,
  onReviewCleanup,
  onClose,
}: WorkspaceRecoveryPanelProps) {
  const [current, setCurrent] = useState(report)
  const [checking, setChecking] = useState(false)
  const [planningCleanup, setPlanningCleanup] = useState(false)
  const [cleanupPlan, setCleanupPlan] = useState<CleanupPlan | null>(null)
  const [error, setError] = useState('')
  const closeRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLElement>(null)

  useEffect(() => {
    closeRef.current?.focus()
    function handleDialogKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = [...(panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])]
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleDialogKeyDown)
    return () => document.removeEventListener('keydown', handleDialogKeyDown)
  }, [onClose])

  async function checkAgain() {
    if (checking) return
    setChecking(true)
    setError('')
    try {
      setCurrent(await onReconcile())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The recovery check could not finish.')
    } finally {
      setChecking(false)
    }
  }

  async function reviewCleanup() {
    if (planningCleanup) return
    setPlanningCleanup(true)
    setError('')
    try {
      setCleanupPlan(await onReviewCleanup())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The cleanup dry run could not finish.')
    } finally {
      setPlanningCleanup(false)
    }
  }

  const hasCleanupCandidates = current.stale_temp_file_count + current.orphan_file_count > 0
  const critical = hasIntegrityProblem(current)
  const needsAction = critical || current.interrupted_jobs.length > 0
  const status = critical ? 'critical' : needsAction ? 'attention' : 'healthy'
  return (
    <div className="workspace-recovery-backdrop" role="presentation">
      <section
        ref={panelRef}
        className="workspace-recovery-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-recovery-title"
        tabIndex={-1}
      >
        <header>
          <div>
            <span className={`workspace-recovery-status is-${status}`}>
              {critical ? 'Needs attention' : needsAction ? 'Retry needed' : 'Checked'}
            </span>
            <h2 id="workspace-recovery-title">{statusTitle(current)}</h2>
            <p>Checked {timestamp(current.completed_at)}. Recovery never deleted workspace files.</p>
          </div>
          <button ref={closeRef} type="button" className="workspace-recovery-close" onClick={onClose} aria-label="Close recovery details">×</button>
        </header>

        <div className="workspace-recovery-scroll">
          <dl className="workspace-recovery-summary">
            <div><dt>Interrupted jobs</dt><dd>{current.interrupted_jobs.length}</dd></div>
            <div><dt>Temporary files</dt><dd>{current.stale_temp_file_count}</dd></div>
            <div><dt>Orphan files</dt><dd>{current.orphan_file_count}</dd></div>
            <div><dt>Automatic deletions</dt><dd>{current.automatic_deletions}</dd></div>
          </dl>

          <section aria-labelledby="recovery-integrity-title">
            <h3 id="recovery-integrity-title">Safety checks</h3>
            <ul className="workspace-recovery-checks">
              <li className={current.database_integrity ? 'is-pass' : 'is-fail'}>Database structure {current.database_integrity ? 'passed' : 'failed'}</li>
              <li className={current.foreign_key_integrity ? 'is-pass' : 'is-fail'}>Database relationships {current.foreign_key_integrity ? 'passed' : 'failed'}</li>
            </ul>
          </section>

          {current.interrupted_jobs.length > 0 ? (
            <section aria-labelledby="recovery-jobs-title">
              <h3 id="recovery-jobs-title">Jobs requiring a retry</h3>
              <div className="workspace-recovery-jobs">
                {current.interrupted_jobs.map((job) => (
                  <article key={job.job_id}>
                    <div><strong>{job.type}</strong><code>{job.job_id}</code></div>
                    <p>Stopped during {job.stage_before_restart}. It was not silently resumed.</p>
                    <p>{job.action}</p>
                  </article>
                ))}
              </div>
            </section>
          ) : null}

          <section aria-labelledby="recovery-actions-title">
            <h3 id="recovery-actions-title">{needsAction ? 'Next steps' : 'Storage'}</h3>
            {needsAction ? (
              <ul>{current.recommended_actions.map((action) => <li key={action}>{action}</li>)}</ul>
            ) : (
              <p>{hasCleanupCandidates ? 'Unused files are available for optional cleanup. You can keep working.' : 'No action needed.'}</p>
            )}
          </section>

          {cleanupPlan ? (
            <section className="workspace-cleanup-plan" aria-labelledby="cleanup-plan-title">
              <div>
                <h3 id="cleanup-plan-title">Cleanup dry run</h3>
                <strong>{cleanupPlan.candidate_count} candidate{cleanupPlan.candidate_count === 1 ? '' : 's'} · {bytes(cleanupPlan.reclaimable_bytes)}</strong>
              </div>
              <p>Nothing has been deleted. Review these exact paths before applying this fingerprinted plan.</p>
              {cleanupPlan.candidates.length > 0 ? (
                <ul>
                  {cleanupPlan.candidates.map((candidate) => (
                    <li key={candidate.relative_path}>
                      <code>{candidate.relative_path}</code>
                      <span>{candidate.reason} · {bytes(candidate.byte_size)}</span>
                    </li>
                  ))}
                </ul>
              ) : <p>No files currently meet the cleanup safety age.</p>}
              <small>Plan {cleanupPlan.plan_sha256.slice(0, 12)}… · minimum age {cleanupPlan.minimum_age_seconds}s</small>
            </section>
          ) : null}

          {error ? <p className="workspace-recovery-error" role="alert">{error}</p> : null}
        </div>

        <footer>
          <button type="button" onClick={() => void checkAgain()} disabled={checking}>{checking ? 'Checking…' : 'Run safe check again'}</button>
          <button type="button" onClick={() => void reviewCleanup()} disabled={!hasCleanupCandidates || planningCleanup}>{planningCleanup ? 'Planning…' : 'Review cleanup dry run'}</button>
          <button type="button" className="button-primary" onClick={onClose}>Done</button>
        </footer>
      </section>
    </div>
  )
}
