import { describe, expect, it, vi } from 'vitest'
import type {
  ArtifactResource,
  ExportJobResult,
  GeometryJobResult,
  JobResource,
} from '../../contracts'
import {
  OutputJobCoordinator,
  type OutputJobTransport,
  type OutputRequest,
} from './OutputJobCoordinator'

function job(
  id: string,
  type: JobResource['type'],
  state: JobResource['state'],
  stage: JobResource['stage'],
  progress: number,
): JobResource {
  return {
    id,
    project_id: 'project-1',
    revision_id: null,
    type,
    state,
    stage,
    progress,
    request_key: `request-${id}`,
    supersession_key: `${type}-project-1`,
    generation: 1,
    failure: null,
    artifact_ids: state === 'succeeded' ? [`artifact-${id}`] : [],
    created_at: '2026-07-16T00:00:00Z',
    started_at: state === 'queued' ? null : '2026-07-16T00:00:01Z',
    finished_at: state === 'succeeded' ? '2026-07-16T00:00:02Z' : null,
    canceled_at: null,
  }
}

function artifact(id: string, kind: string, mediaType: string): ArtifactResource {
  return {
    id,
    job_id: kind === 'geometry-ir' ? 'geometry-1' : 'export-1',
    revision_id: null,
    kind,
    sha256: id.padEnd(64, 'a').slice(0, 64),
    derivation_key: `derivation-${id}`,
    media_type: mediaType,
    byte_size: 123,
    metadata: {},
    download_url: `/api/artifacts/${id}`,
    created_at: '2026-07-16T00:00:02Z',
  }
}

const request: OutputRequest = {
  projectId: 'project-1',
  previewJobId: 'preview-1',
  expectedDraftGeneration: 7,
  name: 'Mural',
  nozzleDiameterMm: 0.4,
  layerHeightMm: 0.2,
  minimumPartThicknessMm: 0.4,
}

function transport(overrides: Partial<OutputJobTransport> = {}): OutputJobTransport {
  const geometryJob = job('geometry-1', 'geometry', 'succeeded', 'complete', 1)
  const exportJob = job('export-1', 'export', 'succeeded', 'complete', 1)
  const geometryIr = artifact('geometry-ir-1', 'geometry-ir', 'application/json')
  const geometryResult: GeometryJobResult = {
    job: geometryJob,
    artifacts: [geometryIr],
    geometry_svg: null,
    geometry_ir: geometryIr,
    geometry_mesh: null,
    geometry_report: null,
    geometry_preview: null,
    export_ready: true,
  }
  const packageArtifact = artifact('package-1', '3mf-package', 'model/3mf')
  const exportResult: ExportJobResult = {
    job: exportJob,
    artifacts: [packageArtifact],
    quality_report: null,
    validation_report: null,
    validation_log: null,
    package: packageArtifact,
    download_ready: true,
  }
  return {
    startGeometry: async () => ({ job: job('geometry-1', 'geometry', 'queued', 'queued', 0) }),
    fetchGeometryResult: async () => geometryResult,
    startExport: async () => ({ job: job('export-1', 'export', 'queued', 'queued', 0) }),
    fetchExportResult: async () => exportResult,
    fetchJob: async (id) => id === 'geometry-1' ? geometryJob : exportJob,
    cancel: async (id) => job(id, id.startsWith('geometry') ? 'geometry' : 'export', 'canceled', 'canceled', 0),
    ...overrides,
  }
}

describe('OutputJobCoordinator', () => {
  it('chains verified geometry into the exact Bambu export request', async () => {
    const base = transport()
    const startExport = vi.fn(base.startExport)
    const coordinator = new OutputJobCoordinator({
      transport: { ...base, startExport },
      wait: async () => undefined,
    })
    coordinator.reset('project-1')

    const result = await coordinator.start(request)

    expect(result?.package?.download_url).toBe('/api/artifacts/package-1')
    expect(startExport).toHaveBeenCalledWith('project-1', {
      schema_version: 1,
      geometry_artifact_id: 'geometry-ir-1',
      geometry_sha256: 'geometry-ir-1'.padEnd(64, 'a'),
      name: 'Mural',
      material_mapping: [],
      profile: {
        printer_model: 'Bambu Lab P2S',
        nozzle_diameter_mm: 0.4,
        layer_height_mm: 0.2,
        bed_type: 'Textured PEI Plate',
      },
      minimum_part_thickness_mm: 0.4,
    }, expect.any(AbortSignal))
    expect(coordinator.getSnapshot()).toMatchObject({
      phase: 'succeeded',
      resultFreshness: 'current',
      activeStep: null,
    })
  })

  it('cancels an active geometry job without starting export', async () => {
    let release!: (value: JobResource) => void
    const pending = new Promise<JobResource>((resolve) => { release = resolve })
    const base = transport({ fetchJob: async () => pending })
    const cancel = vi.fn(base.cancel)
    const startExport = vi.fn(base.startExport)
    const coordinator = new OutputJobCoordinator({
      transport: { ...base, cancel, startExport },
      wait: async () => undefined,
    })
    coordinator.reset('project-1')
    const running = coordinator.start(request)
    await Promise.resolve()
    await coordinator.cancel()
    release(job('geometry-1', 'geometry', 'succeeded', 'complete', 1))
    await running

    expect(cancel).toHaveBeenCalledWith('geometry-1')
    expect(startExport).not.toHaveBeenCalled()
    expect(coordinator.getSnapshot().phase).toBe('canceled')
  })

  it('cancels a late geometry start response after cancel-before-job-id', async () => {
    let release!: (value: { job: JobResource }) => void
    const pendingStart = new Promise<{ job: JobResource }>((resolve) => { release = resolve })
    const base = transport({ startGeometry: async () => pendingStart })
    const cancel = vi.fn(base.cancel)
    const coordinator = new OutputJobCoordinator({
      transport: { ...base, cancel },
      wait: async () => undefined,
    })
    coordinator.reset('project-1')
    const running = coordinator.start(request)

    await coordinator.cancel()
    release({ job: job('geometry-late', 'geometry', 'queued', 'queued', 0) })
    await running
    await Promise.resolve()

    expect(cancel).toHaveBeenCalledWith('geometry-late')
    expect(coordinator.getSnapshot().phase).toBe('canceled')
  })

  it('invalidates current output when the draft changes', async () => {
    const coordinator = new OutputJobCoordinator({
      transport: transport(),
      wait: async () => undefined,
    })
    coordinator.reset('project-1')
    await coordinator.start(request)

    coordinator.markStale()

    expect(coordinator.getSnapshot()).toMatchObject({
      phase: 'stale',
      resultFreshness: 'stale',
      failure: null,
    })
  })
})
