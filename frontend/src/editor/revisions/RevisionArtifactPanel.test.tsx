import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ArtifactResource } from '../../contracts'
import { RevisionArtifactPanel } from './RevisionArtifactPanel'

function artifact(overrides: Partial<ArtifactResource> = {}): ArtifactResource {
  return {
    id: 'artifact_1', job_id: null, revision_id: 'revision_1', kind: 'preview-image',
    sha256: 'a'.repeat(64), derivation_key: 'preview:v1', media_type: 'image/png',
    byte_size: 2048, metadata: {}, download_url: '/api/projects/project_1/artifacts/artifact_1',
    created_at: '2026-07-17T00:00:00Z', ...overrides,
  }
}

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RevisionArtifactPanel', () => {
  it('downloads, verifies, and preserves immutable revision evidence', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({
      artifact_id: 'artifact_1', kind: 'preview-image', integrity: 'verified', immutable: true,
      regenerable: false, revealable: false, recovery_action: null,
    }))
    render(<RevisionArtifactPanel projectId="project_1" artifacts={[artifact()]} autoInspect={false} />)

    expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute(
      'href', '/api/projects/project_1/artifacts/artifact_1',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Check integrity' }))
    expect(await screen.findByText('verified')).toBeInTheDocument()
    expect(screen.getByText('Locked with this published revision.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete cached artifact' })).not.toBeInTheDocument()
  })

  it('capability-gates Finder reveal and reports typed unsupported results', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url === '/api/capabilities') return response({
        version: '0.1.0', tools: [], features: { finder_reveal: true },
      })
      if (url.endsWith('/inspection')) return response({
        artifact_id: 'artifact_1', kind: 'bambu-3mf', integrity: 'verified', immutable: true,
        regenerable: false, revealable: true, recovery_action: null,
      })
      if (url.endsWith('/reveal')) return response({
        artifact_id: 'artifact_1', supported: false, revealed: false,
        reason: 'Reveal in Finder is available only on macOS.',
      })
      throw new Error(`Unexpected fetch: ${url}`)
    })
    render(<RevisionArtifactPanel projectId="project_1" artifacts={[artifact({ kind: 'bambu-3mf' })]} autoInspect={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Check integrity' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Reveal in Finder' }))
    expect(await screen.findByText('Reveal in Finder is available only on macOS.')).toBeInTheDocument()
    expect(fetch.mock.calls.some(([url, init]) => String(url).endsWith('/reveal') && init?.method === 'POST')).toBe(true)
  })

  it('keeps download available while explicitly gating Finder on server capability', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url === '/api/capabilities') return response({
        version: '0.1.0', tools: [], features: { finder_reveal: false },
      })
      if (url.endsWith('/inspection')) return response({
        artifact_id: 'artifact_1', kind: 'bambu-3mf', integrity: 'verified', immutable: true,
        regenerable: false, revealable: true, recovery_action: null,
      })
      throw new Error(`Unexpected fetch: ${url}`)
    })
    render(<RevisionArtifactPanel projectId="project_1" artifacts={[artifact({ kind: 'bambu-3mf' })]} />)

    expect(await screen.findByText('Finder reveal unavailable here')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Download' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reveal in Finder' })).not.toBeInTheDocument()
  })

  it('requires confirmation and explains recovery before deleting a regenerable artifact', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(response({
        artifact_id: 'artifact_1', kind: 'preview-image', integrity: 'verified', immutable: false,
        regenerable: true, revealable: false,
        recovery_action: 'Generate a new preview to recreate this artifact.',
      }))
      .mockResolvedValueOnce(response({
        artifact_id: 'artifact_1', deleted_record: true, deleted_blob: true,
        shared_blob_retained: false,
        recovery_action: 'Generate a new preview to recreate this artifact.',
      }))
    const onDeleted = vi.fn()
    const cached = artifact({ revision_id: null, job_id: 'job_1' })
    render(<RevisionArtifactPanel projectId="project_1" artifacts={[cached]} onDeleted={onDeleted} autoInspect={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Check integrity' }))
    const deleteButton = await screen.findByRole('button', { name: 'Delete cached artifact' })
    fireEvent.click(deleteButton)
    const confirmation = screen.getByRole('group', { name: 'Confirm deletion of preview image' })
    expect(confirmation).toHaveTextContent(
      'Generate a new preview to recreate this artifact.',
    )
    const keepButton = screen.getByRole('button', { name: 'Keep artifact' })
    await waitFor(() => expect(keepButton).toHaveFocus())
    fireEvent.keyDown(keepButton, { key: 'Escape' })
    await waitFor(() => expect(deleteButton).toHaveFocus())
    fireEvent.click(deleteButton)
    fireEvent.click(screen.getByRole('button', { name: 'Delete artifact' }))
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(cached, expect.objectContaining({ deleted_record: true })))
    expect(screen.queryByText('preview image')).not.toBeInTheDocument()
    expect(fetch.mock.calls[1][1]).toMatchObject({ method: 'DELETE' })
  })

  it('automatically exposes verified integrity without a hidden prerequisite action', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({
      artifact_id: 'artifact_1', kind: 'preview-image', integrity: 'verified', immutable: true,
      regenerable: false, revealable: false, recovery_action: null,
    }))
    render(<RevisionArtifactPanel projectId="project_1" artifacts={[artifact()]} />)

    expect(await screen.findByText('verified')).toBeInTheDocument()
    expect(screen.getByText('Locked with this published revision.')).toBeInTheDocument()
  })
})
