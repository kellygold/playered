import type { EditorShellViewState } from './types'

type EditorShellStateProps = {
  state: Exclude<EditorShellViewState, { kind: 'ready' }>
}

export function EditorShellState({ state }: EditorShellStateProps) {
  if (state.kind === 'loading' || state.kind === 'working') {
    const progress = state.kind === 'working' ? state.progress : undefined
    return (
      <div className="i23-editor-state" data-kind={state.kind} role="status" aria-live="polite">
        <span className="i23-editor-state__spinner" aria-hidden="true" />
        <div>
          <strong>{state.label ?? 'Preparing workspace'}</strong>
          {state.description ? <p>{state.description}</p> : null}
          {progress !== undefined ? (
            <div className="i23-editor-state__progress">
              <progress
                max="1"
                value={Math.min(1, Math.max(0, progress))}
                aria-label={`${state.label} progress`}
              />
              <span>{Math.round(Math.min(1, Math.max(0, progress)) * 100)}%</span>
            </div>
          ) : null}
          {state.kind === 'working' && state.action ? (
            <div className="i23-editor-state__action">{state.action}</div>
          ) : null}
        </div>
      </div>
    )
  }

  if (state.kind === 'error') {
    return (
      <div className="i23-editor-state" data-kind="error" role="alert">
        <span className="i23-editor-state__icon" aria-hidden="true">
          !
        </span>
        <div>
          <strong>{state.title}</strong>
          <p>{state.description}</p>
          {state.action ? <div className="i23-editor-state__action">{state.action}</div> : null}
        </div>
      </div>
    )
  }

  return (
    <div className="i23-editor-state" data-kind="empty">
      <span className="i23-editor-state__icon" aria-hidden="true">
        {state.icon ?? '◇'}
      </span>
      <div>
        <strong>{state.title}</strong>
        {state.description ? <p>{state.description}</p> : null}
        {state.action ? <div className="i23-editor-state__action">{state.action}</div> : null}
      </div>
    </div>
  )
}
