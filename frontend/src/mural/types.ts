export type MuralOrientation = 'auto' | 'native' | 'rotate_90'

export type MuralLayout = {
  rows: number
  columns: number
  panel_width_mm: number
  panel_height_mm: number
  horizontal_gap_mm: number
  vertical_gap_mm: number
  bleed_mm: number
  orientation: MuralOrientation
}

export type BedRectangle = {
  x_mm: number
  y_mm: number
  width_mm: number
  height_mm: number
}

export type MuralPlanSettings = {
  processed_artifact_id: string
  layout: MuralLayout
  edge_clearance_mm: number
  reserved_rectangles: BedRectangle[]
}

export type MuralSourceProvenance = {
  source_asset_id: string
  source_asset_sha256: string
  processed_artifact_id: string
  processed_artifact_sha256: string
  processed_size: { width: number; height: number }
  canonical_transform: Record<string, unknown>
  config_sha256: string
  engine_version: string
  revision_id: string | null
  draft_generation: number | null
}

export type MuralPlanRequest = {
  schema_version: 1
  source: MuralSourceProvenance
  layout: MuralLayout
  bed: {
    printer_id: string
    plate_id: string
    profile_catalog_fingerprint: string
    width_mm: number
    height_mm: number
    edge_clearance_mm: number
    excluded_rectangles: BedRectangle[]
  }
  reserved_rectangles: BedRectangle[]
}

export type MuralTilePlan = {
  id: string
  row: number
  column: number
  build_plate_index: number
  normalized_master_bounds: { x: number; y: number; width: number; height: number }
  master_bounds_mm: { x: number; y: number; width: number; height: number }
  master_pixel_bounds: {
    x_start: number
    y_start: number
    x_end: number
    y_end: number
  }
  sampled_master_bounds_mm: { x: number; y: number; width: number; height: number }
  sampled_master_pixel_bounds: {
    x_start: number
    y_start: number
    x_end: number
    y_end: number
  }
  output_size_mm: { width: number; height: number }
  outside_master_padding: {
    top_mm: number
    right_mm: number
    bottom_mm: number
    left_mm: number
  }
  bed_fit: {
    fits: boolean
    rotation_degrees: 0 | 90
    placed_width_mm: number
    placed_height_mm: number
    origin_x_mm: number | null
    origin_y_mm: number | null
    reason: string | null
  }
}

export type MuralPlan = {
  schema_version: 1
  request_fingerprint: string
  source_fingerprint: string
  master_transform: Record<string, unknown>
  master_size_mm: { width: number; height: number }
  assembled_size_mm: { width: number; height: number }
  tiles: MuralTilePlan[]
  all_tiles_fit: boolean
  warnings: string[]
}

export type MuralPlanPreview = {
  request: MuralPlanRequest
  plan: MuralPlan
}

export type MuralPlanResource = MuralPlanPreview & {
  project_id: string
  schema_version: number
  generation: number
  freshness: 'current' | 'stale'
  stale_reason: string | null
  created_at: string
  updated_at: string
}

export type SaveMuralPlanRequest = MuralPlanSettings & {
  expected_generation: number
}
