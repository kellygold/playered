import { useEffect, useMemo, useSyncExternalStore } from 'react'
import {
  PreviewJobCoordinator,
  type PreviewJobCoordinatorOptions,
} from './PreviewJobCoordinator'

const pendingDisposals = new WeakMap<PreviewJobCoordinator, ReturnType<typeof setTimeout>>()

export function usePreviewJobCoordinator(options: PreviewJobCoordinatorOptions = {}) {
  const { transport, pollIntervalMs, wait } = options
  const coordinator = useMemo(
    () => new PreviewJobCoordinator({ transport, pollIntervalMs, wait }),
    [pollIntervalMs, transport, wait],
  )
  const state = useSyncExternalStore(
    coordinator.subscribe,
    coordinator.getSnapshot,
    coordinator.getSnapshot,
  )

  useEffect(() => {
    const pending = pendingDisposals.get(coordinator)
    if (pending !== undefined) {
      globalThis.clearTimeout(pending)
      pendingDisposals.delete(coordinator)
    }
    return () => {
      const disposal = globalThis.setTimeout(() => {
        if (pendingDisposals.get(coordinator) !== disposal) return
        pendingDisposals.delete(coordinator)
        coordinator.dispose()
      }, 0)
      pendingDisposals.set(coordinator, disposal)
    }
  }, [coordinator])

  return {
    state,
    start: coordinator.start,
    cancel: coordinator.cancel,
    retry: coordinator.retry,
    markStale: coordinator.markStale,
    restore: coordinator.restore,
  }
}
