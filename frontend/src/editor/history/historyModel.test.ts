import { afterEach, describe, expect, it } from 'vitest'
import {
  historyShortcut,
  manualOperationLabel,
  paletteActionLabel,
  shortcutLabel,
} from './historyModel'

function shortcutFrom(
  target: Element,
  init: KeyboardEventInit,
  platform = 'Win32',
): 'undo' | 'redo' | null {
  let result: 'undo' | 'redo' | null = null
  target.addEventListener('keydown', (event) => {
    result = historyShortcut(event as KeyboardEvent, platform)
  }, { once: true })
  target.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, ...init }))
  return result
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('editor history model', () => {
  it('maps platform shortcuts without stealing native form history', () => {
    const canvas = document.createElement('div')
    const input = document.createElement('input')
    document.body.append(canvas, input)
    expect(shortcutFrom(canvas, { key: 'z', ctrlKey: true })).toBe('undo')
    expect(shortcutFrom(canvas, { key: 'Z', ctrlKey: true, shiftKey: true })).toBe('redo')
    expect(shortcutFrom(canvas, { key: 'y', ctrlKey: true })).toBe('redo')
    expect(shortcutFrom(canvas, { key: 'z', metaKey: true }, 'MacIntel')).toBe('undo')
    expect(shortcutFrom(input, { key: 'z', ctrlKey: true })).toBeNull()
    expect(shortcutLabel('redo', 'MacIntel')).toBe('⇧⌘Z')
  })

  it('does not mutate history during composition or any open dialog', () => {
    const canvas = document.createElement('div')
    document.body.append(canvas)
    expect(shortcutFrom(canvas, { key: 'z', ctrlKey: true, isComposing: true })).toBeNull()
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'alertdialog')
    dialog.setAttribute('aria-modal', 'false')
    document.body.append(dialog)
    expect(shortcutFrom(canvas, { key: 'z', ctrlKey: true })).toBeNull()
  })

  it('provides stable user-facing names for palette and manual commands', () => {
    expect(paletteActionLabel('auto-fit')).toBe('Auto-fit palette')
    expect(manualOperationLabel({ parameters: { command: { operation: 'recolor' } } }))
      .toBe('Recolor region')
  })
})
