/// <reference types="node" />

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { OutputReadiness } from './OutputReadiness'
import {
  outputReadiness,
  previewOnlyOutputReadiness,
  type OutputReadinessModel,
} from './outputReadinessModel'
import type { OutputCoordinatorState } from './OutputJobCoordinator'

afterEach(cleanup)

describe('OutputReadiness', () => {
  it('makes geometry generation available after a current preview', () => {
    const onAction = vi.fn()
    render(<OutputReadiness model={previewOnlyOutputReadiness('current')} onAction={onAction} />)

    expect(screen.getByRole('heading', { name: 'Preview ready to build a 3MF' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Output stages' })).toBeInTheDocument()
    for (const stage of ['Preview', 'Geometry', '3MF package', 'Slicer validation', 'Download']) {
      expect(within(screen.getByRole('list', { name: 'Output stages' })).getByText(stage)).toBeInTheDocument()
    }
    expect(screen.getByText('Build printable regions from this exact preview.')).not.toBeVisible()
    fireEvent.click(screen.getByText('Build details'))
    expect(screen.getByText('Build printable regions from this exact preview.')).toBeVisible()
    expect(screen.getByText('Waiting for a packaged 3MF.')).toBeVisible()
    expect(screen.getByText('No file yet')).toBeVisible()
    expect(screen.queryByRole('button', { name: /download/i })).not.toBeInTheDocument()
    const nextAction = screen.getByRole('button', { name: 'Build 3MF' })
    expect(nextAction).not.toHaveAttribute('aria-disabled')
    fireEvent.click(nextAction)
    expect(onAction).toHaveBeenCalledWith('generate-output')
    expect(within(screen.getByRole('list', { name: 'Output stages' })).getByText('Geometry').closest('li')).toHaveAttribute('aria-current', 'step')
  })

  it('makes preview rendering the prominent next action when no preview exists', () => {
    const onAction = vi.fn()
    render(
      <OutputReadiness
        model={previewOnlyOutputReadiness('unavailable')}
        onAction={onAction}
      />,
    )

    const action = screen.getByRole('button', { name: 'Render preview' })
    fireEvent.click(action)
    expect(onAction).toHaveBeenCalledWith('render-preview')
    expect(within(screen.getByRole('list', { name: 'Output stages' })).getByText('Preview').closest('li')).toHaveAttribute('aria-current', 'step')
  })

  it('keeps disabled next actions focusable and exposes their visible reason', () => {
    const onAction = vi.fn()
    render(
      <OutputReadiness
        model={previewOnlyOutputReadiness('stale', 'Resolve the draft conflict before rendering.')}
        onAction={onAction}
      />,
    )

    const action = screen.getByRole('button', { name: 'Update preview' })
    expect(action).toHaveAttribute('aria-disabled', 'true')
    expect(action).toHaveAccessibleDescription('Resolve the draft conflict before rendering.')
    expect(screen.getByText('Resolve the draft conflict before rendering.')).toBeVisible()
    action.focus()
    expect(action).toHaveFocus()
    fireEvent.click(action)
    expect(onAction).not.toHaveBeenCalled()
  })

  it('accepts future completed output stages and a real download action', () => {
    const onAction = vi.fn()
    const model: OutputReadinessModel = {
      headline: 'Validated package ready',
      summary: 'All output checks passed.',
      stages: [
        { id: 'preview', label: 'Preview', state: 'complete', status: 'Complete', detail: 'Current.' },
        { id: 'geometry', label: 'Geometry', state: 'complete', status: 'Complete', detail: 'Generated.' },
        { id: 'package', label: '3MF package', state: 'complete', status: 'Complete', detail: 'Packaged.' },
        { id: 'slicer', label: 'Slicer validation', state: 'complete', status: 'Complete', detail: 'Validated.' },
        { id: 'download', label: 'Download', state: 'available', status: 'Ready', detail: 'File is ready.', current: true },
      ],
      primaryAction: { id: 'download-output', label: 'Download 3MF' },
    }
    render(<OutputReadiness model={model} onAction={onAction} />)

    fireEvent.click(screen.getByRole('button', { name: 'Download 3MF' }))
    expect(onAction).toHaveBeenCalledWith('download-output')
  })

  it('keeps physical-print limitations visible beside a successful download', () => {
    const state: OutputCoordinatorState = {
      projectId: 'project-1',
      phase: 'succeeded',
      requestSequence: 2,
      activeStep: null,
      geometryJob: null,
      geometryResult: null,
      exportJob: null,
      result: { package: { id: 'package-1' } } as OutputCoordinatorState['result'],
      resultFreshness: 'current',
      failure: null,
      lastRequest: null,
      announcement: 'Validated 3MF ready.',
    }

    render(<OutputReadiness model={outputReadiness('current', state)} />)

    expect(screen.getByRole('heading', { name: 'Validated 3MF ready' })).toBeVisible()
    expect(screen.getByText(/This is structural validation, not a physical print/)).toBeVisible()
    expect(screen.getByText(/adhesion, color order, surface finish, and tiny-feature survival are not verified/)).toBeVisible()
    fireEvent.click(screen.getByText('Build details'))
    expect(screen.getByText(/no physical print was performed/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Download 3MF' })).toBeVisible()
  })

  it.each([
    ['geometry', 'Generating printable geometry', 'Building printable regions from the exact preview.'],
    ['export', 'Building and validating the 3MF', 'Writing the Bambu Studio project package.'],
  ] as const)('keeps the %s stage current while cancellation is pending', (activeStep, heading, detail) => {
    const state: OutputCoordinatorState = {
      projectId: 'project-1',
      phase: 'canceling',
      requestSequence: 2,
      activeStep,
      geometryJob: null,
      geometryResult: null,
      exportJob: null,
      result: null,
      resultFreshness: 'none',
      failure: null,
      lastRequest: null,
      announcement: 'Canceling 3MF generation.',
    }

    render(<OutputReadiness model={outputReadiness('current', state)} />)

    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    fireEvent.click(screen.getByText('Build details'))
    expect(screen.getByText(detail)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Canceling…' })).toHaveAttribute('aria-disabled', 'true')
  })

  it('reflows stage cards and actions instead of clipping compact viewports', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/editor/output/OutputReadiness.css'), 'utf8')

    expect(css).toMatch(/grid-template-columns:\s*repeat\(5, minmax\(0, 1fr\)\)/)
    expect(css).toMatch(/@media \(max-width: 620px\)[\s\S]*?grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/)
    expect(css).toMatch(/@media \(max-width: 400px\), \(max-height: 560px\)[\s\S]*?grid-template-columns:\s*1fr/)
    expect(css).toMatch(/overflow-wrap:\s*anywhere/)
  })
})
