import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { JobResource, PreviewJobResult } from '../../contracts'
import { previewStageLabel, type PreviewCoordinatorState } from '../preview'
import { PreviewJobStatus } from './PreviewJobStatus'

function job(overrides: Partial<JobResource> = {}): JobResource {
  return {
    id: 'job-1',
    project_id: 'project-1',
    revision_id: null,
    type: 'preview',
    state: 'running',
    stage: 'quantizing',
    progress: 0.46,
    request_key: 'request-1',
    supersession_key: 'preview-project-1',
    generation: 1,
    failure: null,
    artifact_ids: [],
    created_at: '2026-07-16T00:00:00Z',
    started_at: '2026-07-16T00:00:01Z',
    finished_at: null,
    canceled_at: null,
    ...overrides,
  }
}

function state(overrides: Partial<PreviewCoordinatorState> = {}): PreviewCoordinatorState {
  return {
    phase: 'running',
    requestSequence: 1,
    job: job(),
    result: null,
    resultFreshness: 'none',
    acceptedStart: null,
    failure: null,
    lastRequest: null,
    announcement: 'Preview quantizing, 46 percent.',
    ...overrides,
  }
}

afterEach(cleanup)

describe('PreviewJobStatus', () => {
  it('shows human stage copy, exact progress, and an accessible live announcement', () => {
    render(<PreviewJobStatus state={state()} onCancel={vi.fn()} />)

    expect(screen.getByRole('heading', { name: 'Reducing colors' })).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: 'Preview rendering progress' })).toHaveAttribute('value', '0.46')
    expect(screen.getByText('46% of job checkpoints')).toBeVisible()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', expect.stringContaining('not a time estimate'))
    expect(screen.getByRole('status')).toHaveTextContent('Preview quantizing, 46 percent.')
    expect(screen.getByText('No preview yet')).toBeInTheDocument()
  })

  it('names analysis checkpoints and distinguishes result loading from completion', () => {
    const { rerender } = render(<PreviewJobStatus state={state({ job: job({ stage: 'analyzing', progress: 0.65 }) })} />)
    expect(screen.getByRole('heading', { name: 'Checking printable widths' })).toBeVisible()
    rerender(<PreviewJobStatus state={state({ job: job({ stage: 'complete', state: 'succeeded', progress: 1 }) })} />)
    expect(screen.getByRole('heading', { name: 'Loading preview files' })).toBeVisible()
    expect(screen.getByText('Processing finished. Loading the result into the editor.')).toBeVisible()
  })

  it('exposes cancellation while active and disables it during parent mutations', () => {
    const onCancel = vi.fn()
    const { rerender } = render(<PreviewJobStatus state={state()} onCancel={onCancel} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel preview' }))
    expect(onCancel).toHaveBeenCalledOnce()

    rerender(<PreviewJobStatus state={state()} onCancel={onCancel} disabled />)
    expect(screen.getByRole('button', { name: 'Cancel preview' })).toBeDisabled()
  })

  it('labels an old result as stale while the newest request runs', () => {
    render(<PreviewJobStatus state={state({ resultFreshness: 'stale' })} onCancel={vi.fn()} />)

    expect(screen.getByText('Stale preview visible')).toBeInTheDocument()
    expect(screen.getByText(/previous preview remains visible/i)).toBeInTheDocument()
  })

  it('renders retryable diagnostics, request identifiers, details, and retry action', () => {
    const onRetry = vi.fn()
    render(<PreviewJobStatus state={state({
      phase: 'failed',
      job: job({ state: 'failed', stage: 'failed' }),
      announcement: 'Preview processing failed.',
      failure: {
        source: 'request',
        code: 'resource_limit',
        title: 'Preview request failed',
        message: 'The preview exceeded the safe pixel budget.',
        suggestion: 'Reduce the preview dimensions and try again.',
        requestId: 'req-24kg',
        retryable: true,
        details: { maximum_pixels: 12_000_000, stage: 'normalizing' },
      },
    })} onRetry={onRetry} />)

    expect(screen.getByRole('alert')).toHaveTextContent('safe pixel budget')
    expect(screen.getByText('Reduce the preview dimensions and try again.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Technical details'))
    expect(screen.getByText('resource_limit')).toBeInTheDocument()
    expect(screen.getByText('req-24kg')).toBeInTheDocument()
    expect(screen.getByText('12000000')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry preview' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('does not offer retry for a non-retryable failure', () => {
    render(<PreviewJobStatus state={state({
      phase: 'failed',
      failure: {
        source: 'job',
        code: 'invalid_palette',
        title: 'Preview processing failed',
        message: 'The palette is invalid.',
        suggestion: null,
        requestId: null,
        retryable: false,
        details: {},
      },
    })} onRetry={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Retry preview' })).not.toBeInTheDocument()
  })

  it('uses deterministic labels and a readable fallback for future stages', () => {
    expect(previewStageLabel('cleaning')).toBe('Cleaning tiny details')
    expect(previewStageLabel('future_stage')).toBe('Future stage')
  })

  it('explains scoped reuse and its affected pixel evidence', () => {
    const completed = job({ state: 'succeeded', stage: 'complete', progress: 1 })
    render(<PreviewJobStatus state={state({
      phase: 'succeeded',
      job: completed,
      resultFreshness: 'current',
      result: {
        job: completed,
        reprocessing_plan: {
          schema_version: 1,
          mode: 'scoped',
          reason: 'local_interior_edit',
          message: 'Cached quantization and cleanup were reused.',
          baseline_job_id: 'job-baseline',
          reused_stages: ['quantization', 'automatic_cleanup'],
          recomputed_stages: ['editor_replay', 'analysis'],
          affected_pixel_count: 1280,
          affected_mask_sha256: 'a'.repeat(64),
          output_equivalence: 'by_construction',
        },
      } as PreviewJobResult,
    })} />)

    expect(screen.getByText('Scoped reprocessing')).toBeInTheDocument()
    expect(screen.getByText(/quantization \+ automatic_cleanup/)).toBeInTheDocument()
    expect(screen.getByText(/1,280 pixels/)).toBeInTheDocument()
  })
})
