import { describe, expect, it, vi } from 'vitest'
import type { PreviewJobResult, RegionGraph } from '../../contracts'
import {
  REGION_ASSIGNMENT_ENCODING,
  decodeRegionAssignment,
  exactRegionAtCanvasPick,
  fetchVerifiedRegionAssignment,
  regionAssignmentArtifact,
  type RegionAssignmentArtifact,
} from './regionAssignment'

const firstId = 'region_aaaaaaaaaaaaaaaaaaaaaaaa'
const secondId = 'region_bbbbbbbbbbbbbbbbbbbbbbbb'

const node = (
  id: string,
  label: number,
  pixelCount: number,
): RegionGraph['regions'][number] => ({
  id,
  label,
  color: label ? '#FF6600' : '#111111',
  pixel_count: pixelCount,
  area_mm2: pixelCount,
  perimeter_edge_count: 8,
  perimeter_mm: 8,
  compactness: 0.5,
  // Deliberately identical: bounds alone cannot resolve these components exactly.
  pixel_bounds: { x: 0, y: 0, width: 3, height: 2 },
  physical_bounds: { x_mm: 0, y_mm: 0, width_mm: 3, height_mm: 2 },
  border_contact: {
    top_mm: 1,
    right_mm: 1,
    bottom_mm: 1,
    left_mm: 1,
    total_mm: 4,
    touches_canvas_border: true,
  },
  width_estimate: {
    method: 'orthogonal-run-spans-v1',
    minimum_mm: 1,
    median_mm: 1,
    p95_mm: 2,
    maximum_mm: 2,
  },
  neighbor_region_ids: [id === firstId ? secondId : firstId],
})

const graph: RegionGraph = {
  schema_version: 1,
  width_px: 3,
  height_px: 2,
  width_mm: 3,
  height_mm: 2,
  pixel_width_mm: 1,
  pixel_height_mm: 1,
  active_pixel_count: 5,
  palette: [{ label: 0, color: '#111111' }, { label: 1, color: '#FF6600' }],
  regions: [node(firstId, 0, 3), node(secondId, 1, 2)],
  adjacency: [{ first_region_id: firstId, second_region_id: secondId, boundary_edge_count: 3, boundary_length_mm: 3 }],
}

const descriptor: RegionAssignmentArtifact = {
  kind: 'region-assignment',
  downloadUrl: '/api/artifacts/assignment',
  sha256: 'a'.repeat(64),
  width: 3,
  height: 2,
  encoding: REGION_ASSIGNMENT_ENCODING,
  inactiveIndex: -1,
  regionCount: 2,
  graphFingerprint: 'f'.repeat(64),
}

function encoded(values: number[]) {
  const bytes = new Uint8Array(values.length * 4)
  const view = new DataView(bytes.buffer)
  values.forEach((value, index) => view.setInt32(index * 4, value, true))
  return bytes
}

describe('exact region assignment adapter', () => {
  it('validates the generic artifact metadata against the graph and risk fingerprint', () => {
    const artifact = {
      id: 'artifact-assignment',
      job_id: 'job-1',
      revision_id: null,
      kind: 'region-assignment',
      sha256: descriptor.sha256,
      derivation_key: 'd'.repeat(64),
      media_type: 'application/octet-stream',
      byte_size: 24,
      metadata: {
        width: 3,
        height: 2,
        encoding: REGION_ASSIGNMENT_ENCODING,
        inactive_index: -1,
        region_count: 2,
        graph_fingerprint: descriptor.graphFingerprint,
      },
      download_url: descriptor.downloadUrl,
      created_at: '2026-07-16T00:00:00Z',
    } satisfies PreviewJobResult['artifacts'][number]

    expect(regionAssignmentArtifact([artifact], graph, descriptor.graphFingerprint)).toEqual(
      descriptor,
    )
    expect(() => regionAssignmentArtifact([artifact], graph, '0'.repeat(64))).toThrow(
      /fingerprint/,
    )
  })

  it('resolves exact graph membership with overlapping bounds and inactive pixels', () => {
    const assignment = decodeRegionAssignment(encoded([0, 1, -1, 0, 1, 0]), descriptor, graph)
    expect(exactRegionAtCanvasPick(assignment, graph, { pixelX: 1, pixelY: 0 })?.id).toBe(
      secondId,
    )
    expect(exactRegionAtCanvasPick(assignment, graph, { pixelX: 2, pixelY: 0 })).toBeNull()
    expect(exactRegionAtCanvasPick(assignment, graph, { pixelX: 2, pixelY: 1 })?.id).toBe(
      firstId,
    )
  })

  it('rejects truncated, out-of-range, and graph-inconsistent assignments', () => {
    expect(() => decodeRegionAssignment(encoded([0]), descriptor, graph)).toThrow(/expected 24/)
    expect(() => decodeRegionAssignment(encoded([0, 2, -1, 0, 1, 0]), descriptor, graph)).toThrow(
      /invalid region index 2/,
    )
    expect(() => decodeRegionAssignment(encoded([0, 1, -1, 0, 0, 0]), descriptor, graph)).toThrow(
      /pixel count does not match/,
    )
  })

  it('verifies content-addressed bytes before decoding', async () => {
    const bytes = encoded([0, 1, -1, 0, 1, 0])
    const fetcher = vi.fn(async () => new Response(bytes, { status: 200 }))
    const hashBytes = vi.fn(async () => descriptor.sha256)
    await expect(
      fetchVerifiedRegionAssignment(descriptor, graph, { fetcher, hashBytes }),
    ).resolves.toMatchObject({ width: 3, height: 2, graphFingerprint: descriptor.graphFingerprint })
    expect(hashBytes).toHaveBeenCalledWith(bytes)
    await expect(
      fetchVerifiedRegionAssignment(
        { ...descriptor, sha256: '0'.repeat(64) },
        { ...graph },
        { fetcher, hashBytes },
      ),
    ).rejects.toThrow(/SHA-256/)
  })
})
