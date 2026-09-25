import { describe, expect, it } from 'vitest'
import type { CanvasSelectionState } from '../selection'
import {
  buildLocalEditOperation,
  DEFAULT_LOCAL_EDIT_DRAFT,
  localEditOperationLabel,
  resolveLocalEdit,
  type LocalEditContext,
} from './localEditModel'

const selection: CanvasSelectionState = {
  schema_version: 1,
  coordinate_space: 'normalized_source',
  source_width_px: 100,
  source_height_px: 80,
  primitives: [{
    kind: 'rectangle',
    primitive_id: 'selection_local_model',
    combine: 'add',
    x: 10,
    y: 10,
    width: 20,
    height: 20,
  }],
  expand_mm: 0,
  feather_mm: 0,
}

const context: LocalEditContext = {
  graphFingerprint: 'a'.repeat(64),
  configFingerprint: 'b'.repeat(64),
  parentRevisionId: 'revision_parent',
  selection,
  paletteLabels: [0, 1, 2],
}

describe('local edit model', () => {
  it('requires a saved selection and actionable operation parameters', () => {
    expect(resolveLocalEdit({ ...context, selection: { ...selection, primitives: [] } }, DEFAULT_LOCAL_EDIT_DRAFT).error).toMatch(/selection/i)
    expect(resolveLocalEdit(context, DEFAULT_LOCAL_EDIT_DRAFT).error).toMatch(/fill color/i)
    expect(resolveLocalEdit(context, {
      ...DEFAULT_LOCAL_EDIT_DRAFT,
      action: 'cleanup',
      sourceLabel: 1,
      targetLabel: 1,
    }).error).toMatch(/must differ/i)
    expect(resolveLocalEdit(context, {
      ...DEFAULT_LOCAL_EDIT_DRAFT,
      action: 'affine',
      editableLabels: [0],
      shearX: 1,
      shearY: 1,
    }).error).toMatch(/singular/i)
  })

  it('builds a fill command with an immutable selection snapshot and provenance', async () => {
    const operation = await buildLocalEditOperation(context, {
      ...DEFAULT_LOCAL_EDIT_DRAFT,
      targetLabel: 2,
    }, {
      commandId: 'cmd_123456781234123412341234',
      createdAt: '2026-07-17T08:00:00.000Z',
    })
    const command = operation.parameters.command as {
      command_type: string
      edit: Record<string, unknown>
      selector: { selection: CanvasSelectionState }
      provenance: Record<string, unknown>
    }

    expect(operation.operation_type).toBe('editor_command_v1')
    expect(command.command_type).toBe('local_raster_edit')
    expect(command.edit).toEqual({ kind: 'fill', target_label: 2, activate_transparent: true })
    expect(command.selector.selection).toEqual(selection)
    expect(command.selector.selection).not.toBe(selection)
    expect(command.provenance.selection_snapshot).toBe(true)
    expect(operation.provenance.regional_edit).toMatchObject({
      execution_kind: 'local',
      parent_revision_id: 'revision_parent',
      reproducibility: 'deterministic',
    })
    expect((operation.provenance.regional_edit as { selection_sha256: string }).selection_sha256).toBe(
      '55cd9236a17388124adcd925cd3f681a8b7e820039c0ac9843aed103645b4371',
    )
    expect(localEditOperationLabel(operation)).toBe('Fill saved selection')
  })

  it('resolves physical morphology, clone, and affine payloads', () => {
    const thickening = resolveLocalEdit(context, {
      ...DEFAULT_LOCAL_EDIT_DRAFT,
      action: 'thicken',
      sourceLabel: 1,
      editableLabels: [0, 2],
      radiusMm: 0.6,
    })
    expect(thickening.edit).toMatchObject({
      kind: 'morphology', operation: 'dilate', source_label: 1, radius_mm: 0.6,
    })
    expect(resolveLocalEdit(context, {
      ...DEFAULT_LOCAL_EDIT_DRAFT,
      action: 'open',
      sourceLabel: 1,
      targetLabel: 0,
    }).edit).toMatchObject({ kind: 'morphology', operation: 'open' })
    expect(resolveLocalEdit(context, {
      ...DEFAULT_LOCAL_EDIT_DRAFT,
      action: 'close',
      sourceLabel: 1,
      editableLabels: [0],
    }).edit).toMatchObject({ kind: 'morphology', operation: 'close' })

    const clone = resolveLocalEdit(context, {
      ...DEFAULT_LOCAL_EDIT_DRAFT,
      action: 'clone',
      editableLabels: [0],
      offsetXmm: -2,
      offsetYmm: 3,
      clipToCanvas: true,
    })
    expect(clone.edit).toMatchObject({
      kind: 'clone', offset_x_mm: -2, offset_y_mm: 3, clip_to_canvas: true,
    })

    const affine = resolveLocalEdit(context, {
      ...DEFAULT_LOCAL_EDIT_DRAFT,
      action: 'affine',
      editableLabels: [0],
      scaleX: 1.5,
    })
    expect(affine.edit).toMatchObject({ kind: 'affine', scale_x: 1.5, clear_source: true })
  })
})
