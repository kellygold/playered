import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  WorkspaceRecoveryNotice,
  WorkspaceRecoveryPanel,
  type StartupReconciliation,
} from './WorkspaceRecoveryPanel'

const interrupted: StartupReconciliation = {
  id: 'reconciliation_1',
  status: 'attention',
  started_at: '2026-07-17T01:00:00Z',
  completed_at: '2026-07-17T01:00:01Z',
  interrupted_jobs: [
    {
      job_id: 'job_export_1',
      project_id: 'project_1',
      type: 'export',
      state_before_restart: 'running',
      stage_before_restart: 'packaging',
      state_after_recovery: 'failed',
      disposition: 'retry_required',
      resumable: false,
      retryable: true,
      action: 'Start a new export from verified geometry evidence.',
    },
  ],
  stale_temp_file_count: 2,
  orphan_file_count: 1,
  database_integrity: true,
  foreign_key_integrity: true,
  automatic_deletions: 0,
  recommended_actions: [
    'Review the interrupted jobs and retry them from the current project state.',
    'Review a workspace cleanup dry run; no temporary or orphaned bytes were deleted.',
  ],
}

const healthy: StartupReconciliation = {
  ...interrupted,
  id: 'reconciliation_2',
  status: 'healthy',
  interrupted_jobs: [],
  stale_temp_file_count: 0,
  orphan_file_count: 0,
  recommended_actions: ['No recovery action is required.'],
}

const cleanupPlan = {
  generated_at: '2026-07-17T01:01:00Z',
  minimum_age_seconds: 86400,
  plan_sha256: 'a'.repeat(64),
  candidate_count: 1,
  reclaimable_bytes: 2048,
  candidates: [
    {
      relative_path: 'temp/interrupted.tmp',
      namespace: 'temp',
      reason: 'stale_temp',
      byte_size: 2048,
      sha256: 'b'.repeat(64),
      mtime_ns: 1,
    },
  ],
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('WorkspaceRecoveryPanel', () => {
  it('shows truthful compact interruption evidence and opens from one clear action', () => {
    const onOpen = vi.fn()
    render(<WorkspaceRecoveryNotice report={interrupted} onOpen={onOpen} />)

    expect(screen.getByRole('region', { name: 'Startup recovery status' })).toHaveTextContent(
      '1 task did not finish. Open the details to see what to retry.',
    )
    fireEvent.click(screen.getByRole('button', { name: 'View details' }))
    expect(onOpen).toHaveBeenCalledOnce()
  })

  it('stays hidden when the latest startup reconciliation is healthy', () => {
    const { container } = render(<WorkspaceRecoveryNotice report={healthy} onOpen={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('does not interrupt users for leftover files alone', () => {
    const { container } = render(
      <WorkspaceRecoveryNotice report={{ ...interrupted, interrupted_jobs: [] }} onOpen={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it.each([
    { status: 'critical' as const },
    { database_integrity: false },
    { foreign_key_integrity: false },
  ])('keeps integrity warnings visible even without interrupted work: %j', (problem) => {
    const onOpen = vi.fn()
    render(<WorkspaceRecoveryNotice report={{ ...healthy, ...problem }} onOpen={onOpen} />)
    expect(screen.getByRole('region', { name: 'Startup recovery status' })).toHaveTextContent(
      'Your saved workspace needs attention',
    )
    fireEvent.click(screen.getByRole('button', { name: 'View details' }))
    expect(onOpen).toHaveBeenCalledOnce()
  })

  it('keeps optional cleanup accessible without claiming there was an interruption', () => {
    render(
      <WorkspaceRecoveryPanel
        report={{ ...interrupted, interrupted_jobs: [] }}
        onReconcile={vi.fn(async () => healthy)}
        onReviewCleanup={vi.fn(async () => cleanupPlan)}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByRole('dialog', { name: 'Workspace status' })).toHaveTextContent(
      'Unused files are available for optional cleanup. You can keep working.',
    )
    expect(screen.getByRole('button', { name: 'Review cleanup dry run' })).toBeEnabled()
    expect(screen.queryByText('Some work was interrupted')).not.toBeInTheDocument()
  })

  it('explains that jobs were not resumed and cleanup remains a reviewed action', async () => {
    const onReconcile = vi.fn(async () => healthy)
    const onReviewCleanup = vi.fn(async () => cleanupPlan)
    render(
      <WorkspaceRecoveryPanel
        report={interrupted}
        onReconcile={onReconcile}
        onReviewCleanup={onReviewCleanup}
        onClose={vi.fn()}
      />,
    )

    const dialog = screen.getByRole('dialog', { name: 'Some work was interrupted' })
    expect(screen.getByRole('button', { name: 'Close recovery details' })).toHaveFocus()
    expect(dialog).toHaveTextContent('Automatic deletions0')
    expect(dialog).toHaveTextContent('Stopped during packaging. It was not silently resumed.')
    const cleanup = screen.getByRole('button', { name: 'Review cleanup dry run' })
    expect(cleanup).toBeEnabled()
    fireEvent.click(cleanup)
    await waitFor(() => expect(onReviewCleanup).toHaveBeenCalledOnce())
    expect(await screen.findByRole('heading', { name: 'Cleanup dry run' })).toBeInTheDocument()
    expect(dialog).toHaveTextContent('Nothing has been deleted')
    expect(dialog).toHaveTextContent('temp/interrupted.tmp')
    fireEvent.click(screen.getByRole('button', { name: 'Run safe check again' }))

    await waitFor(() => expect(onReconcile).toHaveBeenCalledOnce())
    await waitFor(() => expect(dialog).toHaveTextContent('Workspace status'))
    expect(screen.getByRole('button', { name: 'Review cleanup dry run' })).toBeDisabled()
  })

  it('closes on Escape and traps keyboard focus inside the recovery dialog', () => {
    const onClose = vi.fn()
    render(
      <WorkspaceRecoveryPanel
        report={interrupted}
        onReconcile={vi.fn(async () => interrupted)}
        onReviewCleanup={vi.fn(async () => cleanupPlan)}
        onClose={onClose}
      />,
    )
    const dialog = screen.getByRole('dialog')
    const close = screen.getByRole('button', { name: 'Close recovery details' })
    const done = screen.getByRole('button', { name: 'Done' })
    done.focus()
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('keeps the exact report visible when a safe recheck fails', async () => {
    const onReconcile = vi.fn(async () => {
      throw new Error('Recovery service is unavailable.')
    })
    render(
      <WorkspaceRecoveryPanel
        report={interrupted}
        onReconcile={onReconcile}
        onReviewCleanup={vi.fn(async () => cleanupPlan)}
        onClose={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Run safe check again' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Recovery service is unavailable.')
    expect(screen.getByText('job_export_1')).toBeInTheDocument()
  })
})
