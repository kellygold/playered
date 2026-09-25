import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RegionGraph, RiskReport } from '../../contracts'
import { RegionInspector } from './RegionInspector'

const firstId = 'region_aaaaaaaaaaaaaaaaaaaaaaaa'
const secondId = 'region_bbbbbbbbbbbbbbbbbbbbbbbb'

const region = (overrides: Partial<RegionGraph['regions'][number]> = {}): RegionGraph['regions'][number] => ({
  id: firstId,
  label: 0,
  color: '#112233',
  pixel_count: 24,
  area_mm2: 1.5,
  perimeter_edge_count: 18,
  perimeter_mm: 4.25,
  compactness: 0.73,
  pixel_bounds: { x: 1, y: 2, width: 6, height: 4 },
  physical_bounds: { x_mm: 0.2, y_mm: 0.4, width_mm: 1.2, height_mm: 0.8 },
  border_contact: { top_mm: 0.2, right_mm: 0, bottom_mm: 0, left_mm: 0, total_mm: 0.2, touches_canvas_border: true },
  width_estimate: { method: 'orthogonal-run-spans-v1', minimum_mm: 0.2, median_mm: 0.45, p95_mm: 0.72, maximum_mm: 0.8 },
  neighbor_region_ids: [secondId],
  ...overrides,
})

const graph: RegionGraph = {
  schema_version: 1,
  width_px: 100,
  height_px: 100,
  width_mm: 20,
  height_mm: 20,
  pixel_width_mm: 0.2,
  pixel_height_mm: 0.2,
  active_pixel_count: 36,
  palette: [{ label: 0, color: '#112233' }, { label: 1, color: '#FF6600' }],
  regions: [
    region(),
    region({ id: secondId, label: 1, color: '#FF6600', area_mm2: 0.48, pixel_count: 12, border_contact: { top_mm: 0, right_mm: 0, bottom_mm: 0, left_mm: 0, total_mm: 0, touches_canvas_border: false }, neighbor_region_ids: [firstId] }),
  ],
  adjacency: [{ first_region_id: firstId, second_region_id: secondId, boundary_edge_count: 3, boundary_length_mm: 0.6 }],
}

const warning = (id: string, regionIds: string[], title: string): RiskReport['warnings'][number] => ({
  id,
  code: id === 'risk-gap' ? 'narrow_gap' : 'small_island',
  feature_key: id,
  severity: id === 'risk-global' ? 'info' : 'warning',
  title,
  explanation: `${title} has a physical reason that must remain inspectable.`,
  affected_region_ids: regionIds,
  affected_labels: regionIds.length ? [0] : [],
  affected_bounds: [{ x_mm: 0.2, y_mm: 0.4, width_mm: 1.2, height_mm: 0.8 }],
  measurements: [
    { key: 'minimum_width', role: 'measured', value: 0.2, unit: 'mm' },
    { key: 'minimum_width', role: 'threshold', value: 0.4, unit: 'mm' },
  ],
  suggestions: [{ kind: 'widen', title: 'Widen feature', explanation: 'Expand this feature to the selected nozzle threshold.', destructive: true, requires_confirmation: true }],
  classifier_id: 'physical-clearance-v1',
  classifier_version: '1.2.0',
})

const report: RiskReport = {
  schema_version: 1,
  graph_fingerprint: 'a'.repeat(64),
  options_fingerprint: 'b'.repeat(64),
  nozzle_mm: 0.4,
  evaluated_codes: ['small_island', 'narrow_gap'],
  pending_codes: [],
  warnings: [
    warning('risk-island', [firstId], 'Small island'),
    warning('risk-gap', [firstId, secondId], 'Narrow gap'),
    warning('risk-global', [], 'Fragmentation review'),
  ],
  summary: { total: 3, info: 1, warning: 2, error: 0, by_code: [{ code: 'small_island', count: 1 }, { code: 'narrow_gap', count: 1 }] },
}

afterEach(cleanup)

describe('RegionInspector', () => {
  it('renders exact region identity, color, topology, physical widths, and neighbors', () => {
    render(<RegionInspector graph={graph} report={report} selectedRegionId={firstId} selectedRiskId={null} onRegionSelect={() => {}} onRiskSelect={() => {}} />)

    expect(screen.getByText(firstId)).toBeInTheDocument()
    expect(screen.getByText('Palette label 0 · #112233')).toBeInTheDocument()
    expect(screen.getByText('Touches canvas edge')).toBeInTheDocument()
    expect(screen.getByText('1.500 mm²')).toBeInTheDocument()
    expect(screen.getByText('0.200 mm')).toBeInTheDocument()
    expect(screen.getByText('0.450 mm')).toBeInTheDocument()
    expect(screen.getAllByText('bbbbbbbb')).toHaveLength(2)
    expect(screen.getByText('0.600 mm shared')).toBeInTheDocument()
  })

  it('makes every canvas warning a keyboard-focusable list action, including regionless findings', () => {
    const onRiskSelect = vi.fn()
    render(<RegionInspector graph={graph} report={report} selectedRegionId={null} selectedRiskId={null} onRegionSelect={() => {}} onRiskSelect={onRiskSelect} />)

    const list = screen.getByRole('list', { name: 'Print warnings' })
    const actions = within(list).getAllByRole('button')
    expect(actions).toHaveLength(3)
    expect(actions.map((action) => action.textContent)).toEqual(expect.arrayContaining([
      expect.stringContaining('Small island'),
      expect.stringContaining('Narrow gap'),
      expect.stringContaining('Fragmentation review'),
    ]))
    const regionless = within(list).getByRole('button', { name: /Fragmentation review/ })
    regionless.focus()
    expect(regionless).toHaveFocus()
    fireEvent.click(regionless)
    expect(onRiskSelect).toHaveBeenCalledWith('risk-global', null)
  })

  it('keeps a 1,578-warning report bounded, aggregated, filterable, and selected-warning synchronized', () => {
    const warnings = Array.from({ length: 1_578 }, (_, index) => ({
      ...warning(`risk-${index}`, [index % 2 ? firstId : secondId], `Finding ${index}`),
      severity: index % 10 === 0 ? 'error' as const : index % 3 === 0 ? 'info' as const : 'warning' as const,
    }))
    const largeReport = {
      ...report,
      warnings,
      summary: {
        ...report.summary,
        total: warnings.length,
        error: warnings.filter((item) => item.severity === 'error').length,
        warning: warnings.filter((item) => item.severity === 'warning').length,
        info: warnings.filter((item) => item.severity === 'info').length,
      },
    }
    const selectedId = 'risk-1500'
    render(<RegionInspector graph={graph} report={largeReport} selectedRegionId={null} selectedRiskId={selectedId} onRegionSelect={() => {}} onRiskSelect={() => {}} />)

    const list = screen.getByRole('list', { name: 'Print warnings' })
    expect(list.querySelectorAll(':scope > li > button')).toHaveLength(60)
    expect(within(list).getByRole('button', { name: /Finding 1500/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Warning severity totals')).toHaveTextContent(`${largeReport.summary.error} error`)
    expect(screen.getByText(/Showing 60 of 1578 matching warnings/)).toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox', { name: 'Show severity' }), { target: { value: 'info' } })
    expect(list.querySelectorAll(':scope > li > button').length).toBeLessThanOrEqual(60)
    expect(within(list).getByRole('button', { name: /Finding 1500/ })).toBeInTheDocument()
    expect(screen.getByText(/plus the selected warning/)).toBeInTheDocument()
  })

  it('shows the selected risk reason, applied classifier measurements, and suggestions', () => {
    const onSuggestionSelect = vi.fn()
    render(<RegionInspector graph={graph} report={report} selectedRegionId={firstId} selectedRiskId="risk-island" onRegionSelect={() => {}} onRiskSelect={() => {}} onSuggestionSelect={onSuggestionSelect} />)

    expect(screen.getByText(/physical reason that must remain inspectable/)).toBeInTheDocument()
    expect(screen.getByText('physical-clearance-v1@1.2.0')).toBeInTheDocument()
    expect(screen.getByText('0.2 mm')).toBeInTheDocument()
    expect(screen.getByText('0.4 mm')).toBeInTheDocument()
    const suggestion = screen.getByRole('button', { name: /Widen feature/ })
    expect(suggestion).toHaveTextContent('Destructive · confirmation required')
    fireEvent.click(suggestion)
    expect(onSuggestionSelect).toHaveBeenCalledWith({ warning: report.warnings[0], suggestion: report.warnings[0].suggestions[0] })
  })

  it('does not expose an async scroll result as a React effect cleanup', () => {
    const original = Element.prototype.scrollIntoView
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value: vi.fn(() => Promise.resolve()),
    })
    try {
      const { rerender } = render(
        <RegionInspector graph={graph} report={report} selectedRegionId={null} selectedRiskId="risk-island" onRegionSelect={() => {}} onRiskSelect={() => {}} />,
      )
      expect(() => rerender(
        <RegionInspector graph={graph} report={report} selectedRegionId={null} selectedRiskId={null} onRegionSelect={() => {}} onRiskSelect={() => {}} />,
      )).not.toThrow()
    } finally {
      Object.defineProperty(Element.prototype, 'scrollIntoView', {
        configurable: true,
        value: original,
      })
    }
  })

  it('synchronizes region-list selection and supports searching exact IDs and colors', () => {
    const onRegionSelect = vi.fn()
    render(<RegionInspector graph={graph} report={report} selectedRegionId={secondId} selectedRiskId={null} onRegionSelect={onRegionSelect} onRiskSelect={() => {}} />)

    const selected = screen.getByRole('button', { name: /bbbbbbbb/ })
    expect(selected).toHaveAttribute('aria-pressed', 'true')
    fireEvent.change(screen.getByRole('textbox', { name: 'Find region' }), { target: { value: '#112233' } })
    expect(screen.getByRole('button', { name: /aaaaaaaa/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /bbbbbbbb/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /aaaaaaaa/ }))
    expect(onRegionSelect).toHaveBeenCalledWith(firstId)
  })

  it('honestly handles previews that do not yet contain region analysis', () => {
    render(<RegionInspector graph={null} report={null} selectedRegionId={null} selectedRiskId={null} onRegionSelect={() => {}} onRiskSelect={() => {}} />)
    expect(screen.getByRole('status')).toHaveTextContent('Region analysis is not available yet')
  })

  it('keeps list inspection available when exact canvas membership cannot be loaded', () => {
    render(
      <RegionInspector
        graph={graph}
        report={report}
        selectedRegionId={null}
        selectedRiskId={null}
        onRegionSelect={() => {}}
        onRiskSelect={() => {}}
        canvasSelectionStatus="error"
        canvasSelectionMessage="Assignment hash verification failed."
      />,
    )
    expect(screen.getByText('Assignment hash verification failed.')).toHaveAttribute(
      'role',
      'status',
    )
    expect(screen.getByRole('list', { name: 'Print warnings' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Printable regions' })).toBeInTheDocument()
  })
})
