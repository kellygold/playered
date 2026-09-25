import type { RegionGraph, RiskFinding, RiskMeasurement, RiskReport } from '../../contracts'
import type { EditorCanvasPick, EditorCanvasViewId } from '../canvas'

export type RegionNode = RegionGraph['regions'][number]
export type RegionAdjacency = RegionGraph['adjacency'][number]
export type RiskWarning = RiskReport['warnings'][number]

export type RegionTopology = {
  kind: 'canvas-edge' | 'interior'
  label: string
  adjacency: RegionAdjacency[]
}

export type RegionCanvasHighlight = {
  id: string
  x: number
  y: number
  width: number
  height: number
}

export function regionHighlight(region: RegionNode | null): RegionCanvasHighlight | null {
  if (!region) return null
  return {
    id: region.id,
    x: region.pixel_bounds.x,
    y: region.pixel_bounds.y,
    width: region.pixel_bounds.width,
    height: region.pixel_bounds.height,
  }
}

export function regionCenterPick(
  graph: RegionGraph,
  region: RegionNode,
  view: EditorCanvasViewId = 'processed',
): EditorCanvasPick {
  const rasterX = region.pixel_bounds.x + region.pixel_bounds.width / 2
  const rasterY = region.pixel_bounds.y + region.pixel_bounds.height / 2
  const normalizedX = rasterX / graph.width_px
  const normalizedY = rasterY / graph.height_px
  return {
    view,
    screenX: 0,
    screenY: 0,
    rasterX,
    rasterY,
    pixelX: Math.min(graph.width_px - 1, Math.floor(rasterX)),
    pixelY: Math.min(graph.height_px - 1, Math.floor(rasterY)),
    normalizedX,
    normalizedY,
    physicalXmm: normalizedX * graph.width_mm,
    physicalYmm: normalizedY * graph.height_mm,
  }
}

export function risksForRegion(report: RiskReport | null, regionId: string | null): RiskWarning[] {
  if (!report || !regionId) return []
  return report.warnings.filter((warning) => warning.affected_region_ids.includes(regionId))
}

export function firstAffectedRegion(
  warning: RiskWarning,
  graph: RegionGraph | null,
): string | null {
  if (!graph) return warning.affected_region_ids[0] ?? null
  const ids = new Set(graph.regions.map((region) => region.id))
  return warning.affected_region_ids.find((id) => ids.has(id)) ?? null
}

export function regionTopology(graph: RegionGraph, region: RegionNode): RegionTopology {
  const adjacency = graph.adjacency.filter(
    (edge) => edge.first_region_id === region.id || edge.second_region_id === region.id,
  )
  return {
    kind: region.border_contact.touches_canvas_border ? 'canvas-edge' : 'interior',
    label: region.border_contact.touches_canvas_border
      ? 'Touches canvas edge'
      : 'Enclosed interior component',
    adjacency,
  }
}

export function otherRegionId(edge: RegionAdjacency, regionId: string) {
  return edge.first_region_id === regionId ? edge.second_region_id : edge.first_region_id
}

export function riskCodeLabel(code: RiskFinding['code']) {
  return code.replaceAll('_', ' ').replace(/\b\w/g, (character) => character.toUpperCase())
}

const MEASUREMENT_LABELS: Record<RiskMeasurement['key'], string> = {
  area: 'Area',
  hole_area: 'Hole area',
  perimeter: 'Perimeter',
  compactness: 'Compactness',
  equivalent_diameter: 'Equivalent diameter',
  minimum_width: 'Minimum width',
  median_width: 'Median width',
  maximum_width: 'Maximum width',
  length: 'Length',
  gap_width: 'Gap width',
  component_count: 'Component count',
  components_per_100_mm2: 'Components per 100 mm²',
  pixel_count: 'Pixel count',
  coverage_ratio: 'Coverage ratio',
  nozzle_diameter: 'Nozzle diameter',
}

export function measurementLabel(measurement: RiskMeasurement) {
  return MEASUREMENT_LABELS[measurement.key]
}

export function measurementValue(measurement: RiskMeasurement) {
  const formatted = Number.isInteger(measurement.value)
    ? measurement.value.toLocaleString()
    : measurement.value.toLocaleString(undefined, { maximumFractionDigits: 3 })
  if (measurement.unit === 'mm') return `${formatted} mm`
  if (measurement.unit === 'mm2') return `${formatted} mm²`
  if (measurement.unit === 'count_per_100_mm2') return `${formatted} / 100 mm²`
  if (measurement.unit === 'ratio') return `${formatted} ratio`
  return formatted
}

export function shortRegionId(id: string) {
  return id.startsWith('region_') ? id.slice(7, 15) : id.slice(0, 8)
}
