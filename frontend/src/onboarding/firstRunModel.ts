export const FIRST_RUN_STAGE_IDS = ['source', 'processed', 'geometry', 'validated'] as const

export type FirstRunStageId = (typeof FIRST_RUN_STAGE_IDS)[number]

export type FirstRunState = Readonly<{
  hasProject: boolean
  previewCurrent: boolean
  geometryReady: boolean
  outputWorking: boolean
  outputCurrent: boolean
}>

export function firstRunStage(state: FirstRunState): FirstRunStageId {
  if (!state.hasProject || !state.previewCurrent) return 'source'
  if (state.outputCurrent) return 'validated'
  if (state.geometryReady || state.outputWorking) return 'geometry'
  return 'processed'
}

export function firstRunStageIndex(stage: FirstRunStageId): number {
  return FIRST_RUN_STAGE_IDS.indexOf(stage)
}
