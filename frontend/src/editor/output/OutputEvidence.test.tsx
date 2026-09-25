import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ArtifactResource, ExportJobResult, JobResource } from '../../contracts'
import { OutputEvidence } from './OutputEvidence'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function artifact(
  id: string,
  kind: string,
  byteSize: number,
  metadata: Record<string, unknown> = {},
): ArtifactResource {
  return {
    id,
    job_id: 'export-1',
    revision_id: null,
    kind,
    sha256: id.padEnd(64, 'a').slice(0, 64),
    derivation_key: `derivation-${id}`,
    media_type: kind === 'bambu-3mf' ? 'model/3mf' : 'application/json',
    byte_size: byteSize,
    metadata,
    download_url: `/api/artifacts/${id}`,
    created_at: '2026-07-17T00:00:00Z',
  }
}

function result(): ExportJobResult {
  const job: JobResource = {
    id: 'export-1',
    project_id: 'project-1',
    revision_id: null,
    type: 'export',
    state: 'succeeded',
    stage: 'complete',
    progress: 1,
    request_key: 'request-1',
    supersession_key: 'export-project-1',
    generation: 1,
    failure: null,
    artifact_ids: ['package-1', 'quality-1', 'validation-1', 'log-1'],
    created_at: '2026-07-17T00:00:00Z',
    started_at: '2026-07-17T00:00:01Z',
    finished_at: '2026-07-17T00:00:02Z',
    canceled_at: null,
  }
  const packageArtifact = artifact('package-1', 'bambu-3mf', 2048)
  const quality = artifact('quality-1', 'geometry-quality-report', 700)
  const validation = artifact('validation-1', 'bambu-validation-report', 900, {
    status: 'validated',
    manual_print_gate: 'not_observed',
  })
  const log = artifact('log-1', 'bambu-validation-log', 300)
  return {
    job,
    artifacts: [packageArtifact, quality, validation, log],
    quality_report: quality,
    validation_report: validation,
    validation_log: log,
    package: packageArtifact,
    download_ready: true,
  }
}

describe('OutputEvidence', () => {
  it('shows package provenance, explicit artifact access, and retained slicer warnings', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => String(input) === '/api/capabilities'
      ? new Response(JSON.stringify({ version: '0.1.0', tools: [], features: { finder_reveal: false } }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      : new Response(JSON.stringify({
      result: {
        warnings: ['Legacy floating region warning'],
        report: {
          warnings: [{
            category: 'support_required',
            message: 'Support may be required below the marked bridge.',
          }],
        },
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const output = result()

    render(<OutputEvidence projectId="project_1" result={output} current />)

    expect(screen.getByRole('heading', { name: 'Verified output evidence' })).toBeInTheDocument()
    expect(screen.getAllByText('2.00 KiB (2048 bytes)')).toHaveLength(2)
    expect(screen.getByText(output.package!.sha256)).toBeVisible()
    expect(screen.getByText('Bambu Studio · validated')).toBeVisible()
    expect(screen.getByText('not observed')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Download 3MF package' }))
      .toHaveAttribute('href', '/api/artifacts/package-1')
    expect(screen.getByRole('link', { name: 'Open geometry quality report (JSON)' }))
      .toHaveAttribute('href', '/api/artifacts/quality-1')
    expect(screen.getByRole('link', { name: 'Open slicer validation report (JSON)' }))
      .toHaveAttribute('href', '/api/artifacts/validation-1')
    expect(screen.getByRole('link', { name: 'Open retained validation log (text)' }))
      .toHaveAttribute('href', '/api/artifacts/log-1')
    expect(await screen.findByText('Legacy floating region warning')).toBeVisible()
    expect(screen.getByText('support required')).toBeVisible()
    expect(screen.getByText('Support may be required below the marked bridge.')).toBeVisible()
  })

  it('does not fetch or expose evidence from a stale result', () => {
    const fetch = vi.spyOn(globalThis, 'fetch')

    render(<OutputEvidence projectId="project_1" result={result()} current={false} />)

    expect(screen.queryByRole('heading', { name: 'Verified output evidence' })).not.toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('keeps retained artifact links available when warning details cannot be loaded', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => String(input) === '/api/capabilities'
      ? new Response(JSON.stringify({ version: '0.1.0', tools: [], features: { finder_reveal: false } }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      : new Response('', { status: 503 }))

    render(<OutputEvidence projectId="project_1" result={result()} current />)

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Validation evidence returned HTTP 503.')
    })
    expect(screen.getByRole('link', { name: 'Open slicer validation report (JSON)' })).toBeVisible()
  })

  it('reveals the verified package through the project-scoped API when advertised', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url === '/api/capabilities') return new Response(JSON.stringify({
        version: '0.1.0', tools: [], features: { finder_reveal: true },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (url.endsWith('/reveal')) return new Response(JSON.stringify({
        artifact_id: 'package-1', supported: true, revealed: true, reason: null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      return new Response(JSON.stringify({ result: { warnings: [] } }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      })
    })
    render(<OutputEvidence projectId="project_1" result={result()} current />)

    fireEvent.click(await screen.findByRole('button', { name: 'Reveal verified 3MF in Finder' }))
    expect(await screen.findByText('Revealed verified package in Finder.')).toBeVisible()
    expect(fetch.mock.calls.some(([url, init]) => (
      String(url) === '/api/projects/project_1/artifacts/package-1/reveal'
      && init?.method === 'POST'
    ))).toBe(true)
  })
})
