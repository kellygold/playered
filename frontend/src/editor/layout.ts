export type EditorRailId = 'left' | 'right'

export type EditorRailLayout = {
  collapsed: boolean
  width: number
}

export type EditorShellLayoutState = {
  left: EditorRailLayout
  right: EditorRailLayout
}

export type EditorShellLayoutInput = {
  left?: Partial<EditorRailLayout>
  right?: Partial<EditorRailLayout>
}

export type EditorRailLimits = {
  min: number
  max: number
}

export type EditorShellLayoutLimits = {
  left: EditorRailLimits
  right: EditorRailLimits
}

export type EditorShellLayoutAction =
  | { type: 'toggle'; rail: EditorRailId }
  | { type: 'collapse'; rail: EditorRailId; collapsed: boolean }
  | { type: 'resize'; rail: EditorRailId; width: number }
  | { type: 'reset' }

export const DEFAULT_EDITOR_SHELL_LAYOUT: EditorShellLayoutState = {
  left: { collapsed: false, width: 292 },
  right: { collapsed: false, width: 332 },
}

export const DEFAULT_EDITOR_SHELL_LIMITS: EditorShellLayoutLimits = {
  left: { min: 232, max: 440 },
  right: { min: 260, max: 480 },
}

function clamp(value: number, limits: EditorRailLimits, fallback: number) {
  const finiteValue = Number.isFinite(value) ? value : fallback
  return Math.min(limits.max, Math.max(limits.min, Math.round(finiteValue)))
}

export function normalizeEditorShellLayout(
  value: EditorShellLayoutInput | undefined,
  limits: EditorShellLayoutLimits = DEFAULT_EDITOR_SHELL_LIMITS,
): EditorShellLayoutState {
  return {
    left: {
      collapsed: value?.left?.collapsed ?? DEFAULT_EDITOR_SHELL_LAYOUT.left.collapsed,
      width: clamp(
        value?.left?.width ?? DEFAULT_EDITOR_SHELL_LAYOUT.left.width,
        limits.left,
        DEFAULT_EDITOR_SHELL_LAYOUT.left.width,
      ),
    },
    right: {
      collapsed: value?.right?.collapsed ?? DEFAULT_EDITOR_SHELL_LAYOUT.right.collapsed,
      width: clamp(
        value?.right?.width ?? DEFAULT_EDITOR_SHELL_LAYOUT.right.width,
        limits.right,
        DEFAULT_EDITOR_SHELL_LAYOUT.right.width,
      ),
    },
  }
}

export function updateEditorShellLayout(
  state: EditorShellLayoutState,
  action: EditorShellLayoutAction,
  limits: EditorShellLayoutLimits = DEFAULT_EDITOR_SHELL_LIMITS,
): EditorShellLayoutState {
  const current = normalizeEditorShellLayout(state, limits)

  if (action.type === 'reset') return normalizeEditorShellLayout(undefined, limits)

  const existing = current[action.rail]
  const nextRail =
    action.type === 'resize'
      ? { ...existing, width: clamp(action.width, limits[action.rail], existing.width) }
      : action.type === 'collapse'
        ? { ...existing, collapsed: action.collapsed }
        : { ...existing, collapsed: !existing.collapsed }

  return { ...current, [action.rail]: nextRail }
}
