import type {
  MuralPlanPreview,
  MuralPlanResource,
  MuralPlanSettings,
  SaveMuralPlanRequest,
} from './types'
import {
  cancelJob,
  fetchGeometryResult,
  fetchJob,
  startGeometry,
} from '../api'
import type {
  MuralBuildResult,
  MuralBuildStart,
  MuralBuildTransport,
} from './MuralBuildCoordinator'

type ApiProblem = {
  error?: {
    code?: string
    message?: string
    details?: Record<string, unknown>
  }
}

export class MuralApiError extends Error {
  readonly status: number
  readonly code: string | null
  readonly details: Record<string, unknown>

  constructor(status: number, problem: ApiProblem | null) {
    super(problem?.error?.message ?? `Mural request failed (${status})`)
    this.name = 'MuralApiError'
    this.status = status
    this.code = problem?.error?.code ?? null
    this.details = problem?.error?.details ?? {}
  }
}

async function requestJson<T>(
  url: string,
  init: RequestInit,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(url, { ...init, signal })
  if (response.ok) return response.json() as Promise<T>
  let problem: ApiProblem | null = null
  try {
    problem = await response.json() as ApiProblem
  } catch {
    // Keep the stable status fallback when a proxy returns a non-JSON failure.
  }
  throw new MuralApiError(response.status, problem)
}

function endpoint(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/mural-plan`
}

function buildEndpoint(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/mural-builds`
}

export async function fetchMuralPlan(
  projectId: string,
  signal?: AbortSignal,
): Promise<MuralPlanResource | null> {
  try {
    return await requestJson<MuralPlanResource>(endpoint(projectId), { method: 'GET' }, signal)
  } catch (error) {
    if (error instanceof MuralApiError && error.status === 404) return null
    throw error
  }
}

export function previewMuralPlan(
  projectId: string,
  settings: MuralPlanSettings,
  signal?: AbortSignal,
): Promise<MuralPlanPreview> {
  return requestJson<MuralPlanPreview>(`${endpoint(projectId)}/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  }, signal)
}

export function saveMuralPlan(
  projectId: string,
  request: SaveMuralPlanRequest,
  signal?: AbortSignal,
): Promise<MuralPlanResource> {
  return requestJson<MuralPlanResource>(endpoint(projectId), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  }, signal)
}

export async function deleteMuralPlan(
  projectId: string,
  generation: number,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(
    `${endpoint(projectId)}?expected_generation=${encodeURIComponent(generation)}`,
    { method: 'DELETE', signal },
  )
  if (response.ok) return
  let problem: ApiProblem | null = null
  try {
    problem = await response.json() as ApiProblem
  } catch {
    // Keep the stable status fallback when a proxy returns a non-JSON failure.
  }
  throw new MuralApiError(response.status, problem)
}

export const muralBuildTransport: MuralBuildTransport = {
  startGeometry: (request, signal) => startGeometry(
    request.projectId,
    request.previewJobId,
    request.expectedDraftGeneration,
    signal,
  ),
  fetchGeometryResult,
  startMural: (request, geometry, signal): Promise<MuralBuildStart> => {
    if (!geometry.geometry_ir || !geometry.export_ready) {
      return Promise.reject(new Error('Verified geometry is required before mural packaging.'))
    }
    return requestJson<MuralBuildStart>(buildEndpoint(request.projectId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        schema_version: 1,
        expected_plan_generation: request.planGeneration,
        expected_request_fingerprint: request.requestFingerprint,
        geometry_artifact_id: geometry.geometry_ir.id,
        geometry_sha256: geometry.geometry_ir.sha256,
        name: request.name,
        material_mapping: request.materialMapping,
        profile: request.profile,
        assembly_aids: {
          enabled: request.assemblyAids.enabled,
          rear_identifiers: request.assemblyAids.rearIdentifiers,
          edge_identifiers: request.assemblyAids.edgeIdentifiers,
          orientation_marks: request.assemblyAids.orientationMarks,
          crop_marks: request.assemblyAids.cropMarks,
          alignment_jig_metadata: request.assemblyAids.alignmentJigMetadata,
        },
      }),
    }, signal)
  },
  fetchJob,
  fetchResult: async (projectId, jobId, signal): Promise<MuralBuildResult> => {
    const result = await requestJson<Omit<MuralBuildResult, 'seam_qa_report'>>(
      `${buildEndpoint(projectId)}/${encodeURIComponent(jobId)}`,
      { method: 'GET' },
      signal,
    )
    const seamQaReport = result.seam_qa
      ? await requestJson<MuralBuildResult['seam_qa_report']>(
          result.seam_qa.download_url,
          { method: 'GET' },
          signal,
        )
      : null
    return { ...result, seam_qa_report: seamQaReport }
  },
  cancel: cancelJob,
}
