import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  applyGarbageCollection,
  branchRevision,
  cancelJob,
  fetchPreviewResult,
  fetchJob,
  fetchPrintabilityProfiles,
  fetchProfileCatalog,
  fetchProject,
  fetchStartupRecovery,
  fetchRevision,
  fetchRevisions,
  importProject,
  jobEventsUrl,
  moveDraftHistory,
  planGarbageCollection,
  publishRevision,
  reconcileWorkspace,
  resolvePrintabilityProfile,
  saveDraft,
  startPreview,
  validatePrintSetup,
} from './api'

afterEach(() => vi.restoreAllMocks())

describe('API contract client', () => {
  it('sends exact idempotent command and cursor guards for draft history', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockImplementation(async () => new Response(JSON.stringify({ generation: 3 }), { status: 200 }))
    const config = { schema_version: 1 } as never
    const historyCommand = {
      schema_version: 1 as const,
      id: '00000000-0000-4000-8000-000000000001',
      command_type: 'config_change' as const,
      label: 'Resize canvas',
      before_state_sha256: 'a'.repeat(64),
      expected_cursor_node_id: 'node_1',
    }

    await saveDraft('project / 1', config, [], 2, historyCommand)
    await moveDraftHistory('project / 1', 'undo', {
      request_id: '00000000-0000-4000-8000-000000000002',
      expected_draft_generation: 3,
      expected_cursor_node_id: 'node_2',
    })

    expect(fetch).toHaveBeenNthCalledWith(1, '/api/projects/project%20%2F%201/draft', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        config, operations: [], expected_draft_generation: 2, history_command: historyCommand,
      }), signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/projects/project%20%2F%201/draft/history/undo', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_id: '00000000-0000-4000-8000-000000000002',
        expected_draft_generation: 3,
        expected_cursor_node_id: 'node_2',
      }), signal: undefined,
    })
  })

  it('uses compact revision history, exact detail, guarded publish, and guarded branch endpoints', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'revision_1' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: { id: 'revision_2' }, draft: { generation: 8 }, project: { active_revision_id: 'revision_2' } }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ base_revision: { id: 'revision_1' }, draft: { generation: 9 }, project: { active_revision_id: 'revision_2' } }), { status: 200 }))

    await fetchRevisions('project / 1', { limit: 50 })
    await fetchRevision('project / 1', 'revision / 1')
    await publishRevision('project / 1', {
      label: 'Named proof',
      notes: 'Exact saved state.',
      expected_draft_generation: 7,
      preview_job_id: 'job_1',
    })
    await branchRevision('project / 1', 'revision / 1', 8)

    expect(fetch).toHaveBeenNthCalledWith(1, '/api/projects/project%20%2F%201/revisions?limit=50', { signal: undefined })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/projects/project%20%2F%201/revisions/revision%20%2F%201', { signal: undefined })
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/projects/project%20%2F%201/revisions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: 'Named proof', notes: 'Exact saved state.', expected_draft_generation: 7, preview_job_id: 'job_1' }), signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(4, '/api/projects/project%20%2F%201/revisions/revision%20%2F%201/branch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_draft_generation: 8 }), signal: undefined,
    })
  })

  it('preserves structured request correlation when a job is missing', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: 'not_found',
            message: 'The requested job does not exist.',
            request_id: 'proof-123',
            retryable: false,
            details: { resource: 'job', id: 'job_missing' },
          },
        }),
        { status: 404, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(fetchJob('job_missing')).rejects.toMatchObject({
      name: 'ApiRequestError',
      status: 404,
      problem: { error: { code: 'not_found', request_id: 'proof-123' } },
    })
  })

  it('uses the cancel resource and returns its deterministic terminal state', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'job_1',
          project_id: 'project_1',
          revision_id: null,
          type: 'preview',
          state: 'canceled',
          stage: 'canceled',
          progress: 0.4,
          request_key: null,
          failure: null,
          artifact_ids: [],
          created_at: '2026-07-16T00:00:00Z',
          started_at: '2026-07-16T00:00:01Z',
          finished_at: '2026-07-16T00:00:02Z',
          canceled_at: '2026-07-16T00:00:02Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const job = await cancelJob('job_1')

    expect(job.state).toBe('canceled')
    expect(job.stage).toBe('canceled')
    expect(fetch).toHaveBeenCalledWith('/api/jobs/job_1/cancel', {
      method: 'POST',
      signal: undefined,
    })
  })

  it('requires a reviewed dry-run fingerprint before applying garbage collection', async () => {
    const fetch = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            generated_at: '2026-07-16T00:00:00Z',
            minimum_age_seconds: 86400,
            plan_sha256: 'a'.repeat(64),
            candidate_count: 1,
            reclaimable_bytes: 42,
            candidates: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            applied: true,
            plan_sha256: 'a'.repeat(64),
            eligible_count: 1,
            deleted_count: 1,
            reclaimed_bytes: 42,
            skipped: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )

    const plan = await planGarbageCollection(86400)
    const result = await applyGarbageCollection(plan)

    expect(result.deleted_count).toBe(1)
    expect(fetch).toHaveBeenNthCalledWith(
      1,
      '/api/workspace/gc/plan?minimum_age_seconds=86400',
      { method: 'POST', signal: undefined },
    )
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/workspace/gc/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_sha256: 'a'.repeat(64), minimum_age_seconds: 86400 }),
      signal: undefined,
    })
  })

  it('reads durable startup evidence and runs only the explicit safe reconciliation action', async () => {
    const report = {
      id: 'reconciliation_1',
      status: 'attention',
      started_at: '2026-07-17T00:00:00Z',
      completed_at: '2026-07-17T00:00:01Z',
      interrupted_jobs: [],
      stale_temp_file_count: 1,
      orphan_file_count: 1,
      database_integrity: true,
      foreign_key_integrity: true,
      automatic_deletions: 0,
      recommended_actions: ['Review cleanup.'],
    }
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const startup = await fetchStartupRecovery()
    const checked = await reconcileWorkspace()

    expect(startup.automatic_deletions).toBe(0)
    expect(checked.id).toBe(report.id)
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/workspace/recovery/latest', {
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/workspace/recovery/reconcile', {
      method: 'POST',
      signal: undefined,
    })
  })

  it('loads profile choices and posts the selected profile ids for validation', async () => {
    const catalog = {
      schema_version: 1,
      catalog_id: 'image23mf-bundled-printers',
      catalog_version: '2026.07.16',
      source: {
        name: 'Bambu Studio bundled system profiles',
        version: '02.07.01.62',
        captured_on: '2026-07-16',
        profile_paths: ['profiles/BBL/machine/Bambu Lab P2S.json'],
      },
      printers: [],
    }
    const setup = {
      printer_id: 'bambu-p2s',
      nozzle_id: 'nozzle-0.4-hardened-steel',
      plate_id: 'textured-pei',
      layer_height_mm: 0.2,
      canvas_width_mm: 200,
      canvas_height_mm: 200,
      base_thickness_mm: 1.2,
      art_thickness_mm: 0.6,
    }
    const fetch = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        new Response(JSON.stringify(catalog), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ request: setup, profile_catalog_fingerprint: 'a'.repeat(64) }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )

    const loaded = await fetchProfileCatalog()
    const validated = await validatePrintSetup(setup)

    expect(loaded.catalog_id).toBe('image23mf-bundled-printers')
    expect(validated.profile_catalog_fingerprint).toHaveLength(64)
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/profiles', { signal: undefined })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/profiles/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(setup),
      signal: undefined,
    })
  })

  it('loads provisional printability evidence and preserves explicit overrides', async () => {
    const catalog = {
      schema_version: 1,
      catalog_id: 'image23mf-bundled-printability',
      catalog_version: '2026.07.16',
      source: {
        name: 'Image23MF local printability calibration',
        version: '1',
        captured_on: '2026-07-16',
        method: 'Provisional engineering baseline.',
        references: ['docs/risk-taxonomy.md'],
      },
      profiles: [],
    }
    const request = {
      printer_id: 'bambu-p2s',
      nozzle_id: 'nozzle-0.4-hardened-steel',
      material_class: 'pla',
      overrides: { minimum_line_width_mm: 0.52 },
    }
    const resolved = {
      profile_id: 'bambu-p2s-0.4-hardened-steel-pla-v1',
      profile_display_name: 'P2S 0.4 PLA baseline',
      profile_catalog_fingerprint: 'b'.repeat(64),
      evidence_status: 'pending_print_calibration',
      warning: 'Pending a recorded local print calibration.',
      values: [
        {
          name: 'minimum_line_width_mm',
          value: 0.52,
          unit: 'mm',
          source: 'user_override',
          profile_value: 0.48,
          profile_basis: 'engineering_baseline',
          profile_confidence: 'provisional',
          rationale: 'User override replaces the profile value.',
        },
      ],
    }
    const fetch = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        new Response(JSON.stringify(catalog), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(resolved), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )

    const loaded = await fetchPrintabilityProfiles()
    const settings = await resolvePrintabilityProfile(request)

    expect(loaded.catalog_id).toBe('image23mf-bundled-printability')
    expect(settings.values[0].source).toBe('user_override')
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/printability-profiles', {
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/printability-profiles/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal: undefined,
    })
  })

  it('uses raw image import and generation-guarded preview resources', async () => {
    const config = {
      schema_version: 1 as const,
      source_asset_id: 'asset_1',
      canvas: { width_mm: 200, height_mm: 200 },
      crop: { mode: 'cover' as const, x: 0, y: 0, width: 1, height: 1 },
      printer: {
        profile_catalog_id: 'image23mf-bundled-printers',
        profile_catalog_version: '2026.07.16',
        printer_id: 'bambu-p2s',
        nozzle_id: 'nozzle-0.4-hardened-steel',
        nozzle_mm: 0.4,
        layer_height_mm: 0.2,
        plate_id: 'textured-pei',
      },
      palette: { colors: [] },
      cleanup: {
        min_island_mm2: 0.3,
        max_hole_mm2: 0.5,
        smoothing_radius_mm: 0,
        merge_policy: 'review' as const,
        preserve_long_lines: true,
      },
      geometry: {
        style: 'flush_inlay' as const,
        base_thickness_mm: 1.2,
        art_thickness_mm: 0.6,
        corner_radius_mm: 0,
      },
    }
    const imported = {
      project: { id: 'project_1' },
      source_asset: { id: 'asset_1' },
      draft: { generation: 1, config },
    }
    const started = {
      draft: { generation: 2, config },
      job: { id: 'job_1', state: 'queued' },
    }
    const fetch = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify(imported), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(imported), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(started), { status: 202 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ job: { id: 'job_1', state: 'succeeded' }, artifacts: [] }),
          { status: 200 },
        ),
      )
    const image = new Blob(['png'], { type: 'image/png' })

    const workspace = await importProject(image, 'Mural #1.png', 'My mural')
    await fetchProject(workspace.project.id)
    const preview = await startPreview('project_1', config, workspace.draft!.generation)
    const result = await fetchPreviewResult(preview.job.id)

    expect(result.job.state).toBe('succeeded')
    expect(jobEventsUrl('job / 1')).toBe('/api/jobs/job%20%2F%201/events')
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/projects/import?project_name=My+mural', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': 'Mural #1.png' },
      body: image,
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/projects/project_1', { signal: undefined })
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/projects/project_1/previews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config, expected_draft_generation: 1 }),
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(4, '/api/jobs/job_1/result', { signal: undefined })
  })
})
