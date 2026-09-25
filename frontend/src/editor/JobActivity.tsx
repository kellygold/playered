import { useEffect, useState } from 'react'
import type { JobResource } from '../contracts'
import './JobActivity.css'

export function JobActivity({ job, label = 'Processing progress' }: {
  job: JobResource | null
  label?: string
}) {
  const [now, setNow] = useState(() => Date.now())
  const active = job?.state === 'running' || job?.state === 'queued'
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active, job?.id])
  const start = Date.parse(job?.started_at ?? job?.created_at ?? '')
  const end = job?.finished_at ? Date.parse(job.finished_at) : now
  const seconds = Number.isFinite(start) && Number.isFinite(end)
    ? Math.max(0, Math.floor((end - start) / 1000)) : null
  const elapsed = seconds === null ? null : seconds < 60
    ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const progress = job ? Math.max(0, Math.min(1, job.progress)) : undefined
  const slicing = job?.stage === 'slicing'

  return (
    <div className="job-activity">
      <div className="job-activity__meta">
        <span>{job?.state === 'queued' ? 'Waiting for a worker' : 'Elapsed'}{elapsed ? ` · ${elapsed}` : ''}</span>
        {progress !== undefined ? <span>{Math.round(progress * 100)}% of job checkpoints</span> : null}
      </div>
      <progress max="1" value={progress} aria-label={label}
        aria-valuetext={progress !== undefined ? `${Math.round(progress * 100)}% of job checkpoints; not a time estimate` : undefined} />
      <p>{job?.state === 'succeeded'
        ? 'Processing finished. Loading the result into the editor.'
        : slicing
        ? 'Bambu Studio is slicing. It may take several minutes without reporting a new checkpoint.'
        : 'Progress updates at checkpoints. A long pause does not necessarily mean it has stopped.'}</p>
    </div>
  )
}
