import { describe, expect, it } from 'vitest'
import type { MuralPlanResource } from './types'
import { muralBuildReadiness } from './muralBuildReadiness'

const plan = {
  generation: 2,
  freshness: 'current',
  stale_reason: null,
  request: {
    source: { processed_artifact_id: 'master-a' },
    reserved_rectangles: [{ x_mm: 216, y_mm: 5, width_mm: 35, height_mm: 35 }],
  },
  plan: { all_tiles_fit: true, warnings: [], tiles: [{ id: 'tile-r01-c01' }] },
} as unknown as MuralPlanResource

describe('muralBuildReadiness', () => {
  it('requires a saved, current, clean, fitting plan for the active master', () => {
    const preview = { previewJobId: 'preview-a', previewCurrent: true }
    expect(muralBuildReadiness({
      plan,
      planDirty: false,
      processedArtifactId: 'master-a',
      ...preview,
    })).toEqual({ ready: true, reason: null })
    expect(muralBuildReadiness({
      plan: null,
      planDirty: false,
      processedArtifactId: 'master-a',
      ...preview,
    }).reason).toMatch(/Save the current mural plan/)
    expect(muralBuildReadiness({
      plan,
      planDirty: true,
      processedArtifactId: 'master-a',
      ...preview,
    }).reason).toMatch(/Save the mural plan changes/)
    expect(muralBuildReadiness({
      plan,
      planDirty: false,
      processedArtifactId: 'master-b',
      ...preview,
    }).reason).toMatch(/older processed master/)
    expect(muralBuildReadiness({
      plan: { ...plan, plan: { ...plan.plan, all_tiles_fit: false, warnings: ['Too wide.'] } },
      planDirty: false,
      processedArtifactId: 'master-a',
      ...preview,
    }).reason).toBe('Too wide.')
    expect(muralBuildReadiness({
      plan,
      planDirty: false,
      processedArtifactId: 'master-a',
      previewJobId: null,
      previewCurrent: false,
    }).reason).toMatch(/Render a current preview/)
    expect(muralBuildReadiness({
      plan: {
        ...plan,
        request: { ...plan.request, reserved_rectangles: [] },
      } as unknown as MuralPlanResource,
      planDirty: false,
      processedArtifactId: 'master-a',
      ...preview,
    }).reason).toMatch(/reserved bed area/)
    expect(muralBuildReadiness({
      plan: {
        ...plan,
        request: {
          ...plan.request,
          reserved_rectangles: [
            ...plan.request.reserved_rectangles,
            { x_mm: 175, y_mm: 5, width_mm: 35, height_mm: 35 },
          ],
        },
      } as unknown as MuralPlanResource,
      planDirty: false,
      processedArtifactId: 'master-a',
      ...preview,
    }).reason).toMatch(/exactly one/)
  })
})
