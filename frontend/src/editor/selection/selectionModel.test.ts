import { describe, expect, it } from 'vitest'
import type { CanonicalTransform, RegionOperation } from '../../contracts'
import {
  appendSelectionPrimitive,
  buildCanvasSelectionOperation,
  emptyCanvasSelection,
  latestCanvasSelection,
  normalizedSourcePointToWorking,
  updateSelectionEdge,
  workingPointToNormalizedSource,
} from './selectionModel'

const transform: CanonicalTransform = {
  schema_version: 1,
  original_size: { width: 1000, height: 500 },
  normalized_size: { width: 1000, height: 500 },
  exif_orientation: 1,
  crop_rect: { x: 100, y: 50, width: 500, height: 250 },
  fit_mode: 'stretch',
  working_size: { width: 1000, height: 1000 },
  canvas_size: { width: 200, height: 200 },
}

describe('canonical selection model', () => {
  it('round-trips working and normalized-source coordinates after crop and resize', () => {
    const source = workingPointToNormalizedSource({ x: 400, y: 600 }, transform)
    expect(source).toEqual({ x: 300, y: 200 })
    expect(normalizedSourcePointToWorking(source, transform)).toEqual({ x: 400, y: 600 })
  })

  it('preserves ordered add/subtract primitives and validates physical edges', () => {
    const initial = emptyCanvasSelection(transform)
    const withRectangle = appendSelectionPrimitive(initial, {
      kind: 'rectangle', primitive_id: 'selection_rectangle1', combine: 'add',
      x: 100, y: 50, width: 100, height: 100,
    })
    const withSubtract = appendSelectionPrimitive(withRectangle, {
      kind: 'brush', primitive_id: 'selection_brush001', combine: 'subtract',
      points: [{ x: 150, y: 75 }], radius_mm: 1,
    })
    expect(withSubtract.primitives.map((item) => item.combine)).toEqual(['add', 'subtract'])
    expect(updateSelectionEdge(withSubtract, { expandMm: -0.4, featherMm: 0.8 }))
      .toMatchObject({ expand_mm: -0.4, feather_mm: 0.8 })
    expect(() => updateSelectionEdge(withSubtract, { featherMm: -1 })).toThrow(/between 0 and 100/)
  })

  it('persists a typed snapshot and restores the latest compatible command', () => {
    const selection = appendSelectionPrimitive(emptyCanvasSelection(transform), {
      kind: 'lasso', primitive_id: 'selection_lasso001', combine: 'add',
      points: [{ x: 100, y: 50 }, { x: 200, y: 50 }, { x: 100, y: 150 }],
    })
    const operation = buildCanvasSelectionOperation(
      selection,
      { graphFingerprint: 'a'.repeat(64), configFingerprint: 'b'.repeat(64) },
      { commandId: 'cmd_selection01', createdAt: '2026-07-17T00:00:00Z' },
    )
    expect(operation.parameters.command).toMatchObject({
      command_type: 'canvas_selection',
      selector: { selection },
    })
    expect(operation.operation_type).toBe('canvas_selection_v1')
    expect(latestCanvasSelection([operation], transform, 'b'.repeat(64))).toEqual(selection)
    expect(latestCanvasSelection([operation], transform, 'c'.repeat(64)).primitives).toEqual([])
  })

  it('ignores malformed historical envelopes instead of guessing', () => {
    const malformed = {
      operation_type: 'editor_command_v1', selection: {},
      parameters: { command: { command_type: 'canvas_selection', selector: {} } },
      source: 'manual', provenance: {},
    } as RegionOperation
    expect(latestCanvasSelection([malformed], transform, 'b'.repeat(64)).primitives).toEqual([])
  })
})
