import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RevisionResource, RevisionSummary } from '../../contracts'
import { RevisionComparison } from './RevisionComparison'

function revision(id: string, label: string, width: number, parent: string | null): RevisionResource {
  return {
    id, label, parent_revision_id: parent,
    config: { canvas: { width_mm: width, height_mm: 140 }, palette: { colors: [] } },
    operations: [],
  } as unknown as RevisionResource
}

const parent = revision('revision_parent', 'First proof', 200, null)
const selected = revision('revision_selected', 'Print ready', 210, parent.id)
const other = revision('revision_other', 'Alternate cleanup', 220, parent.id)
const summaries = [parent, selected, other].map((item) => ({
  id: item.id, label: item.label, parent_revision_id: item.parent_revision_id,
}) as RevisionSummary)

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RevisionComparison', () => {
  it('defaults to the immutable parent and exposes exact setting deltas', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      expect(String(input)).toContain(parent.id)
      return response(parent)
    })
    render(<RevisionComparison projectId="project_1" revision={selected} revisions={summaries} />)

    expect(screen.getByRole('combobox', { name: 'Comparison revision' })).toHaveValue(parent.id)
    expect(await screen.findByText('canvas · width mm')).toBeInTheDocument()
    expect(screen.getByText('200')).toBeInTheDocument()
    expect(screen.getByText('210')).toBeInTheDocument()
  })

  it('switches comparison targets and recovers a failed exact revision request', async () => {
    let parentAttempts = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith(parent.id)) {
        parentAttempts += 1
        if (parentAttempts === 1) return response({ error: { message: 'Comparison unavailable.' } }, 503)
        return response(parent)
      }
      if (url.endsWith(other.id)) return response(other)
      throw new Error(`Unexpected fetch: ${url}`)
    })
    render(<RevisionComparison projectId="project_1" revision={selected} revisions={summaries} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Comparison unavailable.')
    fireEvent.click(screen.getByRole('button', { name: 'Retry comparison' }))
    expect(await screen.findByText('canvas · width mm')).toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox', { name: 'Comparison revision' }), {
      target: { value: other.id },
    })
    await waitFor(() => expect(screen.getByText('220')).toBeInTheDocument())
    expect(screen.getByText('210')).toBeInTheDocument()
  })
})
