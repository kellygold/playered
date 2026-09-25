import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PaletteColor, RegionGraph } from '../../contracts'
import { ManualOperationsPanel, type ManualOperationsPanelProps } from './ManualOperationsPanel'

const firstId = 'region_111111111111111111111111'
const secondId = 'region_222222222222222222222222'
const orangeId = 'region_333333333333333333333333'

function region(id: string, label: number, x: number, overrides: Partial<RegionGraph['regions'][number]> = {}): RegionGraph['regions'][number] {
  return {
    id, label, color: label === 0 ? '#112233' : '#FF6600', pixel_count: 10, area_mm2: 1,
    perimeter_edge_count: 12, perimeter_mm: 3, compactness: 0.5,
    pixel_bounds: { x, y: 0, width: 2, height: 2 },
    physical_bounds: { x_mm: x * 0.2, y_mm: 0, width_mm: 0.4, height_mm: 0.4 },
    border_contact: { top_mm: 0, right_mm: 0, bottom_mm: 0, left_mm: 0, total_mm: 0, touches_canvas_border: false },
    width_estimate: { method: 'orthogonal-run-spans-v1', minimum_mm: 0.4, median_mm: 0.5, p95_mm: 0.6, maximum_mm: 0.7 },
    neighbor_region_ids: [], ...overrides,
  }
}

const graph: RegionGraph = {
  schema_version: 1, width_px: 20, height_px: 10, width_mm: 4, height_mm: 2,
  pixel_width_mm: 0.2, pixel_height_mm: 0.2, active_pixel_count: 30,
  palette: [{ label: 0, color: '#112233' }, { label: 1, color: '#FF6600' }],
  regions: [
    region(firstId, 0, 0, { neighbor_region_ids: [orangeId] }),
    region(secondId, 0, 4),
    region(orangeId, 1, 8, { neighbor_region_ids: [firstId] }),
  ],
  adjacency: [{ first_region_id: firstId, second_region_id: orangeId, boundary_edge_count: 2, boundary_length_mm: 0.4 }],
}

const palette: PaletteColor[] = [
  { id: 'dark', name: 'Dark', hex: '#112233', locked: false, filament_id: null },
  { id: 'orange', name: 'Orange', hex: '#FF6600', locked: false, filament_id: null },
]

function props(overrides: Partial<ManualOperationsPanelProps> = {}): ManualOperationsPanelProps {
  return {
    graph, graphFingerprint: 'a'.repeat(64), configFingerprint: 'b'.repeat(64), palette,
    holeAnalysis: null, holeAnalysisFingerprint: null, selectedRegionId: firstId, onRegionSelect: vi.fn(), onCommit: vi.fn(),
    ...overrides,
  }
}

afterEach(cleanup)

describe('ManualOperationsPanel', () => {
  it('requires an exact selected region and honestly disables unavailable hole work', () => {
    const { rerender } = render(<ManualOperationsPanel {...props({ selectedRegionId: null })} />)
    expect(screen.getByRole('status')).toHaveTextContent('Select an exact region')
    rerender(<ManualOperationsPanel {...props()} />)
    const fill = screen.getByRole('button', { name: /Fill hole/ })
    expect(fill).not.toBeDisabled()
    expect(fill).toHaveAttribute('aria-disabled', 'true')
    expect(fill).toHaveAccessibleDescription(/not part of a classified enclosed hole/)
    fireEvent.click(fill)
    expect(screen.getByRole('button', { name: /^Keep \/ protect/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /Review exact change/ })).toBeEnabled()
  })

  it('previews exact apply-to-similar IDs and clears highlights on cancel', () => {
    const onAffectedRegionsChange = vi.fn()
    const onRegionSelect = vi.fn()
    render(<ManualOperationsPanel {...props({ onAffectedRegionsChange, onRegionSelect })} />)

    fireEvent.click(screen.getByRole('radio', { name: 'Apply to similar' }))
    expect(screen.getByText(/same palette label \(0\)/)).toBeInTheDocument()
    expect(screen.getByText('2 exact regions will be affected.')).toBeInTheDocument()
    expect(onAffectedRegionsChange).toHaveBeenLastCalledWith([])

    fireEvent.click(screen.getByRole('button', { name: 'Review exact change' }))
    expect(onAffectedRegionsChange).toHaveBeenLastCalledWith([firstId, secondId])
    const dialog = screen.getByRole('region', { name: 'Confirm protect operation' })
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus()
    const regionActions = within(dialog).getAllByRole('button', { name: /region_/ })
    expect(regionActions.map((button) => button.textContent)).toEqual([firstId, secondId])
    regionActions[1].focus()
    expect(regionActions[1]).toHaveFocus()
    fireEvent.click(regionActions[1])
    expect(onRegionSelect).toHaveBeenCalledWith(secondId)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(onAffectedRegionsChange).toHaveBeenLastCalledWith([])
    expect(screen.queryByRole('region', { name: 'Confirm protect operation' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review exact change' })).toHaveFocus()
  })

  it('cancels review with Escape and restores the initiating focus', () => {
    render(<ManualOperationsPanel {...props()} />)
    const review = screen.getByRole('button', { name: 'Review exact change' })
    review.focus()
    fireEvent.click(review)
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('region', { name: 'Confirm protect operation' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review exact change' })).toHaveFocus()
  })

  it('never commits before confirmation and serializes the confirmed operation', async () => {
    const onCommit = vi.fn()
    const onAffectedRegionsChange = vi.fn()
    render(<ManualOperationsPanel {...props({ onCommit, onAffectedRegionsChange })} />)
    fireEvent.click(screen.getByRole('button', { name: /^Recolor/ }))
    expect(screen.getByRole('button', { name: 'Review exact change' })).toBeDisabled()
    fireEvent.change(screen.getByRole('combobox', { name: 'Target color' }), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Review exact change' }))
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByText(/No pixels change until you confirm/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm recolor' }))

    await waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1))
    expect(onAffectedRegionsChange).toHaveBeenLastCalledWith([])
    const operation = onCommit.mock.calls[0][0]
    expect(operation.operation_type).toBe('editor_command_v1')
    expect(operation.parameters.command).toMatchObject({
      command_type: 'region_operation', operation: 'recolor', target_label: 1,
      selector: { graph_fingerprint: 'a'.repeat(64), config_fingerprint: 'b'.repeat(64) },
    })
  })

  it('blocks unsafe apply-to-similar merge and invalid thicken inputs', () => {
    render(<ManualOperationsPanel {...props()} />)
    fireEvent.click(screen.getByRole('button', { name: /^Merge/ }))
    fireEvent.change(screen.getByRole('combobox', { name: 'Target color' }), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('radio', { name: 'Apply to similar' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Every merged region must touch')
    expect(screen.getByRole('button', { name: 'Review exact change' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: /^Thicken/ }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose at least one replaceable')
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Expansion radius millimetres' }), { target: { value: '0' } })
    expect(screen.getByRole('alert')).toHaveTextContent('Thickness must be greater than 0')
  })

  it('applies inspector suggestion intent without committing or bypassing review', () => {
    const onCommit = vi.fn()
    const { rerender } = render(<ManualOperationsPanel {...props({ onCommit })} />)
    rerender(<ManualOperationsPanel {...props({ onCommit, suggestionIntent: { key: 1, kind: 'widen', featureKey: 'thin-line-1' } })} />)
    expect(screen.getByRole('button', { name: /^Thicken/ })).toHaveAttribute('aria-pressed', 'true')
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Review exact change' })).toBeDisabled()
  })

  it('never aliases specialized hole suggestions to semantically different region edits', () => {
    const { rerender } = render(<ManualOperationsPanel {...props()} />)
    rerender(<ManualOperationsPanel {...props({ suggestionIntent: { key: 1, kind: 'recolor_hole', featureKey: 'feature_x' } })} />)
    expect(screen.getByText(/needs a specialized editor command/)).toHaveAttribute('role', 'status')
    expect(screen.getByRole('button', { name: /^Keep \/ protect/ })).toHaveAttribute('aria-pressed', 'true')
  })

  it('locks every authoring control while the exact preview is stale', () => {
    render(<ManualOperationsPanel {...props({ disabled: true, disabledReason: 'Update preview before another operation.' })} />)
    expect(screen.getByText('Update preview before another operation.')).toHaveAttribute('role', 'status')
    expect(screen.getAllByRole('button').every((button) => button.hasAttribute('disabled'))).toBe(true)
    expect(screen.getAllByRole('radio').every((radio) => radio.hasAttribute('disabled'))).toBe(true)
  })

  it('locks a second sequential command immediately after the first command commits', async () => {
    function Harness() {
      const [stale, setStale] = useState(false)
      return (
        <ManualOperationsPanel
          {...props({
            disabled: stale,
            disabledReason: stale ? 'Update preview before another operation.' : undefined,
            onCommit: () => setStale(true),
          })}
        />
      )
    }
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Review exact change' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm protect' }))
    await waitFor(() => expect(screen.getByText('Update preview before another operation.')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /^Keep \/ protect/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Review exact change' })).toBeDisabled()
  })
})
