import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from '../../api'
import type { EditorHistorySummary } from '../../contracts'
import { HistoryControls, type HistoryDirection } from './HistoryControls'
import { useEditorHistory } from './useEditorHistory'

const history: EditorHistorySummary = {
  lineage_id: 'lineage_1', cursor_node_id: 'node_1', tip_node_id: 'node_1',
  cursor: 1, total: 1, limit: 100, can_undo: true, can_redo: false,
  undo_label: 'Resize canvas', redo_label: null, state_sha256: 'a'.repeat(64),
}

function Harness({ onMove, currentHistory = history, onResync = async () => undefined }: {
  onMove: (direction: HistoryDirection, requestId: string) => Promise<void>
  currentHistory?: EditorHistorySummary
  onResync?: () => Promise<void>
}) {
  const controller = useEditorHistory({
    projectId: 'project_1', history: currentHistory, onMove, onResync,
  })
  return (
    <HistoryControls
      history={currentHistory}
      working={controller.working}
      error={controller.error}
      conflict={controller.conflict}
      retryable={controller.retryable}
      announcement={controller.announcement}
      blockedReason={null}
      onMove={(direction) => void controller.move(direction)}
      onRetry={controller.retry}
      onResync={controller.resync}
    />
  )
}

afterEach(cleanup)

describe('useEditorHistory', () => {
  it('locks rapid actions and announces the exact completed step', async () => {
    let finish!: () => void
    const onMove = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    render(<Harness onMove={onMove} />)
    const undo = screen.getByRole('button', { name: 'Undo: Resize canvas' })
    fireEvent.click(undo)
    fireEvent.click(undo)
    expect(onMove).toHaveBeenCalledOnce()
    expect(screen.getByRole('group', { name: 'Edit history' })).toHaveAttribute('aria-busy', 'true')
    finish()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Undid Resize canvas.'))
  })

  it('retries an ambiguous transient failure with the same idempotency UUID', async () => {
    const ids: string[] = []
    const onMove = vi.fn(async (_direction: HistoryDirection, requestId: string) => {
      ids.push(requestId)
      if (ids.length === 1) throw new Error('Temporary connection failure.')
    })
    render(<Harness onMove={onMove} />)
    fireEvent.click(screen.getByRole('button', { name: 'Undo: Resize canvas' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(ids).toHaveLength(2))
    expect(ids[1]).toBe(ids[0])
  })

  it('requires resync instead of retry when another session moves the cursor', async () => {
    const onMove = vi.fn(async () => {
      throw new ApiRequestError(409, {
        error: {
          code: 'stale_draft', message: 'The history cursor changed.', request_id: 'conflict',
          retryable: false, details: {},
        },
      })
    })
    render(<Harness onMove={onMove} />)
    fireEvent.click(screen.getByRole('button', { name: 'Undo: Resize canvas' }))
    expect(await screen.findByRole('button', { name: 'Reload current draft' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Undo: Resize canvas' })).toBeDisabled()
    fireEvent.keyDown(window, { key: 'z', metaKey: true })
    expect(onMove).toHaveBeenCalledOnce()
  })

  it('keeps its lock and retry UUID when same-lineage history props refresh', async () => {
    const requestIds: string[] = []
    let reject!: (error: unknown) => void
    const onMove = vi.fn((_direction: HistoryDirection, requestId: string) => {
      requestIds.push(requestId)
      return new Promise<void>((_resolve, fail) => { reject = fail })
    })
    const view = render(<Harness onMove={onMove} />)
    fireEvent.click(screen.getByRole('button', { name: 'Undo: Resize canvas' }))

    view.rerender(<Harness
      onMove={onMove}
      currentHistory={{ ...history, cursor_node_id: 'node_refreshed', state_sha256: 'b'.repeat(64) }}
    />)
    fireEvent.keyDown(window, { key: 'z', metaKey: true })
    expect(onMove).toHaveBeenCalledOnce()

    reject(new Error('Temporary connection failure.'))
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(onMove).toHaveBeenCalledTimes(2))
    expect(requestIds[1]).toBe(requestIds[0])
  })

  it('keeps conflict locked through a failed resync and unlocks only after success', async () => {
    const onMove = vi.fn(async () => {
      throw new ApiRequestError(409, {
        error: {
          code: 'stale_draft', message: 'The history cursor changed.', request_id: 'conflict',
          retryable: false, details: {},
        },
      })
    })
    const onResync = vi.fn()
      .mockRejectedValueOnce(new Error('Reload failed.'))
      .mockResolvedValueOnce(undefined)
    render(<Harness onMove={onMove} onResync={onResync} />)
    fireEvent.click(screen.getByRole('button', { name: 'Undo: Resize canvas' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Reload current draft' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Reload failed.')
    expect(screen.getByRole('button', { name: 'Undo: Resize canvas' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Reload current draft' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Undo: Resize canvas' })).toBeEnabled()
  })

  it('clears the live region before repeating an identical announcement', async () => {
    let secondFinish!: () => void
    const onMove = vi.fn()
      .mockResolvedValueOnce(undefined)
      .mockImplementationOnce(() => new Promise<void>((resolve) => { secondFinish = resolve }))
    render(<Harness onMove={onMove} />)
    fireEvent.click(screen.getByRole('button', { name: 'Undo: Resize canvas' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Undid Resize canvas.'))

    fireEvent.click(screen.getByRole('button', { name: 'Undo: Resize canvas' }))
    expect(screen.getByRole('status')).toBeEmptyDOMElement()
    secondFinish()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Undid Resize canvas.'))
  })
})
