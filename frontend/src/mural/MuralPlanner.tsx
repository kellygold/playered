import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import {
  deleteMuralPlan,
  fetchMuralPlan,
  MuralApiError,
  previewMuralPlan,
  saveMuralPlan,
} from './muralApi'
import type {
  BedRectangle,
  MuralLayout,
  MuralOrientation,
  MuralPlanPreview,
  MuralPlanResource,
  MuralPlanSettings,
} from './types'
import './MuralPlanner.css'

const DEFAULT_LAYOUT: MuralLayout = {
  rows: 2,
  columns: 3,
  panel_width_mm: 200,
  panel_height_mm: 200,
  horizontal_gap_mm: 0,
  vertical_gap_mm: 0,
  bleed_mm: 0,
  orientation: 'auto',
}

type NumericLayoutKey = Exclude<keyof MuralLayout, 'orientation'>

export type MuralPlannerProps = {
  projectId: string
  processedArtifactId: string
  masterImageUrl: string
  disabled?: boolean
  onPlanStateChange?: (state: MuralPlannerState) => void
}

export type MuralPlannerState = Readonly<{
  plan: MuralPlanResource | null
  dirty: boolean
  hydrated: boolean
}>

function errorMessage(error: unknown): string {
  if (error instanceof MuralApiError) {
    const reason = error.details.reason
    if (typeof reason === 'string') return reason
    if (error.status === 409) return 'This plan changed elsewhere. Reload it before saving again.'
  }
  return error instanceof Error ? error.message : 'The mural planner could not complete this action.'
}

function formatMm(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
}

function clamp(value: number, minimum: number, maximum: number, integer = false): number {
  if (!Number.isFinite(value)) return minimum
  const bounded = Math.min(maximum, Math.max(minimum, value))
  return integer ? Math.round(bounded) : Math.round(bounded * 100) / 100
}

function settingsFromResource(resource: MuralPlanResource): MuralPlanSettings {
  return {
    processed_artifact_id: resource.request.source.processed_artifact_id,
    layout: resource.request.layout,
    edge_clearance_mm: resource.request.bed.edge_clearance_mm,
    reserved_rectangles: resource.request.reserved_rectangles,
  }
}

function settingsFingerprint(settings: MuralPlanSettings): string {
  return JSON.stringify(settings)
}

export function MuralPlanner({
  projectId,
  processedArtifactId,
  masterImageUrl,
  disabled = false,
  onPlanStateChange,
}: MuralPlannerProps) {
  const loadSequence = useRef(0)
  const saveSequence = useRef(0)
  const saveController = useRef<AbortController | null>(null)
  const [loadedProjectId, setLoadedProjectId] = useState<string | null>(null)
  const [layout, setLayout] = useState<MuralLayout>(DEFAULT_LAYOUT)
  const [edgeClearanceMm, setEdgeClearanceMm] = useState(0)
  const [reservations, setReservations] = useState<BedRectangle[]>([])
  const [generation, setGeneration] = useState(0)
  const [saved, setSaved] = useState<MuralPlanResource | null>(null)
  const [savedSettings, setSavedSettings] = useState<MuralPlanSettings | null>(null)
  const [preview, setPreview] = useState<MuralPlanPreview | null>(null)
  const [loadingPreview, setLoadingPreview] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)

  const settings = useMemo<MuralPlanSettings>(() => ({
    processed_artifact_id: processedArtifactId,
    layout,
    edge_clearance_mm: edgeClearanceMm,
    reserved_rectangles: reservations,
  }), [edgeClearanceMm, layout, processedArtifactId, reservations])
  const settingsRef = useRef(settings)
  const hydrated = loadedProjectId === projectId
  const dirty = savedSettings === null
    || settingsFingerprint(settings) !== settingsFingerprint(savedSettings)
  const hasSavableChanges = dirty || saved?.freshness === 'stale'

  useEffect(() => {
    onPlanStateChange?.({ plan: saved, dirty, hydrated })
  }, [dirty, hydrated, onPlanStateChange, saved])

  useEffect(() => {
    settingsRef.current = settings
  }, [settings])

  useEffect(() => {
    const controller = new AbortController()
    const sequence = ++loadSequence.current
    saveController.current?.abort()
    saveSequence.current += 1
    fetchMuralPlan(projectId, controller.signal)
      .then((resource) => {
        if (controller.signal.aborted || sequence !== loadSequence.current) return
        setError(null)
        setConflict(false)
        if (resource) {
          const restoredSettings = settingsFromResource(resource)
          setLayout(resource.request.layout)
          setEdgeClearanceMm(resource.request.bed.edge_clearance_mm)
          setReservations(resource.request.reserved_rectangles)
          setGeneration(resource.generation)
          setSaved(resource)
          setSavedSettings(restoredSettings)
          setPreview(resource)
        } else {
          setLayout(DEFAULT_LAYOUT)
          setEdgeClearanceMm(0)
          setReservations([])
          setGeneration(0)
          setSaved(null)
          setSavedSettings(null)
          setPreview(null)
        }
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError' && sequence === loadSequence.current) {
          setSaved(null)
          setSavedSettings(null)
          setPreview(null)
          setGeneration(0)
          setError(errorMessage(reason))
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && sequence === loadSequence.current) {
          setSaving(false)
          setLoadedProjectId(projectId)
        }
      })
    return () => controller.abort()
  }, [projectId])

  useEffect(() => () => saveController.current?.abort(), [])

  useEffect(() => {
    if (!hydrated || !processedArtifactId) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      setLoadingPreview(true)
      setError(null)
      previewMuralPlan(projectId, settings, controller.signal)
        .then((result) => {
          if (!controller.signal.aborted) setPreview(result)
        })
        .catch((reason: unknown) => {
          if ((reason as Error).name !== 'AbortError' && !controller.signal.aborted) {
            setPreview(null)
            setError(errorMessage(reason))
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoadingPreview(false)
        })
    }, 180)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [hydrated, processedArtifactId, projectId, settings])

  const updateLayoutNumber = (
    key: NumericLayoutKey,
    raw: number,
    minimum: number,
    maximum: number,
    integer = false,
  ) => {
    setLayout((current) => ({
      ...current,
      [key]: clamp(raw, minimum, maximum, integer),
    }))
  }

  const updateReservation = (
    index: number,
    key: keyof BedRectangle,
    raw: number,
  ) => {
    setReservations((current) => current.map((rectangle, rectangleIndex) => {
      if (rectangleIndex !== index) return rectangle
      const bedWidth = bed?.width_mm ?? 2_000
      const bedHeight = bed?.height_mm ?? 2_000
      const maximum = key === 'x_mm'
        ? Math.max(0, bedWidth - rectangle.width_mm)
        : key === 'y_mm'
          ? Math.max(0, bedHeight - rectangle.height_mm)
          : key === 'width_mm'
            ? Math.max(0.1, bedWidth - rectangle.x_mm)
            : Math.max(0.1, bedHeight - rectangle.y_mm)
      const minimum = key === 'width_mm' || key === 'height_mm' ? 0.1 : 0
      return { ...rectangle, [key]: clamp(raw, minimum, maximum) }
    }))
  }

  const addReservation = () => {
    const width = Math.min(35, bed?.width_mm ?? 35)
    const height = Math.min(35, bed?.height_mm ?? 35)
    setReservations((current) => [
      ...current,
      {
        x_mm: Math.max(0, (bed?.width_mm ?? width) - width - 5),
        y_mm: Math.min(5, Math.max(0, (bed?.height_mm ?? height) - height)),
        width_mm: width,
        height_mm: height,
      },
    ])
  }

  const save = async () => {
    const controller = new AbortController()
    saveController.current?.abort()
    saveController.current = controller
    const sequence = ++saveSequence.current
    const snapshot = settings
    const snapshotFingerprint = settingsFingerprint(snapshot)
    setSaving(true)
    setError(null)
    setConflict(false)
    try {
      const resource = await saveMuralPlan(projectId, {
        ...snapshot,
        expected_generation: generation,
      }, controller.signal)
      if (controller.signal.aborted || sequence !== saveSequence.current) return
      setSaved(resource)
      setSavedSettings(snapshot)
      if (settingsFingerprint(settingsRef.current) === snapshotFingerprint) setPreview(resource)
      setGeneration(resource.generation)
    } catch (reason) {
      if (controller.signal.aborted || sequence !== saveSequence.current) return
      if (reason instanceof MuralApiError && reason.status === 409) setConflict(true)
      setError(errorMessage(reason))
    } finally {
      if (!controller.signal.aborted && sequence === saveSequence.current) setSaving(false)
    }
  }

  const reloadSaved = async () => {
    const sequence = ++loadSequence.current
    setError(null)
    try {
      const resource = await fetchMuralPlan(projectId)
      if (sequence !== loadSequence.current) return
      if (!resource) {
        setGeneration(0)
        setSaved(null)
        setSavedSettings(null)
        setConflict(false)
        return
      }
      const restoredSettings = settingsFromResource(resource)
      setLayout(restoredSettings.layout)
      setEdgeClearanceMm(restoredSettings.edge_clearance_mm)
      setReservations(restoredSettings.reserved_rectangles)
      setGeneration(resource.generation)
      setSaved(resource)
      setSavedSettings(restoredSettings)
      setPreview(resource)
      setConflict(false)
    } catch (reason) {
      setError(errorMessage(reason))
    }
  }

  const remove = async () => {
    if (generation < 1) return
    setDeleting(true)
    setError(null)
    try {
      await deleteMuralPlan(projectId, generation)
      setGeneration(0)
      setSaved(null)
      setSavedSettings(null)
      setConflict(false)
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setDeleting(false)
    }
  }

  const result = preview?.plan ?? null
  const firstTile = result?.tiles[0] ?? null
  const bed = preview?.request.bed ?? saved?.request.bed ?? null
  const gridStyle = {
    '--mural-columns': layout.columns,
    '--mural-rows': layout.rows,
    aspectRatio: result
      ? `${result.master_size_mm.width} / ${result.master_size_mm.height}`
      : `${layout.columns * layout.panel_width_mm} / ${layout.rows * layout.panel_height_mm}`,
  } as CSSProperties
  const busy = !hydrated || loadingPreview || saving || deleting
  const controlsDisabled = disabled || saving || deleting
  const saveLabel = saving ? 'Saving plan…' : generation > 0 ? 'Update mural plan' : 'Save mural plan'

  return (
    <section className="mural-planner" aria-labelledby="mural-planner-heading" aria-busy={busy}>
      <header className="mural-planner-heading">
        <div>
          <span className="eyebrow">Master canvas</span>
          <h2 id="mural-planner-heading">Mural tile planner</h2>
          <p>Divide one processed master mathematically; every tile keeps a shared edge.</p>
        </div>
        <span
          className="mural-save-state"
          data-state={conflict ? 'conflict' : saved?.freshness === 'stale' ? 'stale' : dirty ? 'dirty' : 'current'}
        >
          {conflict
            ? 'Save conflict'
            : saved?.freshness === 'stale'
            ? 'Saved plan stale'
            : dirty && generation > 0
              ? 'Unsaved changes'
            : generation > 0
              ? `Saved · v${generation}`
              : 'Not saved'}
        </span>
      </header>

      {saved?.freshness === 'stale' ? (
        <p className="mural-callout is-warning" role="status">
          The processed master changed. Review this preview and save a new generation.
          {saved.stale_reason ? ` ${saved.stale_reason}` : ''}
        </p>
      ) : null}

      <div className="mural-control-grid">
        <label>
          <span>Columns</span>
          <input
            aria-label="Mural columns"
            type="number"
            min="1"
            max="50"
            step="1"
            value={layout.columns}
            disabled={controlsDisabled}
            onChange={(event) => updateLayoutNumber('columns', Number(event.target.value), 1, 50, true)}
          />
        </label>
        <label>
          <span>Rows</span>
          <input
            aria-label="Mural rows"
            type="number"
            min="1"
            max="50"
            step="1"
            value={layout.rows}
            disabled={controlsDisabled}
            onChange={(event) => updateLayoutNumber('rows', Number(event.target.value), 1, 50, true)}
          />
        </label>
        <label>
          <span>Panel width</span>
          <span className="mural-number">
            <input
              aria-label="Panel width millimetres"
              type="number"
              min="10"
              max="2000"
              step="1"
              value={layout.panel_width_mm}
              disabled={controlsDisabled}
              onChange={(event) => updateLayoutNumber('panel_width_mm', Number(event.target.value), 10, 2000)}
            />
            <i>mm</i>
          </span>
        </label>
        <label>
          <span>Panel height</span>
          <span className="mural-number">
            <input
              aria-label="Panel height millimetres"
              type="number"
              min="10"
              max="2000"
              step="1"
              value={layout.panel_height_mm}
              disabled={controlsDisabled}
              onChange={(event) => updateLayoutNumber('panel_height_mm', Number(event.target.value), 10, 2000)}
            />
            <i>mm</i>
          </span>
        </label>
      </div>

      <label className="mural-select">
        <span>Build-plate orientation</span>
        <select
          aria-label="Build-plate orientation"
          value={layout.orientation}
          disabled={controlsDisabled}
          onChange={(event) => setLayout((current) => ({
            ...current,
            orientation: event.target.value as MuralOrientation,
          }))}
        >
          <option value="auto">Auto · prefer native</option>
          <option value="native">Native only</option>
          <option value="rotate_90">Rotate every tile 90°</option>
        </select>
      </label>

      <details className="mural-spacing">
        <summary>Assembly spacing and bleed</summary>
        <div className="mural-control-grid">
          <label>
            <span>Horizontal gap</span>
            <input
              aria-label="Horizontal gap millimetres"
              type="number"
              min="0"
              max="500"
              step="0.5"
              value={layout.horizontal_gap_mm}
              disabled={controlsDisabled}
              onChange={(event) => updateLayoutNumber('horizontal_gap_mm', Number(event.target.value), 0, 500)}
            />
          </label>
          <label>
            <span>Vertical gap</span>
            <input
              aria-label="Vertical gap millimetres"
              type="number"
              min="0"
              max="500"
              step="0.5"
              value={layout.vertical_gap_mm}
              disabled={controlsDisabled}
              onChange={(event) => updateLayoutNumber('vertical_gap_mm', Number(event.target.value), 0, 500)}
            />
          </label>
          <label>
            <span>Bleed per edge</span>
            <input
              aria-label="Bleed millimetres"
              type="number"
              min="0"
              max="100"
              step="0.25"
              value={layout.bleed_mm}
              disabled={controlsDisabled}
              onChange={(event) => updateLayoutNumber('bleed_mm', Number(event.target.value), 0, 100)}
            />
          </label>
          <label>
            <span>Plate clearance</span>
            <input
              aria-label="Plate edge clearance millimetres"
              type="number"
              min="0"
              max="100"
              step="0.5"
              value={edgeClearanceMm}
              disabled={controlsDisabled}
              onChange={(event) => setEdgeClearanceMm(clamp(Number(event.target.value), 0, 100))}
            />
          </label>
        </div>
        <p>Gaps change the installed footprint, not the master crop. Bleed adds overlap outside each nominal seam.</p>
      </details>

      <details className="mural-spacing mural-reservations">
        <summary>Reserved plate areas · {reservations.length}</summary>
        <p>Keep each tile clear of prime towers, clips, or any area you do not want the model to occupy.</p>
        <div className="mural-reservation-list">
          {reservations.map((rectangle, index) => (
            <fieldset key={index}>
              <legend>Area {index + 1}</legend>
              <div className="mural-control-grid">
                {(
                  [
                    ['x_mm', 'X'],
                    ['y_mm', 'Y'],
                    ['width_mm', 'Width'],
                    ['height_mm', 'Height'],
                  ] as const
                ).map(([key, label]) => (
                  <label key={key}>
                    <span>{label}</span>
                    <input
                      aria-label={`Reservation ${index + 1} ${label} millimetres`}
                      type="number"
                      min={key === 'width_mm' || key === 'height_mm' ? 0.1 : 0}
                      max={key === 'x_mm'
                        ? Math.max(0, (bed?.width_mm ?? 2_000) - rectangle.width_mm)
                        : key === 'y_mm'
                          ? Math.max(0, (bed?.height_mm ?? 2_000) - rectangle.height_mm)
                          : key === 'width_mm'
                            ? Math.max(0.1, (bed?.width_mm ?? 2_000) - rectangle.x_mm)
                            : Math.max(0.1, (bed?.height_mm ?? 2_000) - rectangle.y_mm)}
                      step="0.5"
                      value={rectangle[key]}
                      disabled={controlsDisabled}
                      onChange={(event) => updateReservation(index, key, Number(event.target.value))}
                    />
                  </label>
                ))}
              </div>
              <button
                type="button"
                className="mural-reservation-remove"
                disabled={controlsDisabled}
                onClick={() => setReservations((current) => current.filter((_, itemIndex) => itemIndex !== index))}
              >
                Remove area {index + 1}
              </button>
            </fieldset>
          ))}
        </div>
        <button
          type="button"
          className="mural-reservation-add"
          disabled={controlsDisabled || reservations.length >= 12}
          onClick={addReservation}
        >
          Add reserved area
        </button>
      </details>

      <figure className="mural-preview">
        <div className="mural-preview-title">
          <figcaption>Single-master division</figcaption>
          <span>{layout.columns} × {layout.rows} · {layout.columns * layout.rows} plates</span>
        </div>
        <div className="mural-master" style={gridStyle} aria-label="Mural tile preview">
          <img src={masterImageUrl} alt="Processed master artwork" />
          <div className="mural-tile-grid" aria-hidden="true">
            {Array.from({ length: layout.rows * layout.columns }, (_, index) => (
              <span
                key={index}
                className={(index + 1) % layout.columns === 0 ? 'is-row-end' : undefined}
              >
                {index + 1}
              </span>
            ))}
          </div>
        </div>
        {result ? (
          <dl className="mural-measurements">
            <div>
              <dt>Master</dt>
              <dd>{formatMm(result.master_size_mm.width)} × {formatMm(result.master_size_mm.height)} mm</dd>
            </div>
            <div>
              <dt>Installed</dt>
              <dd>{formatMm(result.assembled_size_mm.width)} × {formatMm(result.assembled_size_mm.height)} mm</dd>
            </div>
            <div>
              <dt>Each output</dt>
              <dd>{firstTile ? `${formatMm(firstTile.output_size_mm.width)} × ${formatMm(firstTile.output_size_mm.height)} mm` : '—'}</dd>
            </div>
            <div>
              <dt>Plate fit</dt>
              <dd className={result.all_tiles_fit ? 'is-fit' : 'is-no-fit'}>
                {result.all_tiles_fit
                  ? `Fits${firstTile?.bed_fit.rotation_degrees === 90 ? ' · rotate 90°' : ' · native'}`
                  : 'Does not fit'}
              </dd>
            </div>
          </dl>
        ) : (
          <p className="mural-preview-loading">{loadingPreview ? 'Calculating exact crops and plate fit…' : 'Preview unavailable.'}</p>
        )}
        {bed ? (
          <div className="mural-bed-summary">
            <div>
              <strong>{bed.printer_id} · {bed.plate_id}</strong>
              <span>
                {formatMm(bed.width_mm)} × {formatMm(bed.height_mm)} mm bed · {formatMm(edgeClearanceMm)} mm edge clearance
              </span>
            </div>
            <div
              className="mural-bed-map"
              role="img"
              aria-label={`${reservations.length} reserved areas on ${formatMm(bed.width_mm)} by ${formatMm(bed.height_mm)} millimetre bed`}
              style={{ aspectRatio: `${bed.width_mm} / ${bed.height_mm}` }}
            >
              <span
                className="mural-bed-usable"
                style={{
                  inset: `${edgeClearanceMm / bed.height_mm * 100}% ${edgeClearanceMm / bed.width_mm * 100}%`,
                }}
              />
              {bed.excluded_rectangles.map((rectangle, index) => (
                <span
                  className="mural-bed-exclusion"
                  key={`profile-${index}`}
                  style={{
                    left: `${rectangle.x_mm / bed.width_mm * 100}%`,
                    top: `${rectangle.y_mm / bed.height_mm * 100}%`,
                    width: `${rectangle.width_mm / bed.width_mm * 100}%`,
                    height: `${rectangle.height_mm / bed.height_mm * 100}%`,
                  }}
                />
              ))}
              {reservations.map((rectangle, index) => (
                <span
                  className="mural-bed-reservation"
                  key={`reserved-${index}`}
                  style={{
                    left: `${rectangle.x_mm / bed.width_mm * 100}%`,
                    top: `${rectangle.y_mm / bed.height_mm * 100}%`,
                    width: `${rectangle.width_mm / bed.width_mm * 100}%`,
                    height: `${rectangle.height_mm / bed.height_mm * 100}%`,
                  }}
                >
                  {index + 1}
                </span>
              ))}
            </div>
          </div>
        ) : null}
      </figure>

      {result && !result.all_tiles_fit ? (
        <p className="mural-callout is-error" role="alert">{result.warnings[0]}</p>
      ) : null}
      {error ? <p className="mural-callout is-error" role="alert">{error}</p> : null}
      {conflict ? (
        <button type="button" className="mural-reload" onClick={() => void reloadSaved()}>
          Reload saved plan
        </button>
      ) : null}

      <div className="mural-actions">
        {generation > 0 ? (
          <button
            type="button"
            className="mural-remove"
            disabled={disabled || deleting || saving}
            onClick={() => void remove()}
          >
            {deleting ? 'Removing…' : 'Remove saved plan'}
          </button>
        ) : <span />}
        <button
          type="button"
          className="mural-save"
          disabled={disabled || busy || !result || !hasSavableChanges}
          onClick={() => void save()}
        >
          {saveLabel}
        </button>
      </div>
    </section>
  )
}
