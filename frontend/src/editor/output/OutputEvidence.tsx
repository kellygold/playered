import { useEffect, useId, useState } from 'react'
import { fetchCapabilities } from '../../api'
import type { ArtifactResource, ExportJobResult } from '../../contracts'
import { revealArtifact } from '../revisions/artifactClient'
import {
  formatArtifactBytes,
  validationWarnings,
  type ValidationWarningEvidence,
} from './outputEvidenceModel'
import './OutputEvidence.css'

type ValidationEvidenceLoad =
  | { artifactId: string; state: 'ready'; warnings: readonly ValidationWarningEvidence[]; error: null }
  | { artifactId: string; state: 'error'; warnings: readonly ValidationWarningEvidence[]; error: string }

type OutputEvidenceProps = {
  projectId: string
  result: ExportJobResult | null
  current: boolean
}

type EvidenceLink = {
  artifact: ArtifactResource
  label: string
  download: boolean
}

function artifactLinks(result: ExportJobResult): EvidenceLink[] {
  const candidates: Array<EvidenceLink | null> = [
    result.package
      ? { artifact: result.package, label: 'Download 3MF package', download: true }
      : null,
    result.quality_report
      ? { artifact: result.quality_report, label: 'Open geometry quality report (JSON)', download: false }
      : null,
    result.validation_report
      ? { artifact: result.validation_report, label: 'Open slicer validation report (JSON)', download: false }
      : null,
    result.validation_log
      ? { artifact: result.validation_log, label: 'Open retained validation log (text)', download: false }
      : null,
  ]
  return candidates.filter((item): item is EvidenceLink => item !== null)
}

function metadataString(artifact: ArtifactResource | null, key: string): string | null {
  const value = artifact?.metadata[key]
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

export function OutputEvidence({ projectId, result, current }: OutputEvidenceProps) {
  const generatedId = useId().replaceAll(':', '')
  const headingId = `output-evidence-${generatedId}`
  const report = current ? result?.validation_report ?? null : null
  const [validation, setValidation] = useState<ValidationEvidenceLoad | null>(null)
  const [finderAvailable, setFinderAvailable] = useState<boolean | null>(null)
  const [revealState, setRevealState] = useState<'idle' | 'working'>('idle')
  const [revealMessage, setRevealMessage] = useState<string | null>(null)

  useEffect(() => {
    if (!report) return
    const controller = new AbortController()
    fetch(report.download_url, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Validation evidence returned HTTP ${response.status}.`)
        return response.json() as Promise<unknown>
      })
      .then((payload) => {
        if (!controller.signal.aborted) {
          setValidation({
            artifactId: report.id,
            state: 'ready',
            warnings: validationWarnings(payload),
            error: null,
          })
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && (error as Error).name !== 'AbortError') {
          setValidation({
            artifactId: report.id,
            state: 'error',
            warnings: [],
            error: error instanceof Error ? error.message : 'Validation evidence could not be read.',
          })
        }
      })
    return () => controller.abort()
  }, [report])

  useEffect(() => {
    if (!current || !result?.package) return
    const controller = new AbortController()
    fetchCapabilities(controller.signal)
      .then((value) => setFinderAvailable(value.features.finder_reveal === true))
      .catch((error: unknown) => {
        if ((error as Error).name !== 'AbortError') setFinderAvailable(false)
      })
    return () => controller.abort()
  }, [current, result?.package])

  if (!current || !result?.package) return null

  const status = metadataString(result.validation_report, 'status') ?? 'validated'
  const manualPrintGate = metadataString(result.validation_report, 'manual_print_gate')
  const links = artifactLinks(result)
  const currentValidation = report && validation?.artifactId === report.id ? validation : null

  return (
    <section className="output-evidence" aria-labelledby={headingId}>
      <div className="output-evidence__heading">
        <div>
          <span>Retained artifacts</span>
          <h3 id={headingId}>Verified output evidence</h3>
        </div>
        <strong data-status={status}>{status.replaceAll('_', ' ')}</strong>
      </div>

      <dl className="output-evidence__package">
        <div>
          <dt>Package size</dt>
          <dd>{formatArtifactBytes(result.package.byte_size)}</dd>
        </div>
        <div>
          <dt>Package SHA-256</dt>
          <dd><code>{result.package.sha256}</code></dd>
        </div>
        <div>
          <dt>Automated validation</dt>
          <dd>Bambu Studio · {status.replaceAll('_', ' ')}</dd>
        </div>
        <div>
          <dt>Physical print gate</dt>
          <dd>{manualPrintGate?.replaceAll('_', ' ') ?? 'Not reported'}</dd>
        </div>
      </dl>

      <div className="output-evidence__artifacts">
        <strong>Artifact access</strong>
        <ul>
          {links.map(({ artifact, label, download }) => (
            <li key={artifact.id}>
              <a
                href={artifact.download_url}
                download={download ? '' : undefined}
                target={download ? undefined : '_blank'}
                rel={download ? undefined : 'noreferrer'}
              >
                {label}
              </a>
              <small>{formatArtifactBytes(artifact.byte_size)}</small>
            </li>
          ))}
        </ul>
        {finderAvailable ? (
          <button
            type="button"
            disabled={revealState === 'working'}
            onClick={() => {
              setRevealState('working')
              setRevealMessage(null)
              void revealArtifact(projectId, result.package!.id)
                .then((response) => setRevealMessage(
                  response.revealed ? 'Revealed verified package in Finder.' : response.reason,
                ))
                .catch((error: unknown) => setRevealMessage(
                  error instanceof Error ? error.message : 'Finder reveal failed.',
                ))
                .finally(() => setRevealState('idle'))
            }}
          >
            {revealState === 'working' ? 'Revealing package…' : 'Reveal verified 3MF in Finder'}
          </button>
        ) : finderAvailable === false ? (
          <p>Finder reveal is unavailable here. Download remains available.</p>
        ) : null}
        {revealMessage ? <p role="status">{revealMessage}</p> : null}
      </div>

      <div className="output-evidence__warnings" aria-live="polite">
        <strong>Slicer warnings</strong>
        {report && !currentValidation ? <p role="status">Reading retained validation evidence…</p> : null}
        {currentValidation?.state === 'error' ? (
          <p role="alert">Warning details could not be loaded. {currentValidation.error}</p>
        ) : null}
        {currentValidation?.state === 'ready' && currentValidation.warnings.length === 0 ? (
          <p data-tone="positive">No slicer warnings were reported.</p>
        ) : null}
        {currentValidation?.state === 'ready' && currentValidation.warnings.length > 0 ? (
          <ul>
            {currentValidation.warnings.map((warning) => (
              <li key={`${warning.category ?? ''}:${warning.message}`}>
                {warning.category ? <span>{warning.category}</span> : null}
                <p>{warning.message}</p>
              </li>
            ))}
          </ul>
        ) : null}
        {!report ? (
          <p>No validation report artifact was retained for warning inspection.</p>
        ) : null}
      </div>
    </section>
  )
}
