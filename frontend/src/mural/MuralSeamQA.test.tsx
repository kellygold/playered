import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MuralSeamQA } from './MuralSeamQA'
import type {
  MuralSeamQaReport,
  SharedEdgeEvidence,
  TileSeamEvidence,
} from './seamTypes'

function tile(index: number): TileSeamEvidence {
  const row = Math.floor(index / 3) + 1
  const column = index % 3 + 1
  const tileId = `tile-r${String(row).padStart(2, '0')}-c${String(column).padStart(2, '0')}`
  return {
    tile_id: tileId,
    plate_number: index + 1,
    row,
    column,
    rotation_degrees: index === 4 ? 90 : 0,
    master_pixel_bounds: {
      x_start: (column - 1) * 400,
      y_start: (row - 1) * 400,
      x_end: column === 3 ? 1201 : column * 400,
      y_end: row === 2 ? 799 : row * 400,
    },
    shared_edge_count: column === 2 ? 3 : 2,
    protected_sides: [
      ...(row > 1 ? ['top' as const] : []),
      ...(column < 3 ? ['right' as const] : []),
      ...(row < 2 ? ['bottom' as const] : []),
      ...(column > 1 ? ['left' as const] : []),
    ],
    visible_labels_sha256: String(index + 1).repeat(64),
    vector_clip_bounds_master_mm: {
      x: (column - 1) * 200,
      y: row === 1 ? 200 : 0,
      width: 200,
      height: 200,
    },
    tile_topology_artifact_sha256: '7'.repeat(64),
    tile_geometry_fingerprint: '8'.repeat(64),
    topology_source_mode: 'master_topology_exact_label_clip',
    fits_build_plate: true,
    risk: index === 1
      ? {
          status: 'warning',
          finding_count: 1,
          info_count: 0,
          warning_count: 1,
          error_count: 0,
          findings: [{
            tile_id: tileId,
            code: 'tiny_feature',
            severity: 'warning',
            message: 'A small feature approaches the nozzle threshold.',
          }],
        }
      : {
          status: 'pass',
          finding_count: 0,
          info_count: 0,
          warning_count: 0,
          error_count: 0,
          findings: [],
        },
  }
}

function seam(
  id: string,
  orientation: 'vertical' | 'horizontal',
  coordinate: number,
  first: string,
  second: string,
): SharedEdgeEvidence {
  return {
    seam_id: id,
    orientation,
    coordinate_px: coordinate,
    span_start_px: 0,
    span_end_px: orientation === 'vertical' ? 400 : 400,
    first_tile_id: first,
    second_tile_id: second,
    first_side: orientation === 'vertical' ? 'right' : 'bottom',
    second_side: orientation === 'vertical' ? 'left' : 'top',
    first_boundary_px: coordinate,
    second_boundary_px: coordinate,
    first_guard_sha256: 'a'.repeat(64),
    second_guard_sha256: 'b'.repeat(64),
    first_guard_role: 'first_tile_inner_edge_strip',
    second_guard_role: 'second_tile_inner_edge_strip',
    guard_hashes_expected_to_match: false,
    exact_coordinate_match: true,
    exact_span_match: true,
    no_gap_no_overlap: true,
    evidence_kind: 'raster_partition_boundary',
    overlay_source: 'diagnostic_metadata_only',
  }
}

function report(): MuralSeamQaReport {
  const tiles = Array.from({ length: 6 }, (_, index) => tile(index))
  const seams = [
    seam('seam-v-400-0-400', 'vertical', 400, tiles[0].tile_id, tiles[1].tile_id),
    seam('seam-v-800-0-400', 'vertical', 800, tiles[1].tile_id, tiles[2].tile_id),
    seam('seam-v-400-400-799', 'vertical', 400, tiles[3].tile_id, tiles[4].tile_id),
    seam('seam-v-800-400-799', 'vertical', 800, tiles[4].tile_id, tiles[5].tile_id),
    seam('seam-h-400-0-400', 'horizontal', 400, tiles[0].tile_id, tiles[3].tile_id),
    seam('seam-h-400-400-800', 'horizontal', 400, tiles[1].tile_id, tiles[4].tile_id),
    seam('seam-h-400-800-1201', 'horizontal', 400, tiles[2].tile_id, tiles[5].tile_id),
  ]
  return {
    schema_version: 1,
    request_fingerprint: 'c'.repeat(64),
    partition_sha256: 'd'.repeat(64),
    rows: 2,
    columns: 3,
    panel_size_mm: { width: 200, height: 200 },
    master_size_mm: { width: 600, height: 400 },
    assembled_size_mm: { width: 616, height: 412 },
    horizontal_gap_mm: 8,
    vertical_gap_mm: 12,
    expected_seam_count: 7,
    exact_shared_edge_count: 7,
    failed_shared_edge_count: 0,
    status: 'warning',
    artwork: {
      authoritative_master_sha256: 'e'.repeat(64),
      recomposed_visible_art_sha256: 'e'.repeat(64),
      visible_art_unchanged: true,
      overlay_storage: 'metadata_only',
      overlay_pixels_written: 0,
      tile_number_pixels_written: 0,
      orientation_marker_pixels_written: 0,
      proof: 'Every tile recomposed byte-for-byte; diagnostics write zero pixels.',
    },
    topology: {
      evidence_kind: 'exact_master_topology_clip',
      label_partition_sha256: 'f'.repeat(64),
      topology_partition_sha256: '1'.repeat(64),
      seam_topology_sha256: '2'.repeat(64),
      master_topology_fingerprint: '3'.repeat(64),
      master_topology_artifact_sha256: '4'.repeat(64),
      recomposed_labels_sha256: 'e'.repeat(64),
      source_pixel_count: 959599,
      represented_pixel_count: 959599,
      every_master_pixel_represented: true,
    },
    seams,
    tiles,
  }
}

afterEach(cleanup)

describe('MuralSeamQA', () => {
  it('renders an assembled six-plate preview with exaggerated overlay-only diagnostics', () => {
    const { container } = render(<MuralSeamQA masterImageUrl="/master.png" report={report()} />)

    expect(screen.getByRole('heading', { name: 'Shared-edge inspector' })).toBeInTheDocument()
    expect(screen.getByText('7/7 gap-free')).toBeInTheDocument()
    expect(screen.getByText('959599 pixels closed')).toBeInTheDocument()
    expect(screen.getByText('Byte-identical')).toBeInTheDocument()
    expect(screen.getByText('616 × 412 mm')).toBeInTheDocument()
    expect(screen.getByLabelText('Assembled mural preview')).toHaveAttribute(
      'data-diagnostics',
      'true',
    )
    expect(screen.getAllByRole('button', { name: /Plate / })).toHaveLength(6)
    expect(container.querySelectorAll('.mural-seam-qa__tile img')).toHaveLength(6)
    expect(container.querySelector('[data-overlay-proof="metadata-only"]')).toHaveTextContent(
      'Overlay only · not printed',
    )
    expect(screen.getByText('No seam guides are baked into visible art')).toBeInTheDocument()
    expect(screen.getByText('Overlay pixels').parentElement).toHaveTextContent('0')
  })

  it('can remove every diagnostic guide while leaving the assembled artwork visible', () => {
    render(<MuralSeamQA masterImageUrl="/master.png" report={report()} />)
    const toggle = screen.getByRole('button', { name: 'Hide diagnostic overlay' })

    fireEvent.click(toggle)

    expect(screen.getByLabelText('Assembled mural preview')).toHaveAttribute(
      'data-diagnostics',
      'false',
    )
    expect(screen.getByRole('button', { name: 'Show exaggerated seams' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    expect(screen.getAllByRole('presentation')).toHaveLength(6)
  })

  it('shows plate orientation, scoped risks, and exact adjacent-boundary evidence', () => {
    render(<MuralSeamQA masterImageUrl="/master.png" report={report()} />)
    const plateTwo = screen.getByRole('button', {
      name: 'Plate 2, row 1, column 2, native orientation, 1 risks',
    })
    fireEvent.click(plateTwo)

    const selection = screen.getByRole('heading', { name: 'Plate 2 · tile-r01-c02' }).parentElement
      ?.parentElement?.parentElement
    expect(selection).not.toBeNull()
    expect(within(selection as HTMLElement).getByText('tiny feature')).toBeInTheDocument()
    expect(within(selection as HTMLElement).getByText(/x 400–800 · y 0–400/)).toBeInTheDocument()
    const details = within(selection as HTMLElement).getByText(
      'Raster boundary evidence · 3',
    )
    fireEvent.click(details)
    expect(within(selection as HTMLElement).getAllByText('No gap / overlap')).toHaveLength(3)

    fireEvent.click(screen.getByRole('button', {
      name: 'Plate 5, row 2, column 2, rotate 90 degrees, 0 risks',
    }))
    expect(screen.getByText('Rotate 90° on plate')).toBeInTheDocument()
  })

  it('keeps short-viewport layout rail-owned and the seam overlay CSS-only', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/mural/MuralSeamQA.css'), 'utf8')
    expect(css).toMatch(/@media \(max-width: 540px\), \(max-height: 560px\)/)
    expect(css).toMatch(/data-diagnostics="true"[\s\S]*?outline:/)
    expect(css).not.toMatch(/position:\s*fixed/)
  })
})
