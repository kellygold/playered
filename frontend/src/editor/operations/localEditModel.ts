import type { RegionOperation } from '../../contracts'
import type { CanvasSelectionState } from '../selection'

export type LocalEditAction =
  | 'fill'
  | 'cleanup'
  | 'thicken'
  | 'erode'
  | 'open'
  | 'close'
  | 'clone'
  | 'affine'

export type LocalEditDraft = {
  action: LocalEditAction
  sourceLabel: number | null
  targetLabel: number | null
  editableLabels: number[]
  radiusMm: number
  activateTransparent: boolean
  offsetXmm: number
  offsetYmm: number
  scaleX: number
  scaleY: number
  shearX: number
  shearY: number
  translateXmm: number
  translateYmm: number
  clearSource: boolean
  sourceBackgroundLabel: number | null
  clipToCanvas: boolean
}

export type LocalEditContext = {
  graphFingerprint: string
  configFingerprint: string
  parentRevisionId: string | null
  selection: CanvasSelectionState
  paletteLabels: number[]
}

export type LocalEditResolution = {
  valid: boolean
  error: string | null
  summary: string
  edit: Record<string, unknown> | null
}

export const DEFAULT_LOCAL_EDIT_DRAFT: LocalEditDraft = {
  action: 'fill',
  sourceLabel: null,
  targetLabel: null,
  editableLabels: [],
  radiusMm: 0.4,
  activateTransparent: true,
  offsetXmm: 1,
  offsetYmm: 0,
  scaleX: 1,
  scaleY: 1,
  shearX: 0,
  shearY: 0,
  translateXmm: 0,
  translateYmm: 0,
  clearSource: true,
  sourceBackgroundLabel: null,
  clipToCanvas: false,
}

function sortedUnique(values: number[]) {
  return [...new Set(values)].sort((first, second) => first - second)
}

function knownLabel(value: number | null, labels: Set<number>): value is number {
  return value !== null && labels.has(value)
}

function finite(value: number) {
  return Number.isFinite(value)
}

export function resolveLocalEdit(
  context: LocalEditContext,
  draft: LocalEditDraft,
): LocalEditResolution {
  if (!context.selection.primitives.length) {
    return { valid: false, error: 'Draw or add at least one saved selection first.', summary: 'No selected pixels.', edit: null }
  }
  const labels = new Set(context.paletteLabels)
  const editable = sortedUnique(draft.editableLabels)
  if (editable.some((label) => !labels.has(label))) {
    return { valid: false, error: 'A replaceable color is no longer in the current palette.', summary: 'Palette mismatch.', edit: null }
  }
  if (draft.action === 'fill') {
    if (!knownLabel(draft.targetLabel, labels)) return { valid: false, error: 'Choose a fill color.', summary: 'Fill selection.', edit: null }
    return {
      valid: true,
      error: null,
      summary: `Fill the saved selection with label ${draft.targetLabel}${draft.activateTransparent ? ', activating transparency' : ''}.`,
      edit: { kind: 'fill', target_label: draft.targetLabel, activate_transparent: draft.activateTransparent },
    }
  }
  if (draft.action === 'cleanup') {
    if (!knownLabel(draft.sourceLabel, labels) || !knownLabel(draft.targetLabel, labels)) {
      return { valid: false, error: 'Choose source and target cleanup colors.', summary: 'Clean one color.', edit: null }
    }
    if (draft.sourceLabel === draft.targetLabel) return { valid: false, error: 'Cleanup source and target must differ.', summary: 'Clean one color.', edit: null }
    return {
      valid: true,
      error: null,
      summary: `Replace label ${draft.sourceLabel} with label ${draft.targetLabel} only inside the saved selection.`,
      edit: { kind: 'color_cleanup', source_labels: [draft.sourceLabel], target_label: draft.targetLabel },
    }
  }
  if (['thicken', 'erode', 'open', 'close'].includes(draft.action)) {
    if (!knownLabel(draft.sourceLabel, labels)) return { valid: false, error: 'Choose the feature color.', summary: 'Physical morphology.', edit: null }
    if (!finite(draft.radiusMm) || draft.radiusMm <= 0 || draft.radiusMm > 25) {
      return { valid: false, error: 'Radius must be greater than 0 and at most 25 mm.', summary: 'Physical morphology.', edit: null }
    }
    if (draft.action === 'thicken' || draft.action === 'close') {
      const allowed = editable.filter((label) => label !== draft.sourceLabel)
      if (!allowed.length) return { valid: false, error: `Choose at least one color that ${draft.action === 'close' ? 'closing' : 'thickening'} may replace.`, summary: draft.action === 'close' ? 'Close gaps in selected feature.' : 'Thicken selected feature.', edit: null }
      const operation = draft.action === 'close' ? 'close' : 'dilate'
      return {
        valid: true,
        error: null,
        summary: `${draft.action === 'close' ? 'Close gaps in' : 'Thicken'} label ${draft.sourceLabel} with a ${draft.radiusMm} mm kernel into ${allowed.length} allowed color${allowed.length === 1 ? '' : 's'}.`,
        edit: { kind: 'morphology', operation, source_label: draft.sourceLabel, radius_mm: draft.radiusMm, editable_labels: allowed, replacement_label: null },
      }
    }
    if (!knownLabel(draft.targetLabel, labels) || draft.targetLabel === draft.sourceLabel) {
      return { valid: false, error: 'Choose a different replacement color for erosion.', summary: 'Erode selected feature.', edit: null }
    }
    return {
      valid: true,
      error: null,
      summary: `${draft.action === 'open' ? 'Open and remove specks from' : 'Erode'} label ${draft.sourceLabel} with a ${draft.radiusMm} mm kernel into label ${draft.targetLabel}.`,
      edit: { kind: 'morphology', operation: draft.action, source_label: draft.sourceLabel, radius_mm: draft.radiusMm, editable_labels: [], replacement_label: draft.targetLabel },
    }
  }
  if (draft.action === 'clone') {
    if (![draft.offsetXmm, draft.offsetYmm].every(finite) || (draft.offsetXmm === 0 && draft.offsetYmm === 0)) {
      return { valid: false, error: 'Clone requires a non-zero physical offset.', summary: 'Clone selection.', edit: null }
    }
    if (!editable.length) return { valid: false, error: 'Choose at least one color the clone may overwrite.', summary: 'Clone selection.', edit: null }
    return {
      valid: true,
      error: null,
      summary: `Clone by ${draft.offsetXmm} mm × ${draft.offsetYmm} mm${draft.clipToCanvas ? ', clipping at the canvas' : ''}.`,
      edit: { kind: 'clone', offset_x_mm: draft.offsetXmm, offset_y_mm: draft.offsetYmm, overwrite_labels: editable, include_transparent: false, clip_to_canvas: draft.clipToCanvas },
    }
  }
  const affineValues = [draft.scaleX, draft.scaleY, draft.shearX, draft.shearY, draft.translateXmm, draft.translateYmm]
  if (!affineValues.every(finite) || draft.scaleX < 0.05 || draft.scaleY < 0.05 || draft.scaleX > 20 || draft.scaleY > 20) {
    return { valid: false, error: 'Affine scales must be between 0.05× and 20×.', summary: 'Resize or warp selection.', edit: null }
  }
  const determinant = draft.scaleX * draft.scaleY - draft.shearX * draft.shearY
  if (Math.abs(determinant) < 0.000001) return { valid: false, error: 'Resize/warp matrix is singular.', summary: 'Resize or warp selection.', edit: null }
  if (draft.scaleX === 1 && draft.scaleY === 1 && draft.shearX === 0 && draft.shearY === 0 && draft.translateXmm === 0 && draft.translateYmm === 0) {
    return { valid: false, error: 'Change scale, shear, or translation before applying.', summary: 'Resize or warp selection.', edit: null }
  }
  if (!editable.length) return { valid: false, error: 'Choose at least one color the transformed selection may overwrite.', summary: 'Resize or warp selection.', edit: null }
  return {
    valid: true,
    error: null,
    summary: `Resize/warp to ${draft.scaleX}× × ${draft.scaleY}× with shear ${draft.shearX}/${draft.shearY}.`,
    edit: {
      kind: 'affine',
      scale_x: draft.scaleX,
      scale_y: draft.scaleY,
      shear_x: draft.shearX,
      shear_y: draft.shearY,
      translate_x_mm: draft.translateXmm,
      translate_y_mm: draft.translateYmm,
      origin_x: 0.5,
      origin_y: 0.5,
      overwrite_labels: editable,
      clear_source: draft.clearSource,
      source_background_label: draft.sourceBackgroundLabel,
      include_transparent: false,
      clip_to_canvas: draft.clipToCanvas,
    },
  }
}

function commandId() {
  return `cmd_${crypto.randomUUID().replaceAll('-', '').slice(0, 24)}`
}

const SELECTION_FLOAT_KEYS = new Set([
  'x', 'y', 'width', 'height', 'radius_mm', 'expand_mm', 'feather_mm',
])

function canonicalSelectionValue(value: unknown, key?: string): string {
  if (typeof value === 'number' && key && SELECTION_FLOAT_KEYS.has(key) && Number.isInteger(value)) return `${value}.0`
  if (value === null || typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map((item) => canonicalSelectionValue(item)).join(',')}]`
  const record = value as Record<string, unknown>
  return `{${Object.keys(record).sort().map((itemKey) => `${JSON.stringify(itemKey)}:${canonicalSelectionValue(record[itemKey], itemKey)}`).join(',')}}`
}

async function selectionSha256(selection: CanvasSelectionState): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalSelectionValue(selection))
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

export async function buildLocalEditOperation(
  context: LocalEditContext,
  draft: LocalEditDraft,
  identity?: { commandId?: string; createdAt?: string },
): Promise<RegionOperation> {
  const resolution = resolveLocalEdit(context, draft)
  if (!resolution.valid || !resolution.edit) throw new Error(resolution.error ?? 'Local edit is invalid.')
  const selector = {
    graph_fingerprint: context.graphFingerprint,
    config_fingerprint: context.configFingerprint,
    selection: structuredClone(context.selection),
  }
  const command = {
    schema_version: 1,
    command_type: 'local_raster_edit',
    command_id: identity?.commandId ?? commandId(),
    source: 'manual',
    created_at: identity?.createdAt ?? new Date().toISOString(),
    provenance: {
      ui: 'local-edits-panel-v1',
      selection_snapshot: true,
      selection_primitive_count: context.selection.primitives.length,
      operation_summary: resolution.summary,
    },
    selector,
    edit: resolution.edit,
  }
  const regionalEditProvenance = {
    schema_version: 1,
    execution_kind: 'local',
    selection_sha256: await selectionSha256(context.selection),
    parent_revision_id: context.parentRevisionId,
    engine_id: 'image23mf-local-raster',
    engine_version: '1',
    reproducibility: 'deterministic',
    reproducibility_reason: 'The exact selection and typed local operation replay without a remote model call.',
  }
  return {
    operation_type: 'editor_command_v1',
    selection: selector,
    parameters: { command },
    source: 'manual',
    provenance: {
      editor_schema_version: 1,
      ui: 'local-edits-panel-v1',
      regional_edit: regionalEditProvenance,
    },
  }
}

export function localEditOperationLabel(operation: RegionOperation): string {
  const command = operation.parameters.command as { command_type?: string; edit?: { kind?: string; operation?: string } } | undefined
  if (command?.command_type !== 'local_raster_edit') return 'Apply local edit'
  const kind = command.edit?.kind === 'morphology' ? command.edit.operation : command.edit?.kind
  const labels: Record<string, string> = {
    fill: 'Fill saved selection',
    color_cleanup: 'Clean color in selection',
    dilate: 'Thicken selected feature',
    erode: 'Erode selected feature',
    open: 'Open selected feature mask',
    close: 'Close selected feature mask',
    clone: 'Clone saved selection',
    affine: 'Resize or warp selection',
  }
  return labels[kind ?? ''] ?? 'Apply local edit'
}
