export const CURRENT_JOB_SCHEMA_VERSION = 1 as const

export type CropMode = 'contain' | 'cover' | 'stretch' | 'extend'
export type MergePolicy = 'review' | 'keep' | 'dominant_neighbor' | 'perceptual_neighbor'
export type ArtStyle = 'flush_inlay' | 'raised'

export type PaletteColor = {
  id: string
  name: string
  hex: string
  locked: boolean
  filament_id: string | null
}

export type RegionOperation = {
  operation_type: string
  selection: Record<string, unknown>
  parameters: Record<string, unknown>
  source: 'automatic' | 'manual' | 'model'
  provenance: Record<string, unknown>
}

export type Filament = {
  id: string
  manufacturer: string
  family: string
  name: string
  hex_color: string
  material: string
  finish: string
  owned: boolean
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export type FilamentCatalogEntry = {
  id: string
  manufacturer: string
  family: string
  name: string
  hex_color: string
  material: string
  finish: string
}

export type FilamentCatalog = {
  schema_version: 1
  catalog_id: string
  catalog_version: string
  display_name: string
  entries: FilamentCatalogEntry[]
}

export type AutoPaletteFit = {
  colors: string[]
  iterations: number
  converged: boolean
  sample_size: number
  visible_pixel_count: number
  unique_color_count: number
  options_fingerprint: string
}

export type JobConfigV1 = {
  schema_version: typeof CURRENT_JOB_SCHEMA_VERSION
  source_asset_id: string
  canvas: { width_mm: number; height_mm: number }
  crop: { mode: CropMode; x: number; y: number; width: number; height: number }
  printer: {
    profile_catalog_id: string
    profile_catalog_version: string
    printer_id: string
    nozzle_id: string
    nozzle_mm: number
    layer_height_mm: number
    plate_id: string
  }
  palette: { colors: PaletteColor[] }
  cleanup: {
    min_island_mm2: number
    minimum_island_diameter_mm?: number | null
    max_hole_mm2: number
    maximum_tiny_hole_diameter_mm?: number | null
    minimum_ring_width_mm?: number | null
    minimum_line_width_mm?: number | null
    minimum_neck_width_mm?: number | null
    minimum_gap_width_mm?: number | null
    long_line_minimum_length_mm?: number | null
    smoothing_radius_mm: number
    merge_policy: MergePolicy
    preserve_long_lines: boolean
    printability_profile_id?: string | null
    printability_profile_catalog_fingerprint?: string | null
    override_fields?: (
      | 'long_line_minimum_length_mm'
      | 'max_hole_mm2'
      | 'maximum_tiny_hole_diameter_mm'
      | 'min_island_mm2'
      | 'minimum_gap_width_mm'
      | 'minimum_island_diameter_mm'
      | 'minimum_line_width_mm'
      | 'minimum_neck_width_mm'
      | 'minimum_ring_width_mm'
      | 'smoothing_radius_mm'
    )[]
  }
  geometry: {
    style: ArtStyle
    base_thickness_mm: number
    art_thickness_mm: number
    corner_radius_mm: number
  }
}

export type ProfileSource = {
  name: string
  version: string
  captured_on: string
  profile_paths: string[]
}

export type PrintableArea = {
  origin_x_mm: number
  origin_y_mm: number
  width_mm: number
  depth_mm: number
  height_mm: number
  excluded_rectangles: {
    x_mm: number
    y_mm: number
    width_mm: number
    depth_mm: number
  }[]
}

export type NozzleProfile = {
  id: string
  diameter_mm: number
  material: string
  slicer_machine_name: string
  slicer_variant: string
  min_layer_height_mm: number
  max_layer_height_mm: number
  default_layer_height_mm: number
  recommended_layer_heights_mm: number[]
  process_profile_names: string[]
}

export type PlateProfile = {
  id: string
  display_name: string
  slicer_name: string
  surface: string
  first_layer_finish: string
  notes: string
}

export type ThicknessPolicy = {
  min_base_mm: number
  max_base_mm: number
  default_base_target_mm: number
  min_art_layers: number
  max_art_mm: number
  default_art_target_mm: number
  max_total_mm: number
  require_layer_multiples: boolean
}

export type PrinterProfile = {
  id: string
  display_name: string
  manufacturer: string
  technology: string
  slicer_model_name: string
  printable_area: PrintableArea
  nozzles: NozzleProfile[]
  plates: PlateProfile[]
  default_nozzle_id: string
  default_plate_id: string
  thickness_policy: ThicknessPolicy
}

export type ProfileCatalog = {
  schema_version: 1
  catalog_id: string
  catalog_version: string
  source: ProfileSource
  printers: PrinterProfile[]
}

export type PrintSetupRequest = {
  printer_id: string
  nozzle_id: string
  plate_id: string
  layer_height_mm: number
  canvas_width_mm: number
  canvas_height_mm: number
  base_thickness_mm: number
  art_thickness_mm: number
}

export type ValidatedPrintSetup = {
  request: PrintSetupRequest
  printer: PrinterProfile
  nozzle: NozzleProfile
  plate: PlateProfile
  profile_catalog_fingerprint: string
}

export type CalibrationEvidenceStatus =
  | 'pending_print_calibration'
  | 'partially_validated'
  | 'print_validated'
export type CalibrationBasis = 'engineering_baseline' | 'printed_calibration'
export type CalibrationConfidence = 'provisional' | 'moderate' | 'high'
export type CalibrationFeatureKind = 'dot' | 'hole' | 'line' | 'neck' | 'gap'
export type RecommendationUnit = 'mm' | 'mm2'

export type CalibrationRecommendation = {
  value: number
  unit: RecommendationUnit
  basis: CalibrationBasis
  confidence: CalibrationConfidence
  rationale: string
  evidence_feature_kinds: CalibrationFeatureKind[]
  evidence_run_ids: string[]
}

export type PrintabilityRecommendationName =
  | 'minimum_island_area_mm2'
  | 'minimum_island_diameter_mm'
  | 'maximum_tiny_hole_area_mm2'
  | 'maximum_tiny_hole_diameter_mm'
  | 'minimum_ring_width_mm'
  | 'minimum_line_width_mm'
  | 'minimum_neck_width_mm'
  | 'minimum_gap_width_mm'
  | 'long_line_minimum_length_mm'
  | 'smoothing_radius_mm'

export type PrintabilityRecommendations = Record<
  PrintabilityRecommendationName,
  CalibrationRecommendation
>

export type PrintabilityProfile = {
  id: string
  display_name: string
  printer_id: string
  nozzle_id: string
  nozzle_diameter_mm: number
  material_class: string
  evidence_status: CalibrationEvidenceStatus
  reviewed_on: string
  sweep: {
    dot_diameters_mm: number[]
    hole_diameters_mm: number[]
    line_widths_mm: number[]
    neck_widths_mm: number[]
    gap_widths_mm: number[]
  }
  recommendations: PrintabilityRecommendations
}

export type PrintabilityProfileCatalog = {
  schema_version: 1
  catalog_id: string
  catalog_version: string
  source: {
    name: string
    version: string
    captured_on: string
    method: string
    references: string[]
  }
  profiles: PrintabilityProfile[]
}

export type PrintabilityOverrides = Partial<Record<PrintabilityRecommendationName, number>>

export type ResolvePrintabilityRequest = {
  printer_id: string
  nozzle_id: string
  material_class: string
  catalog_fingerprint?: string | null
  overrides?: PrintabilityOverrides
}

export type ResolvedPrintabilitySettings = {
  profile_id: string
  profile_display_name: string
  profile_catalog_fingerprint: string
  evidence_status: CalibrationEvidenceStatus
  warning: string | null
  values: {
    name: PrintabilityRecommendationName
    value: number
    unit: RecommendationUnit
    source: 'profile' | 'user_override'
    profile_value: number
    profile_basis: CalibrationBasis
    profile_confidence: CalibrationConfidence
    rationale: string
  }[]
}

export type JobType = 'preview' | 'geometry' | 'export' | 'validation' | 'prompted_edit'
export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled' | 'superseded'
export type JobStage =
  | 'queued'
  | 'ingesting'
  | 'normalizing'
  | 'quantizing'
  | 'analyzing'
  | 'cleaning'
  | 'vectorizing'
  | 'meshing'
  | 'packaging'
  | 'slicing'
  | 'validating'
  | 'generating'
  | 'complete'
  | 'failed'
  | 'canceled'
  | 'superseded'

export type JobResource = {
  id: string
  project_id: string
  revision_id: string | null
  type: JobType
  state: JobState
  stage: JobStage
  progress: number
  request_key: string | null
  supersession_key: string | null
  generation: number
  failure: {
    code: string
    message: string
    retryable: boolean
    details: Record<string, unknown>
  } | null
  artifact_ids: string[]
  created_at: string
  started_at: string | null
  finished_at: string | null
  canceled_at: string | null
}

export type SourceImageMetadata = {
  schema_version: number
  original_filename: string
  declared_extension: string | null
  detected_format: 'png' | 'jpeg' | 'webp'
  source_media_type: string
  source_byte_size: number
  source_sha256: string
  source_width_px: number
  source_height_px: number
  source_mode: string
  frame_count: number
  exif_orientation: number
  orientation_applied: boolean
  embedded_icc_profile: boolean
  source_icc_sha256: string | null
  color_conversion: string
  source_had_alpha: boolean
  source_had_transparency: boolean
  normalized_width_px: number
  normalized_height_px: number
  normalized_mode: 'RGBA'
  normalized_color_space: 'srgb'
  normalized_media_type: 'image/png'
  normalized_byte_size: number
  normalized_sha256: string
}

export type ProjectWorkspace = {
  project: {
    id: string
    name: string
    description: string
    active_revision_id: string | null
    preferences: Record<string, unknown>
    created_at: string
    updated_at: string
    archived_at: string | null
  }
  source_asset: {
    id: string
    sha256: string
    media_type: string
    original_filename: string
    byte_size: number
    width_px: number
    height_px: number
    metadata: SourceImageMetadata
    created_at: string
  } | null
  draft: {
    project_id: string
    base_revision_id: string | null
    config: JobConfigV1
    operations: RegionOperation[]
    config_sha256: string
    editor_sequence_sha256?: string
    generation: number
    updated_at: string
    history: EditorHistorySummary
  } | null
  latest_preview_job: JobResource | null
}

export type ProjectSummary = {
  project: ProjectWorkspace['project']
  thumbnail_url: string
  thumbnail_kind: 'preview' | 'source'
  source_filename: string
  source_width_px: number
  source_height_px: number
  canvas_width_mm: number
  canvas_height_mm: number
  color_count: number
  current_revision_id: string | null
  current_revision_label: string | null
  status: 'archived' | 'draft' | 'preview_processing' | 'preview_ready' | 'needs_attention'
  validation: 'not_requested' | 'processing' | 'validated' | 'failed'
}

export type ProjectSummaryCollection = {
  items: ProjectSummary[]
  total: number
}

export type EditorHistorySummary = {
  lineage_id: string
  cursor_node_id: string
  tip_node_id: string
  cursor: number
  total: number
  limit: 100
  can_undo: boolean
  can_redo: boolean
  undo_label: string | null
  redo_label: string | null
  state_sha256: string
}

export type EditorHistoryCommandType =
  | 'config_change'
  | 'palette_change'
  | 'manual_operation'
  | 'clear_manual_and_apply'

export type EditorHistoryCommand = {
  schema_version: 1
  id: string
  command_type: EditorHistoryCommandType
  label: string
  before_state_sha256: string
  expected_cursor_node_id: string
}

export type RevisionPreviewEvidence = {
  status: 'fresh' | 'missing' | 'stale' | 'legacy'
  preview_job_id?: string | null
  derivation_key?: string | null
  reason: string
  source_draft_generation: number | null
  editor_sequence_sha256: string
  artifact_manifest_sha256?: string | null
  artifact_count: number
}

export type ArtifactResource = {
  id: string
  job_id: string | null
  revision_id: string | null
  kind: string
  sha256: string
  derivation_key: string
  media_type: string
  byte_size: number
  metadata: Record<string, unknown>
  download_url: string
  created_at: string
}

export type RevisionResource = {
  id: string
  project_id: string
  source_asset_id: string
  parent_revision_id: string | null
  schema_version: number
  engine_version: string
  config: JobConfigV1
  config_sha256: string
  editor_sequence_sha256: string
  label: string
  notes: string
  operations: RegionOperation[]
  artifacts: ArtifactResource[]
  preview_evidence: RevisionPreviewEvidence
  published_at: string
}

export type RevisionSummary = Pick<
  RevisionResource,
  | 'id'
  | 'project_id'
  | 'parent_revision_id'
  | 'label'
  | 'config_sha256'
  | 'editor_sequence_sha256'
  | 'preview_evidence'
  | 'published_at'
> & {
  operation_count: number
  artifact_count: number
  is_active: boolean
}

export type RevisionList = {
  items: RevisionSummary[]
  total: number
  next_cursor?: string | null
}

export type RevisionPublication = {
  revision: RevisionResource
  draft: NonNullable<ProjectWorkspace['draft']>
  project: ProjectWorkspace['project']
}

export type RevisionBranch = {
  base_revision: RevisionResource
  draft: NonNullable<ProjectWorkspace['draft']>
  project: ProjectWorkspace['project']
}

export type PreviewStart = {
  draft: NonNullable<ProjectWorkspace['draft']>
  job: JobResource
}

export type GeometryStart = {
  job: JobResource
}

export type GeometryJobResult = {
  job: JobResource
  artifacts: ArtifactResource[]
  geometry_svg: ArtifactResource | null
  geometry_ir: ArtifactResource | null
  geometry_mesh: ArtifactResource | null
  geometry_report: ArtifactResource | null
  geometry_preview: ArtifactResource | null
  export_ready: boolean
}

export type ExportMaterialMapping = {
  material_id: string
  extruder: number
  filament_type: 'PLA'
  preset: 'Bambu PLA Basic' | 'Bambu PLA Matte'
}

export type ExportProfileSettings = {
  printer_model: 'Bambu Lab P2S'
  nozzle_diameter_mm: 0.2 | 0.4
  layer_height_mm: 0.1 | 0.2
  bed_type: 'Textured PEI Plate'
}

export type StartExportRequest = {
  schema_version: 1
  geometry_artifact_id: string
  geometry_sha256: string
  name: string
  material_mapping: ExportMaterialMapping[]
  profile: ExportProfileSettings
  minimum_part_thickness_mm: number
}

export type ExportStart = {
  job: JobResource
}

export type ExportJobResult = {
  job: JobResource
  artifacts: ArtifactResource[]
  quality_report: ArtifactResource | null
  validation_report: ArtifactResource | null
  validation_log: ArtifactResource | null
  package: ArtifactResource | null
  download_ready: boolean
}

export type CanonicalTransform = {
  schema_version: 1
  original_size: { width: number; height: number }
  normalized_size: { width: number; height: number }
  exif_orientation: number
  crop_rect: { x: number; y: number; width: number; height: number }
  fit_mode: CropMode
  working_size: { width: number; height: number }
  canvas_size: { width: number; height: number }
}

export type PreviewStatistics = {
  schema_version: 1
  config_sha256: string
  profile_catalog_fingerprint: string
  source_asset_id: string
  source: { width: number; height: number }
  preview: { width: number; height: number }
  physical: {
    width_mm: number
    height_mm: number
    mm_per_pixel_x: number
    mm_per_pixel_y: number
  }
  alpha: {
    opaque_pixels: number
    translucent_pixels: number
    transparent_pixels: number
  }
  transform: CanonicalTransform
  preview_sha256: string
}

export type PaletteColorMetrics = {
  index: number
  color: string
  pixel_count: number
  coverage_ratio: number
  mean_delta_e: number | null
  p95_delta_e: number | null
  component_count: number
  largest_component_share: number
  smallest_component_pixels: number | null
  smallest_component_mm2: number | null
}

export type PaletteMetrics = {
  schema_version: 1
  config_sha256: string
  quantization_fingerprint: string
  options_fingerprint: string
  palette: string[]
  visible_pixel_count: number
  physical_area_mm2: number
  colors: PaletteColorMetrics[]
  reconstruction: {
    alpha_weighted_mean_delta_e: number
    alpha_weighted_p95_delta_e: number
  }
  adjacency: {
    first_index: number
    second_index: number
    delta_e: number
    boundary_edge_count: number
  }[]
  minimum_adjacent_delta_e: number | null
  fragmentation: {
    component_count: number
    excess_component_count: number
    single_pixel_component_count: number
    components_per_100_mm2: number
  }
}

export type RegionGraph = {
  schema_version: 1
  width_px: number
  height_px: number
  width_mm: number
  height_mm: number
  pixel_width_mm: number
  pixel_height_mm: number
  active_pixel_count: number
  palette: {
    label: number
    color: string
  }[]
  regions: {
    id: string
    label: number
    color: string
    pixel_count: number
    area_mm2: number
    perimeter_edge_count: number
    perimeter_mm: number
    compactness: number
    pixel_bounds: { x: number; y: number; width: number; height: number }
    physical_bounds: { x_mm: number; y_mm: number; width_mm: number; height_mm: number }
    border_contact: {
      top_mm: number
      right_mm: number
      bottom_mm: number
      left_mm: number
      total_mm: number
      touches_canvas_border: boolean
    }
    width_estimate: {
      method: 'orthogonal-run-spans-v1'
      minimum_mm: number
      median_mm: number
      p95_mm: number
      maximum_mm: number
    }
    neighbor_region_ids: string[]
  }[]
  adjacency: {
    first_region_id: string
    second_region_id: string
    boundary_edge_count: number
    boundary_length_mm: number
  }[]
}

export type RiskCode =
  | 'small_island'
  | 'tiny_hole'
  | 'hollow_ring'
  | 'narrow_neck'
  | 'thin_line'
  | 'narrow_gap'
  | 'excess_fragmentation'
  | 'color_absent'

export type RiskActionKind =
  | 'review'
  | 'keep'
  | 'merge_dominant_neighbor'
  | 'merge_perceptual_neighbor'
  | 'merge_explicit_color'
  | 'fill_hole'
  | 'recolor_hole'
  | 'collapse_ring'
  | 'widen'
  | 'preserve_line'
  | 'close_gap'
  | 'smooth'
  | 'remove_color'
  | 'replace_color'

export type PhysicalMmBounds = {
  x_mm: number
  y_mm: number
  width_mm: number
  height_mm: number
}

export type RiskMeasurement = {
  key:
    | 'area'
    | 'hole_area'
    | 'perimeter'
    | 'compactness'
    | 'equivalent_diameter'
    | 'minimum_width'
    | 'median_width'
    | 'maximum_width'
    | 'length'
    | 'gap_width'
    | 'component_count'
    | 'components_per_100_mm2'
    | 'pixel_count'
    | 'coverage_ratio'
    | 'nozzle_diameter'
  role: 'measured' | 'threshold' | 'context'
  value: number
  unit: 'mm' | 'mm2' | 'count' | 'count_per_100_mm2' | 'ratio' | 'unitless'
}

export type RiskSuggestion = {
  kind: RiskActionKind
  title: string
  explanation: string
  destructive: boolean
  requires_confirmation: boolean
}

export type RiskFinding = {
  code: RiskCode
  feature_key: string
  severity: 'info' | 'warning' | 'error'
  title: string
  explanation: string
  affected_region_ids: string[]
  affected_labels: number[]
  affected_bounds: PhysicalMmBounds[]
  measurements: RiskMeasurement[]
  suggestions: RiskSuggestion[]
  classifier_id: string
  classifier_version: string
}

export type RiskReport = {
  schema_version: 1
  graph_fingerprint: string
  options_fingerprint: string
  nozzle_mm: number
  evaluated_codes: RiskCode[]
  pending_codes: RiskCode[]
  warnings: (RiskFinding & { id: string })[]
  summary: {
    total: number
    info: number
    warning: number
    error: number
    by_code: { code: RiskCode; count: number }[]
  }
}

export type IslandAnalysis = {
  schema_version: 1
  graph_fingerprint: string
  options: {
    minimum_area_mm2: number
    minimum_equivalent_diameter_mm: number
    preserve_long_lines: boolean
    long_line_minimum_aspect_ratio: number
    long_line_minimum_length_mm: number
    error_ratio: number
  }
  options_fingerprint: string
  candidates: {
    region_id: string
    label: number
    color: string
    status: 'risk' | 'long_line_exempt'
    triggers: ('area' | 'equivalent_diameter')[]
    area_mm2: number
    equivalent_diameter_mm: number
    perimeter_mm: number
    compactness: number
    bounds: PhysicalMmBounds
    bbox_length_mm: number
    bbox_width_mm: number
    bbox_aspect_ratio: number
    touches_canvas_border: boolean
    neighbors: {
      region_id: string
      label: number
      color: string
      boundary_edge_count: number
      boundary_length_mm: number
      delta_e: number
    }[]
  }[]
  summary: {
    candidate_count: number
    risk_count: number
    long_line_exempt_count: number
  }
}

export type ClearanceAnalysis = {
  schema_version: 1
  graph_fingerprint: string
  options: {
    nozzle_mm: number
    minimum_width_mm: number
    minimum_line_width_mm: number
    minimum_neck_width_mm: number
    minimum_gap_width_mm: number
    minimum_line_length_mm: number
    minimum_line_aspect_ratio: number
    wide_support_ratio: number
    error_ratio: number
    distance_method: 'euclidean-center-distance-v1'
    width_method: 'orthogonal-run-at-clearance-ridge-v1'
  }
  options_fingerprint: string
  features: {
    id: string
    kind: 'thin_line' | 'narrow_neck' | 'narrow_gap'
    region_ids: string[]
    labels: number[]
    pixel_bounds: {
      x: number
      y: number
      width: number
      height: number
    }
    bounds: PhysicalMmBounds
    minimum_width_mm: number
    median_width_mm: number
    maximum_width_mm: number
    length_mm: number
    evidence_sample_count: number
    touches_canvas_border: boolean
  }[]
  summary: {
    feature_count: number
    thin_line_count: number
    narrow_neck_count: number
    narrow_gap_count: number
  }
}

export type HoleAnalysis = {
  schema_version: 1
  graph_fingerprint: string
  options: {
    maximum_area_mm2: number
    maximum_equivalent_diameter_mm: number
    minimum_surviving_ring_width_mm: number
    maximum_ring_to_center_area_ratio: number
    error_ratio: number
  }
  options_fingerprint: string
  features: {
    id: string
    kind: 'tiny_hole' | 'hollow_ring'
    center_kind: 'active_region' | 'transparent_void'
    center_component_id: string
    center_region_id: string | null
    center_label: number | null
    ring_region_id: string
    ring_label: number
    ring_pixel_count: number
    ring_pixel_bounds: { x: number; y: number; width: number; height: number }
    ring_bounds: PhysicalMmBounds
    outer_region_ids: string[]
    outer_labels: number[]
    triggers: ('area' | 'equivalent_diameter')[]
    center_pixel_count: number
    center_area_mm2: number
    center_equivalent_diameter_mm: number
    center_perimeter_mm: number
    center_pixel_bounds: { x: number; y: number; width: number; height: number }
    center_bounds: PhysicalMmBounds
    ring_minimum_width_mm: number
    ring_median_width_mm: number
    ring_maximum_width_mm: number
    ring_area_mm2: number
    ring_to_center_area_ratio: number
    ring_perimeter_mm: number
  }[]
  summary: {
    feature_count: number
    tiny_hole_count: number
    hollow_ring_count: number
    transparent_center_count: number
  }
}

export type PreviewJobResult = {
  job: JobResource
  artifacts: ArtifactResource[]
  statistics: PreviewStatistics | null
  palette_metrics: PaletteMetrics | null
  region_graph: RegionGraph | null
  risk_report: RiskReport | null
  island_analysis: IslandAnalysis | null
  clearance_analysis: ClearanceAnalysis | null
  hole_analysis: HoleAnalysis | null
  reprocessing_plan?: {
    schema_version: 1
    mode: 'scoped' | 'full'
    reason: string
    message: string
    baseline_job_id?: string | null
    reused_stages: string[]
    recomputed_stages: string[]
    affected_pixel_count: number
    affected_mask_sha256: string
    output_equivalence: 'by_construction' | 'full_recompute'
  } | null
}

export type PromptedEditProvider = {
  provider_id: string
  display_name: string
  model_id: string
  model_version: string
  capabilities: {
    supports_mask: boolean
    supports_references: boolean
    supports_multiple_alternatives: boolean
    supports_seed: boolean
    guarantees_seeded_replay: boolean
    maximum_references: number
    maximum_alternatives: number
    accepted_media_types: string[]
    output_media_types: string[]
  }
  privacy: {
    policy_version: string
    data_residency: string
    retention: string
    training_use: 'none' | 'opt_out' | 'may_train' | 'unknown'
    subprocessors: string[]
    policy_url: string | null
  }
}

export type PromptedEditDisclosure = {
  schema_version: 1
  disclosure_sha256: string
  project_id: string
  provider: PromptedEditProvider
  source_sha256: string
  mask_sha256: string
  reference_sha256: string[]
  prompt: string
  options: Record<string, unknown>
  data_categories: string[]
}

export type PromptedEditAlternative = {
  id: string
  index: number
  status: 'review' | 'rejected' | 'accepted'
  output_sha256: string
  output_media_type: string
  output_url: string
  changed_mask_sha256: string
  changed_mask_url: string
  changed_mask_preview_url: string
  changed_pixel_count: number
  width_px: number
  height_px: number
  provenance: {
    prompt: string
    provider_id: string
    model_id: string
    model_version: string
    seed: number | null
    reproducibility: 'exact_seeded' | 'best_effort'
    reproducibility_reason: string
    consent_event: { event_id: string; recorded_at: string; disclosure: string }
  }
}

export type PromptedEditSession = {
  id: string
  project_id: string
  parent_revision_id: string
  retry_of_session_id: string | null
  status: 'prepared' | 'queued' | 'running' | 'complete' | 'partial' | 'failed' | 'canceled' | 'rejected' | 'accepted'
  request_sha256: string
  selection_sha256: string
  provider: PromptedEditProvider
  disclosure: PromptedEditDisclosure
  prompt: string
  options: Record<string, unknown>
  seed: number | null
  requested_alternative_count: number
  alternatives: PromptedEditAlternative[]
  failures: { index: number; code: string; retryable: boolean }[]
  accepted_alternative_id: string | null
  accepted_revision_id: string | null
  created_at: string
  updated_at: string
}

export type PromptedEditPreparation = {
  session_id: string
  request_sha256: string
  provider: PromptedEditProvider
  disclosure: PromptedEditDisclosure
  status: 'prepared'
}
