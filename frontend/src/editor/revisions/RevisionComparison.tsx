import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchRevision } from '../../api'
import type { RevisionResource, RevisionSummary } from '../../contracts'
import { compareRevisions, regionalEditEvidence } from './revisionModel'
import './RevisionComparison.css'

type RevisionComparisonProps = {
  projectId: string
  revision: RevisionResource
  revisions: RevisionSummary[]
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'The comparison revision could not be opened.'
}

export function RevisionComparison({ projectId, revision, revisions }: RevisionComparisonProps) {
  return (
    <RevisionComparisonSession
      key={`${projectId}:${revision.id}`}
      projectId={projectId}
      revision={revision}
      revisions={revisions}
    />
  )
}

function RevisionComparisonSession({ projectId, revision, revisions }: RevisionComparisonProps) {
  const candidates = revisions.filter((candidate) => candidate.id !== revision.id)
  const defaultId = revision.parent_revision_id ?? candidates[0]?.id ?? ''
  const [baselineId, setBaselineId] = useState(defaultId)
  const [baseline, setBaseline] = useState<RevisionResource | null>(null)
  const [loading, setLoading] = useState(Boolean(defaultId))
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const sequence = useRef(0)

  useEffect(() => {
    if (!baselineId) return
    const controller = new AbortController()
    const request = ++sequence.current
    fetchRevision(projectId, baselineId, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted && request === sequence.current) setBaseline(value)
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError' && request === sequence.current) {
          setBaseline(null)
          setError(message(reason))
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && request === sequence.current) setLoading(false)
      })
    return () => controller.abort()
  }, [attempt, baselineId, projectId])

  const comparison = useMemo(
    () => baseline ? compareRevisions(baseline, revision) : null,
    [baseline, revision],
  )

  return (
    <section className="revision-comparison" aria-label={`Compare ${revision.label}`}>
      <div className="revision-comparison-heading">
        <div>
          <span className="eyebrow">Exact delta</span>
          <h4>Compare revisions</h4>
        </div>
        {comparison ? (
          <span>{comparison.config.length + comparison.operations.length} changes</span>
        ) : null}
      </div>
      {candidates.length ? (
        <label>
          <span>Compare against</span>
          <select
            aria-label="Comparison revision"
            value={baselineId}
            onChange={(event) => {
              setBaselineId(event.target.value)
              setBaseline(null)
              setError(null)
              setLoading(true)
            }}
          >
            {candidates.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.label}{candidate.id === revision.parent_revision_id ? ' · parent' : ''}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <p className="revision-comparison-empty">Publish another revision to compare exact settings and edits.</p>
      )}
      {loading ? <p className="revision-comparison-empty" role="status">Comparing immutable snapshots…</p> : null}
      {error ? (
        <div className="revision-comparison-error" role="alert">
          <p>{error}</p>
          <button type="button" onClick={() => {
            setError(null)
            setLoading(true)
            setAttempt((value) => value + 1)
          }}>Retry comparison</button>
        </div>
      ) : null}
      {comparison?.unchanged ? (
        <p className="revision-comparison-empty">These revisions have the same configuration and manual operations.</p>
      ) : null}
      {comparison && comparison.config.length > 0 ? (
        <div className="revision-delta-group">
          <h5>Settings · {comparison.config.length}</h5>
          <ul>
            {comparison.config.map((delta) => (
              <li key={delta.path}>
                <strong>{delta.label}</strong>
                <span><del>{delta.before}</del><ins>{delta.after}</ins></span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {comparison && comparison.operations.length > 0 ? (
        <div className="revision-delta-group">
          <h5>Manual operations · {comparison.operations.length}</h5>
          <ul>
            {comparison.operations.map((delta) => (
              <li key={delta.identity} data-kind={delta.kind}>
                <strong>{delta.label}</strong>
                <span>
                  {regionalEditEvidence(delta.after ?? delta.before!) ? (
                    <em data-reproducibility={regionalEditEvidence(delta.after ?? delta.before!)?.classification}>
                      {regionalEditEvidence(delta.after ?? delta.before!)?.label}
                    </em>
                  ) : null}
                  {delta.kind}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}
