import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ImportPanel } from './ImportPanel'

afterEach(cleanup)

describe('ImportPanel', () => {
  it('offers the bundled sample and in-app workflow before requiring a source image', () => {
    const onTrySample = vi.fn()
    const onOpenGuide = vi.fn()
    render(
      <ImportPanel
        engineReady
        samplePending={false}
        onChoose={vi.fn()}
        onTrySample={onTrySample}
        onOpenGuide={onOpenGuide}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Try guided sample' }))
    fireEvent.click(screen.getByRole('button', { name: 'Read the workflow' }))
    expect(onTrySample).toHaveBeenCalledOnce()
    expect(onOpenGuide).toHaveBeenCalledOnce()
    expect(screen.getByText(/source → processed proof → geometry → slicer validation/)).toHaveTextContent(
      'broad features designed around a 0.4 mm nozzle',
    )
  })

  it('does not start the sample until the local engine is ready', () => {
    render(
      <ImportPanel
        engineReady={false}
        samplePending={false}
        onChoose={vi.fn()}
        onTrySample={vi.fn()}
        onOpenGuide={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: 'Try guided sample' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('local engine is offline')
  })
})
