export type SeamQaStatus = 'pass' | 'warning' | 'fail'
export type SeamOrientation = 'vertical' | 'horizontal'
export type TileSide = 'top' | 'right' | 'bottom' | 'left'

export type TileRiskFinding = {
  tile_id: string
  code: string
  severity: 'info' | 'warning' | 'error'
  message: string
}

export type TileRiskSummary = {
  status: SeamQaStatus
  finding_count: number
  info_count: number
  warning_count: number
  error_count: number
  findings: TileRiskFinding[]
}

export type SharedEdgeEvidence = {
  seam_id: string
  orientation: SeamOrientation
  coordinate_px: number
  span_start_px: number
  span_end_px: number
  first_tile_id: string
  second_tile_id: string
  first_side: TileSide
  second_side: TileSide
  first_boundary_px: number
  second_boundary_px: number
  first_guard_sha256: string
  second_guard_sha256: string
  first_guard_role: 'first_tile_inner_edge_strip'
  second_guard_role: 'second_tile_inner_edge_strip'
  guard_hashes_expected_to_match: false
  exact_coordinate_match: boolean
  exact_span_match: boolean
  no_gap_no_overlap: boolean
  evidence_kind: 'raster_partition_boundary'
  overlay_source: 'diagnostic_metadata_only'
}

export type TileSeamEvidence = {
  tile_id: string
  plate_number: number
  row: number
  column: number
  rotation_degrees: 0 | 90
  master_pixel_bounds: {
    x_start: number
    y_start: number
    x_end: number
    y_end: number
  }
  shared_edge_count: number
  protected_sides: TileSide[]
  visible_labels_sha256: string
  vector_clip_bounds_master_mm: {
    x: number
    y: number
    width: number
    height: number
  }
  tile_topology_artifact_sha256: string
  tile_geometry_fingerprint: string
  topology_source_mode: 'master_topology_exact_label_clip'
  fits_build_plate: boolean
  risk: TileRiskSummary
}

export type MuralSeamQaReport = {
  schema_version: 1
  request_fingerprint: string
  partition_sha256: string
  rows: number
  columns: number
  panel_size_mm: { width: number; height: number }
  master_size_mm: { width: number; height: number }
  assembled_size_mm: { width: number; height: number }
  horizontal_gap_mm: number
  vertical_gap_mm: number
  expected_seam_count: number
  exact_shared_edge_count: number
  failed_shared_edge_count: number
  status: SeamQaStatus
  artwork: {
    authoritative_master_sha256: string
    recomposed_visible_art_sha256: string
    visible_art_unchanged: boolean
    overlay_storage: 'metadata_only'
    overlay_pixels_written: 0
    tile_number_pixels_written: 0
    orientation_marker_pixels_written: 0
    proof: string
  }
  topology: {
    evidence_kind: 'exact_master_topology_clip'
    label_partition_sha256: string
    topology_partition_sha256: string
    seam_topology_sha256: string
    master_topology_fingerprint: string
    master_topology_artifact_sha256: string
    recomposed_labels_sha256: string
    source_pixel_count: number
    represented_pixel_count: number
    every_master_pixel_represented: boolean
  }
  seams: SharedEdgeEvidence[]
  tiles: TileSeamEvidence[]
}
