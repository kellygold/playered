import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from '../api'
import { CalibrationWorkspace } from './CalibrationWorkspace'
import type {
  CalibrationCatalogCollection,
  CalibrationDraft,
  CalibrationSpecimen,
  PrintabilityProfile,
} from './contracts'

const api = vi.hoisted(() => ({
  acceptCalibrationProposal: vi.fn(),
  attestCalibrationDraft: vi.fn(),
  createCalibrationDraft: vi.fn(),
  fetchCalibrationCatalogs: vi.fn(),
  fetchCalibrationDrafts: vi.fn(),
  fetchCalibrationDraft: vi.fn(),
  fetchCalibrationProposals: vi.fn(),
  fetchCalibrationRuns: vi.fn(),
  fetchCalibrationSpecimen: vi.fn(),
  finalizeCalibrationDraft: vi.fn(),
  rejectCalibrationProposal: vi.fn(),
  removeCalibrationDraftMember: vi.fn(),
  updateCalibrationDraft: vi.fn(),
  uploadCalibrationDraftMember: vi.fn(),
}))

vi.mock('./api', () => api)

const HASH = 'a'.repeat(64)

const profile = {
  id: 'bambu-p2s-0.4-hardened-steel-pla-v1',
  display_name: 'Bambu Lab P2S · 0.4 mm · PLA',
  printer_id: 'bambu-p2s',
  nozzle_id: 'nozzle-0.4-hardened-steel',
  nozzle_diameter_mm: 0.4,
  material_class: 'pla',
  evidence_status: 'pending_print_calibration',
  reviewed_on: '2026-07-17',
  sweep: {
    dot_diameters_mm: [0.2, 0.4, 0.6],
    hole_diameters_mm: [0.2, 0.4, 0.6],
    line_widths_mm: [0.2, 0.4, 0.6],
    neck_widths_mm: [0.2, 0.4, 0.6],
    gap_widths_mm: [0.2, 0.4, 0.6],
  },
  recommendations: {},
} as PrintabilityProfile

const catalog = {
  active_fingerprint: HASH,
  items: [{
    fingerprint: HASH,
    active: true,
    parent_fingerprint: null,
    created_at: '2026-07-17T00:00:00Z',
    catalog: {
      schema_version: 1,
      catalog_id: 'catalog',
      catalog_version: '2026.07.17',
      source: {
        name: 'Image23MF',
        version: '1',
        captured_on: '2026-07-17',
        method: 'Engineering baseline awaiting physical calibration.',
        references: ['local'],
      },
      profiles: [profile],
    },
  }],
} as CalibrationCatalogCollection

const features = (['dot', 'hole', 'line', 'neck', 'gap'] as const).map((kind, index) => ({
  id: `${kind}-00-0p${index + 2}`,
  kind,
  column: 0,
  nominal_dimension_mm: 0.2 + index * 0.1,
  rasterized_dimension_mm: 0.2 + index * 0.1,
  rasterized_width_mm: 0.2 + index * 0.1,
  rasterized_height_mm: 0.2 + index * 0.1,
  dimension_name: kind === 'gap' ? 'gap' as const : kind === 'dot' || kind === 'hole' ? 'diameter' as const : 'width' as const,
  center_x_mm: 10,
  center_y_mm: 10,
  bounds: { x_mm: 9, y_mm: 9, width_mm: 2, height_mm: 2 },
}))

function draft(): CalibrationDraft {
  return {
    id: 'calibration_draft_12345678',
    catalog_fingerprint: HASH,
    profile,
    artifact: {
      schema_version: 2,
      profile_id: profile.id,
      profile_fingerprint: HASH,
      printer_id: profile.printer_id,
      nozzle_id: profile.nozzle_id,
      material_class: profile.material_class,
      width_mm: 160,
      height_mm: 112,
      raster_mm_per_pixel: 0.05,
      width_px: 3200,
      height_px: 2240,
      svg_sha256: HASH,
      png_sha256: HASH,
      features,
    },
    record: {
      schema_version: 1,
      status: 'template',
      record_id: null,
      profile_id: profile.id,
      artifact_fingerprint: HASH,
      printer_id: profile.printer_id,
      nozzle_id: profile.nozzle_id,
      material_class: profile.material_class,
      layer_height_mm: null,
      plate_id: null,
      filament: null,
      operator: null,
      printed_on: null,
      slicer_profile: null,
      observations: features.map((feature) => ({
        feature_id: feature.id, outcome: 'untested', measured_dimension_mm: null, notes: '',
      })),
      notes: '',
    },
    metadata: {
      slicer_application: null,
      slicer_version: null,
      slicer_executable_sha256: null,
      machine_profile_name: null,
      machine_profile_sha256: null,
      process_profile_name: null,
      process_profile_sha256: null,
      filament_profile_name: null,
      filament_profile_sha256: null,
      filament_id: null,
      filament_manufacturer: null,
      filament_family: '',
      filament_name: null,
      filament_material: null,
      filament_finish: '',
      filament_color_hex: null,
    },
    generation: 1,
    attested_at: null,
    attested_candidate_sha256: null,
    candidate_sha256: HASH,
    attestation_statement: 'I personally observed this physical print and recorded these outcomes accurately.',
    finalized_run_id: null,
    members: [],
    readiness: {
      evidence_state: 'preparation',
      ready_to_attest: false,
      ready_to_finalize: false,
      blockers: [{ code: 'record_id_missing', field: 'record.record_id', message: 'Add a stable run ID.' }],
    },
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
  }
}

const specimen = {
  catalog_fingerprint: HASH,
  catalog_id: 'catalog',
  catalog_version: '2026.07.17',
  profile,
  artifact: draft().artifact,
  record_template: draft().record,
  evidence_state: 'not_observed',
  evidence_message: 'Generated preparation files are not physically observed calibration evidence.',
  svg_url: `/api/calibration/specimens/${profile.id}/coupon.svg`,
  png_url: `/api/calibration/specimens/${profile.id}/coupon.png`,
  bundle_url: `/api/calibration/specimens/${profile.id}/bundle`,
} as CalibrationSpecimen

beforeEach(() => {
  api.fetchCalibrationCatalogs.mockResolvedValue(catalog)
  api.fetchCalibrationDrafts.mockResolvedValue({ items: [], total: 0 })
  api.fetchCalibrationRuns.mockResolvedValue({ items: [], total: 0 })
  api.fetchCalibrationProposals.mockResolvedValue({ items: [] })
  api.fetchCalibrationSpecimen.mockResolvedValue(specimen)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('CalibrationWorkspace', () => {
  it('shows honest profile state and starts a catalog-pinned resumable run', async () => {
    const created = draft()
    api.createCalibrationDraft.mockResolvedValue(created)
    render(<CalibrationWorkspace onClose={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: 'Start from a known specimen' }))
      .toBeInTheDocument()
    expect(screen.getByText('Pending print calibration')).toBeInTheDocument()
    expect(screen.getByText(/generated baselines, not physically observed evidence/i))
      .toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Start physical run' }))

    expect(await screen.findByRole('heading', { name: profile.display_name, level: 2 }))
      .toBeInTheDocument()
    expect(api.createCalibrationDraft).toHaveBeenCalledWith({
      profile_id: profile.id,
      expected_catalog_fingerprint: HASH,
    })
    expect(screen.getByText('Not physically observed.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Preparation bundle' })).toHaveAttribute(
      'href',
      `/api/calibration/specimens/${profile.id}/bundle`,
    )
  })

  it('retains measured zero and uncertain observations in an explicit save', async () => {
    const existing = draft()
    api.fetchCalibrationDrafts.mockResolvedValue({ items: [existing], total: 1 })
    api.updateCalibrationDraft.mockImplementation(async (_id: string, request: { record: CalibrationDraft['record'] }) => ({
      ...existing,
      record: request.record,
      generation: 2,
      readiness: { ...existing.readiness, evidence_state: 'unverified_draft' },
    }))
    render(<CalibrationWorkspace onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: new RegExp(profile.display_name) }))
    fireEvent.click(screen.getByRole('button', { name: '2. Observations' }))
    const holeGroup = screen.getByRole('group', { name: 'hole observations' })
    fireEvent.change(within(holeGroup).getByRole('combobox'), { target: { value: 'uncertain' } })
    fireEvent.change(within(holeGroup).getByRole('spinbutton'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save observations' }))

    await waitFor(() => expect(api.updateCalibrationDraft).toHaveBeenCalledTimes(1))
    const request = api.updateCalibrationDraft.mock.calls[0][1]
    const saved = request.record.observations.find(
      (item: CalibrationDraft['record']['observations'][number]) => item.feature_id.startsWith('hole-'),
    )
    expect(saved).toMatchObject({ outcome: 'uncertain', measured_dimension_mm: 0 })
  })

  it('renders exact readiness blockers and keeps attestation disabled until eligible', async () => {
    const existing = draft()
    api.fetchCalibrationDrafts.mockResolvedValue({ items: [existing], total: 1 })
    render(<CalibrationWorkspace onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: new RegExp(profile.display_name) }))
    fireEvent.click(screen.getByRole('button', { name: '4. Review & seal' }))

    expect(screen.getByText('Add a stable run ID.')).toBeInTheDocument()
    expect(screen.getByText('record.record_id')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Confirm physical observation attestation' }))
      .toBeDisabled()
    expect(screen.getByRole('button', { name: 'Seal immutable run' })).toBeDisabled()
  })

  it('uses retained draft coupons instead of substituting the active catalog specimen', async () => {
    const existing = draft()
    existing.catalog_fingerprint = 'b'.repeat(64)
    existing.members = [
      {
        id: 'coupon_svg_member', role: 'coupon_svg', ordinal: 0, filename: 'coupon.svg',
        sha256: 'c'.repeat(64), byte_size: 100, media_type: 'image/svg+xml', extension: '.svg',
        download_url: '/api/calibration/drafts/draft-old/members/coupon-svg',
        created_at: existing.created_at,
      },
      {
        id: 'coupon_png_member', role: 'coupon_png', ordinal: 0, filename: 'coupon.png',
        sha256: 'd'.repeat(64), byte_size: 100, media_type: 'image/png', extension: '.png',
        download_url: '/api/calibration/drafts/draft-old/members/coupon-png',
        created_at: existing.created_at,
      },
    ]
    api.fetchCalibrationDrafts.mockResolvedValue({ items: [existing], total: 1 })
    render(<CalibrationWorkspace onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: new RegExp(profile.display_name) }))

    expect(screen.getByRole('img', { name: `Calibration coupon for ${profile.display_name}` }))
      .toHaveAttribute('src', '/api/calibration/drafts/draft-old/members/coupon-png')
    expect(screen.getByRole('link', { name: 'SVG' })).toHaveAttribute(
      'href', '/api/calibration/drafts/draft-old/members/coupon-svg',
    )
    expect(screen.queryByRole('link', { name: 'Preparation bundle' })).not.toBeInTheDocument()
    expect(screen.getByText(/active catalog’s preparation bundle is intentionally not substituted/i))
      .toBeInTheDocument()
  })

  it('reloads the retained generation after a stale mutation instead of replaying it', async () => {
    const existing = draft()
    const recovered = { ...existing, generation: 7, record: { ...existing.record, notes: 'Saved elsewhere.' } }
    api.fetchCalibrationDrafts.mockResolvedValue({ items: [existing], total: 1 })
    api.updateCalibrationDraft.mockRejectedValue(new ApiRequestError(409, {
      error: {
        code: 'conflict',
        message: 'stale calibration draft generation',
        request_id: 'request_1',
        retryable: false,
        details: {},
      },
    }))
    api.fetchCalibrationDraft.mockResolvedValue(recovered)
    render(<CalibrationWorkspace onClose={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: new RegExp(profile.display_name) }))
    fireEvent.click(screen.getByRole('button', { name: 'Save setup' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The latest retained generation was reloaded',
    )
    expect(screen.getByText(/generation 7/)).toBeInTheDocument()
    expect(screen.getByLabelText('Run notes')).toHaveValue('Saved elsewhere.')
  })
})
