import type {
  HoleAnalysis,
  PaletteColor,
  RegionGraph,
  RegionOperation,
} from '../../contracts'

export type ManualAction = 'protect' | 'merge' | 'delete' | 'fill' | 'recolor' | 'thicken'
export type ManualScopeMode = 'selected' | 'similar'

export type SimilarRegionPredicate = {
  area_ratio_tolerance: number
  minimum_width_ratio_tolerance: number
  compactness_tolerance: number
  match_border_contact: boolean
  match_neighbor_labels: boolean
}

export type RegionScope = {
  mode: ManualScopeMode
  anchor_region_ids: string[]
  predicate: SimilarRegionPredicate | null
  resolved_region_ids: string[]
  resolution_fingerprint: string
}

export type ManualOperationDraft = {
  action: ManualAction
  scopeMode: ManualScopeMode
  selectedRegionId: string
  selectedHoleFeatureId: string | null
  targetLabel: number | null
  radiusMm: number
  editableLabels: number[]
  predicate: SimilarRegionPredicate
}

export type ManualOperationResolution = {
  action: ManualAction
  affectedRegionIds: string[]
  affectedFeatureIds: string[]
  predicateLabel: string
  valid: boolean
  error: string | null
}

export type ManualOperationContext = {
  graph: RegionGraph
  graphFingerprint: string
  configFingerprint: string
  palette: PaletteColor[]
  holeAnalysis: HoleAnalysis | null
  holeAnalysisFingerprint: string | null
}

export const MAX_SCOPED_REGIONS = 256

export const DEFAULT_SIMILAR_REGION_PREDICATE: SimilarRegionPredicate = {
  area_ratio_tolerance: 0.25,
  minimum_width_ratio_tolerance: 0.25,
  compactness_tolerance: 0.15,
  match_border_contact: true,
  match_neighbor_labels: false,
}

const sortedUnique = <T extends string | number>(values: T[]) => [...new Set(values)].sort(
  (first, second) => first < second ? -1 : first > second ? 1 : 0,
)

function ratioMatches(first: number, second: number, tolerance: number) {
  if (first === second) return true
  if (first <= 0 || second <= 0) return false
  return Math.abs(first - second) / Math.max(first, second) <= tolerance
}

function neighborLabels(graph: RegionGraph, regionId: string) {
  return sortedUnique(
    graph.adjacency.flatMap((edge) => {
      const neighborId = edge.first_region_id === regionId
        ? edge.second_region_id
        : edge.second_region_id === regionId
          ? edge.first_region_id
          : null
      const neighbor = graph.regions.find((candidate) => candidate.id === neighborId)
      return neighbor ? [neighbor.label] : []
    }),
  )
}

function regionMatches(
  graph: RegionGraph,
  candidate: RegionGraph['regions'][number],
  anchor: RegionGraph['regions'][number],
  predicate: SimilarRegionPredicate,
) {
  if (candidate.label !== anchor.label) return false
  if (!ratioMatches(candidate.area_mm2, anchor.area_mm2, predicate.area_ratio_tolerance)) return false
  if (!ratioMatches(
    candidate.width_estimate.minimum_mm,
    anchor.width_estimate.minimum_mm,
    predicate.minimum_width_ratio_tolerance,
  )) return false
  if (Math.abs(candidate.compactness - anchor.compactness) > predicate.compactness_tolerance) return false
  if (
    predicate.match_border_contact &&
    candidate.border_contact.touches_canvas_border !== anchor.border_contact.touches_canvas_border
  ) return false
  if (
    predicate.match_neighbor_labels &&
    JSON.stringify(neighborLabels(graph, candidate.id)) !== JSON.stringify(neighborLabels(graph, anchor.id))
  ) return false
  return true
}

export function resolveSimilarRegions(
  graph: RegionGraph,
  anchorRegionIds: string[],
  predicate: SimilarRegionPredicate,
) {
  const anchors = sortedUnique(anchorRegionIds)
    .map((id) => graph.regions.find((region) => region.id === id))
    .filter((region): region is RegionGraph['regions'][number] => Boolean(region))
  return sortedUnique(
    graph.regions
      .filter((candidate) => anchors.some((anchor) => regionMatches(graph, candidate, anchor, predicate)))
      .map((region) => region.id),
  )
}

export function similarRegionPredicateLabel(
  graph: RegionGraph,
  anchorRegionId: string,
  predicate: SimilarRegionPredicate,
) {
  const anchor = graph.regions.find((region) => region.id === anchorRegionId)
  if (!anchor) return 'No valid anchor region.'
  const qualifiers = [
    `same palette label (${anchor.label})`,
    `area within ${Math.round(predicate.area_ratio_tolerance * 100)}%`,
    `minimum width within ${Math.round(predicate.minimum_width_ratio_tolerance * 100)}%`,
    `compactness within ${predicate.compactness_tolerance.toFixed(2)}`,
  ]
  if (predicate.match_border_contact) qualifiers.push('same canvas-edge contact')
  if (predicate.match_neighbor_labels) qualifiers.push('same neighboring label set')
  return `Match ${qualifiers.join(', ')}. Candidates match the selected anchor deterministically.`
}

function matchingHoleFeatures(
  analysis: HoleAnalysis,
  anchorFeatureId: string,
  similar: boolean,
) {
  const anchor = analysis.features.find((feature) => feature.id === anchorFeatureId)
  if (!anchor) return []
  if (!similar) return [anchor]
  return analysis.features.filter((feature) =>
    feature.kind === anchor.kind &&
    feature.center_kind === anchor.center_kind &&
    feature.ring_label === anchor.ring_label,
  )
}

export function holePredicateLabel(analysis: HoleAnalysis, featureId: string) {
  const feature = analysis.features.find((candidate) => candidate.id === featureId)
  if (!feature) return 'No classified hole feature is selected.'
  return `Match feature kind “${feature.kind}”, center kind “${feature.center_kind}”, and ring palette label ${feature.ring_label}.`
}

export function holeFeaturesForRegion(analysis: HoleAnalysis | null, regionId: string) {
  if (!analysis) return []
  return analysis.features
    .filter((feature) =>
      feature.center_region_id === regionId || feature.ring_region_id === regionId,
    )
    .sort((first, second) => first.id.localeCompare(second.id))
}

export function resolveManualOperation(
  context: ManualOperationContext,
  draft: ManualOperationDraft,
): ManualOperationResolution {
  const selected = context.graph.regions.find((region) => region.id === draft.selectedRegionId)
  if (!selected) {
    return { action: draft.action, affectedRegionIds: [], affectedFeatureIds: [], predicateLabel: '', valid: false, error: 'Select a current region first.' }
  }

  if (draft.action === 'fill') {
    if (!context.holeAnalysis || !context.holeAnalysisFingerprint || !draft.selectedHoleFeatureId) {
      return { action: draft.action, affectedRegionIds: [], affectedFeatureIds: [], predicateLabel: '', valid: false, error: 'Select a classified hole feature to fill.' }
    }
    const features = matchingHoleFeatures(
      context.holeAnalysis,
      draft.selectedHoleFeatureId,
      draft.scopeMode === 'similar',
    )
    if (features.length > MAX_SCOPED_REGIONS) {
      return {
        action: draft.action,
        affectedRegionIds: [],
        affectedFeatureIds: [],
        predicateLabel: holePredicateLabel(context.holeAnalysis, draft.selectedHoleFeatureId),
        valid: false,
        error: `This scope matches ${features.length} features; narrow it to ${MAX_SCOPED_REGIONS} or fewer before saving.`,
      }
    }
    return {
      action: draft.action,
      affectedRegionIds: sortedUnique(features.flatMap((feature) =>
        [feature.center_region_id, feature.ring_region_id].filter((id): id is string => Boolean(id)),
      )),
      affectedFeatureIds: features.map((feature) => feature.id).sort(),
      predicateLabel: draft.scopeMode === 'similar'
        ? holePredicateLabel(context.holeAnalysis, draft.selectedHoleFeatureId)
        : `Only feature ${draft.selectedHoleFeatureId}.`,
      valid: features.length > 0,
      error: features.length ? null : 'The selected hole feature is not present in this preview.',
    }
  }

  const affectedRegionIds = draft.scopeMode === 'similar'
    ? resolveSimilarRegions(context.graph, [selected.id], draft.predicate)
    : [selected.id]
  const predicateLabel = draft.scopeMode === 'similar'
    ? similarRegionPredicateLabel(context.graph, selected.id, draft.predicate)
    : `Only selected region ${selected.id}.`

  if (affectedRegionIds.length > MAX_SCOPED_REGIONS) {
    return {
      action: draft.action,
      affectedRegionIds: [],
      affectedFeatureIds: [],
      predicateLabel,
      valid: false,
      error: `This scope matches ${affectedRegionIds.length} regions; narrow it to ${MAX_SCOPED_REGIONS} or fewer before saving.`,
    }
  }

  if ((draft.action === 'merge' || draft.action === 'recolor') && draft.targetLabel === null) {
    return { action: draft.action, affectedRegionIds, affectedFeatureIds: [], predicateLabel, valid: false, error: 'Choose a target palette color.' }
  }
  if ((draft.action === 'merge' || draft.action === 'recolor') && draft.targetLabel === selected.label) {
    return { action: draft.action, affectedRegionIds, affectedFeatureIds: [], predicateLabel, valid: false, error: 'The target color must differ from the selected region.' }
  }
  if (draft.action === 'merge') {
    const selectedIds = new Set(affectedRegionIds)
    const invalid = affectedRegionIds.find((regionId) => !context.graph.adjacency.some((edge) => {
      if (edge.first_region_id !== regionId && edge.second_region_id !== regionId) return false
      const neighborId = edge.first_region_id === regionId ? edge.second_region_id : edge.first_region_id
      return context.graph.regions.find((region) => region.id === neighborId)?.label === draft.targetLabel && !selectedIds.has(neighborId)
    }))
    if (invalid) {
      return { action: draft.action, affectedRegionIds, affectedFeatureIds: [], predicateLabel, valid: false, error: 'Every merged region must touch an unselected region in the target color.' }
    }
  }
  if (draft.action === 'thicken') {
    if (!(draft.radiusMm > 0 && draft.radiusMm <= 10)) {
      return { action: draft.action, affectedRegionIds, affectedFeatureIds: [], predicateLabel, valid: false, error: 'Thickness must be greater than 0 mm and no more than 10 mm.' }
    }
    if (!draft.editableLabels.length) {
      return { action: draft.action, affectedRegionIds, affectedFeatureIds: [], predicateLabel, valid: false, error: 'Choose at least one replaceable palette label.' }
    }
  }
  return { action: draft.action, affectedRegionIds, affectedFeatureIds: [], predicateLabel, valid: affectedRegionIds.length > 0, error: affectedRegionIds.length ? null : 'No regions match this scope.' }
}

const PYTHON_FLOAT_KEYS = new Set([
  'area_ratio_tolerance',
  'minimum_width_ratio_tolerance',
  'compactness_tolerance',
])

function canonicalValue(value: unknown, key?: string): string {
  if (typeof value === 'number' && key && PYTHON_FLOAT_KEYS.has(key) && Number.isInteger(value)) return `${value}.0`
  if (value === null || typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map((item) => canonicalValue(item)).join(',')}]`
  const record = value as Record<string, unknown>
  return `{${Object.keys(record).sort().map((itemKey) => `${JSON.stringify(itemKey)}:${canonicalValue(record[itemKey], itemKey)}`).join(',')}}`
}

async function sha256(value: unknown) {
  const bytes = new TextEncoder().encode(canonicalValue(value))
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

function commandId(randomId = crypto.randomUUID()) {
  return `cmd_${randomId.replaceAll('-', '').slice(0, 24)}`
}

export async function buildManualRegionOperation(
  context: ManualOperationContext,
  draft: ManualOperationDraft,
  resolution: ManualOperationResolution,
  identity?: { commandId?: string; createdAt?: string },
): Promise<RegionOperation> {
  if (!resolution.valid) throw new Error(resolution.error ?? 'Manual operation is invalid.')
  const anchorRegionIds = [draft.selectedRegionId]
  const resolutionPayload = {
    schema_version: 1,
    graph_fingerprint: context.graphFingerprint,
    mode: draft.scopeMode,
    anchor_region_ids: anchorRegionIds,
    predicate: draft.scopeMode === 'similar' ? draft.predicate : null,
    resolved_region_ids: resolution.affectedRegionIds,
  }
  const scope: RegionScope = {
    mode: draft.scopeMode,
    anchor_region_ids: anchorRegionIds,
    predicate: draft.scopeMode === 'similar' ? draft.predicate : null,
    resolved_region_ids: resolution.affectedRegionIds,
    resolution_fingerprint: await sha256(resolutionPayload),
  }
  const selector = {
    graph_fingerprint: context.graphFingerprint,
    config_fingerprint: context.configFingerprint,
    scope,
  }
  const command = {
    schema_version: 1,
    command_type: 'region_operation',
    command_id: identity?.commandId ?? commandId(),
    source: 'manual',
    created_at: identity?.createdAt ?? new Date().toISOString(),
    provenance: {
      ui: 'manual-operations-panel-v1',
      scope_summary: resolution.predicateLabel,
    },
    selector,
    operation: draft.action,
    target_label: draft.action === 'merge' || draft.action === 'recolor' ? draft.targetLabel : null,
    radius_mm: draft.action === 'thicken' ? draft.radiusMm : null,
    editable_labels: draft.action === 'thicken' ? sortedUnique(draft.editableLabels) : [],
  }
  return {
    operation_type: 'editor_command_v1',
    selection: selector,
    parameters: { command },
    source: 'manual',
    provenance: { editor_schema_version: 1, ui: 'manual-operations-panel-v1' },
  }
}

export async function buildFillOperation(
  context: ManualOperationContext,
  draft: ManualOperationDraft,
  resolution: ManualOperationResolution,
  identity?: { commandId?: string; createdAt?: string },
): Promise<RegionOperation> {
  if (!resolution.valid || !context.holeAnalysis || !context.holeAnalysisFingerprint) throw new Error(resolution.error ?? 'Fill operation is invalid.')
  const selector = {
    graph_fingerprint: context.graphFingerprint,
    config_fingerprint: context.configFingerprint,
    feature_ids: resolution.affectedFeatureIds,
    classification_fingerprint: context.holeAnalysisFingerprint,
  }
  const command = {
    schema_version: 1,
    command_type: 'hole_correction',
    command_id: identity?.commandId ?? commandId(),
    source: 'manual',
    created_at: identity?.createdAt ?? new Date().toISOString(),
    provenance: {
      ui: 'manual-operations-panel-v1',
      scope_mode: draft.scopeMode,
      scope_summary: resolution.predicateLabel,
      resolved_feature_ids: resolution.affectedFeatureIds,
    },
    selector,
    analysis_options: context.holeAnalysis.options,
    policy: 'fill_hole',
    explicit_target_label: null,
  }
  return {
    operation_type: 'editor_command_v1',
    selection: selector,
    parameters: { command },
    source: 'manual',
    provenance: { editor_schema_version: 1, ui: 'manual-operations-panel-v1' },
  }
}

export async function buildManualOperation(
  context: ManualOperationContext,
  draft: ManualOperationDraft,
  resolution: ManualOperationResolution,
  identity?: { commandId?: string; createdAt?: string },
) {
  return draft.action === 'fill'
    ? buildFillOperation(context, draft, resolution, identity)
    : buildManualRegionOperation(context, draft, resolution, identity)
}
