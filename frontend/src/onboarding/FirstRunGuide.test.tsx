import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FirstRunGuide } from './FirstRunGuide'

function props() {
  return {
    open: true,
    stage: 'processed' as const,
    sampleActive: true,
    previewWorking: false,
    outputWorking: false,
    canGenerate: true,
    canDownload: false,
    onClose: vi.fn(),
    onOpen: vi.fn(),
    onGenerate: vi.fn(),
    onDownload: vi.fn(),
  }
}

describe('FirstRunGuide', () => {
  it('explains the complete print workflow and exposes the contextual action', () => {
    const value = props()
    render(<FirstRunGuide {...value} />)

    expect(screen.getByRole('heading', { name: 'Image → validated 3MF' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'First-run workflow' })).toHaveTextContent('Source')
    expect(screen.getByText('Processed').closest('li')).toHaveAttribute('aria-current', 'step')
    expect(screen.getByText(/Tiny islands, narrow gaps/)).toBeInTheDocument()
    expect(screen.getByText('Palette and physical cleanup')).toBeInTheDocument()
    expect(screen.getByText('Before the physical print')).toBeInTheDocument()
    expect(screen.getByText('Saving, revisions, and backups')).toBeInTheDocument()
    expect(screen.getByText('Current limits')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Build and validate 3MF' }))
    expect(value.onGenerate).toHaveBeenCalledOnce()
  })

  it('collapses to a persistent workflow-guide trigger', () => {
    const value = props()
    render(<FirstRunGuide {...value} open={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Workflow guide' }))
    expect(value.onOpen).toHaveBeenCalledOnce()
  })

  it('distinguishes slicer validation from a physical-print guarantee', () => {
    render(<FirstRunGuide {...props()} stage="validated" canGenerate={false} canDownload />)
    expect(screen.getByText(/does not guarantee bed adhesion/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download verified 3MF' })).toBeInTheDocument()
  })
})
