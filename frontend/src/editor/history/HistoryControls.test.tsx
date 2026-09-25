/// <reference types="node" />

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EditorHistorySummary } from '../../contracts'
import { HistoryControls } from './HistoryControls'

const history: EditorHistorySummary = {
  lineage_id: 'lineage_1', cursor_node_id: 'node_2', tip_node_id: 'node_3',
  cursor: 2, total: 3, limit: 100, can_undo: true, can_redo: true,
  undo_label: 'Resize canvas', redo_label: 'Recolor region', state_sha256: 'a'.repeat(64),
}

afterEach(cleanup)

describe('HistoryControls', () => {
  it('exposes named actions, save blocking, and a polite result', () => {
    const onMove = vi.fn()
    const view = render(
      <HistoryControls
        history={history}
        working={null}
        error={null}
        conflict={false}
        retryable={false}
        announcement="Undid Resize canvas."
        blockedReason="Finish saving before undo or redo"
        onMove={onMove}
        onRetry={vi.fn()}
        onResync={vi.fn()}
      />,
    )
    const undo = screen.getByRole('button', { name: 'Undo: Resize canvas' })
    expect(undo).toBeDisabled()
    expect(undo).toHaveAttribute('title', 'Finish saving before undo or redo')
    expect(undo).toHaveAccessibleDescription('Finish saving before undo or redo')
    expect(screen.getByRole('status')).toHaveTextContent('Undid Resize canvas.')

    view.rerender(
      <HistoryControls
        history={history}
        working={null}
        error={null}
        conflict={false}
        retryable={false}
        announcement=""
        onMove={onMove}
        onRetry={vi.fn()}
        onResync={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Redo: Recolor region' }))
    expect(onMove).toHaveBeenCalledWith('redo')
  })

  it('offers resync instead of an infinite exact retry for conflicts', () => {
    const onRetry = vi.fn()
    const onResync = vi.fn()
    render(
      <HistoryControls
        history={history}
        working={null}
        error="The history cursor changed in another session."
        conflict
        retryable={false}
        announcement=""
        onMove={vi.fn()}
        onRetry={onRetry}
        onResync={onResync}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Undo: Resize canvas' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Undo: Resize canvas' })).toHaveAccessibleDescription(
      /Reload the current draft before editing, undoing, or redoing/,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Reload current draft' }))
    expect(onResync).toHaveBeenCalledOnce()
  })

  it('keeps the compact error surface within the viewport and allows actions to wrap', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/editor/history/HistoryControls.css'), 'utf8')

    expect(css).toMatch(/@media \(max-width: 620px\)[\s\S]*?\.editor-history-error\s*\{[^}]*width:\s*min\(20rem, calc\(100vw - 2rem\)\);[^}]*flex-wrap:\s*wrap;/)
    expect(css).toMatch(/\.editor-history-error > span\s*\{[^}]*width:\s*100%;/)
  })
})
