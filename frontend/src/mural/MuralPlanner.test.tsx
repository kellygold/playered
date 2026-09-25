import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MuralPlanner } from './MuralPlanner'
import type {
  MuralLayout,
  MuralPlanPreview,
  MuralPlanResource,
  MuralPlanSettings,
  MuralTilePlan,
} from './types'

const baseLayout: MuralLayout = {
  rows: 2,
  columns: 3,
  panel_width_mm: 200,
  panel_height_mm: 200,
  horizontal_gap_mm: 0,
  vertical_gap_mm: 0,
  bleed_mm: 0,
  orientation: 'auto',
}

function tile(index: number, layout: MuralLayout, fits: boolean): MuralTilePlan {
  const row = Math.floor(index / layout.columns)
  const column = index % layout.columns
  return {
    id: `tile-r${String(row + 1).padStart(2, '0')}-c${String(column + 1).padStart(2, '0')}`,
    row: row + 1,
    column: column + 1,
    build_plate_index: index + 1,
    normalized_master_bounds: {
      x: column / layout.columns,
      y: row / layout.rows,
      width: 1 / layout.columns,
      height: 1 / layout.rows,
    },
    master_bounds_mm: {
      x: column * layout.panel_width_mm,
      y: row * layout.panel_height_mm,
      width: layout.panel_width_mm,
      height: layout.panel_height_mm,
    },
    master_pixel_bounds: { x_start: column, y_start: row, x_end: column + 1, y_end: row + 1 },
    sampled_master_bounds_mm: {
      x: column * layout.panel_width_mm,
      y: row * layout.panel_height_mm,
      width: layout.panel_width_mm,
      height: layout.panel_height_mm,
    },
    sampled_master_pixel_bounds: { x_start: column, y_start: row, x_end: column + 1, y_end: row + 1 },
    output_size_mm: {
      width: layout.panel_width_mm + layout.bleed_mm * 2,
      height: layout.panel_height_mm + layout.bleed_mm * 2,
    },
    outside_master_padding: { top_mm: 0, right_mm: 0, bottom_mm: 0, left_mm: 0 },
    bed_fit: {
      fits,
      rotation_degrees: 0,
      placed_width_mm: layout.panel_width_mm,
      placed_height_mm: layout.panel_height_mm,
      origin_x_mm: fits ? 0 : null,
      origin_y_mm: fits ? 0 : null,
      reason: fits ? null : 'Reduce panel size.',
    },
  }
}

function preview(layout: MuralLayout = baseLayout, fits = true): MuralPlanPreview {
  const masterWidth = layout.columns * layout.panel_width_mm
  const masterHeight = layout.rows * layout.panel_height_mm
  return {
    request: {
      schema_version: 1,
      source: {
        source_asset_id: 'asset_1',
        source_asset_sha256: 'a'.repeat(64),
        processed_artifact_id: 'artifact_1',
        processed_artifact_sha256: 'b'.repeat(64),
        processed_size: { width: 1200, height: 800 },
        canonical_transform: {},
        config_sha256: 'c'.repeat(64),
        engine_version: '0.1.0',
        revision_id: null,
        draft_generation: 2,
      },
      layout,
      bed: {
        printer_id: 'bambu-p2s',
        plate_id: 'textured-pei',
        profile_catalog_fingerprint: 'd'.repeat(64),
        width_mm: 256,
        height_mm: 256,
        edge_clearance_mm: 0,
        excluded_rectangles: [],
      },
      reserved_rectangles: [],
    },
    plan: {
      schema_version: 1,
      request_fingerprint: 'e'.repeat(64),
      source_fingerprint: 'f'.repeat(64),
      master_transform: {},
      master_size_mm: { width: masterWidth, height: masterHeight },
      assembled_size_mm: {
        width: masterWidth + (layout.columns - 1) * layout.horizontal_gap_mm,
        height: masterHeight + (layout.rows - 1) * layout.vertical_gap_mm,
      },
      tiles: Array.from(
        { length: layout.rows * layout.columns },
        (_, index) => tile(index, layout, fits),
      ),
      all_tiles_fit: fits,
      warnings: fits ? [] : ['Tile does not fit; reduce panel size or bleed.'],
    },
  }
}

function resource(
  layout: MuralLayout = baseLayout,
  freshness: 'current' | 'stale' = 'current',
): MuralPlanResource {
  return {
    ...preview(layout),
    project_id: 'project_1',
    schema_version: 1,
    generation: 3,
    freshness,
    stale_reason: freshness === 'stale' ? 'mural draft generation is stale' : null,
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:01:00Z',
  }
}

function response(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function installFetch(
  saved: MuralPlanResource | null = null,
  fits = true,
  conflictOnSave = false,
) {
  const requests: { url: string; method: string; body: unknown }[] = []
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null
    requests.push({ url, method, body })
    if (method === 'GET') {
      return saved
        ? response(saved)
        : response({ error: { code: 'not_found', message: 'Missing' } }, 404)
    }
    if (method === 'POST' && url.endsWith('/preview')) {
      return response(preview((body as MuralPlanSettings).layout, fits))
    }
    if (method === 'PUT') {
      if (conflictOnSave) {
        return response({
          error: {
            code: 'generation_conflict',
            message: 'Generation conflict',
            details: { current_generation: saved?.generation ?? 0 },
          },
        }, 409)
      }
      const settings = body as MuralPlanSettings
      return response({
        ...resource(settings.layout),
        generation: saved?.generation ? saved.generation + 1 : 1,
      })
    }
    if (method === 'DELETE') return response(null, 204)
    throw new Error(`Unexpected request: ${method} ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  return { fetch, requests }
}

beforeEach(() => vi.restoreAllMocks())
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('MuralPlanner', () => {
  it('previews one continuous 3x2 master, changes the grid, and saves generation one', async () => {
    const { requests } = installFetch()
    const { container } = render(
      <MuralPlanner
        projectId="project_1"
        processedArtifactId="artifact_1"
        masterImageUrl="/api/artifacts/artifact_1"
      />,
    )

    expect((await screen.findAllByText('600 × 400 mm')).length).toBeGreaterThan(0)
    expect(screen.getByText('3 × 2 · 6 plates')).toBeInTheDocument()
    expect(container.querySelectorAll('.mural-tile-grid span')).toHaveLength(6)
    expect(screen.getByAltText('Processed master artwork')).toHaveAttribute(
      'src',
      '/api/artifacts/artifact_1',
    )
    expect(screen.getByText('Fits · native')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Mural columns'), { target: { value: '4' } })
    expect((await screen.findAllByText('800 × 400 mm')).length).toBeGreaterThan(0)
    expect(container.querySelectorAll('.mural-tile-grid span')).toHaveLength(8)

    fireEvent.click(screen.getByRole('button', { name: 'Save mural plan' }))
    expect(await screen.findByText('Saved · v1')).toBeInTheDocument()
    const save = requests.find((item) => item.method === 'PUT')
    expect(save?.body).toMatchObject({
      expected_generation: 0,
      processed_artifact_id: 'artifact_1',
      layout: { columns: 4, rows: 2 },
    })
  })

  it('hydrates a stale saved plan and keeps every physical control editable', async () => {
    const layout: MuralLayout = {
      ...baseLayout,
      rows: 3,
      columns: 2,
      panel_width_mm: 180,
      panel_height_mm: 160,
      horizontal_gap_mm: 8,
      vertical_gap_mm: 12,
      bleed_mm: 2,
      orientation: 'rotate_90',
    }
    installFetch(resource(layout, 'stale'))
    render(
      <MuralPlanner
        projectId="project_1"
        processedArtifactId="artifact_1"
        masterImageUrl="/master.png"
      />,
    )

    expect(await screen.findByText('Saved plan stale')).toBeInTheDocument()
    expect(screen.getByLabelText('Mural columns')).toHaveValue(2)
    expect(screen.getByLabelText('Mural rows')).toHaveValue(3)
    expect(screen.getByLabelText('Panel width millimetres')).toHaveValue(180)
    expect(screen.getByLabelText('Panel height millimetres')).toHaveValue(160)
    expect(screen.getByLabelText('Build-plate orientation')).toHaveValue('rotate_90')
    fireEvent.click(screen.getByText('Assembly spacing and bleed'))
    expect(screen.getByLabelText('Horizontal gap millimetres')).toHaveValue(8)
    expect(screen.getByLabelText('Vertical gap millimetres')).toHaveValue(12)
    expect(screen.getByLabelText('Bleed millimetres')).toHaveValue(2)
    expect(screen.getByRole('button', { name: 'Update mural plan' })).toBeEnabled()
  })

  it('shows impossible fit as an actionable preview instead of treating it as a request error', async () => {
    installFetch(null, false)
    render(
      <MuralPlanner
        projectId="project_1"
        processedArtifactId="artifact_1"
        masterImageUrl="/master.png"
      />,
    )

    expect(await screen.findByText('Does not fit')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('reduce panel size or bleed')
    expect(screen.getByRole('button', { name: 'Save mural plan' })).toBeEnabled()
  })

  it('distinguishes saved state from unsaved edits and restores a save conflict', async () => {
    installFetch(resource(), true, true)
    render(
      <MuralPlanner
        projectId="project_1"
        processedArtifactId="artifact_1"
        masterImageUrl="/master.png"
      />,
    )

    expect(await screen.findByText('Saved · v3')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update mural plan' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Mural columns'), { target: { value: '4' } })
    expect(await screen.findByText('Unsaved changes')).toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Update mural plan' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    expect(await screen.findByText('Save conflict')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Reload it before saving again')
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved plan' }))
    expect(await screen.findByText('Saved · v3')).toBeInTheDocument()
    expect(screen.getByLabelText('Mural columns')).toHaveValue(3)
  })

  it('edits accessible reserved areas and previews the selected printer envelope', async () => {
    const { requests } = installFetch()
    render(
      <MuralPlanner
        projectId="project_1"
        processedArtifactId="artifact_1"
        masterImageUrl="/master.png"
      />,
    )

    expect(await screen.findByText('bambu-p2s · textured-pei')).toBeInTheDocument()
    expect(screen.getByText('256 × 256 mm bed · 0 mm edge clearance')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Reserved plate areas · 0'))
    fireEvent.click(screen.getByRole('button', { name: 'Add reserved area' }))
    expect(screen.getByLabelText('Reservation 1 Width millimetres')).toHaveValue(35)
    fireEvent.change(screen.getByLabelText('Reservation 1 X millimetres'), {
      target: { value: '200' },
    })

    await waitFor(() => {
      const latestPreview = requests.filter((item) => item.method === 'POST').at(-1)
      expect(latestPreview?.body).toMatchObject({
        reserved_rectangles: [{ x_mm: 200, width_mm: 35, height_mm: 35 }],
      })
    })
    expect(screen.getByRole('img', { name: /1 reserved areas on 256 by 256/ })).toBeInTheDocument()
  })

  it('locks inputs while a save is in flight and does not apply a delayed response after unmount', async () => {
    const deferred: { resolve?: (value: Response) => void } = {}
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (method === 'GET') {
        return response({ error: { code: 'not_found', message: 'Missing' } }, 404)
      }
      if (method === 'POST') return response(preview())
      if (method === 'PUT') {
        return new Promise<Response>((resolve) => {
          deferred.resolve = resolve
        })
      }
      throw new Error(`Unexpected request ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetch)
    const { unmount } = render(
      <MuralPlanner
        projectId="project_1"
        processedArtifactId="artifact_1"
        masterImageUrl="/master.png"
      />,
    )

    const save = await screen.findByRole('button', { name: 'Save mural plan' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    expect(screen.getByLabelText('Mural columns')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Saving plan…' })).toBeDisabled()
    unmount()
    deferred.resolve?.(response(resource()))
  })

  it('keeps short-viewport controls scroll-owned by the editor rail', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/mural/MuralPlanner.css'), 'utf8')
    expect(css).toMatch(/@media \(max-width: 540px\), \(max-height: 560px\)/)
    expect(css).toMatch(/\.mural-control-grid,[\s\S]*?grid-template-columns: minmax\(0, 1fr\)/)
    expect(css).not.toMatch(/position:\s*fixed/)
  })
})
