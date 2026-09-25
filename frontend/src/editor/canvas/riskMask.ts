import type { PreviewJobResult, RegionGraph, RiskReport } from '../../contracts'
import type { EditorCanvasPick } from './viewportMath'
import type { RegionAssignmentArtifact, RegionAssignmentMap } from '../inspector/regionAssignment'

export const RISK_MASK_ENCODING = 'uint8-risk-severity-bitset-row-major' as const

export type RiskSeverity = 'info' | 'warning' | 'error'
export type RiskMaskMode = 'selected' | 'severity' | 'overview'

export type RiskMaskArtifact = {
  kind: 'risk-mask'
  downloadUrl: string
  sha256: string
  width: number
  height: number
  encoding: typeof RISK_MASK_ENCODING
  severityBits: Record<RiskSeverity, number>
  exactWarningCount: number
  approximateWarningCount: number
  sourceSha256: string
  configFingerprint: string
  operationsFingerprint: string
  assignmentSha256: string
  graphFingerprint: string
  riskReportFingerprint: string
}

export type RiskMaskMap = Omit<RiskMaskArtifact, 'kind' | 'downloadUrl' | 'sha256'> & {
  pixels: Uint8Array
}

export type RiskMaskFingerprintContext = {
  sourceSha256?: string | null
  configFingerprint?: string | null
  operationsFingerprint?: string | null
  riskReportFingerprint?: string | null
}

type PreviewArtifact = PreviewJobResult['artifacts'][number]

const HEX_256 = /^[0-9a-f]{64}$/
const COLORS: Record<RiskSeverity, readonly [number, number, number, number]> = {
  info: [71, 223, 185, 130],
  warning: [245, 189, 104, 150],
  error: [255, 129, 120, 175],
}

function integerMetadata(metadata: Record<string, unknown>, key: string) {
  const value = metadata[key]
  if (!Number.isInteger(value)) throw new Error(`Risk mask metadata ${key} must be an integer.`)
  return value as number
}

function fingerprintMetadata(metadata: Record<string, unknown>, key: string) {
  const value = metadata[key]
  if (typeof value !== 'string' || !HEX_256.test(value)) {
    throw new Error(`Risk mask metadata ${key} must be a SHA-256 fingerprint.`)
  }
  return value
}

function severityBits(metadata: Record<string, unknown>) {
  const value = metadata.severity_bits
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Risk mask severity_bits metadata is missing.')
  }
  const bits = value as Record<string, unknown>
  if (bits.info !== 1 || bits.warning !== 2 || bits.error !== 4) {
    throw new Error('Risk mask severity bits must be info=1, warning=2, error=4.')
  }
  return { info: 1, warning: 2, error: 4 } satisfies Record<RiskSeverity, number>
}

export function riskMaskArtifact(
  artifacts: PreviewArtifact[],
  graph: RegionGraph,
  report: RiskReport,
  assignment: RegionAssignmentArtifact,
  expected: RiskMaskFingerprintContext = {},
): RiskMaskArtifact | null {
  const artifact = artifacts.find((candidate) => candidate.kind === 'risk-mask')
  if (!artifact) return null
  const metadata = artifact.metadata
  if (metadata.encoding !== RISK_MASK_ENCODING) {
    throw new Error(`Unsupported risk mask encoding: ${String(metadata.encoding)}.`)
  }
  const width = integerMetadata(metadata, 'width')
  const height = integerMetadata(metadata, 'height')
  const graphFingerprint = fingerprintMetadata(metadata, 'graph_fingerprint')
  const assignmentSha256 = fingerprintMetadata(metadata, 'assignment_sha256')
  const riskReportFingerprint = fingerprintMetadata(metadata, 'risk_report_fingerprint')
  const sourceSha256 = fingerprintMetadata(metadata, 'source_sha256')
  const configFingerprint = fingerprintMetadata(metadata, 'config_fingerprint')
  const operationsFingerprint = fingerprintMetadata(metadata, 'operations_fingerprint')
  if (width !== graph.width_px || height !== graph.height_px) {
    throw new Error('Risk mask dimensions do not match the region graph.')
  }
  if (graphFingerprint !== report.graph_fingerprint || graphFingerprint !== assignment.graphFingerprint) {
    throw new Error('Risk mask graph fingerprint does not match its region evidence.')
  }
  if (assignmentSha256 !== assignment.sha256) {
    throw new Error('Risk mask does not reference the current region assignment.')
  }
  const fingerprintChecks: [string, string | null | undefined, string][] = [
    [sourceSha256, expected.sourceSha256, 'source'],
    [configFingerprint, expected.configFingerprint, 'configuration'],
    [operationsFingerprint, expected.operationsFingerprint, 'operations'],
    [riskReportFingerprint, expected.riskReportFingerprint, 'risk report'],
  ]
  for (const [actual, wanted, label] of fingerprintChecks) {
    if (wanted && actual !== wanted) throw new Error(`Risk mask ${label} fingerprint is stale.`)
  }
  return {
    kind: 'risk-mask',
    downloadUrl: artifact.download_url,
    sha256: artifact.sha256,
    width,
    height,
    encoding: RISK_MASK_ENCODING,
    severityBits: severityBits(metadata),
    exactWarningCount: integerMetadata(metadata, 'exact_warning_count'),
    approximateWarningCount: integerMetadata(metadata, 'approximate_warning_count'),
    sourceSha256,
    configFingerprint,
    operationsFingerprint,
    assignmentSha256,
    graphFingerprint,
    riskReportFingerprint,
  }
}

async function sha256Hex(bytes: Uint8Array) {
  const digest = await crypto.subtle.digest('SHA-256', bytes.slice().buffer)
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('')
}

export async function fetchVerifiedRiskMask(
  artifact: RiskMaskArtifact,
  options: { fetcher?: typeof fetch; hashBytes?: (bytes: Uint8Array) => Promise<string>; signal?: AbortSignal } = {},
): Promise<RiskMaskMap> {
  const response = await (options.fetcher ?? fetch)(artifact.downloadUrl, { signal: options.signal })
  if (!response.ok) throw new Error(`Risk mask download failed with HTTP ${response.status}.`)
  const pixels = new Uint8Array(await response.arrayBuffer())
  if (pixels.byteLength !== artifact.width * artifact.height) {
    throw new Error('Downloaded risk mask byte count does not match its declared dimensions.')
  }
  const invalid = pixels.findIndex((value) => (value & ~7) !== 0)
  if (invalid >= 0) throw new Error(`Risk mask pixel ${invalid} contains unsupported severity bits.`)
  const actualSha256 = await (options.hashBytes ?? sha256Hex)(pixels)
  if (actualSha256 !== artifact.sha256.toLowerCase()) {
    throw new Error('Downloaded risk mask failed SHA-256 verification.')
  }
  return {
    width: artifact.width,
    height: artifact.height,
    encoding: artifact.encoding,
    severityBits: artifact.severityBits,
    exactWarningCount: artifact.exactWarningCount,
    approximateWarningCount: artifact.approximateWarningCount,
    sourceSha256: artifact.sourceSha256,
    configFingerprint: artifact.configFingerprint,
    operationsFingerprint: artifact.operationsFingerprint,
    assignmentSha256: artifact.assignmentSha256,
    graphFingerprint: artifact.graphFingerprint,
    riskReportFingerprint: artifact.riskReportFingerprint,
    pixels,
  }
}

export function warningRegionIndices(
  graph: RegionGraph,
  warning: RiskReport['warnings'][number],
) {
  const ids = new Set(warning.affected_region_ids)
  const labels = new Set(warning.affected_labels)
  const indices = new Set<number>()
  graph.regions.forEach((region, index) => {
    if (ids.has(region.id) || labels.has(region.label)) indices.add(index)
  })
  return indices
}

function severityForBits(bits: number): RiskSeverity | null {
  if (bits & 4) return 'error'
  if (bits & 2) return 'warning'
  if (bits & 1) return 'info'
  return null
}

export function riskOverlayRgba(
  mask: RiskMaskMap,
  assignment: RegionAssignmentMap,
  graph: RegionGraph,
  report: RiskReport,
  options: { mode: RiskMaskMode; selectedRiskId?: string | null; severity?: RiskSeverity },
) {
  if (mask.width !== assignment.width || mask.height !== assignment.height) {
    throw new Error('Risk mask and region assignment dimensions do not match.')
  }
  const selectedWarning = options.mode === 'selected'
    ? report.warnings.find((warning) => warning.id === options.selectedRiskId)
    : null
  const selectedRegions = selectedWarning ? warningRegionIndices(graph, selectedWarning) : null
  const rgba = new Uint8ClampedArray(mask.pixels.length * 4)
  const requestedBit = mask.severityBits[options.severity ?? 'warning']
  for (let pixel = 0; pixel < mask.pixels.length; pixel += 1) {
    let severity: RiskSeverity | null = null
    if (options.mode === 'selected') {
      if (selectedWarning && selectedRegions?.has(assignment.indices[pixel])) severity = selectedWarning.severity
    } else if (options.mode === 'severity') {
      if (mask.pixels[pixel] & requestedBit) severity = options.severity ?? 'warning'
    } else {
      severity = severityForBits(mask.pixels[pixel])
    }
    if (!severity) continue
    const offset = pixel * 4
    const color = COLORS[severity]
    rgba[offset] = color[0]
    rgba[offset + 1] = color[1]
    rgba[offset + 2] = color[2]
    // Alternating alpha is a non-color exact-mask texture that remains legible in grayscale.
    const x = pixel % mask.width
    const y = Math.floor(pixel / mask.width)
    rgba[offset + 3] = (x + y) % 3 === 0 ? 230 : color[3]
  }
  return rgba
}

export function exactWarningsAtCanvasPick(
  assignment: RegionAssignmentMap,
  graph: RegionGraph,
  report: RiskReport,
  pick: Pick<EditorCanvasPick, 'pixelX' | 'pixelY'>,
) {
  if (pick.pixelX < 0 || pick.pixelY < 0 || pick.pixelX >= assignment.width || pick.pixelY >= assignment.height) return []
  const regionIndex = assignment.indices[pick.pixelY * assignment.width + pick.pixelX]
  if (regionIndex < 0) return []
  const region = graph.regions[regionIndex]
  if (!region) return []
  const rank: Record<RiskSeverity, number> = { error: 3, warning: 2, info: 1 }
  return report.warnings
    .filter((warning) => warning.affected_region_ids.includes(region.id) || warning.affected_labels.includes(region.label))
    .sort((left, right) => rank[right.severity] - rank[left.severity] || left.id.localeCompare(right.id))
}
