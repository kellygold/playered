import { useEffect, useId, useRef, useState } from 'react'
import type { RegionGraph, RegionOperation } from '../../contracts'
import type { CanvasSelectionState } from '../selection'
import {
  buildLocalEditOperation,
  DEFAULT_LOCAL_EDIT_DRAFT,
  resolveLocalEdit,
  type LocalEditAction,
  type LocalEditDraft,
} from './localEditModel'
import './LocalEditsPanel.css'

export type LocalEditsPanelProps = {
  graph: RegionGraph | null
  graphFingerprint: string | null
  configFingerprint: string | null
  parentRevisionId?: string | null
  selection: CanvasSelectionState | null
  disabled?: boolean
  disabledReason?: string
  persistence?: 'saved' | 'saving' | 'error'
  onCommit: (operation: RegionOperation) => void | Promise<void>
}

const ACTIONS: { value: LocalEditAction; label: string }[] = [
  { value: 'fill', label: 'Fill selection' },
  { value: 'cleanup', label: 'Clean one color' },
  { value: 'thicken', label: 'Thicken feature' },
  { value: 'erode', label: 'Erode feature' },
  { value: 'open', label: 'Open / remove specks' },
  { value: 'close', label: 'Close small gaps' },
  { value: 'clone', label: 'Clone selection' },
  { value: 'affine', label: 'Resize / warp' },
]

function numericValue(value: string, fallback: number) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

export function LocalEditsPanel({
  graph,
  graphFingerprint,
  configFingerprint,
  parentRevisionId = null,
  selection,
  disabled = false,
  disabledReason,
  persistence = 'saved',
  onCommit,
}: LocalEditsPanelProps) {
  const headingId = useId()
  const reviewButtonRef = useRef<HTMLButtonElement | null>(null)
  const cancelButtonRef = useRef<HTMLButtonElement | null>(null)
  const wasReviewingRef = useRef(false)
  const [draft, setDraft] = useState<LocalEditDraft>(DEFAULT_LOCAL_EDIT_DRAFT)
  const [reviewing, setReviewing] = useState(false)
  const [committing, setCommitting] = useState(false)
  const [commitError, setCommitError] = useState<string | null>(null)
  const palette = graph?.palette ?? []
  const context = graph && graphFingerprint && configFingerprint && selection
    ? {
        graphFingerprint,
        configFingerprint,
        parentRevisionId,
        selection,
        paletteLabels: palette.map((item) => item.label),
      }
    : null
  const resolution = context ? resolveLocalEdit(context, draft) : null
  const unavailable = disabled || !context || !resolution?.valid

  useEffect(() => {
    if (reviewing) cancelButtonRef.current?.focus()
    else if (wasReviewingRef.current) reviewButtonRef.current?.focus()
    wasReviewingRef.current = reviewing
    if (!reviewing) return
    const cancelOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || committing) return
      event.preventDefault()
      setReviewing(false)
      setCommitError(null)
    }
    document.addEventListener('keydown', cancelOnEscape)
    return () => document.removeEventListener('keydown', cancelOnEscape)
  }, [committing, reviewing])

  const patchDraft = (patch: Partial<LocalEditDraft>) => {
    setDraft((current) => ({ ...current, ...patch }))
    setReviewing(false)
    setCommitError(null)
  }
  const chooseLabel = (value: string) => value === '' ? null : Number(value)
  const paletteName = (label: number, color: string) => `Label ${label} · ${color}`
  const toggleEditable = (label: number, checked: boolean) => patchDraft({
    editableLabels: checked
      ? [...new Set([...draft.editableLabels, label])].sort((a, b) => a - b)
      : draft.editableLabels.filter((candidate) => candidate !== label),
  })
  const confirm = async () => {
    if (!context || !resolution?.valid) return
    setCommitting(true)
    setCommitError(null)
    try {
      await onCommit(await buildLocalEditOperation(context, draft))
      setReviewing(false)
      reviewButtonRef.current?.focus()
    } catch (error) {
      setCommitError(error instanceof Error ? error.message : 'The local edit could not be saved.')
    } finally {
      setCommitting(false)
    }
  }

  return (
    <section className="local-edits" aria-labelledby={headingId}>
      <div className="local-edits-heading">
        <div><span>Saved selection</span><h3 id={headingId}>Local deterministic edit</h3></div>
        <output data-state={persistence}>{persistence === 'saving' ? 'Saving…' : persistence === 'error' ? 'Save failed' : 'Saved'}</output>
      </div>
      <p className="local-edits-intro">
        The green canvas overlay is the exact source. The accepted command embeds this selection,
        then the next preview replays it without a model call.
      </p>
      {disabled && disabledReason ? <p className="local-edits-lock" role="status">{disabledReason}</p> : null}
      {!selection?.primitives.length ? <p className="local-edits-lock" role="status">Draw a rectangle, lasso, or brush—or add an exact region—before editing.</p> : null}

      <label className="local-edits-field">
        <span>Operation</span>
        <select
          value={draft.action}
          disabled={disabled || !context}
          onChange={(event) => patchDraft({ action: event.target.value as LocalEditAction })}
        >
          {ACTIONS.map((action) => <option key={action.value} value={action.value}>{action.label}</option>)}
        </select>
      </label>

      {['cleanup', 'thicken', 'erode', 'open', 'close'].includes(draft.action) ? (
        <label className="local-edits-field">
          <span>Feature / source color</span>
          <select value={draft.sourceLabel ?? ''} disabled={disabled} onChange={(event) => patchDraft({ sourceLabel: chooseLabel(event.target.value) })}>
            <option value="">Choose a color…</option>
            {palette.map((item) => <option key={item.label} value={item.label}>{paletteName(item.label, item.color)}</option>)}
          </select>
        </label>
      ) : null}

      {['fill', 'cleanup', 'erode', 'open'].includes(draft.action) ? (
        <label className="local-edits-field">
          <span>{draft.action === 'fill' ? 'Fill color' : 'Target / replacement color'}</span>
          <select value={draft.targetLabel ?? ''} disabled={disabled} onChange={(event) => patchDraft({ targetLabel: chooseLabel(event.target.value) })}>
            <option value="">Choose a color…</option>
            {palette.map((item) => <option key={item.label} value={item.label}>{paletteName(item.label, item.color)}</option>)}
          </select>
        </label>
      ) : null}

      {draft.action === 'fill' ? (
        <label className="local-edits-check"><input type="checkbox" checked={draft.activateTransparent} disabled={disabled} onChange={(event) => patchDraft({ activateTransparent: event.target.checked })} />Activate transparent pixels inside the selection</label>
      ) : null}

      {['thicken', 'erode', 'open', 'close'].includes(draft.action) ? (
        <label className="local-edits-field">
          <span>Physical radius</span>
          <span className="local-edits-number"><input aria-label="Local edit radius millimetres" type="number" min="0.01" max="25" step="0.01" value={draft.radiusMm} disabled={disabled} onChange={(event) => patchDraft({ radiusMm: numericValue(event.target.value, draft.radiusMm) })} /><em>mm</em></span>
        </label>
      ) : null}

      {draft.action === 'clone' ? (
        <div className="local-edits-grid">
          <label><span>Horizontal offset</span><span className="local-edits-number"><input aria-label="Clone horizontal offset millimetres" type="number" step="0.1" value={draft.offsetXmm} disabled={disabled} onChange={(event) => patchDraft({ offsetXmm: numericValue(event.target.value, draft.offsetXmm) })} /><em>mm</em></span></label>
          <label><span>Vertical offset</span><span className="local-edits-number"><input aria-label="Clone vertical offset millimetres" type="number" step="0.1" value={draft.offsetYmm} disabled={disabled} onChange={(event) => patchDraft({ offsetYmm: numericValue(event.target.value, draft.offsetYmm) })} /><em>mm</em></span></label>
        </div>
      ) : null}

      {draft.action === 'affine' ? (
        <div className="local-edits-grid">
          <label><span>Scale X</span><input aria-label="Resize scale X" type="number" min="0.05" max="20" step="0.05" value={draft.scaleX} disabled={disabled} onChange={(event) => patchDraft({ scaleX: numericValue(event.target.value, draft.scaleX) })} /></label>
          <label><span>Scale Y</span><input aria-label="Resize scale Y" type="number" min="0.05" max="20" step="0.05" value={draft.scaleY} disabled={disabled} onChange={(event) => patchDraft({ scaleY: numericValue(event.target.value, draft.scaleY) })} /></label>
          <label><span>Shear X</span><input aria-label="Warp shear X" type="number" min="-5" max="5" step="0.05" value={draft.shearX} disabled={disabled} onChange={(event) => patchDraft({ shearX: numericValue(event.target.value, draft.shearX) })} /></label>
          <label><span>Shear Y</span><input aria-label="Warp shear Y" type="number" min="-5" max="5" step="0.05" value={draft.shearY} disabled={disabled} onChange={(event) => patchDraft({ shearY: numericValue(event.target.value, draft.shearY) })} /></label>
          <label><span>Move X</span><span className="local-edits-number"><input aria-label="Warp translation X millimetres" type="number" step="0.1" value={draft.translateXmm} disabled={disabled} onChange={(event) => patchDraft({ translateXmm: numericValue(event.target.value, draft.translateXmm) })} /><em>mm</em></span></label>
          <label><span>Move Y</span><span className="local-edits-number"><input aria-label="Warp translation Y millimetres" type="number" step="0.1" value={draft.translateYmm} disabled={disabled} onChange={(event) => patchDraft({ translateYmm: numericValue(event.target.value, draft.translateYmm) })} /><em>mm</em></span></label>
        </div>
      ) : null}

      {['thicken', 'close', 'clone', 'affine'].includes(draft.action) ? (
        <fieldset className="local-edits-palette">
          <legend>Colors this edit may overwrite</legend>
          {palette.filter((item) => item.label !== draft.sourceLabel).map((item) => (
            <label key={item.label}>
              <input type="checkbox" disabled={disabled} checked={draft.editableLabels.includes(item.label)} onChange={(event) => toggleEditable(item.label, event.target.checked)} />
              <i style={{ backgroundColor: item.color }} aria-hidden="true" />
              <span>{paletteName(item.label, item.color)}</span>
            </label>
          ))}
        </fieldset>
      ) : null}

      {draft.action === 'clone' || draft.action === 'affine' ? (
        <label className="local-edits-check"><input type="checkbox" checked={draft.clipToCanvas} disabled={disabled} onChange={(event) => patchDraft({ clipToCanvas: event.target.checked })} />Allow explicit clipping at the canvas edge</label>
      ) : null}
      {draft.action === 'affine' ? (
        <label className="local-edits-check"><input type="checkbox" checked={draft.clearSource} disabled={disabled} onChange={(event) => patchDraft({ clearSource: event.target.checked })} />Clear the original selected pixels</label>
      ) : null}

      <div className="local-edits-resolution" data-valid={resolution?.valid ?? false}>
        <strong>Exact replay plan</strong>
        <p>{resolution?.summary ?? 'Render a current preview to bind this edit.'}</p>
        {resolution?.error ? <p role="status">{resolution.error}</p> : null}
      </div>

      {!reviewing ? (
        <button ref={reviewButtonRef} className="local-edits-review" type="button" disabled={unavailable} onClick={() => setReviewing(true)}>Review local edit</button>
      ) : (
        <div className="local-edits-confirm" role="region" aria-label="Confirm local deterministic edit">
          <strong>Confirm destructive local edit</strong>
          <p>{resolution?.summary} The current preview will become stale until this exact command is replayed.</p>
          {commitError ? <p role="alert">{commitError}</p> : null}
          <div>
            <button ref={cancelButtonRef} type="button" disabled={committing} onClick={() => setReviewing(false)}>Cancel</button>
            <button type="button" disabled={committing} onClick={() => void confirm()}>{committing ? 'Saving…' : 'Save edit'}</button>
          </div>
        </div>
      )}
    </section>
  )
}
