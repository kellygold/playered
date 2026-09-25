import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiRequestError } from '../../api'
import type { EditorHistorySummary } from '../../contracts'
import { historyRequestId, historyShortcut } from './historyModel'
import type { HistoryDirection, HistoryWorking } from './HistoryControls'

type UseEditorHistoryInput = {
  projectId: string
  history: EditorHistorySummary
  onMove: (direction: HistoryDirection, requestId: string) => Promise<void>
  onResync: () => Promise<void>
  blocked?: boolean
}

type HistoryStatus = {
  sessionKey: string
  working: HistoryWorking | null
  error: string | null
  conflict: boolean
  retryable: boolean
  announcement: string
}

function emptyStatus(sessionKey: string): HistoryStatus {
  return {
    sessionKey,
    working: null,
    error: null,
    conflict: false,
    retryable: false,
    announcement: '',
  }
}

function historyError(error: unknown): string {
  return error instanceof Error ? error.message : 'Edit history could not be changed.'
}

function historyErrorRetryable(error: unknown): boolean {
  if (!(error instanceof ApiRequestError)) return true
  if (error.status === 409) return false
  return error.problem?.error.retryable ?? error.status >= 500
}

export function useEditorHistory({
  projectId,
  history,
  onMove,
  onResync,
  blocked = false,
}: UseEditorHistoryInput) {
  const sessionKey = `${projectId}:${history.lineage_id}`
  const [status, setStatus] = useState<HistoryStatus>(() => emptyStatus(sessionKey))
  const lockRef = useRef(false)
  const conflictRef = useRef(false)
  const retryRef = useRef<{ direction: HistoryDirection; requestId: string } | null>(null)
  const sessionRef = useRef(sessionKey)
  const historyRef = useRef(history)
  const visibleStatus = status.sessionKey === sessionKey ? status : emptyStatus(sessionKey)

  useEffect(() => {
    historyRef.current = history
  }, [history])

  useEffect(() => {
    sessionRef.current = sessionKey
    lockRef.current = false
    conflictRef.current = false
    retryRef.current = null
  }, [sessionKey])

  const patchStatus = useCallback((patch: Partial<HistoryStatus>) => {
    setStatus((current) => ({
      ...(current.sessionKey === sessionKey ? current : emptyStatus(sessionKey)),
      ...patch,
      sessionKey,
    }))
  }, [sessionKey])

  const move = useCallback(async (
    direction: HistoryDirection,
    requestId = historyRequestId(),
  ) => {
    if (lockRef.current || conflictRef.current || blocked) return
    const current = historyRef.current
    if (direction === 'undo' ? !current.can_undo : !current.can_redo) return
    const label = direction === 'undo' ? current.undo_label : current.redo_label
    const session = sessionKey
    lockRef.current = true
    retryRef.current = { direction, requestId }
    patchStatus({
      working: direction,
      error: null,
      conflict: false,
      retryable: false,
      announcement: '',
    })
    try {
      await onMove(direction, requestId)
      if (sessionRef.current !== session) return
      retryRef.current = null
      patchStatus({
        announcement: `${direction === 'undo' ? 'Undid' : 'Redid'} ${label ?? 'edit'}.`,
      })
    } catch (caught) {
      if (sessionRef.current !== session || (caught as Error).name === 'AbortError') return
      const conflict = caught instanceof ApiRequestError && caught.status === 409
      const retryable = !conflict && historyErrorRetryable(caught)
      conflictRef.current = conflict
      if (!retryable) retryRef.current = null
      patchStatus({
        error: historyError(caught),
        conflict,
        retryable,
      })
    } finally {
      if (sessionRef.current === session) {
        lockRef.current = false
        patchStatus({ working: null })
      }
    }
  }, [blocked, onMove, patchStatus, sessionKey])

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      const direction = historyShortcut(event)
      if (!direction || blocked) return
      const current = historyRef.current
      if (direction === 'undo' ? !current.can_undo : !current.can_redo) return
      event.preventDefault()
      void move(direction)
    }
    window.addEventListener('keydown', keydown)
    return () => window.removeEventListener('keydown', keydown)
  }, [blocked, move])

  const resync = useCallback(async () => {
    if (lockRef.current) return
    const session = sessionKey
    lockRef.current = true
    patchStatus({ working: 'resync', retryable: false, announcement: '' })
    try {
      await onResync()
      if (sessionRef.current !== session) return
      retryRef.current = null
      conflictRef.current = false
      patchStatus({
        error: null,
        conflict: false,
        announcement: 'Reloaded the current saved draft.',
      })
    } catch (caught) {
      if (sessionRef.current !== session || (caught as Error).name === 'AbortError') return
      // Resync has no idempotent history request to replay. Keep the explicit
      // reload action available instead of exposing a dead "Retry" control.
      patchStatus({ error: historyError(caught), retryable: false })
    } finally {
      if (sessionRef.current === session) {
        lockRef.current = false
        patchStatus({ working: null })
      }
    }
  }, [onResync, patchStatus, sessionKey])

  return {
    working: visibleStatus.working,
    error: visibleStatus.error,
    conflict: visibleStatus.conflict,
    retryable: visibleStatus.retryable,
    announcement: visibleStatus.announcement,
    move,
    retry: () => {
      if (retryRef.current) void move(retryRef.current.direction, retryRef.current.requestId)
    },
    resync: () => void resync(),
  }
}
