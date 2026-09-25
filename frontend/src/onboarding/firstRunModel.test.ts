import { describe, expect, it } from 'vitest'
import { firstRunStage, firstRunStageIndex } from './firstRunModel'

describe('firstRunModel', () => {
  it('advances only when durable evidence for the next stage exists', () => {
    expect(firstRunStage({
      hasProject: false,
      previewCurrent: false,
      geometryReady: false,
      outputWorking: false,
      outputCurrent: false,
    })).toBe('source')
    expect(firstRunStage({
      hasProject: true,
      previewCurrent: false,
      geometryReady: false,
      outputWorking: false,
      outputCurrent: false,
    })).toBe('source')
    expect(firstRunStage({
      hasProject: true,
      previewCurrent: true,
      geometryReady: false,
      outputWorking: false,
      outputCurrent: false,
    })).toBe('processed')
    expect(firstRunStage({
      hasProject: true,
      previewCurrent: true,
      geometryReady: false,
      outputWorking: true,
      outputCurrent: false,
    })).toBe('geometry')
    expect(firstRunStage({
      hasProject: true,
      previewCurrent: true,
      geometryReady: true,
      outputWorking: false,
      outputCurrent: true,
    })).toBe('validated')
  })

  it('orders source, processed, geometry, and slicer evidence', () => {
    expect((['source', 'processed', 'geometry', 'validated'] as const).map(firstRunStageIndex)).toEqual([0, 1, 2, 3])
  })
})
