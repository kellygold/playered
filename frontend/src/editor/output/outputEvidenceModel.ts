export type ValidationWarningEvidence = Readonly<{
  category: string | null
  message: string
}>

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function warningArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function normalizeCategory(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  return value.trim().replaceAll('_', ' ')
}

function normalizeWarning(value: unknown): ValidationWarningEvidence | null {
  if (typeof value === 'string') {
    const message = value.trim()
    return message ? { category: null, message: message.slice(0, 500) } : null
  }
  const item = record(value)
  if (!item) return null
  const candidate = [item.message, item.text, item.title, item.raw].find(
    (entry) => typeof entry === 'string' && entry.trim(),
  )
  if (typeof candidate !== 'string') return null
  return {
    category: normalizeCategory(item.category ?? item.code ?? item.severity),
    message: candidate.trim().slice(0, 500),
  }
}

export function validationWarnings(payload: unknown): readonly ValidationWarningEvidence[] {
  const root = record(payload)
  const result = record(root?.result)
  const rootReport = record(root?.report)
  const resultReport = record(result?.report)
  const slicerReport = record(root?.slicer_report)
  const candidates = [
    ...warningArray(root?.warnings),
    ...warningArray(result?.warnings),
    ...warningArray(rootReport?.warnings),
    ...warningArray(resultReport?.warnings),
    ...warningArray(slicerReport?.warnings),
  ]
  const unique = new Map<string, ValidationWarningEvidence>()
  for (const candidate of candidates) {
    const warning = normalizeWarning(candidate)
    if (!warning) continue
    unique.set(`${warning.category ?? ''}:${warning.message}`, warning)
    if (unique.size === 20) break
  }
  return [...unique.values()]
}

export function formatArtifactBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B (${bytes} bytes)`
  const units = ['KiB', 'MiB', 'GiB']
  let value = bytes / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024
    unit = units[index]
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${unit} (${bytes} bytes)`
}
