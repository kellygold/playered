import {
  ApiRequestError,
  cancelJob,
  fetchJob,
  fetchPreviewResult,
  startPreview,
} from '../../api'
import type {
  JobConfigV1,
  JobResource,
  PreviewJobResult,
  PreviewStart,
} from '../../contracts'

export type PreviewRequest = {
  projectId: string
  config: JobConfigV1
  expectedDraftGeneration: number
}

export type PreviewJobTransport = {
  start: (request: PreviewRequest, signal: AbortSignal) => Promise<PreviewStart>
  fetchJob: (jobId: string, signal: AbortSignal) => Promise<JobResource>
  fetchResult: (jobId: string, signal: AbortSignal) => Promise<PreviewJobResult>
  cancel: (jobId: string, signal?: AbortSignal) => Promise<JobResource>
}

export const previewApiTransport: PreviewJobTransport = {
  start: (request, signal) => startPreview(
    request.projectId,
    request.config,
    request.expectedDraftGeneration,
    signal,
  ),
  fetchJob,
  fetchResult: fetchPreviewResult,
  cancel: cancelJob,
}

export type PreviewCoordinatorPhase =
  | 'idle'
  | 'submitting'
  | 'queued'
  | 'running'
  | 'canceling'
  | 'succeeded'
  | 'failed'
  | 'canceled'
  | 'superseded'

export type PreviewResultFreshness = 'none' | 'current' | 'stale'

export type PreviewFailureDiagnostic = {
  source: 'request' | 'job' | 'cancel'
  code: string
  title: string
  message: string
  suggestion: string | null
  requestId: string | null
  retryable: boolean
  details: Record<string, unknown>
}

export type PreviewCoordinatorState = Readonly<{
  phase: PreviewCoordinatorPhase
  requestSequence: number
  job: JobResource | null
  result: PreviewJobResult | null
  resultFreshness: PreviewResultFreshness
  acceptedStart: PreviewStart | null
  failure: PreviewFailureDiagnostic | null
  lastRequest: PreviewRequest | null
  announcement: string
}>

export type PreviewJobCoordinatorOptions = {
  transport?: PreviewJobTransport
  pollIntervalMs?: number
  wait?: (milliseconds: number, signal: AbortSignal) => Promise<void>
}

type ActiveRequest = {
  sequence: number
  editRevision: number
  controller: AbortController
  jobId: string | null
}

type Listener = () => void

const activeJobStates = new Set(['queued', 'running'])

function createInitialState(): PreviewCoordinatorState {
  return {
    phase: 'idle',
    requestSequence: 0,
    job: null,
    result: null,
    resultFreshness: 'none',
    acceptedStart: null,
    failure: null,
    lastRequest: null,
    announcement: 'No preview has been rendered.',
  }
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(resolve, milliseconds)
    signal.addEventListener('abort', () => {
      globalThis.clearTimeout(timer)
      reject(new DOMException('Preview polling was canceled.', 'AbortError'))
    }, { once: true })
  })
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === 'AbortError'
    : error instanceof Error && error.name === 'AbortError'
}

function requestFailure(error: unknown, source: 'request' | 'cancel'): PreviewFailureDiagnostic {
  const action = source === 'cancel' ? 'cancel the preview' : 'render the preview'
  if (error instanceof ApiRequestError) {
    const problem = error.problem?.error
    const suggestion = problem?.details.suggestion
    return {
      source,
      code: problem?.code ?? `http_${error.status}`,
      title: source === 'cancel' ? 'Preview cancellation failed' : 'Preview request failed',
      message: problem?.message ?? error.message,
      suggestion: typeof suggestion === 'string'
        ? suggestion
        : problem?.retryable
          ? `Try to ${action} again.`
          : null,
      requestId: problem?.request_id ?? null,
      retryable: problem?.retryable ?? error.status >= 500,
      details: problem?.details ?? {},
    }
  }
  return {
    source,
    code: 'network_error',
    title: source === 'cancel' ? 'Preview cancellation failed' : 'Preview request failed',
    message: error instanceof Error ? error.message : `Could not ${action}.`,
    suggestion: 'Check that the local engine is running, then try again.',
    requestId: null,
    retryable: true,
    details: {},
  }
}

function jobFailure(job: JobResource): PreviewFailureDiagnostic {
  return {
    source: 'job',
    code: job.failure?.code ?? 'preview_job_failed',
    title: 'Preview processing failed',
    message: job.failure?.message ?? 'The worker stopped before producing a preview.',
    suggestion: job.failure?.retryable ? 'Try rendering the preview again.' : null,
    requestId: null,
    retryable: job.failure?.retryable ?? false,
    details: job.failure?.details ?? {},
  }
}

function stageAnnouncement(job: JobResource): string {
  const stage = job.stage.replaceAll('_', ' ')
  return `Preview ${stage}, ${Math.round(Math.max(0, Math.min(1, job.progress)) * 100)} percent.`
}

export class PreviewJobCoordinator {
  private readonly transport: PreviewJobTransport
  private readonly pollIntervalMs: number
  private readonly wait: (milliseconds: number, signal: AbortSignal) => Promise<void>
  private readonly listeners = new Set<Listener>()
  private state: PreviewCoordinatorState = createInitialState()
  private active: ActiveRequest | null = null
  private sequence = 0
  private editRevision = 0
  private disposed = false

  constructor(options: PreviewJobCoordinatorOptions = {}) {
    this.transport = options.transport ?? previewApiTransport
    this.pollIntervalMs = options.pollIntervalMs ?? 180
    this.wait = options.wait ?? abortableDelay
  }

  getSnapshot = (): PreviewCoordinatorState => this.state

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private publish(patch: Partial<PreviewCoordinatorState>): void {
    if (this.disposed) return
    this.state = { ...this.state, ...patch }
    for (const listener of this.listeners) listener()
  }

  private isCurrent(sequence: number): boolean {
    return !this.disposed && sequence === this.sequence
  }

  private staleResultFreshness(): PreviewResultFreshness {
    return this.state.result ? 'stale' : 'none'
  }

  private cancelQuietly(jobId: string): void {
    void this.transport.cancel(jobId).catch(() => undefined)
  }

  markStale = (): void => {
    this.editRevision += 1
    this.publish({
      resultFreshness: this.staleResultFreshness(),
      announcement: this.state.result
        ? 'The visible preview is stale because the draft changed.'
        : this.state.announcement,
    })
  }

  start = async (request: PreviewRequest): Promise<PreviewJobResult | null> => {
    if (this.disposed) throw new Error('Preview coordinator has been disposed.')

    const previous = this.active
    previous?.controller.abort()
    if (previous?.jobId) this.cancelQuietly(previous.jobId)

    const sequence = ++this.sequence
    const controller = new AbortController()
    const active: ActiveRequest = {
      sequence,
      editRevision: this.editRevision,
      controller,
      jobId: null,
    }
    this.active = active
    this.publish({
      phase: 'submitting',
      requestSequence: sequence,
      job: null,
      resultFreshness: this.staleResultFreshness(),
      acceptedStart: null,
      failure: null,
      lastRequest: request,
      announcement: 'Submitting a new preview request.',
    })

    let started: PreviewStart
    try {
      started = await this.transport.start(request, controller.signal)
    } catch (error) {
      if (!this.isCurrent(sequence) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        failure: requestFailure(error, 'request'),
        announcement: 'The preview request failed.',
      })
      return null
    }

    if (!this.isCurrent(sequence)) {
      this.cancelQuietly(started.job.id)
      return null
    }

    active.jobId = started.job.id
    this.publish({
      phase: activeJobStates.has(started.job.state) ? started.job.state : 'running',
      job: started.job,
      acceptedStart: started,
      announcement: stageAnnouncement(started.job),
    })
    return this.follow(active, started.job)
  }

  private async follow(
    active: ActiveRequest,
    initialJob: JobResource,
  ): Promise<PreviewJobResult | null> {
    let job = initialJob
    try {
      while (this.isCurrent(active.sequence)) {
        if (job.state === 'succeeded') {
          const result = await this.transport.fetchResult(job.id, active.controller.signal)
          if (!this.isCurrent(active.sequence)) return null
          this.active = null
          const isCurrentDraft = active.editRevision === this.editRevision
          this.publish({
            phase: 'succeeded',
            job: result.job,
            result,
            resultFreshness: isCurrentDraft ? 'current' : 'stale',
            failure: null,
            announcement: isCurrentDraft
              ? 'Preview complete and current.'
              : 'Preview complete, but the draft changed while it rendered.',
          })
          return result
        }
        if (job.state === 'failed') {
          this.active = null
          this.publish({
            phase: 'failed',
            job,
            resultFreshness: this.staleResultFreshness(),
            failure: jobFailure(job),
            announcement: 'Preview processing failed.',
          })
          return null
        }
        if (job.state === 'canceled' || job.state === 'superseded') {
          this.active = null
          this.publish({
            phase: job.state,
            job,
            resultFreshness: this.staleResultFreshness(),
            announcement: job.state === 'canceled'
              ? 'Preview canceled.'
              : 'Preview superseded by a newer request.',
          })
          return null
        }

        this.publish({
          phase: job.state,
          job,
          announcement: stageAnnouncement(job),
        })
        await this.wait(this.pollIntervalMs, active.controller.signal)
        job = await this.transport.fetchJob(job.id, active.controller.signal)
      }
    } catch (error) {
      if (!this.isCurrent(active.sequence) || isAbortError(error)) return null
      this.active = null
      this.publish({
        phase: 'failed',
        resultFreshness: this.staleResultFreshness(),
        failure: requestFailure(error, 'request'),
        announcement: 'Preview status could not be retrieved.',
      })
    }
    return null
  }

  cancel = async (): Promise<void> => {
    if (this.disposed || !this.active) return
    const canceledActive = this.active
    const sequence = ++this.sequence
    this.active = null
    canceledActive.controller.abort()
    this.publish({
      phase: 'canceling',
      requestSequence: sequence,
      resultFreshness: this.staleResultFreshness(),
      failure: null,
      announcement: 'Canceling the preview.',
    })

    if (!canceledActive.jobId) {
      this.publish({ phase: 'canceled', announcement: 'Preview canceled before it started.' })
      return
    }

    try {
      const job = await this.transport.cancel(canceledActive.jobId)
      if (!this.isCurrent(sequence)) return
      if (job.state === 'succeeded') {
        const result = await this.transport.fetchResult(job.id, new AbortController().signal)
        if (!this.isCurrent(sequence)) return
        const isCurrentDraft = canceledActive.editRevision === this.editRevision
        this.publish({
          phase: 'succeeded',
          job: result.job,
          result,
          resultFreshness: isCurrentDraft ? 'current' : 'stale',
          announcement: 'The preview completed before cancellation took effect.',
        })
        return
      }
      if (job.state !== 'canceled' && job.state !== 'superseded') {
        throw new Error(`The engine returned ${job.state} instead of confirming cancellation.`)
      }
      this.publish({
        phase: job.state,
        job,
        announcement: job.state === 'canceled'
          ? 'Preview canceled.'
          : 'Preview superseded by a newer request.',
      })
    } catch (error) {
      if (!this.isCurrent(sequence)) return
      this.publish({
        phase: 'failed',
        failure: requestFailure(error, 'cancel'),
        announcement: 'Preview cancellation failed.',
      })
    }
  }

  retry = async (): Promise<PreviewJobResult | null> => {
    if (!this.state.lastRequest || !this.state.failure?.retryable) return null
    return this.start(this.state.lastRequest)
  }

  restore = (
    job: JobResource,
    result: PreviewJobResult | null = null,
    resultIsCurrent = true,
    retryRequest: PreviewRequest | null = null,
  ): Promise<PreviewJobResult | null> | null => {
    if (this.disposed) throw new Error('Preview coordinator has been disposed.')
    this.active?.controller.abort()
    const sequence = ++this.sequence
    if (result) {
      this.active = null
      this.publish({
        phase: 'succeeded',
        requestSequence: sequence,
        job: result.job,
        result,
        resultFreshness: resultIsCurrent ? 'current' : 'stale',
        acceptedStart: null,
        failure: null,
        lastRequest: retryRequest,
        announcement: resultIsCurrent ? 'Saved preview restored.' : 'A stale saved preview was restored.',
      })
      return null
    }
    if (!activeJobStates.has(job.state) && job.state !== 'succeeded') {
      this.active = null
      this.publish({
        phase: job.state,
        requestSequence: sequence,
        job,
        result: null,
        resultFreshness: 'none',
        acceptedStart: null,
        failure: job.state === 'failed' ? jobFailure(job) : null,
        lastRequest: retryRequest,
        announcement: job.state === 'failed' ? 'Saved preview job failed.' : `Saved preview ${job.state}.`,
      })
      return null
    }
    const active: ActiveRequest = {
      sequence,
      editRevision: this.editRevision,
      controller: new AbortController(),
      jobId: job.id,
    }
    this.active = active
    this.publish({
      phase: job.state,
      requestSequence: sequence,
      job,
      acceptedStart: null,
      result: null,
      resultFreshness: 'none',
      failure: null,
      lastRequest: retryRequest,
      announcement: stageAnnouncement(job),
    })
    return this.follow(active, job)
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
