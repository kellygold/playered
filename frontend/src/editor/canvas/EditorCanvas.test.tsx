/// <reference types="node" />

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EditorCanvas } from './EditorCanvas'
import type { RegionGraph, RiskReport } from '../../contracts'
import type { CanonicalTransform } from '../../contracts'
import type { RegionAssignmentMap } from '../inspector/regionAssignment'
import { emptyCanvasSelection } from '../selection'
import type { RiskMaskMap } from './riskMask'
import {
  createEditorCanvasSourceCatalog,
  type CanvasArtifactLike,
  type EditorCanvasSourceCatalog,
} from './canvasModel'

const artifact = (kind: string): CanvasArtifactLike => ({
  kind,
  download_url: `/api/artifacts/${kind}`,
  sha256: kind.padEnd(64, '0').slice(0, 64),
  media_type: 'image/png',
})

const space = { widthPx: 1000, heightPx: 500, widthMm: 200, heightMm: 100 }
const transform: CanonicalTransform = {
  schema_version: 1,
  original_size: { width: 1000, height: 500 },
  normalized_size: { width: 1000, height: 500 },
  exif_orientation: 1,
  crop_rect: { x: 0, y: 0, width: 1000, height: 500 },
  fit_mode: 'stretch',
  working_size: { width: 1000, height: 500 },
  canvas_size: { width: 200, height: 100 },
}

function catalog(): EditorCanvasSourceCatalog {
  return createEditorCanvasSourceCatalog({
    artifacts: [
      artifact('preview-image'),
      artifact('quantized-preview-image'),
      artifact('palette-preview-image'),
      artifact('editor-changed-mask-preview-image'),
    ],
    space,
    riskReport: {
      warnings: [
        {
          id: 'risk-1',
          severity: 'warning',
          title: 'Small island',
          affected_bounds: [{ x_mm: 10, y_mm: 20, width_mm: 5, height_mm: 4 }],
        },
      ],
    },
  })
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('EditorCanvas', () => {
  it('renders the authoritative processed source with nearest-neighbor sampling and honest availability', () => {
    render(<EditorCanvas catalog={catalog()} viewportSize={{ width: 1000, height: 600 }} />)

    const image = screen.getByRole('img', { name: 'Processed canvas view' })
    expect(image).toHaveAttribute('data-artifact-kind', 'palette-preview-image')
    expect(image).toHaveAttribute('data-interpolation', 'nearest')
    expect(screen.getByRole('button', { name: 'Processed' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    const geometry = screen.getByRole('button', { name: 'Geometry' })
    const sliced = screen.getByRole('button', { name: 'Sliced' })
    expect(geometry).toHaveAttribute('aria-disabled', 'true')
    expect(sliced).toHaveAttribute('aria-disabled', 'true')
    expect(geometry).toHaveAccessibleDescription(/Build a 3MF.*Geometry view/)
    expect(sliced).toHaveAccessibleDescription(/Build a new 3MF to see its sliced paths/)
    const readiness = document.querySelector('.i23-canvas-output-readiness')
    expect(readiness).toHaveTextContent(
      'From preview to print. Build, validate, and download the 3MF from Output readiness. Geometry and Sliced views appear after the build. Compare solid artwork with extrusion paths; thin details can leave gaps that expose the backing.',
    )
    expect(readiness).not.toHaveAttribute('role')
    fireEvent.click(geometry)
    expect(screen.getByRole('img', { name: 'Processed canvas view' })).toBeInTheDocument()
  })

  it('explains the selected view and why later stages are unavailable without replacing the image', () => {
    render(<EditorCanvas catalog={catalog()} viewportSize={{ width: 1000, height: 600 }} />)
    const helper = document.querySelector('.i23-canvas-view-context > p')!
    expect(helper).toHaveTextContent('after cleanup and local edits')
    fireEvent.click(screen.getByRole('button', { name: 'Quantized' }))
    expect(helper).toHaveTextContent('before cleanup')
    fireEvent.click(screen.getByRole('button', { name: 'Sliced' }))
    expect(document.querySelector('.i23-canvas-unavailable-hint')).toHaveTextContent('inspect the exported project in Bambu Studio')
    expect(screen.getByRole('img', { name: 'Quantized canvas view' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Original' }))
    expect(document.querySelector('.i23-canvas-unavailable-hint')).not.toBeInTheDocument()
  })

  it('offers a working fit recovery when artwork is panned completely out of view', () => {
    const onPick = vi.fn()
    render(<EditorCanvas catalog={catalog()} viewportSize={{ width: 1000, height: 600 }} defaultCamera={{ zoom: 7.18, panX: 10_000, panY: 0 }} onPick={onPick} />)
    const recovery = screen.getByRole('button', { name: 'Bring artwork into view' })
    fireEvent.keyDown(recovery, { key: 'Enter' })
    expect(onPick).not.toHaveBeenCalled()
    fireEvent.click(recovery)
    expect(screen.queryByText('The artwork is outside this view')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Zoom level')).toHaveTextContent('100%')
    expect(onPick).not.toHaveBeenCalled()
  })

  it('explains comparison modes without changing the image or losing the reference', () => {
    render(<EditorCanvas catalog={catalog()} viewportSize={{ width: 1000, height: 600 }} />)
    expect(screen.getByRole('group', { name: 'Compare views' })).toHaveAccessibleDescription(/Show one view/)
    fireEvent.click(screen.getByRole('button', { name: 'Split' }))
    expect(screen.getByRole('group', { name: 'Compare views' })).toHaveAccessibleDescription(/reference is on the left/)
    fireEvent.click(screen.getByRole('button', { name: 'Single' }))
    expect(screen.getByRole('combobox', { name: 'Reference' })).toHaveValue('original')
    expect(screen.getByRole('img', { name: 'Processed canvas view' })).toBeInTheDocument()
  })

  it('switches among exact pixel stages and converts physical risk bounds to raster coordinates', () => {
    render(<EditorCanvas catalog={catalog()} viewportSize={{ width: 1000, height: 600 }} selectedRiskId="risk-1" />)

    fireEvent.click(screen.getByRole('button', { name: 'Quantized' }))
    expect(screen.getByRole('img', { name: 'Quantized canvas view' })).toHaveAttribute(
      'data-artifact-kind',
      'quantized-preview-image',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Risk' }))
    expect(screen.getByRole('img', { name: 'Print risks canvas view' })).toHaveAttribute(
      'data-artifact-kind',
      'palette-preview-image',
    )
    const risk = document.querySelector('[data-risk-id="risk-1"]')
    expect(risk).toHaveAttribute('x', '50')
    expect(risk).toHaveAttribute('y', '100')
    expect(risk).toHaveAttribute('width', '25')
    expect(risk).toHaveAttribute('height', '20')
  })

  it('keeps the raster and SVG overlays on one exact border-free coordinate plane', () => {
    render(<EditorCanvas catalog={catalog()} viewportSize={{ width: 1000, height: 600 }} defaultView="risk" selectedRiskId="risk-1" />)

    const plane = document.querySelector<HTMLElement>('[data-plane="primary"]')!
    const image = screen.getByRole('img', { name: 'Print risks canvas view' })
    const overlay = document.querySelector<SVGSVGElement>('.i23-canvas-risks')!
    expect(plane).toHaveStyle({ width: '1000px', height: '500px' })
    expect(image).toHaveAttribute('width', '1000')
    expect(image).toHaveAttribute('height', '500')
    expect(overlay).toHaveAttribute('viewBox', '0 0 1000 500')
    expect(overlay).toHaveAttribute('preserveAspectRatio', 'none')
    expect(overlay).toHaveAccessibleName(/Approximate affected-area bounds.*not an exact pixel mask/)

    const css = readFileSync(resolve(process.cwd(), 'src/editor/canvas/EditorCanvas.css'), 'utf8')
    expect(css).toMatch(/\.i23-canvas-plane\s*\{[^}]*border:\s*0;/)
    expect(css).toMatch(/\.i23-canvas-plane\s*\{[^}]*outline:\s*1px/)
    expect(css).toMatch(/\.i23-editor-canvas-viewer \.i23-canvas-plane img\s*\{[^}]*width:\s*100%;[^}]*height:\s*100%;[^}]*max-width:\s*none;[^}]*max-height:\s*none;[^}]*object-fit:\s*fill;/)
    expect(css).toMatch(/\.i23-canvas-risks rect\[data-selected="true"\]\s*\{[^}]*stroke:\s*#fff;[^}]*stroke-width:\s*4px;[^}]*stroke-dasharray:\s*9 4;/)
    expect(css).toMatch(/\.i23-canvas-view-switcher button,[\s\S]*?min-width:\s*44px;[\s\S]*?min-height:\s*44px;/)
    expect(css).toMatch(/\.i23-canvas-compare-bar select\s*\{[^}]*min-height:\s*44px;/)
  })

  it('keeps the exact raster risk mask on the identical source plane at Fit and zoom', () => {
    const context = {
      clearRect: vi.fn(),
      createImageData: vi.fn((width: number, height: number) => ({ data: new Uint8ClampedArray(width * height * 4) })),
      putImageData: vi.fn(),
      save: vi.fn(),
      restore: vi.fn(),
      setLineDash: vi.fn(),
      strokeRect: vi.fn(),
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    const graph = {
      schema_version: 1,
      width_px: 1000,
      height_px: 500,
      width_mm: 200,
      height_mm: 100,
      pixel_width_mm: 0.2,
      pixel_height_mm: 0.2,
      active_pixel_count: 500_000,
      palette: [],
      regions: [],
      adjacency: [],
    } satisfies RegionGraph
    const report = {
      schema_version: 1,
      graph_fingerprint: 'a'.repeat(64),
      options_fingerprint: 'b'.repeat(64),
      nozzle_mm: 0.4,
      evaluated_codes: [],
      pending_codes: [],
      warnings: [],
      summary: { total: 0, info: 0, warning: 0, error: 0, by_code: [] },
    } satisfies RiskReport
    const assignment: RegionAssignmentMap = {
      width: 1000,
      height: 500,
      graphFingerprint: report.graph_fingerprint,
      regionIds: [],
      indices: new Int32Array(500_000).fill(-1),
    }
    const mask: RiskMaskMap = {
      width: 1000,
      height: 500,
      encoding: 'uint8-risk-severity-bitset-row-major',
      severityBits: { info: 1, warning: 2, error: 4 },
      exactWarningCount: 0,
      approximateWarningCount: 0,
      sourceSha256: 'c'.repeat(64),
      configFingerprint: 'd'.repeat(64),
      operationsFingerprint: 'e'.repeat(64),
      assignmentSha256: 'f'.repeat(64),
      graphFingerprint: report.graph_fingerprint,
      riskReportFingerprint: '1'.repeat(64),
      pixels: new Uint8Array(500_000),
    }
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        defaultView="risk"
        regionAssignment={assignment}
        riskMask={mask}
        riskGraph={graph}
        riskReport={report}
      />,
    )
    const plane = document.querySelector<HTMLElement>('[data-plane="primary"]')!
    const overlay = screen.getByRole('img', { name: /Exact risk overview|Select a warning/ }) as HTMLCanvasElement
    expect(overlay.parentElement).toBe(plane)
    expect(overlay.width).toBe(1000)
    expect(overlay.height).toBe(500)
    expect(plane).toHaveStyle({ transform: 'translate3d(24px, 62px, 0) scale(0.952)' })
    fireEvent.click(screen.getByRole('button', { name: 'Overview' }))
    expect(overlay).toHaveAttribute('data-mode', 'overview')
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(plane).toHaveStyle({ transform: 'translate3d(-95px, 2.5px, 0) scale(1.19)' })
    expect(overlay.parentElement).toBe(plane)
    expect(overlay.width).toBe(1000)
    expect(overlay.height).toBe(500)
  })

  it('discloses exact and approximate finding counts and explains an approximate selection', () => {
    const context = {
      clearRect: vi.fn(),
      createImageData: vi.fn((width: number, height: number) => ({ data: new Uint8ClampedArray(width * height * 4) })),
      putImageData: vi.fn(),
      save: vi.fn(),
      restore: vi.fn(),
      setLineDash: vi.fn(),
      strokeRect: vi.fn(),
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    const graph = {
      schema_version: 1,
      width_px: 1000,
      height_px: 500,
      width_mm: 200,
      height_mm: 100,
      pixel_width_mm: 0.2,
      pixel_height_mm: 0.2,
      active_pixel_count: 500_000,
      palette: [],
      regions: [],
      adjacency: [],
    } satisfies RegionGraph
    const report = {
      schema_version: 1,
      graph_fingerprint: 'a'.repeat(64),
      options_fingerprint: 'b'.repeat(64),
      nozzle_mm: 0.4,
      evaluated_codes: ['small_island'],
      pending_codes: [],
      warnings: [{
        id: 'risk-1',
        code: 'small_island',
        feature_key: 'risk-1',
        severity: 'warning',
        title: 'Approximate gap',
        explanation: 'Only physical bounds are available.',
        affected_region_ids: [],
        affected_labels: [],
        affected_bounds: [{ x_mm: 10, y_mm: 20, width_mm: 5, height_mm: 4 }],
        measurements: [{ key: 'area', role: 'measured', value: 1, unit: 'mm2' }],
        suggestions: [{
          kind: 'review',
          title: 'Review',
          explanation: 'Review the bounds.',
          destructive: false,
          requires_confirmation: false,
        }],
        classifier_id: 'test',
        classifier_version: '1',
      }],
      summary: {
        total: 1,
        info: 0,
        warning: 1,
        error: 0,
        by_code: [{ code: 'small_island', count: 1 }],
      },
    } satisfies RiskReport
    const assignment: RegionAssignmentMap = {
      width: 1000,
      height: 500,
      graphFingerprint: report.graph_fingerprint,
      regionIds: [],
      indices: new Int32Array(500_000).fill(-1),
    }
    const mask: RiskMaskMap = {
      width: 1000,
      height: 500,
      encoding: 'uint8-risk-severity-bitset-row-major',
      severityBits: { info: 1, warning: 2, error: 4 },
      exactWarningCount: 2,
      approximateWarningCount: 1,
      sourceSha256: 'c'.repeat(64),
      configFingerprint: 'd'.repeat(64),
      operationsFingerprint: 'e'.repeat(64),
      assignmentSha256: 'f'.repeat(64),
      graphFingerprint: report.graph_fingerprint,
      riskReportFingerprint: '1'.repeat(64),
      pixels: new Uint8Array(500_000),
    }

    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        defaultView="risk"
        selectedRiskId="risk-1"
        regionAssignment={assignment}
        riskMask={mask}
        riskGraph={graph}
        riskReport={report}
      />,
    )

    const legend = document.querySelector('.i23-canvas-risk-legend')
    expect(legend).not.toBeVisible()
    fireEvent.click(screen.getByText('How to read the overlay'))
    expect(legend).toBeVisible()
    expect(legend).toHaveTextContent('2 region-backed findings appear in Overview and Severity.')
    expect(legend).toHaveTextContent('1 bounds-only finding is excluded from those exact mask modes')
    expect(legend).toHaveTextContent(
      'Approximate gap uses approximate dashed bounds because no exact region membership is available.',
    )
    expect(screen.getByRole('img', { name: /Approximate dashed bounds/ })).toHaveAttribute(
      'data-presentation',
      'approximate',
    )
  })

  it('does not paint a warning storm and renders only the explicitly selected finding', () => {
    const sourceCatalog = catalog()
    sourceCatalog.sources.risk!.risks = Array.from({ length: 1_500 }, (_, index) => ({
      id: `risk-${index}`,
      severity: 'warning' as const,
      title: `Finding ${index}`,
      presentation: 'feature-bounds' as const,
      affectedRegionCount: 0,
      boundsMm: [{ xMm: index % 190, yMm: index % 90, widthMm: 5, heightMm: 4 }],
      boundsPx: [],
    }))
    const { rerender } = render(
      <EditorCanvas catalog={sourceCatalog} viewportSize={{ width: 1000, height: 600 }} defaultView="risk" />,
    )

    expect(document.querySelectorAll('.i23-canvas-risks rect')).toHaveLength(0)
    const guidance = document.querySelector('.i23-canvas-risk-guidance')
    expect(guidance).toHaveTextContent(
      'No warning boxes are drawn until one is selected.',
    )
    expect(guidance).not.toHaveAttribute('role')

    rerender(
      <EditorCanvas
        catalog={sourceCatalog}
        viewportSize={{ width: 1000, height: 600 }}
        defaultView="risk"
        selectedRiskId="risk-1499"
      />,
    )
    expect(document.querySelectorAll('.i23-canvas-risks rect')).toHaveLength(1)
    expect(document.querySelector('[data-risk-id="risk-1499"]')).toBeInTheDocument()
    expect(document.querySelector('[data-risk-id="risk-1"]')).not.toBeInTheDocument()
  })

  it('suppresses a selected fragmentation aggregate that has no honest spatial overlay', () => {
    const sourceCatalog = catalog()
    sourceCatalog.sources.risk!.risks = [{
      id: 'aggregate',
      severity: 'warning',
      title: 'Fragmented artwork',
      presentation: 'list-only',
      affectedRegionCount: 320,
      boundsMm: [],
      boundsPx: [],
    }]
    render(
      <EditorCanvas
        catalog={sourceCatalog}
        viewportSize={{ width: 1000, height: 600 }}
        defaultView="risk"
        selectedRiskId="aggregate"
      />,
    )

    expect(document.querySelector('.i23-canvas-risk-guidance')).toHaveTextContent(
      'Canvas rectangles are intentionally hidden',
    )
    expect(document.querySelectorAll('.i23-canvas-risks rect')).toHaveLength(0)
    expect(document.querySelector('.i23-canvas-risk-key')).not.toBeInTheDocument()
  })

  it('keeps dense 1024-square fragmentation extents registered at Fit and zoom', () => {
    const regionIds = Array.from({ length: 96 }, (_, index) => `region-${index}`)
    const regions = regionIds.map((id, index) => ({
      id,
      pixel_bounds: {
        x: 3 + (index % 12) * 80,
        y: 5 + Math.floor(index / 12) * 110,
        width: 2,
        height: 3,
      },
    }))
    const sourceCatalog = createEditorCanvasSourceCatalog({
      artifacts: [artifact('palette-preview-image')],
      space: { widthPx: 1024, heightPx: 1024, widthMm: 200, heightMm: 200 },
      regionGraph: { width_px: 1024, height_px: 1024, regions },
      riskReport: {
        warnings: [{
          id: 'fragmentation',
          code: 'excess_fragmentation',
          severity: 'warning',
          title: '96 separate regions',
          affected_region_ids: regionIds,
          affected_bounds: [{ x_mm: 0, y_mm: 0, width_mm: 200, height_mm: 200 }],
        }],
      },
    })
    render(
      <EditorCanvas
        catalog={sourceCatalog}
        viewportSize={{ width: 1000, height: 600 }}
        defaultView="risk"
        selectedRiskId="fragmentation"
      />,
    )

    const plane = document.querySelector<HTMLElement>('[data-plane="primary"]')!
    const overlay = document.querySelector<SVGSVGElement>('.i23-canvas-risks')!
    const extents = [...document.querySelectorAll<SVGRectElement>('.i23-canvas-risks rect')]
    expect(extents).toHaveLength(96)
    expect(extents[0]).toHaveAttribute('x', '3')
    expect(extents[0]).toHaveAttribute('y', '5')
    expect(extents[0]).toHaveAttribute('width', '2')
    expect(extents[0]).toHaveAttribute('height', '3')
    expect(overlay).toHaveAttribute('viewBox', '0 0 1024 1024')
    expect(overlay).toHaveAttribute('data-presentation', 'region-extents')
    expect(plane).toHaveStyle({
      width: '1024px',
      height: '1024px',
      transform: 'translate3d(224px, 24px, 0) scale(0.5390625)',
    })
    expect(screen.getByText('Dashed boxes · per-region extents, not masks')).toBeInTheDocument()
    expect(document.querySelector('.i23-canvas-risk-guidance')).toHaveTextContent(
      'Each dashed rectangle marks a region’s exact outer bounds, not its pixel shape or a mask.',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(plane).toHaveStyle({
      transform: 'translate3d(155px, -45px, 0) scale(0.673828125)',
    })
    expect(overlay.parentElement).toBe(plane)
    fireEvent.click(screen.getByRole('button', { name: 'Fit artwork to canvas' }))
    expect(plane).toHaveStyle({
      transform: 'translate3d(224px, 24px, 0) scale(0.5390625)',
    })
  })

  it('synchronizes selected region bounds and pointer-selected risk overlays', () => {
    const onRiskSelect = vi.fn()
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        defaultView="risk"
        selectedRegion={{ id: 'region-a', x: 12, y: 18, width: 20, height: 9 }}
        selectedRiskId="risk-1"
        onRiskSelect={onRiskSelect}
      />,
    )

    const region = document.querySelector('[data-region-id="region-a"]')
    expect(region).toHaveAttribute('x', '12')
    expect(region).toHaveAttribute('height', '9')
    const risk = document.querySelector('[data-risk-id="risk-1"]')!
    expect(risk).toHaveAttribute('data-selected', 'true')
    fireEvent.click(risk)
    expect(onRiskSelect).toHaveBeenCalledWith('risk-1')
  })

  it('renders a distinct non-destructive operation preview for every resolved region', () => {
    const { rerender } = render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        selectedRegion={{ id: 'region-a', x: 12, y: 18, width: 20, height: 9 }}
        operationPreviewRegions={[
          { id: 'region-a', x: 12, y: 18, width: 20, height: 9 },
          { id: 'region-b', x: 40, y: 28, width: 8, height: 6 },
        ]}
      />,
    )
    expect(document.querySelectorAll('[data-operation-preview="true"]')).toHaveLength(2)
    expect(document.querySelector('[data-operation-preview="true"][data-region-id="region-b"]')).toHaveAttribute('width', '8')
    rerender(<EditorCanvas catalog={catalog()} viewportSize={{ width: 1000, height: 600 }} selectedRegion={{ id: 'region-a', x: 12, y: 18, width: 20, height: 9 }} operationPreviewRegions={[]} />)
    expect(document.querySelectorAll('[data-operation-preview="true"]')).toHaveLength(0)
  })

  it('picks an exact pixel and physical position through the fitted projection', () => {
    const onPick = vi.fn()
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        onPick={onPick}
      />,
    )

    fireEvent.click(screen.getByRole('application', { name: 'Processed image canvas' }), {
      clientX: 500,
      clientY: 300,
    })

    expect(onPick).toHaveBeenCalledTimes(1)
    expect(onPick.mock.calls[0][0]).toMatchObject({
      view: 'processed',
      pixelX: 500,
      pixelY: 250,
      physicalXmm: 100,
      physicalYmm: 50,
    })
    const coordinate = screen.getByLabelText('Selected canvas coordinate')
    expect(coordinate).toHaveTextContent(
      'Pixel 500, 250 · 100.00, 50.00 mm',
    )
    expect(coordinate).not.toHaveAttribute('aria-live')
    expect(screen.getByText(/Selected pixel 500, 250 at 100.00, 50.00 millimetres/)).toHaveAttribute(
      'aria-live',
      'polite',
    )
  })

  it('moves and announces an exact keyboard pixel cursor before selecting through onPick', () => {
    const onPick = vi.fn()
    const onCameraChange = vi.fn()
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        onCameraChange={onCameraChange}
        onPick={onPick}
      />,
    )
    const viewport = screen.getByRole('application', { name: 'Processed image canvas' })

    fireEvent.keyDown(viewport, { key: 'ArrowRight' })
    fireEvent.keyDown(viewport, { key: 'ArrowDown', shiftKey: true })
    const cursor = document.querySelector<HTMLElement>('.i23-canvas-keyboard-cursor')!
    expect(Number.parseFloat(cursor.style.left)).toBeCloseTo(501.428)
    expect(Number.parseFloat(cursor.style.top)).toBeCloseTo(309.996)
    expect(screen.getByText('Cursor pixel 501, 260.')).toHaveAttribute('aria-live', 'polite')
    expect(onCameraChange).not.toHaveBeenCalled()

    expect(fireEvent.keyDown(viewport, { key: 'Enter' })).toBe(false)
    expect(onPick).toHaveBeenCalledTimes(1)
    expect(onPick).toHaveBeenLastCalledWith(expect.objectContaining({
      view: 'processed',
      pixelX: 501,
      pixelY: 260,
      physicalYmm: 52.1,
    }))
    expect(onPick.mock.calls[0][0].physicalXmm).toBeCloseTo(100.3)
    expect(screen.getByLabelText('Selected canvas coordinate')).toHaveTextContent(
      'Pixel 501, 260 · 100.30, 52.10 mm',
    )
    expect(screen.getByText(/Selected pixel 501, 260 at 100.30, 52.10 millimetres/)).toBeInTheDocument()

    fireEvent.keyDown(viewport, { key: '+' })
    expect(Number.parseFloat(cursor.style.left)).toBeCloseTo(501.785)
    expect(Number.parseFloat(cursor.style.top)).toBeCloseTo(312.495)
    expect(document.querySelector<HTMLElement>('.i23-canvas-pick-marker')).toHaveStyle({
      left: '501.5px',
      top: '260.5px',
    })
    fireEvent.keyDown(viewport, { key: 'f' })
    expect(Number.parseFloat(cursor.style.left)).toBeCloseTo(501.428)
    expect(Number.parseFloat(cursor.style.top)).toBeCloseTo(309.996)

    fireEvent.keyDown(viewport, { key: 'h' })
    fireEvent.keyDown(viewport, { key: 'ArrowRight', shiftKey: true })
    expect(onCameraChange).toHaveBeenLastCalledWith({ zoom: 1, panX: -60, panY: 0 })
    expect(onPick).toHaveBeenCalledTimes(1)
  })

  it('offers keyboard and button camera controls with a deterministic fit reset', () => {
    const onCameraChange = vi.fn()
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        onCameraChange={onCameraChange}
      />,
    )
    const viewport = screen.getByRole('application', { name: 'Processed image canvas' })

    fireEvent.keyDown(viewport, { key: '+' })
    expect(screen.getByLabelText('Zoom level')).toHaveTextContent('125%')
    fireEvent.keyDown(viewport, { key: 'h' })
    fireEvent.keyDown(viewport, { key: 'ArrowRight', shiftKey: true })
    expect(onCameraChange).toHaveBeenLastCalledWith({ zoom: 1.25, panX: -60, panY: 0 })
    fireEvent.click(screen.getByRole('button', { name: 'Fit artwork to canvas' }))
    expect(screen.getByLabelText('Zoom level')).toHaveTextContent('100%')
    expect(onCameraChange).toHaveBeenLastCalledWith({ zoom: 1, panX: 0, panY: 0 })
  })

  it('pans by pointer in the explicit pan tool without emitting a pixel pick', () => {
    const onCameraChange = vi.fn()
    const onPick = vi.fn()
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        onCameraChange={onCameraChange}
        onPick={onPick}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Pan' }))
    const viewport = screen.getByRole('application', { name: 'Processed image canvas' })
    fireEvent.pointerDown(viewport, { pointerId: 4, button: 0, clientX: 100, clientY: 120 })
    fireEvent.pointerMove(viewport, { pointerId: 4, clientX: 135, clientY: 142 })
    fireEvent.pointerUp(viewport, { pointerId: 4, clientX: 135, clientY: 142 })
    fireEvent.click(viewport, { clientX: 135, clientY: 142 })

    expect(onCameraChange).toHaveBeenLastCalledWith({ zoom: 1, panX: 35, panY: 22 })
    expect(onPick).not.toHaveBeenCalled()
  })

  it('authors a canonical rectangle independent of the fitted viewport projection', () => {
    const onCanvasSelectionChange = vi.fn()
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        canonicalTransform={transform}
        canvasSelection={emptyCanvasSelection(transform)}
        onCanvasSelectionChange={onCanvasSelectionChange}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Rectangle' }))
    const viewport = screen.getByRole('application', { name: 'Processed image canvas' })
    fireEvent.pointerDown(viewport, { pointerId: 7, button: 0, clientX: 214.4, clientY: 157.2 })
    fireEvent.pointerMove(viewport, { pointerId: 7, clientX: 404.8, clientY: 252.4 })
    fireEvent.pointerUp(viewport, { pointerId: 7, clientX: 404.8, clientY: 252.4 })

    expect(onCanvasSelectionChange).toHaveBeenCalledTimes(1)
    const [selection, label] = onCanvasSelectionChange.mock.calls[0]
    expect(label).toBe('Add rectangle selection')
    expect(selection.primitives[0]).toMatchObject({ kind: 'rectangle', combine: 'add' })
    expect(selection.primitives[0].x).toBeCloseTo(200)
    expect(selection.primitives[0].y).toBeCloseTo(100)
    expect(selection.primitives[0].width).toBeCloseTo(200)
    expect(selection.primitives[0].height).toBeCloseTo(100)
  })

  it('offers accessible region add/subtract, edge controls, and clear history snapshots', () => {
    const onCanvasSelectionChange = vi.fn()
    const selection = emptyCanvasSelection(transform)
    const { rerender } = render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        canonicalTransform={transform}
        canvasSelection={selection}
        selectedRegionId="region-1"
        selectionRegionBounds={[{ id: 'region-1', x: 10, y: 20, width: 4, height: 5 }]}
        onCanvasSelectionChange={onCanvasSelectionChange}
      />,
    )
    fireEvent.change(screen.getByLabelText('Selection mode'), { target: { value: 'subtract' } })
    fireEvent.click(screen.getByRole('button', { name: 'Subtract selected region' }))
    const next = onCanvasSelectionChange.mock.calls[0][0]
    expect(next.primitives[0]).toMatchObject({
      kind: 'regions', combine: 'subtract', region_ids: ['region-1'],
    })
    expect(document.querySelector('[data-combine="region"]')).toHaveAttribute('x', '10')

    rerender(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        canonicalTransform={transform}
        canvasSelection={next}
        selectedRegionId="region-1"
        onCanvasSelectionChange={onCanvasSelectionChange}
      />,
    )
    fireEvent.change(screen.getByLabelText(/^Expand/), { target: { value: '0.8' } })
    fireEvent.blur(screen.getByLabelText(/^Expand/))
    expect(onCanvasSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ expand_mm: 0.8 }),
      'Change selection expand',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onCanvasSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ primitives: [] }),
      'Clear canvas selection',
    )
  })

  it('supports aligned split comparison and controlled reveal position', () => {
    const onComparisonChange = vi.fn()
    const onPick = vi.fn()
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        defaultComparison={{ mode: 'split', secondaryView: 'original', splitPercent: 45 }}
        onComparisonChange={onComparisonChange}
        onPick={onPick}
      />,
    )

    expect(screen.getByRole('button', { name: 'Split' })).toHaveAttribute('aria-pressed', 'true')
    const slider = screen.getByRole('slider', { name: /^Divider/ })
    expect(slider).toHaveValue('45')
    fireEvent.change(slider, { target: { value: '70' } })
    expect(onComparisonChange).toHaveBeenLastCalledWith({
      mode: 'split',
      secondaryView: 'original',
      splitPercent: 70,
    })
    expect(document.querySelector('[data-plane="secondary"]')).toHaveStyle({
      clipPath: 'inset(0 30% 0 0)',
    })
    const primaryTransform = document.querySelector<HTMLElement>('[data-plane="primary"]')!.style.transform
    expect(document.querySelector<HTMLElement>('[data-plane="secondary"]')!.style.transform)
      .toBe(primaryTransform)
    const viewport = screen.getByRole('application', { name: 'Processed image canvas' })
    fireEvent.click(viewport, { clientX: 200, clientY: 300 })
    fireEvent.click(viewport, { clientX: 900, clientY: 300 })
    expect(onPick.mock.calls[0][0].view).toBe('original')
    expect(onPick.mock.calls[1][0].view).toBe('processed')
  })

  it('flickers aligned sources and provides an explicit pause control', () => {
    vi.useFakeTimers()
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        defaultComparison={{ mode: 'flicker', secondaryView: 'original' }}
      />,
    )

    const primary = document.querySelector('[data-plane="primary"]')
    const secondary = document.querySelector('[data-plane="secondary"]')
    expect(primary).toHaveAttribute('data-visible', 'true')
    expect(secondary).toHaveAttribute('data-visible', 'false')
    act(() => vi.advanceTimersByTime(700))
    expect(primary).toHaveAttribute('data-visible', 'false')
    expect(secondary).toHaveAttribute('data-visible', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Pause flicker' }))
    expect(screen.getByRole('button', { name: 'Resume flicker' })).toBeInTheDocument()
  })

  it('stops flicker immediately when reduced motion changes at runtime', () => {
    vi.useFakeTimers()
    let reduced = false
    const preferenceEvents = new EventTarget()
    const preference = {
      get matches() { return reduced },
      media: '(prefers-reduced-motion: reduce)',
      onchange: null,
      addEventListener: preferenceEvents.addEventListener.bind(preferenceEvents),
      removeEventListener: preferenceEvents.removeEventListener.bind(preferenceEvents),
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: preferenceEvents.dispatchEvent.bind(preferenceEvents),
    } as MediaQueryList
    vi.stubGlobal('matchMedia', vi.fn(() => preference))
    render(
      <EditorCanvas
        catalog={catalog()}
        viewportSize={{ width: 1000, height: 600 }}
        defaultComparison={{ mode: 'flicker', secondaryView: 'original' }}
      />,
    )
    const primary = document.querySelector('[data-plane="primary"]')
    const secondary = document.querySelector('[data-plane="secondary"]')

    act(() => vi.advanceTimersByTime(700))
    expect(primary).toHaveAttribute('data-visible', 'false')
    expect(secondary).toHaveAttribute('data-visible', 'true')

    reduced = true
    act(() => preference.dispatchEvent(new Event('change')))
    expect(primary).toHaveAttribute('data-visible', 'true')
    expect(secondary).toHaveAttribute('data-visible', 'false')
    const paused = screen.getByRole('button', { name: 'Flicker paused (reduced motion)' })
    expect(paused).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(paused)
    act(() => vi.advanceTimersByTime(2_100))
    expect(primary).toHaveAttribute('data-visible', 'true')
    expect(secondary).toHaveAttribute('data-visible', 'false')

    reduced = false
    act(() => preference.dispatchEvent(new Event('change')))
    expect(screen.getByRole('button', { name: 'Pause flicker' })).toHaveAttribute('aria-disabled', 'false')
    act(() => vi.advanceTimersByTime(700))
    expect(secondary).toHaveAttribute('data-visible', 'true')
  })

  it('refuses comparison when render spaces differ and explains why', () => {
    const sourceCatalog = catalog()
    const original = sourceCatalog.sources.original!
    sourceCatalog.sources.original = {
      ...original,
      space: { ...original.space, widthPx: 999 },
    }
    render(
      <EditorCanvas
        catalog={sourceCatalog}
        viewportSize={{ width: 1000, height: 600 }}
        defaultComparison={{ mode: 'split', secondaryView: 'original' }}
      />,
    )

    expect(screen.getByRole('button', { name: 'Split' })).toBeDisabled()
    expect(screen.getByText(/two different views of the same image/)).toBeInTheDocument()
  })

  it('announces source loading and errors without changing to a misleading view', () => {
    const sourceCatalog = catalog()
    sourceCatalog.sources.processed = { ...sourceCatalog.sources.processed!, state: 'loading' }
    const { rerender } = render(
      <EditorCanvas catalog={sourceCatalog} viewportSize={{ width: 1000, height: 600 }} />,
    )
    expect(screen.getByText('Loading pixel source…')).toBeInTheDocument()

    sourceCatalog.sources.processed = {
      ...sourceCatalog.sources.processed!,
      state: 'error',
      errorMessage: 'Hash verification failed.',
    }
    rerender(<EditorCanvas catalog={sourceCatalog} viewportSize={{ width: 1000, height: 600 }} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Hash verification failed.')
    expect(screen.getByRole('img', { name: 'Processed canvas view' })).toHaveAttribute(
      'data-artifact-kind',
      'palette-preview-image',
    )
  })

  it('turns a browser image failure into an actionable canvas error state', () => {
    render(<EditorCanvas catalog={catalog()} viewportSize={{ width: 1000, height: 600 }} />)
    fireEvent.error(screen.getByRole('img', { name: 'Processed canvas view' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Processed pixels could not be loaded.')
  })
})
