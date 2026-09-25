/// <reference types="node" />

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EditorShell, type EditorShellProps } from './EditorShell'

const baseProps: EditorShellProps = {
  project: {
    name: 'The Wager',
    assetName: 'wager.png',
    detail: '200 × 200 mm',
    status: 'Draft saved',
    statusTone: 'positive',
  },
  canvas: <div>Rendered artwork</div>,
  leftRail: <label>Minimum area<input aria-label="Minimum area" /></label>,
  rightRail: <div>Printability findings</div>,
  canvasToolbar: <button type="button">Fit canvas</button>,
  canvasFooter: <span>0.4 mm nozzle</span>,
  headerActions: <button type="button">Render preview</button>,
}

afterEach(cleanup)

describe('EditorShell', () => {
  it('renders project context and keyboard-accessible workspace landmarks', () => {
    render(<EditorShell {...baseProps} />)

    expect(screen.getByRole('region', { name: 'Image editor' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Canvas workspace' })).toHaveAttribute('tabindex', '-1')
    expect(screen.getByRole('complementary', { name: 'Controls' })).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: 'Inspection' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'The Wager' })).toBeInTheDocument()
    expect(screen.getByText('wager.png')).toBeInTheDocument()
    expect(screen.getByText('Rendered artwork')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Skip to canvas' })).toHaveAttribute(
      'href',
      expect.stringMatching(/^#i23-canvas-/),
    )
  })

  it('makes the editor controls inert while an exclusive mutation is in flight', () => {
    const { container } = render(<EditorShell {...baseProps} workspaceDisabled />)
    const workspace = container.querySelector<HTMLElement>('.i23-editor-workspace')

    expect(workspace).toHaveAttribute('inert')
    expect(workspace).toHaveAttribute('aria-busy', 'true')
  })

  it('constrains desktop grid tracks so each rail body owns its overflow', () => {
    const { container } = render(<EditorShell {...baseProps} />)
    const workspace = container.querySelector<HTMLElement>('.i23-editor-workspace')
    const leftRail = container.querySelector<HTMLElement>(
      '.i23-editor-rail[data-rail="left"]',
    )
    const leftBody = leftRail?.querySelector<HTMLElement>('.i23-editor-rail__body')

    expect(workspace).not.toBeNull()
    expect(leftRail).not.toBeNull()
    expect(leftBody).not.toBeNull()
    const css = readFileSync(resolve(process.cwd(), 'src/editor/editor-shell.css'), 'utf8')
    expect(css).toMatch(
      /\.i23-editor-workspace\s*\{[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\);/,
    )
    expect(css).toMatch(/\.i23-editor-rail\s*\{[^}]*min-height:\s*0;/)
    expect(css).toMatch(/\.i23-editor-rail__body\s*\{[^}]*overflow:\s*auto;/)
  })

  it('collapses and restores rails while keeping focus on the deterministic toggle', () => {
    const onLayoutChange = vi.fn()
    render(<EditorShell {...baseProps} onLayoutChange={onLayoutChange} />)

    const collapse = screen.getByRole('button', { name: 'Collapse left controls rail' })
    collapse.focus()
    fireEvent.click(collapse)

    expect(onLayoutChange).toHaveBeenLastCalledWith({
      left: { collapsed: true, width: 292 },
      right: { collapsed: false, width: 332 },
    })
    expect(screen.getByLabelText('Minimum area')).not.toBeVisible()
    const expand = screen.getByRole('button', { name: 'Expand left controls rail' })
    expect(expand).toHaveFocus()

    fireEvent.click(expand)
    expect(screen.getByLabelText('Minimum area')).toBeVisible()
  })

  it('resizes both rails with physical arrow direction, larger shift steps, and limits', () => {
    const onLayoutChange = vi.fn()
    render(<EditorShell {...baseProps} onLayoutChange={onLayoutChange} />)

    const left = screen.getByRole('separator', { name: 'Resize controls rail' })
    fireEvent.keyDown(left, { key: 'ArrowRight' })
    expect(left).toHaveAttribute('aria-valuenow', '300')

    const right = screen.getByRole('separator', { name: 'Resize inspection rail' })
    fireEvent.keyDown(right, { key: 'ArrowLeft', shiftKey: true })
    expect(right).toHaveAttribute('aria-valuenow', '356')

    fireEvent.keyDown(right, { key: 'End' })
    expect(right).toHaveAttribute('aria-valuenow', '480')
    fireEvent.keyDown(right, { key: 'Home' })
    expect(right).toHaveAttribute('aria-valuenow', '260')
    expect(onLayoutChange).toHaveBeenCalledTimes(4)
  })

  it('resizes a rail with pointer movement and keeps the result inside configured bounds', () => {
    const onLayoutChange = vi.fn()
    render(<EditorShell {...baseProps} onLayoutChange={onLayoutChange} />)

    const left = screen.getByRole('separator', { name: 'Resize controls rail' })
    fireEvent.pointerDown(left, { pointerId: 7, clientX: 100 })
    fireEvent.pointerMove(left, { pointerId: 7, clientX: 134 })
    fireEvent.pointerUp(left, { pointerId: 7, clientX: 134 })

    expect(left).toHaveAttribute('aria-valuenow', '326')
    expect(onLayoutChange).toHaveBeenLastCalledWith({
      left: { collapsed: false, width: 326 },
      right: { collapsed: false, width: 332 },
    })
  })

  it('renders explicit empty, loading, error, and non-destructive working states', () => {
    const { rerender } = render(
      <EditorShell
        {...baseProps}
        viewState={{ kind: 'empty', title: 'Choose an image', description: 'PNG, JPEG, or WebP.' }}
      />,
    )
    expect(screen.getByText('Choose an image')).toBeInTheDocument()
    expect(screen.queryByText('Rendered artwork')).not.toBeInTheDocument()

    rerender(
      <EditorShell
        {...baseProps}
        viewState={{ kind: 'loading', label: 'Opening project', description: 'Reading the draft.' }}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Opening project')

    rerender(
      <EditorShell
        {...baseProps}
        viewState={{ kind: 'error', title: 'Preview unavailable', description: 'Try again.' }}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Preview unavailable')

    rerender(
      <EditorShell
        {...baseProps}
        viewState={{ kind: 'working', label: 'Analyzing regions', progress: 0.35 }}
      />,
    )
    expect(screen.getByText('Rendered artwork')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Analyzing regions')
    expect(screen.getByRole('status')).toHaveTextContent('35%')
  })

  it('provides independent notification and dialog layers and an explicit stacked mode', () => {
    render(
      <EditorShell
        {...baseProps}
        compactMode="stacked"
        notificationSlot={<p role="status">Draft saved.</p>}
        dialogSlot={<div role="dialog" aria-label="Confirm replacement" />}
      />,
    )

    expect(screen.getByLabelText('Notifications')).toContainElement(screen.getByText('Draft saved.'))
    expect(screen.getByRole('dialog', { name: 'Confirm replacement' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Image editor' })).toHaveAttribute(
      'data-compact',
      'stacked',
    )
  })

  it('supports controlled layout ownership without silently mutating the supplied state', () => {
    const onLayoutChange = vi.fn()
    render(
      <EditorShell
        {...baseProps}
        layout={{
          left: { collapsed: false, width: 250 },
          right: { collapsed: true, width: 300 },
        }}
        onLayoutChange={onLayoutChange}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Collapse left controls rail' }))
    expect(onLayoutChange).toHaveBeenCalledWith({
      left: { collapsed: true, width: 250 },
      right: { collapsed: true, width: 300 },
    })
    expect(screen.getByRole('button', { name: 'Collapse left controls rail' })).toBeInTheDocument()
  })
})
