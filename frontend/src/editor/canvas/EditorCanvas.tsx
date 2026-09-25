import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type WheelEvent,
} from 'react'
import {
  EDITOR_CANVAS_VIEWS,
  editorCanvasPhysicalBoundsToRaster,
  editorCanvasSourcesComparable,
  editorCanvasViewMetadata,
  type EditorCanvasComparisonMode,
  type EditorCanvasSourceCatalog,
  type EditorCanvasTool,
  type EditorCanvasViewId,
  type EditorCanvasViewSource,
} from './canvasModel'
import {
  DEFAULT_EDITOR_CANVAS_CAMERA,
  panEditorCanvas,
  projectEditorCanvas,
  resetEditorCanvasCamera,
  screenToEditorCanvasPick,
  zoomEditorCanvasAt,
  type EditorCanvasCamera,
  type EditorCanvasPick,
  type EditorCanvasViewport,
} from './viewportMath'
import type { RegionGraph, RiskReport } from '../../contracts'
import type { CanonicalTransform } from '../../contracts'
import type { RegionAssignmentMap } from '../inspector/regionAssignment'
import {
  appendSelectionPrimitive,
  normalizedSourcePointToWorking,
  selectionPrimitiveId,
  updateSelectionEdge,
  workingPointToNormalizedSource,
  type CanvasSelectionState,
  type SelectionCombineMode,
  type SourceSelectionPoint,
} from '../selection'
import { RiskOverlayCanvas } from './RiskOverlayCanvas'
import {
  warningRegionIndices,
  type RiskMaskMap,
  type RiskMaskMode,
  type RiskSeverity,
} from './riskMask'
import './EditorCanvas.css'

export type EditorCanvasComparison = {
  mode: EditorCanvasComparisonMode
  secondaryView: EditorCanvasViewId
  splitPercent: number
}

export type EditorCanvasRegionSelection = {
  id: string
  x: number
  y: number
  width: number
  height: number
}

export type EditorCanvasProps = {
  catalog: EditorCanvasSourceCatalog
  ariaLabel?: string
  view?: EditorCanvasViewId
  defaultView?: EditorCanvasViewId
  onViewChange?: (view: EditorCanvasViewId) => void
  comparison?: EditorCanvasComparison
  defaultComparison?: Partial<EditorCanvasComparison>
  onComparisonChange?: (comparison: EditorCanvasComparison) => void
  camera?: EditorCanvasCamera
  defaultCamera?: EditorCanvasCamera
  onCameraChange?: (camera: EditorCanvasCamera) => void
  selectedPick?: EditorCanvasPick | null
  onPick?: (pick: EditorCanvasPick) => void
  selectedRegion?: EditorCanvasRegionSelection | null
  operationPreviewRegions?: EditorCanvasRegionSelection[]
  canonicalTransform?: CanonicalTransform | null
  canvasSelection?: CanvasSelectionState | null
  selectionRegionBounds?: EditorCanvasRegionSelection[]
  selectedRegionId?: string | null
  selectionDisabled?: boolean
  onCanvasSelectionChange?: (selection: CanvasSelectionState, label: string) => void
  selectedRiskId?: string | null
  onRiskSelect?: (riskId: string) => void
  regionAssignment?: RegionAssignmentMap | null
  riskMask?: RiskMaskMap | null
  riskGraph?: RegionGraph | null
  riskReport?: RiskReport | null
  viewportSize?: EditorCanvasViewport
  fitPadding?: number
  className?: string
}

type PanDrag = {
  pointerId: number
  startClientX: number
  startClientY: number
  startCamera: EditorCanvasCamera
  moved: boolean
}

type KeyboardPixelCursor = {
  pixelX: number
  pixelY: number
}

type SelectionDrag = {
  pointerId: number
  tool: 'rectangle' | 'lasso' | 'brush'
  points: SourceSelectionPoint[]
}

const DEFAULT_VIEWPORT = { width: 900, height: 620 }
const DEFAULT_COMPARISON: EditorCanvasComparison = {
  mode: 'single',
  secondaryView: 'original',
  splitPercent: 50,
}

function availableViews(catalog: EditorCanvasSourceCatalog) {
  return EDITOR_CANVAS_VIEWS.map((view) => view.id).filter((view) => catalog.sources[view])
}

function firstView(catalog: EditorCanvasSourceCatalog) {
  const preferred: EditorCanvasViewId[] = ['processed', 'quantized', 'original']
  return (
    preferred.find((view) => catalog.sources[view]) ??
    availableViews(catalog)[0] ??
    'processed'
  )
}

function clampSplit(value: number) {
  if (!Number.isFinite(value)) return 50
  return Math.min(95, Math.max(5, Math.round(value)))
}

function normalizeComparison(
  value: Partial<EditorCanvasComparison> | undefined,
  fallbackSecondary: EditorCanvasViewId,
): EditorCanvasComparison {
  return {
    mode: value?.mode ?? DEFAULT_COMPARISON.mode,
    secondaryView: value?.secondaryView ?? fallbackSecondary,
    splitPercent: clampSplit(value?.splitPercent ?? DEFAULT_COMPARISON.splitPercent),
  }
}

function useMeasuredViewport(
  supplied: EditorCanvasViewport | undefined,
  element: HTMLDivElement | null,
) {
  const [measured, setMeasured] = useState<EditorCanvasViewport>(DEFAULT_VIEWPORT)

  useEffect(() => {
    if (supplied || !element) return
    const update = () => {
      const bounds = element.getBoundingClientRect()
      if (bounds.width <= 0 || bounds.height <= 0) return
      setMeasured((current) =>
        current.width === bounds.width && current.height === bounds.height
          ? current
          : { width: bounds.width, height: bounds.height },
      )
    }
    const observer =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => update())
    observer?.observe(element)
    window.addEventListener('resize', update)
    const frame = window.requestAnimationFrame(update)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', update)
      window.cancelAnimationFrame(frame)
    }
  }, [element, supplied])

  return supplied ?? measured
}

function sourceStateMessage(source: EditorCanvasViewSource) {
  if (source.state === 'loading') return 'Loading pixel source…'
  if (source.state === 'error') return source.errorMessage ?? 'This pixel source could not be loaded.'
  return null
}

export function EditorCanvas({
  catalog,
  ariaLabel = 'Artwork comparison canvas',
  view: controlledView,
  defaultView,
  onViewChange,
  comparison: controlledComparison,
  defaultComparison,
  onComparisonChange,
  camera: controlledCamera,
  defaultCamera = DEFAULT_EDITOR_CANVAS_CAMERA,
  onCameraChange,
  selectedPick,
  onPick,
  selectedRegion,
  operationPreviewRegions = [],
  canonicalTransform,
  canvasSelection,
  selectionRegionBounds = [],
  selectedRegionId,
  selectionDisabled = false,
  onCanvasSelectionChange,
  selectedRiskId,
  onRiskSelect,
  regionAssignment,
  riskMask,
  riskGraph,
  riskReport,
  viewportSize: suppliedViewport,
  fitPadding = 24,
  className,
}: EditorCanvasProps) {
  const generatedId = useId().replaceAll(':', '')
  const [viewportElement, setViewportElement] = useState<HTMLDivElement | null>(null)
  const viewport = useMeasuredViewport(suppliedViewport, viewportElement)
  const initialView = defaultView && catalog.sources[defaultView] ? defaultView : firstView(catalog)
  const [uncontrolledView, setUncontrolledView] = useState<EditorCanvasViewId>(initialView)
  const requestedView = controlledView ?? uncontrolledView
  const activeView = catalog.sources[requestedView] ? requestedView : firstView(catalog)
  const activeSource = catalog.sources[activeView]
  const secondaryFallback =
    availableViews(catalog).find((candidate) => candidate !== activeView) ?? activeView
  const [uncontrolledComparison, setUncontrolledComparison] = useState(() =>
    normalizeComparison(defaultComparison, secondaryFallback),
  )
  const comparison = normalizeComparison(
    controlledComparison ?? uncontrolledComparison,
    secondaryFallback,
  )
  const secondarySource = catalog.sources[comparison.secondaryView]
  const comparisonReady = Boolean(
    activeSource &&
      secondarySource &&
      activeView !== comparison.secondaryView &&
      editorCanvasSourcesComparable(activeSource, secondarySource),
  )
  const comparisonMode = comparisonReady ? comparison.mode : 'single'
  const [uncontrolledCamera, setUncontrolledCamera] = useState(defaultCamera)
  const camera = controlledCamera ?? uncontrolledCamera
  const [tool, setTool] = useState<EditorCanvasTool>('inspect')
  const [selectionCombine, setSelectionCombine] = useState<SelectionCombineMode>('add')
  const [brushRadiusMm, setBrushRadiusMm] = useState(1)
  const [pixelGrid, setPixelGrid] = useState(false)
  const [unavailableView, setUnavailableView] = useState<EditorCanvasViewId | null>(null)
  const [riskMode, setRiskMode] = useState<RiskMaskMode>('selected')
  const [riskSeverity, setRiskSeverity] = useState<RiskSeverity>('warning')
  const [internalPick, setInternalPick] = useState<EditorCanvasPick | null>(null)
  const currentPick = selectedPick === undefined ? internalPick : selectedPick
  const [keyboardCursor, setKeyboardCursor] = useState<KeyboardPixelCursor | null>(null)
  const [keyboardAnnouncement, setKeyboardAnnouncement] = useState('')
  const [flickerSecondary, setFlickerSecondary] = useState(false)
  const [reducedMotion, setReducedMotion] = useState(() =>
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false,
  )
  const [flickerPausedByUser, setFlickerPausedByUser] = useState(false)
  const flickerPaused = reducedMotion || flickerPausedByUser
  const [loadFailures, setLoadFailures] = useState<Record<string, string>>({})
  const dragRef = useRef<PanDrag | null>(null)
  const selectionDragRef = useRef<SelectionDrag | null>(null)
  const [selectionDraft, setSelectionDraft] = useState<SelectionDrag | null>(null)
  const suppressClickRef = useRef(false)

  const projection = useMemo(
    () =>
      activeSource
        ? projectEditorCanvas(activeSource.space, viewport, camera, fitPadding)
        : null,
    [activeSource, camera, fitPadding, viewport],
  )

  useEffect(() => {
    const preference = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    if (!preference) return
    const applyPreference = () => {
      setReducedMotion(preference.matches)
      if (preference.matches) setFlickerSecondary(false)
    }
    applyPreference()
    preference.addEventListener?.('change', applyPreference)
    return () => preference.removeEventListener?.('change', applyPreference)
  }, [])

  useEffect(() => {
    if (comparisonMode !== 'flicker' || flickerPaused) return
    const interval = window.setInterval(() => setFlickerSecondary((current) => !current), 700)
    return () => window.clearInterval(interval)
  }, [comparisonMode, flickerPaused])

  const changeView = (next: EditorCanvasViewId) => {
    if (!catalog.sources[next]) {
      setUnavailableView(next)
      return
    }
    setUnavailableView(null)
    if (controlledView === undefined) setUncontrolledView(next)
    onViewChange?.(next)
    if (comparison.secondaryView === next) {
      const replacement = availableViews(catalog).find((candidate) => candidate !== next)
      if (replacement) changeComparison({ secondaryView: replacement })
    }
  }

  const changeComparison = (patch: Partial<EditorCanvasComparison>) => {
    const next = normalizeComparison({ ...comparison, ...patch }, secondaryFallback)
    if (controlledComparison === undefined) setUncontrolledComparison(next)
    onComparisonChange?.(next)
  }

  const changeCamera = (next: EditorCanvasCamera) => {
    if (controlledCamera === undefined) setUncontrolledCamera(next)
    onCameraChange?.(next)
  }

  const zoomAt = (nextZoom: number, anchor = { x: viewport.width / 2, y: viewport.height / 2 }) => {
    if (!activeSource) return
    changeCamera(
      zoomEditorCanvasAt(camera, nextZoom, anchor, activeSource.space, viewport, fitPadding),
    )
  }

  const fit = () => changeCamera(resetEditorCanvasCamera())

  const selectionPoint = (clientX: number, clientY: number): SourceSelectionPoint | null => {
    if (!activeSource || !projection || !viewportElement || !canonicalTransform) return null
    const bounds = viewportElement.getBoundingClientRect()
    const pick = screenToEditorCanvasPick(
      activeView,
      { x: clientX - bounds.left, y: clientY - bounds.top },
      activeSource.space,
      projection,
    )
    if (!pick) return null
    return workingPointToNormalizedSource(
      { x: pick.rasterX, y: pick.rasterY },
      canonicalTransform,
    )
  }

  const commitSelection = (next: CanvasSelectionState, label: string) => {
    onCanvasSelectionChange?.(next, label)
    setKeyboardAnnouncement(`${label}. ${next.primitives.length} selection step${next.primitives.length === 1 ? '' : 's'} saved.`)
  }

  const startSelection = (event: PointerEvent<HTMLDivElement>) => {
    if (!['rectangle', 'lasso', 'brush'].includes(tool) || selectionDisabled || !canvasSelection) {
      return false
    }
    const point = selectionPoint(event.clientX, event.clientY)
    if (!point) return false
    event.preventDefault()
    const drag: SelectionDrag = {
      pointerId: event.pointerId,
      tool: tool as SelectionDrag['tool'],
      points: [point],
    }
    selectionDragRef.current = drag
    setSelectionDraft(drag)
    event.currentTarget.setPointerCapture?.(event.pointerId)
    return true
  }

  const continueSelection = (event: PointerEvent<HTMLDivElement>) => {
    const drag = selectionDragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return false
    const point = selectionPoint(event.clientX, event.clientY)
    if (!point) return true
    const previous = drag.points[drag.points.length - 1]
    const distance = Math.hypot(point.x - previous.x, point.y - previous.y)
    if (drag.tool === 'rectangle') drag.points = [drag.points[0], point]
    else if (distance >= 0.25) drag.points = [...drag.points, point]
    selectionDragRef.current = drag
    setSelectionDraft({ ...drag, points: [...drag.points] })
    return true
  }

  const finishSelection = (event: PointerEvent<HTMLDivElement>, canceled = false) => {
    const drag = selectionDragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return false
    selectionDragRef.current = null
    setSelectionDraft(null)
    suppressClickRef.current = true
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    if (canceled || !canvasSelection) return true
    const first = drag.points[0]
    const last = drag.points[drag.points.length - 1]
    let primitive
    if (drag.tool === 'rectangle') {
      const x = Math.min(first.x, last.x)
      const y = Math.min(first.y, last.y)
      const width = Math.abs(last.x - first.x)
      const height = Math.abs(last.y - first.y)
      if (width < 0.01 || height < 0.01) return true
      primitive = {
        kind: 'rectangle' as const,
        primitive_id: selectionPrimitiveId(),
        combine: selectionCombine,
        x, y, width, height,
      }
    } else if (drag.tool === 'lasso') {
      if (drag.points.length < 3) return true
      primitive = {
        kind: 'lasso' as const,
        primitive_id: selectionPrimitiveId(),
        combine: selectionCombine,
        points: drag.points,
      }
    } else {
      primitive = {
        kind: 'brush' as const,
        primitive_id: selectionPrimitiveId(),
        combine: selectionCombine,
        points: drag.points,
        radius_mm: brushRadiusMm,
      }
    }
    commitSelection(
      appendSelectionPrimitive(canvasSelection, primitive),
      `${selectionCombine === 'add' ? 'Add' : 'Subtract'} ${drag.tool} selection`,
    )
    return true
  }

  const pointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (!startSelection(event)) startPan(event)
  }

  const pointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!continueSelection(event)) continuePan(event)
  }

  const pointerUp = (event: PointerEvent<HTMLDivElement>) => {
    if (!finishSelection(event)) finishPan(event)
  }

  const pointerCancel = (event: PointerEvent<HTMLDivElement>) => {
    if (!finishSelection(event, true)) finishPan(event)
  }

  const startPan = (event: PointerEvent<HTMLDivElement>) => {
    if (tool !== 'pan' && event.button !== 1) return
    event.preventDefault()
    dragRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startCamera: camera,
      moved: false,
    }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }

  const continuePan = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const delta = {
      x: event.clientX - drag.startClientX,
      y: event.clientY - drag.startClientY,
    }
    if (Math.abs(delta.x) > 2 || Math.abs(delta.y) > 2) drag.moved = true
    changeCamera(panEditorCanvas(drag.startCamera, delta))
  }

  const finishPan = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    suppressClickRef.current = drag.moved
    dragRef.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }

  const pick = (clientX: number, clientY: number) => {
    if (!activeSource || !projection || !viewportElement) return
    const bounds = viewportElement.getBoundingClientRect()
    const screen = { x: clientX - bounds.left, y: clientY - bounds.top }
    let pickedView = activeView
    if (
      comparisonMode === 'split' &&
      secondarySource &&
      screen.x <=
        projection.originX + projection.displayWidth * (comparison.splitPercent / 100)
    ) {
      pickedView = comparison.secondaryView
    }
    if (comparisonMode === 'flicker' && secondarySource && flickerSecondary && !flickerPaused) {
      pickedView = comparison.secondaryView
    }
    const result = screenToEditorCanvasPick(
      pickedView,
      screen,
      activeSource.space,
      projection,
    )
    if (!result) return
    if (selectedPick === undefined) setInternalPick(result)
    onPick?.(result)
    setKeyboardAnnouncement(
      `Selected pixel ${result.pixelX}, ${result.pixelY} at ${result.physicalXmm.toFixed(2)}, ${result.physicalYmm.toFixed(2)} millimetres.`,
    )
  }

  const clickCanvas = (event: React.MouseEvent<HTMLDivElement>) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false
      return
    }
    if (tool === 'inspect') pick(event.clientX, event.clientY)
  }

  const wheelCanvas = (event: WheelEvent<HTMLDivElement>) => {
    if (!activeSource) return
    event.preventDefault()
    const bounds = event.currentTarget.getBoundingClientRect()
    const factor = Math.exp(-event.deltaY * 0.0015)
    zoomAt(camera.zoom * factor, {
      x: event.clientX - bounds.left,
      y: event.clientY - bounds.top,
    })
  }

  const keyboardCursorOrigin = (): KeyboardPixelCursor | null => {
    if (!activeSource) return null
    const candidate = keyboardCursor ?? currentPick
    return {
      pixelX: Math.min(
        activeSource.space.widthPx - 1,
        Math.max(0, candidate?.pixelX ?? Math.floor(activeSource.space.widthPx / 2)),
      ),
      pixelY: Math.min(
        activeSource.space.heightPx - 1,
        Math.max(0, candidate?.pixelY ?? Math.floor(activeSource.space.heightPx / 2)),
      ),
    }
  }

  const moveKeyboardCursor = (deltaX: number, deltaY: number) => {
    if (!activeSource) return
    const origin = keyboardCursorOrigin()
    if (!origin) return
    const next = {
      pixelX: Math.min(activeSource.space.widthPx - 1, Math.max(0, origin.pixelX + deltaX)),
      pixelY: Math.min(activeSource.space.heightPx - 1, Math.max(0, origin.pixelY + deltaY)),
    }
    setKeyboardCursor(next)
    setKeyboardAnnouncement(`Cursor pixel ${next.pixelX}, ${next.pixelY}.`)
  }

  const selectKeyboardCursor = () => {
    if (!activeSource || !projection) return
    const cursor = keyboardCursorOrigin()
    if (!cursor) return
    setKeyboardCursor(cursor)
    const rasterX = cursor.pixelX + 0.5
    const rasterY = cursor.pixelY + 0.5
    const normalizedX = rasterX / activeSource.space.widthPx
    const normalizedY = rasterY / activeSource.space.heightPx
    const result: EditorCanvasPick = {
      view: activeView,
      screenX: projection.originX + rasterX * projection.scale,
      screenY: projection.originY + rasterY * projection.scale,
      rasterX,
      rasterY,
      pixelX: cursor.pixelX,
      pixelY: cursor.pixelY,
      normalizedX,
      normalizedY,
      physicalXmm: normalizedX * activeSource.space.widthMm,
      physicalYmm: normalizedY * activeSource.space.heightMm,
    }
    if (selectedPick === undefined) setInternalPick(result)
    onPick?.(result)
    setKeyboardAnnouncement(
      `Selected pixel ${cursor.pixelX}, ${cursor.pixelY} at ${result.physicalXmm.toFixed(2)}, ${result.physicalYmm.toFixed(2)} millimetres.`,
    )
  }

  const keyboardCanvas = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return
    const panStep = event.shiftKey ? 60 : 20
    const cursorStep = event.shiftKey ? 10 : 1
    if (event.key === '+' || event.key === '=') zoomAt(camera.zoom * 1.25)
    else if (event.key === '-') zoomAt(camera.zoom / 1.25)
    else if (event.key === '0' || event.key.toLowerCase() === 'f') fit()
    else if (event.key.toLowerCase() === 'i') setTool('inspect')
    else if (event.key.toLowerCase() === 'h') setTool('pan')
    else if (event.key.toLowerCase() === 'r' && !selectionDisabled) setTool('rectangle')
    else if (event.key.toLowerCase() === 'l' && !selectionDisabled) setTool('lasso')
    else if (event.key.toLowerCase() === 'b' && !selectionDisabled) setTool('brush')
    else if (tool === 'inspect' && event.key === 'ArrowLeft') moveKeyboardCursor(-cursorStep, 0)
    else if (tool === 'inspect' && event.key === 'ArrowRight') moveKeyboardCursor(cursorStep, 0)
    else if (tool === 'inspect' && event.key === 'ArrowUp') moveKeyboardCursor(0, -cursorStep)
    else if (tool === 'inspect' && event.key === 'ArrowDown') moveKeyboardCursor(0, cursorStep)
    else if (tool === 'inspect' && (event.key === 'Enter' || event.key === ' ')) selectKeyboardCursor()
    else if (tool === 'pan' && event.key === 'ArrowLeft') changeCamera(panEditorCanvas(camera, { x: panStep, y: 0 }))
    else if (tool === 'pan' && event.key === 'ArrowRight') changeCamera(panEditorCanvas(camera, { x: -panStep, y: 0 }))
    else if (tool === 'pan' && event.key === 'ArrowUp') changeCamera(panEditorCanvas(camera, { x: 0, y: panStep }))
    else if (tool === 'pan' && event.key === 'ArrowDown') changeCamera(panEditorCanvas(camera, { x: 0, y: -panStep }))
    else return
    event.preventDefault()
  }

  const renderRisks = (source: EditorCanvasViewSource) => {
    if (regionAssignment && riskMask && riskGraph && riskReport) {
      return (
        <RiskOverlayCanvas
          source={source}
          assignment={regionAssignment}
          mask={riskMask}
          graph={riskGraph}
          report={riskReport}
          mode={riskMode}
          severity={riskSeverity}
          selectedRiskId={selectedRiskId}
        />
      )
    }
    const selectedRisk = source.risks?.find((risk) => risk.id === selectedRiskId)
    if (!selectedRisk) return null
    const rasterBounds = [
      ...selectedRisk.boundsPx,
      ...selectedRisk.boundsMm.map((bounds) =>
        editorCanvasPhysicalBoundsToRaster(bounds, source.space),
      ),
    ]
    if (!rasterBounds.length) return null
    return (
      <svg
        className="i23-canvas-risks"
        data-presentation={selectedRisk.presentation}
        viewBox={`0 0 ${source.space.widthPx} ${source.space.heightPx}`}
        preserveAspectRatio="none"
        aria-label={selectedRisk.presentation === 'region-extents'
          ? `Per-region pixel extents for ${selectedRisk.title}; not exact region shapes or masks`
          : `Approximate affected-area bounds for ${selectedRisk.title}; not an exact pixel mask`}
      >
        {rasterBounds.map((raster, index) => (
          <rect
              key={`${selectedRisk.id}-${index}`}
              data-risk-id={selectedRisk.id}
              data-severity={selectedRisk.severity}
              data-selected="true"
              data-presentation={selectedRisk.presentation}
              x={raster.x}
              y={raster.y}
              width={raster.width}
              height={raster.height}
              vectorEffect="non-scaling-stroke"
              onClick={(event) => {
                event.stopPropagation()
                onRiskSelect?.(selectedRisk.id)
              }}
            >
              <title>{selectedRisk.title}</title>
            </rect>
        ))}
      </svg>
    )
  }

  const renderSelectedRegion = (source: EditorCanvasViewSource) => {
    const savedPrimitives = canvasSelection?.primitives ?? []
    if (
      !selectedRegion
      && !operationPreviewRegions.length
      && !savedPrimitives.length
      && !selectionDraft
      && !selectionRegionBounds.length
    ) return null
    const toWorking = (point: SourceSelectionPoint) => canonicalTransform
      ? normalizedSourcePointToWorking(point, canonicalTransform)
      : point
    const pointsAttribute = (points: SourceSelectionPoint[]) =>
      points.map((point) => {
        const working = toWorking(point)
        return `${working.x},${working.y}`
      }).join(' ')
    const brushWidth = (radiusMm: number) =>
      Math.max(0.5, (radiusMm * 2 * source.space.widthPx) / source.space.widthMm)
    return (
      <svg
        className="i23-canvas-region-selection"
        viewBox={`0 0 ${source.space.widthPx} ${source.space.heightPx}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        {savedPrimitives.map((primitive) => {
          if (primitive.kind === 'rectangle') {
            const start = toWorking({ x: primitive.x, y: primitive.y })
            const end = toWorking({
              x: primitive.x + primitive.width,
              y: primitive.y + primitive.height,
            })
            return (
              <rect
                key={primitive.primitive_id}
                data-canvas-selection="true"
                data-combine={primitive.combine}
                x={Math.min(start.x, end.x)}
                y={Math.min(start.y, end.y)}
                width={Math.abs(end.x - start.x)}
                height={Math.abs(end.y - start.y)}
                vectorEffect="non-scaling-stroke"
              />
            )
          }
          if (primitive.kind === 'lasso') return (
            <polygon
              key={primitive.primitive_id}
              data-canvas-selection="true"
              data-combine={primitive.combine}
              points={pointsAttribute(primitive.points)}
              vectorEffect="non-scaling-stroke"
            />
          )
          if (primitive.kind === 'brush') return (
            <polyline
              key={primitive.primitive_id}
              data-canvas-selection="true"
              data-combine={primitive.combine}
              points={pointsAttribute(primitive.points)}
              strokeWidth={brushWidth(primitive.radius_mm)}
              vectorEffect="non-scaling-stroke"
            />
          )
          return null
        })}
        {selectionRegionBounds.map((region) => (
          <rect
            key={`selection-region-${region.id}`}
            data-canvas-selection="true"
            data-combine="region"
            x={region.x}
            y={region.y}
            width={region.width}
            height={region.height}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {selectionDraft && selectionDraft.points.length ? (() => {
          const first = toWorking(selectionDraft.points[0])
          const last = toWorking(selectionDraft.points[selectionDraft.points.length - 1])
          if (selectionDraft.tool === 'rectangle') return (
            <rect
              data-selection-draft="true"
              x={Math.min(first.x, last.x)}
              y={Math.min(first.y, last.y)}
              width={Math.abs(last.x - first.x)}
              height={Math.abs(last.y - first.y)}
              vectorEffect="non-scaling-stroke"
            />
          )
          if (selectionDraft.tool === 'lasso') return (
            <polyline
              data-selection-draft="true"
              points={pointsAttribute(selectionDraft.points)}
              vectorEffect="non-scaling-stroke"
            />
          )
          return (
            <polyline
              data-selection-draft="true"
              points={pointsAttribute(selectionDraft.points)}
              strokeWidth={brushWidth(brushRadiusMm)}
              vectorEffect="non-scaling-stroke"
            />
          )
        })() : null}
        {operationPreviewRegions.map((region) => (
          <rect
            key={`operation-${region.id}`}
            data-operation-preview="true"
            data-region-id={region.id}
            x={region.x}
            y={region.y}
            width={region.width}
            height={region.height}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {selectedRegion ? (
          <rect
            data-selected-region="true"
            data-region-id={selectedRegion.id}
            x={selectedRegion.x}
            y={selectedRegion.y}
            width={selectedRegion.width}
            height={selectedRegion.height}
            vectorEffect="non-scaling-stroke"
          />
        ) : null}
      </svg>
    )
  }

  const renderPlane = (
    source: EditorCanvasViewSource,
    role: 'primary' | 'secondary',
    visible: boolean,
  ) => {
    if (!projection) return null
    const planeStyle = {
      width: `${source.space.widthPx}px`,
      height: `${source.space.heightPx}px`,
      transform: `translate3d(${projection.originX}px, ${projection.originY}px, 0) scale(${projection.scale})`,
      clipPath:
        role === 'secondary' && comparisonMode === 'split'
          ? `inset(0 ${100 - comparison.splitPercent}% 0 0)`
          : undefined,
    } as CSSProperties
    return (
      <div
        className="i23-canvas-plane"
        data-plane={role}
        data-visible={visible}
        data-has-risk-actions={source.view === 'risk' && Boolean(onRiskSelect)}
        style={planeStyle}
        aria-hidden={!visible}
      >
        <img
          src={source.raster.url}
          alt={`${source.label} canvas view`}
          width={source.space.widthPx}
          height={source.space.heightPx}
          draggable={false}
          data-artifact-kind={source.raster.artifactKind}
          data-interpolation={source.raster.interpolation ?? 'nearest'}
          onError={() =>
            setLoadFailures((current) => ({
              ...current,
              [source.raster.url]: `${source.label} pixels could not be loaded.`,
            }))
          }
          onLoad={() =>
            setLoadFailures((current) => {
              if (!current[source.raster.url]) return current
              const next = { ...current }
              delete next[source.raster.url]
              return next
            })
          }
        />
        {source.view === 'risk' ? renderRisks(source) : null}
        {role === 'primary' ? renderSelectedRegion(source) : null}
        {pixelGrid && projection.scale >= 6 ? <span className="i23-canvas-pixel-grid" /> : null}
        {currentPick && role === 'primary' ? (
          <span
            className="i23-canvas-pick-marker"
            style={{
              left: `${currentPick.pixelX + 0.5}px`,
              top: `${currentPick.pixelY + 0.5}px`,
            }}
          />
        ) : null}
      </div>
    )
  }

  const loadFailure = activeSource ? loadFailures[activeSource.raster.url] : undefined
  const sourceMessage = activeSource ? sourceStateMessage(activeSource) ?? loadFailure : null
  const sourceError = activeSource?.state === 'error' || Boolean(loadFailure)
  const selectedRisk = activeSource?.view === 'risk'
    ? activeSource.risks?.find((risk) => risk.id === selectedRiskId) ?? null
    : null
  const exactRiskEvidence = Boolean(regionAssignment && riskMask && riskGraph && riskReport)
  const selectedRiskWarning = riskReport?.warnings.find((warning) => warning.id === selectedRiskId) ?? null
  const selectedRiskHasExactRegions = Boolean(
    selectedRiskWarning
      && riskGraph
      && warningRegionIndices(riskGraph, selectedRiskWarning).size > 0,
  )
  const visiblePrimary = comparisonMode !== 'flicker' || flickerPaused || !flickerSecondary
  const visibleSecondary = comparisonMode === 'split' || (comparisonMode === 'flicker' && flickerSecondary)
  const rootClass = ['i23-editor-canvas-viewer', className].filter(Boolean).join(' ')
  const helpId = `i23-canvas-help-${generatedId}`
  const readinessId = `i23-canvas-readiness-${generatedId}`
  const viewHelpId = `i23-view-help-${generatedId}`
  const toolHelpId = `i23-tool-help-${generatedId}`
  const compareHelpId = `i23-compare-help-${generatedId}`
  const toolHelp: Record<EditorCanvasTool, string> = {
    inspect: 'Click a color region to inspect it. Scroll to zoom; Fit brings the whole image back.',
    pan: 'Drag to move around the image. Use Fit to return to the whole artwork.',
    rectangle: 'Drag a box to select an area for a local edit. Selecting does not change the image.',
    lasso: 'Draw around an area to select it for a local edit. Selecting does not change the image.',
    brush: 'Paint a selection for a local edit. This selects pixels; it does not paint a new color.',
  }
  const compareHelp = comparisonMode === 'split'
    ? 'The reference is on the left; your selected view is on the right. Move the divider to compare details.'
    : comparisonMode === 'flicker'
      ? 'Alternates the two views in the same position so changes stand out. Pause whenever you need.'
      : 'Show one view. Choose Split or Flicker to compare it with the reference.'
  const artworkOffscreen = projection && (
    projection.originX + projection.displayWidth <= 0 || projection.originY + projection.displayHeight <= 0
    || projection.originX >= viewport.width || projection.originY >= viewport.height
  )
  const selectionAvailable = Boolean(
    canonicalTransform && canvasSelection && onCanvasSelectionChange,
  )

  const addSelectedRegion = () => {
    if (!canvasSelection || !selectedRegionId) return
    commitSelection(
      appendSelectionPrimitive(canvasSelection, {
        kind: 'regions',
        primitive_id: selectionPrimitiveId(),
        combine: selectionCombine,
        region_ids: [selectedRegionId],
      }),
      `${selectionCombine === 'add' ? 'Add' : 'Subtract'} exact region selection`,
    )
  }

  const changeSelectionEdge = (kind: 'expand' | 'feather', raw: string) => {
    if (!canvasSelection) return
    const value = Number(raw)
    try {
      const next = updateSelectionEdge(
        canvasSelection,
        kind === 'expand' ? { expandMm: value } : { featherMm: value },
      )
      commitSelection(next, `Change selection ${kind}`)
    } catch (error) {
      setKeyboardAnnouncement(error instanceof Error ? error.message : `Invalid ${kind} value.`)
    }
  }

  return (
    <section className={rootClass} aria-label={ariaLabel}>
      <div className="i23-canvas-toolbar">
        <fieldset className="i23-canvas-view-switcher">
          <legend>Canvas view</legend>
          <div>
            {EDITOR_CANVAS_VIEWS.map((option) => {
              const available = Boolean(catalog.sources[option.id])
              const unavailableId = `${readinessId}-${option.id}`
              return (
                <button
                  key={option.id}
                  type="button"
                  aria-label={option.shortLabel}
                  aria-pressed={activeView === option.id}
                  aria-disabled={!available}
                  aria-describedby={!available ? unavailableId : activeView === option.id ? viewHelpId : undefined}
                  title={available ? option.description : catalog.unavailable[option.id]}
                  onClick={() => changeView(option.id)}
                >
                  {option.shortLabel}
                  {!available ? (
                    <span id={unavailableId} className="i23-canvas-sr-only">
                      {catalog.unavailable[option.id]}
                    </span>
                  ) : null}
                </button>
              )
            })}
          </div>
        </fieldset>

        <div className="i23-canvas-view-context">
          <p id={viewHelpId}><strong>{editorCanvasViewMetadata(activeView).label}.</strong> {activeSource?.description ?? editorCanvasViewMetadata(activeView).description}</p>
          <details className="i23-canvas-guide">
            <summary>View guide</summary>
            <div className="i23-canvas-guide-body">
              <dl>{EDITOR_CANVAS_VIEWS.map((option) => (
                <div key={option.id}><dt>{option.shortLabel}</dt><dd>{option.description}{!catalog.sources[option.id] ? <span className="i23-canvas-guide-availability">{catalog.unavailable[option.id]}</span> : null}</dd></div>
              ))}</dl>
              <p><strong>Working with a photo?</strong> Compare Quantized with Processed to see what cleanup changed. If specks remain, check the automatic island policy: Review only flags small regions; it does not merge them. Zoom into faces and edges before increasing cleanup. A broad lens flare is part of the source photo and may need a local edit rather than small-island cleanup.</p>
              <p className="i23-canvas-output-readiness" id={readinessId}><strong>From preview to print.</strong> Build, validate, and download the 3MF from Output readiness. Geometry and Sliced views appear after the build. Compare solid artwork with extrusion paths; thin details can leave gaps that expose the backing.</p>
              <p><strong>Keyboard:</strong> Focus the image, then use + / − to zoom, F to fit, I to inspect, H to pan, R for rectangle, L for lasso, or B for brush. Arrow keys move the pixel cursor in Inspect or move the image in Pan.</p>
            </div>
          </details>
        </div>
        {unavailableView ? <p className="i23-canvas-unavailable-hint" role="status">{catalog.unavailable[unavailableView]}</p> : null}
      </div>

      <div className="i23-canvas-interaction-bar">
        <div className="i23-canvas-tools" role="group" aria-label="Canvas tools" aria-describedby={toolHelpId}>
          <button type="button" title="Inspect a color region (I)" aria-pressed={tool === 'inspect'} onClick={() => setTool('inspect')}>
            Inspect
          </button>
          <button type="button" title="Move around the image (H)" aria-pressed={tool === 'pan'} onClick={() => setTool('pan')}>
            Pan
          </button>
          {(['rectangle', 'lasso', 'brush'] as const).map((selectionTool) => (
            <button
              key={selectionTool}
              type="button"
              aria-pressed={tool === selectionTool}
              title={toolHelp[selectionTool]}
              disabled={!selectionAvailable || selectionDisabled}
              onClick={() => setTool(selectionTool)}
            >
              {selectionTool[0].toUpperCase() + selectionTool.slice(1)}
            </button>
          ))}
        </div>
        <div className="i23-canvas-zoom" role="group" aria-label="Zoom and display">
          <button type="button" onClick={() => zoomAt(camera.zoom / 1.25)} aria-label="Zoom out">
            −
          </button>
          <output aria-label="Zoom level">{Math.round(camera.zoom * 100)}%</output>
          <button type="button" onClick={() => zoomAt(camera.zoom * 1.25)} aria-label="Zoom in">
            +
          </button>
          <button type="button" onClick={fit} aria-label="Fit artwork to canvas" title="Show the whole artwork (F)">
            Fit
          </button>
          <button
            type="button"
            aria-pressed={pixelGrid}
            title="Show pixel boundaries when zoomed in far enough"
            onClick={() => setPixelGrid((current) => !current)}
          >
            Pixel grid
          </button>
        </div>
        <p id={toolHelpId} className="i23-canvas-tool-help">{toolHelp[tool]}{pixelGrid && projection && projection.scale < 6 ? ' Zoom in further to see the pixel grid.' : ''}</p>
      </div>

      {selectionAvailable ? (
        <details className="i23-canvas-selection-options" open={['rectangle', 'lasso', 'brush'].includes(tool) ? true : undefined}>
          <summary>Selection options <span>For local edits</span></summary>
          <p className="i23-canvas-selection-help">Add includes more of the image; Subtract removes part of your selection. Expand grows or shrinks its edge. Feather softens the edge of supported local edits. These controls do not change the artwork by themselves.</p>
        <div className="i23-canvas-selection-bar" aria-label="Selection controls">
          <label>
            <span>Selection mode</span>
            <select
              value={selectionCombine}
              disabled={selectionDisabled}
              onChange={(event) => setSelectionCombine(event.target.value as SelectionCombineMode)}
            >
              <option value="add">Add</option>
              <option value="subtract">Subtract</option>
            </select>
          </label>
          <label>
            <span>Brush radius</span>
            <input
              title="Size of the selection brush in print millimetres"
              type="number"
              min="0.1"
              max="100"
              step="0.1"
              value={brushRadiusMm}
              disabled={selectionDisabled}
              onChange={(event) => {
                const value = Number(event.target.value)
                if (Number.isFinite(value) && value >= 0.1 && value <= 100) setBrushRadiusMm(value)
              }}
            />
            <span>mm</span>
          </label>
          <label>
            <span>Expand</span>
            <input
              key={`expand-${canvasSelection?.expand_mm}`}
              type="number"
              min="-100"
              max="100"
              step="0.1"
              defaultValue={canvasSelection?.expand_mm ?? 0}
              disabled={selectionDisabled}
              onBlur={(event) => changeSelectionEdge('expand', event.target.value)}
            />
            <span>mm</span>
          </label>
          <label>
            <span>Feather</span>
            <input
              key={`feather-${canvasSelection?.feather_mm}`}
              type="number"
              min="0"
              max="100"
              step="0.1"
              defaultValue={canvasSelection?.feather_mm ?? 0}
              disabled={selectionDisabled}
              onBlur={(event) => changeSelectionEdge('feather', event.target.value)}
            />
            <span>mm</span>
          </label>
          <button
            type="button"
            disabled={selectionDisabled || !selectedRegionId}
            onClick={addSelectedRegion}
          >
            {selectionCombine === 'add' ? 'Add' : 'Subtract'} selected region
          </button>
          <button
            type="button"
            disabled={selectionDisabled || !canvasSelection?.primitives.length}
            onClick={() => canvasSelection && commitSelection(
              { ...canvasSelection, primitives: [] },
              'Clear canvas selection',
            )}
          >
            Clear
          </button>
          <output aria-live="polite">
            {canvasSelection?.primitives.length ?? 0} saved step{canvasSelection?.primitives.length === 1 ? '' : 's'}
          </output>
        </div>
        </details>
      ) : null}

      <div className="i23-canvas-compare-bar" role="group" aria-label="Compare views" aria-describedby={compareHelpId}>
        <label>
          <span>Reference</span>
          <select
            value={comparison.secondaryView}
            onChange={(event) =>
              changeComparison({ secondaryView: event.target.value as EditorCanvasViewId })
            }
          >
            {EDITOR_CANVAS_VIEWS.filter((option) => catalog.sources[option.id]).map((option) => (
              <option key={option.id} value={option.id} disabled={option.id === activeView}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <div className="i23-canvas-compare-modes" aria-label="Comparison mode">
          {(['single', 'split', 'flicker'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              aria-pressed={comparisonMode === mode}
              title={mode === 'single' ? 'Show only the selected view' : mode === 'split' ? 'Compare with a movable divider' : 'Alternate between the two views'}
              disabled={mode !== 'single' && !comparisonReady}
              onClick={() => changeComparison({ mode })}
            >
              {mode === 'single' ? 'Single' : mode === 'split' ? 'Split' : 'Flicker'}
            </button>
          ))}
        </div>
        {comparisonMode === 'split' ? (
          <label className="i23-canvas-split-control">
            <span>Divider</span>
            <input
              type="range"
              min="5"
              max="95"
              value={comparison.splitPercent}
              onChange={(event) => changeComparison({ splitPercent: Number(event.target.value) })}
            />
            <output>{comparison.splitPercent}%</output>
          </label>
        ) : null}
        {comparisonMode === 'flicker' ? (
          <button
            className="i23-canvas-flicker-toggle"
            type="button"
            aria-pressed={flickerPaused}
            aria-disabled={reducedMotion}
            title={reducedMotion ? 'Your reduced-motion preference keeps flicker paused.' : undefined}
            onClick={() => {
              if (!reducedMotion) setFlickerPausedByUser((current) => !current)
            }}
          >
            {reducedMotion
              ? 'Flicker paused (reduced motion)'
              : flickerPaused
                ? 'Resume flicker'
                : 'Pause flicker'}
          </button>
        ) : null}
        {!comparisonReady && comparison.mode !== 'single' ? (
          <span className="i23-canvas-compare-warning" role="status">
            Choose two different views of the same image to compare.
          </span>
        ) : null}
        <p id={compareHelpId} className="i23-canvas-compare-help">{compareHelp}</p>
      </div>

      {activeSource?.view === 'risk' ? (
        <div className="i23-canvas-risk-guidance">
          {exactRiskEvidence ? (
            <>
              <fieldset className="i23-canvas-risk-modes">
                <legend>Risk overlay mode</legend>
                {(['selected', 'severity', 'overview'] as const).map((mode) => (
                  <button key={mode} type="button" aria-pressed={riskMode === mode} onClick={() => setRiskMode(mode)}>
                    {mode === 'selected' ? 'Selected' : mode === 'severity' ? 'Severity' : 'Overview'}
                  </button>
                ))}
                {riskMode === 'severity' ? (
                  <label>
                    <span>Level</span>
                    <select value={riskSeverity} onChange={(event) => setRiskSeverity(event.target.value as RiskSeverity)}>
                      <option value="error">Error</option>
                      <option value="warning">Warning</option>
                      <option value="info">Info</option>
                    </select>
                  </label>
                ) : null}
              </fieldset>
              <p className="i23-canvas-risk-mode-help">{riskMode === 'selected'
                ? 'Selected shows one warning. Pick a finding in Inspect regions & repair details to highlight it here.'
                : riskMode === 'severity'
                  ? 'Severity shows regions linked to warnings at the chosen level.'
                  : 'Overview shows regions linked to all warnings. The strongest severity is shown where they overlap.'}</p>
              <details className="i23-canvas-risk-details"><summary>How to read the overlay</summary>
              <span className="i23-canvas-risk-legend">
                <i aria-hidden="true" /> Texture covers the exact pixels in a finding’s linked
                {' '}regions; it does not claim to trace the defect itself.
                {' '}<b aria-hidden="true" /> Dashed outlines are approximate feature bounds.
                {' '}<strong>{riskMask?.exactWarningCount ?? 0} region-backed</strong>
                {' '}finding{riskMask?.exactWarningCount === 1 ? '' : 's'} {riskMask?.exactWarningCount === 1 ? 'appears' : 'appear'} in Overview and Severity.
                {' '}<strong>{riskMask?.approximateWarningCount ?? 0} bounds-only</strong>
                {' '}finding{riskMask?.approximateWarningCount === 1 ? '' : 's'} {riskMask?.approximateWarningCount === 1 ? 'is' : 'are'} excluded from those
                {' '}exact mask modes; select one to see its dashed bounds.
                {riskMode === 'selected' && selectedRiskWarning
                  ? selectedRiskHasExactRegions
                    ? ` ${selectedRiskWarning.title} uses exact linked-region pixels.`
                    : ` ${selectedRiskWarning.title} uses approximate dashed bounds because no exact region membership is available.`
                  : riskMode === 'overview'
                    ? ' Highest severity wins where findings overlap.'
                    : ''}
              </span>
              </details>
            </>
          ) : selectedRisk ? (
            <>
              {selectedRisk.presentation === 'list-only' ? (
                <>
                  <strong>{selectedRisk.title}</strong> affects {selectedRisk.affectedRegionCount || 'many'}
                  {' '}scattered regions. Canvas rectangles are intentionally hidden because a combined
                  extent or box storm would obscure the artwork; inspect the exact regions in the finding list.
                </>
              ) : selectedRisk.presentation === 'region-extents' ? (
                <>
                  Showing {selectedRisk.boundsPx.length} per-region pixel extents for <strong>{selectedRisk.title}</strong>.
                  {' '}Each dashed rectangle marks a region’s exact outer bounds, not its pixel shape or a mask.
                </>
              ) : (
                <>
                  Showing approximate affected-area bounds for <strong>{selectedRisk.title}</strong>;
                  {' '}these dash-pattern rectangles are not exact pixel masks.
                </>
              )}
            </>
          ) : (
            <>Choose a finding in the inspection rail to show its approximate affected-area bounds. No warning boxes are drawn until one is selected.</>
          )}
        </div>
      ) : null}

      {/* The canvas is a documented composite application widget with keyboard camera controls. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        className="i23-canvas-viewport"
        ref={setViewportElement}
        tabIndex={0}
        role="application"
        aria-label={`${activeSource?.label ?? 'Unavailable'} image canvas`}
        aria-describedby={helpId}
        data-tool={tool}
        onKeyDown={keyboardCanvas}
        onPointerDown={pointerDown}
        onPointerMove={pointerMove}
        onPointerUp={pointerUp}
        onPointerCancel={pointerCancel}
        onClick={clickCanvas}
        onWheel={wheelCanvas}
      >
        <p id={helpId} className="i23-canvas-sr-only">
          Use plus and minus to zoom and F or zero to fit. Press I for Inspect: arrow keys move
          the exact pixel cursor, Shift plus an arrow moves ten pixels, and Enter or Space selects.
          Press H for Pan: arrow keys move the artwork and Shift moves farther. Press R for
          Rectangle, L for Lasso, or B for Brush selection. Selection coordinates are saved against
          the normalized source, independent of pan and zoom.
        </p>
        {activeSource && projection ? renderPlane(activeSource, 'primary', visiblePrimary) : null}
        {comparisonReady && secondarySource && projection
          ? renderPlane(secondarySource, 'secondary', visibleSecondary)
          : null}
        {comparisonMode === 'split' && projection ? (
          <span
            className="i23-canvas-split-line"
            style={{
              left: `${projection.originX + projection.displayWidth * (comparison.splitPercent / 100)}px`,
              top: `${projection.originY}px`,
              height: `${projection.displayHeight}px`,
            }}
            aria-hidden="true"
          />
        ) : null}
        {keyboardCursor && projection ? (
          <span
            className="i23-canvas-keyboard-cursor"
            style={{
              left: `${projection.originX + (keyboardCursor.pixelX + 0.5) * projection.scale}px`,
              top: `${projection.originY + (keyboardCursor.pixelY + 0.5) * projection.scale}px`,
            }}
            aria-hidden="true"
          />
        ) : null}
        {!exactRiskEvidence && selectedRisk && selectedRisk.presentation !== 'list-only' ? (
          <span className="i23-canvas-risk-key" data-presentation={selectedRisk.presentation}>
            {selectedRisk.presentation === 'region-extents'
              ? 'Dashed boxes · per-region extents, not masks'
              : 'Dash-pattern boxes · approximate bounds, not masks'}
          </span>
        ) : null}
        {activeSource && artworkOffscreen && !sourceMessage ? (
          <div className="i23-canvas-message" role="status">
            <strong>The artwork is outside this view</strong>
            <p>It is still here. Reset the zoom and position to see the whole image.</p>
            <button type="button" onPointerDown={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); fit() }}>Bring artwork into view</button>
          </div>
        ) : null}
        {!activeSource ? (
          <div className="i23-canvas-message" role="status">
            <strong>No canvas image is available</strong>
            <p>Render a preview or choose a stage with an image artifact.</p>
          </div>
        ) : null}
        {sourceMessage ? (
          <div
            className="i23-canvas-message"
            data-kind={sourceError ? 'error' : activeSource?.state}
            role={sourceError ? 'alert' : 'status'}
          >
            <strong>{sourceError ? 'Pixel source unavailable' : sourceMessage}</strong>
            {sourceError ? <p>{sourceMessage}</p> : null}
          </div>
        ) : null}
      </div>

      <footer className="i23-canvas-statusbar">
        <span>{activeSource ? `${editorCanvasViewMetadata(activeView).label} view` : 'No source selected.'}</span>
        {activeSource ? (
          <span>
            {activeSource.space.widthPx} × {activeSource.space.heightPx} px ·{' '}
            {activeSource.space.widthMm} × {activeSource.space.heightMm} mm
          </span>
        ) : null}
        <output aria-label="Selected canvas coordinate">
          {currentPick
            ? `${editorCanvasViewMetadata(currentPick.view).shortLabel} · Pixel ${currentPick.pixelX}, ${currentPick.pixelY} · ${currentPick.physicalXmm.toFixed(2)}, ${currentPick.physicalYmm.toFixed(2)} mm`
            : 'No pixel selected'}
        </output>
        <output className="i23-canvas-sr-only" aria-live="polite" aria-atomic="true">
          {keyboardAnnouncement}
        </output>
      </footer>
    </section>
  )
}
