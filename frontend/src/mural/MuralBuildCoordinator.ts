import type {
  ArtifactResource,
  ExportMaterialMapping,
  ExportProfileSettings,
  GeometryJobResult,
  GeometryStart,
  JobResource,
} from '../contracts'
import type { MuralSeamQaReport } from './seamTypes'

export type MuralBuildRequest = Readonly<{
  projectId: string
  previewJobId: string
  expectedDraftGeneration: number
  processedArtifactId: string
  planGeneration: number
  requestFingerprint: string
  name: string
  materialMapping: ExportMaterialMapping[]
  profile: ExportProfileSettings
  assemblyAids: Readonly<{
    enabled: boolean
    rearIdentifiers: boolean
    edgeIdentifiers: boolean
    orientationMarks: boolean
    cropMarks: boolean
    alignmentJigMetadata: boolean
  }>
}>

export type MuralBuildResult = Readonly<{
  job: JobResource
  freshness: 'current' | 'stale'
  stale_reason: string | null
  cache_hit: boolean
  artifacts: ArtifactResource[]
  package: ArtifactResource | null
  label_partition: ArtifactResource | null
  topology_partition: ArtifactResource | null
  seam_qa: ArtifactResource | null
  seam_qa_report: MuralSeamQaReport | null
  validation_report: ArtifactResource | null
  validation_log: ArtifactResource | null
  assembly_aids: ArtifactResource | null
  assembly_sheet: ArtifactResource | null
  validation: {
    status:
      | 'validated'
      | 'unavailable'
      | 'failed'
      | 'timed_out'
      | 'canceled'
      | 'profile_error'
      | 'invalid_artifact'
      | 'not_run'
    reason: string | null
    attempted: boolean
    executable: string | null
    version: string | null
  }
  download_ready: boolean
}>

export type MuralBuildStart = Readonly<{ job: JobResource }>

export type MuralBuildTransport = Readonly<{
  startGeometry: (request: MuralBuildRequest, signal: AbortSignal) => Promise<GeometryStart>
  fetchGeometryResult: (
    projectId: string,
    jobId: string,
    signal: AbortSignal,
  ) => Promise<GeometryJobResult>
  startMural: (
    request: MuralBuildRequest,
    geometry: GeometryJobResult,
    signal: AbortSignal,
  ) => Promise<MuralBuildStart>
  fetchJob: (jobId: string, signal: AbortSignal) => Promise<JobResource>
  fetchResult: (
    projectId: string,
    jobId: string,
    signal: AbortSignal,
  ) => Promise<MuralBuildResult>
  cancel: (jobId: string, signal?: AbortSignal) => Promise<JobResource>
}>

export type MuralBuildStep = 'geometry' | 'mural'
export type MuralBuildPhase =
  | 'idle'
  | 'submitting-geometry'
  | 'geometry-queued'
  | 'geometry-running'
  | 'submitting-mural'
  | 'mural-queued'
  | 'mural-running'
  | 'canceling'
  | 'succeeded'
  | 'validation-blocked'
  | 'failed'
  | 'canceled'
  | 'superseded'
  | 'stale'

export type MuralBuildFailure = Readonly<{
  title: string
  message: string
  retryable: boolean
}>

export type MuralBuildState = Readonly<{
  projectId: string | null
  phase: MuralBuildPhase
  requestSequence: number
  activeStep: MuralBuildStep | null
  geometryJob: JobResource | null
  geometryResult: GeometryJobResult | null
  job: JobResource | null
  result: MuralBuildResult | null
  resultCurrent: boolean
  failure: MuralBuildFailure | null
  lastRequest: MuralBuildRequest | null
  announcement: string
}>

export type MuralBuildCoordinatorOptions = Readonly<{
  transport: MuralBuildTransport
  pollIntervalMs?: number
  wait?: (milliseconds: number, signal: AbortSignal) => Promise<void>
}>

type ActiveRequest = {
  sequence: number
  request: MuralBuildRequest
  controller: AbortController
  step: MuralBuildStep
  jobId: string | null
}

type Listener = () => void

function initialState(projectId: string | null = null): MuralBuildState {
  return {
    projectId,
    phase: 'idle',
    requestSequence: 0,
    activeStep: null,
    geometryJob: null,
    geometryResult: null,
    job: null,
    result: null,
    resultCurrent: false,
    failure: null,
    lastRequest: null,
    announcement: 'No mural package has been built.',
  }
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(resolve, milliseconds)
    signal.addEventListener('abort', () => {
      globalThis.clearTimeout(timer)
      reject(new DOMException('Mural build polling was canceled.', 'AbortError'))
    }, { once: true })
  })
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === 'AbortError'
    : error instanceof Error && error.name === 'AbortError'
}

function failure(error: unknown, title: string): MuralBuildFailure {
  return {
    title,
    message: error instanceof Error ? error.message : 'The mural package could not be completed.',
    retryable: true,
  }
}

function jobFailure(job: JobResource, step: MuralBuildStep): MuralBuildFailure {
  const label = step === 'geometry' ? 'Geometry preflight' : 'Multi-plate build'
  return {
    title: `${label} failed`,
    message: job.failure?.message ?? 'The local engine stopped before producing verified output.',
    retryable: job.failure?.retryable ?? false,
  }
}

export class MuralBuildCoordinator {
  private readonly transport: MuralBuildTransport
  private readonly pollIntervalMs: number
  private readonly wait: (milliseconds: number, signal: AbortSignal) => Promise<void>
  private readonly listeners = new Set<Listener>()
  private state: MuralBuildState = initialState()
  private active: ActiveRequest | null = null
  private sequence = 0
  private disposed = false

  constructor(options: MuralBuildCoordinatorOptions) {
    this.transport = options.transport
    this.pollIntervalMs = options.pollIntervalMs ?? 180
    this.wait = options.wait ?? abortableDelay
  }

  getSnapshot = (): MuralBuildState => this.state

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private publish(patch: Partial<MuralBuildState>): void {
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
    this.state = { ...initialState(projectId), requestSequence: this.sequence }
    for (const listener of this.listeners) listener()
  }

  markStale = (reason = 'The saved mural plan or processed master changed.'): void => {
    if (this.disposed) return
    const previous = this.active
    if (previous) {
      this.sequence += 1
      previous.controller.abort()
      if (previous.jobId) this.cancelQuietly(previous.jobId)
      this.active = null
    }
    if (!previous && !this.state.result && this.state.phase === 'idle') return
    this.publish({
      phase: 'stale',
      requestSequence: this.sequence,
      activeStep: null,
      geometryJob: null,
      geometryResult: null,
      job: null,
      resultCurrent: false,
      failure: null,
      announcement: `${reason} Build a new multi-plate package.`,
    })
  }

  start = async (request: MuralBuildRequest): Promise<MuralBuildResult | null> => {
    if (this.disposed) throw new Error('Mural build coordinator has been disposed.')
    if (request.projectId !== this.state.projectId) this.reset(request.projectId)
    const previous = this.active
    previous?.controller.abort()
    if (previous?.jobId) this.cancelQuietly(previous.jobId)
    const active: ActiveRequest = {
      sequence: ++this.sequence,
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
      job: null,
      result: null,
      resultCurrent: false,
      failure: null,
      lastRequest: request,
      announcement: 'Preparing exact geometry for the saved mural plan.',
    })

    const geometry = await this.prepareGeometry(active)
    if (!geometry || !this.isCurrent(active)) return null

    active.step = 'mural'
    active.jobId = null
    this.publish({
      phase: 'submitting-mural',
      activeStep: 'mural',
      announcement: 'Submitting exact geometry for multi-plate mural packaging.',
    })
    let started: MuralBuildStart
    try {
      started = await this.transport.startMural(request, geometry, active.controller.signal)
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        failure: failure(error, 'Could not start mural packaging'),
        announcement: 'The multi-plate mural package could not start.',
      })
      return null
    }
    if (!this.isCurrent(active)) {
      this.cancelQuietly(started.job.id)
      return null
    }
    active.jobId = started.job.id
    this.publishJob('mural', started.job)
    const job = await this.follow(active, started.job)
    if (!job || !this.isCurrent(active) || job.state !== 'succeeded') return null

    let result: MuralBuildResult
    try {
      result = await this.transport.fetchResult(
        request.projectId,
        job.id,
        active.controller.signal,
      )
      if (
        result.freshness !== 'current'
        || result.job.project_id !== request.projectId
        || result.job.id !== job.id
        || !result.package
        || !result.label_partition
        || !result.topology_partition
        || !result.seam_qa
        || !result.seam_qa_report
        || result.seam_qa_report.status === 'fail'
        || result.seam_qa_report.request_fingerprint !== request.requestFingerprint
      ) {
        throw new Error('The completed 3MF is not verified for the saved mural plan that started it.')
      }
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        failure: failure(error, 'Mural verification failed'),
        announcement: 'The mural build finished without a verified matching package.',
      })
      return null
    }
    if (!this.isCurrent(active)) return null
    this.active = null
    if (!result.download_ready || result.validation.status !== 'validated') {
      this.publish({
        phase: 'validation-blocked',
        activeStep: null,
        job,
        result,
        resultCurrent: true,
        failure: {
          title: 'Bambu validation required',
          message: result.validation.reason
            ?? 'Install or repair Bambu Studio, then retry validation before downloading.',
          retryable: true,
        },
        announcement: 'Exact mural QA is ready, but Bambu validation must pass before download.',
      })
      return result
    }
    this.publish({
      phase: 'succeeded',
      activeStep: null,
      job,
      result,
      resultCurrent: true,
      failure: null,
      announcement: `Verified ${result.seam_qa_report?.tiles.length ?? 0}-plate 3MF ready to download.`,
    })
    return result
  }

  private prepareGeometry = async (
    active: ActiveRequest,
  ): Promise<GeometryJobResult | null> => {
    let started: GeometryStart
    try {
      started = await this.transport.startGeometry(active.request, active.controller.signal)
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        failure: failure(error, 'Could not prepare mural geometry'),
        announcement: 'Exact geometry preparation could not start.',
      })
      return null
    }
    if (!this.isCurrent(active)) {
      this.cancelQuietly(started.job.id)
      return null
    }
    active.jobId = started.job.id
    this.publishJob('geometry', started.job)
    const job = await this.follow(active, started.job)
    if (!job || !this.isCurrent(active) || job.state !== 'succeeded') return null
    try {
      const geometry = await this.transport.fetchGeometryResult(
        active.request.projectId,
        job.id,
        active.controller.signal,
      )
      if (!geometry.export_ready || !geometry.geometry_ir) {
        throw new Error('Geometry completed without a verified canonical geometry artifact.')
      }
      if (!this.isCurrent(active)) return null
      this.publish({ geometryResult: geometry })
      return geometry
    } catch (error) {
      if (!this.isCurrent(active) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        failure: failure(error, 'Mural geometry verification failed'),
        announcement: 'Exact geometry finished without a usable canonical artifact.',
      })
      return null
    }
  }

  private follow = async (
    active: ActiveRequest,
    initial: JobResource,
  ): Promise<JobResource | null> => {
    let job = initial
    while (job.state === 'queued' || job.state === 'running') {
      try {
        await this.wait(this.pollIntervalMs, active.controller.signal)
        job = await this.transport.fetchJob(job.id, active.controller.signal)
      } catch (error) {
        if (!this.isCurrent(active) || isAbortError(error)) return null
        this.active = null
        this.publish({
          phase: 'failed',
          failure: failure(error, `${active.step === 'geometry' ? 'Geometry' : 'Mural build'} status was lost`),
          announcement: 'The local build status could not be refreshed.',
        })
        return null
      }
      if (!this.isCurrent(active)) return null
      this.publishJob(active.step, job)
    }
    if (job.state === 'failed') {
      this.active = null
      this.publish({
        phase: 'failed',
        failure: jobFailure(job, active.step),
        announcement: `${active.step === 'geometry' ? 'Geometry preflight' : 'The multi-plate mural build'} failed.`,
      })
    } else if (job.state === 'canceled') {
      this.active = null
      this.publish({
        phase: 'canceled',
        failure: null,
        announcement: 'The mural build was canceled.',
      })
    }
    return job
  }

  private publishJob(step: MuralBuildStep, job: JobResource): void {
    const phase: MuralBuildPhase = job.state === 'queued'
      ? step === 'geometry' ? 'geometry-queued' : 'mural-queued'
      : job.state === 'running'
        ? step === 'geometry' ? 'geometry-running' : 'mural-running'
        : this.state.phase
    this.publish({
      phase,
      activeStep: step,
      ...(step === 'geometry' ? { geometryJob: job } : { job }),
      announcement: `${step === 'geometry' ? 'Geometry preflight' : 'Mural build'} ${job.stage.replaceAll('_', ' ')}, ${Math.round(job.progress * 100)} percent.`,
    })
  }

  cancel = async (): Promise<void> => {
    const active = this.active
    if (!active) return
    this.publish({ phase: 'canceling', announcement: 'Canceling the mural build.' })
    active.controller.abort()
    if (!active.jobId) {
      if (this.isCurrent(active)) {
        this.active = null
        this.publish({ phase: 'canceled', activeStep: null, announcement: 'The mural build was canceled.' })
      }
      return
    }
    try {
      const job = await this.transport.cancel(active.jobId)
      if (!this.isCurrent(active)) return
      this.active = null
      this.publish({
        phase: job.state === 'canceled' ? 'canceled' : 'superseded',
        activeStep: null,
        announcement: job.state === 'canceled'
          ? 'The mural build was canceled.'
          : 'The mural build was superseded; its result will not be shown.',
      })
    } catch (error) {
      if (!this.isCurrent(active)) return
      this.active = null
      this.publish({
        phase: 'failed',
        activeStep: null,
        failure: failure(error, 'Could not cancel mural build'),
        announcement: 'The mural build cancellation could not be confirmed.',
      })
    }
  }

  retry = (): Promise<MuralBuildResult | null> => {
    if (!this.state.lastRequest) return Promise.resolve(null)
    return this.start(this.state.lastRequest)
  }

  dispose = (): void => {
    if (this.disposed) return
    this.disposed = true
    this.sequence += 1
    const previous = this.active
    previous?.controller.abort()
    if (previous?.jobId) this.cancelQuietly(previous.jobId)
    this.active = null
    this.listeners.clear()
  }
}
