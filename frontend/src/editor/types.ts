import type { ReactNode } from 'react'

export type EditorShellTone = 'neutral' | 'positive' | 'warning' | 'danger'

export type EditorProjectContext = {
  name: string
  assetName?: string
  detail?: string
  eyebrow?: string
  status?: string
  statusTone?: EditorShellTone
}

export type EditorShellViewState =
  | { kind: 'ready' }
  | {
      kind: 'empty'
      title: string
      description?: string
      icon?: ReactNode
      action?: ReactNode
    }
  | {
      kind: 'loading'
      label?: string
      description?: string
    }
  | {
      kind: 'working'
      label: string
      description?: string
      progress?: number
      action?: ReactNode
    }
  | {
      kind: 'error'
      title: string
      description: string
      action?: ReactNode
    }
