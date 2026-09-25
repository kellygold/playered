import { previewStageLabel } from '../preview/previewJobPresentation'
import type { JobResource } from '../../contracts'
import type { OutputCoordinatorState } from './OutputJobCoordinator'

export const OUTPUT_STAGE_IDS = [
  'preview',
  'geometry',
  'package',
  'slicer',
  'download',
] as const

export type OutputStageId = (typeof OUTPUT_STAGE_IDS)[number]
export type OutputStageState = 'complete' | 'action' | 'working' | 'blocked' | 'waiting' | 'available'
export type OutputActionId =
  | 'render-preview'
  | 'generate-output'
  | 'cancel-output'
  | 'download-output'

export type OutputReadinessStage = Readonly<{
  id: OutputStageId
  label: string
  state: OutputStageState
  status: string
  detail: string
  current?: boolean
}>

export type OutputReadinessAction = Readonly<{
  id: OutputActionId
  label: string
  disabled?: boolean
  disabledReason?: string
}>

export type OutputReadinessModel = Readonly<{
  headline: string
  summary: string
  stages: readonly OutputReadinessStage[]
  primaryAction?: OutputReadinessAction
}>

export type PreviewOutputFreshness = 'unavailable' | 'stale' | 'rendering' | 'current'

const waitingStages: OutputReadinessStage[] = [
  {
    id: 'geometry',
    label: 'Geometry',
    state: 'waiting',
    status: 'Waiting',
    detail: 'Waiting for a current exact preview.',
  },
  {
    id: 'package',
    label: '3MF package',
    state: 'waiting',
    status: 'Waiting',
    detail: 'Waiting for verified geometry.',
  },
  {
    id: 'slicer',
    label: 'Slicer validation',
    state: 'waiting',
    status: 'Waiting',
    detail: 'Waiting for a packaged 3MF.',
  },
  {
    id: 'download',
    label: 'Download',
    state: 'waiting',
    status: 'No file yet',
    detail: 'A download appears after packaging and validation succeed.',
  },
]

function completePreview(): OutputReadinessStage {
  return {
    id: 'preview',
    label: 'Preview',
    state: 'complete',
    status: 'Complete',
    detail: 'Raster, regions, palette, and print-risk analysis are current.',
  }
}

function progress(job: JobResource | null, fallback: string): string {
  return job ? previewStageLabel(job.stage) : fallback
}

function readyToGenerate(actionDisabledReason?: string): OutputReadinessModel {
  return {
    headline: 'Preview ready to build a 3MF',
    summary: 'Build verified geometry, package it for Bambu Studio, and validate the result.',
    stages: [
      completePreview(),
      {
        id: 'geometry',
        label: 'Geometry',
        state: 'action',
        status: 'Ready',
        detail: 'Build printable regions from this exact preview.',
        current: true,
      },
      ...waitingStages.slice(1),
    ],
    primaryAction: {
      id: 'generate-output',
      label: 'Build 3MF',
      disabled: Boolean(actionDisabledReason),
      disabledReason: actionDisabledReason,
    },
  }
}

export function outputReadiness(
  freshness: PreviewOutputFreshness,
  output: OutputCoordinatorState,
  actionDisabledReason?: string,
): OutputReadinessModel {
  if (freshness !== 'current') {
    if (freshness === 'rendering') {
      return {
        headline: 'Rendering the exact preview',
        summary: 'Preview processing is in progress. Output generation starts after it is current.',
        stages: [
          {
            id: 'preview',
            label: 'Preview',
            state: 'working',
            status: 'In progress',
            detail: 'The local engine is rebuilding raster and analysis artifacts.',
            current: true,
          },
          ...waitingStages,
        ],
      }
    }
    const stale = freshness === 'stale'
    return {
      headline: stale ? 'Update the preview before output' : 'Start with an exact preview',
      summary: stale
        ? 'The visible preview does not match the saved draft. Render it again before generating output.'
        : 'Render the artwork to inspect the exact palette, regions, and print risks.',
      stages: [
        {
          id: 'preview',
          label: 'Preview',
          state: 'action',
          status: stale ? 'Needs update' : 'Ready to render',
          detail: stale
            ? 'Draft changes have made the previous preview stale.'
            : 'No exact preview has been rendered yet.',
          current: true,
        },
        ...waitingStages,
      ],
      primaryAction: {
        id: 'render-preview',
        label: stale ? 'Update preview' : 'Render preview',
        disabled: Boolean(actionDisabledReason),
        disabledReason: actionDisabledReason,
      },
    }
  }

  if (output.phase === 'succeeded' && output.resultFreshness === 'current' && output.result?.package) {
    return {
      headline: 'Validated 3MF ready',
      summary: 'Bambu Studio opened and sliced the package for the current preview. This is structural validation, not a physical print; adhesion, color order, surface finish, and tiny-feature survival are not verified.',
      stages: [
        completePreview(),
        { id: 'geometry', label: 'Geometry', state: 'complete', status: 'Complete', detail: 'Printable geometry was verified.' },
        { id: 'package', label: '3MF package', state: 'complete', status: 'Complete', detail: 'The Bambu Studio package was created.' },
        { id: 'slicer', label: 'Slicer validation', state: 'complete', status: 'Passed', detail: 'Bambu Studio opened and sliced the package; no physical print was performed.' },
        { id: 'download', label: 'Download', state: 'available', status: 'Ready', detail: 'Verified .3mf package is ready.', current: true },
      ],
      primaryAction: { id: 'download-output', label: 'Download 3MF' },
    }
  }

  if (output.phase === 'failed' && output.failure) {
    const geometryFailed = output.failure.step === 'geometry'
    return {
      headline: output.failure.title,
      summary: [output.failure.message, output.failure.suggestion].filter(Boolean).join(' '),
      stages: [
        completePreview(),
        {
          id: 'geometry',
          label: 'Geometry',
          state: geometryFailed ? 'blocked' : 'complete',
          status: geometryFailed ? 'Failed' : 'Complete',
          detail: geometryFailed ? output.failure.message : 'Printable geometry was verified.',
          current: geometryFailed,
        },
        {
          id: 'package',
          label: '3MF package',
          state: geometryFailed ? 'waiting' : 'blocked',
          status: geometryFailed ? 'Waiting' : 'Failed',
          detail: geometryFailed ? 'Geometry must succeed first.' : output.failure.message,
          current: !geometryFailed,
        },
        ...waitingStages.slice(2),
      ],
      primaryAction: {
        id: 'generate-output',
        label: output.failure.retryable ? 'Retry 3MF build' : 'Build again',
        disabled: !output.failure.retryable || Boolean(actionDisabledReason),
        disabledReason: !output.failure.retryable
          ? output.failure.suggestion ?? 'Correct the reported output problem before trying again.'
          : actionDisabledReason,
      },
    }
  }

  const geometryActive = [
    'submitting-geometry',
    'geometry-queued',
    'geometry-running',
  ].includes(output.phase) || (output.phase === 'canceling' && output.activeStep === 'geometry')
  const exportActive = [
    'submitting-export',
    'export-queued',
    'export-running',
  ].includes(output.phase) || (output.phase === 'canceling' && output.activeStep === 'export')

  if (geometryActive || exportActive) {
    const exporting = exportActive && output.activeStep === 'export'
    const validating = exporting && ['slicing', 'validating'].includes(output.exportJob?.stage ?? '')
    return {
      headline: geometryActive ? 'Generating printable geometry' : 'Building and validating the 3MF',
      summary: output.phase === 'canceling' ? 'Waiting for the local engine to cancel.'
        : validating ? 'Bambu Studio is checking the 3MF before download.'
        : geometryActive ? 'Tracing shapes and building printable solids.'
        : 'Writing the 3MF package.',
      stages: [
        completePreview(),
        {
          id: 'geometry',
          label: 'Geometry',
          state: geometryActive ? 'working' : 'complete',
          status: geometryActive ? progress(output.geometryJob, 'Submitting') : 'Complete',
          detail: geometryActive ? 'Building printable regions from the exact preview.' : 'Printable geometry was verified.',
          current: geometryActive,
        },
        {
          id: 'package',
          label: '3MF package',
          state: !exporting ? 'waiting' : validating ? 'complete' : 'working',
          status: !exporting ? 'Waiting' : validating ? 'Complete' : progress(output.exportJob, 'Submitting'),
          detail: !exporting ? 'Waiting for verified geometry.' : validating ? 'The package is ready for validation.' : 'Writing the Bambu Studio project package.',
          current: exporting && !validating,
        },
        {
          id: 'slicer',
          label: 'Slicer validation',
          state: validating ? 'working' : 'waiting',
          status: validating ? progress(output.exportJob, 'Validating') : 'Waiting',
          detail: validating ? 'Checking the packaged output before download.' : 'Waiting for a packaged 3MF.',
          current: validating,
        },
        waitingStages[3],
      ],
      primaryAction: output.phase === 'canceling'
        ? {
            id: 'cancel-output',
            label: 'Canceling…',
            disabled: true,
            disabledReason: 'Waiting for the local engine to confirm cancellation.',
          }
        : { id: 'cancel-output', label: 'Cancel 3MF build' },
    }
  }

  return readyToGenerate(actionDisabledReason)
}

export function previewOnlyOutputReadiness(
  freshness: PreviewOutputFreshness,
  actionDisabledReason?: string,
): OutputReadinessModel {
  return outputReadiness(freshness, {
    projectId: null,
    phase: 'idle',
    requestSequence: 0,
    activeStep: null,
    geometryJob: null,
    geometryResult: null,
    exportJob: null,
    result: null,
    resultFreshness: 'none',
    failure: null,
    lastRequest: null,
    announcement: 'No 3MF output has been generated.',
  }, actionDisabledReason)
}
