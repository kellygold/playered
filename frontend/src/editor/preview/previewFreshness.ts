import type { PreviewJobResult, ProjectWorkspace } from '../../contracts'

type Draft = NonNullable<ProjectWorkspace['draft']>

export function previewMatchesDraft(preview: PreviewJobResult | null, draft: Draft | null): boolean {
  if (!preview?.statistics || !draft?.editor_sequence_sha256) return false
  if (preview.statistics.config_sha256 !== draft.config_sha256) return false
  const replay = preview.artifacts.find((artifact) => artifact.kind === 'editor-replay')
  return replay?.metadata.sequence_fingerprint === draft.editor_sequence_sha256
}
