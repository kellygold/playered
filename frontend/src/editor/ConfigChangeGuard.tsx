import { useEffect, useRef, type KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import './ConfigChangeGuard.css'

type ConfigChangeGuardProps = {
  operationCount: number
  retainedOperationCount: number
  onCancel: () => void
  onConfirm: () => void
}

const FOCUSABLE = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function operationLabel(count: number): string {
  return `${count} manual ${count === 1 ? 'edit' : 'edits'}`
}

export function ConfigChangeGuard({
  operationCount,
  retainedOperationCount,
  onCancel,
  onConfirm,
}: ConfigChangeGuardProps) {
  const dialogRef = useRef<HTMLElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const actionLockRef = useRef(false)
  const restoreFocusRef = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  )

  useEffect(() => {
    const app = document.querySelector<HTMLElement>('.app-shell')
    const ownedInert = app !== null && !app.hasAttribute('inert')
    const restoreFocus = restoreFocusRef.current
    if (ownedInert) app.setAttribute('inert', '')
    cancelRef.current?.focus()
    return () => {
      if (ownedInert) app?.removeAttribute('inert')
      if (restoreFocus?.isConnected && !restoreFocus.closest('[inert]')) restoreFocus.focus()
    }
  }, [])

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      if (!actionLockRef.current) onCancel()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
    )
    if (focusable.length === 0) {
      event.preventDefault()
      dialogRef.current?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable.at(-1) ?? first
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  const confirm = () => {
    if (actionLockRef.current) return
    actionLockRef.current = true
    onConfirm()
  }

  return createPortal(
    <div className="config-change-guard-backdrop" role="presentation">
      {/* This is a focus-trapped alertdialog; the key handler implements its modal keyboard contract. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <section
        ref={dialogRef}
        className="config-change-guard"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="config-change-guard-heading"
        aria-describedby="config-change-guard-description config-change-guard-outcome"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <span className="config-change-guard-icon" aria-hidden="true">⌁</span>
        <span className="eyebrow">Manual edit safety</span>
        <h2 id="config-change-guard-heading">Change settings and clear manual edits?</h2>
        <p id="config-change-guard-description">
          {operationLabel(operationCount)} {operationCount === 1 ? 'is' : 'are'} bound to the
          current rendered configuration. Changing crop, canvas, cleanup, or palette settings can
          change region identities, so those selectors cannot be carried forward safely.
        </p>
        <div className="config-change-guard-impact" id="config-change-guard-outcome">
          <strong>{operationLabel(operationCount)} will be cleared</strong>
          <span>Your requested settings will be saved in the same draft update.</span>
          {retainedOperationCount > 0 ? (
            <span>{retainedOperationCount} compatible palette {retainedOperationCount === 1 ? 'step stays' : 'steps stay'} in history.</span>
          ) : null}
        </div>
        <p className="config-change-guard-note">
          Nothing changes until you confirm. Manual edits are never rebound to different regions.
        </p>
        <div className="config-change-guard-actions">
          <button
            ref={cancelRef}
            className="button button-secondary"
            type="button"
            onClick={onCancel}
          >
            Keep current settings
          </button>
          <button className="button button-primary" type="button" onClick={confirm}>
            Clear {operationLabel(operationCount)} and apply
          </button>
        </div>
      </section>
    </div>,
    document.body,
  )
}
