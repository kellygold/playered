import { StrictMode, useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { JobConfigV1 } from '../../contracts'
import type { PreviewJobTransport, PreviewRequest } from './PreviewJobCoordinator'
import { usePreviewJobCoordinator } from './usePreviewJobCoordinator'

const request: PreviewRequest = {
  projectId: 'project-1',
  config: {} as JobConfigV1,
  expectedDraftGeneration: 1,
}

afterEach(cleanup)

describe('usePreviewJobCoordinator', () => {
  it('remains usable after the React Strict Mode effect cleanup cycle', async () => {
    const start = vi.fn(async () => {
      throw new Error('Expected test transport failure.')
    })
    const transport: PreviewJobTransport = {
      start,
      fetchJob: async () => { throw new Error('not reached') },
      fetchResult: async () => { throw new Error('not reached') },
      cancel: async () => { throw new Error('not reached') },
    }

    function Harness() {
      const { state, start: startPreview } = usePreviewJobCoordinator({ transport })
      const [rejection, setRejection] = useState('none')
      return (
        <>
          <button
            type="button"
            onClick={() => {
              void startPreview(request).catch((error: unknown) => {
                setRejection(error instanceof Error ? error.message : String(error))
              })
            }}
          >
            Start
          </button>
          <output aria-label="Phase">{state.phase}</output>
          <output aria-label="Rejection">{rejection}</output>
        </>
      )
    }

    render(<StrictMode><Harness /></StrictMode>)
    await new Promise((resolve) => globalThis.setTimeout(resolve, 0))
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))

    await waitFor(() => expect(screen.getByLabelText('Phase')).toHaveTextContent('failed'))
    expect(start).toHaveBeenCalledOnce()
    expect(screen.getByLabelText('Rejection')).toHaveTextContent('none')
  })
})
