import type { MuralPlanResource } from './types'

export type MuralBuildReadinessInput = Readonly<{
  plan: MuralPlanResource | null
  planDirty: boolean
  processedArtifactId: string
  previewJobId: string | null
  previewCurrent: boolean
  disabled?: boolean
  disabledReason?: string
}>

export type MuralBuildReadiness = Readonly<{
  ready: boolean
  reason: string | null
}>

export function muralBuildReadiness({
  plan,
  planDirty,
  processedArtifactId,
  previewJobId,
  previewCurrent,
  disabled = false,
  disabledReason,
}: MuralBuildReadinessInput): MuralBuildReadiness {
  if (disabled) {
    return { ready: false, reason: disabledReason ?? 'Finish the current editor action first.' }
  }
  if (!plan) return { ready: false, reason: 'Save the current mural plan before building.' }
  if (!previewCurrent || !previewJobId) {
    return { ready: false, reason: 'Render a current preview before building the mural.' }
  }
  if (planDirty) return { ready: false, reason: 'Save the mural plan changes before building.' }
  if (plan.freshness === 'stale') {
    return { ready: false, reason: plan.stale_reason ?? 'Review and resave the stale mural plan.' }
  }
  if (plan.request.source.processed_artifact_id !== processedArtifactId) {
    return { ready: false, reason: 'The saved plan belongs to an older processed master.' }
  }
  if (plan.request.reserved_rectangles.length !== 1) {
    return {
      ready: false,
      reason: plan.request.reserved_rectangles.length === 0
        ? 'Add and save one reserved bed area for the slicer prime tower before building.'
        : 'Keep exactly one saved prime-tower reserve before building.',
    }
  }
  if (!plan.plan.all_tiles_fit) {
    return { ready: false, reason: plan.plan.warnings[0] ?? 'Every tile must fit the selected bed.' }
  }
  if (plan.generation < 1 || plan.plan.tiles.length < 1) {
    return { ready: false, reason: 'The saved plan does not contain printable build plates.' }
  }
  return { ready: true, reason: null }
}
