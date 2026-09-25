import { useEffect, useMemo, useRef, useState } from 'react'
import type { MuralPlanResource } from './types'
import type { ExportProfileSettings } from '../contracts'
import {
  type MuralBuildRequest,
  type MuralBuildTransport,
} from './MuralBuildCoordinator'
import { muralBuildReadiness } from './muralBuildReadiness'
import { MuralSeamQA } from './MuralSeamQA'
import { useMuralBuildCoordinator } from './useMuralBuildCoordinator'
import './MuralBuildWorkflow.css'

export type MuralBuildWorkflowProps = {
  projectId: string
  processedArtifactId: string
  previewJobId: string | null
  expectedDraftGeneration: number
  previewCurrent: boolean
  profile: ExportProfileSettings
  masterImageUrl: string
  plan: MuralPlanResource | null
  planDirty: boolean
  transport: MuralBuildTransport
  disabled?: boolean
  disabledReason?: string
  pollIntervalMs?: number
}

function binding(
  plan: MuralPlanResource | null,
  processedArtifactId: string,
  previewJobId: string | null,
  expectedDraftGeneration: number,
  previewCurrent: boolean,
  assemblyAidsBinding: string,
): string {
  if (!plan) {
    return `none:${processedArtifactId}:${previewJobId}:${expectedDraftGeneration}:${previewCurrent}:${assemblyAidsBinding}`
  }
  return [
    processedArtifactId,
    previewJobId,
    expectedDraftGeneration,
    previewCurrent,
    plan.generation,
    plan.plan.request_fingerprint,
    plan.request.source.processed_artifact_id,
    assemblyAidsBinding,
  ].join(':')
}

function isWorking(phase: string): boolean {
  return [
    'submitting-geometry',
    'geometry-queued',
    'geometry-running',
    'submitting-mural',
    'mural-queued',
    'mural-running',
    'canceling',
  ].includes(phase)
}

function phaseTitle(phase: string): string {
  if (phase === 'submitting-geometry') return 'Starting geometry preflight'
  if (phase === 'geometry-queued') return 'Geometry preflight queued'
  if (phase === 'geometry-running') return 'Preparing exact geometry'
  if (phase === 'submitting-mural') return 'Starting mural packaging'
  if (phase === 'mural-queued') return 'Mural packaging queued'
  if (phase === 'mural-running') return 'Building multi-plate 3MF'
  if (phase === 'canceling') return 'Canceling mural build'
  if (phase === 'canceled') return 'Build canceled'
  if (phase === 'stale') return 'Previous build is stale'
  if (phase === 'superseded') return 'Previous build superseded'
  if (phase === 'validation-blocked') return 'Bambu validation required'
  return 'Build mural package'
}

export function MuralBuildWorkflow({
  projectId,
  processedArtifactId,
  previewJobId,
  expectedDraftGeneration,
  previewCurrent,
  profile,
  masterImageUrl,
  plan,
  planDirty,
  transport,
  disabled = false,
  disabledReason,
  pollIntervalMs,
}: MuralBuildWorkflowProps) {
  const [assemblyAids, setAssemblyAids] = useState({
    enabled: false,
    rearIdentifiers: true,
    edgeIdentifiers: true,
    orientationMarks: true,
    cropMarks: true,
    alignmentJigMetadata: true,
  })
  const {
    state,
    start,
    cancel,
    retry,
    markStale,
    reset,
  } = useMuralBuildCoordinator({ transport, pollIntervalMs })
  const previousContext = useRef<{ projectId: string; binding: string } | null>(null)
  const currentBinding = binding(
    plan,
    processedArtifactId,
    previewJobId,
    expectedDraftGeneration,
    previewCurrent,
    JSON.stringify(assemblyAids),
  )
  const readiness = muralBuildReadiness({
    plan,
    planDirty,
    processedArtifactId,
    previewJobId,
    previewCurrent,
    disabled,
    disabledReason,
  })

  useEffect(() => {
    const previous = previousContext.current
    reset(projectId)
    if (previous?.projectId === projectId && previous.binding !== currentBinding) {
      markStale('The saved mural plan or processed master changed.')
    }
    previousContext.current = { projectId, binding: currentBinding }
  }, [currentBinding, markStale, projectId, reset])

  useEffect(() => {
    if (planDirty) markStale('The mural plan has unsaved changes.')
  }, [markStale, planDirty])

  const resultMatchesCurrentPlan = Boolean(
    state.result
      && plan
      && state.resultCurrent
      && state.result.job.project_id === projectId
      && state.result.seam_qa_report?.request_fingerprint === plan.plan.request_fingerprint,
  )
  const result = resultMatchesCurrentPlan ? state.result : null
  const working = isWorking(state.phase)
  const selectedAssemblyAidCount = [
    assemblyAids.rearIdentifiers,
    assemblyAids.edgeIdentifiers,
    assemblyAids.orientationMarks,
    assemblyAids.cropMarks,
    assemblyAids.alignmentJigMetadata,
  ].filter(Boolean).length
  const assemblyAidsValid = !assemblyAids.enabled || selectedAssemblyAidCount > 0
  const progress = state.activeStep === 'geometry'
    ? state.geometryJob?.progress ?? 0
    : state.job?.progress ?? 0
  const plateCount = plan?.plan.tiles.length ?? 0
  const request = useMemo<MuralBuildRequest | null>(() => plan && previewJobId ? ({
    projectId,
    previewJobId,
    expectedDraftGeneration,
    processedArtifactId,
    planGeneration: plan.generation,
    requestFingerprint: plan.plan.request_fingerprint,
    name: `${plan.request.layout.columns}x${plan.request.layout.rows}-mural`,
    materialMapping: [],
    profile,
    assemblyAids,
  }) : null, [assemblyAids, expectedDraftGeneration, plan, previewJobId, processedArtifactId, profile, projectId])

  return (
    <section className="mural-build" aria-labelledby="mural-build-heading" aria-busy={working}>
      <header className="mural-build__heading">
        <div>
          <span className="eyebrow">Printable package</span>
          <h2 id="mural-build-heading">Multi-plate mural 3MF</h2>
          <p>Build one verified Bambu project with one numbered tile on each build plate.</p>
        </div>
        <span className="mural-build__phase" data-phase={state.phase}>
          {phaseTitle(state.phase)}
        </span>
      </header>

      <ol className="mural-build__steps" aria-label="Mural build workflow">
        <li data-complete={Boolean(plan && !planDirty && plan.freshness === 'current')}>
          <b>1</b><span>Saved current plan</span>
        </li>
        <li data-complete={Boolean(result)} data-active={working}>
          <b>2</b><span>Build {plateCount || 'multi'} plates</span>
        </li>
        <li data-complete={Boolean(result)}>
          <b>3</b><span>Verify seams</span>
        </li>
        <li data-complete={Boolean(result?.download_ready)}>
          <b>4</b><span>Download 3MF</span>
        </li>
      </ol>

      <p className="mural-build__announcement" role="status" aria-live="polite">
        {state.announcement}
      </p>

      <fieldset className="mural-build__assembly-options" disabled={working}>
        <legend>Assembly guide</legend>
        <div className="mural-build__assembly-toggle">
          <input
            id="mural-assembly-enabled"
            type="checkbox"
            aria-describedby="mural-assembly-description"
            checked={assemblyAids.enabled}
            onChange={(event) => setAssemblyAids((current) => ({
              ...current,
              enabled: event.target.checked,
            }))}
          />
          <span>
            <label htmlFor="mural-assembly-enabled">Include external assembly aids</label>
            <small id="mural-assembly-description">Adds a printable SVG and rear-label/jig metadata. The 3MF and visible art stay unchanged.</small>
          </span>
        </div>
        {assemblyAids.enabled ? (
          <div className="mural-build__assembly-grid" aria-label="Assembly aid contents">
            {([
              ['rearIdentifiers', 'Rear identifiers'],
              ['edgeIdentifiers', 'Edge pair labels'],
              ['orientationMarks', 'TOP orientation marks'],
              ['cropMarks', 'Sheet crop marks'],
              ['alignmentJigMetadata', 'Spacer and jig dimensions'],
            ] as const).map(([key, label]) => (
              <label key={key} htmlFor={`mural-assembly-${key}`}>
                <input
                  id={`mural-assembly-${key}`}
                  type="checkbox"
                  checked={assemblyAids[key]}
                  onChange={(event) => setAssemblyAids((current) => ({
                    ...current,
                    [key]: event.target.checked,
                  }))}
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
        ) : null}
      </fieldset>

      {working ? (
        <div className="mural-build__progress">
          <div>
            <strong>{phaseTitle(state.phase)}</strong>
            <span>{Math.round(progress * 100)}%</span>
          </div>
          <progress max="1" value={progress} aria-label="Mural package build progress" />
          <button
            type="button"
            onClick={() => void cancel()}
            disabled={state.phase === 'canceling'}
          >
            {state.phase === 'canceling' ? 'Canceling…' : 'Cancel build'}
          </button>
        </div>
      ) : null}

      {state.failure ? (
        <div className="mural-build__failure" role="alert">
          <strong>{state.failure.title}</strong>
          <p>{state.failure.message}</p>
          {state.failure.retryable && readiness.ready ? (
            <button type="button" onClick={() => void retry()}>Retry build</button>
          ) : null}
        </div>
      ) : null}

      {!working && !result ? (
        <div className="mural-build__action">
          <button
            type="button"
            disabled={!readiness.ready || !request || !assemblyAidsValid}
            aria-describedby={
              !readiness.ready || !assemblyAidsValid ? 'mural-build-disabled-reason' : undefined
            }
            onClick={() => request && void start(request)}
          >
            Build {plateCount || 'multi'}-plate 3MF
          </button>
          {!assemblyAidsValid ? (
            <p id="mural-build-disabled-reason">Choose at least one assembly aid, or turn the guide off.</p>
          ) : !readiness.ready && readiness.reason ? (
            <p id="mural-build-disabled-reason">{readiness.reason}</p>
          ) : (
            <p>Uses the saved plan generation and exact processed-master fingerprint.</p>
          )}
        </div>
      ) : null}

      {result && result.seam_qa_report ? (
        <div className="mural-build__result">
          <div className="mural-build__verified" role="status" data-download-ready={result.download_ready}>
            <div>
              <span>{result.download_ready ? 'Verified package' : 'Exact package awaiting validation'}</span>
              <strong>{result.seam_qa_report.tiles.length} unique build plates</strong>
              <small>
                {result.package?.kind ?? 'bambu-mural-3mf'}
                {result.package ? ` · ${(result.package.byte_size / 1_000_000).toFixed(1)} MB` : ''}
              </small>
            </div>
            {result.download_ready && result.package ? (
              <div className="mural-build__downloads">
                <a href={result.package.download_url} download>
                  Download verified multi-plate 3MF
                </a>
                {result.assembly_sheet ? (
                  <a href={result.assembly_sheet.download_url} download>
                    Download assembly sheet
                  </a>
                ) : null}
                {result.assembly_aids ? (
                  <a href={result.assembly_aids.download_url} download>
                    Download assembly metadata
                  </a>
                ) : null}
              </div>
            ) : (
              <span className="mural-build__validation-note">
                Install or repair Bambu Studio, then retry validation.
              </span>
            )}
          </div>
          <MuralSeamQA masterImageUrl={masterImageUrl} report={result.seam_qa_report} />
        </div>
      ) : null}
    </section>
  )
}
