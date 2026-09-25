import { describe, expect, it } from 'vitest'
import type { RegionOperation, RevisionResource } from '../../contracts'
import {
  compareRevisions,
  evidenceLabel,
  publicationGate,
  regionalEditEvidence,
  relationshipLabel,
  revisionRelationship,
} from './revisionModel'

const revision = {
  id: 'revision_1',
  preview_evidence: { status: 'fresh', artifact_count: 4 },
} as RevisionResource

describe('revision lifecycle model', () => {
  it('blocks unnamed, saving, and failed drafts with truthful reasons', () => {
    expect(publicationGate('  ', 'saved', 'current')).toMatchObject({ allowed: false, message: expect.stringContaining('name') })
    expect(publicationGate('Proof', 'saving', 'current')).toMatchObject({ allowed: false, message: expect.stringContaining('finish') })
    expect(publicationGate('Proof', 'error', 'current')).toMatchObject({ allowed: false, message: expect.stringContaining('Retry') })
  })

  it('requires explicit confirmation for stale or missing artifacts only', () => {
    expect(publicationGate('Proof', 'saved', 'current')).toEqual({
      allowed: true,
      requiresArtifactConfirmation: false,
      message: 'The saved draft and preview evidence are current.',
    })
    expect(publicationGate('Proof', 'saved', 'stale')).toMatchObject({ allowed: true, requiresArtifactConfirmation: true })
    expect(publicationGate('Proof', 'saved', 'missing')).toMatchObject({ allowed: true, requiresArtifactConfirmation: true })
  })

  it('distinguishes current publication, branch base, and older immutable history', () => {
    expect(relationshipLabel(revisionRelationship(revision, 'revision_1', 'revision_1'))).toBe('Current · draft base')
    expect(relationshipLabel(revisionRelationship(revision, 'revision_1', 'revision_2'))).toBe('Current published')
    expect(relationshipLabel(revisionRelationship(revision, 'revision_2', 'revision_1'))).toBe('Draft branched here')
    expect(relationshipLabel(revisionRelationship(revision, 'revision_2', 'revision_3'))).toBe('Older revision')
    expect(evidenceLabel(revision)).toBe('4 verified artifacts')
  })

  it('reports deterministic configuration deltas while matching palette colors by id', () => {
    const before = {
      config: {
        canvas: { width_mm: 200, height_mm: 140 },
        palette: { colors: [
          { id: 'cream', name: 'Cream', hex: '#f0ead6', locked: false },
          { id: 'black', name: 'Black', hex: '#000000', locked: true },
        ] },
        cleanup: { min_island_mm2: 0.2 },
      },
      operations: [],
    } as unknown as RevisionResource
    const after = {
      ...before,
      config: {
        canvas: { width_mm: 210, height_mm: 140 },
        palette: { colors: [
          { id: 'black', name: 'Black', hex: '#000000', locked: true },
          { id: 'cream', name: 'Bone', hex: '#f0ead6', locked: false },
        ] },
        cleanup: { min_island_mm2: 0.35 },
      },
    } as unknown as RevisionResource

    const comparison = compareRevisions(before, after)
    expect(comparison.config.map((delta) => delta.path)).toEqual([
      'canvas.width_mm',
      'cleanup.min_island_mm2',
      'palette.colors[cream].name',
    ])
    expect(comparison.config[0]).toMatchObject({ before: '200', after: '210' })
    expect(comparison.unchanged).toBe(false)
  })

  it('distinguishes added, removed, and changed operations without mutating snapshots', () => {
    const retained = {
      operation_type: 'palette-edit', selection: { color_id: 'cream' },
      parameters: { id: 'palette-1', value: '#eee0c0' }, source: 'manual', provenance: {},
    }
    const removed = {
      operation_type: 'remove-island', selection: { region_id: 'dot-1' },
      parameters: {}, source: 'manual', provenance: {},
    }
    const added = {
      operation_type: 'fill-hole', selection: { region_id: 'hole-2' },
      parameters: {}, source: 'manual', provenance: {},
    }
    const before = { config: {}, operations: [retained, removed] } as unknown as RevisionResource
    const after = {
      config: {},
      operations: [{ ...retained, parameters: { ...retained.parameters, value: '#fff0d0' } }, added],
    } as unknown as RevisionResource

    expect(compareRevisions(before, after).operations.map(({ kind, label }) => ({ kind, label }))).toEqual([
      { kind: 'added', label: 'fill hole' },
      { kind: 'changed', label: 'palette edit' },
      { kind: 'removed', label: 'remove island' },
    ])
    expect(retained.parameters.value).toBe('#eee0c0')
  })

  it('distinguishes deterministic, seeded, and best-effort regional edit evidence', () => {
    const operation: RegionOperation = {
      operation_type: 'regional-model-edit', selection: {}, parameters: {}, source: 'model',
      provenance: {
        regional_edit: {
          execution_kind: 'provider', reproducibility: 'best_effort',
          reproducibility_reason: 'The provider does not guarantee exact seeded replay.',
          provider_id: 'gemini', model_id: 'image-model', model_version: '2026-07-01',
          output_sha256: 'a'.repeat(64),
        },
      },
    }
    expect(regionalEditEvidence(operation)).toEqual({
      classification: 'best_effort',
      label: 'Best effort',
      reason: 'The provider does not guarantee exact seeded replay.',
      executionKind: 'provider',
      providerModel: 'gemini · image-model · 2026-07-01',
      outputSha256: 'a'.repeat(64),
    })
    expect(regionalEditEvidence({ ...operation, provenance: {} })).toBeNull()
  })
})
