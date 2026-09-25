import {
  useId,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from 'react'
import { EditorShellState } from './EditorShellState'
import {
  DEFAULT_EDITOR_SHELL_LIMITS,
  normalizeEditorShellLayout,
  updateEditorShellLayout,
  type EditorRailId,
  type EditorShellLayoutInput,
  type EditorShellLayoutLimits,
  type EditorShellLayoutState,
} from './layout'
import type { EditorProjectContext, EditorShellViewState } from './types'
import './editor-shell.css'

export type EditorShellProps = {
  project: EditorProjectContext
  canvas: ReactNode
  leftRail: ReactNode
  rightRail: ReactNode
  ariaLabel?: string
  canvasLabel?: string
  canvasId?: string
  showSkipLink?: boolean
  canvasToolbar?: ReactNode
  canvasFooter?: ReactNode
  headerActions?: ReactNode
  notificationSlot?: ReactNode
  dialogSlot?: ReactNode
  layout?: EditorShellLayoutState
  defaultLayout?: EditorShellLayoutInput
  layoutLimits?: EditorShellLayoutLimits
  onLayoutChange?: (layout: EditorShellLayoutState) => void
  onBack?: () => void
  leftRailLabel?: string
  rightRailLabel?: string
  viewState?: EditorShellViewState
  compactMode?: 'auto' | 'stacked' | 'desktop'
  workspaceDisabled?: boolean
  className?: string
}

type DragState = {
  pointerId: number
  rail: EditorRailId
  startWidth: number
  startX: number
}

const COLLAPSED_RAIL_WIDTH = 52
const KEYBOARD_RESIZE_STEP = 8

function railDirection(rail: EditorRailId) {
  return rail === 'left' ? 1 : -1
}

export function EditorShell({
  project,
  canvas,
  leftRail,
  rightRail,
  ariaLabel = 'Image editor',
  canvasLabel = 'Canvas workspace',
  canvasId: providedCanvasId,
  showSkipLink = true,
  canvasToolbar,
  canvasFooter,
  headerActions,
  notificationSlot,
  dialogSlot,
  layout: controlledLayout,
  defaultLayout,
  layoutLimits = DEFAULT_EDITOR_SHELL_LIMITS,
  onLayoutChange,
  onBack,
  leftRailLabel = 'Controls',
  rightRailLabel = 'Inspection',
  viewState = { kind: 'ready' },
  compactMode = 'auto',
  workspaceDisabled = false,
  className,
}: EditorShellProps) {
  const generatedId = useId().replaceAll(':', '')
  const [uncontrolledLayout, setUncontrolledLayout] = useState(() =>
    normalizeEditorShellLayout(defaultLayout, layoutLimits),
  )
  const resolvedLayout = normalizeEditorShellLayout(
    controlledLayout ?? uncontrolledLayout,
    layoutLimits,
  )
  const layoutRef = useRef(resolvedLayout)
  useEffect(() => {
    layoutRef.current = resolvedLayout
  }, [resolvedLayout])
  const dragRef = useRef<DragState | null>(null)
  const leftHeadingId = `i23-left-${generatedId}`
  const rightHeadingId = `i23-right-${generatedId}`
  const canvasId = providedCanvasId ?? `i23-canvas-${generatedId}`

  const changeLayout = (
    action: Parameters<typeof updateEditorShellLayout>[1],
  ) => {
    const next = updateEditorShellLayout(layoutRef.current, action, layoutLimits)
    layoutRef.current = next
    if (!controlledLayout) setUncontrolledLayout(next)
    onLayoutChange?.(next)
  }

  const beginResize = (rail: EditorRailId, event: PointerEvent<HTMLDivElement>) => {
    if (layoutRef.current[rail].collapsed) return
    dragRef.current = {
      pointerId: event.pointerId,
      rail,
      startWidth: layoutRef.current[rail].width,
      startX: event.clientX,
    }
    event.currentTarget.setPointerCapture?.(event.pointerId)
    event.currentTarget.dataset.dragging = 'true'
  }

  const continueResize = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const distance = (event.clientX - drag.startX) * railDirection(drag.rail)
    changeLayout({ type: 'resize', rail: drag.rail, width: drag.startWidth + distance })
  }

  const endResize = (event: PointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    delete event.currentTarget.dataset.dragging
  }

  const resizeWithKeyboard = (rail: EditorRailId, event: KeyboardEvent<HTMLDivElement>) => {
    const current = layoutRef.current[rail].width
    const direction = railDirection(rail)
    const step = event.shiftKey ? KEYBOARD_RESIZE_STEP * 3 : KEYBOARD_RESIZE_STEP
    let next: number | null = null

    if (event.key === 'ArrowLeft') next = current - step * direction
    if (event.key === 'ArrowRight') next = current + step * direction
    if (event.key === 'Home') next = layoutLimits[rail].min
    if (event.key === 'End') next = layoutLimits[rail].max
    if (next === null) return

    event.preventDefault()
    changeLayout({ type: 'resize', rail, width: next })
  }

  const leftTrack = resolvedLayout.left.collapsed
    ? COLLAPSED_RAIL_WIDTH
    : resolvedLayout.left.width
  const rightTrack = resolvedLayout.right.collapsed
    ? COLLAPSED_RAIL_WIDTH
    : resolvedLayout.right.width
  const style = {
    '--i23-editor-left-width': `${leftTrack}px`,
    '--i23-editor-right-width': `${rightTrack}px`,
  } as CSSProperties
  const rootClassName = ['i23-editor-shell', className].filter(Boolean).join(' ')

  const renderRail = (rail: EditorRailId, label: string, headingId: string, body: ReactNode) => {
    const state = resolvedLayout[rail]
    const sideLabel = rail === 'left' ? 'left' : 'right'
    return (
      <aside
        className="i23-editor-rail"
        data-collapsed={state.collapsed}
        data-rail={rail}
        aria-labelledby={headingId}
      >
        <div className="i23-editor-rail__header">
          <h2 id={headingId}>{label}</h2>
          <button
            className="i23-editor-rail__toggle"
            type="button"
            aria-expanded={!state.collapsed}
            aria-controls={`${headingId}-body`}
            aria-label={`${state.collapsed ? 'Expand' : 'Collapse'} ${sideLabel} ${label.toLowerCase()} rail`}
            title={`${state.collapsed ? 'Expand' : 'Collapse'} ${label}`}
            onClick={() => changeLayout({ type: 'toggle', rail })}
          >
            <span aria-hidden="true">{state.collapsed ? (rail === 'left' ? '›' : '‹') : '×'}</span>
          </button>
        </div>
        <div id={`${headingId}-body`} className="i23-editor-rail__body" hidden={state.collapsed}>
          {body}
        </div>
      </aside>
    )
  }

  const renderSeparator = (rail: EditorRailId, label: string) => {
    const state = resolvedLayout[rail]
    return (
      <>
        {/* WAI-ARIA separators become interactive widgets when focusable and value-backed. */}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <div
          className="i23-editor-resizer"
          data-rail={rail}
          hidden={state.collapsed}
          role="separator"
          aria-label={`Resize ${label.toLowerCase()} rail`}
          aria-orientation="vertical"
          aria-valuemin={layoutLimits[rail].min}
          aria-valuemax={layoutLimits[rail].max}
          aria-valuenow={state.width}
          tabIndex={0}
          onKeyDown={(event) => resizeWithKeyboard(rail, event)}
          onPointerDown={(event) => beginResize(rail, event)}
          onPointerMove={continueResize}
          onPointerUp={endResize}
          onPointerCancel={endResize}
        >
          <span aria-hidden="true" />
        </div>
      </>
    )
  }

  return (
    <section
      className={rootClassName}
      style={style}
      aria-label={ariaLabel}
      data-compact={compactMode}
    >
      {showSkipLink ? (
        <a className="i23-editor-skip-link" href={`#${canvasId}`}>
          Skip to canvas
        </a>
      ) : null}
      <header className="i23-editor-header">
        <div className="i23-editor-header__identity">
          {onBack ? (
            <button
              className="i23-editor-back"
              type="button"
              onClick={onBack}
              aria-label="Back to projects"
            >
              <span aria-hidden="true">←</span>
            </button>
          ) : null}
          <div className="i23-editor-project">
            <span className="i23-editor-project__eyebrow">{project.eyebrow ?? 'PLAyered'}</span>
            <div className="i23-editor-project__title-row">
              <h1>{project.name}</h1>
              {project.status ? (
                <span className="i23-editor-status" data-tone={project.statusTone ?? 'neutral'}>
                  {project.status}
                </span>
              ) : null}
            </div>
            {project.assetName || project.detail ? (
              <p>
                {project.assetName ? <strong>{project.assetName}</strong> : null}
                {project.assetName && project.detail ? <span aria-hidden="true"> · </span> : null}
                {project.detail}
              </p>
            ) : null}
          </div>
        </div>
        {headerActions ? <div className="i23-editor-header__actions">{headerActions}</div> : null}
      </header>

      {notificationSlot ? (
        <div className="i23-editor-notifications" aria-label="Notifications">
          {notificationSlot}
        </div>
      ) : null}

      <div
        className="i23-editor-workspace"
        inert={workspaceDisabled ? true : undefined}
        aria-busy={workspaceDisabled}
      >
        {renderRail('left', leftRailLabel, leftHeadingId, leftRail)}
        {renderSeparator('left', leftRailLabel)}
        <section
          id={canvasId}
          className="i23-editor-canvas"
          tabIndex={-1}
          aria-label={canvasLabel}
          aria-busy={viewState.kind === 'loading' || viewState.kind === 'working'}
        >
          {canvasToolbar ? <div className="i23-editor-canvas__toolbar">{canvasToolbar}</div> : null}
          <div className="i23-editor-canvas__viewport" data-state={viewState.kind}>
            {viewState.kind === 'ready' || viewState.kind === 'working' ? canvas : null}
            {viewState.kind !== 'ready' ? (
              <div className="i23-editor-canvas__state-layer">
                <EditorShellState state={viewState} />
              </div>
            ) : null}
          </div>
          {canvasFooter ? <footer className="i23-editor-canvas__footer">{canvasFooter}</footer> : null}
        </section>
        {renderSeparator('right', rightRailLabel)}
        {renderRail('right', rightRailLabel, rightHeadingId, rightRail)}
      </div>
      {dialogSlot ? (
        <div className="i23-editor-dialog-layer" aria-label="Editor dialogs">
          {dialogSlot}
        </div>
      ) : null}
    </section>
  )
}
