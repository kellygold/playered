import { describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from '../../api'
import type {
  JobConfigV1,
  JobResource,
  PreviewJobResult,
  PreviewStart,
} from '../../contracts'
import {
  PreviewJobCoordinator,
  type PreviewJobTransport,
  type PreviewRequest,
} from './PreviewJobCoordinator'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const config: JobConfigV1 = {
  schema_version: 1,
  source_asset_id: 'asset-1',
  canvas: { width_mm: 200, height_mm: 150 },
  crop: { mode: 'contain', x: 0, y: 0, width: 1, height: 1 },
  printer: {
    profile_catalog_id: 'bambu',
    profile_catalog_version: '1',
    printer_id: 'bambu-p2s',
    nozzle_id: '0.4-hardened',
    nozzle_mm: 0.4,
    layer_height_mm: 0.2,
    plate_id: 'textured',
  },
  palette: {
    colors: [
      { id: 'black', name: 'Black', hex: '#000000', locked: false, filament_id: null },
      { id: 'white', name: 'White', hex: '#FFFFFF', locked: false, filament_id: null },
    ],
  },
  cleanup: {
    min_island_mm2: 0.3,
    max_hole_mm2: 0.5,
    smoothing_radius_mm: 0,
    merge_policy: 'review',
    preserve_long_lines: true,
  },
  geometry: {
    style: 'flush_inlay',
    base_thickness_mm: 0.8,
    art_thickness_mm: 0.4,
    corner_radius_mm: 2,
  },
}

function request(generation = 1): PreviewRequest {
  return { projectId: 'project-1', config, expectedDraftGeneration: generation }
}

function job(
  id: string,
  state: JobResource['state'],
  stage: JobResource['stage'],
  progress: number,
  failure: JobResource['failure'] = null,
): JobResource {
  return {
    id,
    project_id: 'project-1',
    revision_id: null,
    type: 'preview',
    state,
    stage,
    progress,
    request_key: `request-${id}`,
    supersession_key: 'preview-project-1',
    generation: 1,
    failure,
    artifact_ids: state === 'succeeded' ? [`artifact-${id}`] : [],
    created_at: '2026-07-16T00:00:00Z',
    started_at: state === 'queued' ? null : '2026-07-16T00:00:01Z',
    finished_at: ['succeeded', 'failed', 'canceled', 'superseded'].includes(state)
      ? '2026-07-16T00:00:02Z'
      : null,
    canceled_at: state === 'canceled' ? '2026-07-16T00:00:02Z' : null,
  }
}

function started(currentJob: JobResource): PreviewStart {
  return {
    draft: {
      project_id: 'project-1',
      base_revision_id: null,
      config,
      operations: [],
      config_sha256: 'a'.repeat(64),
      generation: 2,
      updated_at: '2026-07-16T00:00:00Z',
      history: {
        lineage_id: 'lineage-1', cursor_node_id: 'node-1', tip_node_id: 'node-1',
        cursor: 0, total: 0, limit: 100, can_undo: false, can_redo: false,
        undo_label: null, redo_label: null, state_sha256: 'c'.repeat(64),
      },
    },
    job: currentJob,
  }
}

function result(currentJob: JobResource): PreviewJobResult {
  return {
    job: currentJob,
    artifacts: [{
      id: `artifact-${currentJob.id}`,
      job_id: currentJob.id,
      revision_id: null,
      kind: 'preview-image',
      sha256: currentJob.id.padEnd(64, 'a').slice(0, 64),
      derivation_key: `derivation-${currentJob.id}`,
      media_type: 'image/png',
      byte_size: 100,
      metadata: {},
      download_url: `/api/artifacts/artifact-${currentJob.id}`,
      created_at: '2026-07-16T00:00:02Z',
    }],
    statistics: null,
    palette_metrics: null,
    region_graph: null,
    risk_report: null,
    island_analysis: null,
    clearance_analysis: null,
    hole_analysis: null,
  }
}

function immediateTransport(overrides: Partial<PreviewJobTransport> = {}): PreviewJobTransport {
  const complete = job('job-1', 'succeeded', 'complete', 1)
  return {
    start: async () => started(job('job-1', 'queued', 'queued', 0)),
    fetchJob: async () => complete,
    fetchResult: async () => result(complete),
    cancel: async (jobId) => job(jobId, 'canceled', 'canceled', 0),
    ...overrides,
  }
}

const immediateWait = async () => undefined

describe('PreviewJobCoordinator', () => {
  it('publishes deterministic stage and progress updates before accepting a result', async () => {
    const jobs = [
      job('job-1', 'running', 'analyzing', 0.42),
      job('job-1', 'running', 'cleaning', 0.78),
      job('job-1', 'succeeded', 'complete', 1),
    ]
    const snapshots: Array<{ phase: string; stage: string | null; progress: number | null }> = []
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport({ fetchJob: async () => jobs.shift()! }),
      wait: immediateWait,
    })
    coordinator.subscribe(() => {
      const snapshot = coordinator.getSnapshot()
      snapshots.push({
        phase: snapshot.phase,
        stage: snapshot.job?.stage ?? null,
        progress: snapshot.job?.progress ?? null,
      })
    })

    const preview = await coordinator.start(request())

    expect(preview?.job.id).toBe('job-1')
    expect(snapshots).toEqual(expect.arrayContaining([
      { phase: 'running', stage: 'analyzing', progress: 0.42 },
      { phase: 'running', stage: 'cleaning', progress: 0.78 },
      { phase: 'succeeded', stage: 'complete', progress: 1 },
    ]))
    expect(coordinator.getSnapshot()).toMatchObject({
      phase: 'succeeded',
      resultFreshness: 'current',
      announcement: 'Preview complete and current.',
    })
  })

  it('cancels a remote active job and aborts local polling', async () => {
    const polling = deferred<JobResource>()
    const cancel = vi.fn(async (jobId: string) => job(jobId, 'canceled', 'canceled', 0.2))
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport({
        fetchJob: () => polling.promise,
        cancel,
      }),
      wait: immediateWait,
    })
    const rendering = coordinator.start(request())
    await vi.waitFor(() => expect(coordinator.getSnapshot().phase).toBe('queued'))

    await coordinator.cancel()
    polling.resolve(job('job-1', 'running', 'analyzing', 0.4))
    await rendering

    expect(cancel).toHaveBeenCalledWith('job-1')
    expect(coordinator.getSnapshot()).toMatchObject({
      phase: 'canceled',
      announcement: 'Preview canceled.',
    })
  })

  it('invalidates submission before a server job ID exists and cancels a late response', async () => {
    const pendingStart = deferred<PreviewStart>()
    const cancel = vi.fn(async (jobId: string) => job(jobId, 'canceled', 'canceled', 0))
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport({ start: () => pendingStart.promise, cancel }),
      wait: immediateWait,
    })
    const rendering = coordinator.start(request())

    await coordinator.cancel()
    pendingStart.resolve(started(job('late-job', 'queued', 'queued', 0)))
    await rendering

    expect(coordinator.getSnapshot().phase).toBe('canceled')
    expect(cancel).toHaveBeenCalledWith('late-job')
  })

  it('enforces newest-request-wins when start responses resolve out of order', async () => {
    const first = deferred<PreviewStart>()
    const second = deferred<PreviewStart>()
    const starts = [first, second]
    const canceled: string[] = []
    const transport = immediateTransport({
      start: () => starts.shift()!.promise,
      fetchJob: async (jobId) => job(jobId, 'succeeded', 'complete', 1),
      fetchResult: async (jobId) => result(job(jobId, 'succeeded', 'complete', 1)),
      cancel: async (jobId) => {
        canceled.push(jobId)
        return job(jobId, 'canceled', 'canceled', 0)
      },
    })
    const coordinator = new PreviewJobCoordinator({ transport, wait: immediateWait })
    const oldRequest = coordinator.start(request(1))
    const newRequest = coordinator.start(request(2))

    second.resolve(started(job('new-job', 'queued', 'queued', 0)))
    await newRequest
    first.resolve(started(job('old-job', 'queued', 'queued', 0)))
    await oldRequest

    expect(coordinator.getSnapshot().result?.job.id).toBe('new-job')
    expect(coordinator.getSnapshot().requestSequence).toBe(2)
    expect(canceled).toContain('old-job')
  })

  it('ignores a delayed poll response after a newer preview has completed', async () => {
    const oldPoll = deferred<JobResource>()
    const starts = [
      started(job('old-job', 'queued', 'queued', 0)),
      started(job('new-job', 'queued', 'queued', 0)),
    ]
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport({
        start: async () => starts.shift()!,
        fetchJob: (jobId) => jobId === 'old-job'
          ? oldPoll.promise
          : Promise.resolve(job(jobId, 'succeeded', 'complete', 1)),
        fetchResult: async (jobId) => result(job(jobId, 'succeeded', 'complete', 1)),
      }),
      wait: immediateWait,
    })
    const oldRequest = coordinator.start(request(1))
    await vi.waitFor(() => expect(coordinator.getSnapshot().job?.id).toBe('old-job'))
    await coordinator.start(request(2))
    oldPoll.resolve(job('old-job', 'succeeded', 'complete', 1))
    await oldRequest

    expect(coordinator.getSnapshot().result?.job.id).toBe('new-job')
  })

  it('keeps a completed result stale if the draft changes during rendering', async () => {
    const poll = deferred<JobResource>()
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport({ fetchJob: () => poll.promise }),
      wait: immediateWait,
    })
    const rendering = coordinator.start(request())
    await vi.waitFor(() => expect(coordinator.getSnapshot().phase).toBe('queued'))

    coordinator.markStale()
    poll.resolve(job('job-1', 'succeeded', 'complete', 1))
    await rendering

    expect(coordinator.getSnapshot()).toMatchObject({
      phase: 'succeeded',
      resultFreshness: 'stale',
      announcement: 'Preview complete, but the draft changed while it rendered.',
    })
  })

  it('surfaces retryable job diagnostics and retries the last exact request', async () => {
    const failed = job('job-failed', 'failed', 'failed', 0.6, {
      code: 'worker_memory_limit',
      message: 'The preview exceeded the worker memory limit.',
      retryable: true,
      details: { peak_megabytes: 512, suggestion: 'Try a smaller preview.' },
    })
    const starts = [
      started(failed),
      started(job('job-retry', 'succeeded', 'complete', 1)),
    ]
    const start = vi.fn(async (previewRequest: PreviewRequest, signal: AbortSignal) => {
      void previewRequest
      void signal
      return starts.shift()!
    })
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport({
        start,
        fetchResult: async (jobId) => result(job(jobId, 'succeeded', 'complete', 1)),
      }),
      wait: immediateWait,
    })

    await coordinator.start(request(4))
    expect(coordinator.getSnapshot().failure).toMatchObject({
      source: 'job',
      code: 'worker_memory_limit',
      retryable: true,
      details: { peak_megabytes: 512 },
    })
    await coordinator.retry()

    expect(start).toHaveBeenCalledTimes(2)
    expect(start.mock.calls[1]?.[0]).toEqual(request(4))
    expect(coordinator.getSnapshot()).toMatchObject({ phase: 'succeeded', failure: null })
  })

  it('preserves structured API diagnostics and refuses a non-retryable retry', async () => {
    const start = vi.fn(async () => {
      throw new ApiRequestError(409, {
        error: {
          code: 'stale_draft',
          message: 'Expected draft generation 4 but found 5.',
          request_id: 'request-stale-5',
          retryable: false,
          details: { expected_generation: 4, actual_generation: 5 },
        },
      })
    })
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport({ start }),
      wait: immediateWait,
    })

    await coordinator.start(request(4))

    expect(coordinator.getSnapshot().failure).toEqual(expect.objectContaining({
      source: 'request',
      code: 'stale_draft',
      requestId: 'request-stale-5',
      retryable: false,
      details: { expected_generation: 4, actual_generation: 5 },
    }))
    await expect(coordinator.retry()).resolves.toBeNull()
    expect(start).toHaveBeenCalledOnce()
  })

  it('restores and follows an active saved job', async () => {
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport(),
      wait: immediateWait,
    })

    await coordinator.restore(job('restored-job', 'running', 'quantizing', 0.3))

    expect(coordinator.getSnapshot()).toMatchObject({
      phase: 'succeeded',
      resultFreshness: 'current',
    })
  })

  it('fetches a succeeded saved job and replaces an unrelated visible result', async () => {
    const restoredJob = job('restored-complete', 'succeeded', 'complete', 1)
    const coordinator = new PreviewJobCoordinator({
      transport: immediateTransport({
        fetchResult: async () => result(restoredJob),
      }),
      wait: immediateWait,
    })
    coordinator.restore(
      job('previous', 'succeeded', 'complete', 1),
      result(job('previous', 'succeeded', 'complete', 1)),
    )

    await coordinator.restore(restoredJob)

    expect(coordinator.getSnapshot().result?.job.id).toBe('restored-complete')
    expect(coordinator.getSnapshot().resultFreshness).toBe('current')
  })
})
