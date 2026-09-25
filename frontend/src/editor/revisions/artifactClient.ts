export type ArtifactInspection = {
  artifact_id: string
  kind: string
  integrity: 'verified' | 'missing' | 'corrupt'
  immutable: boolean
  regenerable: boolean
  revealable: boolean
  recovery_action: string | null
}

export type ArtifactDeletion = {
  artifact_id: string
  deleted_record: boolean
  deleted_blob: boolean
  shared_blob_retained: boolean
  recovery_action: string
}

export type ArtifactReveal = {
  artifact_id: string
  supported: boolean
  revealed: boolean
  reason: string | null
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (response.ok) return response.json() as Promise<T>
  let message = `Artifact request failed (${response.status}).`
  try {
    const body = await response.json() as { error?: { message?: string; details?: { reason?: string } } }
    message = body.error?.details?.reason ?? body.error?.message ?? message
  } catch {
    // Keep the stable fallback when a proxy or interrupted server returns non-JSON.
  }
  throw new Error(message)
}

function artifactUrl(projectId: string, artifactId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}`
}

export function inspectArtifact(
  projectId: string,
  artifactId: string,
  signal?: AbortSignal,
): Promise<ArtifactInspection> {
  return requestJson(`${artifactUrl(projectId, artifactId)}/inspection`, { signal })
}

export function revealArtifact(projectId: string, artifactId: string): Promise<ArtifactReveal> {
  return requestJson(`${artifactUrl(projectId, artifactId)}/reveal`, { method: 'POST' })
}

export function deleteArtifact(projectId: string, artifactId: string): Promise<ArtifactDeletion> {
  return requestJson(artifactUrl(projectId, artifactId), { method: 'DELETE' })
}
