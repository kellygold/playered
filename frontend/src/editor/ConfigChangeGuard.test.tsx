import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConfigChangeGuard } from './ConfigChangeGuard'

afterEach(cleanup)

describe('ConfigChangeGuard', () => {
  it('explains the atomic outcome, traps focus, and cancels with Escape', () => {
    const onCancel = vi.fn()
    const trigger = document.createElement('button')
    trigger.textContent = 'Setting trigger'
    document.body.append(trigger)
    trigger.focus()

    render(
      <main className="app-shell">
        <ConfigChangeGuard
          operationCount={2}
          retainedOperationCount={1}
          onCancel={onCancel}
          onConfirm={vi.fn()}
        />
      </main>,
    )

    const dialog = screen.getByRole('alertdialog', {
      name: 'Change settings and clear manual edits?',
    })
    const cancel = screen.getByRole('button', { name: 'Keep current settings' })
    const confirm = screen.getByRole('button', { name: 'Clear 2 manual edits and apply' })
    expect(document.querySelector('.app-shell')).toHaveAttribute('inert')
    expect(cancel).toHaveFocus()
    expect(dialog).toHaveTextContent('2 manual edits will be cleared')
    expect(dialog).toHaveTextContent('1 compatible palette step stays in history')

    confirm.focus()
    fireEvent.keyDown(confirm, { key: 'Tab' })
    expect(cancel).toHaveFocus()
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(confirm).toHaveFocus()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledOnce()
    trigger.remove()
  })

  it('suppresses duplicate confirmation activation synchronously', () => {
    const onConfirm = vi.fn()
    render(
      <main className="app-shell">
        <ConfigChangeGuard
          operationCount={1}
          retainedOperationCount={0}
          onCancel={vi.fn()}
          onConfirm={onConfirm}
        />
      </main>,
    )
    const confirm = screen.getByRole('button', { name: 'Clear 1 manual edit and apply' })
    fireEvent.click(confirm)
    fireEvent.click(confirm)
    expect(onConfirm).toHaveBeenCalledOnce()
  })
})
