export type Sha256 = string

export type CalibrationEvidenceStatus =
  | 'pending_print_calibration'
  | 'partially_validated'
  | 'print_validated'

export type CalibrationBasis = 'engineering_baseline' | 'printed_calibration'
export type CalibrationConfidence = 'provisional' | 'moderate' | 'high'
export type CalibrationFeatureKind = 'dot' | 'hole' | 'line' | 'neck' | 'gap'
export type CalibrationOutcome = 'untested' | 'pass' | 'fail' | 'uncertain'
export type CalibrationRunStatus = 'template' | 'completed'
export type CalibrationEvidenceRole =
  | 'coupon_svg'
  | 'coupon_png'
  | 'project_3mf'
  | 'sliced_gcode'
  | 'slicer_settings'
  | 'photo'

export type CalibrationRecommendation = {
  value: number
  unit: 'mm' | 'mm2'
  basis: CalibrationBasis
  confidence: CalibrationConfidence
  rationale: string
  evidence_feature_kinds: CalibrationFeatureKind[]
  evidence_run_ids: string[]
}

export type PrintabilityRecommendations = {
  minimum_island_area_mm2: CalibrationRecommendation
  minimum_island_diameter_mm: CalibrationRecommendation
  maximum_tiny_hole_area_mm2: CalibrationRecommendation
  maximum_tiny_hole_diameter_mm: CalibrationRecommendation
  minimum_ring_width_mm: CalibrationRecommendation
  minimum_line_width_mm: CalibrationRecommendation
  minimum_neck_width_mm: CalibrationRecommendation
  minimum_gap_width_mm: CalibrationRecommendation
  long_line_minimum_length_mm: CalibrationRecommendation
  smoothing_radius_mm: CalibrationRecommendation
}

export type CalibrationSweep = {
  dot_diameters_mm: number[]
  hole_diameters_mm: number[]
  line_widths_mm: number[]
  neck_widths_mm: number[]
  gap_widths_mm: number[]
}

export type PrintabilityProfile = {
  id: string
  display_name: string
  printer_id: string
  nozzle_id: string
  nozzle_diameter_mm: number
  material_class: string
  evidence_status: CalibrationEvidenceStatus
  reviewed_on: string
  sweep: CalibrationSweep
  recommendations: PrintabilityRecommendations
}

export type CalibrationCatalogSource = {
  name: string
  version: string
  captured_on: string
  method: string
  references: string[]
}

export type PrintabilityProfileCatalog = {
  schema_version: 1
  catalog_id: string
  catalog_version: string
  source: CalibrationCatalogSource
  profiles: PrintabilityProfile[]
}

export type CalibrationCatalogVersion = {
  fingerprint: Sha256
  catalog: PrintabilityProfileCatalog
  parent_fingerprint: Sha256 | null
  created_at: string
  active: boolean
}

export type CalibrationCatalogCollection = {
  items: CalibrationCatalogVersion[]
  active_fingerprint: Sha256
}

export type CalibrationBounds = {
  x_mm: number
  y_mm: number
  width_mm: number
  height_mm: number
}

export type CalibrationFeature = {
  id: string
  kind: CalibrationFeatureKind
  column: number
  nominal_dimension_mm: number
  rasterized_dimension_mm: number
  rasterized_width_mm: number
  rasterized_height_mm: number
  dimension_name: 'diameter' | 'width' | 'gap'
  center_x_mm: number
  center_y_mm: number
  bounds: CalibrationBounds
}

export type CalibrationArtifactManifest = {
  schema_version: 2
  profile_id: string
  profile_fingerprint: Sha256
  printer_id: string
  nozzle_id: string
  material_class: string
  width_mm: number
  height_mm: number
  raster_mm_per_pixel: number
  width_px: number
  height_px: number
  svg_sha256: Sha256
  png_sha256: Sha256
  features: CalibrationFeature[]
}

export type CalibrationObservation = {
  feature_id: string
  outcome: CalibrationOutcome
  measured_dimension_mm: number | null
  notes: string
}

export type CalibrationRunRecord = {
  schema_version: 1
  status: CalibrationRunStatus
  record_id: string | null
  profile_id: string
  artifact_fingerprint: Sha256
  printer_id: string
  nozzle_id: string
  material_class: string
  layer_height_mm: number | null
  plate_id: string | null
  filament: string | null
  operator: string | null
  printed_on: string | null
  slicer_profile: string | null
  observations: CalibrationObservation[]
  notes: string
}

export type CalibrationSpecimen = {
  catalog_fingerprint: Sha256
  catalog_id: string
  catalog_version: string
  profile: PrintabilityProfile
  artifact: CalibrationArtifactManifest
  record_template: CalibrationRunRecord
  evidence_state: 'not_observed'
  evidence_message: string
  svg_url: string
  png_url: string
  bundle_url: string
}

export type CalibrationDraftMetadata = {
  slicer_application: string | null
  slicer_version: string | null
  slicer_executable_sha256: Sha256 | null
  machine_profile_name: string | null
  machine_profile_sha256: Sha256 | null
  process_profile_name: string | null
  process_profile_sha256: Sha256 | null
  filament_profile_name: string | null
  filament_profile_sha256: Sha256 | null
  filament_id: string | null
  filament_manufacturer: string | null
  filament_family: string
  filament_name: string | null
  filament_material: string | null
  filament_finish: string
  filament_color_hex: string | null
}

export type CalibrationDraftBlocker = {
  code: string
  field: string
  message: string
}

export type CalibrationDraftReadiness = {
  evidence_state:
    | 'preparation'
    | 'unverified_draft'
    | 'attested_draft'
    | 'sealed_physical_evidence'
  ready_to_attest: boolean
  ready_to_finalize: boolean
  blockers: CalibrationDraftBlocker[]
}

export type CalibrationDraftMember = {
  id: string
  role: CalibrationEvidenceRole
  ordinal: number
  filename: string
  sha256: Sha256
  byte_size: number
  media_type: string
  extension: string
  download_url: string
  created_at: string
}

export type CalibrationDraft = {
  id: string
  catalog_fingerprint: Sha256
  profile: PrintabilityProfile
  artifact: CalibrationArtifactManifest
  record: CalibrationRunRecord
  metadata: CalibrationDraftMetadata
  generation: number
  attested_at: string | null
  attested_candidate_sha256: Sha256 | null
  candidate_sha256: Sha256
  attestation_statement: string
  finalized_run_id: string | null
  members: CalibrationDraftMember[]
  readiness: CalibrationDraftReadiness
  created_at: string
  updated_at: string
}

export type CalibrationDraftCollection = {
  items: CalibrationDraft[]
  total: number
}

export type CreateCalibrationDraftRequest = {
  profile_id: string
  expected_catalog_fingerprint: Sha256
}

export type UpdateCalibrationDraftRequest = {
  expected_generation: number
  record: CalibrationRunRecord
  metadata: CalibrationDraftMetadata
}

export type AttestCalibrationDraftRequest = {
  expected_generation: number
  confirmed: true
}

export type CalibrationEvidenceMember = {
  role: CalibrationEvidenceRole
  ordinal: number
  archive_path: string
  filename: string
  sha256: Sha256
  byte_size: number
  media_type: string
  extension: string
}

export type CalibrationAttestation = {
  operator: string
  attested_at: string
  statement: 'I personally observed this physical print and recorded these outcomes accurately.'
}

export type CalibrationPinnedProfileEvidence = {
  role: 'machine' | 'process' | 'filament'
  name: string
  sha256: Sha256
}

export type CalibrationSlicerEvidence = {
  application: string
  version: string
  executable_sha256: Sha256
  profiles: CalibrationPinnedProfileEvidence[]
}

export type CalibrationFilamentEvidence = {
  filament_id: string | null
  manufacturer: string
  family: string
  name: string
  material: string
  finish: string
  color_hex: string
  snapshot_sha256: Sha256
}

export type CalibrationEvidenceManifest = {
  schema_version: 1
  catalog_id: string
  catalog_version: string
  catalog_fingerprint: Sha256
  profile: PrintabilityProfile
  artifact: CalibrationArtifactManifest
  record: CalibrationRunRecord
  slicer: CalibrationSlicerEvidence
  filaments: CalibrationFilamentEvidence[]
  attestation: CalibrationAttestation
  members: CalibrationEvidenceMember[]
}

export type CalibrationMember = {
  id: string
  role: CalibrationEvidenceRole
  ordinal: number
  filename: string
  sha256: Sha256
  byte_size: number
  media_type: string
  download_url: string
}

export type CalibrationSourceBundle = {
  filename: string
  sha256: Sha256
  byte_size: number
  media_type: string
  download_url: string
}

export type CalibrationArtifact = {
  id: string
  catalog_id: string
  catalog_version: string
  catalog_fingerprint: Sha256
  profile_id: string
  profile_fingerprint: Sha256
  artifact_fingerprint: Sha256
  manifest: CalibrationArtifactManifest
  members: CalibrationMember[]
  created_at: string
}

export type CalibrationRun = {
  id: string
  artifact: CalibrationArtifact
  catalog_id: string
  catalog_version: string
  catalog_fingerprint: Sha256
  profile_id: string
  profile_fingerprint: Sha256
  printer_id: string
  nozzle_id: string
  material_class: string
  process_fingerprint: Sha256
  record_sha256: Sha256
  evidence_sha256: Sha256
  evidence: CalibrationEvidenceManifest
  members: CalibrationMember[]
  source_bundle: CalibrationSourceBundle
  imported_at: string
  integrity: 'verified'
}

export type CalibrationRunCollection = {
  items: CalibrationRun[]
  total: number
}

export type CalibrationImportResult = {
  run: CalibrationRun
  duplicate: boolean
}

export type CalibrationDraftFinalizeResult = CalibrationImportResult & {
  draft: CalibrationDraft
}

export type CalibrationProposalContribution = {
  run_id: string
  feature_id: string
  feature_kind: CalibrationFeatureKind
  nominal_dimension_mm: number
  rasterized_dimension_mm: number
  rasterized_width_mm: number
  rasterized_height_mm: number
  outcome: CalibrationOutcome
  measured_dimension_mm: number | null
  notes: string
  included: boolean
  exclusion_reason: string | null
}

export type CalibrationTransitionAnalysis = {
  feature_kind: CalibrationFeatureKind
  recommendation_names: string[]
  status: 'proposed' | 'blocked'
  proposed_dimension_mm: number | null
  largest_failed_dimension_mm: number | null
  smallest_passed_dimension_mm: number | null
  reason: string
  contributing_run_ids: string[]
}

export type CalibrationRecommendationChange = {
  name: keyof PrintabilityRecommendations
  before: CalibrationRecommendation
  after: CalibrationRecommendation
}

export type CalibrationProfileProposal = {
  schema_version: 1
  id: string
  profile_id: string
  process_fingerprint: Sha256
  base_catalog_fingerprint: Sha256
  proposed_catalog_fingerprint: Sha256
  algorithm_version: 'transition-bracket-v1'
  run_ids: string[]
  analyses: CalibrationTransitionAnalysis[]
  contributions: CalibrationProposalContribution[]
  base_evidence_status: CalibrationEvidenceStatus
  proposed_evidence_status: CalibrationEvidenceStatus
  recommendation_changes: CalibrationRecommendationChange[]
  proposed_catalog: PrintabilityProfileCatalog
  proposal_sha256: Sha256
}

export type CalibrationProposalState = 'pending' | 'accepted' | 'rejected'

export type CalibrationProposal = {
  proposal: CalibrationProfileProposal
  state: CalibrationProposalState
  created_at: string
  reviewed_at: string | null
  reviewer: string | null
  review_reason: string | null
}

export type CalibrationProposalCollection = {
  items: CalibrationProposal[]
}

export type CreateCalibrationProposalRequest = {
  profile_id: string
  run_ids: string[]
  expected_catalog_fingerprint: Sha256
}

export type AcceptCalibrationProposalRequest = {
  expected_catalog_fingerprint: Sha256
  reviewer: string
  reason: string
}

export type RejectCalibrationProposalRequest = {
  reviewer: string
  reason: string
}

export type CalibrationPromotionResult = {
  proposal: CalibrationProposal
  active_catalog: CalibrationCatalogVersion
}

export type CalibrationDownload = {
  blob: Blob
  filename: string
  media_type: string
}
