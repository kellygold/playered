import { describe, expect, it } from 'vitest'
import {
  DEFAULT_EDITOR_SHELL_LAYOUT,
  normalizeEditorShellLayout,
  updateEditorShellLayout,
} from './layout'

describe('editor shell layout model', () => {
  it('normalizes partial, invalid, and out-of-range persisted values deterministically', () => {
    expect(
      normalizeEditorShellLayout({
        left: { collapsed: true, width: Number.NaN },
        right: { collapsed: false, width: 900 },
      }),
    ).toEqual({
      left: { collapsed: true, width: DEFAULT_EDITOR_SHELL_LAYOUT.left.width },
      right: { collapsed: false, width: 480 },
    })
    expect(normalizeEditorShellLayout({ left: { collapsed: true } }).left).toEqual({
      collapsed: true,
      width: DEFAULT_EDITOR_SHELL_LAYOUT.left.width,
    })
  })

  it('applies serializable toggle, collapse, resize, and reset actions without mutation', () => {
    const initial = normalizeEditorShellLayout(undefined)
    const toggled = updateEditorShellLayout(initial, { type: 'toggle', rail: 'left' })
    const resized = updateEditorShellLayout(toggled, {
      type: 'resize',
      rail: 'right',
      width: 100,
    })
    const expanded = updateEditorShellLayout(resized, {
      type: 'collapse',
      rail: 'left',
      collapsed: false,
    })

    expect(initial).toEqual(DEFAULT_EDITOR_SHELL_LAYOUT)
    expect(toggled.left.collapsed).toBe(true)
    expect(resized.right.width).toBe(260)
    expect(expanded.left.collapsed).toBe(false)
    expect(updateEditorShellLayout(expanded, { type: 'reset' })).toEqual(
      DEFAULT_EDITOR_SHELL_LAYOUT,
    )
  })
})
