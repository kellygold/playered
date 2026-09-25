const stageLabels: Record<string, string> = {
  queued: 'Waiting for the local engine',
  ingesting: 'Reading source image',
  normalizing: 'Normalizing image',
  quantizing: 'Reducing colors',
  analyzing: 'Analyzing printable regions',
  cleaning: 'Cleaning tiny details',
  vectorizing: 'Tracing region boundaries',
  meshing: 'Building printable geometry',
  packaging: 'Packaging output',
  slicing: 'Checking slicer output',
  validating: 'Validating result',
  complete: 'Preview complete',
  failed: 'Preview failed',
  canceled: 'Preview canceled',
  superseded: 'Preview superseded',
}

export function previewStageLabel(stage: string): string {
  return stageLabels[stage] ?? stage.replaceAll('_', ' ').replace(/^./, (value) => value.toUpperCase())
}

// Checkpoints are emitted by PreviewProcessor; these describe work, not remaining time.
export function previewCheckpointLabel(stage: string, progress: number): string {
  if (stage === 'cleaning' && progress >= 0.48) return 'Applying saved edits'
  if (stage === 'analyzing') {
    if (progress >= 0.9) return 'Saving preview files'
    if (progress >= 0.82) return 'Preparing print-risk overlays'
    if (progress >= 0.72) return 'Checking holes and gaps'
    if (progress >= 0.65) return 'Checking printable widths'
    if (progress >= 0.6) return 'Finding small regions'
    if (progress >= 0.55) return 'Measuring color coverage'
  }
  return previewStageLabel(stage)
}
