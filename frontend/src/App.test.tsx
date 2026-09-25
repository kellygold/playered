/// <reference types="node" />

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type {
  JobConfigV1,
  JobResource,
  PreviewJobResult,
  ProjectWorkspace,
  RegionOperation,
  RevisionResource,
  ResolvedPrintabilitySettings,
} from './contracts'

const config: JobConfigV1 = {
  schema_version: 1,
  source_asset_id: 'asset_1',
  canvas: { width_mm: 200, height_mm: 200 },
  crop: { mode: 'cover', x: 0, y: 0, width: 1, height: 1 },
  printer: {
    profile_catalog_id: 'image23mf-bundled-printers',
    profile_catalog_version: '2026.07.16',
    printer_id: 'bambu-p2s',
    nozzle_id: 'nozzle-0.4-hardened-steel',
    nozzle_mm: 0.4,
    layer_height_mm: 0.2,
    plate_id: 'textured-pei',
  },
  palette: {
    colors: [
      { id: 'light', name: 'Light', hex: '#FFFFFF', locked: false, filament_id: null },
      { id: 'dark', name: 'Dark', hex: '#000000', locked: false, filament_id: null },
    ],
  },
  cleanup: {
    min_island_mm2: 0.3,
    max_hole_mm2: 0.5,
    smoothing_radius_mm: 0,
    merge_policy: 'review',
    preserve_long_lines: true,
  },
  geometry: {
    style: 'flush_inlay',
    base_thickness_mm: 1.2,
    art_thickness_mm: 0.6,
    corner_radius_mm: 0,
  },
}

const printabilityValues = {
  minimum_island_area_mm2: 0.282743,
  minimum_island_diameter_mm: 0.6,
  maximum_tiny_hole_area_mm2: 0.502655,
  maximum_tiny_hole_diameter_mm: 0.8,
  minimum_ring_width_mm: 0.6,
  minimum_line_width_mm: 0.48,
  minimum_neck_width_mm: 0.6,
  minimum_gap_width_mm: 0.5,
  long_line_minimum_length_mm: 1.6,
  smoothing_radius_mm: 0,
} as const

const resolvedPrintability: ResolvedPrintabilitySettings = {
  profile_id: 'bambu-p2s-0.4-hardened-steel-pla-v1',
  profile_display_name: 'Bambu Lab P2S · 0.4 mm · PLA',
  profile_catalog_fingerprint: '1'.repeat(64),
  evidence_status: 'pending_print_calibration',
  warning: 'Engineering baseline pending a printed calibration coupon.',
  values: Object.entries(printabilityValues).map(([name, value]) => ({
    name: name as keyof typeof printabilityValues,
    value,
    unit: name.includes('area') ? 'mm2' : 'mm',
    source: 'profile',
    profile_value: value,
    profile_basis: 'engineering_baseline',
    profile_confidence: 'provisional',
    rationale: `Rationale for ${name}`,
  })),
}

const queuedJob: JobResource = {
  id: 'job_1',
  project_id: 'project_1',
  revision_id: null,
  type: 'preview',
  state: 'queued',
  stage: 'queued',
  progress: 0,
  request_key: 'a'.repeat(64),
  supersession_key: 'preview:project_1',
  generation: 1,
  failure: null,
  artifact_ids: [],
  created_at: '2026-07-16T00:00:00Z',
  started_at: null,
  finished_at: null,
  canceled_at: null,
}

const succeededJob: JobResource = {
  ...queuedJob,
  state: 'succeeded',
  stage: 'complete',
  progress: 1,
  artifact_ids: ['artifact_metrics', 'artifact_palette', 'artifact_image', 'artifact_stats'],
  started_at: '2026-07-16T00:00:01Z',
  finished_at: '2026-07-16T00:00:02Z',
}

const workspace: ProjectWorkspace = {
  project: {
    id: 'project_1',
    name: 'Processing proof',
    description: '',
    active_revision_id: null,
    preferences: {},
    created_at: '2026-07-16T00:00:00Z',
    updated_at: '2026-07-16T00:00:00Z',
    archived_at: null,
  },
  source_asset: {
    id: 'asset_1',
    sha256: 'b'.repeat(64),
    media_type: 'image/png',
    original_filename: 'art.png',
    byte_size: 100,
    width_px: 800,
    height_px: 500,
    created_at: '2026-07-16T00:00:00Z',
    metadata: {
      schema_version: 1,
      original_filename: 'art.png',
      declared_extension: '.png',
      detected_format: 'png',
      source_media_type: 'image/png',
      source_byte_size: 100,
      source_sha256: 'c'.repeat(64),
      source_width_px: 800,
      source_height_px: 500,
      source_mode: 'RGBA',
      frame_count: 1,
      exif_orientation: 1,
      orientation_applied: false,
      embedded_icc_profile: false,
      source_icc_sha256: null,
      color_conversion: 'assumed-srgb',
      source_had_alpha: true,
      source_had_transparency: false,
      normalized_width_px: 800,
      normalized_height_px: 500,
      normalized_mode: 'RGBA',
      normalized_color_space: 'srgb',
      normalized_media_type: 'image/png',
      normalized_byte_size: 100,
      normalized_sha256: 'b'.repeat(64),
    },
  },
  draft: {
    project_id: 'project_1',
    base_revision_id: null,
    config,
    operations: [],
    config_sha256: 'd'.repeat(64),
    editor_sequence_sha256: '7'.repeat(64),
    generation: 1,
    updated_at: '2026-07-16T00:00:00Z',
    history: {
      lineage_id: 'lineage_1', cursor_node_id: 'node_1', tip_node_id: 'node_1',
      cursor: 0, total: 0, limit: 100, can_undo: false, can_redo: false,
      undo_label: null, redo_label: null, state_sha256: '8'.repeat(64),
    },
  },
  latest_preview_job: null,
}

const preview: PreviewJobResult = {
  job: succeededJob,
  artifacts: [
    {
      id: 'artifact_replay',
      job_id: 'job_1',
      revision_id: null,
      kind: 'editor-replay',
      sha256: '7'.repeat(64),
      derivation_key: 'a'.repeat(64),
      media_type: 'application/json',
      byte_size: 500,
      metadata: { sequence_fingerprint: '7'.repeat(64) },
      download_url: '/api/artifacts/artifact_replay',
      created_at: '2026-07-16T00:00:02Z',
    },
    {
      id: 'artifact_metrics',
      job_id: 'job_1',
      revision_id: null,
      kind: 'palette-metrics',
      sha256: '8'.repeat(64),
      derivation_key: 'a'.repeat(64),
      media_type: 'application/json',
      byte_size: 500,
      metadata: {},
      download_url: '/api/artifacts/artifact_metrics',
      created_at: '2026-07-16T00:00:02Z',
    },
    {
      id: 'artifact_palette',
      job_id: 'job_1',
      revision_id: null,
      kind: 'palette-preview-image',
      sha256: '9'.repeat(64),
      derivation_key: 'a'.repeat(64),
      media_type: 'image/png',
      byte_size: 180,
      metadata: {},
      download_url: '/api/artifacts/artifact_palette',
      created_at: '2026-07-16T00:00:02Z',
    },
    {
      id: 'artifact_image',
      job_id: 'job_1',
      revision_id: null,
      kind: 'preview-image',
      sha256: 'e'.repeat(64),
      derivation_key: 'a'.repeat(64),
      media_type: 'image/png',
      byte_size: 200,
      metadata: {},
      download_url: '/api/artifacts/artifact_image',
      created_at: '2026-07-16T00:00:02Z',
    },
    {
      id: 'artifact_stats',
      job_id: 'job_1',
      revision_id: null,
      kind: 'preview-statistics',
      sha256: 'f'.repeat(64),
      derivation_key: 'a'.repeat(64),
      media_type: 'application/json',
      byte_size: 300,
      metadata: {},
      download_url: '/api/artifacts/artifact_stats',
      created_at: '2026-07-16T00:00:02Z',
    },
  ],
  statistics: {
    schema_version: 1,
    config_sha256: 'd'.repeat(64),
    profile_catalog_fingerprint: '1'.repeat(64),
    source_asset_id: 'asset_1',
    source: { width: 800, height: 500 },
    preview: { width: 1024, height: 1024 },
    physical: { width_mm: 200, height_mm: 200, mm_per_pixel_x: 0.195, mm_per_pixel_y: 0.195 },
    alpha: { opaque_pixels: 100, translucent_pixels: 0, transparent_pixels: 0 },
    transform: {
      schema_version: 1,
      original_size: { width: 800, height: 500 },
      normalized_size: { width: 800, height: 500 },
      exif_orientation: 1,
      crop_rect: { x: 0, y: 0, width: 800, height: 500 },
      fit_mode: 'cover',
      working_size: { width: 1024, height: 1024 },
      canvas_size: { width: 200, height: 200 },
    },
    preview_sha256: 'e'.repeat(64),
  },
  palette_metrics: {
    schema_version: 1,
    config_sha256: 'd'.repeat(64),
    quantization_fingerprint: '2'.repeat(64),
    options_fingerprint: '3'.repeat(64),
    palette: ['#FFFFFF', '#000000'],
    visible_pixel_count: 1_000_000,
    physical_area_mm2: 40_000,
    colors: [
      {
        index: 0,
        color: '#FFFFFF',
        pixel_count: 600_000,
        coverage_ratio: 0.6,
        mean_delta_e: 4.2,
        p95_delta_e: 8.1,
        component_count: 3,
        largest_component_share: 0.9,
        smallest_component_pixels: 20,
        smallest_component_mm2: 0.8,
      },
      {
        index: 1,
        color: '#000000',
        pixel_count: 400_000,
        coverage_ratio: 0.4,
        mean_delta_e: 3.8,
        p95_delta_e: 7.2,
        component_count: 7,
        largest_component_share: 0.8,
        smallest_component_pixels: 2,
        smallest_component_mm2: 0.08,
      },
    ],
    reconstruction: {
      alpha_weighted_mean_delta_e: 4.0,
      alpha_weighted_p95_delta_e: 7.9,
    },
    adjacency: [
      { first_index: 0, second_index: 1, delta_e: 100, boundary_edge_count: 1200 },
    ],
    minimum_adjacent_delta_e: 100,
    fragmentation: {
      component_count: 10,
      excess_component_count: 8,
      single_pixel_component_count: 1,
      components_per_100_mm2: 0.025,
    },
  },
  region_graph: null,
  risk_report: null,
  island_analysis: null,
  clearance_analysis: null,
  hole_analysis: null,
}

type FakeXHROptions = {
  status?: number
  response?: unknown
  responses?: unknown[]
  deferred?: boolean
}

function installXHR(options: FakeXHROptions = {}) {
  const instances: FakeXMLHttpRequest[] = []
  class FakeXMLHttpRequest {
    response: unknown
    responseType = ''
    status = options.status ?? 201
    upload = { onprogress: null as ((event: ProgressEvent) => void) | null }
    onload: ((event: Event) => void) | null = null
    onerror: ((event: Event) => void) | null = null
    onabort: ((event: Event) => void) | null = null
    constructor() {
      this.response = options.responses?.[instances.length] ?? options.response ?? workspace
      instances.push(this)
    }
    open() {}
    setRequestHeader() {}
    send(body: Blob) {
      this.upload.onprogress?.(
        new ProgressEvent('progress', {
          lengthComputable: true,
          loaded: body.size / 2,
          total: body.size,
        }),
      )
      if (!options.deferred) this.onload?.(new Event('load'))
    }
    abort() {
      this.onabort?.(new Event('abort'))
    }
  }
  vi.stubGlobal('XMLHttpRequest', FakeXMLHttpRequest)
  return instances
}

type DraftSaveRequest = {
  config: JobConfigV1
  operations: RegionOperation[]
  expected_draft_generation: number
  history_command?: {
    id: string
    label: string
    command_type: string
    expected_cursor_node_id: string
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function makeReplacementWorkspace() {
  const replacement = structuredClone(workspace)
  replacement.project.id = 'project_2'
  replacement.project.name = 'Replacement proof'
  replacement.project.active_revision_id = null
  replacement.source_asset!.id = 'asset_2'
  replacement.source_asset!.original_filename = 'replacement.png'
  replacement.draft!.project_id = 'project_2'
  replacement.draft!.base_revision_id = null
  replacement.draft!.config.canvas.width_mm = 310
  replacement.latest_preview_job = null
  return replacement
}

function operation(operationType: string, parameters: Record<string, unknown> = {}): RegionOperation {
  return {
    operation_type: operationType,
    selection: {},
    parameters,
    source: 'manual',
    provenance: {},
  }
}

function workspaceWithManualHistory(): ProjectWorkspace {
  const value = structuredClone(workspace)
  value.draft!.operations = [
    operation('palette-edit', { action: 'rename', before_palette: [], after_palette: [] }),
    operation('editor_command_v1'),
    operation('legacy-free-form-brush'),
  ]
  return value
}

type DraftResponder = (
  request: DraftSaveRequest,
  commit: () => Response,
) => Response | Promise<Response>

type RevisionResponder = (response: Response) => Response | Promise<Response>

function installFetch(
  job: JobResource = succeededJob,
  options: {
    initialWorkspace?: ProjectWorkspace
    draftResponder?: DraftResponder
    projectResponder?: () => Response | Promise<Response>
    publishStaleOnce?: boolean
    publishResponder?: RevisionResponder
    branchResponder?: RevisionResponder
    historyMoveResponder?: (
      direction: 'undo' | 'redo',
    ) => Response | Promise<Response> | null
  } = {},
) {
  let currentWorkspace = structuredClone(options.initialWorkspace ?? workspace)
  const historyEntries: {
    beforeConfig: JobConfigV1
    beforeOperations: RegionOperation[]
    afterConfig: JobConfigV1
    afterOperations: RegionOperation[]
    label: string
  }[] = []
  let historyCursor = 0
  let historyLineage = 1
  const historySummary = () => ({
    lineage_id: `lineage_${historyLineage}`,
    cursor_node_id: `lineage_${historyLineage}_node_${historyCursor}`,
    tip_node_id: `lineage_${historyLineage}_node_${historyEntries.length}`,
    cursor: historyCursor,
    total: historyEntries.length,
    limit: 100 as const,
    can_undo: historyCursor > 0,
    can_redo: historyCursor < historyEntries.length,
    undo_label: historyCursor > 0 ? historyEntries[historyCursor - 1].label : null,
    redo_label: historyCursor < historyEntries.length ? historyEntries[historyCursor].label : null,
    state_sha256: String(historyCursor + historyLineage).padStart(64, '0'),
  })
  const resetHistory = () => {
    historyEntries.splice(0)
    historyCursor = 0
    historyLineage += 1
  }
  const revisions: RevisionResource[] = []
  let publishStaleRemaining = options.publishStaleOnce ? 1 : 0
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    if (url === '/api/health') {
      return new Response(
        JSON.stringify({ status: 'ok', version: '0.1.0', workspace: 'workspace', capabilities: [] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    }
    if (url === '/api/printability-profiles/resolve' && init?.method === 'POST') {
      return new Response(JSON.stringify(resolvedPrintability), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.endsWith('/previews') && init?.method === 'POST') {
      const request = JSON.parse(String(init.body)) as { config: JobConfigV1 }
      const draft = {
        ...currentWorkspace.draft!,
        config: request.config,
        generation: currentWorkspace.draft!.generation + 1,
      }
      currentWorkspace = { ...currentWorkspace, draft, latest_preview_job: queuedJob }
      return new Response(
        JSON.stringify({
          draft,
          job: queuedJob,
        }),
        { status: 202, headers: { 'Content-Type': 'application/json' } },
      )
    }
    if (url.endsWith('/geometry') && init?.method === 'POST') {
      return new Response(JSON.stringify({
        job: {
          ...succeededJob,
          id: 'geometry_job_1',
          type: 'geometry',
          artifact_ids: ['geometry_ir_1', 'geometry_preview_1'],
        },
      }), { status: 202, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/geometry/geometry_job_1')) {
      const geometryIr = {
        ...preview.artifacts[0],
        id: 'geometry_ir_1',
        job_id: 'geometry_job_1',
        kind: 'geometry-ir',
        media_type: 'application/json',
        sha256: '3'.repeat(64),
        download_url: '/api/artifacts/geometry_ir_1',
      }
      const geometryPreview = {
        ...preview.artifacts[0],
        id: 'geometry_preview_1',
        job_id: 'geometry_job_1',
        kind: 'geometry-preview',
        media_type: 'image/png',
        sha256: '4'.repeat(64),
        download_url: '/api/artifacts/geometry_preview_1',
      }
      return new Response(JSON.stringify({
        job: { ...succeededJob, id: 'geometry_job_1', type: 'geometry' },
        artifacts: [geometryIr, geometryPreview],
        geometry_svg: null,
        geometry_ir: geometryIr,
        geometry_mesh: null,
        geometry_report: null,
        geometry_preview: geometryPreview,
        export_ready: true,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/exports') && init?.method === 'POST') {
      return new Response(JSON.stringify({
        job: { ...succeededJob, id: 'export_job_1', type: 'export' },
      }), { status: 202, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/exports/export_job_1')) {
      const packageArtifact = {
        ...preview.artifacts[0],
        id: 'package_1',
        job_id: 'export_job_1',
        kind: '3mf-package',
        media_type: 'model/3mf',
        sha256: '5'.repeat(64),
        download_url: '/api/artifacts/package_1',
      }
      const qualityReport = {
        ...preview.artifacts[0],
        id: 'quality_1',
        job_id: 'export_job_1',
        kind: 'geometry-quality-report',
        download_url: '/api/artifacts/quality_1',
      }
      const validationReport = {
        ...preview.artifacts[0],
        id: 'validation_1',
        job_id: 'export_job_1',
        kind: 'bambu-validation-report',
        metadata: { status: 'validated', manual_print_gate: 'not_observed' },
        download_url: '/api/artifacts/validation_1',
      }
      const validationLog = {
        ...preview.artifacts[0],
        id: 'validation_log_1',
        job_id: 'export_job_1',
        kind: 'bambu-validation-log',
        media_type: 'text/plain',
        download_url: '/api/artifacts/validation_log_1',
      }
      return new Response(JSON.stringify({
        job: { ...succeededJob, id: 'export_job_1', type: 'export' },
        artifacts: [packageArtifact, qualityReport, validationReport, validationLog, {
          ...preview.artifacts[0], id: 'slice_preview_1', job_id: 'export_job_1',
          kind: 'bambu-slicer-preview-image', media_type: 'image/png',
          download_url: '/api/artifacts/slice_preview_1', sha256: '6'.repeat(64),
        }],
        quality_report: qualityReport,
        validation_report: validationReport,
        validation_log: validationLog,
        package: packageArtifact,
        download_ready: true,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/draft') && init?.method === 'PUT') {
      const request = JSON.parse(String(init.body)) as DraftSaveRequest
      const commit = () => {
        if (request.history_command) {
          historyEntries.splice(historyCursor)
          historyEntries.push({
            beforeConfig: structuredClone(currentWorkspace.draft!.config),
            beforeOperations: structuredClone(currentWorkspace.draft!.operations),
            afterConfig: structuredClone(request.config),
            afterOperations: structuredClone(request.operations),
            label: request.history_command.label,
          })
          historyCursor += 1
        }
        const draft = {
          ...currentWorkspace.draft!,
          config: request.config,
          operations: request.operations,
          generation: currentWorkspace.draft!.generation + 1,
          history: request.history_command ? historySummary() : currentWorkspace.draft!.history,
        }
        currentWorkspace = { ...currentWorkspace, draft }
        return new Response(JSON.stringify(draft), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return options.draftResponder ? options.draftResponder(request, commit) : commit()
    }
    const historyDirection = url.endsWith('/draft/history/undo')
      ? 'undo'
      : url.endsWith('/draft/history/redo')
        ? 'redo'
        : null
    if (historyDirection && init?.method === 'POST') {
      const intercepted = options.historyMoveResponder?.(historyDirection)
      if (intercepted) return intercepted
      const entry = historyDirection === 'undo'
        ? historyEntries[historyCursor - 1]
        : historyEntries[historyCursor]
      if (!entry) throw new Error(`No ${historyDirection} entry in test history.`)
      historyCursor += historyDirection === 'undo' ? -1 : 1
      const draft = {
        ...currentWorkspace.draft!,
        config: structuredClone(historyDirection === 'undo' ? entry.beforeConfig : entry.afterConfig),
        operations: structuredClone(
          historyDirection === 'undo' ? entry.beforeOperations : entry.afterOperations,
        ),
        generation: currentWorkspace.draft!.generation + 1,
        history: historySummary(),
      }
      currentWorkspace = { ...currentWorkspace, draft }
      return new Response(JSON.stringify(draft), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.includes('/revisions?') && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify({
        items: revisions.map((revision) => ({
          id: revision.id,
          project_id: revision.project_id,
          parent_revision_id: revision.parent_revision_id,
          label: revision.label,
          config_sha256: revision.config_sha256,
          editor_sequence_sha256: revision.editor_sequence_sha256,
          preview_evidence: revision.preview_evidence,
          published_at: revision.published_at,
          operation_count: revision.operations.length,
          artifact_count: revision.artifacts.length,
          is_active: revision.id === currentWorkspace.project.active_revision_id,
        })),
        total: revisions.length,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/revisions') && init?.method === 'POST') {
      const request = JSON.parse(String(init.body)) as {
        label: string
        notes: string
        expected_draft_generation: number
        preview_job_id: string | null
      }
      if (publishStaleRemaining > 0) {
        publishStaleRemaining -= 1
        currentWorkspace = {
          ...currentWorkspace,
          draft: {
            ...currentWorkspace.draft!,
            generation: currentWorkspace.draft!.generation + 1,
          },
        }
        return new Response(JSON.stringify({
          error: {
            code: 'stale_draft',
            message: 'Expected draft generation no longer matches.',
            request_id: 'revision-stale',
            retryable: true,
            details: {},
          },
        }), { status: 409, headers: { 'Content-Type': 'application/json' } })
      }
      const revision: RevisionResource = {
        id: `revision_${revisions.length + 1}`,
        project_id: currentWorkspace.project.id,
        source_asset_id: currentWorkspace.source_asset!.id,
        parent_revision_id: currentWorkspace.draft!.base_revision_id,
        schema_version: 1,
        engine_version: '0.1.0',
        config: currentWorkspace.draft!.config,
        config_sha256: currentWorkspace.draft!.config_sha256,
        editor_sequence_sha256: currentWorkspace.draft!.editor_sequence_sha256!,
        label: request.label,
        notes: request.notes,
        operations: currentWorkspace.draft!.operations,
        artifacts: preview.artifacts,
        preview_evidence: {
          status: request.preview_job_id ? 'fresh' : 'missing',
          preview_job_id: request.preview_job_id,
          reason: request.preview_job_id ? 'Exact current preview was copied.' : 'No preview supplied.',
          source_draft_generation: request.expected_draft_generation,
          editor_sequence_sha256: currentWorkspace.draft!.editor_sequence_sha256!,
          artifact_count: request.preview_job_id ? preview.artifacts.length : 0,
        },
        published_at: '2026-07-16T01:00:00Z',
      }
      revisions.unshift(revision)
      const project = { ...currentWorkspace.project, active_revision_id: revision.id }
      const draft = {
        ...currentWorkspace.draft!,
        base_revision_id: revision.id,
        generation: currentWorkspace.draft!.generation + 1,
      }
      resetHistory()
      draft.history = historySummary()
      currentWorkspace = { ...currentWorkspace, project, draft }
      const publicationResponse = new Response(JSON.stringify({ revision, project, draft }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      })
      return options.publishResponder
        ? options.publishResponder(publicationResponse)
        : publicationResponse
    }
    const revisionDetail = revisions.find((revision) => url.endsWith(`/revisions/${revision.id}`))
    if (revisionDetail && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify(revisionDetail), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    const branchBase = revisions.find((revision) => url.endsWith(`/revisions/${revision.id}/branch`))
    if (branchBase && init?.method === 'POST') {
      const project = currentWorkspace.project
      const draft = {
        ...currentWorkspace.draft!,
        base_revision_id: branchBase.id,
        config: branchBase.config,
        operations: branchBase.operations,
        config_sha256: branchBase.config_sha256,
        editor_sequence_sha256: branchBase.editor_sequence_sha256,
        generation: currentWorkspace.draft!.generation + 1,
      }
      resetHistory()
      draft.history = historySummary()
      currentWorkspace = { ...currentWorkspace, project, draft }
      const branchResponse = new Response(JSON.stringify({ base_revision: branchBase, project, draft }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
      return options.branchResponder ? options.branchResponder(branchResponse) : branchResponse
    }
    if (url === '/api/filaments?owned=true') {
      return new Response(JSON.stringify({ items: [], total: 0 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url === '/api/artifacts/validation_1') {
      return new Response(JSON.stringify({
        result: { warnings: [] },
        slicer_report: {
          warnings: [{
            category: 'floating_region',
            message: 'A floating region was retained in slicer evidence.',
          }],
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/mural-plan') && (!init?.method || init.method === 'GET')) {
      return new Response(JSON.stringify({
        error: { code: 'not_found', message: 'No saved mural plan.' },
      }), { status: 404, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/mural-plan/preview') && init?.method === 'POST') {
      const request = JSON.parse(String(init.body)) as {
        layout: {
          rows: number
          columns: number
          panel_width_mm: number
          panel_height_mm: number
          horizontal_gap_mm: number
          vertical_gap_mm: number
          bleed_mm: number
          orientation: string
        }
        edge_clearance_mm: number
        reserved_rectangles: unknown[]
      }
      const masterWidth = request.layout.columns * request.layout.panel_width_mm
      const masterHeight = request.layout.rows * request.layout.panel_height_mm
      const outputWidth = request.layout.panel_width_mm + request.layout.bleed_mm * 2
      const outputHeight = request.layout.panel_height_mm + request.layout.bleed_mm * 2
      return new Response(JSON.stringify({
        request: {
          schema_version: 1,
          source: {
            source_asset_id: 'asset_1', source_asset_sha256: 'a'.repeat(64),
            processed_artifact_id: 'artifact_palette', processed_artifact_sha256: '9'.repeat(64),
            processed_size: { width: 1024, height: 1024 }, canonical_transform: {},
            config_sha256: 'd'.repeat(64), engine_version: '0.1.0',
            revision_id: null, draft_generation: 1,
          },
          layout: request.layout,
          bed: {
            printer_id: 'bambu-p2s', plate_id: 'textured-pei',
            profile_catalog_fingerprint: '1'.repeat(64), width_mm: 256, height_mm: 256,
            edge_clearance_mm: request.edge_clearance_mm, excluded_rectangles: [],
          },
          reserved_rectangles: request.reserved_rectangles,
        },
        plan: {
          schema_version: 1, request_fingerprint: '2'.repeat(64), source_fingerprint: '3'.repeat(64),
          master_transform: {},
          master_size_mm: { width: masterWidth, height: masterHeight },
          assembled_size_mm: {
            width: masterWidth + (request.layout.columns - 1) * request.layout.horizontal_gap_mm,
            height: masterHeight + (request.layout.rows - 1) * request.layout.vertical_gap_mm,
          },
          tiles: Array.from({ length: request.layout.rows * request.layout.columns }, (_, index) => ({
            id: `tile-${index + 1}`,
            output_size_mm: { width: outputWidth, height: outputHeight },
            bed_fit: { fits: true, rotation_degrees: 0 },
          })),
          all_tiles_fit: true,
          warnings: [],
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/result')) {
      return new Response(JSON.stringify(preview), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.endsWith('/cancel') && init?.method === 'POST') {
      return new Response(
        JSON.stringify({
          ...queuedJob,
          state: 'canceled',
          stage: 'canceled',
          finished_at: '2026-07-16T00:00:02Z',
          canceled_at: '2026-07-16T00:00:02Z',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    }
    if (url.includes('/api/jobs/')) {
      return new Response(JSON.stringify(job), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url === '/api/projects/project_1') {
      if (options.projectResponder) return options.projectResponder()
      return new Response(JSON.stringify(currentWorkspace), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    throw new Error(`Unexpected fetch: ${url}`)
  })
}

function pngFile(name = 'art.png') {
  return new File(['png-bytes'], name, { type: 'image/png' })
}

async function importThroughPicker(file = pngFile()) {
  const input = screen.getByLabelText('Choose source image')
  fireEvent.change(input, { target: { files: [file] } })
  expect(await screen.findByRole('heading', { name: 'Processing proof' })).toBeInTheDocument()
}

beforeEach(() => {
  window.localStorage.clear()
  vi.stubGlobal('URL', {
    createObjectURL: vi.fn(() => 'blob:source'),
    revokeObjectURL: vi.fn(),
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Image Lab app', () => {
  it('reports local readiness and exposes the polished import entry points', async () => {
    installFetch()
    render(<App />)

    expect(await screen.findByText('Local engine · v0.1.0')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Turn artwork into geometry you can trust.' }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Choose an image' })).toBeEnabled()
    expect(screen.getByLabelText('Choose source image')).toHaveAttribute('tabindex', '-1')
    expect(screen.getByText('Drop an image anywhere')).toBeInTheDocument()
    expect(screen.getByText('or paste directly from your clipboard')).toBeInTheDocument()
  })

  it('opens and closes the calibration review center from the global navigation', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url === '/api/health') {
        return new Response(JSON.stringify({
          status: 'ok', version: '0.1.0', workspace: 'workspace', capabilities: [],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      if (url === '/api/printability-profile-catalogs') {
        return new Response(JSON.stringify({ items: [], active_fingerprint: 'a'.repeat(64) }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url === '/api/calibration/drafts') {
        return new Response(JSON.stringify({ items: [], total: 0 }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url === '/api/calibration/runs') {
        return new Response(JSON.stringify({ items: [], total: 0 }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url === '/api/calibration/proposals') {
        return new Response(JSON.stringify({ items: [] }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      throw new Error(`Unexpected calibration navigation fetch: ${url}`)
    })
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: 'Calibration' }))
    expect(await screen.findByRole('heading', { name: 'Calibration review center' }))
      .toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Turn artwork into geometry you can trust.' }))
      .not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Back to studio' }))
    expect(await screen.findByRole('heading', { name: 'Turn artwork into geometry you can trust.' }))
      .toBeInTheDocument()
  })

  it('resolves a restored draft against its exact retained calibration catalog', async () => {
    const pinnedWorkspace = structuredClone(workspace)
    pinnedWorkspace.draft!.config.cleanup.printability_profile_catalog_fingerprint =
      'a'.repeat(64)
    window.localStorage.setItem('image23mf.recentProjectId', 'project_1')
    const fetchMock = installFetch(succeededJob, { initialWorkspace: pinnedWorkspace })

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Processing proof' })).toBeInTheDocument()
    await waitFor(() => {
      const resolveCalls = fetchMock.mock.calls.filter(
        ([input]) => String(input) === '/api/printability-profiles/resolve',
      )
      expect(resolveCalls.some(([, init]) => {
        const request = JSON.parse(String(init?.body)) as Record<string, unknown>
        return request.catalog_fingerprint === 'a'.repeat(64)
      })).toBe(true)
    })
  })

  it('makes startup recovery modal, focus-restoring, and cleanup read-only', async () => {
    const recovery = {
      id: 'reconciliation_1',
      status: 'attention',
      started_at: '2026-07-17T00:00:00Z',
      completed_at: '2026-07-17T00:00:01Z',
      interrupted_jobs: [
        {
          job_id: 'job_interrupted',
          project_id: 'project_1',
          type: 'preview',
          state_before_restart: 'running',
          stage_before_restart: 'quantizing',
          state_after_recovery: 'failed',
          disposition: 'retry_required',
          resumable: false,
          retryable: true,
          action: 'Render the preview again from the recovered draft.',
        },
      ],
      stale_temp_file_count: 1,
      orphan_file_count: 0,
      database_integrity: true,
      foreign_key_integrity: true,
      automatic_deletions: 0,
      recommended_actions: ['Review the interrupted job.'],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/health') {
        return new Response(JSON.stringify({
          status: 'ok', version: '0.1.0', workspace: 'workspace', capabilities: [],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      if (url === '/api/workspace/recovery/latest') {
        return new Response(JSON.stringify(recovery), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url === '/api/workspace/gc/plan?minimum_age_seconds=86400') {
        return new Response(JSON.stringify({
          generated_at: '2026-07-17T00:01:00Z',
          minimum_age_seconds: 86400,
          plan_sha256: 'a'.repeat(64),
          candidate_count: 1,
          reclaimable_bytes: 7,
          candidates: [{
            relative_path: 'temp/abandoned.tmp', namespace: 'temp', reason: 'stale_temp',
            byte_size: 7, sha256: 'b'.repeat(64), mtime_ns: 1,
          }],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      if (url === '/api/workspace/recovery/reconcile' && init?.method === 'POST') {
        return new Response(JSON.stringify(recovery), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      throw new Error(`Unexpected recovery test fetch: ${url}`)
    })
    render(<App />)

    const review = await screen.findByRole('button', { name: 'View details' })
    review.focus()
    fireEvent.click(review)
    const dialog = await screen.findByRole('dialog', {
      name: 'Some work was interrupted',
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Close recovery details' })).toHaveFocus(),
    )
    expect(document.querySelector('.app-shell')).toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('hidden')

    fireEvent.click(screen.getByRole('button', { name: 'Review cleanup dry run' }))
    expect(await screen.findByRole('heading', { name: 'Cleanup dry run' })).toBeInTheDocument()
    expect(dialog).toHaveTextContent('Nothing has been deleted')
    expect(dialog).toHaveTextContent('temp/abandoned.tmp')

    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(document.querySelector('.app-shell')).not.toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('')
    await waitFor(() => expect(review).toHaveFocus())
  })

  it('makes an active upload keyboard-modal, safely cancelable, and focus-restoring', async () => {
    installXHR({ deferred: true })
    installFetch()
    render(<App />)
    const choose = screen.getByRole('button', { name: 'Choose an image' })
    choose.focus()
    fireEvent.click(choose)
    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('slow-upload.png')] },
    })

    const dialog = await screen.findByRole('dialog', { name: 'Importing image' })
    const cancel = screen.getByRole('button', { name: 'Cancel import' })
    await waitFor(() => expect(cancel).toHaveFocus())
    expect(document.querySelector('.app-shell')).toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('hidden')

    fireEvent.keyDown(cancel, { key: 'Tab' })
    expect(cancel).toHaveFocus()
    fireEvent.keyDown(dialog, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Importing image' })).not.toBeInTheDocument())
    expect(document.querySelector('.app-shell')).not.toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('')
    await waitFor(() => expect(choose).toHaveFocus())
  })

  it.each(['picker', 'drop', 'paste'])('imports through %s and renders a real preview', async (path) => {
    installXHR()
    installFetch()
    render(<App />)
    const file = pngFile(`${path}.png`)

    if (path === 'picker') {
      fireEvent.change(screen.getByLabelText('Choose source image'), {
        target: { files: [file] },
      })
    } else if (path === 'drop') {
      fireEvent.drop(screen.getByRole('main'), { dataTransfer: { files: [file] } })
    } else {
      fireEvent.paste(window, { clipboardData: { files: [file] } })
    }

    expect(await screen.findByRole('heading', { name: 'Processing proof' })).toBeInTheDocument()
    expect(await screen.findByAltText('Processed canvas view')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Mural tile planner' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Multi-plate mural 3MF' })).toBeInTheDocument()
    expect(screen.getByText('3 × 2 · 6 plates')).toBeInTheDocument()
    expect(screen.getAllByText('Current')).toHaveLength(2)
    expect(screen.getByRole('heading', { name: 'Preview ready to build a 3MF' })).toBeInTheDocument()
    const outputStages = screen.getByRole('list', { name: 'Output stages' })
    for (const stage of ['Preview', 'Geometry', '3MF package', 'Slicer validation', 'Download']) {
      expect(outputStages).toHaveTextContent(stage)
    }
    fireEvent.click(screen.getByText('Build details'))
    expect(screen.getByText('Build printable regions from this exact preview.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Build 3MF' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Workflow: Build 3MF' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Build multi-plate 3MF' })).toBeDisabled()
    expect(screen.getByText('Save the current mural plan before building.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /download 3mf/i })).not.toBeInTheDocument()
  })

  it('generates geometry, exports a validated 3MF, and downloads the exact package', async () => {
    installXHR()
    const fetch = installFetch()
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.click(screen.getByRole('button', { name: 'Workflow: Build 3MF' }))

    const downloadButton = await screen.findByRole('button', { name: 'Workflow: Download verified 3MF' })
    expect(screen.getByRole('button', { name: 'Workflow: Download verified 3MF' })).toBeEnabled()
    expect(screen.getByRole('heading', { name: 'Validated 3MF ready' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Geometry' })).toHaveAttribute('aria-disabled', 'false')
    const geometryRequest = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/geometry') && init?.method === 'POST',
    )
    const exportRequest = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/exports') && init?.method === 'POST',
    )
    expect(JSON.parse(String(geometryRequest?.[1]?.body))).toMatchObject({
      schema_version: 1,
      preview_job_id: 'job_1',
      expected_draft_generation: 2,
    })
    expect(JSON.parse(String(exportRequest?.[1]?.body))).toMatchObject({
      geometry_artifact_id: 'geometry_ir_1',
      geometry_sha256: '3'.repeat(64),
      profile: {
        printer_model: 'Bambu Lab P2S',
        nozzle_diameter_mm: 0.4,
        layer_height_mm: 0.2,
        bed_type: 'Textured PEI Plate',
      },
      minimum_part_thickness_mm: 0.6,
    })

    fireEvent.click(downloadButton)
    expect(download).toHaveBeenCalledOnce()
    expect(screen.getByRole('link', { name: 'Download 3MF package' }))
      .toHaveAttribute('href', '/api/artifacts/package_1')
    expect(screen.getByText('5'.repeat(64))).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open geometry quality report (JSON)' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open slicer validation report (JSON)' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open retained validation log (text)' })).toBeVisible()
    expect(await screen.findByText('A floating region was retained in slicer evidence.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Sliced' })).toHaveAttribute('aria-disabled', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'Sliced' }))
    expect(screen.getByRole('img', { name: 'Sliced canvas view' })).toHaveAttribute(
      'src', expect.stringContaining('/api/artifacts/slice_preview_1'),
    )
    fireEvent.change(screen.getByLabelText('Width crop percent'), { target: { value: '80' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sliced' }))
      .toHaveAttribute('aria-disabled', 'true'))
    expect(screen.queryByRole('img', { name: 'Sliced canvas view' })).not.toBeInTheDocument()

  })

  it('supports crop modes, numeric editing, and keyboard movement', async () => {
    installXHR()
    const fetch = installFetch()
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    for (const name of [
      'Contain: Keep the crop; allow clear margins.',
      'Stretch: Force the crop to the plate shape.',
      'Extend: Preserve scale on a clear canvas.',
      'Cover: Fill the plate; crop overflow.',
    ]) {
      fireEvent.click(screen.getByRole('button', { name }))
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-pressed', 'true')
    }
    fireEvent.change(screen.getByLabelText('Width crop percent'), { target: { value: '80' } })
    fireEvent.keyDown(screen.getByRole('application', { name: 'Crop selection' }), { key: 'ArrowRight' })

    expect(screen.getByLabelText('Left crop percent')).toHaveValue(0.5)
    expect(screen.getAllByText('Changes not rendered')).not.toHaveLength(0)
    await waitFor(() =>
      expect(
        fetch.mock.calls.filter(
          ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
        ),
      ).toHaveLength(6),
    )
    const autosaves = fetch.mock.calls.filter(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )
    const autosave = autosaves.at(-1)!
    const saved = JSON.parse(String(autosave[1]?.body)) as { config: JobConfigV1 }
    expect(saved.config.crop.x).toBe(0.005)
    expect(saved.config.crop.width).toBe(0.8)
  })

  it('serializes rapid mixed edits and persists the newest complete draft', async () => {
    installXHR()
    const pending: {
      request: DraftSaveRequest
      commit: () => Response
      resolve: (response: Response) => void
    }[] = []
    const fetch = installFetch(succeededJob, {
      draftResponder: (request, commit) =>
        new Promise<Response>((resolve) => pending.push({ request, commit, resolve })),
    })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.change(screen.getByLabelText('Width crop percent'), { target: { value: '80' } })
    await waitFor(() => expect(pending).toHaveLength(1))

    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '220' },
    })
    const name = screen.getByLabelText('Name for color 1')
    fireEvent.change(name, { target: { value: 'Latest cream' } })
    fireEvent.blur(name)

    expect(pending).toHaveLength(1)
    await act(async () => pending[0].resolve(pending[0].commit()))
    await waitFor(() => expect(pending).toHaveLength(2))

    const canvasStep = pending[1].request
    expect(canvasStep.expected_draft_generation).toBe(
      pending[0].request.expected_draft_generation + 1,
    )
    expect(canvasStep.history_command?.label).toBe('Resize canvas')
    expect(canvasStep.config.palette.colors[0].name).toBe('Light')
    await act(async () => pending[1].resolve(pending[1].commit()))
    await waitFor(() => expect(pending).toHaveLength(3))

    const newest = pending[2].request
    expect(newest.expected_draft_generation).toBe(
      pending[1].request.expected_draft_generation + 1,
    )
    expect(newest.config.crop.width).toBe(0.8)
    expect(newest.config.canvas).toEqual({ width_mm: 220, height_mm: 220 })
    expect(newest.config.palette.colors[0].name).toBe('Latest cream')
    expect(newest.operations).toHaveLength(1)
    expect(newest.history_command?.label).toBe('Rename palette color')
    await act(async () => pending[2].resolve(pending[2].commit()))

    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    const saves = fetch.mock.calls.filter(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )
    expect(saves).toHaveLength(3)
  })

  it('reports autosave failure truthfully and retries the exact latest draft to recovery', async () => {
    installXHR()
    const attempts: DraftSaveRequest[] = []
    installFetch(succeededJob, {
      draftResponder: (request, commit) => {
        attempts.push(structuredClone(request))
        if (attempts.length === 1) {
          return new Response(
            JSON.stringify({
              error: {
                code: 'internal_error',
                message: 'Temporary draft failure.',
                request_id: 'save-proof',
                retryable: true,
                details: {},
              },
            }),
            { status: 503, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return commit()
      },
    })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.change(screen.getByLabelText('Width crop percent'), { target: { value: '72' } })

    expect(await screen.findByText('Draft save needs attention')).toBeInTheDocument()
    expect(screen.queryByText('Draft saved locally')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Temporary draft failure.')
    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))

    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Retry save' })).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(attempts).toHaveLength(2)
    expect(attempts[1]).toEqual(attempts[0])
  })

  it.each(['crop', 'canvas', 'cleanup', 'palette'] as const)(
    'guards the %s configuration pathway when manual edits are selector-bound',
    async (path) => {
      const withHistory = workspaceWithManualHistory()
      installXHR({ response: withHistory })
      const fetch = installFetch(succeededJob, { initialWorkspace: withHistory })
      render(<App />)
      await importThroughPicker()
      await screen.findByAltText('Processed canvas view')

      if (path === 'crop') {
        fireEvent.change(screen.getByLabelText('Width crop percent'), {
          target: { value: '80' },
        })
      } else if (path === 'canvas') {
        fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
          target: { value: '220' },
        })
      } else if (path === 'cleanup') {
        fireEvent.change(screen.getByLabelText('Automatic island policy'), {
          target: { value: 'dominant_neighbor' },
        })
      } else {
        const name = screen.getByLabelText('Name for color 1')
        fireEvent.change(name, { target: { value: 'Guarded cream' } })
        fireEvent.blur(name)
      }

      const dialog = await screen.findByRole('alertdialog', {
        name: 'Change settings and clear manual edits?',
      })
      expect(dialog).toHaveTextContent('2 manual edits will be cleared')
      expect(dialog).toHaveTextContent(
        path === 'palette'
          ? '2 compatible palette steps stay in history'
          : '1 compatible palette step stays in history',
      )
      expect(
        fetch.mock.calls.filter(
          ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
        ),
      ).toHaveLength(0)
    },
  )

  it('keeps cancellation non-destructive, restores focus, and applies config plus cleared history atomically', async () => {
    const withHistory = workspaceWithManualHistory()
    installXHR({ response: withHistory })
    const fetch = installFetch(succeededJob, { initialWorkspace: withHistory })
    const view = render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')
    const width = screen.getByLabelText('Canvas width millimetres')

    width.focus()
    fireEvent.change(width, { target: { value: '220' } })
    const firstDialog = await screen.findByRole('alertdialog', {
      name: 'Change settings and clear manual edits?',
    })
    expect(screen.getByRole('button', { name: 'Keep current settings' })).toHaveFocus()
    fireEvent.keyDown(firstDialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(width).toHaveValue(200)
    expect(width).toHaveFocus()
    expect(
      fetch.mock.calls.filter(
        ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
      ),
    ).toHaveLength(0)

    fireEvent.change(width, { target: { value: '220' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear 2 manual edits and apply' }))
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    expect(width).toHaveValue(220)

    const saves = fetch.mock.calls.filter(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )
    expect(saves).toHaveLength(1)
    const accepted = JSON.parse(String(saves[0][1]?.body)) as DraftSaveRequest
    expect(accepted.config.canvas).toEqual({ width_mm: 220, height_mm: 220 })
    expect(accepted.operations.map((item) => item.operation_type)).toEqual(['palette-edit'])

    view.unmount()
    render(<App />)
    expect(await screen.findByLabelText('Canvas width millimetres')).toHaveValue(220)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('allows palette-only history to follow a config change without a confirmation', async () => {
    const paletteOnly = structuredClone(workspace)
    paletteOnly.draft!.operations = [operation('palette-edit')]
    installXHR({ response: paletteOnly })
    const fetch = installFetch(succeededJob, { initialWorkspace: paletteOnly })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '240' },
    })
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(
        fetch.mock.calls.filter(
          ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
        ),
      ).toHaveLength(1),
    )
    const save = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )!
    const request = JSON.parse(String(save[1]?.body)) as DraftSaveRequest
    expect(request.operations.map((item) => item.operation_type)).toEqual(['palette-edit'])
  })

  it('does not prompt or save when a guarded control reports the unchanged config', async () => {
    const withHistory = workspaceWithManualHistory()
    installXHR({ response: withHistory })
    const fetch = installFetch(succeededJob, { initialWorkspace: withHistory })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '200' },
    })
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(
      fetch.mock.calls.filter(
        ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
      ),
    ).toHaveLength(0)
  })

  it('does not let an accepted clear from the previous project replace a newly imported workspace', async () => {
    const withHistory = workspaceWithManualHistory()
    const replacementWorkspace = makeReplacementWorkspace()
    const pending: {
      commit: () => Response
      resolve: (response: Response) => void
    }[] = []
    installXHR({ responses: [withHistory, replacementWorkspace] })
    installFetch(succeededJob, {
      initialWorkspace: withHistory,
      draftResponder: (_request, commit) =>
        new Promise<Response>((resolve) => pending.push({ commit, resolve })),
    })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '220' },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear 2 manual edits and apply' }))
    await waitFor(() => expect(pending).toHaveLength(1))

    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('replacement.png')] },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Import replacement' }))
    expect(await screen.findByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument()
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(310)

    await act(async () => pending[0].resolve(pending[0].commit()))
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument(),
    )
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(310)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('surfaces a non-retryable history guard without blindly replaying the invalid snapshot', async () => {
    const withHistory = workspaceWithManualHistory()
    const attempts: DraftSaveRequest[] = []
    installXHR({ response: withHistory })
    installFetch(succeededJob, {
      initialWorkspace: withHistory,
      draftResponder: (request, commit) => {
        attempts.push(structuredClone(request))
        if (attempts.length === 1) {
          return new Response(JSON.stringify({
            error: {
              code: 'incompatible_editor_history',
              message: 'The draft configuration changed while bound manual edits remained.',
              request_id: 'history-guard',
              retryable: false,
              details: { operation_count: 2, action: 'clear_or_restore_config' },
            },
          }), { status: 422, headers: { 'Content-Type': 'application/json' } })
        }
        return commit()
      },
    })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '225' },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear 2 manual edits and apply' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Those settings cannot reuse the current manual edits',
    )
    expect(screen.getByRole('alert')).toHaveTextContent(
      'explicitly clear the bound manual edits',
    )
    expect(screen.getByText('Draft save needs attention')).toBeInTheDocument()
    expect(attempts[0].operations.map((item) => item.operation_type)).toEqual(['palette-edit'])

    expect(screen.queryByRole('button', { name: 'Retry save' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Undo' })).toHaveAccessibleDescription(
      'Correct the invalid settings before undo or redo',
    )
    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '230' },
    })
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    expect(attempts).toHaveLength(2)
    expect(attempts[1].config.canvas.width_mm).toBe(230)
    expect(attempts[1].history_command?.id).not.toBe(attempts[0].history_command?.id)
  })

  it('reopens autosaved crop and canvas edits without rendering another preview', async () => {
    installXHR()
    const fetch = installFetch()
    const view = render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')
    const initialPreviewCalls = fetch.mock.calls.filter(
      ([url, init]) => String(url).endsWith('/previews') && init?.method === 'POST',
    ).length

    fireEvent.change(screen.getByLabelText('Width crop percent'), { target: { value: '75' } })
    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '240' },
    })
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    await waitFor(() =>
      expect(
        fetch.mock.calls.filter(
          ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
        ),
      ).toHaveLength(1),
    )
    expect(
      fetch.mock.calls.filter(
        ([url, init]) => String(url).endsWith('/previews') && init?.method === 'POST',
      ),
    ).toHaveLength(initialPreviewCalls)

    view.unmount()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Processing proof' })).toBeInTheDocument()
    expect(screen.getByLabelText('Width crop percent')).toHaveValue(75)
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(240)
    expect(screen.getByLabelText('Canvas height millimetres')).toHaveValue(240)
    expect(
      fetch.mock.calls.filter(
        ([url, init]) => String(url).endsWith('/previews') && init?.method === 'POST',
      ),
    ).toHaveLength(initialPreviewCalls)
  })

  it('keeps a fresh import when a delayed recent-project restore finishes afterward', async () => {
    const freshWorkspace = structuredClone(workspace)
    freshWorkspace.project.id = 'project_2'
    freshWorkspace.project.name = 'Fresh import wins'
    freshWorkspace.source_asset!.id = 'asset_2'
    freshWorkspace.source_asset!.original_filename = 'fresh.png'
    freshWorkspace.draft!.project_id = 'project_2'
    freshWorkspace.latest_preview_job = null
    installXHR({ response: freshWorkspace })

    let finishRestore: ((response: Response) => void) | null = null
    installFetch(succeededJob, {
      projectResponder: () =>
        new Promise<Response>((resolve) => {
          finishRestore = resolve
        }),
    })
    window.localStorage.setItem('image23mf.recentProjectId', 'project_1')
    render(<App />)
    await waitFor(() => expect(finishRestore).not.toBeNull())

    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('fresh.png')] },
    })
    expect(await screen.findByRole('heading', { name: 'Fresh import wins' })).toBeInTheDocument()

    await act(async () => {
      finishRestore?.(
        new Response(JSON.stringify(workspace), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Fresh import wins' })).toBeInTheDocument(),
    )
    expect(window.localStorage.getItem('image23mf.recentProjectId')).toBe('project_2')
  })

  it('previews cleanup sliders provisionally and persists one physical override on commit', async () => {
    installXHR()
    const fetch = installFetch()
    render(<App />)
    await importThroughPicker()
    const slider = await screen.findByRole('slider', { name: 'Tiny islands slider' })

    fireEvent.change(slider, { target: { value: '0.4' } })
    expect(screen.getByLabelText('Island threshold')).toHaveValue(0.4)
    expect(
      fetch.mock.calls.filter(
        ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
      ),
    ).toHaveLength(0)

    fireEvent.pointerUp(slider, { pointerId: 1 })
    await waitFor(() =>
      expect(
        fetch.mock.calls.filter(
          ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
        ),
      ).toHaveLength(1),
    )
    const save = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )!
    const saved = JSON.parse(String(save[1]?.body)) as { config: JobConfigV1 }
    expect(saved.config.cleanup.min_island_mm2).toBe(0.4)
    expect(saved.config.cleanup.override_fields).toContain('min_island_mm2')
  })

  it('waits for a guarded cleanup slider commit and applies its final value atomically', async () => {
    const withHistory = workspaceWithManualHistory()
    installXHR({ response: withHistory })
    const fetch = installFetch(succeededJob, { initialWorkspace: withHistory })
    render(<App />)
    await importThroughPicker()
    const slider = await screen.findByRole('slider', { name: 'Tiny islands slider' })

    fireEvent.change(slider, { target: { value: '0.36' } })
    fireEvent.change(slider, { target: { value: '0.44' } })
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Island threshold')).toHaveValue(0.3)
    expect(screen.getByText('Cleanup settings saved')).toBeInTheDocument()

    fireEvent.pointerUp(slider, { pointerId: 1 })
    expect(await screen.findByRole('alertdialog', {
      name: 'Change settings and clear manual edits?',
    })).toHaveTextContent('2 manual edits will be cleared')
    fireEvent.click(screen.getByRole('button', { name: 'Clear 2 manual edits and apply' }))

    await waitFor(() =>
      expect(
        fetch.mock.calls.filter(
          ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
        ),
      ).toHaveLength(1),
    )
    const save = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )!
    const accepted = JSON.parse(String(save[1]?.body)) as DraftSaveRequest
    expect(accepted.config.cleanup.min_island_mm2).toBe(0.44)
    expect(accepted.operations.map((item) => item.operation_type)).toEqual(['palette-edit'])
  })

  it('requires confirmation before replacing an imported source', async () => {
    const imports = installXHR()
    installFetch()
    render(<App />)
    await importThroughPicker()

    const replace = screen.getByRole('button', { name: 'Replace image' })
    replace.focus()
    fireEvent.click(replace)
    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('replacement.png')] },
    })
    const dialog = screen.getByRole('dialog', { name: 'Replace the current source?' })
    const cancel = screen.getByRole('button', { name: 'Keep current image' })
    const confirm = screen.getByRole('button', { name: 'Import replacement' })
    expect(dialog).toBeInTheDocument()
    expect(screen.getByText('replacement.png')).toBeInTheDocument()
    await waitFor(() => expect(cancel).toHaveFocus())
    expect(document.querySelector('.app-shell')).toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('hidden')
    confirm.focus()
    fireEvent.keyDown(confirm, { key: 'Tab' })
    expect(cancel).toHaveFocus()
    cancel.focus()
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(confirm).toHaveFocus()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Replace the current source?' })).not.toBeInTheDocument()
    expect(document.querySelector('.app-shell')).not.toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('')
    await waitFor(() => expect(replace).toHaveFocus())

    fireEvent.click(replace)
    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('confirmed.png')] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Import replacement' }))
    await waitFor(() => expect(imports).toHaveLength(2))
    expect(screen.queryByRole('dialog', { name: 'Replace the current source?' })).not.toBeInTheDocument()
    await waitFor(() => {
      expect(document.querySelector('.app-shell')).not.toHaveAttribute('inert')
      expect(document.body.style.overflow).toBe('')
    })
  })

  it('keeps conflict recovery in the compact autosave surface and reloads the server draft', async () => {
    installXHR()
    let saves = 0
    installFetch(succeededJob, {
      draftResponder: (_request, commit) => {
        saves += 1
        if (saves > 1) return commit()
        return new Response(JSON.stringify({
          error: {
            code: 'stale_draft', message: 'The server draft moved.', request_id: 'compact-conflict',
            retryable: false, details: {},
          },
        }), { status: 409, headers: { 'Content-Type': 'application/json' } })
      },
    })
    render(<App />)
    await importThroughPicker()
    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '240' },
    })

    const reload = await screen.findByRole('button', { name: 'Reload current draft' })
    expect(reload.closest('.autosave-control')).toHaveAttribute('data-state', 'error')
    fireEvent.click(reload)
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(200)
  })

  it('keeps modal and compact recovery CSS scrollable and non-clipping', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')

    expect(css).toMatch(/\.modal-backdrop\s*\{[^}]*overflow:\s*auto;/s)
    expect(css).toMatch(/\.(?:upload-dialog|confirm-dialog),[\s\S]*?max-height:\s*calc\(100dvh - 40px\);/)
    expect(css).toMatch(/@media \(max-width: 540px\), \(max-height: 560px\)/)
    expect(css).not.toMatch(/\.autosave-control,\s*\n\s*\.brand small\s*\{\s*display:\s*none;/)
    expect(css).toMatch(/\.autosave-control\[data-state="error"\] \.autosave-state/)
    expect(css).toMatch(/\.studio-editor-shell\.i23-editor-shell\[data-compact="auto"\] \.i23-editor-header__actions\s*\{[^}]*overflow:\s*visible;/s)
  })

  it('starts a replacement in a fresh editor session with its own aspect ratio', async () => {
    const replacementWorkspace = makeReplacementWorkspace()
    replacementWorkspace.source_asset!.width_px = 1200
    replacementWorkspace.source_asset!.height_px = 400
    replacementWorkspace.draft!.config.canvas.width_mm = 310
    replacementWorkspace.draft!.config.canvas.height_mm = 100
    installXHR({ responses: [workspace, replacementWorkspace] })
    installFetch()
    render(<App />)
    await importThroughPicker()

    fireEvent.click(await screen.findByRole('button', { name: 'Advanced' }))
    expect(screen.getByRole('button', { name: 'Advanced' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Lock ratio' }))
    expect(screen.getByRole('checkbox', { name: 'Lock ratio' })).not.toBeChecked()

    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('replacement-wide.png')] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Import replacement' }))

    expect(await screen.findByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Basic' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('checkbox', { name: 'Lock ratio' })).toBeChecked()
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(310)
    expect(screen.getByLabelText('Canvas height millimetres')).toHaveValue(100)

    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '620' },
    })
    expect(screen.getByLabelText('Canvas height millimetres')).toHaveValue(200)
  })

  it('publishes an exact named revision, reopens it after restart, and restores page scrolling', async () => {
    installXHR()
    const fetch = installFetch()
    const view = render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    expect(document.body.style.overflow).toBe('hidden')
    expect(await screen.findByText('No published revisions yet. Your autosaved draft is still safe.')).toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), {
      target: { value: 'First printable proof' },
    })
    fireEvent.change(screen.getByPlaceholderText(/What changed/), {
      target: { value: 'Verified after automatic cleanup.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    expect(await screen.findByRole('region', { name: 'Revision details for First printable proof' })).toHaveTextContent('Locked')
    const publication = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/revisions') && init?.method === 'POST',
    )!
    expect(JSON.parse(String(publication[1]?.body))).toMatchObject({
      label: 'First printable proof',
      notes: 'Verified after automatic cleanup.',
      preview_job_id: 'job_1',
    })
    fireEvent.click(screen.getByRole('button', { name: 'Close revisions' }))
    expect(document.body.style.overflow).toBe('')

    view.unmount()
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Processing proof' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    expect(await screen.findByText('Current · draft base')).toBeInTheDocument()
    expect(screen.getByText('First printable proof')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close revisions' }))
    expect(document.body.style.overflow).toBe('')

    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('replacement-after-revision.png')] },
    })
    expect(screen.getByRole('dialog', { name: 'Replace the current source?' })).toBeInTheDocument()
    expect(document.body.style.overflow).toBe('hidden')
    fireEvent.click(screen.getByRole('button', { name: 'Keep current image' }))
    expect(document.body.style.overflow).toBe('')
  })

  it('resynchronizes a stale publication without silently reusing old preview evidence', async () => {
    installXHR()
    const fetch = installFetch(succeededJob, { publishStaleOnce: true })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), {
      target: { value: 'Conflict-safe proof' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Expected draft generation no longer matches.')
    expect(screen.getByText('Preview stale')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('e.g. Cleanup approved')).toHaveValue('Conflict-safe proof')
    fireEvent.click(screen.getByRole('button', { name: 'Retry action' }))
    expect(screen.getByRole('alertdialog', { name: 'Publish without current preview artifacts' })).toBeInTheDocument()
    expect(
      fetch.mock.calls.filter(([url, init]) => String(url).endsWith('/revisions') && init?.method === 'POST'),
    ).toHaveLength(1)
  })

  it('branches an older immutable revision into the editor without changing the active publication', async () => {
    installXHR()
    const fetch = installFetch()
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: 'Original dimensions' } })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    await screen.findByRole('region', { name: 'Revision details for Original dimensions' })
    fireEvent.click(screen.getByRole('button', { name: 'Close revisions' }))

    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), { target: { value: '240' } })
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: 'Wide canvas' } })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    fireEvent.click(screen.getByRole('button', { name: 'Publish without artifacts' }))
    await screen.findByRole('region', { name: 'Revision details for Wide canvas' })

    fireEvent.click(screen.getByRole('button', { name: /Original dimensions/ }))
    const original = await screen.findByRole('region', { name: 'Revision details for Original dimensions' })
    expect(original).toHaveTextContent('200 × 200 mm')
    fireEvent.click(screen.getByRole('button', { name: 'Branch from this revision' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create branch draft' }))

    await waitFor(() => expect(screen.getByText('The working draft already continues from this revision.')).toBeInTheDocument())
    expect(screen.getByText('Draft branched here')).toBeInTheDocument()
    expect(screen.getByText('Current published')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Revision details for Original dimensions' })).toHaveTextContent('Locked')
    const branchRequest = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/revisions/revision_1/branch') && init?.method === 'POST',
    )!
    expect(JSON.parse(String(branchRequest[1]?.body))).toEqual({ expected_draft_generation: 5 })
    fireEvent.click(screen.getByRole('button', { name: 'Close revisions' }))
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(200)
  })

  it('keeps a replacement project untouched when an older publication completes afterward', async () => {
    const replacement = makeReplacementWorkspace()
    installXHR({ responses: [workspace, replacement] })
    const pendingPublication = deferred<Response>()
    let publicationStarted = false
    installFetch(succeededJob, {
      publishResponder: () => {
        publicationStarted = true
        return pendingPublication.promise
      },
    })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), {
      target: { value: 'Superseded publication' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    await waitFor(() => expect(publicationStarted).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: 'Close revisions' }))

    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('replacement.png')] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Import replacement' }))
    expect(await screen.findByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument()
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(310)

    await act(async () => pendingPublication.resolve(new Response(JSON.stringify({
      revision: {
        id: 'revision_old_project', project_id: 'project_1', source_asset_id: 'asset_1',
        parent_revision_id: null, schema_version: 1, engine_version: '0.1.0',
        config: workspace.draft!.config, config_sha256: workspace.draft!.config_sha256,
        editor_sequence_sha256: workspace.draft!.editor_sequence_sha256!, label: 'Superseded publication',
        notes: '', operations: [], artifacts: [], preview_evidence: {
          status: 'missing', preview_job_id: null, reason: 'No preview supplied.',
          source_draft_generation: 2, editor_sequence_sha256: workspace.draft!.editor_sequence_sha256!, artifact_count: 0,
        }, published_at: '2026-07-16T01:00:00Z',
      },
      project: { ...workspace.project, active_revision_id: 'revision_old_project' },
      draft: { ...workspace.draft!, base_revision_id: 'revision_old_project', generation: 3 },
    }), { status: 201, headers: { 'Content-Type': 'application/json' } })))

    expect(screen.getByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument()
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(310)
    expect(window.localStorage.getItem('image23mf.recentProjectId')).toBe('project_2')
  })

  it('keeps a replacement project untouched when an older revision branch completes afterward', async () => {
    const replacement = makeReplacementWorkspace()
    installXHR({ responses: [workspace, replacement] })
    const pendingBranch = deferred<Response>()
    let branchStarted = false
    installFetch(succeededJob, {
      branchResponder: (response) => {
        branchStarted = true
        return pendingBranch.promise.then(() => response)
      },
    })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: 'Branch base' } })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    await screen.findByRole('region', { name: 'Revision details for Branch base' })
    fireEvent.click(screen.getByRole('button', { name: 'Close revisions' }))
    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), { target: { value: '240' } })
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Revisions' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Cleanup approved'), { target: { value: 'Current revision' } })
    fireEvent.click(screen.getByRole('button', { name: 'Publish revision' }))
    fireEvent.click(screen.getByRole('button', { name: 'Publish without artifacts' }))
    await screen.findByRole('region', { name: 'Revision details for Current revision' })
    fireEvent.click(screen.getByRole('button', { name: /Branch base/ }))
    await screen.findByRole('region', { name: 'Revision details for Branch base' })
    fireEvent.click(screen.getByRole('button', { name: 'Branch from this revision' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create branch draft' }))
    await waitFor(() => expect(branchStarted).toBe(true))

    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('replacement.png')] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Import replacement' }))
    expect(await screen.findByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument()
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(310)

    await act(async () => pendingBranch.resolve(new Response()))
    expect(screen.getByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument()
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(310)
    expect(window.localStorage.getItem('image23mf.recentProjectId')).toBe('project_2')
  })

  it('does not let an older project autosave reclaim the workspace after replacement', async () => {
    const replacementWorkspace = makeReplacementWorkspace()
    installXHR({ responses: [workspace, replacementWorkspace] })

    let finishOldSave: (() => void) | null = null
    installFetch(succeededJob, {
      draftResponder: (_request, commit) =>
        new Promise<Response>((resolve) => {
          finishOldSave = () => resolve(commit())
        }),
    })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.change(screen.getByLabelText('Width crop percent'), { target: { value: '80' } })
    await waitFor(() => expect(finishOldSave).not.toBeNull())

    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('replacement.png')] },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Import replacement' }))
    expect(await screen.findByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument()

    await act(async () => finishOldSave?.())
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Replacement proof' })).toBeInTheDocument(),
    )
    expect(window.localStorage.getItem('image23mf.recentProjectId')).toBe('project_2')
  })

  it('shows true upload progress and lets the user cancel import', async () => {
    const instances = installXHR({ deferred: true })
    installFetch()
    render(<App />)
    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile()] },
    })

    const progress = screen.getByRole('progressbar', { name: 'Image upload progress' })
    expect(Number(progress.getAttribute('value'))).toBeGreaterThan(0)
    expect(Number(progress.getAttribute('value'))).toBeLessThan(1)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel import' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(instances).toHaveLength(1)
    expect(screen.getByText('Drop an image anywhere')).toBeInTheDocument()
  })

  it('cancels an active preview job without discarding the project', async () => {
    installXHR()
    installFetch({ ...queuedJob, state: 'running', stage: 'analyzing', progress: 0.4 })
    render(<App />)
    await importThroughPicker()

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel' })).not
      .toBeInTheDocument())
    expect(screen.getByRole('heading', { name: 'Processing proof' })).toBeInTheDocument()
  })

  it('surfaces corrupt and oversized image feedback with next steps', async () => {
    installXHR({
      status: 422,
      response: {
        error: {
          code: 'validation_error',
          message: 'The image stream is corrupt or incomplete.',
          request_id: 'proof',
          retryable: false,
          details: { suggestion: 'Re-export the original image as PNG, JPEG, or WebP.' },
        },
      },
    })
    installFetch()
    const view = render(<App />)
    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [pngFile('corrupt.png')] },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('The image stream is corrupt')
    view.unmount()

    installXHR()
    installFetch()
    render(<App />)
    const large = pngFile('huge.png')
    Object.defineProperty(large, 'size', { value: 65 * 1024 * 1024 })
    fireEvent.change(screen.getByLabelText('Choose source image'), {
      target: { files: [large] },
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('larger than 64 MB')
  })

  it('autosaves palette history, restores it after reload, and performs durable unified undo', async () => {
    installXHR()
    const fetch = installFetch()
    const view = render(<App />)
    await importThroughPicker()
    const name = screen.getByLabelText('Name for color 1')
    fireEvent.change(name, { target: { value: 'Warm cream' } })
    fireEvent.blur(name)

    await waitFor(() =>
      expect(
        fetch.mock.calls.some(
          ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
        ),
      ).toBe(true),
    )
    const saveCall = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )!
    const saved = JSON.parse(String(saveCall[1]?.body)) as {
      config: JobConfigV1
      operations: RegionOperation[]
    }
    expect(saved.config.palette.colors[0].name).toBe('Warm cream')
    expect(saved.operations[0].parameters.action).toBe('rename')
    expect(window.localStorage.getItem('image23mf.recentProjectId')).toBe('project_1')

    view.unmount()
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Processing proof' })).toBeInTheDocument()
    expect(screen.getByLabelText('Name for color 1')).toHaveValue('Warm cream')
    fireEvent.click(screen.getByRole('button', { name: 'Undo: Rename palette color' }))
    await waitFor(() => expect(screen.getByLabelText('Name for color 1')).toHaveValue('Light'))

    const undoCalls = fetch.mock.calls.filter(
      ([url, init]) => String(url).endsWith('/draft/history/undo') && init?.method === 'POST',
    )
    expect(undoCalls).toHaveLength(1)
    const undoRequest = JSON.parse(String(undoCalls[0][1]?.body)) as {
      request_id: string
      expected_cursor_node_id: string
    }
    expect(undoRequest.request_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(undoRequest.expected_cursor_node_id).toBe('lineage_1_node_1')
  })

  it('blocks undo during a provisional cleanup gesture and unlocks after its atomic commit', async () => {
    installXHR()
    const fetch = installFetch()
    render(<App />)
    await importThroughPicker()
    const name = screen.getByLabelText('Name for color 1')
    fireEvent.change(name, { target: { value: 'History seed' } })
    fireEvent.blur(name)
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())

    const slider = await screen.findByRole('slider', { name: 'Tiny islands slider' })
    fireEvent.change(slider, { target: { value: '0.42' } })
    const undo = screen.getByRole('button', { name: 'Undo: Rename palette color' })
    expect(undo).toBeDisabled()
    expect(undo).toHaveAccessibleDescription(/Finish the current crop or cleanup gesture/)
    fireEvent.keyDown(window, { key: 'z', metaKey: true })
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/draft/history/undo'))).toHaveLength(0)

    fireEvent.pointerUp(slider, { pointerId: 1 })
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Undo: Change cleanup settings' })).toBeEnabled()
  })

  it('blocks undo during a crop drag until the pointer gesture commits', async () => {
    installXHR()
    const fetch = installFetch()
    render(<App />)
    await importThroughPicker()
    const name = screen.getByLabelText('Name for color 1')
    fireEvent.change(name, { target: { value: 'History seed' } })
    fireEvent.blur(name)
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())

    const stage = screen.getByTestId('crop-stage')
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 100,
      width: 100, height: 100, toJSON: () => ({}),
    })
    const handle = screen.getByRole('button', { name: 'Resize crop from south east' })
    fireEvent.pointerDown(handle, { pointerId: 2, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(window, { pointerId: 2, clientX: 90, clientY: 90 })

    expect(screen.getByRole('button', { name: 'Undo: Rename palette color' })).toBeDisabled()
    fireEvent.keyDown(window, { key: 'z', metaKey: true })
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/draft/history/undo'))).toHaveLength(0)

    fireEvent.pointerUp(window, { pointerId: 2, clientX: 90, clientY: 90 })
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Undo: Resize crop' })).toBeEnabled()
  })

  it('rejects editor mutations while a history move is in flight', async () => {
    const move = deferred<Response>()
    installXHR()
    const fetch = installFetch(succeededJob, {
      historyMoveResponder: () => move.promise,
    })
    render(<App />)
    await importThroughPicker()
    const name = screen.getByLabelText('Name for color 1')
    fireEvent.change(name, { target: { value: 'Pending undo' } })
    fireEvent.blur(name)
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    const saveCount = fetch.mock.calls.filter(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    ).length

    fireEvent.click(screen.getByRole('button', { name: 'Undo: Rename palette color' }))
    await waitFor(() => expect(screen.getByRole('group', { name: 'Edit history' }))
      .toHaveAttribute('aria-busy', 'true'))
    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '275' },
    })
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(200)
    expect(fetch.mock.calls.filter(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )).toHaveLength(saveCount)

    const moved = structuredClone(workspace.draft!)
    moved.generation = 4
    moved.history = {
      lineage_id: 'lineage_1', cursor_node_id: 'lineage_1_node_0', tip_node_id: 'lineage_1_node_1',
      cursor: 0, total: 1, limit: 100, can_undo: false, can_redo: true,
      undo_label: null, redo_label: 'Rename palette color', state_sha256: '9'.repeat(64),
    }
    move.resolve(new Response(JSON.stringify(moved), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    await waitFor(() => expect(screen.getByLabelText('Name for color 1')).toHaveValue('Light'))
  })

  it('records clear-and-apply as one compound step and restores exact bound edits on undo', async () => {
    const withHistory = workspaceWithManualHistory()
    installXHR({ response: withHistory })
    const fetch = installFetch(succeededJob, { initialWorkspace: withHistory })
    render(<App />)
    await importThroughPicker()
    await screen.findByAltText('Processed canvas view')

    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), { target: { value: '220' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Clear 2 manual edits and apply' }))
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())

    const save = fetch.mock.calls.find(
      ([url, init]) => String(url).endsWith('/draft') && init?.method === 'PUT',
    )!
    const request = JSON.parse(String(save[1]?.body)) as DraftSaveRequest
    expect(request.history_command).toMatchObject({
      command_type: 'clear_manual_and_apply',
      label: 'Resize canvas + clear 2 manual edits',
    })
    expect(request.operations.map((item) => item.operation_type)).toEqual(['palette-edit'])

    fireEvent.click(screen.getByRole('button', {
      name: 'Undo: Resize canvas + clear 2 manual edits',
    }))
    await waitFor(() => expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(200))

    fireEvent.click(screen.getByRole('button', {
      name: 'Contain: Keep the crop; allow clear margins.',
    }))
    expect(await screen.findByRole('alertdialog', {
      name: 'Change settings and clear manual edits?',
    })).toHaveTextContent('2 manual edits will be cleared')
  })

  it('invalidates redo after undo followed by a divergent named edit and keeps it invalid on reload', async () => {
    installXHR()
    const fetch = installFetch()
    const view = render(<App />)
    await importThroughPicker()

    const name = screen.getByLabelText('Name for color 1')
    fireEvent.change(name, { target: { value: 'First name' } })
    fireEvent.blur(name)
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Undo: Rename palette color' }))
    await waitFor(() => expect(screen.getByLabelText('Name for color 1')).toHaveValue('Light'))
    expect(screen.getByRole('button', { name: 'Redo: Rename palette color' })).toBeEnabled()

    const width = screen.getByLabelText('Canvas width millimetres')
    fireEvent.change(width, { target: { value: '230' } })
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Redo' })).toBeDisabled()

    view.unmount()
    render(<App />)
    expect(await screen.findByLabelText('Canvas width millimetres')).toHaveValue(230)
    expect(screen.getByRole('button', { name: 'Redo' })).toBeDisabled()
    expect(fetch.mock.calls.some(([url]) => String(url).endsWith('/draft/history/undo'))).toBe(true)
  })

  it('does not retry a stale history cursor and resyncs the exact current server draft', async () => {
    installXHR()
    let conflicts = 0
    installFetch(succeededJob, {
      historyMoveResponder: () => {
        conflicts += 1
        return new Response(JSON.stringify({
          error: {
            code: 'stale_draft', message: 'The editor history changed before this command was saved.',
            request_id: 'other-session', retryable: false,
            details: { action: 'Reload the project before making another edit.' },
          },
        }), { status: 409, headers: { 'Content-Type': 'application/json' } })
      },
    })
    render(<App />)
    await importThroughPicker()
    const name = screen.getByLabelText('Name for color 1')
    fireEvent.change(name, { target: { value: 'Conflict proof' } })
    fireEvent.blur(name)
    await waitFor(() => expect(screen.getByText('Draft saved locally')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Undo: Rename palette color' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('editor history changed')
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Undo: Rename palette color' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Canvas width millimetres'), {
      target: { value: '250' },
    })
    expect(screen.getByLabelText('Canvas width millimetres')).toHaveValue(200)
    fireEvent.keyDown(window, { key: 'z', metaKey: true })
    expect(conflicts).toBe(1)
    fireEvent.click(screen.getByRole('button', { name: 'Reload current draft' }))
    await waitFor(() => expect(screen.queryByText(/editor history changed/i)).not.toBeInTheDocument())
    expect(screen.getByLabelText('Name for color 1')).toHaveValue('Conflict proof')
    expect(conflicts).toBe(1)
  })
})
