import type { RegionOperation, RevisionResource, RevisionSummary } from '../../contracts'

export type DraftPersistence = 'saved' | 'saving' | 'error'
export type ArtifactFreshness = 'current' | 'stale' | 'missing'

export type RevisionValueDelta = {
  path: string
  label: string
  before: string
  after: string
}

export type RevisionOperationDelta = {
  kind: 'added' | 'removed' | 'changed'
  identity: string
  label: string
  before?: RegionOperation
  after?: RegionOperation
}

export type RevisionComparisonResult = {
  config: RevisionValueDelta[]
  operations: RevisionOperationDelta[]
  unchanged: boolean
}

export type RegionalEditEvidence = {
  classification: 'deterministic' | 'exact_seeded' | 'best_effort'
  label: string
  reason: string
  executionKind: 'local' | 'provider'
  providerModel: string | null
  outputSha256: string | null
}

export function regionalEditEvidence(operation: RegionOperation): RegionalEditEvidence | null {
  const regional = operation.provenance.regional_edit
  if (!isRecord(regional)) return null
  const classification = regional.reproducibility
  const executionKind = regional.execution_kind
  const reason = regional.reproducibility_reason
  if (
    !['deterministic', 'exact_seeded', 'best_effort'].includes(String(classification))
    || !['local', 'provider'].includes(String(executionKind))
    || typeof reason !== 'string'
  ) return null
  const labels = {
    deterministic: 'Deterministic replay',
    exact_seeded: 'Seeded reproducible',
    best_effort: 'Best effort',
  } as const
  const providerModel = executionKind === 'provider'
    && typeof regional.provider_id === 'string'
    && typeof regional.model_id === 'string'
    && typeof regional.model_version === 'string'
    ? `${regional.provider_id} · ${regional.model_id} · ${regional.model_version}`
    : null
  return {
    classification: classification as RegionalEditEvidence['classification'],
    label: labels[classification as RegionalEditEvidence['classification']],
    reason,
    executionKind: executionKind as RegionalEditEvidence['executionKind'],
    providerModel,
    outputSha256: typeof regional.output_sha256 === 'string' ? regional.output_sha256 : null,
  }
}

export type PublicationGate =
  | { allowed: true; requiresArtifactConfirmation: boolean; message: string }
  | { allowed: false; requiresArtifactConfirmation: false; message: string }

export function publicationGate(
  label: string,
  persistence: DraftPersistence,
  artifacts: ArtifactFreshness,
): PublicationGate {
  if (!label.trim()) {
    return {
      allowed: false,
      requiresArtifactConfirmation: false,
      message: 'Give this revision a name before publishing.',
    }
  }
  if (persistence === 'saving') {
    return {
      allowed: false,
      requiresArtifactConfirmation: false,
      message: 'Wait for the latest draft save to finish.',
    }
  }
  if (persistence === 'error') {
    return {
      allowed: false,
      requiresArtifactConfirmation: false,
      message: 'Retry the draft save before publishing.',
    }
  }
  if (artifacts !== 'current') {
    return {
      allowed: true,
      requiresArtifactConfirmation: true,
      message:
        artifacts === 'stale'
          ? 'The visible preview is older than this draft.'
          : 'This draft does not have a completed preview.',
    }
  }
  return {
    allowed: true,
    requiresArtifactConfirmation: false,
    message: 'The saved draft and preview evidence are current.',
  }
}

export function revisionRelationship(
  revision: RevisionResource | RevisionSummary,
  activeRevisionId: string | null,
  draftBaseRevisionId: string | null,
): 'active-base' | 'active' | 'draft-base' | 'older' {
  if (revision.id === activeRevisionId && revision.id === draftBaseRevisionId) return 'active-base'
  if (revision.id === activeRevisionId) return 'active'
  if (revision.id === draftBaseRevisionId) return 'draft-base'
  return 'older'
}

export function relationshipLabel(relationship: ReturnType<typeof revisionRelationship>): string {
  switch (relationship) {
    case 'active-base':
      return 'Current · draft base'
    case 'active':
      return 'Current published'
    case 'draft-base':
      return 'Draft branched here'
    case 'older':
      return 'Older revision'
  }
}

export function evidenceLabel(revision: RevisionResource | RevisionSummary): string {
  const count = revision.preview_evidence.artifact_count
  switch (revision.preview_evidence.status) {
    case 'fresh':
      return `${count} verified artifact${count === 1 ? '' : 's'}`
    case 'stale':
      return 'Published without current preview artifacts'
    case 'missing':
      return 'No preview artifacts at publication'
    case 'legacy':
      return 'Legacy preview provenance'
  }
}

export function compareRevisions(
  before: RevisionResource,
  after: RevisionResource,
): RevisionComparisonResult {
  const beforeConfig = flattenValue(before.config)
  const afterConfig = flattenValue(after.config)
  const config = [...new Set([...beforeConfig.keys(), ...afterConfig.keys()])]
    .sort((left, right) => left.localeCompare(right))
    .flatMap((path) => {
      const previous = beforeConfig.get(path)
      const next = afterConfig.get(path)
      if (stableStringify(previous) === stableStringify(next)) return []
      return [{
        path,
        label: comparisonLabel(path),
        before: displayValue(previous),
        after: displayValue(next),
      }]
    })
  const operations = compareOperations(before.operations, after.operations)
  return { config, operations, unchanged: config.length === 0 && operations.length === 0 }
}

function flattenValue(value: unknown, path = '', result = new Map<string, unknown>()): Map<string, unknown> {
  if (Array.isArray(value)) {
    if (value.length === 0) {
      result.set(path, [])
      return result
    }
    value.forEach((item, index) => {
      const stableId = isRecord(item) && typeof item.id === 'string' ? item.id : String(index + 1)
      flattenValue(item, `${path}[${stableId}]`, result)
    })
    return result
  }
  if (isRecord(value)) {
    const entries = Object.entries(value).sort(([left], [right]) => left.localeCompare(right))
    if (entries.length === 0) result.set(path, {})
    entries.forEach(([key, item]) => flattenValue(item, path ? `${path}.${key}` : key, result))
    return result
  }
  result.set(path, value)
  return result
}

function compareOperations(
  before: RegionOperation[],
  after: RegionOperation[],
): RevisionOperationDelta[] {
  const previous = operationOccurrences(before)
  const next = operationOccurrences(after)
  const deltas: RevisionOperationDelta[] = []
  const identities = [...new Set([...previous.keys(), ...next.keys()])]
    .sort((left, right) => left.localeCompare(right))
  identities.forEach((identity) => {
    const oldOperation = previous.get(identity)
    const newOperation = next.get(identity)
    if (!oldOperation && newOperation) {
      deltas.push({ kind: 'added', identity, label: operationLabel(newOperation), after: newOperation })
    } else if (oldOperation && !newOperation) {
      deltas.push({ kind: 'removed', identity, label: operationLabel(oldOperation), before: oldOperation })
    } else if (oldOperation && newOperation && stableStringify(oldOperation) !== stableStringify(newOperation)) {
      deltas.push({
        kind: 'changed',
        identity,
        label: operationLabel(newOperation),
        before: oldOperation,
        after: newOperation,
      })
    }
  })
  return deltas
}

function operationOccurrences(operations: RegionOperation[]): Map<string, RegionOperation> {
  const seen = new Map<string, number>()
  const result = new Map<string, RegionOperation>()
  operations.forEach((operation) => {
    const base = operationIdentity(operation)
    const occurrence = (seen.get(base) ?? 0) + 1
    seen.set(base, occurrence)
    result.set(`${base}#${occurrence}`, operation)
  })
  return result
}

function operationIdentity(operation: RegionOperation): string {
  const candidates = [
    operation.provenance.operation_id,
    operation.provenance.id,
    operation.parameters.operation_id,
    operation.parameters.id,
  ]
  const explicit = candidates.find((value) => typeof value === 'string' && value.length > 0)
  if (typeof explicit === 'string') return `${operation.operation_type}:${explicit}`
  return `${operation.operation_type}:${stableStringify(operation.selection)}`
}

function operationLabel(operation: RegionOperation): string {
  return operation.operation_type.replaceAll('-', ' ').replaceAll('_', ' ')
}

function comparisonLabel(path: string): string {
  return path
    .replaceAll('.', ' · ')
    .replaceAll('_', ' ')
    .replace(/\[([^\]]+)\]/g, ' · $1')
}

function displayValue(value: unknown): string {
  if (value === undefined) return 'Not set'
  if (value === null) return 'None'
  if (typeof value === 'boolean') return value ? 'On' : 'Off'
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  return stableStringify(value)
}

function stableStringify(value: unknown): string {
  if (value === undefined) return 'undefined'
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
