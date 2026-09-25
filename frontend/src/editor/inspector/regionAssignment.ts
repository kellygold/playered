import type { PreviewJobResult, RegionGraph } from '../../contracts'
import type { EditorCanvasPick } from '../canvas'
import type { RegionNode } from './inspectorModel'

export const REGION_ASSIGNMENT_ENCODING =
  'int32-region-index-row-major-little-endian' as const

export type RegionAssignmentArtifact = {
  kind: 'region-assignment'
  downloadUrl: string
  sha256: string
  width: number
  height: number
  encoding: typeof REGION_ASSIGNMENT_ENCODING
  inactiveIndex: -1
  regionCount: number
  graphFingerprint: string
}

export type RegionAssignmentMap = {
  width: number
  height: number
  graphFingerprint: string
  regionIds: string[]
  indices: Int32Array
}

export type RegionAssignmentLoadOptions = {
  fetcher?: typeof fetch
  hashBytes?: (bytes: Uint8Array) => Promise<string>
  signal?: AbortSignal
}

type PreviewArtifact = PreviewJobResult['artifacts'][number]

function integerMetadata(metadata: Record<string, unknown>, key: string) {
  const value = metadata[key]
  if (!Number.isInteger(value)) throw new Error(`Region assignment metadata ${key} must be an integer.`)
  return value as number
}

function stringMetadata(metadata: Record<string, unknown>, key: string) {
  const value = metadata[key]
  if (typeof value !== 'string' || !value) throw new Error(`Region assignment metadata ${key} must be a string.`)
  return value
}

export function regionAssignmentArtifact(
  artifacts: PreviewArtifact[],
  graph: RegionGraph,
  expectedGraphFingerprint?: string | null,
): RegionAssignmentArtifact | null {
  const artifact = artifacts.find((candidate) => candidate.kind === 'region-assignment')
  if (!artifact) return null
  const metadata = artifact.metadata
  const encoding = stringMetadata(metadata, 'encoding')
  if (encoding !== REGION_ASSIGNMENT_ENCODING) {
    throw new Error(`Unsupported region assignment encoding: ${encoding}.`)
  }
  const width = integerMetadata(metadata, 'width')
  const height = integerMetadata(metadata, 'height')
  const inactiveIndex = integerMetadata(metadata, 'inactive_index')
  const regionCount = integerMetadata(metadata, 'region_count')
  const graphFingerprint = stringMetadata(metadata, 'graph_fingerprint')
  if (width !== graph.width_px || height !== graph.height_px) {
    throw new Error('Region assignment dimensions do not match the region graph.')
  }
  if (inactiveIndex !== -1) throw new Error('Region assignment must reserve -1 for inactive pixels.')
  if (regionCount !== graph.regions.length) {
    throw new Error('Region assignment count does not match the region graph.')
  }
  if (expectedGraphFingerprint && graphFingerprint !== expectedGraphFingerprint) {
    throw new Error('Region assignment fingerprint does not match the risk report graph.')
  }
  return {
    kind: 'region-assignment',
    downloadUrl: artifact.download_url,
    sha256: artifact.sha256,
    width,
    height,
    encoding,
    inactiveIndex: -1,
    regionCount,
    graphFingerprint,
  }
}

async function sha256Hex(bytes: Uint8Array) {
  const digest = await crypto.subtle.digest('SHA-256', bytes.slice().buffer)
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('')
}

export function decodeRegionAssignment(
  bytes: Uint8Array,
  artifact: RegionAssignmentArtifact,
  graph: RegionGraph,
): RegionAssignmentMap {
  const expectedBytes = artifact.width * artifact.height * Int32Array.BYTES_PER_ELEMENT
  if (bytes.byteLength !== expectedBytes) {
    throw new Error(`Region assignment has ${bytes.byteLength} bytes; expected ${expectedBytes}.`)
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const indices = new Int32Array(artifact.width * artifact.height)
  const counts = new Uint32Array(artifact.regionCount)
  let activeCount = 0
  for (let offset = 0, pixel = 0; offset < bytes.byteLength; offset += 4, pixel += 1) {
    const index = view.getInt32(offset, true)
    if (index < artifact.inactiveIndex || index >= artifact.regionCount) {
      throw new Error(`Region assignment pixel ${pixel} references invalid region index ${index}.`)
    }
    indices[pixel] = index
    if (index >= 0) {
      counts[index] += 1
      activeCount += 1
    }
  }
  if (activeCount !== graph.active_pixel_count) {
    throw new Error('Region assignment active pixel count does not match the region graph.')
  }
  graph.regions.forEach((region, index) => {
    if (counts[index] !== region.pixel_count) {
      throw new Error(`Region assignment pixel count does not match ${region.id}.`)
    }
  })
  return {
    width: artifact.width,
    height: artifact.height,
    graphFingerprint: artifact.graphFingerprint,
    regionIds: graph.regions.map((region) => region.id),
    indices,
  }
}

export async function fetchVerifiedRegionAssignment(
  artifact: RegionAssignmentArtifact,
  graph: RegionGraph,
  options: RegionAssignmentLoadOptions = {},
) {
  const response = await (options.fetcher ?? fetch)(artifact.downloadUrl, {
    signal: options.signal,
  })
  if (!response.ok) throw new Error(`Region assignment download failed with HTTP ${response.status}.`)
  const bytes = new Uint8Array(await response.arrayBuffer())
  const actualSha256 = await (options.hashBytes ?? sha256Hex)(bytes)
  if (actualSha256 !== artifact.sha256.toLowerCase()) {
    throw new Error('Downloaded region assignment failed SHA-256 verification.')
  }
  return decodeRegionAssignment(bytes, artifact, graph)
}

export function exactRegionAtCanvasPick(
  assignment: RegionAssignmentMap,
  graph: RegionGraph,
  pick: Pick<EditorCanvasPick, 'pixelX' | 'pixelY'>,
): RegionNode | null {
  if (
    assignment.width !== graph.width_px ||
    assignment.height !== graph.height_px ||
    pick.pixelX < 0 ||
    pick.pixelY < 0 ||
    pick.pixelX >= assignment.width ||
    pick.pixelY >= assignment.height
  ) {
    return null
  }
  const index = assignment.indices[pick.pixelY * assignment.width + pick.pixelX]
  if (index < 0) return null
  const region = graph.regions[index]
  if (!region || assignment.regionIds[index] !== region.id) {
    throw new Error('Region assignment order does not match the current region graph.')
  }
  return region
}
