import { describe, expect, it, vi } from 'vitest'
import type { ArtifactResource, GeometryJobResult, JobResource } from '../contracts'
import {
  MuralBuildCoordinator,
  type MuralBuildRequest,
  type MuralBuildResult,
  type MuralBuildStart,
  type MuralBuildTransport,
} from './MuralBuildCoordinator'
import type { MuralSeamQaReport } from './seamTypes'

function job(
  id: string,
  state: JobResource['state'],
  projectId = 'project-a',
): JobResource {
  return {
    id,
    project_id: projectId,
    revision_id: null,
    type: 'export',
    state,
    stage: state === 'succeeded' ? 'complete' : state === 'failed' ? 'failed' : 'packaging',
    progress: state === 'succeeded' ? 1 : 0.45,
    request_key: null,
    supersession_key: null,
    generation: 1,
    failure: null,
    artifact_ids: [],
    created_at: '2026-07-17T00:00:00Z',
    started_at: null,
    finished_at: null,
    canceled_at: null,
  }
}

function artifact(id: string, kind: string): ArtifactResource {
  return {
    id,
    job_id: null,
    revision_id: null,
    kind,
    sha256: id.padEnd(64, 'a').slice(0, 64),
    derivation_key: 'd'.repeat(64),
    media_type: 'application/octet-stream',
    byte_size: 1234,
    metadata: {},
    download_url: `/api/artifacts/${id}`,
    created_at: '2026-07-17T00:00:00Z',
  }
}

const request: MuralBuildRequest = {
  projectId: 'project-a',
  previewJobId: 'preview-a',
  expectedDraftGeneration: 8,
  processedArtifactId: 'artifact-master-a',
  planGeneration: 4,
  requestFingerprint: 'a'.repeat(64),
  name: '3x2-mural',
  materialMapping: [],
  profile: {
    printer_model: 'Bambu Lab P2S',
    nozzle_diameter_mm: 0.4,
    layer_height_mm: 0.2,
    bed_type: 'Textured PEI Plate',
  },
  assemblyAids: {
    enabled: false,
    rearIdentifiers: true,
    edgeIdentifiers: true,
    orientationMarks: true,
    cropMarks: true,
    alignmentJigMetadata: true,
  },
}

function geometryResult(): GeometryJobResult {
  const geometryIr = artifact('geometry-ir', 'geometry-ir')
  return {
    job: job('geometry-job', 'succeeded'),
    artifacts: [geometryIr],
    geometry_svg: null,
    geometry_ir: geometryIr,
    geometry_mesh: null,
    geometry_report: null,
    geometry_preview: null,
    export_ready: true,
  }
}

function report(fingerprint = request.requestFingerprint): MuralSeamQaReport {
  return {
    request_fingerprint: fingerprint,
    status: 'pass',
    tiles: Array.from({ length: 6 }, () => ({})),
  } as unknown as MuralSeamQaReport
}

function result(overrides: Partial<MuralBuildResult> = {}): MuralBuildResult {
  const packageArtifact = artifact('mural-package', 'bambu-mural-3mf')
  const labels = artifact('mural-labels', 'mural-label-partition')
  const topology = artifact('mural-topology', 'mural-topology-partition')
  return {
    job: job('mural-job', 'succeeded'),
    freshness: 'current',
    stale_reason: null,
    cache_hit: false,
    artifacts: [packageArtifact, labels, topology],
    package: packageArtifact,
    label_partition: labels,
    topology_partition: topology,
    seam_qa: artifact('seam-qa', 'mural-seam-qa'),
    seam_qa_report: report(),
    validation_report: artifact('validation-report', 'bambu-validation-report'),
    validation_log: artifact('validation-log', 'bambu-validation-log'),
    assembly_aids: null,
    assembly_sheet: null,
    validation: {
      status: 'validated',
      reason: null,
      attempted: true,
      executable: '/Applications/BambuStudio.app',
      version: '2.0',
    },
    download_ready: true,
    ...overrides,
  }
}

function transport(overrides: Partial<MuralBuildTransport> = {}): MuralBuildTransport {
  return {
    startGeometry: vi.fn(async () => ({ job: job('geometry-job', 'succeeded') })),
    fetchGeometryResult: vi.fn(async () => geometryResult()),
    startMural: vi.fn(async () => ({ job: job('mural-job', 'succeeded') })),
    fetchJob: vi.fn(async (id) => job(id, 'succeeded')),
    fetchResult: vi.fn(async () => result()),
    cancel: vi.fn(async (id) => job(id, 'canceled')),
    ...overrides,
  }
}

describe('MuralBuildCoordinator', () => {
  it('runs geometry preflight before queued/running multi-plate packaging', async () => {
    const queues: Record<string, JobResource[]> = {
      'geometry-job': [job('geometry-job', 'running'), job('geometry-job', 'succeeded')],
      'mural-job': [job('mural-job', 'running'), job('mural-job', 'succeeded')],
    }
    const api = transport({
      startGeometry: vi.fn(async () => ({ job: job('geometry-job', 'queued') })),
      startMural: vi.fn(async () => ({ job: job('mural-job', 'queued') })),
      fetchJob: vi.fn(async (id) => queues[id].shift() ?? job(id, 'succeeded')),
    })
    const coordinator = new MuralBuildCoordinator({ transport: api, wait: async () => undefined })
    coordinator.reset(request.projectId)
    const phases: string[] = []
    coordinator.subscribe(() => phases.push(coordinator.getSnapshot().phase))

    const completed = await coordinator.start(request)

    expect(completed?.download_ready).toBe(true)
    expect(phases).toEqual(expect.arrayContaining([
      'submitting-geometry',
      'geometry-running',
      'submitting-mural',
      'mural-running',
      'succeeded',
    ]))
    expect(api.startMural).toHaveBeenCalledWith(
      request,
      expect.objectContaining({ export_ready: true }),
      expect.any(AbortSignal),
    )
    expect(coordinator.getSnapshot().announcement).toBe('Verified 6-plate 3MF ready to download.')
  })

  it('fails closed when seam evidence belongs to another saved plan', async () => {
    const api = transport({
      fetchResult: vi.fn(async () => result({ seam_qa_report: report('f'.repeat(64)) })),
    })
    const coordinator = new MuralBuildCoordinator({ transport: api })
    coordinator.reset(request.projectId)

    expect(await coordinator.start(request)).toBeNull()
    expect(coordinator.getSnapshot()).toMatchObject({ phase: 'failed', resultCurrent: false })
    expect(coordinator.getSnapshot().failure?.title).toBe('Mural verification failed')
  })

  it('keeps exact seam QA visible but blocks download when Bambu validation is unavailable', async () => {
    const api = transport({
      fetchResult: vi.fn(async () => result({
        download_ready: false,
        validation_report: null,
        validation_log: null,
        validation: {
          status: 'unavailable',
          reason: 'Bambu Studio was not detected.',
          attempted: true,
          executable: null,
          version: null,
        },
      })),
    })
    const coordinator = new MuralBuildCoordinator({ transport: api })
    coordinator.reset(request.projectId)

    expect(await coordinator.start(request)).not.toBeNull()
    expect(coordinator.getSnapshot()).toMatchObject({
      phase: 'validation-blocked',
      resultCurrent: true,
      failure: { title: 'Bambu validation required' },
    })
  })

  it('cancels the active geometry preflight and never starts mural packaging', async () => {
    const api = transport({
      startGeometry: vi.fn(async () => ({ job: job('geometry-job', 'running') })),
      fetchJob: vi.fn(async () => job('geometry-job', 'running')),
    })
    const coordinator = new MuralBuildCoordinator({
      transport: api,
      wait: (_milliseconds, signal) => new Promise((_resolve, reject) => {
        signal.addEventListener(
          'abort',
          () => reject(new DOMException('canceled', 'AbortError')),
          { once: true },
        )
      }),
    })
    coordinator.reset(request.projectId)
    const pending = coordinator.start(request)
    await vi.waitFor(() => expect(coordinator.getSnapshot().phase).toBe('geometry-running'))

    await coordinator.cancel()

    expect(await pending).toBeNull()
    expect(api.cancel).toHaveBeenCalledWith('geometry-job')
    expect(api.startMural).not.toHaveBeenCalled()
    expect(coordinator.getSnapshot().phase).toBe('canceled')
  })

  it('supersedes a late geometry response when the project is replaced', async () => {
    let resolveStart!: (value: { job: JobResource }) => void
    const api = transport({
      startGeometry: vi.fn((): Promise<MuralBuildStart> => new Promise((resolve) => {
        resolveStart = resolve
      })),
    })
    const coordinator = new MuralBuildCoordinator({ transport: api })
    coordinator.reset(request.projectId)
    const pending = coordinator.start(request)
    coordinator.reset('project-b')
    resolveStart({ job: job('geometry-job', 'queued') })

    expect(await pending).toBeNull()
    expect(api.cancel).toHaveBeenCalledWith('geometry-job')
    expect(coordinator.getSnapshot()).toMatchObject({ projectId: 'project-b', phase: 'idle' })
  })

  it('marks a completed package stale when its plan changes', async () => {
    const coordinator = new MuralBuildCoordinator({ transport: transport() })
    coordinator.reset(request.projectId)
    await coordinator.start(request)

    coordinator.markStale('The plan changed.')

    expect(coordinator.getSnapshot()).toMatchObject({
      phase: 'stale',
      resultCurrent: false,
      announcement: 'The plan changed. Build a new multi-plate package.',
    })
  })
})
