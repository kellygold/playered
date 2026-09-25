import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RegionGraph } from '../../contracts'
import type { CanvasSelectionState } from '../selection'
import { LocalEditsPanel, type LocalEditsPanelProps } from './LocalEditsPanel'

const graph: RegionGraph = {
  schema_version: 1,
  width_px: 20,
  height_px: 10,
  width_mm: 4,
  height_mm: 2,
  pixel_width_mm: 0.2,
  pixel_height_mm: 0.2,
  active_pixel_count: 200,
  palette: [
    { label: 0, color: '#F5E6C8' },
    { label: 1, color: '#111111' },
    { label: 2, color: '#FF6600' },
  ],
  regions: [],
  adjacency: [],
}

const selection: CanvasSelectionState = {
  schema_version: 1,
  coordinate_space: 'normalized_source',
  source_width_px: 20,
  source_height_px: 10,
  primitives: [{
    kind: 'rectangle',
    primitive_id: 'selection_panel_test',
    combine: 'add',
    x: 2,
    y: 2,
    width: 4,
    height: 4,
  }],
  expand_mm: 0,
  feather_mm: 0,
}

function props(overrides: Partial<LocalEditsPanelProps> = {}): LocalEditsPanelProps {
  return {
    graph,
    graphFingerprint: 'a'.repeat(64),
    configFingerprint: 'b'.repeat(64),
    selection,
    onCommit: vi.fn(),
    ...overrides,
  }
}

afterEach(cleanup)

describe('LocalEditsPanel', () => {
  it('requires an exact saved selection before enabling destructive work', () => {
    render(<LocalEditsPanel {...props({ selection: { ...selection, primitives: [] } })} />)

    expect(screen.getByText(/Draw a rectangle, lasso/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review local edit' })).toBeDisabled()
  })

  it('reviews and commits a typed fill without mutating before confirmation', async () => {
    const onCommit = vi.fn()
    render(<LocalEditsPanel {...props({ onCommit })} />)
    fireEvent.change(screen.getByRole('combobox', { name: 'Fill color' }), {
      target: { value: '2' },
    })
    const review = screen.getByRole('button', { name: 'Review local edit' })
    expect(review).toBeEnabled()
    fireEvent.click(review)
    expect(onCommit).not.toHaveBeenCalled()
    let confirmation = screen.getByRole('region', { name: 'Confirm local deterministic edit' })
    expect(within(confirmation).getByRole('button', { name: 'Cancel' })).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })
    const restoredReview = screen.getByRole('button', { name: 'Review local edit' })
    expect(restoredReview).toHaveFocus()
    fireEvent.click(restoredReview)
    confirmation = screen.getByRole('region', { name: 'Confirm local deterministic edit' })
    expect(within(confirmation).getByText(/preview will become stale/i)).toBeInTheDocument()
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Save edit' }))

    await waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))
    expect(onCommit.mock.calls[0][0]).toMatchObject({
      operation_type: 'editor_command_v1',
      parameters: {
        command: {
          command_type: 'local_raster_edit',
          edit: { kind: 'fill', target_label: 2, activate_transparent: true },
          selector: { selection },
        },
      },
    })
  })

  it('exposes physical thickening and explicit overwrite colors', () => {
    render(<LocalEditsPanel {...props()} />)
    fireEvent.change(screen.getByRole('combobox', { name: 'Operation' }), {
      target: { value: 'thicken' },
    })
    fireEvent.change(screen.getByRole('combobox', { name: 'Feature / source color' }), {
      target: { value: '1' },
    })
    expect(screen.getByRole('button', { name: 'Review local edit' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /Label 0/ }))

    expect(screen.getByText(/Thicken label 1 with a 0.4 mm kernel/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review local edit' })).toBeEnabled()
  })

  it('validates affine matrices and exposes canvas clipping as explicit intent', () => {
    render(<LocalEditsPanel {...props()} />)
    fireEvent.change(screen.getByRole('combobox', { name: 'Operation' }), {
      target: { value: 'affine' },
    })
    fireEvent.click(screen.getByRole('checkbox', { name: /Label 0/ }))
    expect(screen.getByText(/Change scale, shear, or translation/)).toBeInTheDocument()
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Resize scale X' }), {
      target: { value: '1.5' },
    })
    fireEvent.click(screen.getByRole('checkbox', { name: /Allow explicit clipping/ }))

    expect(screen.getByText(/Resize\/warp to 1.5×/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review local edit' })).toBeEnabled()
  })
})
