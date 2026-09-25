import { useEffect, useMemo, useSyncExternalStore } from 'react'
import {
  OutputJobCoordinator,
  type OutputJobCoordinatorOptions,
} from './OutputJobCoordinator'

const pendingDisposals = new WeakMap<OutputJobCoordinator, ReturnType<typeof setTimeout>>()

export function useOutputJobCoordinator(options: OutputJobCoordinatorOptions = {}) {
  const { transport, pollIntervalMs, wait } = options
  const coordinator = useMemo(
    () => new OutputJobCoordinator({ transport, pollIntervalMs, wait }),
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
    reset: coordinator.reset,
  }
}
