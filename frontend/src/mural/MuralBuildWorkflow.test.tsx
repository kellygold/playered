import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ArtifactResource, GeometryJobResult, JobResource } from '../contracts'
import type {
  MuralBuildResult,
  MuralBuildStart,
  MuralBuildTransport,
} from './MuralBuildCoordinator'
import { MuralBuildWorkflow, type MuralBuildWorkflowProps } from './MuralBuildWorkflow'
import type { MuralSeamQaReport } from './seamTypes'
import type { MuralPlanResource } from './types'

vi.mock('./MuralSeamQA', () => ({
  MuralSeamQA: ({ masterImageUrl }: { masterImageUrl: string }) => (
    <div data-testid="assembled-seam-qa">Assembled seam QA · {masterImageUrl}</div>
  ),
}))

function job(id: string, state: JobResource['state'], projectId = 'project-a'): JobResource {
  return {
    id,
    project_id: projectId,
    revision_id: null,
    type: 'export',
    state,
    stage: state === 'succeeded' ? 'complete' : state === 'canceled' ? 'canceled' : 'packaging',
    progress: state === 'succeeded' ? 1 : 0.42,
    request_key: null,
    supersession_key: null,
    generation: 1,
    failure: null,
    artifact_ids: [],
    created_at: '2026-07-17T00:00:00Z',
    started_at: null,
    finished_at: null,
    canceled_at: null,
  }
}

function artifact(id: string, kind: string): ArtifactResource {
  return {
    id,
    job_id: null,
    revision_id: null,
    kind,
    sha256: id.padEnd(64, 'a').slice(0, 64),
    derivation_key: 'd'.repeat(64),
    media_type: kind === 'bambu-mural-3mf' ? 'model/3mf' : 'application/json',
    byte_size: kind === 'bambu-mural-3mf' ? 7_500_000 : 1000,
    metadata: {},
    download_url: `/api/artifacts/${id}`,
    created_at: '2026-07-17T00:00:00Z',
  }
}

function plan(
  projectId = 'project-a',
  processedArtifactId = 'master-a',
  generation = 4,
): MuralPlanResource {
  return {
    project_id: projectId,
    schema_version: 1,
    generation,
    freshness: 'current',
    stale_reason: null,
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
    request: {
      schema_version: 1,
      source: {
        source_asset_id: 'asset-a',
        source_asset_sha256: '1'.repeat(64),
        processed_artifact_id: processedArtifactId,
        processed_artifact_sha256: '2'.repeat(64),
        processed_size: { width: 1201, height: 799 },
        canonical_transform: {},
        config_sha256: '3'.repeat(64),
        engine_version: '0.1.0',
        revision_id: null,
        draft_generation: 8,
      },
      layout: {
        rows: 2,
        columns: 3,
        panel_width_mm: 200,
        panel_height_mm: 200,
        horizontal_gap_mm: 0,
        vertical_gap_mm: 0,
        bleed_mm: 0,
        orientation: 'auto',
      },
      bed: {
        printer_id: 'bambu-p2s',
        plate_id: 'textured-pei',
        profile_catalog_fingerprint: '4'.repeat(64),
        width_mm: 256,
        height_mm: 256,
        edge_clearance_mm: 0,
        excluded_rectangles: [],
      },
      reserved_rectangles: [{ x_mm: 216, y_mm: 5, width_mm: 35, height_mm: 35 }],
    },
    plan: {
      schema_version: 1,
      request_fingerprint: 'a'.repeat(64),
      source_fingerprint: 'b'.repeat(64),
      master_transform: {},
      master_size_mm: { width: 600, height: 400 },
      assembled_size_mm: { width: 600, height: 400 },
      tiles: Array.from({ length: 6 }, (_, index) => ({ id: `tile-${index + 1}` })) as never,
      all_tiles_fit: true,
      warnings: [],
    },
  }
}

function seamReport(currentPlan: MuralPlanResource): MuralSeamQaReport {
  return {
    request_fingerprint: currentPlan.plan.request_fingerprint,
    status: 'pass',
    tiles: Array.from({ length: 6 }, () => ({})),
  } as unknown as MuralSeamQaReport
}

function geometry(currentPlan = plan()): GeometryJobResult {
  const geometryIr = artifact('geometry-ir', 'geometry-ir')
  return {
    job: job('geometry-job', 'succeeded', currentPlan.project_id),
    artifacts: [geometryIr],
    geometry_svg: null,
    geometry_ir: geometryIr,
    geometry_mesh: null,
    geometry_report: null,
    geometry_preview: null,
    export_ready: true,
  }
}

function result(
  currentPlan = plan(),
  overrides: Partial<MuralBuildResult> = {},
): MuralBuildResult {
  const packageArtifact = artifact('mural-package', 'bambu-mural-3mf')
  const labels = artifact('mural-labels', 'mural-label-partition')
  const topology = artifact('mural-topology', 'mural-topology-partition')
  return {
    job: job('mural-job', 'succeeded', currentPlan.project_id),
    freshness: 'current',
    stale_reason: null,
    cache_hit: false,
    artifacts: [packageArtifact, labels, topology],
    package: packageArtifact,
    label_partition: labels,
    topology_partition: topology,
    seam_qa: artifact('seam-qa', 'mural-seam-qa'),
    seam_qa_report: seamReport(currentPlan),
    validation_report: artifact('validation-report', 'bambu-validation-report'),
    validation_log: artifact('validation-log', 'bambu-validation-log'),
    assembly_aids: null,
    assembly_sheet: null,
    validation: {
      status: 'validated',
      reason: null,
      attempted: true,
      executable: '/Applications/BambuStudio.app',
      version: '2.0',
    },
    download_ready: true,
    ...overrides,
  }
}

function transport(
  currentPlan = plan(),
  overrides: Partial<MuralBuildTransport> = {},
): MuralBuildTransport {
  return {
    startGeometry: vi.fn(async () => ({ job: job('geometry-job', 'succeeded', currentPlan.project_id) })),
    fetchGeometryResult: vi.fn(async () => geometry(currentPlan)),
    startMural: vi.fn(async () => ({ job: job('mural-job', 'succeeded', currentPlan.project_id) })),
    fetchJob: vi.fn(async (id) => job(id, 'succeeded', currentPlan.project_id)),
    fetchResult: vi.fn(async () => result(currentPlan)),
    cancel: vi.fn(async (id) => job(id, 'canceled', currentPlan.project_id)),
    ...overrides,
  }
}

function props(
  currentPlan: MuralPlanResource | null,
  api = transport(currentPlan ?? plan()),
): MuralBuildWorkflowProps {
  return {
    projectId: currentPlan?.project_id ?? 'project-a',
    processedArtifactId: currentPlan?.request.source.processed_artifact_id ?? 'master-a',
    masterImageUrl: '/wager-master.png',
    previewJobId: 'preview-a',
    expectedDraftGeneration: 8,
    previewCurrent: true,
    profile: {
      printer_model: 'Bambu Lab P2S',
      nozzle_diameter_mm: 0.4,
      layer_height_mm: 0.2,
      bed_type: 'Textured PEI Plate',
    },
    plan: currentPlan,
    planDirty: false,
    transport: api,
  }
}

afterEach(cleanup)

describe('MuralBuildWorkflow', () => {
  it('explains why build is disabled until preview and plan are current', () => {
    const view = render(<MuralBuildWorkflow {...props(null)} />)
    expect(screen.getByRole('button', { name: 'Build multi-plate 3MF' })).toBeDisabled()
    expect(screen.getByText('Save the current mural plan before building.')).toBeInTheDocument()

    view.rerender(<MuralBuildWorkflow {...props(plan())} previewCurrent={false} />)
    expect(screen.getByText('Render a current preview before building the mural.')).toBeInTheDocument()
  })

  it('runs automatic geometry preflight, builds 3×2 package, shows QA, and downloads verified output', async () => {
    const currentPlan = plan()
    const order: string[] = []
    const api = transport(currentPlan, {
      startGeometry: vi.fn(async () => {
        order.push('geometry')
        return { job: job('geometry-job', 'succeeded') }
      }),
      startMural: vi.fn(async () => {
        order.push('mural')
        return { job: job('mural-job', 'succeeded') }
      }),
    })
    render(<MuralBuildWorkflow {...props(currentPlan, api)} />)

    fireEvent.click(screen.getByRole('button', { name: 'Build 6-plate 3MF' }))

    const download = await screen.findByRole('link', {
      name: 'Download verified multi-plate 3MF',
    })
    expect(order).toEqual(['geometry', 'mural'])
    expect(download).toHaveAttribute('href', '/api/artifacts/mural-package')
    expect(screen.getByText('6 unique build plates')).toBeInTheDocument()
    expect(screen.getByTestId('assembled-seam-qa')).toHaveTextContent('/wager-master.png')
  })

  it('configures external assembly aids and downloads the guide without presenting it as art', async () => {
    const currentPlan = plan()
    const assemblyMetadata = artifact('assembly-aids', 'mural-assembly-aids')
    const assemblySheet = {
      ...artifact('assembly-sheet', 'mural-assembly-sheet'),
      media_type: 'image/svg+xml',
    }
    const api = transport(currentPlan, {
      fetchResult: vi.fn(async () => result(currentPlan, {
        assembly_aids: assemblyMetadata,
        assembly_sheet: assemblySheet,
      })),
    })
    render(<MuralBuildWorkflow {...props(currentPlan, api)} />)

    fireEvent.click(screen.getByRole('checkbox', { name: /Include external assembly aids/ }))
    expect(screen.getByRole('checkbox', { name: 'Rear identifiers' })).toBeChecked()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Edge pair labels' }))
    fireEvent.click(screen.getByRole('button', { name: 'Build 6-plate 3MF' }))

    expect(await screen.findByRole('link', { name: 'Download assembly sheet' }))
      .toHaveAttribute('href', '/api/artifacts/assembly-sheet')
    expect(screen.getByRole('link', { name: 'Download assembly metadata' }))
      .toHaveAttribute('href', '/api/artifacts/assembly-aids')
    expect(api.startMural).toHaveBeenCalledWith(
      expect.objectContaining({
        assemblyAids: {
          enabled: true,
          rearIdentifiers: true,
          edgeIdentifiers: false,
          orientationMarks: true,
          cropMarks: true,
          alignmentJigMetadata: true,
        },
      }),
      expect.anything(),
      expect.any(AbortSignal),
    )

    fireEvent.click(screen.getByRole('checkbox', { name: /Include external assembly aids/ }))
    await waitFor(() => {
      expect(screen.queryByRole('link', { name: 'Download assembly sheet' })).not.toBeInTheDocument()
    })
  })

  it('requires at least one selected aid when the external guide is enabled', () => {
    render(<MuralBuildWorkflow {...props(plan())} />)
    fireEvent.click(screen.getByRole('checkbox', { name: /Include external assembly aids/ }))
    for (const name of [
      'Rear identifiers',
      'Edge pair labels',
      'TOP orientation marks',
      'Sheet crop marks',
      'Spacer and jig dimensions',
    ]) {
      fireEvent.click(screen.getByRole('checkbox', { name }))
    }

    expect(screen.getByRole('button', { name: 'Build 6-plate 3MF' })).toBeDisabled()
    expect(screen.getByText('Choose at least one assembly aid, or turn the guide off.'))
      .toBeInTheDocument()
  })

  it('cancels during geometry preflight and never starts mural packaging', async () => {
    const currentPlan = plan()
    const api = transport(currentPlan, {
      startGeometry: vi.fn(async () => ({ job: job('geometry-job', 'running') })),
      fetchJob: vi.fn(async () => job('geometry-job', 'running')),
    })
    render(<MuralBuildWorkflow {...props(currentPlan, api)} pollIntervalMs={10_000} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build 6-plate 3MF' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel build' }))

    await waitFor(() => expect(api.cancel).toHaveBeenCalledWith('geometry-job'))
    expect(api.startMural).not.toHaveBeenCalled()
    expect(await screen.findByText('The mural build was canceled.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Download verified/ })).not.toBeInTheDocument()
  })

  it('shows exact seam QA but no download when Bambu validation is unavailable', async () => {
    const currentPlan = plan()
    const api = transport(currentPlan, {
      fetchResult: vi.fn(async () => result(currentPlan, {
        download_ready: false,
        validation_report: null,
        validation_log: null,
        validation: {
          status: 'unavailable',
          reason: 'Bambu Studio was not detected.',
          attempted: true,
          executable: null,
          version: null,
        },
      })),
    })
    render(<MuralBuildWorkflow {...props(currentPlan, api)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build 6-plate 3MF' }))

    expect(await screen.findByTestId('assembled-seam-qa')).toBeInTheDocument()
    expect(screen.getByText('Bambu Studio was not detected.')).toBeInTheDocument()
    expect(screen.getByText(/Install or repair Bambu Studio/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Download verified/ })).not.toBeInTheDocument()
    expect(screen.getByText('Download 3MF').closest('li')).toHaveAttribute('data-complete', 'false')
  })

  it('offers retry after a preflight failure', async () => {
    const currentPlan = plan()
    const startGeometry = vi.fn()
      .mockRejectedValueOnce(new Error('Geometry worker unavailable.'))
      .mockResolvedValueOnce({ job: job('geometry-job', 'succeeded') })
    const api = transport(currentPlan, { startGeometry })
    render(<MuralBuildWorkflow {...props(currentPlan, api)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build 6-plate 3MF' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Retry build' }))

    expect(await screen.findByRole('link', { name: 'Download verified multi-plate 3MF' })).toBeInTheDocument()
    expect(startGeometry).toHaveBeenCalledTimes(2)
  })

  it('invalidates a completed package after unsaved plan edits', async () => {
    const currentPlan = plan()
    const api = transport(currentPlan)
    const view = render(<MuralBuildWorkflow {...props(currentPlan, api)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build 6-plate 3MF' }))
    await screen.findByRole('link', { name: 'Download verified multi-plate 3MF' })

    view.rerender(<MuralBuildWorkflow {...props(currentPlan, api)} planDirty />)

    await waitFor(() => expect(screen.queryByRole('link', { name: /Download verified/ })).toBeNull())
    expect(screen.getByText('Save the mural plan changes before building.')).toBeInTheDocument()
  })

  it('drops a late preflight response after project replacement', async () => {
    let resolveStart!: (value: { job: JobResource }) => void
    const planA = plan('project-a', 'master-a')
    const api = transport(planA, {
      startGeometry: vi.fn((): Promise<MuralBuildStart> => new Promise((resolve) => {
        resolveStart = resolve
      })),
    })
    const view = render(<MuralBuildWorkflow {...props(planA, api)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Build 6-plate 3MF' }))
    const planB = plan('project-b', 'master-b')
    view.rerender(<MuralBuildWorkflow {...props(planB, api)} />)
    resolveStart({ job: job('geometry-job', 'queued', 'project-a') })

    await waitFor(() => expect(api.cancel).toHaveBeenCalledWith('geometry-job'))
    expect(screen.queryByRole('link', { name: /Download verified/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Build 6-plate 3MF' })).toBeEnabled()
  })

  it('keeps short-viewport layout in normal body flow', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/mural/MuralBuildWorkflow.css'), 'utf8')
    expect(css).toMatch(/@media \(max-width: 540px\), \(max-height: 560px\)/)
    expect(css).not.toMatch(/position:\s*fixed/)
    expect(css).not.toMatch(/overflow:\s*hidden/)
  })
})
