import type { RegionOperation } from '../contracts'

export const PALETTE_HISTORY_OPERATION_TYPE = 'palette-edit'

export function incompatibleManualOperationCount(
  operations: readonly RegionOperation[],
): number {
  return operations.filter(
    (operation) => operation.operation_type !== PALETTE_HISTORY_OPERATION_TYPE,
  ).length
}

export function withoutIncompatibleManualOperations(
  operations: readonly RegionOperation[],
): RegionOperation[] {
  return operations.filter(
    (operation) => operation.operation_type === PALETTE_HISTORY_OPERATION_TYPE,
  )
}
