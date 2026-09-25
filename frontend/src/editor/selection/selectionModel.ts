import type { CanonicalTransform, RegionOperation } from '../../contracts'

export type SelectionCombineMode = 'add' | 'subtract'
export type SourceSelectionPoint = { x: number; y: number }

type SelectionPrimitiveBase = {
  primitive_id: string
  combine: SelectionCombineMode
}

export type RectangleSelectionPrimitive = SelectionPrimitiveBase & {
  kind: 'rectangle'
  x: number
  y: number
  width: number
  height: number
}

export type LassoSelectionPrimitive = SelectionPrimitiveBase & {
  kind: 'lasso'
  points: SourceSelectionPoint[]
}

export type BrushSelectionPrimitive = SelectionPrimitiveBase & {
  kind: 'brush'
  points: SourceSelectionPoint[]
  radius_mm: number
}

export type RegionSelectionPrimitive = SelectionPrimitiveBase & {
  kind: 'regions'
  region_ids: string[]
}

export type CanvasSelectionPrimitive =
  | RectangleSelectionPrimitive
  | LassoSelectionPrimitive
  | BrushSelectionPrimitive
  | RegionSelectionPrimitive

export type CanvasSelectionState = {
  schema_version: 1
  coordinate_space: 'normalized_source'
  source_width_px: number
  source_height_px: number
  primitives: CanvasSelectionPrimitive[]
  expand_mm: number
  feather_mm: number
}

export type CanvasSelectionCommand = {
  schema_version: 1
  command_type: 'canvas_selection'
  command_id: string
  source: 'manual'
  created_at: string
  provenance: Record<string, unknown>
  selector: {
    graph_fingerprint: string
    config_fingerprint: string
    selection: CanvasSelectionState
  }
}

type FitMatrix = { scaleX: number; scaleY: number; offsetX: number; offsetY: number }

function fitMatrix(transform: CanonicalTransform): FitMatrix {
  const { crop_rect: crop, working_size: working } = transform
  const scaleX = working.width / crop.width
  const scaleY = working.height / crop.height
  if (transform.fit_mode === 'stretch') return { scaleX, scaleY, offsetX: 0, offsetY: 0 }
  const scale = transform.fit_mode === 'cover'
    ? Math.max(scaleX, scaleY)
    : Math.min(scaleX, scaleY)
  return {
    scaleX: scale,
    scaleY: scale,
    offsetX: (working.width - crop.width * scale) / 2,
    offsetY: (working.height - crop.height * scale) / 2,
  }
}

function finitePoint(point: SourceSelectionPoint) {
  return Number.isFinite(point.x) && Number.isFinite(point.y)
}

export function emptyCanvasSelection(transform: CanonicalTransform): CanvasSelectionState {
  return {
    schema_version: 1,
    coordinate_space: 'normalized_source',
    source_width_px: transform.normalized_size.width,
    source_height_px: transform.normalized_size.height,
    primitives: [],
    expand_mm: 0,
    feather_mm: 0,
  }
}

export function workingPointToNormalizedSource(
  point: SourceSelectionPoint,
  transform: CanonicalTransform,
): SourceSelectionPoint {
  const matrix = fitMatrix(transform)
  return {
    x: Math.min(
      transform.normalized_size.width,
      Math.max(0, (point.x - matrix.offsetX) / matrix.scaleX + transform.crop_rect.x),
    ),
    y: Math.min(
      transform.normalized_size.height,
      Math.max(0, (point.y - matrix.offsetY) / matrix.scaleY + transform.crop_rect.y),
    ),
  }
}

export function normalizedSourcePointToWorking(
  point: SourceSelectionPoint,
  transform: CanonicalTransform,
): SourceSelectionPoint {
  const matrix = fitMatrix(transform)
  return {
    x: (point.x - transform.crop_rect.x) * matrix.scaleX + matrix.offsetX,
    y: (point.y - transform.crop_rect.y) * matrix.scaleY + matrix.offsetY,
  }
}

export function appendSelectionPrimitive(
  current: CanvasSelectionState,
  primitive: CanvasSelectionPrimitive,
): CanvasSelectionState {
  if (current.primitives.some((item) => item.primitive_id === primitive.primitive_id)) {
    throw new Error(`Selection primitive ID is already present: ${primitive.primitive_id}`)
  }
  return { ...current, primitives: [...current.primitives, primitive] }
}

export function updateSelectionEdge(
  current: CanvasSelectionState,
  values: { expandMm?: number; featherMm?: number },
): CanvasSelectionState {
  const expand = values.expandMm ?? current.expand_mm
  const feather = values.featherMm ?? current.feather_mm
  if (!Number.isFinite(expand) || expand < -100 || expand > 100) {
    throw new Error('Selection expansion must be between -100 and 100 millimetres.')
  }
  if (!Number.isFinite(feather) || feather < 0 || feather > 100) {
    throw new Error('Selection feather must be between 0 and 100 millimetres.')
  }
  return { ...current, expand_mm: expand, feather_mm: feather }
}

export function selectionPrimitiveId(): string {
  return `selection_${crypto.randomUUID().replaceAll('-', '')}`
}

function commandId(): string {
  return `cmd_${crypto.randomUUID().replaceAll('-', '')}`
}

export function buildCanvasSelectionOperation(
  selection: CanvasSelectionState,
  context: { graphFingerprint: string; configFingerprint: string },
  identity?: { commandId?: string; createdAt?: string },
): RegionOperation {
  const command: CanvasSelectionCommand = {
    schema_version: 1,
    command_type: 'canvas_selection',
    command_id: identity?.commandId ?? commandId(),
    source: 'manual',
    created_at: identity?.createdAt ?? new Date().toISOString(),
    provenance: { ui: 'canvas-selection-v1' },
    selector: {
      graph_fingerprint: context.graphFingerprint,
      config_fingerprint: context.configFingerprint,
      selection,
    },
  }
  return {
    operation_type: 'canvas_selection_v1',
    selection: command.selector,
    parameters: { command },
    source: 'manual',
    provenance: { editor_schema_version: 1, ui: 'canvas-selection-v1' },
  }
}

export function latestCanvasSelection(
  operations: readonly RegionOperation[],
  transform: CanonicalTransform,
  configFingerprint: string,
): CanvasSelectionState {
  for (let index = operations.length - 1; index >= 0; index -= 1) {
    const command = operations[index]?.parameters.command
    if (!command || typeof command !== 'object' || Array.isArray(command)) continue
    const candidate = command as Partial<CanvasSelectionCommand>
    if (candidate.command_type !== 'canvas_selection') continue
    const selector = candidate.selector
    const selection = selector?.selection
    if (
      selector?.config_fingerprint === configFingerprint
      && validSelectionState(selection, transform)
    ) return selection
  }
  return emptyCanvasSelection(transform)
}

export function validSelectionState(
  value: unknown,
  transform: CanonicalTransform,
): value is CanvasSelectionState {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const selection = value as Partial<CanvasSelectionState>
  if (
    selection.schema_version !== 1
    || selection.coordinate_space !== 'normalized_source'
    || selection.source_width_px !== transform.normalized_size.width
    || selection.source_height_px !== transform.normalized_size.height
    || !Array.isArray(selection.primitives)
    || !Number.isFinite(selection.expand_mm)
    || !Number.isFinite(selection.feather_mm)
  ) return false
  return selection.primitives.every((primitive) => {
    if (!primitive || typeof primitive !== 'object') return false
    if (!primitive.primitive_id || !['add', 'subtract'].includes(primitive.combine)) return false
    if (primitive.kind === 'rectangle') {
      return [primitive.x, primitive.y, primitive.width, primitive.height].every(Number.isFinite)
        && primitive.width > 0 && primitive.height > 0
    }
    if (primitive.kind === 'regions') return Array.isArray(primitive.region_ids)
    return Array.isArray(primitive.points) && primitive.points.every(finitePoint)
  })
}
