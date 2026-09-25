import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  acceptCalibrationProposal,
  attestCalibrationDraft,
  createCalibrationDraft,
  createCalibrationProposal,
  downloadCalibrationDraftMember,
  downloadCalibrationRunBundle,
  downloadCalibrationRunMember,
  downloadCalibrationSpecimen,
  fetchCalibrationCatalog,
  fetchCalibrationCatalogs,
  fetchCalibrationDraft,
  fetchCalibrationDrafts,
  fetchCalibrationProposal,
  fetchCalibrationProposals,
  fetchCalibrationRun,
  fetchCalibrationRuns,
  fetchCalibrationSpecimen,
  finalizeCalibrationDraft,
  importCalibrationRun,
  rejectCalibrationProposal,
  removeCalibrationDraftMember,
  updateCalibrationDraft,
  uploadCalibrationDraftMember,
} from './api'
import type { CalibrationDraftMetadata, CalibrationRunRecord } from './contracts'

afterEach(() => vi.restoreAllMocks())

const jsonResponse = (body: unknown = {}) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })

describe('calibration API client', () => {
  it('uses the catalog and generated specimen resources without calling them observations', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse())

    await fetchCalibrationCatalogs()
    await fetchCalibrationCatalog('catalog / fingerprint')
    await fetchCalibrationSpecimen('profile / 1')

    expect(fetch).toHaveBeenNthCalledWith(1, '/api/printability-profile-catalogs', {
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      '/api/printability-profile-catalogs/catalog%20%2F%20fingerprint',
      { signal: undefined },
    )
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/calibration/specimens/profile%20%2F%201', {
      signal: undefined,
    })
  })

  it('downloads every specimen representation with the server filename', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) =>
      new Response('bytes', {
        status: 200,
        headers: {
          'Content-Type': String(input).endsWith('.png') ? 'image/png' : 'application/zip',
          'Content-Disposition': 'attachment; filename="exact-coupon.bin"',
        },
      }),
    )

    const svg = await downloadCalibrationSpecimen('profile 1', 'svg')
    const png = await downloadCalibrationSpecimen('profile 1', 'png')
    const bundle = await downloadCalibrationSpecimen('profile 1', 'bundle')

    expect(svg.filename).toBe('exact-coupon.bin')
    expect(png.blob.size).toBe(5)
    expect(bundle.media_type).toBe('application/zip')
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      '/api/calibration/specimens/profile%201/coupon.svg',
      '/api/calibration/specimens/profile%201/coupon.png',
      '/api/calibration/specimens/profile%201/bundle',
    ])
  })

  it('covers the guarded, restart-safe draft lifecycle', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse())
    const record = { schema_version: 1, observations: [] } as unknown as CalibrationRunRecord
    const metadata = { filament_family: '', filament_finish: '' } as CalibrationDraftMetadata
    const attachment = new Blob(['3mf'], { type: 'application/vnd.ms-package.3dmanufacturing-3dmodel+xml' })

    await createCalibrationDraft({
      profile_id: 'profile_1',
      expected_catalog_fingerprint: 'a'.repeat(64),
    })
    await fetchCalibrationDrafts()
    await fetchCalibrationDraft('draft / 1')
    await updateCalibrationDraft('draft / 1', {
      expected_generation: 2,
      record,
      metadata,
    })
    await uploadCalibrationDraftMember('draft / 1', 'project_3mf', 0, 3, attachment)
    await removeCalibrationDraftMember('draft / 1', 'photo', 2, 4)
    await attestCalibrationDraft('draft / 1', { expected_generation: 5, confirmed: true })
    await finalizeCalibrationDraft('draft / 1', 6)

    expect(fetch).toHaveBeenNthCalledWith(1, '/api/calibration/drafts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        profile_id: 'profile_1', expected_catalog_fingerprint: 'a'.repeat(64),
      }),
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/calibration/drafts', { signal: undefined })
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/calibration/drafts/draft%20%2F%201', {
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(4, '/api/calibration/drafts/draft%20%2F%201', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_generation: 2, record, metadata }),
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(
      5,
      '/api/calibration/drafts/draft%20%2F%201/members/project_3mf/0?expected_generation=3',
      {
        method: 'PUT',
        headers: { 'Content-Type': 'model/3mf' },
        body: attachment,
        signal: undefined,
      },
    )
    expect(fetch).toHaveBeenNthCalledWith(
      6,
      '/api/calibration/drafts/draft%20%2F%201/members/photo/2?expected_generation=4',
      { method: 'DELETE', signal: undefined },
    )
    expect(fetch).toHaveBeenNthCalledWith(7, '/api/calibration/drafts/draft%20%2F%201/attest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_generation: 5, confirmed: true }),
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(8, '/api/calibration/drafts/draft%20%2F%201/finalize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_generation: 6 }),
      signal: undefined,
    })
  })

  it('downloads retained draft and run members and the immutable source bundle', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response('proof', {
        status: 200,
        headers: {
          'Content-Type': 'application/octet-stream',
          'Content-Disposition': "attachment; filename*=UTF-8''observed%20proof.jpg",
        },
      }),
    )

    const draft = await downloadCalibrationDraftMember('draft 1', 'member 1')
    const bundle = await downloadCalibrationRunBundle('run 1')
    const member = await downloadCalibrationRunMember('run 1', 'member 2')

    expect(draft.filename).toBe('observed proof.jpg')
    expect(bundle.blob.size).toBe(5)
    expect(member.media_type).toBe('application/octet-stream')
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      '/api/calibration/drafts/draft%201/members/member%201',
      '/api/calibration/runs/run%201/bundle',
      '/api/calibration/runs/run%201/members/member%202',
    ])
  })

  it('covers imported run list/detail and canonical profile filters', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse())
    const bundle = new Blob(['zip'], { type: 'application/zip' })

    await importCalibrationRun(bundle)
    await fetchCalibrationRuns(' profile / 1 ')
    await fetchCalibrationRuns()
    await fetchCalibrationRun('run / 1')

    expect(fetch).toHaveBeenNthCalledWith(1, '/api/calibration/runs/import', {
      method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: bundle, signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/calibration/runs?profile_id=profile+%2F+1', {
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/calibration/runs', { signal: undefined })
    expect(fetch).toHaveBeenNthCalledWith(4, '/api/calibration/runs/run%20%2F%201', {
      signal: undefined,
    })
  })

  it('covers proposal derivation, filters, exact detail, acceptance, and rejection', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse())
    const create = {
      profile_id: 'profile_1', run_ids: ['run_1'], expected_catalog_fingerprint: 'b'.repeat(64),
    }
    const review = { reviewer: 'Kelly', reason: 'Reviewed source observations.' }

    await createCalibrationProposal(create)
    await fetchCalibrationProposals('profile / 1')
    await fetchCalibrationProposal('proposal / 1')
    await acceptCalibrationProposal('proposal / 1', {
      ...review, expected_catalog_fingerprint: 'b'.repeat(64),
    })
    await rejectCalibrationProposal('proposal / 2', review)

    expect(fetch).toHaveBeenNthCalledWith(1, '/api/calibration/proposals', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(create), signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/calibration/proposals?profile_id=profile+%2F+1', {
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/calibration/proposals/proposal%20%2F%201', {
      signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(4, '/api/calibration/proposals/proposal%20%2F%201/accept', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...review, expected_catalog_fingerprint: 'b'.repeat(64) }), signal: undefined,
    })
    expect(fetch).toHaveBeenNthCalledWith(5, '/api/calibration/proposals/proposal%20%2F%202/reject', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(review), signal: undefined,
    })
  })

  it('preserves structured API failures for actionable recovery', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        error: {
          code: 'conflict',
          message: 'calibration draft changed before save',
          request_id: 'request-117',
          retryable: false,
          details: { expected_generation: 4, actual_generation: 5 },
        },
      }), { status: 409, headers: { 'Content-Type': 'application/json' } }),
    )

    await expect(fetchCalibrationDraft('stale')).rejects.toMatchObject({
      name: 'ApiRequestError',
      status: 409,
      problem: { error: { code: 'conflict', request_id: 'request-117' } },
    })
  })
})
