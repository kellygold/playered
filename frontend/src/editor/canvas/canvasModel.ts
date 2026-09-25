export type EditorCanvasViewId =
  | 'original'
  | 'quantized'
  | 'processed'
  | 'risk'
  | 'mask'
  | 'geometry'
  | 'slicer'

export type EditorCanvasComparisonMode = 'single' | 'split' | 'flicker'
export type EditorCanvasTool = 'inspect' | 'pan' | 'rectangle' | 'lasso' | 'brush'

export type EditorCanvasSpace = {
  widthPx: number
  heightPx: number
  widthMm: number
  heightMm: number
}

export type EditorCanvasRaster = {
  url: string
  sha256: string
  artifactKind: string
  mediaType: string
  interpolation?: 'nearest' | 'smooth'
}

export type EditorCanvasRiskOverlay = {
  id: string
  severity: 'info' | 'warning' | 'error'
  title: string
  presentation: 'feature-bounds' | 'region-extents' | 'list-only'
  affectedRegionCount: number
  boundsMm: {
    xMm: number
    yMm: number
    widthMm: number
    heightMm: number
  }[]
  boundsPx: {
    x: number
    y: number
    width: number
    height: number
  }[]
}

export type EditorCanvasViewSource = {
  view: EditorCanvasViewId
  label: string
  description: string
  raster: EditorCanvasRaster
  space: EditorCanvasSpace
  risks?: EditorCanvasRiskOverlay[]
  state?: 'ready' | 'loading' | 'error'
  errorMessage?: string
}

export type EditorCanvasSourceCatalog = {
  sources: Partial<Record<EditorCanvasViewId, EditorCanvasViewSource>>
  unavailable: Partial<Record<EditorCanvasViewId, string>>
}

export type CanvasArtifactLike = {
  kind: string
  download_url: string
  sha256: string
  media_type: string
  metadata?: Record<string, unknown>
}

export type CanvasRiskReportLike = {
  warnings: {
    id: string
    code?: string
    severity: 'info' | 'warning' | 'error'
    title: string
    affected_region_ids?: string[]
    affected_bounds: {
      x_mm: number
      y_mm: number
      width_mm: number
      height_mm: number
    }[]
  }[]
}

export type CanvasRegionGraphLike = {
  width_px: number
  height_px: number
  regions: {
    id: string
    pixel_bounds: { x: number; y: number; width: number; height: number }
  }[]
}

export type CanvasPreviewSourceInput = {
  artifacts: CanvasArtifactLike[]
  space: EditorCanvasSpace
  riskReport?: CanvasRiskReportLike | null
  regionGraph?: CanvasRegionGraphLike | null
  slicerUnavailableReason?: string | null
  cacheKey?: boolean
}

export const MAX_FRAGMENTATION_REGION_EXTENTS = 128

export const EDITOR_CANVAS_VIEWS: readonly {
  id: EditorCanvasViewId
  label: string
  shortLabel: string
  description: string
}[] = [
  {
    id: 'original',
    label: 'Original',
    shortLabel: 'Original',
    description: 'Your image at the current crop and print size, before palette reduction or cleanup.',
  },
  {
    id: 'quantized',
    label: 'Quantized',
    shortLabel: 'Quantized',
    description: 'Your image reduced to the chosen palette, before cleanup. Compare with Processed to see what cleanup changes.',
  },
  {
    id: 'processed',
    label: 'Processed',
    shortLabel: 'Processed',
    description: 'Your chosen colors after cleanup and local edits. Solid regions show the intended artwork, not nozzle paths. Thin details may change when sliced.',
  },
  {
    id: 'risk',
    label: 'Print risks',
    shortLabel: 'Risk',
    description: 'Highlights details that may be difficult to print, such as tiny islands, thin lines and narrow gaps. Select a warning to inspect it.',
  },
  {
    id: 'mask',
    label: 'Operation mask',
    shortLabel: 'Mask',
    description: 'Shows the area affected by cleanup or an edit, rather than the artwork colors.',
  },
  {
    id: 'geometry',
    label: 'Geometry',
    shortLabel: 'Geometry',
    description: 'A flat view of the generated model, aligned with the image so you can check which details survived the build.',
  },
  {
    id: 'slicer',
    label: 'Sliced',
    shortLabel: 'Sliced',
    description: 'Top-down paths from this export’s Bambu slice, using its extrusion widths and colors. Gaps can reveal the backing color. This is an approximation, not a physical print simulation.',
  },
] as const

const VIEW_METADATA = Object.fromEntries(EDITOR_CANVAS_VIEWS.map((view) => [view.id, view])) as Record<
  EditorCanvasViewId,
  (typeof EDITOR_CANVAS_VIEWS)[number]
>

const DIRECT_ARTIFACT_KINDS: Partial<Record<EditorCanvasViewId, readonly string[]>> = {
  original: ['preview-image'],
  quantized: ['quantized-preview-image'],
  processed: ['palette-preview-image'],
  mask: [
    'automatic-cleanup-mask-preview-image',
    'editor-mask-preview-image',
    'editor-changed-mask-preview-image',
    'editor-protected-mask-preview-image',
    'color-mask-preview-image',
  ],
  geometry: ['geometry-preview', 'geometry-preview-image', 'geometry-raster-preview-image'],
  slicer: ['slicer-preview-image', 'bambu-slicer-preview-image'],
}

function validDimension(value: number) {
  return Number.isFinite(value) && value > 0
}

export function validateEditorCanvasSpace(space: EditorCanvasSpace): EditorCanvasSpace {
  if (
    !Number.isInteger(space.widthPx) ||
    !Number.isInteger(space.heightPx) ||
    !validDimension(space.widthPx) ||
    !validDimension(space.heightPx) ||
    !validDimension(space.widthMm) ||
    !validDimension(space.heightMm)
  ) {
    throw new Error('Canvas render space requires positive integer pixels and positive millimetres.')
  }
  return { ...space }
}

function displayUrl(artifact: CanvasArtifactLike, cacheKey: boolean) {
  if (!cacheKey || !artifact.sha256) return artifact.download_url
  const separator = artifact.download_url.includes('?') ? '&' : '?'
  return `${artifact.download_url}${separator}sha=${encodeURIComponent(artifact.sha256)}`
}

function findDisplayArtifact(artifacts: CanvasArtifactLike[], kinds: readonly string[]) {
  return kinds
    .map((kind) => artifacts.find((artifact) => artifact.kind === kind))
    .find((artifact) => artifact !== undefined && artifact.media_type.startsWith('image/'))
}

function sourceFromArtifact(
  view: EditorCanvasViewId,
  artifact: CanvasArtifactLike,
  space: EditorCanvasSpace,
  cacheKey: boolean,
): EditorCanvasViewSource {
  const metadata = VIEW_METADATA[view]
  return {
    view,
    label: metadata.label,
    description: metadata.description,
    raster: {
      url: displayUrl(artifact, cacheKey),
      sha256: artifact.sha256,
      artifactKind: artifact.kind,
      mediaType: artifact.media_type,
      interpolation: 'nearest',
    },
    space,
  }
}

export function createEditorCanvasSourceCatalog({
  artifacts,
  space: inputSpace,
  riskReport,
  regionGraph,
  slicerUnavailableReason,
  cacheKey = true,
}: CanvasPreviewSourceInput): EditorCanvasSourceCatalog {
  const space = validateEditorCanvasSpace(inputSpace)
  const sources: Partial<Record<EditorCanvasViewId, EditorCanvasViewSource>> = {}
  const unavailable: Partial<Record<EditorCanvasViewId, string>> = {}

  for (const view of EDITOR_CANVAS_VIEWS) {
    if (view.id === 'risk') continue
    const kinds = DIRECT_ARTIFACT_KINDS[view.id] ?? []
    const artifact = findDisplayArtifact(artifacts, kinds)
    if (artifact) sources[view.id] = sourceFromArtifact(view.id, artifact, space, cacheKey)
    else if (view.id === 'mask') {
      unavailable.mask =
        'No mask preview is available yet. A mask shows the area affected by cleanup or an edit.'
    } else if (view.id === 'geometry') {
      unavailable.geometry =
        'Build a 3MF from Output readiness to generate the Geometry view.'
    } else if (view.id === 'slicer') {
      unavailable.slicer = slicerUnavailableReason
        ? `Sliced preview unavailable for this export: ${slicerUnavailableReason}. Inspect the project in Bambu Studio.`
        : 'Build a new 3MF to see its sliced paths here. If a preview cannot be generated, inspect the exported project in Bambu Studio.'
    } else {
      unavailable[view.id] = `No ${view.label.toLowerCase()} image artifact is available.`
    }
  }

  const processed = sources.processed
  if (processed && riskReport) {
    const alignedRegions = regionGraph &&
      regionGraph.width_px === space.widthPx &&
      regionGraph.height_px === space.heightPx
      ? new Map(regionGraph.regions.map((region) => [region.id, region.pixel_bounds]))
      : null
    sources.risk = {
      ...processed,
      view: 'risk',
      label: VIEW_METADATA.risk.label,
      description: VIEW_METADATA.risk.description,
      risks: riskReport.warnings.map((warning) => {
        const affectedRegionIds = warning.affected_region_ids ?? []
        const fragmentationBounds = warning.code === 'excess_fragmentation' && alignedRegions
          ? affectedRegionIds.map((regionId) => alignedRegions.get(regionId))
          : []
        const hasExactFragmentationBounds =
          warning.code === 'excess_fragmentation' &&
          affectedRegionIds.length > 0 &&
          fragmentationBounds.length === affectedRegionIds.length &&
          fragmentationBounds.every((bounds) => bounds !== undefined)
        const showFragmentationBounds =
          hasExactFragmentationBounds &&
          fragmentationBounds.length <= MAX_FRAGMENTATION_REGION_EXTENTS
        const suppressFragmentationBounds = warning.code === 'excess_fragmentation' &&
          !showFragmentationBounds
        return {
          id: warning.id,
          severity: warning.severity,
          title: warning.title,
          presentation: suppressFragmentationBounds
            ? 'list-only'
            : showFragmentationBounds
              ? 'region-extents'
              : 'feature-bounds',
          affectedRegionCount: affectedRegionIds.length,
          boundsMm: warning.code === 'excess_fragmentation'
            ? []
            : warning.affected_bounds.map((bounds) => ({
                xMm: bounds.x_mm,
                yMm: bounds.y_mm,
                widthMm: bounds.width_mm,
                heightMm: bounds.height_mm,
              })),
          boundsPx: showFragmentationBounds
            ? fragmentationBounds.map((bounds) => ({ ...bounds! }))
            : [],
        } satisfies EditorCanvasRiskOverlay
      }),
    }
  } else {
    unavailable.risk = processed
      ? 'No risk report is available for this processed raster.'
      : 'A processed raster is required before risks can be displayed.'
  }

  return { sources, unavailable }
}

export function editorCanvasSourcesComparable(
  first: EditorCanvasViewSource,
  second: EditorCanvasViewSource,
) {
  const a = first.space
  const b = second.space
  const epsilon = 1e-9
  return (
    a.widthPx === b.widthPx &&
    a.heightPx === b.heightPx &&
    Math.abs(a.widthMm - b.widthMm) <= epsilon &&
    Math.abs(a.heightMm - b.heightMm) <= epsilon
  )
}

export function editorCanvasViewMetadata(view: EditorCanvasViewId) {
  return VIEW_METADATA[view]
}

export function editorCanvasPhysicalBoundsToRaster(
  bounds: EditorCanvasRiskOverlay['boundsMm'][number],
  space: EditorCanvasSpace,
) {
  return {
    x: (bounds.xMm / space.widthMm) * space.widthPx,
    y: (bounds.yMm / space.heightMm) * space.heightPx,
    width: (bounds.widthMm / space.widthMm) * space.widthPx,
    height: (bounds.heightMm / space.heightMm) * space.heightPx,
  }
}
