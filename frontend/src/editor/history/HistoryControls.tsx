import { useId } from 'react'
import type { EditorHistorySummary } from '../../contracts'
import { shortcutLabel } from './historyModel'
import './HistoryControls.css'

export type HistoryDirection = 'undo' | 'redo'
export type HistoryWorking = HistoryDirection | 'resync'

type HistoryControlsProps = {
  history: EditorHistorySummary
  working: HistoryWorking | null
  error: string | null
  conflict: boolean
  retryable: boolean
  announcement: string
  blockedReason?: string | null
  onMove: (direction: HistoryDirection) => void
  onRetry: () => void
  onResync: () => void
}

function actionName(direction: HistoryDirection, label: string | null): string {
  return label ? `${direction === 'undo' ? 'Undo' : 'Redo'}: ${label}` : direction === 'undo' ? 'Undo' : 'Redo'
}

export function HistoryControls({
  history,
  working,
  error,
  conflict,
  retryable,
  announcement,
  blockedReason = null,
  onMove,
  onRetry,
  onResync,
}: HistoryControlsProps) {
  const busy = working !== null
  const undoName = actionName('undo', history.undo_label)
  const redoName = actionName('redo', history.redo_label)
  const blockedId = useId()
  const effectiveBlockedReason = conflict
    ? 'History changed in another session. Reload the current draft before editing, undoing, or redoing.'
    : blockedReason
  return (
    <div className="editor-history-wrap">
      <div className="editor-history-controls" role="group" aria-label="Edit history" aria-busy={busy}>
        <button
          type="button"
          disabled={!history.can_undo || busy || effectiveBlockedReason !== null}
          aria-label={undoName}
          aria-describedby={effectiveBlockedReason ? blockedId : undefined}
          title={effectiveBlockedReason ?? `${undoName} (${shortcutLabel('undo')})`}
          onClick={() => onMove('undo')}
        >
          <span aria-hidden="true">↶</span>
          <span>{working === 'undo' ? 'Undoing…' : 'Undo'}</span>
        </button>
        <button
          type="button"
          disabled={!history.can_redo || busy || effectiveBlockedReason !== null}
          aria-label={redoName}
          aria-describedby={effectiveBlockedReason ? blockedId : undefined}
          title={effectiveBlockedReason ?? `${redoName} (${shortcutLabel('redo')})`}
          onClick={() => onMove('redo')}
        >
          <span aria-hidden="true">↷</span>
          <span>{working === 'redo' ? 'Redoing…' : 'Redo'}</span>
        </button>
      </div>
      {effectiveBlockedReason ? (
        <span id={blockedId} className="visually-hidden">{effectiveBlockedReason}</span>
      ) : null}
      {error ? (
        <div className="editor-history-error" role="alert">
          <span>{error}</span>
          {retryable && !conflict ? <button type="button" disabled={busy} onClick={onRetry}>Retry</button> : null}
          <button type="button" disabled={busy} onClick={onResync}>
            {working === 'resync' ? 'Reloading…' : 'Reload current draft'}
          </button>
        </div>
      ) : null}
      <span className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </span>
    </div>
  )
}
