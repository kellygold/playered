import type { EditorCanvasSpace, EditorCanvasViewId } from './canvasModel'

export type EditorCanvasViewport = {
  width: number
  height: number
}

export type EditorCanvasCamera = {
  zoom: number
  panX: number
  panY: number
}

export type EditorCanvasProjection = {
  fitScale: number
  scale: number
  originX: number
  originY: number
  displayWidth: number
  displayHeight: number
}

export type EditorCanvasPick = {
  view: EditorCanvasViewId
  screenX: number
  screenY: number
  rasterX: number
  rasterY: number
  pixelX: number
  pixelY: number
  normalizedX: number
  normalizedY: number
  physicalXmm: number
  physicalYmm: number
}

export const DEFAULT_EDITOR_CANVAS_CAMERA: EditorCanvasCamera = {
  zoom: 1,
  panX: 0,
  panY: 0,
}

export const EDITOR_CANVAS_MIN_ZOOM = 0.1
export const EDITOR_CANVAS_MAX_ZOOM = 64

function validViewport(viewport: EditorCanvasViewport) {
  return viewport.width > 0 && viewport.height > 0
}

export function clampEditorCanvasZoom(zoom: number) {
  if (!Number.isFinite(zoom)) return 1
  return Math.min(EDITOR_CANVAS_MAX_ZOOM, Math.max(EDITOR_CANVAS_MIN_ZOOM, zoom))
}

export function projectEditorCanvas(
  space: EditorCanvasSpace,
  viewport: EditorCanvasViewport,
  camera: EditorCanvasCamera,
  padding = 24,
): EditorCanvasProjection {
  const availableWidth = Math.max(1, viewport.width - padding * 2)
  const availableHeight = Math.max(1, viewport.height - padding * 2)
  const fitScale = validViewport(viewport)
    ? Math.min(availableWidth / space.widthPx, availableHeight / space.heightPx)
    : 1
  const scale = fitScale * clampEditorCanvasZoom(camera.zoom)
  const displayWidth = space.widthPx * scale
  const displayHeight = space.heightPx * scale
  return {
    fitScale,
    scale,
    displayWidth,
    displayHeight,
    originX: (viewport.width - displayWidth) / 2 + camera.panX,
    originY: (viewport.height - displayHeight) / 2 + camera.panY,
  }
}

export function screenToEditorCanvasPick(
  view: EditorCanvasViewId,
  screen: { x: number; y: number },
  space: EditorCanvasSpace,
  projection: EditorCanvasProjection,
): EditorCanvasPick | null {
  const rasterX = (screen.x - projection.originX) / projection.scale
  const rasterY = (screen.y - projection.originY) / projection.scale
  if (rasterX < 0 || rasterY < 0 || rasterX >= space.widthPx || rasterY >= space.heightPx) {
    return null
  }
  const normalizedX = rasterX / space.widthPx
  const normalizedY = rasterY / space.heightPx
  return {
    view,
    screenX: screen.x,
    screenY: screen.y,
    rasterX,
    rasterY,
    pixelX: Math.min(space.widthPx - 1, Math.floor(rasterX)),
    pixelY: Math.min(space.heightPx - 1, Math.floor(rasterY)),
    normalizedX,
    normalizedY,
    physicalXmm: normalizedX * space.widthMm,
    physicalYmm: normalizedY * space.heightMm,
  }
}

export function editorCanvasRasterToScreen(
  raster: { x: number; y: number },
  projection: EditorCanvasProjection,
) {
  return {
    x: projection.originX + raster.x * projection.scale,
    y: projection.originY + raster.y * projection.scale,
  }
}

export function editorCanvasRasterRectToScreen(
  raster: { x: number; y: number; width: number; height: number },
  projection: EditorCanvasProjection,
) {
  const origin = editorCanvasRasterToScreen(raster, projection)
  return {
    ...origin,
    width: raster.width * projection.scale,
    height: raster.height * projection.scale,
  }
}

export function zoomEditorCanvasAt(
  camera: EditorCanvasCamera,
  requestedZoom: number,
  anchor: { x: number; y: number },
  space: EditorCanvasSpace,
  viewport: EditorCanvasViewport,
  padding = 24,
): EditorCanvasCamera {
  const before = projectEditorCanvas(space, viewport, camera, padding)
  const rasterX = (anchor.x - before.originX) / before.scale
  const rasterY = (anchor.y - before.originY) / before.scale
  const zoom = clampEditorCanvasZoom(requestedZoom)
  const afterScale = before.fitScale * zoom
  const baseOriginX = (viewport.width - space.widthPx * afterScale) / 2
  const baseOriginY = (viewport.height - space.heightPx * afterScale) / 2
  return {
    zoom,
    panX: anchor.x - rasterX * afterScale - baseOriginX,
    panY: anchor.y - rasterY * afterScale - baseOriginY,
  }
}

export function panEditorCanvas(
  camera: EditorCanvasCamera,
  delta: { x: number; y: number },
): EditorCanvasCamera {
  return { ...camera, panX: camera.panX + delta.x, panY: camera.panY + delta.y }
}

export function resetEditorCanvasCamera(): EditorCanvasCamera {
  return { ...DEFAULT_EDITOR_CANVAS_CAMERA }
}
