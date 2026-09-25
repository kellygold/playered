import { describe, expect, it } from 'vitest'
import {
  createEditorCanvasSourceCatalog,
  editorCanvasSourcesComparable,
  validateEditorCanvasSpace,
  type CanvasArtifactLike,
} from './canvasModel'

const artifact = (kind: string, mediaType = 'image/png'): CanvasArtifactLike => ({
  kind,
  download_url: `/api/artifacts/${kind}`,
  sha256: kind.padEnd(64, 'a').slice(0, 64),
  media_type: mediaType,
})

const space = { widthPx: 1000, heightPx: 500, widthMm: 200, heightMm: 100 }

describe('editor canvas source catalog', () => {
  it('maps each view to its exact stage artifact and never substitutes processed pixels', () => {
    const catalog = createEditorCanvasSourceCatalog({
      artifacts: [
        artifact('preview-image'),
        artifact('quantized-preview-image'),
        artifact('palette-preview-image'),
        artifact('editor-changed-mask', 'application/octet-stream'),
        artifact('geometry-preview-image'),
        artifact('slicer-preview-image'),
      ],
      space,
    })

    expect(catalog.sources.original?.raster.artifactKind).toBe('preview-image')
    expect(catalog.sources.quantized?.raster.artifactKind).toBe('quantized-preview-image')
    expect(catalog.sources.processed?.raster.artifactKind).toBe('palette-preview-image')
    expect(catalog.sources.geometry?.raster.artifactKind).toBe('geometry-preview-image')
    expect(catalog.sources.slicer?.raster.artifactKind).toBe('slicer-preview-image')
    expect(catalog.sources.original?.raster.url).toContain('?sha=')
    expect(catalog.sources.mask).toBeUndefined()
    expect(catalog.unavailable.mask).toMatch(/No mask preview is available yet/)
  })

  it('accepts the canonical geometry preview artifact from output generation', () => {
    const catalog = createEditorCanvasSourceCatalog({
      artifacts: [artifact('geometry-preview')],
      space,
    })

    expect(catalog.sources.geometry?.raster.artifactKind).toBe('geometry-preview')
  })

  it('builds risk overlays on processed pixels using physical bounds without changing them', () => {
    const catalog = createEditorCanvasSourceCatalog({
      artifacts: [artifact('palette-preview-image')],
      space,
      riskReport: {
        warnings: [
          {
            id: 'risk-1',
            severity: 'warning',
            title: 'Tiny island',
            affected_bounds: [{ x_mm: 10, y_mm: 20, width_mm: 5, height_mm: 4 }],
          },
        ],
      },
    })

    expect(catalog.sources.risk?.raster.artifactKind).toBe('palette-preview-image')
    expect(catalog.sources.risk?.risks).toEqual([
      {
        id: 'risk-1',
        severity: 'warning',
        title: 'Tiny island',
        presentation: 'feature-bounds',
        affectedRegionCount: 0,
        boundsMm: [{ xMm: 10, yMm: 20, widthMm: 5, heightMm: 4 }],
        boundsPx: [],
      },
    ])
  })

  it('replaces a misleading fragmentation union with bounded per-region pixel extents', () => {
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
    const squareSpace = { widthPx: 1024, heightPx: 1024, widthMm: 200, heightMm: 200 }
    const source = createEditorCanvasSourceCatalog({
      artifacts: [artifact('palette-preview-image')],
      space: squareSpace,
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
    }).sources.risk!

    expect(source.risks?.[0]).toMatchObject({
      presentation: 'region-extents',
      affectedRegionCount: 96,
      boundsMm: [],
    })
    expect(source.risks?.[0].boundsPx).toEqual(regions.map((region) => region.pixel_bounds))
    expect(source.risks?.[0].boundsPx).not.toContainEqual({ x: 0, y: 0, width: 1024, height: 1024 })
  })

  it('suppresses fragmentation rectangles when per-region extents would create a box storm', () => {
    const regionIds = Array.from({ length: 129 }, (_, index) => `region-${index}`)
    const source = createEditorCanvasSourceCatalog({
      artifacts: [artifact('palette-preview-image')],
      space: { widthPx: 1024, heightPx: 1024, widthMm: 200, heightMm: 200 },
      regionGraph: {
        width_px: 1024,
        height_px: 1024,
        regions: regionIds.map((id, index) => ({
          id,
          pixel_bounds: { x: index, y: index, width: 1, height: 1 },
        })),
      },
      riskReport: {
        warnings: [{
          id: 'fragmentation',
          code: 'excess_fragmentation',
          severity: 'error',
          title: '129 separate regions',
          affected_region_ids: regionIds,
          affected_bounds: [{ x_mm: 0, y_mm: 0, width_mm: 200, height_mm: 200 }],
        }],
      },
    }).sources.risk!

    expect(source.risks?.[0]).toMatchObject({
      presentation: 'list-only',
      affectedRegionCount: 129,
      boundsMm: [],
      boundsPx: [],
    })
  })

  it('requires exact render-space alignment for split and flicker comparisons', () => {
    const first = createEditorCanvasSourceCatalog({
      artifacts: [artifact('preview-image')],
      space,
    }).sources.original!
    const aligned = { ...first, view: 'processed' as const }
    const wrongPixels = {
      ...aligned,
      space: { ...space, widthPx: 999 },
    }
    const wrongPhysicalSize = {
      ...aligned,
      space: { ...space, widthMm: 199 },
    }

    expect(editorCanvasSourcesComparable(first, aligned)).toBe(true)
    expect(editorCanvasSourcesComparable(first, wrongPixels)).toBe(false)
    expect(editorCanvasSourcesComparable(first, wrongPhysicalSize)).toBe(false)
  })

  it('rejects invalid dimensions before they can corrupt physical picking', () => {
    expect(() => validateEditorCanvasSpace({ ...space, widthPx: 1.5 })).toThrow(/integer pixels/)
    expect(() => validateEditorCanvasSpace({ ...space, heightMm: 0 })).toThrow(/millimetres/)
  })
})


it('explains a failed slice preview without asking for the same build again', () => {
  const catalog = createEditorCanvasSourceCatalog({
    artifacts: [], space, slicerUnavailableReason: 'Unsupported arc plane',
  })
  expect(catalog.unavailable.slicer).toContain('Unsupported arc plane')
  expect(catalog.unavailable.slicer).not.toContain('Build a new 3MF')
})
