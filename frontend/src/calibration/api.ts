import { ApiRequestError, type ApiProblem } from '../api'
import type {
  AcceptCalibrationProposalRequest,
  AttestCalibrationDraftRequest,
  CalibrationCatalogCollection,
  CalibrationCatalogVersion,
  CalibrationDownload,
  CalibrationDraft,
  CalibrationDraftCollection,
  CalibrationDraftFinalizeResult,
  CalibrationEvidenceRole,
  CalibrationImportResult,
  CalibrationProposal,
  CalibrationProposalCollection,
  CalibrationPromotionResult,
  CalibrationRun,
  CalibrationRunCollection,
  CalibrationSpecimen,
  CreateCalibrationDraftRequest,
  CreateCalibrationProposalRequest,
  RejectCalibrationProposalRequest,
  UpdateCalibrationDraftRequest,
} from './contracts'

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>
  let problem: ApiProblem | null = null
  try {
    problem = (await response.json()) as ApiProblem
  } catch {
    // A proxy or local-engine failure can return text or an empty body.
  }
  throw new ApiRequestError(response.status, problem)
}

function identifier(value: string): string {
  return encodeURIComponent(value)
}

function profileQuery(profileId?: string): string {
  if (!profileId?.trim()) return ''
  return `?${new URLSearchParams({ profile_id: profileId.trim() }).toString()}`
}

function evidenceMediaType(role: CalibrationEvidenceRole, file: Blob): string {
  if (role === 'project_3mf') return 'model/3mf'
  if (role === 'sliced_gcode') return 'text/x-gcode'
  if (role === 'slicer_settings') return 'application/json'
  if (file.type === 'image/jpeg' || file.type === 'image/png' || file.type === 'image/webp') {
    return file.type
  }
  const filename = file instanceof File ? file.name.toLowerCase() : ''
  if (filename.endsWith('.png')) return 'image/png'
  if (filename.endsWith('.webp')) return 'image/webp'
  if (filename.endsWith('.jpg') || filename.endsWith('.jpeg')) return 'image/jpeg'
  return file.type || 'application/octet-stream'
}

function downloadFilename(response: Response, fallback: string): string {
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  const basic = disposition.match(/filename="?([^";]+)"?/i)?.[1]
  if (encoded) {
    try {
      return decodeURIComponent(encoded)
    } catch {
      return fallback
    }
  }
  return basic ?? fallback
}

async function readDownload(response: Response, fallback: string): Promise<CalibrationDownload> {
  if (!response.ok) await readJson<never>(response)
  return {
    blob: await response.blob(),
    filename: downloadFilename(response, fallback),
    media_type: response.headers.get('Content-Type')?.split(';', 1)[0] ?? 'application/octet-stream',
  }
}

export async function fetchCalibrationCatalogs(
  signal?: AbortSignal,
): Promise<CalibrationCatalogCollection> {
  return readJson<CalibrationCatalogCollection>(
    await fetch('/api/printability-profile-catalogs', { signal }),
  )
}

export async function fetchCalibrationCatalog(
  fingerprint: string,
  signal?: AbortSignal,
): Promise<CalibrationCatalogVersion> {
  return readJson<CalibrationCatalogVersion>(
    await fetch(`/api/printability-profile-catalogs/${identifier(fingerprint)}`, { signal }),
  )
}

export async function fetchCalibrationSpecimen(
  profileId: string,
  signal?: AbortSignal,
): Promise<CalibrationSpecimen> {
  return readJson<CalibrationSpecimen>(
    await fetch(`/api/calibration/specimens/${identifier(profileId)}`, { signal }),
  )
}

export async function downloadCalibrationSpecimen(
  profileId: string,
  kind: 'svg' | 'png' | 'bundle',
  signal?: AbortSignal,
): Promise<CalibrationDownload> {
  const suffix = kind === 'bundle' ? 'bundle' : `coupon.${kind}`
  const extension = kind === 'bundle' ? 'zip' : kind
  const response = await fetch(
    `/api/calibration/specimens/${identifier(profileId)}/${suffix}`,
    { signal },
  )
  return readDownload(response, `${profileId}-specimen.${extension}`)
}

export async function createCalibrationDraft(
  request: CreateCalibrationDraftRequest,
  signal?: AbortSignal,
): Promise<CalibrationDraft> {
  return readJson<CalibrationDraft>(
    await fetch('/api/calibration/drafts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}

export async function fetchCalibrationDrafts(
  signal?: AbortSignal,
): Promise<CalibrationDraftCollection> {
  return readJson<CalibrationDraftCollection>(
    await fetch('/api/calibration/drafts', { signal }),
  )
}

export async function fetchCalibrationDraft(
  draftId: string,
  signal?: AbortSignal,
): Promise<CalibrationDraft> {
  return readJson<CalibrationDraft>(
    await fetch(`/api/calibration/drafts/${identifier(draftId)}`, { signal }),
  )
}

export async function updateCalibrationDraft(
  draftId: string,
  request: UpdateCalibrationDraftRequest,
  signal?: AbortSignal,
): Promise<CalibrationDraft> {
  return readJson<CalibrationDraft>(
    await fetch(`/api/calibration/drafts/${identifier(draftId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}

export async function uploadCalibrationDraftMember(
  draftId: string,
  role: CalibrationEvidenceRole,
  ordinal: number,
  expectedGeneration: number,
  file: Blob,
  signal?: AbortSignal,
): Promise<CalibrationDraft> {
  const query = new URLSearchParams({ expected_generation: String(expectedGeneration) })
  return readJson<CalibrationDraft>(
    await fetch(
      `/api/calibration/drafts/${identifier(draftId)}/members/${identifier(role)}/${ordinal}?${query.toString()}`,
      {
        method: 'PUT',
        headers: { 'Content-Type': evidenceMediaType(role, file) },
        body: file,
        signal,
      },
    ),
  )
}

export async function removeCalibrationDraftMember(
  draftId: string,
  role: CalibrationEvidenceRole,
  ordinal: number,
  expectedGeneration: number,
  signal?: AbortSignal,
): Promise<CalibrationDraft> {
  const query = new URLSearchParams({ expected_generation: String(expectedGeneration) })
  return readJson<CalibrationDraft>(
    await fetch(
      `/api/calibration/drafts/${identifier(draftId)}/members/${identifier(role)}/${ordinal}?${query.toString()}`,
      { method: 'DELETE', signal },
    ),
  )
}

export async function downloadCalibrationDraftMember(
  draftId: string,
  memberId: string,
  fallbackFilename = memberId,
  signal?: AbortSignal,
): Promise<CalibrationDownload> {
  const response = await fetch(
    `/api/calibration/drafts/${identifier(draftId)}/members/${identifier(memberId)}`,
    { signal },
  )
  return readDownload(response, fallbackFilename)
}

export async function attestCalibrationDraft(
  draftId: string,
  request: AttestCalibrationDraftRequest,
  signal?: AbortSignal,
): Promise<CalibrationDraft> {
  return readJson<CalibrationDraft>(
    await fetch(`/api/calibration/drafts/${identifier(draftId)}/attest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}

export async function finalizeCalibrationDraft(
  draftId: string,
  expectedGeneration: number,
  signal?: AbortSignal,
): Promise<CalibrationDraftFinalizeResult> {
  return readJson<CalibrationDraftFinalizeResult>(
    await fetch(`/api/calibration/drafts/${identifier(draftId)}/finalize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_generation: expectedGeneration }),
      signal,
    }),
  )
}

export async function importCalibrationRun(
  bundle: Blob,
  signal?: AbortSignal,
): Promise<CalibrationImportResult> {
  return readJson<CalibrationImportResult>(
    await fetch('/api/calibration/runs/import', {
      method: 'POST',
      headers: { 'Content-Type': bundle.type || 'application/zip' },
      body: bundle,
      signal,
    }),
  )
}

export async function fetchCalibrationRuns(
  profileId?: string,
  signal?: AbortSignal,
): Promise<CalibrationRunCollection> {
  return readJson<CalibrationRunCollection>(
    await fetch(`/api/calibration/runs${profileQuery(profileId)}`, { signal }),
  )
}

export async function fetchCalibrationRun(
  runId: string,
  signal?: AbortSignal,
): Promise<CalibrationRun> {
  return readJson<CalibrationRun>(
    await fetch(`/api/calibration/runs/${identifier(runId)}`, { signal }),
  )
}

export async function downloadCalibrationRunBundle(
  runId: string,
  signal?: AbortSignal,
): Promise<CalibrationDownload> {
  const response = await fetch(`/api/calibration/runs/${identifier(runId)}/bundle`, { signal })
  return readDownload(response, `${runId}-evidence.zip`)
}

export async function downloadCalibrationRunMember(
  runId: string,
  memberId: string,
  fallbackFilename = memberId,
  signal?: AbortSignal,
): Promise<CalibrationDownload> {
  const response = await fetch(
    `/api/calibration/runs/${identifier(runId)}/members/${identifier(memberId)}`,
    { signal },
  )
  return readDownload(response, fallbackFilename)
}

export async function createCalibrationProposal(
  request: CreateCalibrationProposalRequest,
  signal?: AbortSignal,
): Promise<CalibrationProposal> {
  return readJson<CalibrationProposal>(
    await fetch('/api/calibration/proposals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}

export async function fetchCalibrationProposals(
  profileId?: string,
  signal?: AbortSignal,
): Promise<CalibrationProposalCollection> {
  return readJson<CalibrationProposalCollection>(
    await fetch(`/api/calibration/proposals${profileQuery(profileId)}`, { signal }),
  )
}

export async function fetchCalibrationProposal(
  proposalId: string,
  signal?: AbortSignal,
): Promise<CalibrationProposal> {
  return readJson<CalibrationProposal>(
    await fetch(`/api/calibration/proposals/${identifier(proposalId)}`, { signal }),
  )
}

export async function acceptCalibrationProposal(
  proposalId: string,
  request: AcceptCalibrationProposalRequest,
  signal?: AbortSignal,
): Promise<CalibrationPromotionResult> {
  return readJson<CalibrationPromotionResult>(
    await fetch(`/api/calibration/proposals/${identifier(proposalId)}/accept`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}

export async function rejectCalibrationProposal(
  proposalId: string,
  request: RejectCalibrationProposalRequest,
  signal?: AbortSignal,
): Promise<CalibrationProposal> {
  return readJson<CalibrationProposal>(
    await fetch(`/api/calibration/proposals/${identifier(proposalId)}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    }),
  )
}
