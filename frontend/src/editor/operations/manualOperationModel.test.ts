import { describe, expect, it } from 'vitest'
import type { HoleAnalysis, PaletteColor, RegionGraph } from '../../contracts'
import {
  buildManualOperation,
  DEFAULT_SIMILAR_REGION_PREDICATE,
  MAX_SCOPED_REGIONS,
  resolveManualOperation,
  resolveSimilarRegions,
  type ManualOperationContext,
  type ManualOperationDraft,
} from './manualOperationModel'

const ids = [
  'region_111111111111111111111111',
  'region_222222222222222222222222',
  'region_333333333333333333333333',
  'region_444444444444444444444444',
]

function node(index: number, overrides: Partial<RegionGraph['regions'][number]> = {}): RegionGraph['regions'][number] {
  return {
    id: ids[index], label: index === 3 ? 1 : 0, color: index === 3 ? '#FF6600' : '#112233',
    pixel_count: 10, area_mm2: 1, perimeter_edge_count: 12, perimeter_mm: 3,
    compactness: 0.5, pixel_bounds: { x: index * 3, y: 0, width: 2, height: 2 },
    physical_bounds: { x_mm: index * 0.6, y_mm: 0, width_mm: 0.4, height_mm: 0.4 },
    border_contact: { top_mm: 0, right_mm: 0, bottom_mm: 0, left_mm: 0, total_mm: 0, touches_canvas_border: false },
    width_estimate: { method: 'orthogonal-run-spans-v1', minimum_mm: 0.4, median_mm: 0.5, p95_mm: 0.6, maximum_mm: 0.7 },
    neighbor_region_ids: [], ...overrides,
  }
}

const graph: RegionGraph = {
  schema_version: 1, width_px: 20, height_px: 10, width_mm: 4, height_mm: 2,
  pixel_width_mm: 0.2, pixel_height_mm: 0.2, active_pixel_count: 40,
  palette: [{ label: 0, color: '#112233' }, { label: 1, color: '#FF6600' }],
  regions: [
    node(0, { neighbor_region_ids: [ids[3]] }),
    node(1, { area_mm2: 0.8, width_estimate: { method: 'orthogonal-run-spans-v1', minimum_mm: 0.31, median_mm: 0.4, p95_mm: 0.5, maximum_mm: 0.6 } }),
    node(2, { area_mm2: 0.7, compactness: 0.9 }),
    node(3, { neighbor_region_ids: [ids[0]] }),
  ],
  adjacency: [{ first_region_id: ids[0], second_region_id: ids[3], boundary_edge_count: 2, boundary_length_mm: 0.4 }],
}

const palette: PaletteColor[] = [
  { id: 'dark', name: 'Dark', hex: '#112233', locked: false, filament_id: null },
  { id: 'orange', name: 'Orange', hex: '#FF6600', locked: false, filament_id: null },
]

const holeAnalysis: HoleAnalysis = {
  schema_version: 1, graph_fingerprint: 'a'.repeat(64),
  options: { maximum_area_mm2: 1, maximum_equivalent_diameter_mm: 1, minimum_surviving_ring_width_mm: 0.4, maximum_ring_to_center_area_ratio: 8, error_ratio: 0.5 },
  options_fingerprint: 'b'.repeat(64),
  features: [
    {
      id: 'feature_111111111111111111111111', kind: 'tiny_hole', center_kind: 'active_region',
      center_component_id: ids[0], center_region_id: ids[0], center_label: 0,
      ring_region_id: ids[3], ring_label: 1, ring_pixel_count: 20,
      ring_pixel_bounds: { x: 0, y: 0, width: 4, height: 4 }, ring_bounds: { x_mm: 0, y_mm: 0, width_mm: 0.8, height_mm: 0.8 },
      outer_region_ids: [], outer_labels: [], triggers: ['area'], center_pixel_count: 2,
      center_area_mm2: 0.08, center_equivalent_diameter_mm: 0.32, center_perimeter_mm: 0.8,
      center_pixel_bounds: { x: 1, y: 1, width: 1, height: 2 }, center_bounds: { x_mm: 0.2, y_mm: 0.2, width_mm: 0.2, height_mm: 0.4 },
      ring_minimum_width_mm: 0.4, ring_median_width_mm: 0.5, ring_maximum_width_mm: 0.6,
      ring_area_mm2: 0.8, ring_to_center_area_ratio: 10, ring_perimeter_mm: 4,
    },
  ],
  summary: { feature_count: 1, tiny_hole_count: 1, hollow_ring_count: 0, transparent_center_count: 0 },
}

const context: ManualOperationContext = {
  graph, graphFingerprint: 'a'.repeat(64), configFingerprint: 'c'.repeat(64), palette, holeAnalysis,
  holeAnalysisFingerprint: 'd'.repeat(64),
}

function draft(overrides: Partial<ManualOperationDraft> = {}): ManualOperationDraft {
  return {
    action: 'protect', scopeMode: 'selected', selectedRegionId: ids[0],
    selectedHoleFeatureId: null, targetLabel: null, radiusMm: 0.2, editableLabels: [1],
    predicate: { ...DEFAULT_SIMILAR_REGION_PREDICATE }, ...overrides,
  }
}

describe('manual operation model', () => {
  it('resolves exact deterministic similarity using symmetric ratio deltas', () => {
    expect(resolveSimilarRegions(graph, [ids[0]], DEFAULT_SIMILAR_REGION_PREDICATE)).toEqual([ids[0], ids[1]])
    const resolution = resolveManualOperation(context, draft({ scopeMode: 'similar' }))
    expect(resolution.affectedRegionIds).toEqual([ids[0], ids[1]])
    expect(resolution.predicateLabel).toContain('same palette label (0)')
    expect(resolution.predicateLabel).toContain('area within 25%')
  })

  it('uses the same strict numerical boundary and persisted scope cap as replay', () => {
    const boundaryGraph = {
      ...graph,
      regions: [
        node(0),
        node(1, { area_mm2: 0.75 }),
        node(2, { area_mm2: 0.7499999999999999, compactness: 0.5 }),
      ],
      adjacency: [],
    }
    expect(resolveSimilarRegions(boundaryGraph, [ids[0]], DEFAULT_SIMILAR_REGION_PREDICATE)).toEqual([
      ids[0],
      ids[1],
    ])

    const repeated = Array.from({ length: MAX_SCOPED_REGIONS + 1 }, (_, index) =>
      node(index % ids.length, {
        id: `region_${index.toString(16).padStart(24, '0')}`,
        label: 0,
        color: '#112233',
        pixel_bounds: { x: index, y: 0, width: 1, height: 1 },
      }),
    )
    const capped = resolveManualOperation(
      { ...context, graph: { ...graph, regions: repeated, adjacency: [] } },
      draft({ selectedRegionId: repeated[0].id, scopeMode: 'similar' }),
    )
    expect(capped.valid).toBe(false)
    expect(capped.error).toContain('257 regions')
    expect(capped.error).toContain('256 or fewer')
  })

  it('rejects invalid targets, unsafe merges, and incomplete thickening without serializing', () => {
    expect(resolveManualOperation(context, draft({ action: 'recolor' })).error).toBe('Choose a target palette color.')
    expect(resolveManualOperation(context, draft({ action: 'merge', targetLabel: 1, scopeMode: 'similar' })).error).toBe('Every merged region must touch an unselected region in the target color.')
    expect(resolveManualOperation(context, draft({ action: 'thicken', editableLabels: [] })).error).toBe('Choose at least one replaceable palette label.')
  })

  it('serializes the frozen cross-language RegionScope fingerprint vector', async () => {
    const vectorGraph = {
      ...graph,
      regions: [node(0), node(1, { area_mm2: 1, width_estimate: { method: 'orthogonal-run-spans-v1', minimum_mm: 0.4, median_mm: 0.5, p95_mm: 0.6, maximum_mm: 0.7 } })],
      adjacency: [],
    }
    const vectorContext = { ...context, graph: vectorGraph }
    const vectorDraft = draft({
      scopeMode: 'similar',
      predicate: { area_ratio_tolerance: 0.25, minimum_width_ratio_tolerance: 0.5, compactness_tolerance: 0.125, match_border_contact: true, match_neighbor_labels: false },
    })
    const resolution = resolveManualOperation(vectorContext, vectorDraft)
    const operation = await buildManualOperation(vectorContext, vectorDraft, resolution, { commandId: 'cmd_goldenvector123456789012', createdAt: '2026-07-16T00:00:00.000Z' })
    const command = operation.parameters.command as { selector: { scope: { resolution_fingerprint: string } } }
    expect(command.selector.scope.resolution_fingerprint).toBe('1aa11bab4bcdc2555a4d827ed8d780f84819b07b8e6726d38ae2b98e4aebaff6')
  })

  it('preserves Pydantic float semantics at similarity-slider endpoints', async () => {
    const vectorContext = { ...context, graph: { ...graph, regions: [node(0)], adjacency: [] } }
    const vectorDraft = draft({
      scopeMode: 'similar',
      predicate: { area_ratio_tolerance: 0, minimum_width_ratio_tolerance: 1, compactness_tolerance: 0, match_border_contact: false, match_neighbor_labels: true },
    })
    const resolution = resolveManualOperation(vectorContext, vectorDraft)
    const operation = await buildManualOperation(vectorContext, vectorDraft, resolution, { commandId: 'cmd_floatendpoint1234567890', createdAt: '2026-07-16T00:00:00.000Z' })
    const command = operation.parameters.command as { selector: { scope: { resolution_fingerprint: string } } }
    expect(command.selector.scope.resolution_fingerprint).toBe('f9b6acee2e653a38085748e5f7ede8bae595fb6ad4432d44d55f95022ca77c2d')
  })

  it('serializes exact fill feature IDs with the hole classification fingerprint', async () => {
    const fillDraft = draft({ action: 'fill', selectedHoleFeatureId: holeAnalysis.features[0].id })
    const resolution = resolveManualOperation(context, fillDraft)
    const operation = await buildManualOperation(context, fillDraft, resolution, { commandId: 'cmd_fillcommand123456789012', createdAt: '2026-07-16T00:00:00.000Z' })
    const command = operation.parameters.command as Record<string, unknown>
    expect(operation.operation_type).toBe('editor_command_v1')
    expect(command.command_type).toBe('hole_correction')
    expect(command.policy).toBe('fill_hole')
    expect((command.selector as { feature_ids: string[] }).feature_ids).toEqual([holeAnalysis.features[0].id])
    expect((command.selector as { classification_fingerprint: string }).classification_fingerprint).toBe('d'.repeat(64))
  })
})
