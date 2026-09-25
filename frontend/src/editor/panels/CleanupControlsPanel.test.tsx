import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CleanupControlValues, CleanupProfileContext } from '../cleanupModel'
import { CleanupControlsPanel, type CleanupControlsPanelProps } from './CleanupControlsPanel'

const values: CleanupControlValues = {
  minimum_island_area_mm2: 0.283,
  maximum_tiny_hole_area_mm2: 0.503,
  smoothing_radius_mm: 0,
  minimum_line_width_mm: 0.48,
  minimum_neck_width_mm: 0.6,
  minimum_gap_width_mm: 0.5,
  long_line_minimum_length_mm: 1.6,
  preserve_long_lines: true,
}

const recommendations = Object.fromEntries(
  Object.entries(values)
    .filter(([field]) => field !== 'preserve_long_lines')
    .map(([field, value]) => [field, {
      value,
      profileValue: value,
      source: 'profile',
      rationale: `Rationale for ${field}`,
    }]),
) as CleanupProfileContext['recommendations']

const profile: CleanupProfileContext = {
  id: 'bbl-p2s-0.4-pla',
  displayName: 'Bambu Lab P2S · 0.4 mm · PLA',
  catalogFingerprint: 'a'.repeat(64),
  nozzleMm: 0.4,
  evidenceStatus: 'pending_print_calibration',
  warning: 'Engineering baseline pending a printed coupon.',
  recommendations,
  preserveLongLinesDefault: true,
  islandDiameterMm: 0.6,
  tinyHoleDiameterMm: 0.8,
}

function props(overrides: Partial<CleanupControlsPanelProps> = {}): CleanupControlsPanelProps {
  return {
    values,
    profile,
    mode: 'advanced',
    previewFreshness: 'current',
    pixelScale: { mmPerPixelX: 0.1, mmPerPixelY: 0.2 },
    onModeChange: vi.fn(),
    mergePolicy: 'review',
    onMergePolicyChange: vi.fn(),
    onChange: vi.fn(),
    ...overrides,
  }
}

afterEach(cleanup)

describe('CleanupControlsPanel', () => {
  it('renders provenance, physical units, evidence, diameter, and anisotropic pixel guidance', () => {
    render(<CleanupControlsPanel {...props()} />)

    expect(screen.getByText('Bambu Lab P2S · 0.4 mm · PLA')).toBeInTheDocument()
    expect(screen.getByText('0.4 mm nozzle')).toBeInTheDocument()
    expect(screen.getByText('Provisional engineering baseline')).toBeInTheDocument()
    expect(screen.getByText(/Profile diameter guidance: islands 0.6 mm/)).toBeInTheDocument()
    expect(screen.getAllByText(/horizontally or .* vertically/).length).toBeGreaterThan(1)
    expect(screen.getAllByText('Profile value').length).toBeGreaterThan(1)
    expect(screen.getByLabelText('Island threshold')).toHaveAccessibleDescription(/square millimetres/)
    expect(screen.getAllByText('Help & profile value')).toHaveLength(7)
  })

  it('uses pressed semantics for basic/advanced mode and hides advanced controls', () => {
    const onModeChange = vi.fn()
    render(<CleanupControlsPanel {...props({ mode: 'basic', onModeChange })} />)

    expect(screen.getByRole('button', { name: 'Basic' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByLabelText('Minimum line width')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Long-line minimum length')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Advanced' }))
    expect(onModeChange).toHaveBeenCalledWith('advanced')
  })

  it('emits provisional pointer changes and commits on pointer-up', () => {
    const onChange = vi.fn()
    render(<CleanupControlsPanel {...props({ onChange })} />)
    const slider = screen.getByRole('slider', { name: 'Lines slider' })

    fireEvent.change(slider, { target: { value: '0.72' } })
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      phase: 'provisional',
      reason: 'field',
      activePreset: 'custom',
      changedFields: ['minimum_line_width_mm'],
    }))
    expect(onChange.mock.calls.at(-1)?.[0].overrides.set).toEqual(['minimum_line_width_mm'])

    fireEvent.pointerUp(slider)
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ phase: 'commit' }))
  })

  it('does not commit or round an untouched slider when focus leaves it', () => {
    const onChange = vi.fn()
    render(<CleanupControlsPanel {...props({ onChange })} />)
    const slider = screen.getByRole('slider', { name: 'Lines slider' })

    fireEvent.focus(slider)
    fireEvent.blur(slider)

    expect(onChange).not.toHaveBeenCalled()
  })

  it('commits number entry on Enter and exposes generic cleanup override provenance', () => {
    const onChange = vi.fn()
    render(<CleanupControlsPanel {...props({ onChange })} />)
    const input = screen.getByLabelText('Island threshold')

    fireEvent.change(input, { target: { value: '0.9' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    const event = onChange.mock.calls.at(-1)?.[0]
    expect(event).toMatchObject({ phase: 'commit', clamped: false })
    expect(event.overrides.backendCleanup.set).toEqual(['min_island_mm2'])
  })

  it('makes destructive island cleanup an explicit policy choice', () => {
    const onMergePolicyChange = vi.fn()
    render(<CleanupControlsPanel {...props({ onMergePolicyChange })} />)

    expect(screen.getByLabelText('Automatic island policy')).toHaveValue('review')
    expect(screen.getByText(/filling is always an explicit edit/i)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Automatic island policy'), {
      target: { value: 'dominant_neighbor' },
    })
    expect(onMergePolicyChange).toHaveBeenCalledWith('dominant_neighbor')
  })

  it('clamps out-of-range numeric values and reports the clamp on commit', () => {
    const onChange = vi.fn()
    render(<CleanupControlsPanel {...props({ onChange })} />)
    const input = screen.getByLabelText('Minimum gap width')

    fireEvent.change(input, { target: { value: '100' } })
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      phase: 'provisional',
      clamped: true,
      values: expect.objectContaining({ minimum_gap_width_mm: 10 }),
    }))
    fireEvent.blur(input)
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ phase: 'commit', clamped: true }))
  })

  it('applies presets as committed deterministic changes and clears overrides for profile defaults', () => {
    const onChange = vi.fn()
    render(<CleanupControlsPanel {...props({
      onChange,
      activePreset: 'custom',
      overriddenFields: ['minimum_island_area_mm2', 'minimum_gap_width_mm'],
    })} />)

    expect(screen.getByText('Custom settings')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Profile defaults' }))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      phase: 'commit',
      reason: 'preset',
      activePreset: 'profile',
      overrides: expect.objectContaining({
        clear: [
          'minimum_island_area_mm2',
          'maximum_tiny_hole_area_mm2',
          'smoothing_radius_mm',
          'minimum_line_width_mm',
          'minimum_neck_width_mm',
          'minimum_gap_width_mm',
          'preserve_long_lines',
          'long_line_minimum_length_mm',
        ],
        backendCleanup: {
          set: [],
          clear: [
            'min_island_mm2',
            'max_hole_mm2',
            'smoothing_radius_mm',
            'minimum_line_width_mm',
            'minimum_neck_width_mm',
            'minimum_gap_width_mm',
            'long_line_minimum_length_mm',
          ],
        },
      }),
    }))
  })

  it('resets one section while preserving unrelated values and clears only its provenance', () => {
    const onChange = vi.fn()
    const edited = { ...values, minimum_island_area_mm2: 2, minimum_gap_width_mm: 2 }
    render(<CleanupControlsPanel {...props({
      values: edited,
      onChange,
      overriddenFields: ['minimum_island_area_mm2', 'minimum_gap_width_mm'],
    })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reset Region cleanup to profile defaults' }))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      reason: 'section_reset',
      activePreset: 'custom',
      values: expect.objectContaining({ minimum_island_area_mm2: 0.283, minimum_gap_width_mm: 2 }),
      overrides: expect.objectContaining({
        clear: [
          'minimum_island_area_mm2',
          'maximum_tiny_hole_area_mm2',
          'smoothing_radius_mm',
        ],
      }),
    }))
  })

  it('marks overrides and dirty state in text rather than color alone', () => {
    render(<CleanupControlsPanel {...props({
      overriddenFields: ['minimum_gap_width_mm'],
      dirtyFields: ['minimum_gap_width_mm'],
    })} />)

    expect(screen.getByText('Unsaved cleanup edits')).toBeInTheDocument()
    expect(screen.getByText('User override · Unsaved')).toBeInTheDocument()
  })

  it.each([
    ['stale', 'Preview is out of date'],
    ['rendering', 'Updating the preview'],
    ['unavailable', 'Render a preview'],
  ] as const)('announces %s preview state', (previewFreshness, message) => {
    render(<CleanupControlsPanel {...props({ previewFreshness })} />)
    expect(screen.getByText(new RegExp(message))).toBeInTheDocument()
  })

  it('disables all mutations and explains profile warnings without relying on hover', () => {
    render(<CleanupControlsPanel {...props({ disabled: true })} />)
    expect(screen.getByRole('button', { name: 'Reliable print' })).toBeDisabled()
    expect(screen.getByLabelText('Minimum neck width')).toBeDisabled()
    expect(screen.getByText('Engineering baseline pending a printed coupon.')).toHaveAttribute('role', 'note')
  })
})
