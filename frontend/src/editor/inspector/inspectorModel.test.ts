import { describe, expect, it } from 'vitest'
import type { RegionGraph, RiskReport } from '../../contracts'
import {
  firstAffectedRegion,
  measurementValue,
  regionCenterPick,
  regionTopology,
  risksForRegion,
} from './inspectorModel'

const graph: RegionGraph = {
  schema_version: 1,
  width_px: 100,
  height_px: 50,
  width_mm: 200,
  height_mm: 100,
  pixel_width_mm: 2,
  pixel_height_mm: 2,
  active_pixel_count: 360,
  palette: [
    { label: 0, color: '#111111' },
    { label: 1, color: '#FF6600' },
  ],
  regions: [
    {
      id: 'region_aaaaaaaaaaaaaaaaaaaaaaaa',
      label: 0,
      color: '#111111',
      pixel_count: 300,
      area_mm2: 1200,
      perimeter_edge_count: 80,
      perimeter_mm: 160,
      compactness: 0.589,
      pixel_bounds: { x: 0, y: 0, width: 30, height: 20 },
      physical_bounds: { x_mm: 0, y_mm: 0, width_mm: 60, height_mm: 40 },
      border_contact: {
        top_mm: 60,
        right_mm: 0,
        bottom_mm: 0,
        left_mm: 40,
        total_mm: 100,
        touches_canvas_border: true,
      },
      width_estimate: {
        method: 'orthogonal-run-spans-v1',
        minimum_mm: 2,
        median_mm: 14,
        p95_mm: 30,
        maximum_mm: 40,
      },
      neighbor_region_ids: ['region_bbbbbbbbbbbbbbbbbbbbbbbb'],
    },
    {
      id: 'region_bbbbbbbbbbbbbbbbbbbbbbbb',
      label: 1,
      color: '#FF6600',
      pixel_count: 60,
      area_mm2: 240,
      perimeter_edge_count: 34,
      perimeter_mm: 68,
      compactness: 0.652,
      pixel_bounds: { x: 10, y: 5, width: 10, height: 8 },
      physical_bounds: { x_mm: 20, y_mm: 10, width_mm: 20, height_mm: 16 },
      border_contact: {
        top_mm: 0,
        right_mm: 0,
        bottom_mm: 0,
        left_mm: 0,
        total_mm: 0,
        touches_canvas_border: false,
      },
      width_estimate: {
        method: 'orthogonal-run-spans-v1',
        minimum_mm: 2,
        median_mm: 8,
        p95_mm: 16,
        maximum_mm: 20,
      },
      neighbor_region_ids: ['region_aaaaaaaaaaaaaaaaaaaaaaaa'],
    },
  ],
  adjacency: [
    {
      first_region_id: 'region_aaaaaaaaaaaaaaaaaaaaaaaa',
      second_region_id: 'region_bbbbbbbbbbbbbbbbbbbbbbbb',
      boundary_edge_count: 8,
      boundary_length_mm: 16,
    },
  ],
}

const report: RiskReport = {
  schema_version: 1,
  graph_fingerprint: 'a'.repeat(64),
  options_fingerprint: 'b'.repeat(64),
  nozzle_mm: 0.4,
  evaluated_codes: ['small_island'],
  pending_codes: [],
  warnings: [
    {
      id: 'warning-small',
      code: 'small_island',
      feature_key: 'region_b',
      severity: 'warning',
      title: 'Small island',
      explanation: 'The island is below the configured physical threshold.',
      affected_region_ids: ['region_bbbbbbbbbbbbbbbbbbbbbbbb'],
      affected_labels: [1],
      affected_bounds: [{ x_mm: 20, y_mm: 10, width_mm: 20, height_mm: 16 }],
      measurements: [{ key: 'area', role: 'measured', value: 0.24, unit: 'mm2' }],
      suggestions: [],
      classifier_id: 'islands-v1',
      classifier_version: '1.0.0',
    },
  ],
  summary: { total: 1, info: 0, warning: 1, error: 0, by_code: [{ code: 'small_island', count: 1 }] },
}

describe('inspector model', () => {
  it('projects a list-selected region back to the shared canvas coordinate system', () => {
    expect(regionCenterPick(graph, graph.regions[1])).toMatchObject({
      view: 'processed',
      pixelX: 15,
      pixelY: 9,
      physicalXmm: 30,
      physicalYmm: 18,
    })
  })

  it('joins topology, adjacency, and risk findings without inventing derived contracts', () => {
    expect(regionTopology(graph, graph.regions[0])).toMatchObject({
      kind: 'canvas-edge',
      label: 'Touches canvas edge',
      adjacency: [{ boundary_length_mm: 16 }],
    })
    expect(risksForRegion(report, graph.regions[1].id).map((warning) => warning.id)).toEqual([
      'warning-small',
    ])
    expect(firstAffectedRegion(report.warnings[0], graph)).toBe(graph.regions[1].id)
  })

  it('formats physical risk measurements with explicit units', () => {
    expect(measurementValue({ key: 'minimum_width', role: 'measured', value: 0.38, unit: 'mm' })).toBe('0.38 mm')
    expect(measurementValue({ key: 'area', role: 'threshold', value: 0.5, unit: 'mm2' })).toBe('0.5 mm²')
  })
})
