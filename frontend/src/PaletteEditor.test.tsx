import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PaletteEditor } from './PaletteEditor'
import type { JobConfigV1 } from './contracts'

const config: JobConfigV1 = {
  schema_version: 1,
  source_asset_id: 'asset_1',
  canvas: { width_mm: 200, height_mm: 200 },
  crop: { mode: 'cover', x: 0, y: 0, width: 1, height: 1 },
  printer: {
    profile_catalog_id: 'image23mf-bundled-printers',
    profile_catalog_version: '2026.07.16',
    printer_id: 'bambu-p2s',
    nozzle_id: 'nozzle-0.4-hardened-steel',
    nozzle_mm: 0.4,
    layer_height_mm: 0.2,
    plate_id: 'textured-pei',
  },
  palette: {
    colors: [
      { id: 'light', name: 'Light', hex: '#CBC6B8', locked: true, filament_id: null },
      { id: 'dark', name: 'Dark', hex: '#000000', locked: false, filament_id: null },
    ],
  },
  cleanup: {
    min_island_mm2: 0.3,
    max_hole_mm2: 0.5,
    smoothing_radius_mm: 0,
    merge_policy: 'review',
    preserve_long_lines: true,
  },
  geometry: {
    style: 'flush_inlay',
    base_thickness_mm: 1.2,
    art_thickness_mm: 0.6,
    corner_radius_mm: 0,
  },
}

const filament = {
  id: 'filament_bone',
  manufacturer: 'Bambu Lab',
  family: 'Matte',
  name: 'Bone White',
  hex_color: '#CBC6B8',
  material: 'PLA',
  finish: 'Matte',
  owned: true,
  metadata: {},
  created_at: '2026-07-16T00:00:00Z',
  updated_at: '2026-07-16T00:00:00Z',
}

function installFetch() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    if (url === '/api/filaments?owned=true') {
      return new Response(JSON.stringify({ items: [filament], total: 1 }), { status: 200 })
    }
    if (url === '/api/projects/project_1/palette/auto') {
      expect(init?.body).toBe(JSON.stringify({ config, color_count: 3 }))
      return new Response(
        JSON.stringify({
          colors: ['#CBC6B8', '#123456', '#ABCDEF'],
          iterations: 4,
          converged: true,
          sample_size: 4096,
          visible_pixel_count: 10000,
          unique_color_count: 3,
          options_fingerprint: 'a'.repeat(64),
        }),
        { status: 200 },
      )
    }
    throw new Error(`Unexpected fetch: ${url}`)
  })
}

function renderEditor() {
  const onCommit = vi.fn()
  render(
    <PaletteEditor
      config={config}
      persistence="saved"
      projectId="project_1"
      onCommit={onCommit}
    />,
  )
  return { onCommit }
}

beforeEach(() => {
  installFetch()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Palette editor', () => {
  it('shows stable indices and commits rename, lock, and keyboard reorder operations', async () => {
    const { onCommit } = renderEditor()
    await screen.findAllByRole('option', { name: 'Bambu Lab · Bone White' })

    expect(screen.getByLabelText('Color index 1')).toHaveTextContent('01')
    const name = screen.getByLabelText('Name for color 1')
    fireEvent.change(name, { target: { value: 'Warm cream' } })
    fireEvent.blur(name)
    expect(onCommit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        palette: { colors: expect.arrayContaining([expect.objectContaining({ name: 'Warm cream' })]) },
      }),
      'rename',
      'light',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Unlock Light' }))
    expect(onCommit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        palette: { colors: expect.arrayContaining([expect.objectContaining({ locked: false })]) },
      }),
      'lock',
      'light',
    )

    fireEvent.keyDown(screen.getByLabelText('Name for color 2'), {
      key: 'ArrowUp',
      altKey: true,
    })
    expect(onCommit).toHaveBeenLastCalledWith(
      expect.objectContaining({ palette: { colors: [config.palette.colors[1], config.palette.colors[0]] } }),
      'reorder',
      'dark',
    )
  })

  it('maps owned filament and samples a Chromium eyedropper color', async () => {
    const { onCommit } = renderEditor()
    const [option] = await screen.findAllByRole('option', { name: 'Bambu Lab · Bone White' })
    fireEvent.change(screen.getByLabelText('Owned filament for Dark'), {
      target: { value: option.getAttribute('value') },
    })
    expect(onCommit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        palette: {
          colors: expect.arrayContaining([
            expect.objectContaining({
              filament_id: 'filament_bone',
              name: 'Bone White',
              hex: '#CBC6B8',
            }),
          ]),
        },
      }),
      'select-filament',
      'dark',
    )

    vi.stubGlobal(
      'EyeDropper',
      class {
        open = vi.fn(async () => ({ sRGBHex: '#12ab34' }))
      },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Sample screen color for Dark' }))
    await waitFor(() =>
      expect(onCommit).toHaveBeenLastCalledWith(
        expect.objectContaining({
          palette: { colors: expect.arrayContaining([expect.objectContaining({ hex: '#12AB34' })]) },
        }),
        'sample',
        'dark',
      ),
    )
  })

  it('groups continuous native color-picker updates into one semantic commit', () => {
    const { onCommit } = renderEditor()
    const picker = screen.getByLabelText('Choose Light color')

    fireEvent.input(picker, { target: { value: '#AA0000' } })
    fireEvent.input(picker, { target: { value: '#00AA00' } })
    fireEvent.change(picker, { target: { value: '#0011AA' } })
    expect(onCommit).not.toHaveBeenCalled()

    fireEvent.blur(picker)
    expect(onCommit).toHaveBeenCalledTimes(1)
    expect(onCommit).toHaveBeenCalledWith(
      expect.objectContaining({
        palette: { colors: expect.arrayContaining([expect.objectContaining({ hex: '#0011AA' })]) },
      }),
      'replace',
      'light',
    )
  })

  it('auto-fits only unlocked slots through the unified commit pathway', async () => {
    const { onCommit } = renderEditor()
    await screen.findAllByRole('option', { name: 'Bambu Lab · Bone White' })
    fireEvent.change(screen.getByLabelText('Automatic palette color count'), {
      target: { value: '3' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Auto-fit unlocked' }))

    await waitFor(() =>
      expect(onCommit).toHaveBeenCalledWith(
        expect.objectContaining({
          palette: {
            colors: [
              expect.objectContaining({ id: 'light', hex: '#CBC6B8', locked: true }),
              expect.objectContaining({ id: 'dark', hex: '#123456', locked: false }),
              expect.objectContaining({ id: 'color-3', hex: '#ABCDEF', locked: false }),
            ],
          },
        }),
        'auto-fit',
        null,
        'automatic',
      ),
    )
  })
})
