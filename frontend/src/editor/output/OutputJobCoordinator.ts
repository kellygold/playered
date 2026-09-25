import {
  ApiRequestError,
  cancelJob,
  fetchExportResult,
  fetchGeometryResult,
  fetchJob,
  startExport,
  startGeometry,
} from '../../api'
import type {
  ExportJobResult,
  ExportStart,
  GeometryJobResult,
  GeometryStart,
  JobResource,
  StartExportRequest,
} from '../../contracts'

export type OutputRequest = {
  projectId: string
  previewJobId: string
  expectedDraftGeneration: number
  name: string
  nozzleDiameterMm: 0.2 | 0.4
  layerHeightMm: 0.1 | 0.2
  minimumPartThicknessMm: number
}

export type OutputStep = 'geometry' | 'export'
export type OutputCoordinatorPhase =
  | 'idle'
  | 'submitting-geometry'
  | 'geometry-queued'
  | 'geometry-running'
  | 'submitting-export'
  | 'export-queued'
  | 'export-running'
  | 'canceling'
  | 'succeeded'
  | 'failed'
  | 'canceled'
  | 'superseded'
  | 'stale'

export type OutputResultFreshness = 'none' | 'current' | 'stale'

export type OutputFailureDiagnostic = {
  step: OutputStep
  source: 'request' | 'job' | 'cancel' | 'result'
  code: string
  title: string
  message: string
  suggestion: string | null
  requestId: string | null
  retryable: boolean
  details: Record<string, unknown>
}

export type OutputCoordinatorState = Readonly<{
  projectId: string | null
  phase: OutputCoordinatorPhase
  requestSequence: number
  activeStep: OutputStep | null
  geometryJob: JobResource | null
  geometryResult: GeometryJobResult | null
  exportJob: JobResource | null
  result: ExportJobResult | null
  resultFreshness: OutputResultFreshness
  failure: OutputFailureDiagnostic | null
  lastRequest: OutputRequest | null
  announcement: string
}>

export type OutputJobTransport = {
  startGeometry: (request: OutputRequest, signal: AbortSignal) => Promise<GeometryStart>
  fetchGeometryResult: (
    projectId: string,
    jobId: string,
    signal: AbortSignal,
  ) => Promise<GeometryJobResult>
  startExport: (
    projectId: string,
    request: StartExportRequest,
    signal: AbortSignal,
  ) => Promise<ExportStart>
  fetchExportResult: (
    projectId: string,
    jobId: string,
    signal: AbortSignal,
  ) => Promise<ExportJobResult>
  fetchJob: (jobId: string, signal: AbortSignal) => Promise<JobResource>
  cancel: (jobId: string, signal?: AbortSignal) => Promise<JobResource>
}

export const outputApiTransport: OutputJobTransport = {
  startGeometry: (request, signal) => startGeometry(
    request.projectId,
    request.previewJobId,
    request.expectedDraftGeneration,
    signal,
  ),
  fetchGeometryResult,
  startExport,
  fetchExportResult,
  fetchJob,
  cancel: cancelJob,
}

export type OutputJobCoordinatorOptions = {
  transport?: OutputJobTransport
  pollIntervalMs?: number
  wait?: (milliseconds: number, signal: AbortSignal) => Promise<void>
}

type ActiveRequest = {
  sequence: number
  editRevision: number
  request: OutputRequest
  controller: AbortController
  step: OutputStep
  jobId: string | null
}

type Listener = () => void

function initialState(projectId: string | null = null): OutputCoordinatorState {
  return {
    projectId,
    phase: 'idle',
    requestSequence: 0,
    activeStep: null,
    geometryJob: null,
    geometryResult: null,
    exportJob: null,
    result: null,
    resultFreshness: 'none',
    failure: null,
    lastRequest: null,
    announcement: 'No 3MF output has been generated.',
  }
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(resolve, milliseconds)
    signal.addEventListener('abort', () => {
      globalThis.clearTimeout(timer)
      reject(new DOMException('Output polling was canceled.', 'AbortError'))
    }, { once: true })
  })
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === 'AbortError'
    : error instanceof Error && error.name === 'AbortError'
}

function requestFailure(
  error: unknown,
  step: OutputStep,
  source: 'request' | 'cancel' | 'result',
): OutputFailureDiagnostic {
  const label = step === 'geometry' ? 'geometry generation' : '3MF export'
  if (error instanceof ApiRequestError) {
    const problem = error.problem?.error
    const suggestion = problem?.details.suggestion ?? problem?.details.action
    return {
      step,
      source,
      code: problem?.code ?? `http_${error.status}`,
      title: source === 'cancel' ? `Could not cancel ${label}` : `${label} failed`,
      message: problem?.message ?? error.message,
      suggestion: typeof suggestion === 'string'
        ? suggestion
        : problem?.retryable
          ? 'Try building the 3MF again.'
          : null,
      requestId: problem?.request_id ?? null,
      retryable: problem?.retryable ?? error.status >= 500,
      details: problem?.details ?? {},
    }
  }
  return {
    step,
    source,
    code: source === 'result' ? 'incomplete_output' : 'network_error',
    title: source === 'cancel' ? `Could not cancel ${label}` : `${label} failed`,
    message: error instanceof Error ? error.message : `Could not complete ${label}.`,
    suggestion: source === 'result'
      ? 'Build the 3MF again. If this persists, review the local engine logs.'
      : 'Check that the local engine is running, then try again.',
    requestId: null,
    retryable: true,
    details: {},
  }
}

function jobFailure(job: JobResource, step: OutputStep): OutputFailureDiagnostic {
  const label = step === 'geometry' ? 'Geometry generation' : '3MF export'
  return {
    step,
    source: 'job',
    code: job.failure?.code ?? `${step}_job_failed`,
    title: `${label} failed`,
    message: job.failure?.message ?? `${label} stopped before producing a complete result.`,
    suggestion: job.failure?.retryable ? 'Try generating the 3MF again.' : null,
    requestId: null,
    retryable: job.failure?.retryable ?? false,
    details: job.failure?.details ?? {},
  }
}

function stageAnnouncement(step: OutputStep, job: JobResource): string {
  const label = step === 'geometry' ? 'Geometry' : '3MF export'
  return `${label} ${job.stage.replaceAll('_', ' ')}, ${Math.round(job.progress * 100)} percent.`
}

export class OutputJobCoordinator {
  private readonly transport: OutputJobTransport
  private readonly pollIntervalMs: number
  private readonly wait: (milliseconds: number, signal: AbortSignal) => Promise<void>
  private readonly listeners = new Set<Listener>()
  private state: OutputCoordinatorState = initialState()
  private active: ActiveRequest | null = null
  private sequence = 0
  private editRevision = 0
  private disposed = false

  constructor(options: OutputJobCoordinatorOptions = {}) {
    this.transport = options.transport ?? outputApiTransport
    this.pollIntervalMs = options.pollIntervalMs ?? 180
    this.wait = options.wait ?? abortableDelay
  }

  getSnapshot = (): OutputCoordinatorState => this.state

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private publish(patch: Partial<OutputCoordinatorState>): void {
    if (this.disposed) return
    this.state = { ...this.state, ...patch }
    for (const listener of this.listeners) listener()
  }

  private isCurrent(active: ActiveRequest): boolean {
    return !this.disposed
      && active.sequence === this.sequence
      && active.request.projectId === this.state.projectId
  }

  private cancelQuietly(jobId: string): void {
    void this.transport.cancel(jobId).catch(() => undefined)
  }

  reset = (projectId: string | null): void => {
    if (this.disposed || projectId === this.state.projectId) return
    const previous = this.active
    this.sequence += 1
    previous?.controller.abort()
    if (previous?.jobId) this.cancelQuietly(previous.jobId)
    this.active = null
    this.editRevision = 0
    this.state = { ...initialState(projectId), requestSequence: this.sequence }
    for (const listener of this.listeners) listener()
  }

  markStale = (): void => {
    if (this.disposed) return
    this.editRevision += 1
    const previous = this.active
    if (previous) {
      this.sequence += 1
      previous.controller.abort()
      if (previous.jobId) this.cancelQuietly(previous.jobId)
      this.active = null
    }
    this.publish({
      phase: 'stale',
      requestSequence: this.sequence,
      activeStep: null,
      resultFreshness: this.state.result ? 'stale' : 'none',
      failure: null,
      announcement: 'The 3MF output is stale because the draft or preview changed.',
    })
  }

  start = async (request: OutputRequest): Promise<ExportJobResult | null> => {
    if (this.disposed) throw new Error('Output coordinator has been disposed.')
    if (request.projectId !== this.state.projectId) this.reset(request.projectId)

    const previous = this.active
    previous?.controller.abort()
    if (previous?.jobId) this.cancelQuietly(previous.jobId)

    const active: ActiveRequest = {
      sequence: ++this.sequence,
      editRevision: this.editRevision,
      request,
      controller: new AbortController(),
      step: 'geometry',
      jobId: null,
    }
    this.active = active
    this.publish({
      phase: 'submitting-geometry',
      requestSequence: active.sequence,
      activeStep: 'geometry',
      geometryJob: null,
      geometryResult: null,
      exportJob: null,
      result: null,
      resultFreshness: 'none',
      failure: null,
      lastRequest: request,
      announcement: 'Submitting geometry generation.',
    })

    let geometryStart: GeometryStart
    try {
      geometryStart = await this.transport.startGeometry(request, active.controller.signal)
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        activeStep: 'geometry',
        failure: requestFailure(error, 'geometry', 'request'),
        announcement: 'Geometry generation could not start.',
      })
      return null
    }
    if (!this.isCurrent(active)) {
      this.cancelQuietly(geometryStart.job.id)
      return null
    }
    active.jobId = geometryStart.job.id
    if (geometryStart.job.state === 'queued' || geometryStart.job.state === 'running') {
      this.publishJob('geometry', geometryStart.job)
    }

    const geometryJob = await this.follow(active, geometryStart.job)
    if (!geometryJob || !this.isCurrent(active)) return null
    if (geometryJob.state !== 'succeeded') return null

    let geometry: GeometryJobResult
    try {
      geometry = await this.transport.fetchGeometryResult(
        request.projectId,
        geometryJob.id,
        active.controller.signal,
      )
      if (!geometry.export_ready || !geometry.geometry_ir) {
        throw new Error('Geometry completed without a verified canonical export artifact.')
      }
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        activeStep: 'geometry',
        failure: requestFailure(error, 'geometry', 'result'),
        announcement: 'Geometry completed without a usable export result.',
      })
      return null
    }
    if (!this.isCurrent(active)) return null
    this.publish({ geometryResult: geometry })

    active.step = 'export'
    active.jobId = null
    this.publish({
      phase: 'submitting-export',
      activeStep: 'export',
      announcement: 'Packaging the verified geometry as a Bambu 3MF.',
    })
    const exportRequest: StartExportRequest = {
      schema_version: 1,
      geometry_artifact_id: geometry.geometry_ir.id,
      geometry_sha256: geometry.geometry_ir.sha256,
      name: request.name,
      material_mapping: [],
      profile: {
        printer_model: 'Bambu Lab P2S',
        nozzle_diameter_mm: request.nozzleDiameterMm,
        layer_height_mm: request.layerHeightMm,
        bed_type: 'Textured PEI Plate',
      },
      minimum_part_thickness_mm: request.minimumPartThicknessMm,
    }

    let exportStart: ExportStart
    try {
      exportStart = await this.transport.startExport(
        request.projectId,
        exportRequest,
        active.controller.signal,
      )
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        activeStep: 'export',
        failure: requestFailure(error, 'export', 'request'),
        announcement: '3MF export could not start.',
      })
      return null
    }
    if (!this.isCurrent(active)) {
      this.cancelQuietly(exportStart.job.id)
      return null
    }
    active.jobId = exportStart.job.id
    if (exportStart.job.state === 'queued' || exportStart.job.state === 'running') {
      this.publishJob('export', exportStart.job)
    }

    const exportJob = await this.follow(active, exportStart.job)
    if (!exportJob || !this.isCurrent(active)) return null
    if (exportJob.state !== 'succeeded') return null

    try {
      const result = await this.transport.fetchExportResult(
        request.projectId,
        exportJob.id,
        active.controller.signal,
      )
      if (!result.download_ready || !result.package) {
        throw new Error('Export completed without a verified downloadable 3MF package.')
      }
      if (!this.isCurrent(active)) return null
      this.active = null
      const current = active.editRevision === this.editRevision
      this.publish({
        phase: 'succeeded',
        activeStep: null,
        exportJob: result.job,
        result,
        resultFreshness: current ? 'current' : 'stale',
        failure: null,
        announcement: current
          ? 'Validated 3MF package ready to download.'
          : 'The 3MF completed, but the draft changed while it was generated.',
      })
      return result
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        activeStep: 'export',
        failure: requestFailure(error, 'export', 'result'),
        announcement: '3MF export completed without a downloadable package.',
      })
      return null
    }
  }

  private async follow(
    active: ActiveRequest,
    initialJob: JobResource,
  ): Promise<JobResource | null> {
    let job = initialJob
    try {
      while (this.isCurrent(active)) {
        if (job.state === 'succeeded') return job
        if (job.state === 'failed') {
          this.active = null
          this.publish({
            phase: 'failed',
            activeStep: active.step,
            failure: jobFailure(job, active.step),
            announcement: `${active.step === 'geometry' ? 'Geometry generation' : '3MF export'} failed.`,
          })
          return job
        }
        if (job.state === 'canceled' || job.state === 'superseded') {
          this.active = null
          this.publish({
            phase: job.state,
            activeStep: null,
            announcement: job.state === 'canceled'
              ? '3MF generation canceled.'
              : '3MF generation superseded by a newer request.',
          })
          return job
        }
        this.publishJob(active.step, job)
        await this.wait(this.pollIntervalMs, active.controller.signal)
        job = await this.transport.fetchJob(job.id, active.controller.signal)
      }
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        activeStep: active.step,
        failure: requestFailure(error, active.step, 'request'),
        announcement: 'Output job status could not be retrieved.',
      })
    }
    return null
  }

  private publishJob(step: OutputStep, job: JobResource): void {
    const phase: OutputCoordinatorPhase = job.state === 'running'
      ? `${step}-running`
      : `${step}-queued`
    this.publish({
      phase,
      activeStep: step,
      ...(step === 'geometry' ? { geometryJob: job } : { exportJob: job }),
      announcement: stageAnnouncement(step, job),
    })
  }

  cancel = async (): Promise<void> => {
    if (this.disposed || !this.active) return
    const canceled = this.active
    const sequence = ++this.sequence
    this.active = null
    canceled.controller.abort()
    this.publish({
      phase: 'canceling',
      requestSequence: sequence,
      activeStep: canceled.step,
      failure: null,
      announcement: 'Canceling 3MF generation.',
    })
    if (!canceled.jobId) {
      this.publish({ phase: 'canceled', activeStep: null, announcement: '3MF generation canceled.' })
      return
    }
    try {
      const job = await this.transport.cancel(canceled.jobId)
      if (this.disposed || sequence !== this.sequence) return
      if (job.state !== 'canceled' && job.state !== 'superseded' && job.state !== 'succeeded') {
        throw new Error(`The engine returned ${job.state} instead of confirming cancellation.`)
      }
      this.publish({
        phase: job.state === 'superseded' ? 'superseded' : 'canceled',
        activeStep: null,
        ...(canceled.step === 'geometry' ? { geometryJob: job } : { exportJob: job }),
        announcement: job.state === 'superseded'
          ? '3MF generation superseded.'
          : '3MF generation canceled.',
      })
    } catch (error) {
      if (this.disposed || sequence !== this.sequence) return
      this.publish({
        phase: 'failed',
        activeStep: canceled.step,
        failure: requestFailure(error, canceled.step, 'cancel'),
        announcement: '3MF cancellation failed.',
      })
    }
  }

  retry = async (): Promise<ExportJobResult | null> => {
    if (!this.state.lastRequest || !this.state.failure?.retryable) return null
    return this.start(this.state.lastRequest)
  }

  dispose = (): void => {
    if (this.disposed) return
    this.disposed = true
    this.sequence += 1
    this.active?.controller.abort()
    if (this.active?.jobId) this.cancelQuietly(this.active.jobId)
    this.active = null
    this.listeners.clear()
  }
}
