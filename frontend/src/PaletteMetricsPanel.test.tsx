import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { PaletteMetricsPanel } from './PaletteMetricsPanel'
import type { PaletteColor, PaletteMetrics } from './contracts'

const colors: PaletteColor[] = [
  { id: 'bone', name: 'Bone White', hex: '#CBC6B8', locked: false, filament_id: null },
  { id: 'black', name: 'Charcoal', hex: '#000000', locked: false, filament_id: null },
]

const metrics: PaletteMetrics = {
  schema_version: 1,
  config_sha256: 'a'.repeat(64),
  quantization_fingerprint: 'b'.repeat(64),
  options_fingerprint: 'c'.repeat(64),
  palette: ['#CBC6B8', '#000000'],
  visible_pixel_count: 10_000,
  physical_area_mm2: 40_000,
  colors: [
    {
      index: 0,
      color: '#CBC6B8',
      pixel_count: 6250,
      coverage_ratio: 0.625,
      mean_delta_e: 4.15,
      p95_delta_e: 8.4,
      component_count: 2,
      largest_component_share: 0.96,
      smallest_component_pixels: 250,
      smallest_component_mm2: 1000,
    },
    {
      index: 1,
      color: '#000000',
      pixel_count: 3750,
      coverage_ratio: 0.375,
      mean_delta_e: 5.02,
      p95_delta_e: 9.1,
      component_count: 5,
      largest_component_share: 0.8,
      smallest_component_pixels: 1,
      smallest_component_mm2: 4,
    },
  ],
  reconstruction: {
    alpha_weighted_mean_delta_e: 4.476,
    alpha_weighted_p95_delta_e: 8.8,
  },
  adjacency: [
    { first_index: 0, second_index: 1, delta_e: 72.34, boundary_edge_count: 900 },
  ],
  minimum_adjacent_delta_e: 72.34,
  fragmentation: {
    component_count: 7,
    excess_component_count: 5,
    single_pixel_component_count: 1,
    components_per_100_mm2: 0.0175,
  },
}

afterEach(cleanup)

describe('PaletteMetricsPanel', () => {
  it('explains the empty state before the first analysis', () => {
    render(<PaletteMetricsPanel colors={colors} loading={false} metrics={null} stale={false} />)

    expect(screen.getByText('No palette analysis yet')).toBeInTheDocument()
    expect(screen.getByText(/Render a preview/)).toBeInTheDocument()
    expect(screen.getByText('Waiting')).toBeInTheDocument()
  })

  it('announces the loading state and what is being measured', () => {
    render(<PaletteMetricsPanel colors={colors} loading metrics={null} stale />)

    expect(screen.getByRole('status')).toHaveTextContent('Analyzing palette relationships')
    expect(screen.getByText(/touching boundaries/)).toBeInTheDocument()
    expect(screen.getByText('Analyzing')).toBeInTheDocument()
  })

  it('renders rounded comparative signals, coverage, and honest interpretation copy', () => {
    render(<PaletteMetricsPanel colors={colors} loading={false} metrics={metrics} stale={false} />)

    expect(screen.getByText('ΔE 4.5')).toBeInTheDocument()
    expect(screen.getByText('ΔE 72.3')).toBeInTheDocument()
    expect(screen.getByText('Bone White meets Charcoal.')).toBeInTheDocument()
    expect(screen.getByText('62.5%')).toBeInTheDocument()
    expect(screen.getByText('37.5%')).toBeInTheDocument()
    expect(screen.getByText(/not a score/)).toBeInTheDocument()
    expect(screen.getByText(/No single value guarantees printability/)).toBeInTheDocument()
  })

  it('keeps prior metrics visible but marks them as updating', () => {
    render(<PaletteMetricsPanel colors={colors} loading metrics={metrics} stale />)

    expect(screen.getByText('Analyzing')).toBeInTheDocument()
    expect(screen.getByText(/Updating these signals/)).toBeInTheDocument()
    expect(screen.getByText('62.5%')).toBeInTheDocument()
  })
})
