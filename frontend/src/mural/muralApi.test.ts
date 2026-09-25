import { afterEach, describe, expect, it, vi } from 'vitest'
import type { GeometryJobResult } from '../contracts'
import type { MuralBuildRequest } from './MuralBuildCoordinator'
import { muralBuildTransport } from './muralApi'

afterEach(() => vi.restoreAllMocks())

const request: MuralBuildRequest = {
  projectId: 'project / one',
  previewJobId: 'preview-1',
  expectedDraftGeneration: 8,
  processedArtifactId: 'master-1',
  planGeneration: 4,
  requestFingerprint: 'a'.repeat(64),
  name: '3x2-wager-mural',
  materialMapping: [],
  profile: {
    printer_model: 'Bambu Lab P2S',
    nozzle_diameter_mm: 0.4,
    layer_height_mm: 0.2,
    bed_type: 'Textured PEI Plate',
  },
  assemblyAids: {
    enabled: true,
    rearIdentifiers: true,
    edgeIdentifiers: true,
    orientationMarks: true,
    cropMarks: true,
    alignmentJigMetadata: true,
  },
}

const geometry = {
  export_ready: true,
  geometry_ir: { id: 'geometry-ir-1', sha256: 'b'.repeat(64) },
} as GeometryJobResult

describe('mural build API transport', () => {
  it('posts the exact saved-plan, geometry, material, and profile guards', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ job: { id: 'mural-job-1' } }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await muralBuildTransport.startMural(request, geometry, new AbortController().signal)

    expect(fetch).toHaveBeenCalledWith('/api/projects/project%20%2F%20one/mural-builds', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        schema_version: 1,
        expected_plan_generation: 4,
        expected_request_fingerprint: 'a'.repeat(64),
        geometry_artifact_id: 'geometry-ir-1',
        geometry_sha256: 'b'.repeat(64),
        name: '3x2-wager-mural',
        material_mapping: [],
        profile: request.profile,
        assembly_aids: {
          enabled: true,
          rear_identifiers: true,
          edge_identifiers: true,
          orientation_marks: true,
          crop_marks: true,
          alignment_jig_metadata: true,
        },
      }),
      signal: expect.any(AbortSignal),
    })
  })

  it('reads the project-scoped build result and preserves stale-plan conflict details', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ job: { id: 'mural-job-1' } }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        error: {
          code: 'conflict',
          message: 'The mural plan changed.',
          details: { reason: 'stale generation', action: 'reload mural plan' },
        },
      }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }))

    await muralBuildTransport.fetchResult('project / one', 'job / one', new AbortController().signal)
    await expect(
      muralBuildTransport.startMural(request, geometry, new AbortController().signal),
    ).rejects.toMatchObject({
      status: 409,
      code: 'conflict',
      details: { reason: 'stale generation', action: 'reload mural plan' },
    })

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      '/api/projects/project%20%2F%20one/mural-builds/job%20%2F%20one',
      { method: 'GET', signal: expect.any(AbortSignal) },
    )
  })

  it('refuses to call mural packaging without verified geometry IR', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
    await expect(
      muralBuildTransport.startMural(
        request,
        { ...geometry, export_ready: false },
        new AbortController().signal,
      ),
    ).rejects.toThrow(/Verified geometry is required/)
    expect(fetch).not.toHaveBeenCalled()
  })
})
