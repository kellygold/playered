import { useId, useRef } from 'react'
import type { MergePolicy } from '../../contracts'
import './CleanupControlsPanel.css'
import {
  applyCleanupPreset,
  backendOverrideDelta,
  changedCleanupFields,
  CLEANUP_CONTROL_SPECS,
  CLEANUP_PRESETS,
  CLEANUP_SECTION_FIELDS,
  detectCleanupPreset,
  equivalentDiameterMm,
  overriddenFieldsForPreset,
  pixelGuidance,
  resetCleanupSection,
  validateCleanupNumericInput,
  type CleanupControlField,
  type CleanupControlValues,
  type CleanupMode,
  type CleanupNumericField,
  type CleanupPresetId,
  type CleanupPresetSelection,
  type CleanupProfileContext,
  type CleanupSection,
  type PixelScale,
  type PreviewFreshness,
} from '../cleanupModel'

export type CleanupControlsChange = {
  values: CleanupControlValues
  changedFields: CleanupControlField[]
  phase: 'provisional' | 'commit'
  reason: 'field' | 'preset' | 'section_reset'
  activePreset: CleanupPresetSelection
  overrides: {
    set: CleanupControlField[]
    clear: CleanupControlField[]
    backendCleanup: ReturnType<typeof backendOverrideDelta>
  }
  clamped: boolean
}

export type CleanupControlsPanelProps = {
  values: CleanupControlValues
  profile: CleanupProfileContext
  mode: CleanupMode
  previewFreshness: PreviewFreshness
  pixelScale: PixelScale | null
  overriddenFields?: readonly CleanupControlField[]
  dirtyFields?: readonly CleanupControlField[]
  activePreset?: CleanupPresetSelection
  disabled?: boolean
  onModeChange: (mode: CleanupMode) => void
  mergePolicy: MergePolicy
  onMergePolicyChange: (policy: MergePolicy) => void
  onChange: (change: CleanupControlsChange) => void
}

const sectionCopy: Record<CleanupSection, { title: string; description: string }> = {
  regions: {
    title: 'Region cleanup',
    description: 'Detect small regions, choose how islands are handled, and soften jagged contours.',
  },
  features: {
    title: 'Printable widths',
    description: 'Set independent physical limits for strokes, connections, and gaps.',
  },
  long_lines: {
    title: 'Long-line protection',
    description: 'Keep intentional narrow strokes from being mistaken for disposable specks.',
  },
}

function fieldValue(
  values: CleanupControlValues,
  field: CleanupControlField,
): number | boolean {
  return values[field]
}

function evidenceLabel(profile: CleanupProfileContext): string {
  if (profile.evidenceStatus === 'print_validated') return 'Print validated'
  if (profile.evidenceStatus === 'partially_validated') return 'Partially print validated'
  return 'Provisional engineering baseline'
}

function previewMessage(freshness: PreviewFreshness): string {
  if (freshness === 'current') return 'Preview matches these cleanup settings.'
  if (freshness === 'rendering') return 'Updating the preview for these settings…'
  if (freshness === 'stale') return 'Preview is out of date. Render a preview to apply saved settings.'
  return 'Render a preview to see cleanup results and pixel equivalents.'
}

function NumericControl({
  field,
  value,
  profile,
  pixelScale,
  overridden,
  dirty,
  disabled,
  onFieldChange,
}: {
  field: CleanupNumericField
  value: number
  profile: CleanupProfileContext
  pixelScale: PixelScale | null
  overridden: boolean
  dirty: boolean
  disabled: boolean
  onFieldChange: (
    field: CleanupNumericField,
    value: number,
    phase: 'provisional' | 'commit',
    clamped?: boolean,
  ) => void
}) {
  const id = useId()
  const provisionalValue = useRef<{ value: number; clamped: boolean } | null>(null)
  const spec = CLEANUP_CONTROL_SPECS[field]
  const sliderMax = Math.min(
    spec.max,
    Math.max(value, profile.recommendations[field].profileValue * 4, profile.nozzleMm * 2),
  )
  const status = overridden ? 'User override' : 'Profile value'
  const guidance = pixelGuidance(field, value, pixelScale)
  const commitValue = () => {
    const pending = provisionalValue.current
    if (!pending) return
    const result = { value: pending.value, valid: true, clamped: pending.clamped, message: null }
    if (result.valid && result.value !== null) {
      onFieldChange(field, result.value, 'commit', result.clamped)
    }
    provisionalValue.current = null
  }
  const provisionalRawValue = (raw: string | number) => {
    const result = validateCleanupNumericInput(field, raw)
    if (result.valid && result.value !== null) {
      provisionalValue.current = { value: result.value, clamped: result.clamped }
      onFieldChange(field, result.value, 'provisional', result.clamped)
    }
  }

  return (
    <div className="cleanup-control" data-field={field}>
      <div className="cleanup-control-heading">
        <label htmlFor={`${id}-number`}>{spec.label}</label>
        <span className="cleanup-control-state">
          {status}{dirty ? ' · Unsaved' : ''}
        </span>
      </div>
      <div className="cleanup-control-inputs">
        <input
          id={`${id}-range`}
          type="range"
          aria-label={`${spec.shortLabel} slider`}
          aria-describedby={`${id}-unit ${id}-help ${id}-status`}
          min={spec.min}
          max={sliderMax}
          step={spec.step}
          value={value}
          disabled={disabled}
          onChange={(event) => provisionalRawValue(event.currentTarget.value)}
          onPointerUp={commitValue}
          onBlur={commitValue}
          onKeyDown={(event) => {
            if (event.key === 'Enter') commitValue()
          }}
        />
        <span className="cleanup-number-input">
          <input
            id={`${id}-number`}
            type="number"
            inputMode="decimal"
            min={spec.min}
            max={spec.max}
            step={spec.step}
            value={value}
            disabled={disabled}
            aria-describedby={`${id}-unit ${id}-help ${id}-status`}
            onChange={(event) => provisionalRawValue(event.currentTarget.value)}
            onBlur={commitValue}
            onKeyDown={(event) => {
              if (event.key === 'Enter') commitValue()
            }}
          />
          <span id={`${id}-unit`}>
            <span aria-hidden="true">{spec.unit}</span>
            <span className="sr-only">{spec.unit === 'mm²' ? 'square millimetres' : 'millimetres'}</span>
          </span>
        </span>
      </div>
      <details className="cleanup-control-rationale">
        <summary>Help & profile value</summary>
        <p id={`${id}-help`} className="cleanup-control-help">{spec.help}</p>
        <p id={`${id}-status`} className="cleanup-control-guidance">
          Profile: {profile.recommendations[field].profileValue} {spec.unit}.
          {guidance ? ` ${guidance}` : ''}
        </p>
        <p>{profile.recommendations[field].rationale}</p>
      </details>
    </div>
  )
}

export function CleanupControlsPanel({
  values,
  profile,
  mode,
  previewFreshness,
  pixelScale,
  overriddenFields = [],
  dirtyFields = [],
  activePreset,
  disabled = false,
  onModeChange,
  mergePolicy,
  onMergePolicyChange,
  onChange,
}: CleanupControlsPanelProps) {
  const headingId = useId()
  const overridden = new Set(overriddenFields)
  const dirty = new Set(dirtyFields)
  const selectedPreset = activePreset ?? detectCleanupPreset(values, profile)
  const isDirty = dirty.size > 0

  const dispatch = (
    next: CleanupControlValues,
    metadata: Omit<CleanupControlsChange, 'values' | 'changedFields' | 'overrides'> & {
      setOverrides: CleanupControlField[]
      clearOverrides: CleanupControlField[]
    },
  ) => {
    const changedFields = changedCleanupFields(values, next)
    onChange({
      values: next,
      changedFields,
      phase: metadata.phase,
      reason: metadata.reason,
      activePreset: metadata.activePreset,
      overrides: {
        set: metadata.setOverrides,
        clear: metadata.clearOverrides,
        backendCleanup: backendOverrideDelta(metadata.setOverrides, metadata.clearOverrides),
      },
      clamped: metadata.clamped,
    })
  }

  const changeField = (
    field: CleanupNumericField,
    value: number,
    phase: 'provisional' | 'commit',
    clamped = false,
  ) => {
    dispatch(
      { ...values, [field]: value },
      {
        phase,
        reason: 'field',
        activePreset: 'custom',
        setOverrides: [field],
        clearOverrides: [],
        clamped,
      },
    )
  }

  const applyPreset = (preset: CleanupPresetId) => {
    const next = applyCleanupPreset(preset, profile)
    const nextOverrides = preset === 'profile' ? [] : overriddenFieldsForPreset(next, profile)
    const cleared = CLEANUP_SECTION_FIELDS.regions
      .concat(CLEANUP_SECTION_FIELDS.features, CLEANUP_SECTION_FIELDS.long_lines)
      .filter((field) => !nextOverrides.includes(field))
    dispatch(next, {
      phase: 'commit',
      reason: 'preset',
      activePreset: preset,
      setOverrides: nextOverrides,
      clearOverrides: cleared,
      clamped: false,
    })
  }

  const resetSection = (section: CleanupSection) => {
    const resetFields = [...CLEANUP_SECTION_FIELDS[section]]
    const next = resetCleanupSection(values, section, profile)
    const remainingOverrides = [...overridden].filter((field) => !resetFields.includes(field))
    dispatch(next, {
      phase: 'commit',
      reason: 'section_reset',
      activePreset: remainingOverrides.length === 0 && detectCleanupPreset(next, profile) === 'profile'
        ? 'profile'
        : 'custom',
      setOverrides: [],
      clearOverrides: resetFields,
      clamped: false,
    })
  }

  const renderSection = (section: CleanupSection) => {
    if (section === 'features' && mode !== 'advanced') return null
    const fields = CLEANUP_SECTION_FIELDS[section]
    const resettable = fields.some((field) => overridden.has(field) || dirty.has(field))
    return (
      <fieldset className="cleanup-section" key={section}>
        <legend>{sectionCopy[section].title}</legend>
        <div className="cleanup-section-heading">
          <p>{sectionCopy[section].description}</p>
          <button
            type="button"
            disabled={disabled || !resettable}
            onClick={() => resetSection(section)}
            aria-label={`Reset ${sectionCopy[section].title} to profile defaults`}
          >
            Reset section
          </button>
        </div>
        {section === 'regions' ? (
          <label className="cleanup-policy-select">
            <span>Automatic island policy</span>
            <select
              aria-label="Automatic island policy"
              value={mergePolicy}
              disabled={disabled}
              onChange={(event) => onMergePolicyChange(event.currentTarget.value as MergePolicy)}
            >
              <option value="review">Review only — do not change pixels</option>
              <option value="keep">Keep all detected islands</option>
              <option value="dominant_neighbor">Merge into dominant touching color</option>
              <option value="perceptual_neighbor">Merge into closest touching color</option>
            </select>
            <small>
              Border-touching regions and protected long lines are never merged automatically.
            </small>
          </label>
        ) : null}
        {section === 'long_lines' ? (
          <div className="cleanup-toggle-row">
            <label>
              <input
                type="checkbox"
                checked={values.preserve_long_lines}
                disabled={disabled}
                onChange={(event) => {
                  const next = { ...values, preserve_long_lines: event.currentTarget.checked }
                  dispatch(next, {
                    phase: 'commit',
                    reason: 'field',
                    activePreset: 'custom',
                    setOverrides: ['preserve_long_lines'],
                    clearOverrides: [],
                    clamped: false,
                  })
                }}
              />
              Preserve intentional long narrow lines
            </label>
            <span>{overridden.has('preserve_long_lines') ? 'User override' : 'Profile value'}</span>
          </div>
        ) : null}
        {fields.map((field) => {
          if (field === 'preserve_long_lines') return null
          if (CLEANUP_CONTROL_SPECS[field].advancedOnly && mode !== 'advanced') return null
          return (
            <NumericControl
              key={field}
              field={field}
              value={fieldValue(values, field) as number}
              profile={profile}
              pixelScale={pixelScale}
              overridden={overridden.has(field)}
              dirty={dirty.has(field)}
              disabled={disabled || (field === 'long_line_minimum_length_mm' && !values.preserve_long_lines)}
              onFieldChange={changeField}
            />
          )
        })}
      </fieldset>
    )
  }

  return (
    <section className="cleanup-controls-panel" aria-labelledby={headingId} aria-busy={previewFreshness === 'rendering'}>
      <header className="cleanup-panel-heading">
        <div>
          <span className="eyebrow">Physical cleanup</span>
          <h2 id={headingId}>Make details printable</h2>
        </div>
        <span className="cleanup-save-state" role="status">
          {isDirty ? 'Unsaved cleanup edits' : 'Cleanup settings saved'}
        </span>
      </header>

      <details className="cleanup-profile-summary">
        <summary>Printer profile & guidance</summary>
        <strong>{profile.displayName}</strong>
        <span>{profile.nozzleMm} mm nozzle</span>
        <span>{evidenceLabel(profile)}</span>
        <span className="sr-only">Profile identifier: {profile.id}</span>
        <span className="sr-only">Profile catalog: {profile.catalogFingerprint}</span>
        {profile.warning ? <p role="note">{profile.warning}</p> : null}
      </details>

      <p className={`cleanup-preview-state is-${previewFreshness}`} role="status" aria-live="polite">
        {previewMessage(previewFreshness)}
      </p>

      <div className="cleanup-mode" role="group" aria-label="Cleanup controls detail level">
        <button type="button" aria-pressed={mode === 'basic'} disabled={disabled} onClick={() => onModeChange('basic')}>
          Basic
        </button>
        <button type="button" aria-pressed={mode === 'advanced'} disabled={disabled} onClick={() => onModeChange('advanced')}>
          Advanced
        </button>
      </div>

      <div className="cleanup-presets" role="group" aria-label="Cleanup presets">
        {CLEANUP_PRESETS.map((preset) => (
          <button
            type="button"
            key={preset.id}
            aria-pressed={selectedPreset === preset.id}
            title={preset.description}
            disabled={disabled}
            onClick={() => applyPreset(preset.id)}
          >
            {preset.label}
          </button>
        ))}
        {selectedPreset === 'custom' ? <span className="cleanup-custom-state">Custom settings</span> : null}
      </div>

      <details className="cleanup-control-rationale">
        <summary>Area & diameter equivalents</summary>
      <p className="cleanup-diameter-guidance">
        Profile diameter guidance: islands {profile.islandDiameterMm} mm; tiny holes {profile.tinyHoleDiameterMm} mm.
        Current area equivalents: islands {equivalentDiameterMm(values.minimum_island_area_mm2).toFixed(2)} mm;
        tiny holes {equivalentDiameterMm(values.maximum_tiny_hole_area_mm2).toFixed(2)} mm.
      </p>

      </details>

      {renderSection('regions')}
      {renderSection('features')}
      {renderSection('long_lines')}
    </section>
  )
}
