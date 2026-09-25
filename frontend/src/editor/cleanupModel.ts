import type {
  CalibrationEvidenceStatus,
  JobConfigV1,
  PrintabilityRecommendationName,
  ResolvedPrintabilitySettings,
} from '../contracts'

export type CleanupNumericField =
  | 'minimum_island_area_mm2'
  | 'maximum_tiny_hole_area_mm2'
  | 'smoothing_radius_mm'
  | 'minimum_line_width_mm'
  | 'minimum_neck_width_mm'
  | 'minimum_gap_width_mm'
  | 'long_line_minimum_length_mm'

export type CleanupControlField = CleanupNumericField | 'preserve_long_lines'
export type CleanupSection = 'regions' | 'features' | 'long_lines'
export type CleanupMode = 'basic' | 'advanced'
export type CleanupPresetId = 'profile' | 'fine_detail' | 'reliable'
export type CleanupPresetSelection = CleanupPresetId | 'custom'
export type PreviewFreshness = 'current' | 'stale' | 'rendering' | 'unavailable'

export type BackendCleanupOverrideField =
  | 'min_island_mm2'
  | 'minimum_island_diameter_mm'
  | 'max_hole_mm2'
  | 'maximum_tiny_hole_diameter_mm'
  | 'minimum_ring_width_mm'
  | 'smoothing_radius_mm'
  | 'minimum_line_width_mm'
  | 'minimum_neck_width_mm'
  | 'minimum_gap_width_mm'
  | 'long_line_minimum_length_mm'

export type CleanupControlValues = Record<CleanupNumericField, number> & {
  preserve_long_lines: boolean
}

export type PixelScale = {
  mmPerPixelX: number
  mmPerPixelY: number
}

export type CleanupRecommendation = {
  value: number
  profileValue: number
  source: 'profile' | 'user_override'
  rationale: string
}

export type CleanupProfileContext = {
  id: string
  displayName: string
  catalogFingerprint: string
  nozzleMm: number
  evidenceStatus: CalibrationEvidenceStatus
  warning: string | null
  recommendations: Record<CleanupNumericField, CleanupRecommendation>
  preserveLongLinesDefault: boolean
  islandDiameterMm: number
  tinyHoleDiameterMm: number
}

export type CleanupControlSpec = {
  field: CleanupNumericField
  label: string
  shortLabel: string
  help: string
  unit: 'mm' | 'mm²'
  min: number
  max: number
  step: number
  section: CleanupSection
  advancedOnly: boolean
}

const areaSpec = {
  unit: 'mm²' as const,
  min: 0,
  max: 25,
  step: 0.001,
  section: 'regions' as const,
  advancedOnly: false,
}

const widthSpec = {
  unit: 'mm' as const,
  min: 0,
  max: 10,
  step: 0.01,
  section: 'features' as const,
  advancedOnly: true,
}

export const CLEANUP_CONTROL_SPECS: Record<CleanupNumericField, CleanupControlSpec> = {
  minimum_island_area_mm2: {
    ...areaSpec,
    field: 'minimum_island_area_mm2',
    label: 'Island threshold',
    shortLabel: 'Tiny islands',
    help: 'Disconnected color regions below this physical area follow the selected island policy.',
  },
  maximum_tiny_hole_area_mm2: {
    ...areaSpec,
    field: 'maximum_tiny_hole_area_mm2',
    label: 'Flag holes smaller than',
    shortLabel: 'Tiny holes',
    help: 'Enclosed holes below this physical area are flagged for review; filling is always an explicit edit.',
  },
  smoothing_radius_mm: {
    field: 'smoothing_radius_mm',
    label: 'Contour smoothing radius',
    shortLabel: 'Smoothing',
    help: 'Rounds jagged boundaries by this physical radius. Zero preserves the original contour.',
    unit: 'mm',
    min: 0,
    max: 5,
    step: 0.01,
    section: 'regions',
    advancedOnly: false,
  },
  minimum_line_width_mm: {
    ...widthSpec,
    field: 'minimum_line_width_mm',
    label: 'Minimum line width',
    shortLabel: 'Lines',
    help: 'Flags long, narrow printed strokes below this width.',
  },
  minimum_neck_width_mm: {
    ...widthSpec,
    field: 'minimum_neck_width_mm',
    label: 'Minimum neck width',
    shortLabel: 'Necks',
    help: 'Flags narrow connections that can split one printed region into separate pieces.',
  },
  minimum_gap_width_mm: {
    ...widthSpec,
    field: 'minimum_gap_width_mm',
    label: 'Minimum gap width',
    shortLabel: 'Gaps',
    help: 'Flags separations that may close up or fuse during printing.',
  },
  long_line_minimum_length_mm: {
    field: 'long_line_minimum_length_mm',
    label: 'Long-line minimum length',
    shortLabel: 'Long-line length',
    help: 'A narrow stroke at least this long is protected when long-line preservation is enabled.',
    unit: 'mm',
    min: 0,
    max: 1000,
    step: 0.1,
    section: 'long_lines',
    advancedOnly: true,
  },
}

export const CLEANUP_NUMERIC_FIELDS = Object.freeze(
  Object.keys(CLEANUP_CONTROL_SPECS) as CleanupNumericField[],
)

export const CLEANUP_FIELDS = Object.freeze([
  ...CLEANUP_NUMERIC_FIELDS,
  'preserve_long_lines' as const,
])

export const CLEANUP_SECTION_FIELDS: Record<CleanupSection, readonly CleanupControlField[]> = {
  regions: [
    'minimum_island_area_mm2',
    'maximum_tiny_hole_area_mm2',
    'smoothing_radius_mm',
  ],
  features: ['minimum_line_width_mm', 'minimum_neck_width_mm', 'minimum_gap_width_mm'],
  long_lines: ['preserve_long_lines', 'long_line_minimum_length_mm'],
}

export const CLEANUP_PRESETS: ReadonlyArray<{
  id: CleanupPresetId
  label: string
  description: string
}> = [
  {
    id: 'profile',
    label: 'Profile defaults',
    description: 'Use the calibrated recommendations for the selected printer and nozzle.',
  },
  {
    id: 'fine_detail',
    label: 'Fine detail',
    description: 'Retain smaller regions and narrower features with less smoothing.',
  },
  {
    id: 'reliable',
    label: 'Reliable print',
    description: 'Use more conservative physical thresholds for stronger, clearer features.',
  },
]

const recommendationNames: Record<CleanupNumericField, PrintabilityRecommendationName> = {
  minimum_island_area_mm2: 'minimum_island_area_mm2',
  maximum_tiny_hole_area_mm2: 'maximum_tiny_hole_area_mm2',
  smoothing_radius_mm: 'smoothing_radius_mm',
  minimum_line_width_mm: 'minimum_line_width_mm',
  minimum_neck_width_mm: 'minimum_neck_width_mm',
  minimum_gap_width_mm: 'minimum_gap_width_mm',
  long_line_minimum_length_mm: 'long_line_minimum_length_mm',
}

const backendOverrideFields: Partial<Record<CleanupControlField, BackendCleanupOverrideField>> = {
  minimum_island_area_mm2: 'min_island_mm2',
  maximum_tiny_hole_area_mm2: 'max_hole_mm2',
  smoothing_radius_mm: 'smoothing_radius_mm',
  minimum_line_width_mm: 'minimum_line_width_mm',
  minimum_neck_width_mm: 'minimum_neck_width_mm',
  minimum_gap_width_mm: 'minimum_gap_width_mm',
  long_line_minimum_length_mm: 'long_line_minimum_length_mm',
}

const configOverrideFields: Partial<Record<BackendCleanupOverrideField, CleanupControlField>> = {
  min_island_mm2: 'minimum_island_area_mm2',
  max_hole_mm2: 'maximum_tiny_hole_area_mm2',
  smoothing_radius_mm: 'smoothing_radius_mm',
  minimum_line_width_mm: 'minimum_line_width_mm',
  minimum_neck_width_mm: 'minimum_neck_width_mm',
  minimum_gap_width_mm: 'minimum_gap_width_mm',
  long_line_minimum_length_mm: 'long_line_minimum_length_mm',
}

function recommendationValue(
  resolved: ResolvedPrintabilitySettings,
  name: PrintabilityRecommendationName,
) {
  const value = resolved.values.find((candidate) => candidate.name === name)
  if (!value) {
    throw new Error(`Resolved printability settings are missing ${name}`)
  }
  return value
}

export function cleanupEditorModelFromBackend(
  config: JobConfigV1,
  resolved: ResolvedPrintabilitySettings,
  nozzleMm: number,
): {
  values: CleanupControlValues
  profile: CleanupProfileContext
  overriddenFields: CleanupControlField[]
} {
  const recommendations = {} as Record<CleanupNumericField, CleanupRecommendation>
  for (const field of CLEANUP_NUMERIC_FIELDS) {
    const setting = recommendationValue(resolved, recommendationNames[field])
    recommendations[field] = {
      value: setting.value,
      profileValue: setting.profile_value,
      source: setting.source,
      rationale: setting.rationale,
    }
  }

  const islandDiameter = recommendationValue(resolved, 'minimum_island_diameter_mm')
  const tinyHoleDiameter = recommendationValue(resolved, 'maximum_tiny_hole_diameter_mm')
  const values: CleanupControlValues = {
    minimum_island_area_mm2: config.cleanup.min_island_mm2,
    maximum_tiny_hole_area_mm2: config.cleanup.max_hole_mm2,
    smoothing_radius_mm: config.cleanup.smoothing_radius_mm,
    minimum_line_width_mm: config.cleanup.minimum_line_width_mm
      ?? recommendations.minimum_line_width_mm.value,
    minimum_neck_width_mm: config.cleanup.minimum_neck_width_mm
      ?? recommendations.minimum_neck_width_mm.value,
    minimum_gap_width_mm: config.cleanup.minimum_gap_width_mm
      ?? recommendations.minimum_gap_width_mm.value,
    long_line_minimum_length_mm: config.cleanup.long_line_minimum_length_mm
      ?? recommendations.long_line_minimum_length_mm.value,
    preserve_long_lines: config.cleanup.preserve_long_lines,
  }

  const overridden = new Set<CleanupControlField>()
  for (const field of config.cleanup.override_fields ?? []) {
    const controlField = configOverrideFields[field]
    if (controlField) overridden.add(controlField)
  }
  for (const field of CLEANUP_NUMERIC_FIELDS) {
    if (recommendations[field].source === 'user_override') overridden.add(field)
  }
  if (values.preserve_long_lines !== true) overridden.add('preserve_long_lines')

  return {
    values,
    profile: {
      id: resolved.profile_id,
      displayName: resolved.profile_display_name,
      catalogFingerprint: resolved.profile_catalog_fingerprint,
      nozzleMm,
      evidenceStatus: resolved.evidence_status,
      warning: resolved.warning,
      recommendations,
      preserveLongLinesDefault: true,
      islandDiameterMm: islandDiameter.profile_value,
      tinyHoleDiameterMm: tinyHoleDiameter.profile_value,
    },
    overriddenFields: [...overridden],
  }
}

export function backendCleanupOverrideField(
  field: CleanupControlField,
): BackendCleanupOverrideField | null {
  return backendOverrideFields[field] ?? null
}

export function profileDefaultValues(profile: CleanupProfileContext): CleanupControlValues {
  const values = {} as CleanupControlValues
  for (const field of CLEANUP_NUMERIC_FIELDS) values[field] = profile.recommendations[field].profileValue
  values.preserve_long_lines = profile.preserveLongLinesDefault
  return values
}

function roundForField(field: CleanupNumericField, value: number): number {
  const { min, max, step } = CLEANUP_CONTROL_SPECS[field]
  const clamped = Math.max(min, Math.min(max, value))
  const precision = Math.max(0, `${step}`.split('.')[1]?.length ?? 0)
  return Number((Math.round(clamped / step) * step).toFixed(precision))
}

export function applyCleanupPreset(
  preset: CleanupPresetId,
  profile: CleanupProfileContext,
): CleanupControlValues {
  const base = profileDefaultValues(profile)
  if (preset === 'profile') return base

  const multiplier = preset === 'fine_detail' ? 0.75 : 1.25
  const next = { ...base }
  for (const field of CLEANUP_NUMERIC_FIELDS) {
    next[field] = roundForField(field, base[field] * multiplier)
  }
  if (preset === 'fine_detail') next.smoothing_radius_mm = 0
  if (preset === 'reliable') {
    next.smoothing_radius_mm = roundForField(
      'smoothing_radius_mm',
      Math.max(base.smoothing_radius_mm, profile.nozzleMm * 0.25),
    )
  }
  next.preserve_long_lines = true
  return next
}

export function resetCleanupSection(
  current: CleanupControlValues,
  section: CleanupSection,
  profile: CleanupProfileContext,
): CleanupControlValues {
  const defaults = profileDefaultValues(profile)
  const next = { ...current }
  for (const field of CLEANUP_SECTION_FIELDS[section]) {
    if (field === 'preserve_long_lines') next.preserve_long_lines = defaults.preserve_long_lines
    else next[field] = defaults[field]
  }
  return next
}

export function changedCleanupFields(
  before: CleanupControlValues,
  after: CleanupControlValues,
): CleanupControlField[] {
  return CLEANUP_FIELDS.filter((field) => before[field] !== after[field])
}

export function overriddenFieldsForPreset(
  values: CleanupControlValues,
  profile: CleanupProfileContext,
): CleanupControlField[] {
  const defaults = profileDefaultValues(profile)
  return CLEANUP_FIELDS.filter((field) => values[field] !== defaults[field])
}

export function detectCleanupPreset(
  values: CleanupControlValues,
  profile: CleanupProfileContext,
): CleanupPresetSelection {
  for (const preset of CLEANUP_PRESETS) {
    const candidate = applyCleanupPreset(preset.id, profile)
    if (CLEANUP_FIELDS.every((field) => values[field] === candidate[field])) return preset.id
  }
  return 'custom'
}

export type NumericValidation = {
  value: number | null
  valid: boolean
  clamped: boolean
  message: string | null
}

export function validateCleanupNumericInput(
  field: CleanupNumericField,
  rawValue: string | number,
): NumericValidation {
  const parsed = typeof rawValue === 'number' ? rawValue : Number(rawValue)
  if (rawValue === '' || !Number.isFinite(parsed)) {
    return { value: null, valid: false, clamped: false, message: 'Enter a number.' }
  }
  const spec = CLEANUP_CONTROL_SPECS[field]
  const value = roundForField(field, parsed)
  const clamped = value !== parsed
  return {
    value,
    valid: true,
    clamped,
    message: clamped ? `Limited to ${spec.min}–${spec.max} ${spec.unit}.` : null,
  }
}

export function equivalentDiameterMm(areaMm2: number): number {
  return 2 * Math.sqrt(Math.max(0, areaMm2) / Math.PI)
}

function formatPixels(value: number): string {
  if (value < 10) return value.toFixed(1)
  return Math.round(value).toLocaleString()
}

export function pixelGuidance(
  field: CleanupNumericField,
  value: number,
  pixelScale: PixelScale | null,
): string | null {
  if (!pixelScale || pixelScale.mmPerPixelX <= 0 || pixelScale.mmPerPixelY <= 0) return null
  const spec = CLEANUP_CONTROL_SPECS[field]
  if (spec.unit === 'mm²') {
    const pixels = value / (pixelScale.mmPerPixelX * pixelScale.mmPerPixelY)
    const diameter = equivalentDiameterMm(value)
    const xDiameter = diameter / pixelScale.mmPerPixelX
    const yDiameter = diameter / pixelScale.mmPerPixelY
    const diameterText = approximatelyIsotropic(pixelScale)
      ? `${formatPixels(xDiameter)} px diameter`
      : `${formatPixels(xDiameter)} px wide × ${formatPixels(yDiameter)} px tall`
    return `About ${formatPixels(pixels)} source pixels, equivalent to ${diameterText}.`
  }
  const xPixels = value / pixelScale.mmPerPixelX
  const yPixels = value / pixelScale.mmPerPixelY
  if (approximatelyIsotropic(pixelScale)) return `About ${formatPixels(xPixels)} px in the current preview.`
  return `About ${formatPixels(xPixels)} px horizontally or ${formatPixels(yPixels)} px vertically.`
}

function approximatelyIsotropic(scale: PixelScale): boolean {
  const largest = Math.max(scale.mmPerPixelX, scale.mmPerPixelY)
  return Math.abs(scale.mmPerPixelX - scale.mmPerPixelY) <= largest * 0.005
}

export function backendOverrideDelta(
  setFields: readonly CleanupControlField[],
  clearFields: readonly CleanupControlField[],
): { set: BackendCleanupOverrideField[]; clear: BackendCleanupOverrideField[] } {
  return {
    set: setFields.map(backendCleanupOverrideField).filter((field) => field !== null),
    clear: clearFields.map(backendCleanupOverrideField).filter((field) => field !== null),
  }
}
