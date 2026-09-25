import { describe, expect, it } from 'vitest'
import type { PreviewJobResult, ProjectWorkspace } from '../../contracts'
import { previewMatchesDraft } from './previewFreshness'

function draft(): NonNullable<ProjectWorkspace['draft']> {
  return {
    project_id: 'project-1',
    base_revision_id: null,
    config: {} as NonNullable<ProjectWorkspace['draft']>['config'],
    operations: [],
    config_sha256: 'a'.repeat(64),
    editor_sequence_sha256: 'b'.repeat(64),
    generation: 2,
    updated_at: '2026-07-16T00:00:00Z',
    history: {
      lineage_id: 'lineage-1', cursor_node_id: 'node-1', tip_node_id: 'node-1',
      cursor: 0, total: 0, limit: 100, can_undo: false, can_redo: false,
      undo_label: null, redo_label: null, state_sha256: 'c'.repeat(64),
    },
  }
}

function preview(): PreviewJobResult {
  return {
    job: {} as PreviewJobResult['job'],
    statistics: { config_sha256: 'a'.repeat(64) } as PreviewJobResult['statistics'],
    artifacts: [
      {
        kind: 'editor-replay',
        metadata: { sequence_fingerprint: 'b'.repeat(64) },
      } as unknown as PreviewJobResult['artifacts'][number],
    ],
    palette_metrics: null,
    region_graph: null,
    risk_report: null,
    island_analysis: null,
    clearance_analysis: null,
    hole_analysis: null,
  }
}

describe('previewMatchesDraft', () => {
  it('requires both exact config and editor sequence fingerprints', () => {
    expect(previewMatchesDraft(preview(), draft())).toBe(true)

    const changedConfig = draft()
    changedConfig.config_sha256 = 'c'.repeat(64)
    expect(previewMatchesDraft(preview(), changedConfig)).toBe(false)

    const changedOperations = draft()
    changedOperations.editor_sequence_sha256 = 'd'.repeat(64)
    expect(previewMatchesDraft(preview(), changedOperations)).toBe(false)
  })

  it('treats missing legacy evidence as stale instead of guessing', () => {
    const legacyDraft = draft()
    delete legacyDraft.editor_sequence_sha256
    expect(previewMatchesDraft(preview(), legacyDraft)).toBe(false)
    expect(previewMatchesDraft(null, draft())).toBe(false)
  })
})
