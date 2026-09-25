import { useEffect, useRef } from 'react'
import type { RegionGraph, RiskReport } from '../../contracts'
import type { RegionAssignmentMap } from '../inspector/regionAssignment'
import { editorCanvasPhysicalBoundsToRaster, type EditorCanvasViewSource } from './canvasModel'
import {
  riskOverlayRgba,
  warningRegionIndices,
  type RiskMaskMap,
  type RiskMaskMode,
  type RiskSeverity,
} from './riskMask'

export type RiskOverlayCanvasProps = {
  source: EditorCanvasViewSource
  assignment: RegionAssignmentMap
  mask: RiskMaskMap
  graph: RegionGraph
  report: RiskReport
  mode: RiskMaskMode
  severity: RiskSeverity
  selectedRiskId?: string | null
}

export function RiskOverlayCanvas({
  source,
  assignment,
  mask,
  graph,
  report,
  mode,
  severity,
  selectedRiskId,
}: RiskOverlayCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const selected = report.warnings.find((warning) => warning.id === selectedRiskId) ?? null
  const selectedIsExact = selected ? warningRegionIndices(graph, selected).size > 0 : false

  useEffect(() => {
    const canvas = canvasRef.current
    const context = canvas?.getContext('2d', { alpha: true })
    if (!canvas || !context) return
    context.clearRect(0, 0, canvas.width, canvas.height)
    const rgba = riskOverlayRgba(mask, assignment, graph, report, {
      mode,
      selectedRiskId,
      severity,
    })
    const image = context.createImageData(mask.width, mask.height)
    image.data.set(rgba)
    context.putImageData(image, 0, 0)
    if (mode !== 'selected' || !selected || selectedIsExact) return
    context.save()
    context.strokeStyle = selected.severity === 'error' ? '#ff8178' : selected.severity === 'info' ? '#47dfb9' : '#f5bd68'
    context.lineWidth = Math.max(1, 2 / Math.max(1, source.space.widthPx / 1000))
    context.setLineDash([7, 4, 2, 4])
    for (const bounds of selected.affected_bounds) {
      const raster = editorCanvasPhysicalBoundsToRaster({
        xMm: bounds.x_mm,
        yMm: bounds.y_mm,
        widthMm: bounds.width_mm,
        heightMm: bounds.height_mm,
      }, source.space)
      context.strokeRect(raster.x, raster.y, raster.width, raster.height)
    }
    context.restore()
  }, [assignment, graph, mask, mode, report, selected, selectedIsExact, selectedRiskId, severity, source])

  const label = mode === 'selected'
    ? selected
      ? selectedIsExact
        ? `Exact region pixels linked to ${selected.title}; this is region membership, not a precise defect boundary`
        : `Approximate dashed bounds for ${selected.title}; no exact region membership is available`
      : 'Select a warning to show the exact pixels belonging to its affected regions'
    : mode === 'severity'
      ? `Exact region pixels linked to ${severity} findings`
      : 'Risk overview; color gives severity and texture gives exact affected-region membership'
  return (
    <canvas
      ref={canvasRef}
      className="i23-canvas-risks"
      width={mask.width}
      height={mask.height}
      role="img"
      aria-label={label}
      data-mode={mode}
      data-presentation={mode === 'selected' && selected && !selectedIsExact ? 'approximate' : 'exact'}
      data-risk-id={mode === 'selected' ? selected?.id : undefined}
    />
  )
}
