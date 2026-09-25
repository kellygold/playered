import { describe, expect, it } from 'vitest'
import type { RegionOperation } from '../contracts'
import {
  incompatibleManualOperationCount,
  withoutIncompatibleManualOperations,
} from './manualHistoryPolicy'

function operation(operationType: string): RegionOperation {
  return {
    operation_type: operationType,
    selection: {},
    parameters: {},
    source: 'manual',
    provenance: {},
  }
}

describe('config change guard policy', () => {
  it('preserves palette audit history while counting and removing exact and legacy manual edits', () => {
    const operations = [
      operation('palette-edit'),
      operation('editor_command_v1'),
      operation('legacy-free-form-brush'),
      operation('palette-edit'),
    ]

    expect(incompatibleManualOperationCount(operations)).toBe(2)
    expect(withoutIncompatibleManualOperations(operations).map((item) => item.operation_type))
      .toEqual(['palette-edit', 'palette-edit'])
  })

  it('does not treat palette-only history as incompatible', () => {
    const operations = [operation('palette-edit')]
    expect(incompatibleManualOperationCount(operations)).toBe(0)
    expect(withoutIncompatibleManualOperations(operations)).toEqual(operations)
  })
})
