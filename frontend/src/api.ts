import type {
  AutoPaletteFit,
  EditorHistoryCommand,
  ExportJobResult,
  ExportStart,
  GeometryJobResult,
  GeometryStart,
  Filament,
  FilamentCatalog,
  JobConfigV1,
  JobResource,
  PreviewJobResult,
  PreviewStart,
  PromptedEditPreparation,
  PromptedEditProvider,
  PromptedEditSession,
  PrintSetupRequest,
  PrintabilityProfileCatalog,
  ProfileCatalog,
  ProjectSummary,
  ProjectSummaryCollection,
  ProjectWorkspace,
  RevisionList,
  RevisionBranch,
  RevisionPublication,
  RevisionResource,
  RegionOperation,
  ResolvePrintabilityRequest,
  ResolvedPrintabilitySettings,
  StartExportRequest,
  ValidatedPrintSetup,
} from './contracts'
import type { CanvasSelectionState } from './editor/selection'
import type { StartupReconciliation } from './recovery'

export type { JobResource, JobStage, JobState, JobType } from './contracts'

export type ToolCapability = {
  id: string
  name: string
  detected: boolean
  available: boolean
  compatible: boolean
  path: string | null
  purpose: string
  version: string | null
  unavailable_reason: string | null
}

export type Health = {
  status: string
  version: string
  workspace: string
  capabilities: ToolCapability[]
}

export type Capabilities = {
  version: string
  tools: ToolCapability[]
  features: Record<string, boolean>
}

export type ProjectBundleImport = {
  project_id: string
  source_project_id: string
  bundle_sha256: string
  duplicate: boolean
  imported_assets: number
  imported_revisions: number
  imported_artifacts: number
  history_mode: 'full_history' | 'current_state_only'
  history_import: 'full_history' | 'legacy_anchor' | 'none'
  imported_history_nodes: number
  abandoned_history_nodes: number
}

export type ProjectBundleDownload = {
  blob: Blob
  filename: string
  sha256: string | null
  historyMode: 'full_history' | 'current_state_only'
}

export type ApiErrorCode =
  | 'validation_error'
  | 'not_found'
  | 'method_not_allowed'
  | 'conflict'
  | 'stale_draft'
  | 'incompatible_editor_history'
  | 'invalid_job_state'
  | 'capability_unavailable'
  | 'blob_unavailable'
  | 'unsupported_media'
  | 'resource_limit'
  | 'internal_error'

export type ApiProblem = {
  error: {
    code: ApiErrorCode
    message: string
    request_id: string
    retryable: boolean
    details: Record<string, unknown>
  }
}

export type WorkspaceHealthIssue = {
  code: string
  severity: 'info' | 'warning' | 'error'
  message: string
  advice: string
  relative_path: string | null
  record_type: string | null
  record_id: string | null
}

export type WorkspaceHealth = {
  generated_at: string
  status: 'healthy' | 'degraded' | 'critical'
  database_integrity: boolean
  foreign_key_integrity: boolean
  migration: {
    database_version: number
    application_version: number
    current: boolean
    applied: number[]
  }
  buckets: { name: string; file_count: number; byte_size: number }[]
  disk_total_bytes: number
  disk_free_bytes: number
  referenced_file_count: number
  orphan_file_count: number
  cache_file_count: number
  stale_temp_file_count: number
  issues: WorkspaceHealthIssue[]
}

export type GarbageCollectionCandidate = {
  relative_path: string
  namespace: string
  reason: string
  byte_size: number
  sha256: string
  mtime_ns: number
}

export type GarbageCollectionPlan = {
  generated_at: string
  minimum_age_seconds: number
  plan_sha256: string
  candidate_count: number
  reclaimable_bytes: number
  candidates: GarbageCollectionCandidate[]
}

export type GarbageCollectionExecution = {
  applied: boolean
  plan_sha256: string
  eligible_count: number
  deleted_count: number
  reclaimed_bytes: number
  skipped: WorkspaceHealthIssue[]
}

export class ApiRequestError extends Error {
  readonly status: number
  readonly problem: ApiProblem | null

  constructor(status: number, problem: ApiProblem | null) {
    super(problem?.error.message ?? `Request failed (${status})`)
    this.name = 'ApiRequestError'
    this.status = status
    this.problem = problem
  }
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>
  let problem: ApiProblem | null = null
  try {
    problem = (await response.json()) as ApiProblem
  } catch {
    // A proxy/network layer may return a non-JSON failure before FastAPI handles the request.
  }
  throw new ApiRequestError(response.status, problem)
}

export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const response = await fetch('/api/health', { signal })
  return readJson<Health>(response)
}

export async function fetchCapabilities(signal?: AbortSignal): Promise<Capabilities> {
  return readJson<Capabilities>(await fetch('/api/capabilities', { signal }))
}

export async function fetchProfileCatalog(signal?: AbortSignal): Promise<ProfileCatalog> {
  return readJson<ProfileCatalog>(await fetch('/api/profiles', { signal }))
}

export async function validatePrintSetup(
  setup: PrintSetupRequest,
  signal?: AbortSignal,
): Promise<ValidatedPrintSetup> {
  return readJson<ValidatedPrintSetup>(
    await fetch('/api/profiles/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(setup),
      signal,
    }),
  )
}

export async function fetchPrintabilityProfiles(
  signal?: AbortSignal,
): Promise<PrintabilityProfileCatalog> {
  return readJson<PrintabilityProfileCatalog>(
    await fetch('/api/printability-profiles', { signal }),
  )
}

export async function resolvePrintabilityProfile(
  request: ResolvePrintabilityRequest,
  signal?: AbortSignal,
): Promise<ResolvedPrintabilitySettings> {
  return readJson<ResolvedPrintabilitySettings>(
    await fetch('/api/printability-profiles/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...request, overrides: request.overrides ?? {} }),
      signal,
    }),
  )
}

export async function importProject(
  image: Blob,
  filename: string,
  projectName?: string,
  signal?: AbortSignal,
): Promise<ProjectWorkspace> {
  const query = new URLSearchParams()
  if (projectName?.trim()) query.set('project_name', projectName.trim())
  const suffix = query.size ? `?${query.toString()}` : ''
  return readJson<ProjectWorkspace>(
    await fetch(`/api/projects/import${suffix}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': filename },
      body: image,
      signal,
    }),
  )
}

export async function fetchProjectBundle(
  projectId: string,
  historyMode: 'full_history' | 'current_state_only' = 'full_history',
  signal?: AbortSignal,
): Promise<ProjectBundleDownload> {
  const query = new URLSearchParams({ history_mode: historyMode })
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/bundle?${query.toString()}`,
    { signal },
  )
  if (!response.ok) {
    await readJson<never>(response)
  }
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  const basic = disposition.match(/filename="?([^";]+)"?/i)?.[1]
  const filename = encoded
    ? decodeURIComponent(encoded)
    : basic ?? `project-${projectId}.image23mf`
  return {
    blob: await response.blob(),
    filename,
    sha256: response.headers.get('X-Image23MF-Bundle-SHA256'),
    historyMode: (
      response.headers.get('X-Image23MF-History-Mode') === 'current_state_only'
        ? 'current_state_only'
        : 'full_history'
    ),
  }
}

export async function importProjectBundle(
  bundle: Blob,
  signal?: AbortSignal,
): Promise<ProjectBundleImport> {
  return readJson<ProjectBundleImport>(
    await fetch('/api/project-bundles/import', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.image23mf.project+zip',
      },
      body: bundle,
      signal,
    }),
  )
}

export function importProjectWithProgress(
  image: Blob,
  filename: string,
  projectName: string | undefined,
  onProgress: (progress: number) => void,
  signal?: AbortSignal,
): Promise<ProjectWorkspace> {
  const query = new URLSearchParams()
  if (projectName?.trim()) query.set('project_name', projectName.trim())
  const suffix = query.size ? `?${query.toString()}` : ''
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('The image import was canceled.', 'AbortError'))
      return
    }
    const request = new XMLHttpRequest()
    const abort = () => request.abort()
    const cleanup = () => signal?.removeEventListener('abort', abort)
    request.open('POST', `/api/projects/import${suffix}`)
    request.responseType = 'json'
    request.setRequestHeader('Content-Type', 'application/octet-stream')
    request.setRequestHeader('X-Filename', filename)
    request.upload.onprogress = (event) => {
      const total = event.lengthComputable && event.total > 0 ? event.total : image.size
      if (total > 0) onProgress(Math.min(0.99, event.loaded / total))
    }
    request.onload = () => {
      cleanup()
      const response = request.response as ProjectWorkspace | ApiProblem | null
      if (request.status >= 200 && request.status < 300) {
        onProgress(1)
        resolve(response as ProjectWorkspace)
        return
      }
      reject(new ApiRequestError(request.status, response as ApiProblem | null))
    }
    request.onerror = () => {
      cleanup()
      reject(new TypeError('The local engine could not be reached.'))
    }
    request.onabort = () => {
      cleanup()
      reject(new DOMException('The image import was canceled.', 'AbortError'))
    }
    signal?.addEventListener('abort', abort, { once: true })
    onProgress(0)
    request.send(image)
  })
}

export async function fetchProject(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectWorkspace> {
  return readJson<ProjectWorkspace>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}`, { signal }),
  )
}

export async function fetchProjects(
  includeArchived = false,
  signal?: AbortSignal,
): Promise<ProjectSummaryCollection> {
  const suffix = includeArchived ? '?include_archived=true' : ''
  return readJson<ProjectSummaryCollection>(await fetch(`/api/projects${suffix}`, { signal }))
}

export async function setProjectArchived(
  projectId: string,
  archived: boolean,
  signal?: AbortSignal,
): Promise<ProjectSummary> {
  return readJson<ProjectSummary>(
    await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/${archived ? 'archive' : 'restore'}`,
      { method: 'POST', signal },
    ),
  )
}

export async function saveDraft(
  projectId: string,
  config: JobConfigV1,
  operations: RegionOperation[],
  expectedDraftGeneration: number,
  historyCommand?: EditorHistoryCommand,
  signal?: AbortSignal,
): Promise<NonNullable<ProjectWorkspace['draft']>> {
  return readJson<NonNullable<ProjectWorkspace['draft']>>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/draft`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        config,
        operations,
        expected_draft_generation: expectedDraftGeneration,
        ...(historyCommand ? { history_command: historyCommand } : {}),
      }),
      signal,
    }),
  )
}

export async function moveDraftHistory(
  projectId: string,
  direction: 'undo' | 'redo',
  request: {
    request_id: string
    expected_draft_generation: number
    expected_cursor_node_id: string
  },
  signal?: AbortSignal,
): Promise<NonNullable<ProjectWorkspace['draft']>> {
  return readJson<NonNullable<ProjectWorkspace['draft']>>(
    await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/draft/history/${direction}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
        signal,
      },
    ),
  )
}

export async function fetchRevisions(
  projectId: string,
  options: { limit?: number; cursor?: string | null } = {},
  signal?: AbortSignal,
): Promise<RevisionList> {
  const query = new URLSearchParams()
  if (options.limit !== undefined) query.set('limit', String(options.limit))
  if (options.cursor) query.set('cursor', options.cursor)
  const suffix = query.size ? `?${query.toString()}` : ''
  return readJson<RevisionList>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/revisions${suffix}`, { signal }),
  )
}

export async function fetchRevision(
  projectId: string,
  revisionId: string,
  signal?: AbortSignal,
): Promise<RevisionResource> {
  return readJson<RevisionResource>(
    await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/revisions/${encodeURIComponent(revisionId)}`,
      { signal },
    ),
  )
}

export async function publishRevision(
  projectId: string,
  request: {
    label: string
    notes?: string
    expected_draft_generation: number
    preview_job_id?: string | null
  },
  signal?: AbortSignal,
): Promise<RevisionPublication> {
  return readJson<RevisionPublication>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/revisions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}

export async function branchRevision(
  projectId: string,
  revisionId: string,
  expectedDraftGeneration: number,
  signal?: AbortSignal,
): Promise<RevisionBranch> {
  return readJson<RevisionBranch>(
    await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/revisions/${encodeURIComponent(revisionId)}/branch`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_draft_generation: expectedDraftGeneration }),
        signal,
      },
    ),
  )
}

export async function autoFitPalette(
  projectId: string,
  config: JobConfigV1,
  colorCount: number,
  signal?: AbortSignal,
): Promise<AutoPaletteFit> {
  return readJson<AutoPaletteFit>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/palette/auto`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config, color_count: colorCount }),
      signal,
    }),
  )
}

export async function fetchOwnedFilaments(signal?: AbortSignal): Promise<Filament[]> {
  const result = await readJson<{ items: Filament[]; total: number }>(
    await fetch('/api/filaments?owned=true', { signal }),
  )
  return result.items
}

export async function fetchStarterFilamentCatalog(
  signal?: AbortSignal,
): Promise<FilamentCatalog> {
  const result = await readJson<{ catalog: FilamentCatalog; fingerprint: string }>(
    await fetch('/api/filament-catalogs/bambu-lab-starter', { signal }),
  )
  return result.catalog
}

export async function importStarterFilaments(
  entryIds: string[],
  signal?: AbortSignal,
): Promise<Filament[]> {
  const result = await readJson<{ filaments: Filament[] }>(
    await fetch('/api/filament-catalogs/bambu-lab-starter/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entry_ids: entryIds, owned: true }),
      signal,
    }),
  )
  return result.filaments
}

export async function startPreview(
  projectId: string,
  config: JobConfigV1,
  expectedDraftGeneration: number,
  signal?: AbortSignal,
): Promise<PreviewStart> {
  return readJson<PreviewStart>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/previews`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        config,
        expected_draft_generation: expectedDraftGeneration,
      }),
      signal,
    }),
  )
}

export async function fetchPreviewResult(
  jobId: string,
  signal?: AbortSignal,
): Promise<PreviewJobResult> {
  return readJson<PreviewJobResult>(
    await fetch(`/api/jobs/${encodeURIComponent(jobId)}/result`, { signal }),
  )
}

export async function startGeometry(
  projectId: string,
  previewJobId: string,
  expectedDraftGeneration: number,
  signal?: AbortSignal,
): Promise<GeometryStart> {
  return readJson<GeometryStart>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/geometry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        schema_version: 1,
        preview_job_id: previewJobId,
        expected_draft_generation: expectedDraftGeneration,
      }),
      signal,
    }),
  )
}

export async function fetchGeometryResult(
  projectId: string,
  jobId: string,
  signal?: AbortSignal,
): Promise<GeometryJobResult> {
  return readJson<GeometryJobResult>(
    await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/geometry/${encodeURIComponent(jobId)}`,
      { signal },
    ),
  )
}

export async function startExport(
  projectId: string,
  request: StartExportRequest,
  signal?: AbortSignal,
): Promise<ExportStart> {
  return readJson<ExportStart>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/exports`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}

export async function fetchExportResult(
  projectId: string,
  jobId: string,
  signal?: AbortSignal,
): Promise<ExportJobResult> {
  return readJson<ExportJobResult>(
    await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/exports/${encodeURIComponent(jobId)}`,
      { signal },
    ),
  )
}

export function jobEventsUrl(jobId: string): string {
  return `/api/jobs/${encodeURIComponent(jobId)}/events`
}

export async function fetchJob(jobId: string, signal?: AbortSignal): Promise<JobResource> {
  return readJson<JobResource>(await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { signal }))
}

export async function cancelJob(jobId: string, signal?: AbortSignal): Promise<JobResource> {
  return readJson<JobResource>(
    await fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', signal }),
  )
}

export async function fetchWorkspaceHealth(signal?: AbortSignal): Promise<WorkspaceHealth> {
  return readJson<WorkspaceHealth>(await fetch('/api/workspace/health', { signal }))
}

export async function fetchStartupRecovery(
  signal?: AbortSignal,
): Promise<StartupReconciliation> {
  return readJson<StartupReconciliation>(
    await fetch('/api/workspace/recovery/latest', { signal }),
  )
}

export async function reconcileWorkspace(
  signal?: AbortSignal,
): Promise<StartupReconciliation> {
  return readJson<StartupReconciliation>(
    await fetch('/api/workspace/recovery/reconcile', { method: 'POST', signal }),
  )
}

export async function planGarbageCollection(
  minimumAgeSeconds: number,
  signal?: AbortSignal,
): Promise<GarbageCollectionPlan> {
  const query = new URLSearchParams({ minimum_age_seconds: String(minimumAgeSeconds) })
  return readJson<GarbageCollectionPlan>(
    await fetch(`/api/workspace/gc/plan?${query.toString()}`, { method: 'POST', signal }),
  )
}

export async function applyGarbageCollection(
  plan: Pick<GarbageCollectionPlan, 'plan_sha256' | 'minimum_age_seconds'>,
  signal?: AbortSignal,
): Promise<GarbageCollectionExecution> {
  return readJson<GarbageCollectionExecution>(
    await fetch('/api/workspace/gc/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plan_sha256: plan.plan_sha256,
        minimum_age_seconds: plan.minimum_age_seconds,
      }),
      signal,
    }),
  )
}

export async function fetchPromptedEditProviders(
  signal?: AbortSignal,
): Promise<PromptedEditProvider[]> {
  const result = await readJson<{ items: PromptedEditProvider[] }>(
    await fetch('/api/prompted-edit/providers', { signal }),
  )
  return result.items
}

export async function preparePromptedEdit(
  projectId: string,
  request: {
    parent_revision_id: string
    preview_job_id: string
    provider_id: string
    selection: CanvasSelectionState
    prompt: string
    reference_asset_ids?: string[]
    options?: Record<string, unknown>
    seed?: number | null
    alternative_count: number
  },
  signal?: AbortSignal,
): Promise<PromptedEditPreparation> {
  return readJson<PromptedEditPreparation>(
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/prompted-edits/prepare`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}

export async function executePromptedEdit(
  sessionId: string,
  preparation: PromptedEditPreparation,
  signal?: AbortSignal,
): Promise<{ session: PromptedEditSession; job: JobResource }> {
  return readJson<{ session: PromptedEditSession; job: JobResource }>(
    await fetch(`/api/prompted-edits/${encodeURIComponent(sessionId)}/execute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_sha256: preparation.request_sha256,
        disclosure_sha256: preparation.disclosure.disclosure_sha256,
        approved: true,
      }),
      signal,
    }),
  )
}

export async function fetchPromptedEditSession(
  sessionId: string,
  signal?: AbortSignal,
): Promise<PromptedEditSession> {
  return readJson<PromptedEditSession>(
    await fetch(`/api/prompted-edits/${encodeURIComponent(sessionId)}`, { signal }),
  )
}

export async function cancelPromptedEdit(
  sessionId: string,
  signal?: AbortSignal,
): Promise<PromptedEditSession> {
  return readJson<PromptedEditSession>(
    await fetch(`/api/prompted-edits/${encodeURIComponent(sessionId)}/cancel`, {
      method: 'POST',
      signal,
    }),
  )
}

export async function retryPromptedEdit(
  sessionId: string,
  signal?: AbortSignal,
): Promise<PromptedEditPreparation> {
  return readJson<PromptedEditPreparation>(
    await fetch(`/api/prompted-edits/${encodeURIComponent(sessionId)}/retry`, {
      method: 'POST',
      signal,
    }),
  )
}

export async function rejectPromptedEdit(
  sessionId: string,
  reason = 'Rejected during review.',
  signal?: AbortSignal,
): Promise<PromptedEditSession> {
  return readJson<PromptedEditSession>(
    await fetch(`/api/prompted-edits/${encodeURIComponent(sessionId)}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
      signal,
    }),
  )
}

export async function acceptPromptedEditAlternative(
  projectId: string,
  sessionId: string,
  alternativeId: string,
  expectedDraftGeneration: number,
  label: string,
  signal?: AbortSignal,
): Promise<{ session: PromptedEditSession; revision_id: string; draft_generation: number }> {
  return readJson(
    await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/prompted-edits/`
      + `${encodeURIComponent(sessionId)}/alternatives/${encodeURIComponent(alternativeId)}/accept`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_draft_generation: expectedDraftGeneration, label }),
        signal,
      },
    ),
  )
}
