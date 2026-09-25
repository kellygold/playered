import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type {
  JobConfigV1,
  RevisionBranch,
  RevisionPublication,
  RevisionResource,
  RevisionSummary,
} from '../../contracts'
import { RevisionLifecycle } from './RevisionLifecycle'

const revisionConfig = {
  canvas: { width_mm: 200, height_mm: 140 },
  palette: { colors: [{ id: 'black' }, { id: 'cream' }] },
} as JobConfigV1

function resource(id: string, label: string, parent: string | null = null): RevisionResource {
  return {
    id,
    project_id: 'project_1',
    source_asset_id: 'asset_1',
    parent_revision_id: parent,
    schema_version: 1,
    engine_version: '0.1.0',
    config: revisionConfig,
    config_sha256: id.padEnd(64, 'a'),
    editor_sequence_sha256: id.padEnd(64, 'b'),
    label,
    notes: `${label} notes`,
    operations: [],
    artifacts: [],
    preview_evidence: {
      status: 'fresh',
      preview_job_id: 'job_1',
      reason: 'Exact current preview was copied.',
      source_draft_generation: 2,
      editor_sequence_sha256: id.padEnd(64, 'b'),
      artifact_count: 4,
    },
    published_at: '2026-07-16T01:00:00Z',
  }
}

function summary(item: RevisionResource): RevisionSummary {
  return {
    id: item.id,
    project_id: item.project_id,
    parent_revision_id: item.parent_revision_id,
    label: item.label,
    config_sha256: item.config_sha256,
    editor_sequence_sha256: item.editor_sequence_sha256,
    preview_evidence: item.preview_evidence,
    published_at: item.published_at,
    operation_count: item.operations.length,
    artifact_count: item.artifacts.length,
    is_active: item.id === current.id,
  }
}

const older = resource('revision_old', 'First cleanup')
const current = resource('revision_current', 'Print-ready proof', older.id)

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function installRevisionFetch(options: { listFailures?: number; detailFailure?: boolean } = {}) {
  let listAttempts = 0
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    if (url.includes('/revisions?')) {
      listAttempts += 1
      if (listAttempts <= (options.listFailures ?? 0)) {
        return response({ error: { code: 'internal_error', message: 'List unavailable.', request_id: '1', retryable: true, details: {} } }, 503)
      }
      return response({ items: [summary(current), summary(older)], total: 2 })
    }
    if (url.endsWith(`/revisions/${older.id}`)) {
      if (options.detailFailure) return response({ error: { code: 'internal_error', message: 'Revision unavailable.', request_id: '2', retryable: true, details: {} } }, 503)
      return response(older)
    }
    if (url.endsWith(`/revisions/${current.id}`)) return response(current)
    throw new Error(`Unexpected fetch: ${url}`)
  })
}

function publication(item = current): RevisionPublication {
  return {
    revision: item,
    project: {
      id: 'project_1', name: 'Proof', description: '', active_revision_id: item.id,
      preferences: {}, created_at: '', updated_at: '', archived_at: null,
    },
    draft: {
      project_id: 'project_1', base_revision_id: item.id, config: revisionConfig,
      operations: [], config_sha256: item.config_sha256, editor_sequence_sha256: item.editor_sequence_sha256,
      generation: 3, updated_at: '',
      history: {
        lineage_id: 'lineage-1', cursor_node_id: 'node-1', tip_node_id: 'node-1',
        cursor: 0, total: 0, limit: 100, can_undo: false, can_redo: false,
        undo_label: null, redo_label: null, state_sha256: 'c'.repeat(64),
      },
    },
  }
}

function branch(item = older): RevisionBranch {
  return {
    base_revision: item,
    project: {
      id: 'project_1', name: 'Proof', description: '', active_revision_id: current.id,
      preferences: {}, created_at: '', updated_at: '', archived_at: null,
    },
    draft: {
      project_id: 'project_1', base_revision_id: item.id, config: revisionConfig,
      operations: [], config_sha256: item.config_sha256, editor_sequence_sha256: item.editor_sequence_sha256,
      generation: 4, updated_at: '',
      history: {
        lineage_id: 'lineage-2', cursor_node_id: 'node-2', tip_node_id: 'node-2',
        cursor: 0, total: 0, limit: 100, can_undo: false, can_redo: false,
        undo_label: null, redo_label: null, state_sha256: 'd'.repeat(64),
      },
    },
  }
}

function setup(overrides: Partial<ComponentProps<typeof RevisionLifecycle>> = {}) {
  const onPublish = vi.fn(async () => publication())
  const onBranch = vi.fn(async () => branch())
  const view = render(
    <RevisionLifecycle
      projectId="project_1"
      activeRevisionId={current.id}
      draftBaseRevisionId={current.id}
      persistence="saved"
      artifactFreshness="current"
      previewJobId="job_1"
      onPublish={onPublish}
      onBranch={onBranch}
      {...overrides}
    />,
  )
  return { ...view, onPublish, onBranch }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RevisionLifecycle', () => {
  it('shows whether a regional edit is deterministic, seeded, or best effort', async () => {
    const withProvenance = {
      ...current,
      operations: [{
        operation_type: 'regional-model-edit', selection: {}, parameters: {}, source: 'model',
        provenance: {
          regional_edit: {
            execution_kind: 'provider', reproducibility: 'best_effort',
            reproducibility_reason: 'Exact replay is not guaranteed by this provider.',
            provider_id: 'gemini', model_id: 'image-model', model_version: '2026-07-01',
            output_sha256: 'a'.repeat(64),
          },
        },
      }],
    } as RevisionResource
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/revisions?')) return response({ items: [summary(withProvenance), summary(older)], total: 2 })
      if (url.endsWith(`/revisions/${withProvenance.id}`)) return response(withProvenance)
      if (url.endsWith(`/revisions/${older.id}`)) return response(older)
      throw new Error(`Unexpected fetch: ${url}`)
    })
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.click(await screen.findByRole('button', { name: /Print-ready proof/ }))

    const provenance = await screen.findByRole('region', { name: 'Regional edit reproducibility' })
    expect(provenance).toHaveTextContent('Best effort')
    expect(provenance).toHaveTextContent('Exact replay is not guaranteed')
    expect(provenance).toHaveTextContent('gemini · image-model · 2026-07-01')
    expect(provenance).toHaveTextContent('Output aaaaaaaaaaaa…')
  })

  it('reopens compact history as exact read-only detail and confirms branching an older revision', async () => {
    installRevisionFetch()
    const { onBranch } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))

    expect(await screen.findByText('Current · draft base')).toBeInTheDocument()
    expect(screen.getByText('Older revision')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /First cleanup/ }))
    expect(await screen.findByRole('region', { name: 'Revision details for First cleanup' })).toHaveTextContent('Locked')
    expect(screen.queryByRole('textbox', { name: /First cleanup/ })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Branch from this revision' }))
    const confirm = screen.getByRole('alertdialog', { name: 'Confirm revision branch' })
    expect(confirm).toHaveTextContent('Published revisions stay unchanged')
    fireEvent.click(screen.getByRole('button', { name: 'Create branch draft' }))
    await waitFor(() => expect(onBranch).toHaveBeenCalledWith(older))
    expect(screen.getAllByText('First cleanup')).toHaveLength(2)
  })

  it('publishes a named current draft with exact preview evidence', async () => {
    installRevisionFetch()
    const { onPublish } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    await screen.findByText('Print-ready proof')
    expect(screen.getByRole('button', { name: 'Publish revision' })).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: '  Color proof  ' } })
    fireEvent.change(screen.getByPlaceholderText(/What changed/), { target: { value: 'Approved on textured plate.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    await waitFor(() => expect(onPublish).toHaveBeenCalledWith({
      label: 'Color proof', notes: 'Approved on textured plate.', previewJobId: 'job_1',
    }))
    expect(screen.queryByRole('alertdialog', { name: /without current/ })).not.toBeInTheDocument()
  })

  it('blocks publish during saving and requires confirmation for a stale artifact snapshot', async () => {
    installRevisionFetch()
    const { rerender, onPublish, onBranch } = setup({ persistence: 'saving', artifactFreshness: 'stale', previewJobId: null })
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: 'Risky snapshot' } })
    expect(screen.getByRole('button', { name: 'Publish revision' })).toBeDisabled()
    expect(screen.getByText('Wait for the latest draft save to finish.')).toBeInTheDocument()

    rerender(<RevisionLifecycle projectId="project_1" activeRevisionId={current.id} draftBaseRevisionId={current.id} persistence="saved" artifactFreshness="stale" previewJobId={null} onPublish={onPublish} onBranch={onBranch} />)
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    expect(screen.getByRole('alertdialog', { name: 'Publish without current preview artifacts' })).toBeInTheDocument()
    expect(onPublish).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Publish without artifacts' }))
    await waitFor(() => expect(onPublish).toHaveBeenCalledWith({ label: 'Risky snapshot', notes: '', previewJobId: null }))
  })

  it('preserves publish input across an API failure and retries the exact action', async () => {
    installRevisionFetch()
    const onPublish = vi.fn()
      .mockRejectedValueOnce(new Error('Generation conflict.'))
      .mockResolvedValueOnce(publication())
    setup({ onPublish })
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: 'Exact retry' } })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Generation conflict.')
    expect(screen.getByPlaceholderText('e.g. Cleanup approved')).toHaveValue('Exact retry')
    fireEvent.click(screen.getByRole('button', { name: 'Retry action' }))
    await waitFor(() => expect(onPublish).toHaveBeenCalledTimes(2))
    expect(onPublish.mock.calls[1]).toEqual(onPublish.mock.calls[0])
  })

  it('recovers a failed revision list without closing the drawer', async () => {
    installRevisionFetch({ listFailures: 1 })
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('List unavailable.')
    fireEvent.click(screen.getByRole('button', { name: 'Retry list' }))
    expect(await screen.findByText('Print-ready proof')).toBeInTheDocument()
  })

  it('resets project-scoped state and ignores a stale list response after project replacement', async () => {
    const oldList = deferred<Response>()
    const replacement = {
      ...current,
      id: 'revision_b',
      project_id: 'project_2',
      label: 'Replacement history',
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/projects/project_1/revisions?')) return oldList.promise
      if (url.includes('/projects/project_2/revisions?')) {
        return response({ items: [summary(replacement)], total: 1 })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    })
    const onPublish = vi.fn(async () => publication())
    const onBranch = vi.fn(async () => branch())
    const view = render(
      <RevisionLifecycle projectId="project_1" activeRevisionId={current.id} draftBaseRevisionId={current.id} persistence="saved" artifactFreshness="current" previewJobId="job_1" onPublish={onPublish} onBranch={onBranch} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    view.rerender(
      <RevisionLifecycle projectId="project_2" activeRevisionId={replacement.id} draftBaseRevisionId={replacement.id} persistence="saved" artifactFreshness="missing" previewJobId={null} onPublish={onPublish} onBranch={onBranch} />,
    )
    expect(screen.queryByRole('dialog', { name: 'Project revisions' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    expect(await screen.findByText('Replacement history')).toBeInTheDocument()
    await act(async () => oldList.resolve(response({ items: [summary(current)], total: 1 })))
    expect(screen.queryByText('Print-ready proof')).not.toBeInTheDocument()
  })

  it('keeps the newest exact detail when older and newer requests complete out of order', async () => {
    const oldDetail = deferred<Response>()
    const currentDetail = deferred<Response>()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/revisions?')) return response({ items: [summary(current), summary(older)], total: 2 })
      if (url.endsWith(`/revisions/${older.id}`)) return oldDetail.promise
      if (url.endsWith(`/revisions/${current.id}`)) return currentDetail.promise
      throw new Error(`Unexpected fetch: ${url}`)
    })
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    await screen.findByText('Older revision')
    fireEvent.click(screen.getByRole('button', { name: /First cleanup/ }))
    fireEvent.click(screen.getByRole('button', { name: /Print-ready proof/ }))
    await act(async () => currentDetail.resolve(response(current)))
    expect(await screen.findByRole('region', { name: 'Revision details for Print-ready proof' })).toBeInTheDocument()
    await act(async () => oldDetail.resolve(response(older)))
    expect(screen.getByRole('region', { name: 'Revision details for Print-ready proof' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Revision details for First cleanup' })).not.toBeInTheDocument()
  })

  it('contains keyboard focus in the drawer and nested confirmation with layered Escape restore', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ items: [], total: 0 }))
    setup({ artifactFreshness: 'stale', previewJobId: null })
    const trigger = screen.getByRole('button', { name: 'Revisions' })
    fireEvent.click(trigger)
    const closeButton = screen.getByRole('button', { name: 'Close revisions' })
    await waitFor(() => expect(closeButton).toHaveFocus())
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: 'Keyboard proof' } })
    const publishButton = screen.getByRole('button', { name: 'Publish revision' })
    await waitFor(() => expect(publishButton).toBeEnabled())
    publishButton.focus()
    fireEvent.keyDown(publishButton, { key: 'Tab' })
    expect(closeButton).toHaveFocus()

    fireEvent.click(publishButton)
    const cancel = screen.getByRole('button', { name: 'Go back' })
    await waitFor(() => expect(cancel).toHaveFocus())
    const drawer = document.getElementById('revision-drawer') as HTMLElement
    expect(drawer).not.toBeNull()
    expect(drawer).toHaveAttribute('aria-hidden', 'true')
    expect(drawer).toHaveAttribute('inert')
    expect(drawer).not.toHaveAttribute('aria-modal')
    closeButton.focus()
    expect(cancel).toHaveFocus()
    fireEvent.click(closeButton)
    expect(screen.getByRole('alertdialog', { name: 'Publish without current preview artifacts' })).toBeInTheDocument()
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(screen.getByRole('button', { name: 'Publish without artifacts' })).toHaveFocus()
    fireEvent.keyDown(screen.getByRole('button', { name: 'Publish without artifacts' }), { key: 'Tab' })
    expect(cancel).toHaveFocus()
    fireEvent.keyDown(cancel, { key: 'Escape' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Publish revision' })).toHaveFocus())
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(drawer).not.toHaveAttribute('aria-hidden')
    expect(drawer).not.toHaveAttribute('inert')
    expect(drawer).toHaveAttribute('aria-modal', 'true')
    fireEvent.keyDown(screen.getByRole('button', { name: 'Publish revision' }), { key: 'Escape' })
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(document.body.style.overflow).toBe('')
  })

  it('synchronously suppresses duplicate publish and branch confirmation activation', async () => {
    installRevisionFetch()
    const pendingPublish = deferred<RevisionPublication>()
    const pendingBranch = deferred<RevisionBranch>()
    const onPublish = vi.fn(() => pendingPublish.promise)
    const onBranch = vi.fn(() => pendingBranch.promise)
    setup({ artifactFreshness: 'stale', previewJobId: null, onPublish, onBranch })
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    await screen.findByText('Older revision')
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: 'Once only' } })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    const confirmPublish = screen.getByRole('button', { name: 'Publish without artifacts' })
    fireEvent.click(confirmPublish)
    fireEvent.click(confirmPublish)
    expect(onPublish).toHaveBeenCalledTimes(1)
    await act(async () => pendingPublish.resolve(publication()))

    fireEvent.click(screen.getByRole('button', { name: /First cleanup/ }))
    await screen.findByRole('region', { name: 'Revision details for First cleanup' })
    fireEvent.click(screen.getByRole('button', { name: 'Branch from this revision' }))
    const confirmBranch = screen.getByRole('button', { name: 'Create branch draft' })
    fireEvent.click(confirmBranch)
    fireEvent.click(confirmBranch)
    expect(onBranch).toHaveBeenCalledTimes(1)
    await act(async () => pendingBranch.resolve(branch()))
  })

  it('appends cursor-paginated older revisions without replacing or duplicating the first page', async () => {
    const olderPage = deferred<Response>()
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('cursor=revision_old')) {
        return olderPage.promise
      }
      return response({ items: [summary(current)], total: 2, next_cursor: older.id })
    })
    setup()
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    const loadOlder = await screen.findByRole('button', { name: 'Load older revisions' })
    expect(screen.getByText('1 of 2 loaded')).toBeInTheDocument()
    fireEvent.click(loadOlder)
    expect(screen.getByRole('button', { name: 'Loading older revisions…' })).toBeDisabled()
    await act(async () => olderPage.resolve(response({ items: [summary(older), summary(current)], total: 2, next_cursor: null })))
    expect(await screen.findByText('First cleanup')).toBeInTheDocument()
    expect(screen.getAllByText('Print-ready proof')).toHaveLength(1)
    expect(screen.getByText('2 saved')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load older revisions' })).not.toBeInTheDocument()
    expect(fetch.mock.calls.some(([url]) => String(url).includes('cursor=revision_old'))).toBe(true)
  })
})
