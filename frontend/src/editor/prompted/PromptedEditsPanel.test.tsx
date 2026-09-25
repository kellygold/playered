import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { PromptedEditsPanel } from './PromptedEditsPanel'

const provider = {
  provider_id: 'mock-provider',
  display_name: 'Mock Image Lab',
  model_id: 'edit-model',
  model_version: '2026-07-17',
  capabilities: {
    supports_mask: true,
    supports_references: false,
    supports_multiple_alternatives: true,
    supports_seed: false,
    guarantees_seeded_replay: false,
    maximum_references: 0,
    maximum_alternatives: 4,
    accepted_media_types: ['image/png'],
    output_media_types: ['image/png'],
  },
  privacy: {
    policy_version: '1',
    data_residency: 'Australia',
    retention: 'Deleted after processing.',
    training_use: 'none',
    subprocessors: [],
    policy_url: null,
  },
} as const

const preparation = {
  session_id: 'prompted_1',
  request_sha256: 'a'.repeat(64),
  provider,
  disclosure: {
    schema_version: 1,
    disclosure_sha256: 'b'.repeat(64),
    project_id: 'project_1',
    provider,
    source_sha256: 'c'.repeat(64),
    mask_sha256: 'd'.repeat(64),
    reference_sha256: [],
    prompt: 'Make the eye larger.',
    options: {},
    data_categories: ['source_pixels', 'selection_mask', 'prompt', 'generation_options'],
  },
  status: 'prepared',
} as const

const session = {
  id: 'prompted_1',
  project_id: 'project_1',
  parent_revision_id: 'revision_parent',
  retry_of_session_id: null,
  status: 'complete',
  request_sha256: 'a'.repeat(64),
  selection_sha256: 'e'.repeat(64),
  provider,
  disclosure: preparation.disclosure,
  prompt: 'Make the eye larger.',
  options: {},
  seed: null,
  requested_alternative_count: 2,
  alternatives: [{
    id: 'alternative_1',
    index: 0,
    status: 'review',
    output_sha256: 'f'.repeat(64),
    output_media_type: 'image/png',
    output_url: '/candidate.png',
    changed_mask_sha256: '1'.repeat(64),
    changed_mask_url: '/changed.mask',
    changed_mask_preview_url: '/changed.png',
    changed_pixel_count: 19,
    width_px: 100,
    height_px: 80,
    provenance: {
      prompt: 'Make the eye larger.',
      provider_id: 'mock-provider',
      model_id: 'edit-model',
      model_version: '2026-07-17',
      seed: null,
      reproducibility: 'best_effort',
      reproducibility_reason: 'Provider does not guarantee byte replay.',
      consent_event: {
        event_id: 'consent_1',
        recorded_at: '2026-07-17T10:00:00Z',
        disclosure: 'Approved pixels.',
      },
    },
  }],
  failures: [],
  accepted_alternative_id: null,
  accepted_revision_id: null,
  created_at: '2026-07-17T10:00:00Z',
  updated_at: '2026-07-17T10:01:00Z',
} as const

afterEach(() => vi.unstubAllGlobals())

it('keeps preparation local, discloses egress, and reviews in the shared comparison canvas', async () => {
  const requests: { url: string; init?: RequestInit }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    requests.push({ url, init })
    if (url === '/api/prompted-edit/providers') return response({ items: [provider] })
    if (url.endsWith('/prepare')) return response(preparation)
    if (url.endsWith('/execute')) {
      return response({
        session,
        job: {
          id: 'job_1', project_id: 'project_1', revision_id: 'revision_parent',
          type: 'prompted_edit', state: 'queued', stage: 'queued', progress: 0,
          request_key: 'prompted_1', supersession_key: null, generation: 0,
          failure: null, artifact_ids: [], created_at: '2026-07-17T10:00:00Z',
          started_at: null, finished_at: null, canceled_at: null,
        },
      })
    }
    throw new Error(`Unexpected fetch: ${url}`)
  }))

  renderPanel()
  await screen.findByRole('option', { name: /Mock Image Lab/ })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Make the eye larger.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review data disclosure' }))

  expect(await screen.findByText('Nothing has been sent yet')).toBeVisible()
  expect(screen.getByText(/source_pixels, selection_mask, prompt/)).toBeVisible()
  expect(requests.filter((item) => item.url.endsWith('/execute'))).toHaveLength(0)

  fireEvent.click(screen.getByRole('button', { name: 'Approve and generate' }))
  expect(await screen.findByRole('button', { name: 'Option 1' })).toBeVisible()
  expect(screen.getByText('19 pixels')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Split' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('button', { name: 'Flicker' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Accept as new revision' })).toBeEnabled()
})

it('degrades quietly when no provider integration is available', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ items: [] })))
  renderPanel()
  expect(await screen.findByText(/No prompted-edit provider is configured/)).toBeVisible()
  expect(screen.queryByRole('alert')).toBeNull()
})

function renderPanel() {
  return render(
    <PromptedEditsPanel
      projectId="project_1"
      parentRevisionId="revision_parent"
      previewJobId="job_preview"
      draftGeneration={4}
      sourceUrl="/source.png"
      sourceSha256={'c'.repeat(64)}
      sourceWidthPx={100}
      sourceHeightPx={80}
      canvasWidthMm={200}
      canvasHeightMm={160}
      selection={{
        schema_version: 1,
        coordinate_space: 'normalized_source',
        source_width_px: 100,
        source_height_px: 80,
        primitives: [{
          kind: 'rectangle',
          primitive_id: 'selection_prompted_test',
          combine: 'add',
          x: 20,
          y: 20,
          width: 30,
          height: 20,
        }],
        expand_mm: 0,
        feather_mm: 0,
      }}
      onAccepted={vi.fn()}
    />,
  )
}

function response(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}
