import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from 'react'
import {
  ApiRequestError,
  autoFitPalette,
  fetchOwnedFilaments,
  fetchStarterFilamentCatalog,
  importStarterFilaments,
} from './api'
import type { Filament, JobConfigV1, PaletteColor } from './contracts'

type PaletteAction =
  | 'auto-fit'
  | 'lock'
  | 'rename'
  | 'reorder'
  | 'replace'
  | 'sample'
  | 'select-filament'

type PaletteEditorProps = {
  config: JobConfigV1
  persistence: 'saved' | 'saving' | 'error'
  projectId: string
  onCommit: (
    config: JobConfigV1,
    action: PaletteAction,
    colorId: string | null,
    source?: 'automatic' | 'manual',
  ) => void
}

type EyeDropperResult = { sRGBHex: string }
type EyeDropperConstructor = new () => { open: () => Promise<EyeDropperResult> }

function normalizedHex(value: string): string | null {
  const trimmed = value.trim().toUpperCase()
  return /^#[0-9A-F]{6}$/.test(trimmed) ? trimmed : null
}

function replaceColor(config: JobConfigV1, id: string, patch: Partial<PaletteColor>): JobConfigV1 {
  return {
    ...config,
    palette: {
      colors: config.palette.colors.map((color) =>
        color.id === id ? { ...color, ...patch } : color,
      ),
    },
  }
}

function moveColor(config: JobConfigV1, from: number, to: number): JobConfigV1 {
  if (from === to || to < 0 || to >= config.palette.colors.length) return config
  const colors = [...config.palette.colors]
  const [moved] = colors.splice(from, 1)
  colors.splice(to, 0, moved)
  return { ...config, palette: { colors } }
}

function nextColorId(colors: PaletteColor[], index: number): string {
  const used = new Set(colors.map((color) => color.id))
  let suffix = index + 1
  while (used.has(`color-${suffix}`)) suffix += 1
  return `color-${suffix}`
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) return error.problem?.error.message ?? error.message
  return error instanceof Error ? error.message : 'The palette action could not be completed.'
}

export function PaletteEditor({
  config,
  persistence,
  projectId,
  onCommit,
}: PaletteEditorProps) {
  const [filaments, setFilaments] = useState<Filament[]>([])
  const [filamentsLoading, setFilamentsLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [targetCount, setTargetCount] = useState(config.palette.colors.length)
  const [colorPickerDrafts, setColorPickerDrafts] = useState<
    Record<string, { baseHex: string; hex: string }>
  >({})
  const configRef = useRef(config)
  const colorPickerTimerRef = useRef<number | null>(null)
  const pendingColorPickerRef = useRef<{ colorId: string; hex: string } | null>(null)

  useEffect(() => {
    configRef.current = config
  }, [config])

  useEffect(() => () => {
    if (colorPickerTimerRef.current !== null) window.clearTimeout(colorPickerTimerRef.current)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetchOwnedFilaments(controller.signal)
      .then(setFilaments)
      .catch((error: unknown) => {
        if ((error as Error).name !== 'AbortError') setMessage(errorMessage(error))
      })
      .finally(() => setFilamentsLoading(false))
    return () => controller.abort()
  }, [])

  const commitColor = (
    color: PaletteColor,
    patch: Partial<PaletteColor>,
    action: PaletteAction,
  ) => {
    const currentConfig = configRef.current
    const next = replaceColor(currentConfig, color.id, patch)
    if (JSON.stringify(next.palette.colors) !== JSON.stringify(currentConfig.palette.colors)) {
      setMessage(null)
      onCommit(next, action, color.id)
    }
  }

  const flushColorPicker = () => {
    if (colorPickerTimerRef.current !== null) window.clearTimeout(colorPickerTimerRef.current)
    colorPickerTimerRef.current = null
    const pending = pendingColorPickerRef.current
    pendingColorPickerRef.current = null
    if (!pending) return
    const currentColor = configRef.current.palette.colors.find(
      (color) => color.id === pending.colorId,
    )
    if (currentColor && currentColor.hex !== pending.hex) {
      commitColor(currentColor, { hex: pending.hex, filament_id: null }, 'replace')
    }
  }

  const previewColorPicker = (colorId: string, value: string) => {
    const hex = value.toUpperCase()
    const baseHex = configRef.current.palette.colors.find((color) => color.id === colorId)?.hex ?? hex
    setColorPickerDrafts((current) => ({ ...current, [colorId]: { baseHex, hex } }))
    pendingColorPickerRef.current = { colorId, hex }
    if (colorPickerTimerRef.current !== null) window.clearTimeout(colorPickerTimerRef.current)
    colorPickerTimerRef.current = window.setTimeout(flushColorPicker, 250)
  }

  const reorder = (from: number, to: number) => {
    const next = moveColor(config, from, to)
    if (next === config) return
    setMessage(null)
    onCommit(next, 'reorder', config.palette.colors[from].id)
  }

  const rowKeyDown = (event: KeyboardEvent<HTMLElement>, index: number) => {
    if (!event.altKey || !['ArrowUp', 'ArrowDown'].includes(event.key)) return
    event.preventDefault()
    reorder(index, index + (event.key === 'ArrowUp' ? -1 : 1))
  }

  const dropColor = (event: DragEvent<HTMLElement>, targetIndex: number) => {
    event.preventDefault()
    const sourceId = draggingId ?? event.dataTransfer.getData('text/plain')
    const sourceIndex = config.palette.colors.findIndex((color) => color.id === sourceId)
    setDraggingId(null)
    if (sourceIndex >= 0) reorder(sourceIndex, targetIndex)
  }

  const sampleColor = async (color: PaletteColor) => {
    const EyeDropper = (window as typeof window & { EyeDropper?: EyeDropperConstructor }).EyeDropper
    if (!EyeDropper) {
      setMessage('Screen color sampling requires a Chromium browser with EyeDropper support.')
      return
    }
    try {
      const sampled = await new EyeDropper().open()
      const hex = normalizedHex(sampled.sRGBHex)
      if (hex) commitColor(color, { hex, filament_id: null }, 'sample')
    } catch (error) {
      if ((error as Error).name !== 'AbortError') setMessage(errorMessage(error))
    }
  }

  const loadStarterColors = async () => {
    setBusy(true)
    setMessage(null)
    try {
      const catalog = await fetchStarterFilamentCatalog()
      await importStarterFilaments(catalog.entries.map((entry) => entry.id))
      setFilaments(await fetchOwnedFilaments())
      setMessage(`${catalog.display_name} is ready in the owned filament chooser.`)
    } catch (error) {
      setMessage(errorMessage(error))
    } finally {
      setBusy(false)
    }
  }

  const fitPalette = async () => {
    const lockedPastTarget = config.palette.colors
      .slice(targetCount)
      .some((color) => color.locked)
    if (lockedPastTarget) {
      setMessage('Unlock or move locked colors into the retained range before reducing the count.')
      return
    }
    setBusy(true)
    setMessage(null)
    try {
      const fit = await autoFitPalette(projectId, config, targetCount)
      const colors = fit.colors.map((hex, index) => {
        const current = config.palette.colors[index]
        if (current) {
          return {
            ...current,
            hex,
            filament_id: current.hex === hex ? current.filament_id : null,
          }
        }
        return {
          id: nextColorId(config.palette.colors, index),
          name: `Color ${index + 1}`,
          hex,
          locked: false,
          filament_id: null,
        }
      })
      onCommit({ ...config, palette: { colors } }, 'auto-fit', null, 'automatic')
      setMessage(
        `Fitted ${fit.unique_color_count} unique colors from ${fit.sample_size.toLocaleString()} samples.`,
      )
    } catch (error) {
      setMessage(errorMessage(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="palette-editor" aria-labelledby="palette-editor-heading">
      <div className="palette-editor-heading">
        <div>
          <span className="eyebrow">Palette Lab</span>
          <h2 id="palette-editor-heading">Print colors</h2>
          <p>Order, lock, sample, and map each color to filament.</p>
        </div>
        <div className="palette-history">
          <span className={`save-chip save-chip-${persistence}`} role="status">
            {persistence === 'saving' ? 'Saving…' : persistence === 'error' ? 'Save failed' : 'Saved'}
          </span>
        </div>
      </div>

      <div className="palette-toolbar">
        <label>
          <span>Color count</span>
          <select
            aria-label="Automatic palette color count"
            value={targetCount}
            onChange={(event) => setTargetCount(Number(event.target.value))}
          >
            {[2, 3, 4, 5, 6, 7, 8].map((count) => (
              <option key={count} value={count}>
                {count} colors
              </option>
            ))}
          </select>
        </label>
        <button className="button button-secondary" type="button" disabled={busy} onClick={fitPalette}>
          {busy ? 'Working…' : 'Auto-fit unlocked'}
        </button>
        <button
          className="starter-link"
          type="button"
          disabled={busy}
          onClick={loadStarterColors}
        >
          Add Bambu starter colors
        </button>
      </div>

      {message ? <p className="palette-message" role="status">{message}</p> : null}

      <ol className="palette-list" aria-label="Ordered print colors">
        {config.palette.colors.map((color, index) => (
          // Drag events are delegated to the list row; native buttons provide the keyboard
          // reorder equivalent and Alt+Arrow continues to work from focused row controls.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
          <li
            aria-label={`Palette color ${index + 1}: ${color.name}`}
            className={draggingId === color.id ? 'is-dragging' : ''}
            draggable
            key={color.id}
            onDragStart={(event) => {
              setDraggingId(color.id)
              event.dataTransfer.effectAllowed = 'move'
              event.dataTransfer.setData('text/plain', color.id)
            }}
            onDragEnd={() => setDraggingId(null)}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => dropColor(event, index)}
            onKeyDown={(event) => rowKeyDown(event, index)}
          >
            <span className="palette-index" aria-label={`Color index ${index + 1}`}>
              {String(index + 1).padStart(2, '0')}
            </span>
            <span className="drag-handle" aria-hidden="true">⠿</span>
            <label className="swatch-control" title="Choose color">
              <span className="visually-hidden">Choose {color.name} color</span>
              <input
                type="color"
                value={colorPickerDrafts[color.id]?.baseHex === color.hex
                  ? colorPickerDrafts[color.id].hex
                  : color.hex}
                onInput={(event) => previewColorPicker(color.id, event.currentTarget.value)}
                onChange={(event) => previewColorPicker(color.id, event.currentTarget.value)}
                onBlur={flushColorPicker}
              />
              <i style={{ backgroundColor: color.hex }} />
            </label>
            <div className="palette-fields">
              <input
                aria-label={`Name for color ${index + 1}`}
                className="palette-name"
                defaultValue={color.name}
                key={`${color.id}:${color.name}`}
                maxLength={120}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') event.currentTarget.blur()
                }}
                onBlur={(event) => {
                  const name = event.target.value.trim()
                  if (name && name !== color.name) commitColor(color, { name }, 'rename')
                  else event.target.value = color.name
                }}
              />
              <input
                aria-label={`Hex value for ${color.name}`}
                className="palette-hex"
                defaultValue={color.hex}
                key={`${color.id}:${color.hex}:hex`}
                maxLength={7}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') event.currentTarget.blur()
                }}
                onBlur={(event) => {
                  const hex = normalizedHex(event.target.value)
                  if (hex && hex !== color.hex) commitColor(color, { hex, filament_id: null }, 'replace')
                  else event.target.value = color.hex
                }}
              />
            </div>
            <label className="filament-select">
              <span className="visually-hidden">Owned filament for {color.name}</span>
              <select
                aria-label={`Owned filament for ${color.name}`}
                disabled={filamentsLoading}
                value={color.filament_id ?? ''}
                onChange={(event) => {
                  const filament = filaments.find((item) => item.id === event.target.value)
                  if (filament) {
                    commitColor(
                      color,
                      { name: filament.name, hex: filament.hex_color, filament_id: filament.id },
                      'select-filament',
                    )
                  } else commitColor(color, { filament_id: null }, 'select-filament')
                }}
              >
                <option value="">{filamentsLoading ? 'Loading filaments…' : 'Custom color'}</option>
                {filaments.map((filament) => (
                  <option key={filament.id} value={filament.id}>
                    {filament.manufacturer} · {filament.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="palette-row-actions">
              <button
                type="button"
                title="Sample a screen color"
                aria-label={`Sample screen color for ${color.name}`}
                onClick={() => void sampleColor(color)}
              >
                ◉
              </button>
              <button
                type="button"
                aria-label={`Move ${color.name} up`}
                disabled={index === 0}
                onClick={() => reorder(index, index - 1)}
              >
                ↑
              </button>
              <button
                type="button"
                aria-label={`Move ${color.name} down`}
                disabled={index === config.palette.colors.length - 1}
                onClick={() => reorder(index, index + 1)}
              >
                ↓
              </button>
              <button
                className={color.locked ? 'is-locked' : ''}
                type="button"
                aria-label={`${color.locked ? 'Unlock' : 'Lock'} ${color.name}`}
                aria-pressed={color.locked}
                onClick={() => commitColor(color, { locked: !color.locked }, 'lock')}
              >
                {color.locked ? '◆' : '◇'}
              </button>
            </div>
          </li>
        ))}
      </ol>
      <p className="palette-keyboard-hint">Drag rows, use the arrow buttons, or press Alt + ↑/↓ to reorder.</p>
    </section>
  )
}
