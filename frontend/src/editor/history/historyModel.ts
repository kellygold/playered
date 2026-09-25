import type { EditorHistoryCommandType } from '../../contracts'

export type EditorChangeIntent = {
  commandType: EditorHistoryCommandType
  label: string
}

export function historyRequestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const value = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`
}

export function manualOperationLabel(operation: {
  parameters: Record<string, unknown>
}): string {
  const command = operation.parameters.command
  const action = command && typeof command === 'object' && 'operation' in command
    ? String(command.operation)
    : 'manual'
  const labels: Record<string, string> = {
    protect: 'Protect region',
    merge: 'Merge region',
    delete: 'Delete region',
    fill: 'Fill hole',
    recolor: 'Recolor region',
    thicken: 'Thicken region',
  }
  return labels[action] ?? 'Apply manual edit'
}

export function paletteActionLabel(action: string): string {
  const labels: Record<string, string> = {
    'auto-fit': 'Auto-fit palette',
    lock: 'Change palette lock',
    rename: 'Rename palette color',
    reorder: 'Reorder palette colors',
    replace: 'Change palette color',
    sample: 'Sample palette color',
    'select-filament': 'Map palette filament',
  }
  return labels[action] ?? 'Change palette'
}

export function isNativeHistoryTarget(event: KeyboardEvent): boolean {
  if (event.isComposing || event.defaultPrevented) return true
  if (document.querySelector('[role="dialog"], [role="alertdialog"]')) {
    return true
  }
  return event.composedPath().some((target) =>
    target instanceof Element &&
      (target.matches('input, textarea, select, [contenteditable="true"]') ||
        target.closest('[data-native-history="true"]') !== null),
  )
}

export function historyShortcut(
  event: KeyboardEvent,
  platform = navigator.platform,
): 'undo' | 'redo' | null {
  if (isNativeHistoryTarget(event) || event.altKey) return null
  const mac = /Mac|iPhone|iPad|iPod/i.test(platform)
  const modifier = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey
  if (!modifier) return null
  const key = event.key.toLowerCase()
  if (key === 'z') return event.shiftKey ? 'redo' : 'undo'
  if (!mac && key === 'y' && !event.shiftKey) return 'redo'
  return null
}

export function shortcutLabel(direction: 'undo' | 'redo', platform = navigator.platform): string {
  const mac = /Mac|iPhone|iPad|iPod/i.test(platform)
  if (direction === 'undo') return mac ? '⌘Z' : 'Ctrl+Z'
  return mac ? '⇧⌘Z' : 'Ctrl+Shift+Z'
}
