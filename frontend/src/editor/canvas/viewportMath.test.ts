import { describe, expect, it } from 'vitest'
import { editorCanvasPhysicalBoundsToRaster } from './canvasModel'
import {
  editorCanvasRasterToScreen,
  editorCanvasRasterRectToScreen,
  panEditorCanvas,
  projectEditorCanvas,
  screenToEditorCanvasPick,
  zoomEditorCanvasAt,
} from './viewportMath'

const space = { widthPx: 1000, heightPx: 500, widthMm: 200, heightMm: 100 }
const viewport = { width: 1000, height: 600 }

describe('editor canvas viewport math', () => {
  it('fits and centers raster edge space without half-pixel drift', () => {
    expect(
      projectEditorCanvas(space, viewport, { zoom: 1, panX: 0, panY: 0 }, 20),
    ).toEqual({
      fitScale: 0.96,
      scale: 0.96,
      displayWidth: 960,
      displayHeight: 480,
      originX: 20,
      originY: 60,
    })
  })

  it('maps a transformed screen point to exact pixel and physical coordinates', () => {
    const projection = projectEditorCanvas(
      space,
      viewport,
      { zoom: 2, panX: 30, panY: -10 },
      20,
    )
    const pick = screenToEditorCanvasPick('processed', { x: 530, y: 290 }, space, projection)

    expect(pick).not.toBeNull()
    expect(pick?.rasterX).toBeCloseTo(500)
    expect(pick?.rasterY).toBeCloseTo(250)
    expect(pick?.pixelX).toBe(500)
    expect(pick?.pixelY).toBe(250)
    expect(pick?.physicalXmm).toBeCloseTo(100)
    expect(pick?.physicalYmm).toBeCloseTo(50)
    expect(
      screenToEditorCanvasPick('processed', { x: -500, y: 0 }, space, projection),
    ).toBeNull()
  })

  it('keeps the raster coordinate beneath the pointer fixed while zooming', () => {
    const camera = { zoom: 1.4, panX: 37, panY: -19 }
    const before = projectEditorCanvas(space, viewport, camera, 20)
    const raster = { x: 723.25, y: 112.75 }
    const anchor = editorCanvasRasterToScreen(raster, before)
    const zoomed = zoomEditorCanvasAt(camera, 7.5, anchor, space, viewport, 20)
    const after = projectEditorCanvas(space, viewport, zoomed, 20)
    const anchored = editorCanvasRasterToScreen(raster, after)

    expect(anchored.x).toBeCloseTo(anchor.x, 8)
    expect(anchored.y).toBeCloseTo(anchor.y, 8)
  })

  it('projects a non-square overlay through fit, zoom, and pan in CSS pixels', () => {
    const nonSquare = { widthPx: 1600, heightPx: 900, widthMm: 320, heightMm: 180 }
    const projection = projectEditorCanvas(
      nonSquare,
      { width: 1200, height: 800 },
      { zoom: 2.5, panX: 37, panY: -19 },
      32,
    )
    const rasterRect = editorCanvasPhysicalBoundsToRaster(
      { xMm: 80, yMm: 45, widthMm: 64, heightMm: 36 },
      nonSquare,
    )
    const screenRect = editorCanvasRasterRectToScreen(rasterRect, projection)

    expect(projection.fitScale).toBeCloseTo(0.71)
    expect(rasterRect).toEqual({ x: 400, y: 225, width: 320, height: 180 })
    expect(screenRect).toEqual({ x: -73, y: -18.375, width: 568, height: 319.5 })
    expect(editorCanvasRasterToScreen(
      { x: rasterRect.x + rasterRect.width, y: rasterRect.y + rasterRect.height },
      projection,
    )).toEqual({ x: 495, y: 301.125 })
  })

  it('keeps CSS-space overlay geometry invariant across device pixel ratios', () => {
    const projection = projectEditorCanvas(space, viewport, { zoom: 1.75, panX: 12, panY: -8 }, 20)
    const cssRect = editorCanvasRasterRectToScreen(
      { x: 50, y: 100, width: 25, height: 20 },
      projection,
    )
    for (const devicePixelRatio of [1, 1.5, 2, 3]) {
      const deviceRect = Object.fromEntries(
        Object.entries(cssRect).map(([key, value]) => [key, value * devicePixelRatio]),
      ) as typeof cssRect
      expect(Object.fromEntries(
        Object.entries(deviceRect).map(([key, value]) => [key, value / devicePixelRatio]),
      )).toEqual(cssRect)
    }
  })

  it('pans in screen pixels and clamps zoom to supported limits', () => {
    expect(panEditorCanvas({ zoom: 1, panX: 3, panY: 4 }, { x: -8, y: 12 })).toEqual({
      zoom: 1,
      panX: -5,
      panY: 16,
    })
    expect(zoomEditorCanvasAt({ zoom: 1, panX: 0, panY: 0 }, 1000, { x: 0, y: 0 }, space, viewport).zoom).toBe(64)
    expect(zoomEditorCanvasAt({ zoom: 1, panX: 0, panY: 0 }, 0, { x: 0, y: 0 }, space, viewport).zoom).toBe(0.1)
  })
})
