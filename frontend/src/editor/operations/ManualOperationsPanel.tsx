import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { HoleAnalysis, PaletteColor, RegionGraph, RegionOperation, RiskActionKind } from '../../contracts'
import { shortRegionId } from '../inspector'
import {
  buildManualOperation,
  DEFAULT_SIMILAR_REGION_PREDICATE,
  holeFeaturesForRegion,
  resolveManualOperation,
  type ManualAction,
  type ManualOperationContext,
  type ManualOperationDraft,
} from './manualOperationModel'
import './ManualOperationsPanel.css'

export type ManualOperationSuggestionIntent = {
  key: number
  kind: RiskActionKind
  featureKey: string
}

export type ManualOperationsPanelProps = {
  graph: RegionGraph | null
  graphFingerprint: string | null
  configFingerprint: string | null
  palette: PaletteColor[]
  holeAnalysis: HoleAnalysis | null
  holeAnalysisFingerprint: string | null
  selectedRegionId: string | null
  suggestionIntent?: ManualOperationSuggestionIntent | null
  disabled?: boolean
  disabledReason?: string
  persistence?: 'saved' | 'saving' | 'error'
  onRegionSelect: (regionId: string) => void
  onAffectedRegionsChange?: (regionIds: string[]) => void
  onCommit: (operation: RegionOperation) => void | Promise<void>
}

const ACTIONS: { id: ManualAction; label: string; description: string }[] = [
  { id: 'protect', label: 'Keep / protect', description: 'Lock exact pixels against later destructive edits.' },
  { id: 'merge', label: 'Merge', description: 'Delete the selected component into an adjacent target color.' },
  { id: 'delete', label: 'Delete', description: 'Remove the selected printable component to transparency.' },
  { id: 'fill', label: 'Fill hole', description: 'Fill a classified enclosed center with its surrounding ring.' },
  { id: 'recolor', label: 'Recolor', description: 'Replace the selected component with another palette label.' },
  { id: 'thicken', label: 'Thicken', description: 'Expand the selected baseline into explicitly replaceable colors.' },
]

function actionFromSuggestion(kind: RiskActionKind): ManualAction | null {
  if (kind === 'keep' || kind === 'preserve_line') return 'protect'
  if (kind.startsWith('merge_')) return 'merge'
  if (kind === 'fill_hole') return 'fill'
  if (kind === 'widen') return 'thicken'
  return null
}

function initialDraft(selectedRegionId: string): ManualOperationDraft {
  return {
    action: 'protect',
    scopeMode: 'selected',
    selectedRegionId,
    selectedHoleFeatureId: null,
    targetLabel: null,
    radiusMm: 0.2,
    editableLabels: [],
    predicate: { ...DEFAULT_SIMILAR_REGION_PREDICATE },
  }
}

export function ManualOperationsPanel(props: ManualOperationsPanelProps) {
  return (
    <ManualOperationsPanelInner
      key={`${props.selectedRegionId ?? 'none'}:${props.suggestionIntent?.key ?? 0}`}
      {...props}
    />
  )
}

function ManualOperationsPanelInner({
  graph,
  graphFingerprint,
  configFingerprint,
  palette,
  holeAnalysis,
  holeAnalysisFingerprint,
  selectedRegionId,
  suggestionIntent,
  disabled = false,
  disabledReason,
  persistence = 'saved',
  onRegionSelect,
  onAffectedRegionsChange,
  onCommit,
}: ManualOperationsPanelProps) {
  const headingId = useId()
  const selectedRegion = graph?.regions.find((region) => region.id === selectedRegionId) ?? null
  const holeFeatures = useMemo(
    () => selectedRegionId ? holeFeaturesForRegion(holeAnalysis, selectedRegionId) : [],
    [holeAnalysis, selectedRegionId],
  )
  const suggestionAction = suggestionIntent ? actionFromSuggestion(suggestionIntent.kind) : null
  const suggestionNote = suggestionIntent && !suggestionAction
    ? `“${suggestionIntent.kind}” needs a specialized editor command and was not mapped to a different operation.`
    : null
  const [draft, setDraft] = useState<ManualOperationDraft | null>(() => {
    if (!selectedRegionId) return null
    const initial = initialDraft(selectedRegionId)
    if (!suggestionAction) return initial
    return {
      ...initial,
      action: suggestionAction,
      selectedHoleFeatureId: suggestionAction === 'fill'
        ? holeFeatures.find((feature) => feature.id === suggestionIntent?.featureKey)?.id ?? holeFeatures[0]?.id ?? null
        : null,
    }
  })
  const [reviewing, setReviewing] = useState(false)
  const [committing, setCommitting] = useState(false)
  const [commitError, setCommitError] = useState<string | null>(null)
  const reviewButtonRef = useRef<HTMLButtonElement | null>(null)
  const cancelButtonRef = useRef<HTMLButtonElement | null>(null)
  const wasReviewingRef = useRef(false)

  const context: ManualOperationContext | null = graph && graphFingerprint && configFingerprint
    ? { graph, graphFingerprint, configFingerprint, palette, holeAnalysis, holeAnalysisFingerprint }
    : null
  const resolution = context && draft ? resolveManualOperation(context, draft) : null
  const affectedRegionsKey = resolution?.affectedRegionIds.join('\u0000') ?? ''
  useEffect(() => {
    onAffectedRegionsChange?.(reviewing && resolution?.valid ? resolution.affectedRegionIds : [])
    return () => onAffectedRegionsChange?.([])
  // `affectedRegionsKey` intentionally makes exact membership, not array identity, the trigger.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [affectedRegionsKey, onAffectedRegionsChange, reviewing, resolution?.valid])

  useEffect(() => {
    if (reviewing) cancelButtonRef.current?.focus()
    else if (wasReviewingRef.current) reviewButtonRef.current?.focus()
    wasReviewingRef.current = reviewing

    if (!reviewing) return
    const cancelOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape' || committing) return
      event.preventDefault()
      setReviewing(false)
      setCommitError(null)
    }
    document.addEventListener('keydown', cancelOnEscape)
    return () => document.removeEventListener('keydown', cancelOnEscape)
  }, [committing, reviewing])

  if (!graph || !graphFingerprint || !configFingerprint) {
    return (
      <section className="manual-operations" aria-labelledby={headingId}>
        <div className="manual-operations-heading"><span>Manual edit</span><h3 id={headingId}>Region operations</h3></div>
        <p className="manual-operations-empty" role="status">Render a current preview before authoring graph-bound operations.</p>
      </section>
    )
  }
  if (!selectedRegion || !draft || !resolution) {
    return (
      <section className="manual-operations" aria-labelledby={headingId}>
        <div className="manual-operations-heading"><span>Manual edit</span><h3 id={headingId}>Region operations</h3></div>
        <p className="manual-operations-empty" role="status">Select an exact region on the canvas or in the inspector to begin.</p>
      </section>
    )
  }

  const patchDraft = (patch: Partial<ManualOperationDraft>) => {
    setDraft((current) => current ? { ...current, ...patch } : current)
    setReviewing(false)
    setCommitError(null)
  }
  const paletteName = (label: number, color: string) =>
    palette[label]?.name ? `${palette[label].name} · ${color}` : `Label ${label} · ${color}`
  const destructive = draft.action !== 'protect'
  const confirm = async () => {
    if (!context || !resolution.valid) return
    setCommitting(true)
    setCommitError(null)
    try {
      await onCommit(await buildManualOperation(context, draft, resolution))
      setReviewing(false)
      onAffectedRegionsChange?.([])
    } catch (error) {
      setCommitError(error instanceof Error ? error.message : 'The operation could not be saved.')
    } finally {
      setCommitting(false)
    }
  }

  return (
    <section className="manual-operations" aria-labelledby={headingId}>
      <div className="manual-operations-heading">
        <div><span>Manual edit</span><h3 id={headingId}>Region operations</h3></div>
        <output aria-label="Draft save state" data-state={persistence}>{persistence === 'saving' ? 'Saving…' : persistence === 'error' ? 'Save failed' : 'Saved'}</output>
      </div>

      <div className="manual-operations-anchor">
        <span className="manual-operations-swatch" style={{ backgroundColor: selectedRegion.color }} aria-hidden="true" />
        <span><strong>{shortRegionId(selectedRegion.id)}</strong><small>{selectedRegion.area_mm2.toFixed(3)} mm² · label {selectedRegion.label}</small></span>
      </div>

      {disabled && disabledReason ? <p className="manual-operations-lock" role="status">{disabledReason}</p> : null}
      {suggestionNote ? <p className="manual-operations-lock" role="status">{suggestionNote}</p> : null}

      <fieldset className="manual-operations-actions">
        <legend>Operation</legend>
        <div>
          {ACTIONS.map((action) => {
            const unavailable = action.id === 'fill' && holeFeatures.length === 0
            const descriptionId = `manual-action-${action.id}`
            const unavailableId = `${descriptionId}-unavailable`
            return (
              <button
                key={action.id}
                type="button"
                aria-pressed={draft.action === action.id}
                aria-disabled={unavailable || disabled}
                aria-describedby={`${descriptionId}${unavailable ? ` ${unavailableId}` : ''}`}
                disabled={disabled}
                onClick={() => {
                  if (unavailable || disabled) return
                  patchDraft({
                    action: action.id,
                    selectedHoleFeatureId: action.id === 'fill'
                      ? draft.selectedHoleFeatureId ?? holeFeatures[0]?.id ?? null
                      : draft.selectedHoleFeatureId,
                  })
                }}
              >
                <span>{action.label}</span>
                <small id={descriptionId}>{action.description}</small>
                {unavailable ? (
                  <small id={unavailableId} className="manual-operations-unavailable">
                    Unavailable: this region is not part of a classified enclosed hole.
                  </small>
                ) : null}
              </button>
            )
          })}
        </div>
      </fieldset>

      {draft.action === 'fill' ? (
        <label className="manual-operations-field">
          <span>Classified hole feature</span>
          <select disabled={disabled} value={draft.selectedHoleFeatureId ?? ''} onChange={(event) => patchDraft({ selectedHoleFeatureId: event.target.value })}>
            {holeFeatures.map((feature) => <option key={feature.id} value={feature.id}>{feature.kind} · {feature.id.slice(8, 16)} · {feature.center_area_mm2.toFixed(3)} mm²</option>)}
          </select>
        </label>
      ) : null}

      {draft.action === 'merge' || draft.action === 'recolor' ? (
        <label className="manual-operations-field">
          <span>Target color</span>
          <select disabled={disabled} value={draft.targetLabel ?? ''} onChange={(event) => patchDraft({ targetLabel: event.target.value === '' ? null : Number(event.target.value) })}>
            <option value="">Choose a target…</option>
            {graph.palette.filter((item) => item.label !== selectedRegion.label).map((item) => (
              <option key={item.label} value={item.label}>{paletteName(item.label, item.color)}</option>
            ))}
          </select>
        </label>
      ) : null}

      {draft.action === 'thicken' ? (
        <div className="manual-operations-thicken">
          <label className="manual-operations-field">
            <span>Expansion radius</span>
            <span className="manual-operations-number"><input disabled={disabled} aria-label="Expansion radius millimetres" type="number" min="0.01" max="10" step="0.01" value={draft.radiusMm} onChange={(event) => patchDraft({ radiusMm: Number(event.target.value) })} /><em>mm</em></span>
          </label>
          <fieldset>
            <legend>Colors this edit may replace</legend>
            {graph.palette.filter((item) => item.label !== selectedRegion.label).map((item) => (
              <label key={item.label}>
                <input disabled={disabled} type="checkbox" checked={draft.editableLabels.includes(item.label)} onChange={(event) => patchDraft({ editableLabels: event.target.checked ? [...draft.editableLabels, item.label].sort((a, b) => a - b) : draft.editableLabels.filter((label) => label !== item.label) })} />
                <span className="manual-operations-swatch" style={{ backgroundColor: item.color }} aria-hidden="true" />
                <span>{paletteName(item.label, item.color)}</span>
              </label>
            ))}
          </fieldset>
        </div>
      ) : null}

      <fieldset className="manual-operations-scope">
        <legend>Scope</legend>
        <div>
          <label><input disabled={disabled} type="radio" name={`${headingId}-scope`} checked={draft.scopeMode === 'selected'} onChange={() => patchDraft({ scopeMode: 'selected' })} />Selected only</label>
          <label><input disabled={disabled} type="radio" name={`${headingId}-scope`} checked={draft.scopeMode === 'similar'} onChange={() => patchDraft({ scopeMode: 'similar' })} />Apply to similar</label>
        </div>
      </fieldset>

      {draft.scopeMode === 'similar' && draft.action !== 'fill' ? (
        <details className="manual-operations-predicate-controls">
          <summary>Similarity tolerances</summary>
          <label><span>Area</span><input disabled={disabled} aria-label="Area ratio tolerance" type="range" min="0" max="1" step="0.05" value={draft.predicate.area_ratio_tolerance} onChange={(event) => patchDraft({ predicate: { ...draft.predicate, area_ratio_tolerance: Number(event.target.value) } })} /><output>{Math.round(draft.predicate.area_ratio_tolerance * 100)}%</output></label>
          <label><span>Minimum width</span><input disabled={disabled} aria-label="Minimum width ratio tolerance" type="range" min="0" max="1" step="0.05" value={draft.predicate.minimum_width_ratio_tolerance} onChange={(event) => patchDraft({ predicate: { ...draft.predicate, minimum_width_ratio_tolerance: Number(event.target.value) } })} /><output>{Math.round(draft.predicate.minimum_width_ratio_tolerance * 100)}%</output></label>
          <label><span>Compactness</span><input disabled={disabled} aria-label="Compactness tolerance" type="range" min="0" max="1" step="0.05" value={draft.predicate.compactness_tolerance} onChange={(event) => patchDraft({ predicate: { ...draft.predicate, compactness_tolerance: Number(event.target.value) } })} /><output>{draft.predicate.compactness_tolerance.toFixed(2)}</output></label>
          <label><input disabled={disabled} type="checkbox" checked={draft.predicate.match_border_contact} onChange={(event) => patchDraft({ predicate: { ...draft.predicate, match_border_contact: event.target.checked } })} />Match canvas-edge contact</label>
          <label><input disabled={disabled} type="checkbox" checked={draft.predicate.match_neighbor_labels} onChange={(event) => patchDraft({ predicate: { ...draft.predicate, match_neighbor_labels: event.target.checked } })} />Match neighboring label set</label>
        </details>
      ) : null}

      <div className="manual-operations-resolution" data-valid={resolution.valid}>
        <strong>Deterministic scope</strong>
        <p>{resolution.predicateLabel}</p>
        <output aria-live="polite">{draft.action === 'fill' ? resolution.affectedFeatureIds.length : resolution.affectedRegionIds.length} exact {draft.action === 'fill' ? 'feature' : 'region'}{(draft.action === 'fill' ? resolution.affectedFeatureIds.length : resolution.affectedRegionIds.length) === 1 ? '' : 's'} will be affected.</output>
        {resolution.error ? <p className="manual-operations-error" role="alert">{resolution.error}</p> : null}
      </div>

      {!reviewing ? (
        <button ref={reviewButtonRef} className="manual-operations-review" type="button" disabled={disabled || !resolution.valid} onClick={() => setReviewing(true)}>Review exact change</button>
      ) : (
        <div className="manual-operations-confirm" role="region" aria-label={`Confirm ${draft.action} operation`}>
          <strong>{destructive ? 'Confirm destructive edit' : 'Confirm pixel protection'}</strong>
          <p>No pixels change until you confirm. This command makes the current preview stale; choose Render preview to apply it exactly.</p>
          <ul aria-label="Affected regions">
            {resolution.affectedRegionIds.map((regionId) => <li key={regionId}><button type="button" onClick={() => onRegionSelect(regionId)}>{regionId}</button></li>)}
          </ul>
          {resolution.affectedFeatureIds.length ? <p>{resolution.affectedFeatureIds.length} classified feature ID{resolution.affectedFeatureIds.length === 1 ? '' : 's'}: {resolution.affectedFeatureIds.join(', ')}</p> : null}
          {commitError ? <p className="manual-operations-error" role="alert">{commitError}</p> : null}
          <div>
            <button ref={cancelButtonRef} type="button" disabled={committing} onClick={() => { setReviewing(false); setCommitError(null) }}>Cancel</button>
            <button type="button" data-danger={destructive} disabled={committing || disabled} onClick={() => void confirm()}>{committing ? 'Saving…' : `Confirm ${draft.action}`}</button>
          </div>
        </div>
      )}
    </section>
  )
}
