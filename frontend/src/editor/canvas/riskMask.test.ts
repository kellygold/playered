import { describe, expect, it, vi } from 'vitest'
import type { PreviewJobResult, RegionGraph, RiskReport } from '../../contracts'
import type { RegionAssignmentArtifact, RegionAssignmentMap } from '../inspector/regionAssignment'
import {
  RISK_MASK_ENCODING,
  exactWarningsAtCanvasPick,
  fetchVerifiedRiskMask,
  riskMaskArtifact,
  riskOverlayRgba,
  type RiskMaskArtifact,
  type RiskMaskMap,
} from './riskMask'

const firstId = 'region_aaaaaaaaaaaaaaaaaaaaaaaa'
const secondId = 'region_bbbbbbbbbbbbbbbbbbbbbbbb'
const graph = {
  schema_version: 1,
  width_px: 3,
  height_px: 2,
  width_mm: 3,
  height_mm: 2,
  pixel_width_mm: 1,
  pixel_height_mm: 1,
  active_pixel_count: 5,
  palette: [{ label: 0, color: '#111111' }, { label: 1, color: '#ff6600' }],
  regions: [
    { id: firstId, label: 0, pixel_count: 3 },
    { id: secondId, label: 1, pixel_count: 2 },
  ].map((region) => ({
    ...region,
    color: region.label ? '#ff6600' : '#111111',
    area_mm2: region.pixel_count,
    perimeter_edge_count: 4,
    perimeter_mm: 4,
    compactness: 0.7,
    pixel_bounds: { x: 0, y: 0, width: 3, height: 2 },
    physical_bounds: { x_mm: 0, y_mm: 0, width_mm: 3, height_mm: 2 },
    border_contact: { top_mm: 0, right_mm: 0, bottom_mm: 0, left_mm: 0, total_mm: 0, touches_canvas_border: false },
    width_estimate: { method: 'orthogonal-run-spans-v1' as const, minimum_mm: 1, median_mm: 1, p95_mm: 1, maximum_mm: 1 },
    neighbor_region_ids: [],
  })),
  adjacency: [],
} satisfies RegionGraph

const finding = (id: string, severity: 'info' | 'warning' | 'error', regionId: string): RiskReport['warnings'][number] => ({
  id,
  code: 'small_island',
  feature_key: id,
  severity,
  title: id,
  explanation: id,
  affected_region_ids: [regionId],
  affected_labels: [],
  affected_bounds: [],
  measurements: [{ key: 'area', role: 'measured', value: 1, unit: 'mm2' }],
  suggestions: [{ kind: 'review', title: 'Review', explanation: 'Review', destructive: false, requires_confirmation: false }],
  classifier_id: 'test',
  classifier_version: '1',
})

const report: RiskReport = {
  schema_version: 1,
  graph_fingerprint: '1'.repeat(64),
  options_fingerprint: '2'.repeat(64),
  nozzle_mm: 0.4,
  evaluated_codes: ['small_island'],
  pending_codes: [],
  warnings: [finding('risk-info', 'info', firstId), finding('risk-error', 'error', secondId)],
  summary: { total: 2, info: 1, warning: 0, error: 1, by_code: [{ code: 'small_island', count: 2 }] },
}

const assignmentArtifact: RegionAssignmentArtifact = {
  kind: 'region-assignment', downloadUrl: '/assignment', sha256: '3'.repeat(64), width: 3, height: 2,
  encoding: 'int32-region-index-row-major-little-endian', inactiveIndex: -1, regionCount: 2,
  graphFingerprint: report.graph_fingerprint,
}

const descriptor: RiskMaskArtifact = {
  kind: 'risk-mask', downloadUrl: '/risk-mask', sha256: '4'.repeat(64), width: 3, height: 2,
  encoding: RISK_MASK_ENCODING, severityBits: { info: 1, warning: 2, error: 4 },
  exactWarningCount: 2, approximateWarningCount: 0, sourceSha256: '5'.repeat(64),
  configFingerprint: '6'.repeat(64), operationsFingerprint: '7'.repeat(64),
  assignmentSha256: assignmentArtifact.sha256, graphFingerprint: report.graph_fingerprint,
  riskReportFingerprint: '8'.repeat(64),
}

const assignment: RegionAssignmentMap = {
  width: 3, height: 2, graphFingerprint: report.graph_fingerprint,
  regionIds: [firstId, secondId], indices: new Int32Array([0, 1, -1, 0, 1, 0]),
}

const mask: RiskMaskMap = {
  ...descriptor,
  pixels: new Uint8Array([1, 4, 0, 1, 4, 1]),
}

describe('exact risk mask adapter', () => {
  it('binds the artifact to source, config, operations, assignment, graph, and report evidence', () => {
    const artifact = {
      id: 'artifact-risk-mask', job_id: 'job', revision_id: null, kind: 'risk-mask', sha256: descriptor.sha256,
      derivation_key: '9'.repeat(64), media_type: 'application/octet-stream', byte_size: 6,
      metadata: {
        schema_version: 1, width: 3, height: 2, encoding: RISK_MASK_ENCODING,
        severity_bits: descriptor.severityBits, exact_warning_count: 2, approximate_warning_count: 0,
        source_sha256: descriptor.sourceSha256, config_fingerprint: descriptor.configFingerprint,
        operations_fingerprint: descriptor.operationsFingerprint, assignment_sha256: descriptor.assignmentSha256,
        graph_fingerprint: descriptor.graphFingerprint, risk_report_fingerprint: descriptor.riskReportFingerprint,
      },
      download_url: descriptor.downloadUrl, created_at: '2026-07-16T00:00:00Z',
    } satisfies PreviewJobResult['artifacts'][number]
    expect(riskMaskArtifact([artifact], graph, report, assignmentArtifact, {
      sourceSha256: descriptor.sourceSha256,
      configFingerprint: descriptor.configFingerprint,
      operationsFingerprint: descriptor.operationsFingerprint,
      riskReportFingerprint: descriptor.riskReportFingerprint,
    })).toEqual(descriptor)
    expect(() => riskMaskArtifact([artifact], graph, report, assignmentArtifact, { sourceSha256: '0'.repeat(64) })).toThrow(/source fingerprint is stale/)
  })

  it('verifies compact bytes and rejects unsupported bits', async () => {
    const fetcher = vi.fn(async () => new Response(mask.pixels.slice().buffer as ArrayBuffer, { status: 200 }))
    await expect(fetchVerifiedRiskMask(descriptor, { fetcher, hashBytes: async () => descriptor.sha256 })).resolves.toMatchObject({ width: 3, pixels: mask.pixels })
    await expect(fetchVerifiedRiskMask(descriptor, { fetcher: async () => new Response(new Uint8Array([8, 0, 0, 0, 0, 0]).buffer as ArrayBuffer), hashBytes: async () => descriptor.sha256 })).rejects.toThrow(/unsupported severity bits/)
  })

  it('renders exact selected and overview membership without region bounds', () => {
    const selected = riskOverlayRgba(mask, assignment, graph, report, { mode: 'selected', selectedRiskId: 'risk-error' })
    expect(selected[1 * 4 + 3]).toBeGreaterThan(0)
    expect(selected[0 * 4 + 3]).toBe(0)
    const overview = riskOverlayRgba(mask, assignment, graph, report, { mode: 'overview' })
    expect(overview[0 * 4 + 3]).toBeGreaterThan(0)
    expect(overview[1 * 4 + 3]).toBeGreaterThan(0)
    expect(overview[2 * 4 + 3]).toBe(0)
  })

  it('hit-tests exact warning membership and orders highest severity first', () => {
    report.warnings[0].affected_labels = [1]
    expect(exactWarningsAtCanvasPick(assignment, graph, report, { pixelX: 1, pixelY: 0 }).map((warning) => warning.id)).toEqual(['risk-error', 'risk-info'])
    report.warnings[0].affected_labels = []
  })
})
