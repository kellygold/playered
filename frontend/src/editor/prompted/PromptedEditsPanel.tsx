import { useEffect, useMemo, useState } from 'react'
import {
  acceptPromptedEditAlternative,
  cancelPromptedEdit,
  executePromptedEdit,
  fetchPromptedEditProviders,
  fetchPromptedEditSession,
  preparePromptedEdit,
  rejectPromptedEdit,
  retryPromptedEdit,
} from '../../api'
import type {
  PromptedEditAlternative,
  PromptedEditPreparation,
  PromptedEditProvider,
  PromptedEditSession,
} from '../../contracts'
import { EditorCanvas } from '../canvas/EditorCanvas'
import type { EditorCanvasSourceCatalog } from '../canvas/canvasModel'
import type { CanvasSelectionState } from '../selection'
import './PromptedEditsPanel.css'

type PromptedEditsPanelProps = {
  projectId: string
  parentRevisionId: string | null
  previewJobId: string | null
  draftGeneration: number
  sourceUrl: string
  sourceSha256: string
  sourceWidthPx: number
  sourceHeightPx: number
  canvasWidthMm: number
  canvasHeightMm: number
  selection: CanvasSelectionState | null
  disabled?: boolean
  onAccepted: () => void | Promise<void>
}

const TERMINAL = new Set(['complete', 'partial', 'failed', 'canceled', 'rejected', 'accepted'])

export function PromptedEditsPanel({
  projectId,
  parentRevisionId,
  previewJobId,
  draftGeneration,
  sourceUrl,
  sourceSha256,
  sourceWidthPx,
  sourceHeightPx,
  canvasWidthMm,
  canvasHeightMm,
  selection,
  disabled = false,
  onAccepted,
}: PromptedEditsPanelProps) {
  const [providers, setProviders] = useState<PromptedEditProvider[] | null>(null)
  const [providerId, setProviderId] = useState('')
  const [prompt, setPrompt] = useState('')
  const [alternativeCount, setAlternativeCount] = useState(2)
  const [preparation, setPreparation] = useState<PromptedEditPreparation | null>(null)
  const [session, setSession] = useState<PromptedEditSession | null>(null)
  const [selectedAlternativeId, setSelectedAlternativeId] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetchPromptedEditProviders(controller.signal)
      .then((items) => {
        setProviders(items)
        setProviderId((current) => current || items[0]?.provider_id || '')
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === 'AbortError')) {
          // Provider editing is optional. Older/offline APIs degrade to local tools quietly.
          setProviders([])
        }
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!session || TERMINAL.has(session.status)) return
    const controller = new AbortController()
    const timer = window.setInterval(() => {
      fetchPromptedEditSession(session.id, controller.signal)
        .then((next) => {
          setSession(next)
          if (next.alternatives.length) {
            setSelectedAlternativeId((current) => current ?? next.alternatives[0].id)
          }
        })
        .catch((reason: unknown) => {
          if (!(reason instanceof DOMException && reason.name === 'AbortError')) {
            setError(reason instanceof Error ? reason.message : 'Edit status could not be loaded.')
          }
        })
    }, 350)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [session])

  const provider = providers?.find((item) => item.provider_id === providerId) ?? null
  const selectedAlternative = session?.alternatives.find(
    (item) => item.id === selectedAlternativeId,
  ) ?? session?.alternatives[0] ?? null
  const catalog = useMemo(
    () => alternativeCatalog({
      sourceUrl,
      sourceSha256,
      alternative: selectedAlternative,
      widthPx: sourceWidthPx,
      heightPx: sourceHeightPx,
      widthMm: canvasWidthMm,
      heightMm: canvasHeightMm,
    }),
    [
      canvasHeightMm,
      canvasWidthMm,
      selectedAlternative,
      sourceHeightPx,
      sourceSha256,
      sourceUrl,
      sourceWidthPx,
    ],
  )
  const canPrepare = Boolean(
    parentRevisionId && previewJobId && selection?.primitives.length && prompt.trim() && provider,
  )

  async function prepare() {
    if (!parentRevisionId || !previewJobId || !selection || !provider) return
    setWorking(true)
    setError(null)
    try {
      const result = await preparePromptedEdit(projectId, {
        parent_revision_id: parentRevisionId,
        preview_job_id: previewJobId,
        provider_id: provider.provider_id,
        selection,
        prompt: prompt.trim(),
        alternative_count: alternativeCount,
      })
      setPreparation(result)
      setSession(null)
      setSelectedAlternativeId(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The edit could not be prepared.')
    } finally {
      setWorking(false)
    }
  }

  async function approve() {
    if (!preparation) return
    setWorking(true)
    setError(null)
    try {
      const result = await executePromptedEdit(preparation.session_id, preparation)
      setSession(result.session)
      setPreparation(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The edit could not be started.')
    } finally {
      setWorking(false)
    }
  }

  async function cancel() {
    if (!session) return
    setWorking(true)
    try {
      setSession(await cancelPromptedEdit(session.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The edit could not be canceled.')
    } finally {
      setWorking(false)
    }
  }

  async function retry() {
    if (!session) return
    setWorking(true)
    setError(null)
    try {
      setPreparation(await retryPromptedEdit(session.id))
      setSession(null)
      setSelectedAlternativeId(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The edit could not be retried.')
    } finally {
      setWorking(false)
    }
  }

  async function reject() {
    if (!session) return
    setWorking(true)
    try {
      setSession(await rejectPromptedEdit(session.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The alternatives could not be rejected.')
    } finally {
      setWorking(false)
    }
  }

  async function accept() {
    if (!session || !selectedAlternative) return
    setWorking(true)
    setError(null)
    try {
      const result = await acceptPromptedEditAlternative(
        projectId,
        session.id,
        selectedAlternative.id,
        draftGeneration,
        `Prompted edit · ${session.prompt.slice(0, 80)}`,
      )
      setSession(result.session)
      await onAccepted()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The alternative could not be accepted.')
    } finally {
      setWorking(false)
    }
  }

  return (
    <section className="control-section prompted-edits" aria-labelledby="prompted-edits-heading">
      <div className="control-heading">
        <div>
          <span className="eyebrow">Optional provider edit</span>
          <h3 id="prompted-edits-heading">Prompt selected area</h3>
        </div>
        {session ? <span className="prompted-edits__status">{session.status}</span> : null}
      </div>

      {providers === null ? <p role="status">Checking configured providers…</p> : null}
      {providers?.length === 0 ? (
        <p>No prompted-edit provider is configured. Local cleanup and mask tools remain available.</p>
      ) : null}

      {providers?.length ? (
        <>
          <label>
            Provider and pinned model
            <select
              value={providerId}
              onChange={(event) => setProviderId(event.target.value)}
              disabled={disabled || working || Boolean(preparation || session)}
            >
              {providers.map((item) => (
                <option key={item.provider_id} value={item.provider_id}>
                  {item.display_name} · {item.model_id} {item.model_version}
                </option>
              ))}
            </select>
          </label>
          {!preparation && !session ? (
            <>
              <label>
                What should change inside the saved selection?
                <textarea
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  rows={4}
                  placeholder="Make the eyes larger while preserving the linework."
                  disabled={disabled || working}
                />
              </label>
              <label>
                Alternatives
                <input
                  type="number"
                  min={1}
                  max={provider?.capabilities.maximum_alternatives ?? 1}
                  value={alternativeCount}
                  onChange={(event) => setAlternativeCount(Number(event.target.value))}
                  disabled={disabled || working}
                />
              </label>
              <p className="prompted-edits__hint">
                {!parentRevisionId
                  ? 'Publish a revision before prompting an edit.'
                  : !previewJobId
                    ? 'Render a current preview first.'
                    : !selection?.primitives.length
                      ? 'Draw and save a rectangle, lasso, or brush selection first.'
                      : 'Preparation stays local and sends nothing.'}
              </p>
              <button type="button" onClick={() => void prepare()} disabled={disabled || working || !canPrepare}>
                Review data disclosure
              </button>
            </>
          ) : null}
        </>
      ) : null}

      {preparation ? (
        <div className="prompted-edits__consent" role="group" aria-label="Provider data disclosure">
          <strong>Nothing has been sent yet</strong>
          <p>
            Approving sends {preparation.disclosure.data_categories.join(', ')} to{' '}
            {preparation.provider.display_name} using {preparation.provider.model_id}{' '}
            {preparation.provider.model_version}.
          </p>
          <dl>
            <div><dt>Retention</dt><dd>{preparation.provider.privacy.retention}</dd></div>
            <div><dt>Training</dt><dd>{preparation.provider.privacy.training_use}</dd></div>
            <div><dt>Residency</dt><dd>{preparation.provider.privacy.data_residency}</dd></div>
          </dl>
          <div className="prompted-edits__actions">
            <button type="button" onClick={() => setPreparation(null)} disabled={working}>Go back</button>
            <button type="button" onClick={() => void approve()} disabled={working}>Approve and generate</button>
          </div>
        </div>
      ) : null}

      {session && ['queued', 'running'].includes(session.status) ? (
        <div role="status" className="prompted-edits__progress">
          <span className="upload-spinner" aria-hidden="true" />
          <p>Generating {session.requested_alternative_count} review candidate(s)…</p>
          <button type="button" onClick={() => void cancel()} disabled={working}>Cancel</button>
        </div>
      ) : null}

      {session?.alternatives.length ? (
        <div className="prompted-edits__review">
          <div className="prompted-edits__alternatives" aria-label="Generated alternatives">
            {session.alternatives.map((item) => (
              <button
                type="button"
                key={item.id}
                aria-pressed={selectedAlternative?.id === item.id}
                onClick={() => setSelectedAlternativeId(item.id)}
              >
                Option {item.index + 1}
              </button>
            ))}
          </div>
          {catalog && selectedAlternative ? (
            <EditorCanvas
              catalog={catalog}
              ariaLabel="Prompted alternative compared with immutable parent"
              defaultView="processed"
              defaultComparison={{ mode: 'split', secondaryView: 'original', splitPercent: 50 }}
              selectionDisabled
              viewportSize={{ width: 560, height: 390 }}
            />
          ) : null}
          {selectedAlternative ? (
            <dl className="prompted-edits__evidence">
              <div><dt>Changed area</dt><dd>{selectedAlternative.changed_pixel_count.toLocaleString()} pixels</dd></div>
              <div><dt>Model</dt><dd>{selectedAlternative.provenance.model_id} {selectedAlternative.provenance.model_version}</dd></div>
              <div><dt>Replay</dt><dd>{selectedAlternative.provenance.reproducibility.replace('_', ' ')}</dd></div>
            </dl>
          ) : null}
          {['complete', 'partial'].includes(session.status) ? (
            <div className="prompted-edits__actions">
              <button type="button" onClick={() => void retry()} disabled={working}>Retry as new session</button>
              <button type="button" onClick={() => void reject()} disabled={working}>Reject all</button>
              <button type="button" onClick={() => void accept()} disabled={working || !selectedAlternative}>Accept as new revision</button>
            </div>
          ) : null}
        </div>
      ) : null}

      {session && ['failed', 'canceled', 'rejected'].includes(session.status) ? (
        <button type="button" onClick={() => void retry()} disabled={working}>Retry as new session</button>
      ) : null}
      {error ? <p className="prompted-edits__error" role="alert">{error}</p> : null}
    </section>
  )
}

function alternativeCatalog({
  sourceUrl,
  sourceSha256,
  alternative,
  widthPx,
  heightPx,
  widthMm,
  heightMm,
}: {
  sourceUrl: string
  sourceSha256: string
  alternative: PromptedEditAlternative | null
  widthPx: number
  heightPx: number
  widthMm: number
  heightMm: number
}): EditorCanvasSourceCatalog | null {
  if (!alternative) return null
  const space = { widthPx, heightPx, widthMm, heightMm }
  return {
    sources: {
      original: {
        view: 'original',
        label: 'Immutable parent',
        description: 'The exact source of the parent revision.',
        raster: {
          url: sourceUrl,
          sha256: sourceSha256,
          artifactKind: 'prompted-parent',
          mediaType: 'image/png',
          interpolation: 'smooth',
        },
        space,
      },
      processed: {
        view: 'processed',
        label: 'Generated alternative',
        description: 'Provider output retained as an unaccepted review candidate.',
        raster: {
          url: alternative.output_url,
          sha256: alternative.output_sha256,
          artifactKind: 'prompted-alternative',
          mediaType: alternative.output_media_type,
          interpolation: 'smooth',
        },
        space,
      },
      mask: {
        view: 'mask',
        label: 'Exact changed area',
        description: 'Pixels whose normalized RGBA value differs from the immutable parent.',
        raster: {
          url: alternative.changed_mask_preview_url,
          sha256: alternative.changed_mask_sha256,
          artifactKind: 'prompted-changed-mask',
          mediaType: 'image/png',
          interpolation: 'nearest',
        },
        space,
      },
    },
    unavailable: {},
  }
}
