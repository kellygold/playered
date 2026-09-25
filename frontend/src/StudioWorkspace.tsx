import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type {
  ExportProfileSettings,
  JobConfigV1,
  JobResource,
  PreviewJobResult,
  ProjectWorkspace,
  RegionOperation,
  ResolvedPrintabilitySettings,
} from './contracts'
import { normalizeCrop, replaceCrop, type CropRect } from './crop'
import { CropEditor } from './CropEditor'
import {
  cleanupEditorModelFromBackend,
  buildCanvasSelectionOperation,
  CleanupControlsPanel,
  createEditorCanvasSourceCatalog,
  EditorCanvas,
  EditorShell,
  exactRegionAtCanvasPick,
  exactWarningsAtCanvasPick,
  type EditorChangeIntent,
  fetchVerifiedRegionAssignment,
  fetchVerifiedRiskMask,
  firstAffectedRegion,
  ManualOperationsPanel,
  manualOperationLabel,
  latestCanvasSelection,
  LocalEditsPanel,
  localEditOperationLabel,
  OutputReadiness,
  OutputEvidence,
  outputReadiness as createOutputReadiness,
  type OutputActionId,
  previewMatchesDraft,
  PreviewJobStatus,
  PromptedEditsPanel,
  RegionInspector,
  regionAssignmentArtifact,
  riskMaskArtifact,
  regionHighlight,
  type CleanupControlField,
  type CleanupControlsChange,
  type CleanupMode,
  type CanvasSelectionState,
  type EditorCanvasViewId,
  type PreviewCoordinatorState,
  type OutputCoordinatorState,
  type RegionAssignmentMap,
  type RiskMaskMap,
  type ManualOperationSuggestionIntent,
} from './editor'
import { PaletteEditor } from './PaletteEditor'
import { PaletteMetricsPanel } from './PaletteMetricsPanel'
import { MuralWorkspace } from './mural/MuralWorkspace'

export const STUDIO_CANVAS_ID = 'image23mf-editor-canvas'

const CROP_MODES = [
  { id: 'cover', name: 'Cover', description: 'Fill the plate; crop overflow.' },
  { id: 'contain', name: 'Contain', description: 'Keep the crop; allow clear margins.' },
  { id: 'stretch', name: 'Stretch', description: 'Force the crop to the plate shape.' },
  { id: 'extend', name: 'Extend', description: 'Preserve scale on a clear canvas.' },
] as const

function muralExportProfile(config: JobConfigV1): ExportProfileSettings | null {
  const nozzle = config.printer.nozzle_mm
  const layerHeight = config.printer.layer_height_mm
  if ((nozzle !== 0.2 && nozzle !== 0.4) || (layerHeight !== 0.1 && layerHeight !== 0.2)) {
    return null
  }
  return {
    printer_model: 'Bambu Lab P2S',
    nozzle_diameter_mm: nozzle,
    layer_height_mm: layerHeight,
    bed_type: 'Textured PEI Plate',
  }
}

export type StudioNotice = {
  title: string
  message: string
  suggestion?: string
}

type StudioWorkspaceProps = {
  config: JobConfigV1
  dirty: boolean
  persistence: 'saved' | 'saving' | 'error'
  job: JobResource | null
  notice: StudioNotice | null
  preview: PreviewJobResult | null
  previewJobState: PreviewCoordinatorState
  outputState: OutputCoordinatorState
  resolvedPrintability: ResolvedPrintabilitySettings | null
  sourceUrl: string
  submitting: boolean
  editingDisabled: boolean
  workspace: ProjectWorkspace
  historyControls: ReactNode
  onCancelJob: () => void
  onChange: (
    config: JobConfigV1,
    intent: EditorChangeIntent,
    persistence?: 'debounced' | 'immediate',
  ) => void
  onProvisionalChange: (config: JobConfigV1) => boolean
  onCleanupCommit: (config: JobConfigV1, intent: EditorChangeIntent) => void
  onPaletteCommit: (
    config: JobConfigV1,
    action: string,
    colorId: string | null,
    source?: 'automatic' | 'manual',
  ) => void
  onOperationCommit: (operation: RegionOperation, intent: EditorChangeIntent) => void
  onClearNotice: () => void
  onPreview: () => void
  onRetryPreview: () => void
  onGenerateOutput: () => void
  onCancelOutput: () => void
  onRetryOutput: () => void
  onDownloadOutput: () => void
  onReplace: () => void
  onPromptedAccepted: () => void | Promise<void>
}

export function StudioWorkspaceView({
  config,
  dirty,
  persistence,
  job,
  notice,
  preview,
  previewJobState,
  outputState,
  resolvedPrintability,
  sourceUrl,
  submitting,
  editingDisabled,
  workspace,
  historyControls,
  onCancelJob,
  onChange,
  onProvisionalChange,
  onCleanupCommit,
  onPaletteCommit,
  onOperationCommit,
  onClearNotice,
  onPreview,
  onRetryPreview,
  onGenerateOutput,
  onCancelOutput,
  onRetryOutput,
  onDownloadOutput,
  onReplace,
  onPromptedAccepted,
}: StudioWorkspaceProps) {
  const [lockAspect, setLockAspect] = useState(true)
  const [cleanupMode, setCleanupMode] = useState<CleanupMode>('basic')
  const [dirtyCleanupFields, setDirtyCleanupFields] = useState<CleanupControlField[]>([])
  const [canvasView, setCanvasView] = useState<EditorCanvasViewId>('processed')
  const [pendingCanvasSelection, setPendingCanvasSelection] = useState<{
    selection: CanvasSelectionState
    baseStateSha256: string | null
  } | null>(null)
  const [selectedRegionId, setSelectedRegionId] = useState<string | null>(null)
  const [selectedRiskId, setSelectedRiskId] = useState<string | null>(null)
  const [operationPreviewRegionIds, setOperationPreviewRegionIds] = useState<string[]>([])
  const [operationSuggestion, setOperationSuggestion] = useState<ManualOperationSuggestionIntent | null>(null)
  const [assignmentLoad, setAssignmentLoad] = useState<{
    sha256: string
    assignment: RegionAssignmentMap | null
    error: string | null
  } | null>(null)
  const [riskMaskLoad, setRiskMaskLoad] = useState<{
    sha256: string
    mask: RiskMaskMap | null
    error: string | null
  } | null>(null)
  const canvasRatio = useRef(config.canvas.width_mm / config.canvas.height_mm)
  const asset = workspace.source_asset
  const isWorking = job?.state === 'queued' || job?.state === 'running'
  const previewMatchesActiveDraft = previewMatchesDraft(preview, workspace.draft)
  const muralMasterArtifact = preview?.artifacts.find(
    (artifact) => artifact.kind === 'palette-preview-image',
  ) ?? null
  const muralProfile = muralExportProfile(config)
  const cleanupModel = useMemo(
    () =>
      resolvedPrintability
        ? cleanupEditorModelFromBackend(config, resolvedPrintability, config.printer.nozzle_mm)
        : null,
    [config, resolvedPrintability],
  )
  const canvasCatalog = useMemo(
    () =>
      preview?.statistics
        ? createEditorCanvasSourceCatalog({
            artifacts: [
              ...preview.artifacts,
              ...(outputState.geometryResult?.geometry_preview &&
                !['stale', 'canceled', 'superseded'].includes(outputState.phase)
                ? [outputState.geometryResult.geometry_preview] : []),
              ...(outputState.phase === 'succeeded' && outputState.resultFreshness === 'current'
                ? outputState.result?.artifacts.filter(a => a.kind === 'bambu-slicer-preview-image') ?? []
                : []),
            ],
            space: {
              widthPx: preview.region_graph?.width_px ?? preview.statistics.preview.width,
              heightPx: preview.region_graph?.height_px ?? preview.statistics.preview.height,
              widthMm: preview.region_graph?.width_mm ?? preview.statistics.physical.width_mm,
              heightMm: preview.region_graph?.height_mm ?? preview.statistics.physical.height_mm,
            },
            slicerUnavailableReason: outputState.phase === 'succeeded' &&
              outputState.resultFreshness === 'current' &&
              typeof outputState.result?.validation_report?.metadata.sliced_preview_unavailable_reason === 'string'
              ? outputState.result.validation_report.metadata.sliced_preview_unavailable_reason : null,
            riskReport: preview.risk_report,
            regionGraph: preview.region_graph,
          })
        : null,
    [outputState.geometryResult, outputState.phase, outputState.result, outputState.resultFreshness, preview],
  )
  const selectedRegion = useMemo(
    () =>
      preview?.region_graph?.regions.find((region) => region.id === selectedRegionId) ?? null,
    [preview?.region_graph, selectedRegionId],
  )
  const operationPreviewRegions = useMemo(
    () => operationPreviewRegionIds.flatMap((regionId) => {
      const region = preview?.region_graph?.regions.find((candidate) => candidate.id === regionId)
      const highlight = regionHighlight(region ?? null)
      return highlight ? [highlight] : []
    }),
    [operationPreviewRegionIds, preview?.region_graph],
  )
  const currentGraphFingerprint =
    preview?.risk_report?.graph_fingerprint ??
    preview?.island_analysis?.graph_fingerprint ??
    preview?.hole_analysis?.graph_fingerprint ??
    null
  const persistedCanvasSelection = useMemo(() => {
    const statistics = preview?.statistics
    if (!statistics) return null
    return latestCanvasSelection(
      workspace.draft?.operations ?? [],
      statistics.transform,
      statistics.config_sha256,
    )
  }, [preview?.statistics, workspace.draft?.operations])
  const canvasSelection = pendingCanvasSelection
    && pendingCanvasSelection.baseStateSha256 === (workspace.draft?.history.state_sha256 ?? null)
    ? pendingCanvasSelection.selection
    : persistedCanvasSelection
  const selectionRegionBounds = useMemo(() => {
    const ids = new Set(
      canvasSelection?.primitives.flatMap((primitive) =>
        primitive.kind === 'regions' ? primitive.region_ids : [],
      ) ?? [],
    )
    return [...ids].flatMap((regionId) => {
      const region = preview?.region_graph?.regions.find((candidate) => candidate.id === regionId)
      const highlight = regionHighlight(region ?? null)
      return highlight ? [highlight] : []
    })
  }, [canvasSelection, preview?.region_graph])
  const currentHoleAnalysisFingerprint = (() => {
    const artifact = preview?.artifacts.find((candidate) => candidate.kind === 'hole-analysis')
    const fingerprint = artifact?.metadata.analysis_fingerprint
    if (
      typeof fingerprint !== 'string' ||
      !/^[0-9a-f]{64}$/.test(fingerprint) ||
      preview?.hole_analysis?.graph_fingerprint !== currentGraphFingerprint
    ) return null
    return fingerprint
  })()
  const assignmentDescriptor = useMemo(() => {
    if (!preview?.region_graph) return { artifact: null, error: null }
    try {
      return {
        artifact: regionAssignmentArtifact(
          preview.artifacts,
          preview.region_graph,
          preview.risk_report?.graph_fingerprint,
        ),
        error: null,
      }
    } catch (error) {
      return {
        artifact: null,
        error: error instanceof Error ? error.message : 'Invalid region assignment metadata.',
      }
    }
  }, [preview])
  const currentAssignment =
    assignmentDescriptor.artifact &&
    assignmentLoad?.sha256 === assignmentDescriptor.artifact.sha256
      ? assignmentLoad.assignment
      : null
  const assignmentError =
    assignmentDescriptor.error ??
    (assignmentDescriptor.artifact &&
    assignmentLoad?.sha256 === assignmentDescriptor.artifact.sha256
      ? assignmentLoad.error
      : null)
  const canvasSelectionStatus = assignmentError
    ? 'error'
    : !assignmentDescriptor.artifact
      ? 'unavailable'
      : currentAssignment
        ? 'ready'
        : 'loading'
  const sourceSha256 = asset?.sha256
  const editorSequenceSha256 = workspace.draft?.editor_sequence_sha256
  const riskMaskDescriptor = useMemo(() => {
    if (!preview?.region_graph || !preview.risk_report || !assignmentDescriptor.artifact) {
      return { artifact: null, error: null }
    }
    const reportArtifact = preview.artifacts.find((candidate) => candidate.kind === 'risk-report')
    const reportFingerprint = reportArtifact?.metadata.report_fingerprint
    try {
      return {
        artifact: riskMaskArtifact(
          preview.artifacts,
          preview.region_graph,
          preview.risk_report,
          assignmentDescriptor.artifact,
          {
            sourceSha256,
            configFingerprint: preview.statistics?.config_sha256,
            operationsFingerprint: previewMatchesActiveDraft ? editorSequenceSha256 : null,
            riskReportFingerprint: typeof reportFingerprint === 'string' ? reportFingerprint : null,
          },
        ),
        error: null,
      }
    } catch (error) {
      return { artifact: null, error: error instanceof Error ? error.message : 'Invalid risk mask metadata.' }
    }
  }, [assignmentDescriptor.artifact, editorSequenceSha256, preview, previewMatchesActiveDraft, sourceSha256])
  const currentRiskMask = riskMaskDescriptor.artifact && riskMaskLoad?.sha256 === riskMaskDescriptor.artifact.sha256
    ? riskMaskLoad.mask
    : null

  useEffect(() => {
    const artifact = assignmentDescriptor.artifact
    const graph = preview?.region_graph
    if (!artifact || !graph) return
    const controller = new AbortController()
    fetchVerifiedRegionAssignment(artifact, graph, { signal: controller.signal })
      .then((assignment) =>
        setAssignmentLoad({ sha256: artifact.sha256, assignment, error: null }),
      )
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setAssignmentLoad({
          sha256: artifact.sha256,
          assignment: null,
          error: error instanceof Error ? error.message : 'Exact region selection failed to load.',
        })
      })
    return () => controller.abort()
  }, [assignmentDescriptor.artifact, preview?.region_graph])

  useEffect(() => {
    const artifact = riskMaskDescriptor.artifact
    if (!artifact) return
    const controller = new AbortController()
    fetchVerifiedRiskMask(artifact, { signal: controller.signal })
      .then((mask) => setRiskMaskLoad({ sha256: artifact.sha256, mask, error: null }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setRiskMaskLoad({
          sha256: artifact.sha256,
          mask: null,
          error: error instanceof Error ? error.message : 'Exact risk masks failed to load.',
        })
      })
    return () => controller.abort()
  }, [riskMaskDescriptor.artifact])

  if (!asset) return null

  const changeCrop = (
    crop: CropRect,
    label = 'Change crop bounds',
    persistence: 'debounced' | 'immediate' = 'debounced',
  ) => onChange(
    replaceCrop(config, crop),
    { commandType: 'config_change', label },
    persistence,
  )
  const patchCropPercent = (field: 'x' | 'y' | 'width' | 'height', value: number) => {
    if (!Number.isFinite(value)) return
    changeCrop(normalizeCrop({ ...config.crop, [field]: value / 100 }))
  }
  const changeCanvas = (axis: 'width_mm' | 'height_mm', value: number) => {
    if (!Number.isFinite(value) || value < 20 || value > 1000) return
    const canvas = { ...config.canvas, [axis]: value }
    if (lockAspect) {
      if (axis === 'width_mm') canvas.height_mm = value / canvasRatio.current
      else canvas.width_mm = value * canvasRatio.current
    } else {
      canvasRatio.current = canvas.width_mm / canvas.height_mm
    }
    onChange(
      { ...config, canvas },
      { commandType: 'config_change', label: 'Resize canvas' },
    )
  }

  const changeCleanup = (change: CleanupControlsChange) => {
    const overrideFields = new Set(config.cleanup.override_fields ?? [])
    for (const field of change.overrides.backendCleanup.clear) overrideFields.delete(field)
    for (const field of change.overrides.backendCleanup.set) overrideFields.add(field)
    const cleanup = {
      ...config.cleanup,
      min_island_mm2: change.values.minimum_island_area_mm2,
      max_hole_mm2: change.values.maximum_tiny_hole_area_mm2,
      smoothing_radius_mm: change.values.smoothing_radius_mm,
      minimum_line_width_mm: change.values.minimum_line_width_mm,
      minimum_neck_width_mm: change.values.minimum_neck_width_mm,
      minimum_gap_width_mm: change.values.minimum_gap_width_mm,
      long_line_minimum_length_mm: change.values.long_line_minimum_length_mm,
      preserve_long_lines: change.values.preserve_long_lines,
      override_fields: [...overrideFields].sort(),
    }
    const next = { ...config, cleanup }
    if (change.phase === 'commit') {
      const label = change.reason === 'preset'
        ? `Apply ${change.activePreset} cleanup preset`
        : change.reason === 'section_reset'
          ? 'Reset cleanup section'
          : change.changedFields.length === 1
            ? `Set ${change.changedFields[0].replaceAll('_', ' ')}`
            : 'Change cleanup settings'
      onCleanupCommit(next, { commandType: 'config_change', label })
      setDirtyCleanupFields([])
    } else if (onProvisionalChange(next)) {
      setDirtyCleanupFields((current) =>
        [...new Set([...current, ...change.changedFields])].sort() as CleanupControlField[],
      )
    }
  }

  const changeIslandPolicy = (mergePolicy: JobConfigV1['cleanup']['merge_policy']) => {
    const next = {
      ...config,
      cleanup: { ...config.cleanup, merge_policy: mergePolicy },
    }
    onCleanupCommit(next, { commandType: 'config_change', label: 'Change island cleanup policy' })
  }

  const selectRegion = (regionId: string) => {
    setSelectedRegionId(regionId)
    const selectedRisk = preview?.risk_report?.warnings.find(
      (warning) => warning.id === selectedRiskId,
    )
    if (!selectedRisk?.affected_region_ids.includes(regionId)) setSelectedRiskId(null)
  }

  const selectRisk = (riskId: string, preferredRegionId?: string | null) => {
    const warning = preview?.risk_report?.warnings.find((candidate) => candidate.id === riskId)
    if (!warning) return
    setSelectedRiskId(riskId)
    setSelectedRegionId(
      preferredRegionId ?? firstAffectedRegion(warning, preview?.region_graph ?? null),
    )
    if (canvasCatalog?.sources.risk) setCanvasView('risk')
  }

  const previewFreshness = isWorking
    ? 'rendering'
    : dirty || (preview !== null && !previewMatchesActiveDraft)
      ? 'stale'
      : preview
        ? 'current'
      : 'unavailable'
  const outputReadiness = createOutputReadiness(
    submitting || isWorking ? 'rendering' : previewFreshness,
    outputState,
    editingDisabled ? 'Resolve draft recovery before rendering a new preview.' : undefined,
  )
  const runOutputAction = (action: OutputActionId) => {
    if (action === 'render-preview') onPreview()
    if (action === 'generate-output') {
      if (outputState.phase === 'failed') onRetryOutput()
      else onGenerateOutput()
    }
    if (action === 'cancel-output') onCancelOutput()
    if (action === 'download-output') onDownloadOutput()
  }
  const workflowAction = outputReadiness.primaryAction
  const workflowActionLabel = submitting
    ? 'Submitting preview…'
    : isWorking
      ? 'Rendering preview…'
      : workflowAction?.id === 'generate-output'
        ? workflowAction.label
        : workflowAction?.id === 'download-output'
          ? 'Download verified 3MF'
          : workflowAction?.id === 'cancel-output'
            ? workflowAction.label === 'Canceling…'
              ? 'Canceling 3MF…'
              : 'Cancel 3MF generation'
            : workflowAction?.label ?? 'Render preview'
  const workflowActionDisabled = submitting || isWorking || !workflowAction || Boolean(workflowAction.disabled)
  const manualAuthoringDisabled = submitting || isWorking || !previewMatchesActiveDraft
  const manualAuthoringDisabledReason = isWorking || submitting
    ? 'Wait for the exact preview to finish before authoring another operation.'
    : !previewMatchesActiveDraft
      ? 'Update preview before another operation. Region identities are bound to the last exact render.'
      : undefined
  const canvasSelectionDisabled = manualAuthoringDisabled
    || !currentGraphFingerprint
    || !preview?.statistics?.config_sha256
  const commitCanvasSelection = (next: CanvasSelectionState, label: string) => {
    const configFingerprint = preview?.statistics?.config_sha256
    if (canvasSelectionDisabled || !currentGraphFingerprint || !configFingerprint) return
    setPendingCanvasSelection({
      selection: next,
      baseStateSha256: workspace.draft?.history.state_sha256 ?? null,
    })
    onOperationCommit(
      buildCanvasSelectionOperation(next, {
        graphFingerprint: currentGraphFingerprint,
        configFingerprint,
      }),
      { commandType: 'manual_operation', label },
    )
  }
  const projectStatus = isWorking
    ? 'Rendering preview'
    : persistence === 'error'
      ? 'Draft needs attention'
      : persistence === 'saving'
        ? 'Saving draft'
        : dirty
          ? 'Changes not rendered'
          : preview && !previewMatchesActiveDraft
            ? 'Saved preview is out of date'
          : 'Draft saved'
  const projectStatusTone = persistence === 'error'
    ? 'danger'
    : dirty || (preview !== null && !previewMatchesActiveDraft)
      ? 'warning'
      : 'positive'

  const center = (
    <div className="studio-main editor-center-stack" data-has-preview={!!preview}>
      <details className="studio-disclosure studio-source" open={!preview ? true : undefined}>
        <summary>Source crop & print colors</summary>
        <div className="studio-disclosure__body">
          <CropEditor
            crop={config.crop}
            filename={asset.original_filename}
            imageUrl={sourceUrl}
            imageWidth={asset.width_px}
            imageHeight={asset.height_px}
            onChange={(crop, phase, label) => {
              if (phase === 'provisional') onProvisionalChange(replaceCrop(config, crop))
              else changeCrop(crop, label, 'immediate')
            }}
          />

          <PaletteEditor
            config={config}
            persistence={persistence}
            projectId={workspace.project.id}
            onCommit={onPaletteCommit}
          />

        </div>
      </details>

      <section className="preview-card" aria-labelledby="preview-heading">
        <div className="preview-card-heading">
          <div>
            <span className="eyebrow">Processed output</span>
            <h2 id="preview-heading">Palette preview</h2>
          </div>
          <span className={`preview-freshness ${previewFreshness === 'stale' ? 'is-dirty' : ''}`}>
            {previewFreshness === 'stale' ? 'Changes not rendered' : preview ? 'Current' : 'Waiting'}
          </span>
        </div>
        <div className="preview-output editor-canvas-output">
          {canvasCatalog ? (
            <EditorCanvas
              catalog={canvasCatalog}
              ariaLabel="Exact artwork preview and comparison"
              view={canvasView}
              onViewChange={setCanvasView}
              selectedRegion={regionHighlight(selectedRegion)}
              operationPreviewRegions={operationPreviewRegions}
              canonicalTransform={preview?.statistics?.transform}
              canvasSelection={canvasSelection}
              selectionRegionBounds={selectionRegionBounds}
              selectedRegionId={selectedRegionId}
              selectionDisabled={canvasSelectionDisabled}
              onCanvasSelectionChange={commitCanvasSelection}
              selectedRiskId={selectedRiskId}
              onRiskSelect={(riskId) => selectRisk(riskId)}
              regionAssignment={currentAssignment}
              riskMask={currentRiskMask}
              riskGraph={preview?.region_graph}
              riskReport={preview?.risk_report}
              onPick={(pick) => {
                if (!preview?.region_graph || !currentAssignment) return
                const region = exactRegionAtCanvasPick(
                  currentAssignment,
                  preview.region_graph,
                  pick,
                )
                setSelectedRegionId(region?.id ?? null)
                if (canvasView === 'risk' && preview.risk_report) {
                  const warning = exactWarningsAtCanvasPick(
                    currentAssignment,
                    preview.region_graph,
                    preview.risk_report,
                    pick,
                  )[0]
                  setSelectedRiskId(warning?.id ?? null)
                } else {
                  setSelectedRiskId(null)
                }
              }}
            />
          ) : (
            <div className="preview-empty">
              <span aria-hidden="true">◫</span>
              <p>Your processed crop will appear here.</p>
            </div>
          )}
        </div>
        {preview?.statistics ? (
          <dl className="preview-stats">
            <div>
              <dt>Preview</dt>
              <dd>
                {preview.statistics.preview.width} × {preview.statistics.preview.height} px
              </dd>
            </div>
            <div>
              <dt>Pixel scale</dt>
              <dd>{preview.statistics.physical.mm_per_pixel_x.toFixed(3)} mm</dd>
            </div>
            <div>
              <dt>Transparent</dt>
              <dd>{preview.statistics.alpha.transparent_pixels.toLocaleString()} px</dd>
            </div>
          </dl>
        ) : null}
      </section>
    </div>
  )

  const cleanupRail = cleanupModel ? (
    <CleanupControlsPanel
      values={cleanupModel.values}
      profile={cleanupModel.profile}
      mode={cleanupMode}
      previewFreshness={previewFreshness}
      pixelScale={
        preview?.statistics
          ? {
              mmPerPixelX: preview.statistics.physical.mm_per_pixel_x,
              mmPerPixelY: preview.statistics.physical.mm_per_pixel_y,
            }
          : null
      }
      overriddenFields={cleanupModel.overriddenFields}
      dirtyFields={dirtyCleanupFields}
      disabled={submitting}
      onModeChange={setCleanupMode}
      mergePolicy={config.cleanup.merge_policy}
      onMergePolicyChange={changeIslandPolicy}
      onChange={changeCleanup}
    />
  ) : (
    <div className="editor-rail-loading" role="status">
      <span className="upload-spinner" aria-hidden="true" />
      <strong>Loading printability profile…</strong>
      <p>Matching physical cleanup controls to this printer and nozzle.</p>
    </div>
  )

  const previewStatus = (
    <PreviewJobStatus
      state={previewJobState}
      onCancel={onCancelJob}
      onRetry={onRetryPreview}
      disabled={submitting}
    />
  )

  const inspectionRail = (
    <div className="editor-inspection-stack">
      {previewJobState.phase === 'succeeded' ? (
        <details className="studio-disclosure">
          <summary>Preview details</summary>
          <div className="studio-disclosure__body">{previewStatus}</div>
        </details>
      ) : null}
      <details className="studio-disclosure">
        <summary>Inspect regions & repair details</summary>
        <div className="studio-disclosure__body">
          <RegionInspector
            graph={preview?.region_graph ?? null}
            report={preview?.risk_report ?? null}
            selectedRegionId={selectedRegionId}
            selectedRiskId={selectedRiskId}
            onRegionSelect={selectRegion}
            onRiskSelect={selectRisk}
            onSuggestionSelect={({ warning, suggestion }) => {
              setOperationSuggestion((current) => ({
                key: (current?.key ?? 0) + 1,
                kind: suggestion.kind,
                featureKey: warning.feature_key,
              }))
            }}
            canvasSelectionStatus={canvasSelectionStatus}
            canvasSelectionMessage={assignmentError ?? undefined}
            disabled={submitting}
          />
          <ManualOperationsPanel
            graph={preview?.region_graph ?? null}
            graphFingerprint={currentGraphFingerprint}
            configFingerprint={preview?.statistics?.config_sha256 ?? null}
            palette={config.palette.colors}
            holeAnalysis={preview?.hole_analysis ?? null}
            holeAnalysisFingerprint={currentHoleAnalysisFingerprint}
            selectedRegionId={selectedRegionId}
            suggestionIntent={operationSuggestion}
            disabled={manualAuthoringDisabled}
            disabledReason={manualAuthoringDisabledReason}
            persistence={persistence}
            onRegionSelect={selectRegion}
            onAffectedRegionsChange={setOperationPreviewRegionIds}
            onCommit={(operation) => onOperationCommit(operation, {
              commandType: 'manual_operation',
              label: manualOperationLabel(operation),
            })}
          />
          <LocalEditsPanel
            graph={preview?.region_graph ?? null}
            graphFingerprint={currentGraphFingerprint}
            configFingerprint={preview?.statistics?.config_sha256 ?? null}
            parentRevisionId={workspace.draft?.base_revision_id ?? null}
            selection={canvasSelection}
            disabled={manualAuthoringDisabled || persistence !== 'saved'}
            disabledReason={
              persistence !== 'saved'
                ? 'Wait for the saved selection to finish before applying it.'
                : manualAuthoringDisabledReason
            }
            persistence={persistence}
            onCommit={(operation) => onOperationCommit(operation, {
              commandType: 'manual_operation',
              label: localEditOperationLabel(operation),
            })}
          />
          {asset ? (
            <PromptedEditsPanel
              projectId={workspace.project.id}
              parentRevisionId={workspace.draft?.base_revision_id ?? null}
              previewJobId={preview?.job.id ?? null}
              draftGeneration={workspace.draft?.generation ?? 0}
              sourceUrl={sourceUrl}
              sourceSha256={asset.sha256}
              sourceWidthPx={asset.width_px}
              sourceHeightPx={asset.height_px}
              canvasWidthMm={config.canvas.width_mm}
              canvasHeightMm={config.canvas.height_mm}
              selection={canvasSelection}
              disabled={manualAuthoringDisabled || persistence !== 'saved'}
              onAccepted={onPromptedAccepted}
            />
          ) : null}
        </div>
      </details>
      <details className="studio-disclosure">
        <summary>Crop & fit</summary>
        <div className="studio-disclosure__body">
          <section className="control-section">
            <div className="control-heading">
              <span>Crop bounds</span>
              <button
                type="button"
                onClick={() => changeCrop(
                  { ...config.crop, x: 0, y: 0, width: 1, height: 1 },
                  'Reset crop',
                  'immediate',
                )}
              >
                Reset
              </button>
            </div>
            <div className="number-grid">
              {(
                [
                  ['x', 'Left'],
                  ['y', 'Top'],
                  ['width', 'Width'],
                  ['height', 'Height'],
                ] as const
              ).map(([field, label]) => (
                <label key={field}>
                  <span>{label}</span>
                  <div className="number-input">
                    <input
                      aria-label={`${label} crop percent`}
                      type="number"
                      min="0"
                      max="100"
                      step="0.5"
                      value={Number((config.crop[field] * 100).toFixed(1))}
                      onChange={(event) => patchCropPercent(field, Number(event.target.value))}
                    />
                    <span>%</span>
                  </div>
                </label>
              ))}
            </div>
          </section>

          <section className="control-section">
            <div className="control-heading">
              <span>Fit mode</span>
            </div>
            <div className="mode-list">
              {CROP_MODES.map((mode) => (
                <button
                  type="button"
                  className={config.crop.mode === mode.id ? 'is-selected' : ''}
                  aria-label={`${mode.name}: ${mode.description}`}
                  aria-pressed={config.crop.mode === mode.id}
                  key={mode.id}
                  onClick={() => changeCrop(
                    { ...config.crop, mode: mode.id },
                    `Change fit mode to ${mode.name}`,
                    'immediate',
                  )}
                >
                  <i aria-hidden="true" />
                  <span>
                    <strong>{mode.name}</strong>
                    <small>{mode.description}</small>
                  </span>
                </button>
              ))}
            </div>
          </section>

        </div>
      </details>
      <section className="control-section">
        <div className="control-heading">
          <span>Physical canvas</span>
          <label className="lock-toggle">
            <input
              type="checkbox"
              checked={lockAspect}
              onChange={(event) => {
                setLockAspect(event.target.checked)
                canvasRatio.current = config.canvas.width_mm / config.canvas.height_mm
              }}
            />
            Lock ratio
          </label>
        </div>
        <div className="number-grid canvas-grid">
          <label>
            <span>Width</span>
            <div className="number-input">
              <input
                aria-label="Canvas width millimetres"
                type="number"
                min="20"
                max="1000"
                step="1"
                value={Number(config.canvas.width_mm.toFixed(2))}
                onChange={(event) => changeCanvas('width_mm', Number(event.target.value))}
              />
              <span>mm</span>
            </div>
          </label>
          <label>
            <span>Height</span>
            <div className="number-input">
              <input
                aria-label="Canvas height millimetres"
                type="number"
                min="20"
                max="1000"
                step="1"
                value={Number(config.canvas.height_mm.toFixed(2))}
                onChange={(event) => changeCanvas('height_mm', Number(event.target.value))}
              />
              <span>mm</span>
            </div>
          </label>
        </div>
        <div className="profile-summary">
          <span>{config.printer.printer_id.replace('bambu-', 'Bambu ').toUpperCase()}</span>
          <span>{config.printer.nozzle_mm} mm nozzle</span>
          <span>{config.printer.layer_height_mm} mm layers</span>
        </div>
      </section>

      {muralMasterArtifact && muralProfile ? (
        <details className="studio-disclosure">
          <summary>Multi-plate mural</summary>
          <div className="studio-disclosure__body">
            <MuralWorkspace
              key={`${workspace.project.id}:${muralMasterArtifact.id}`}
              projectId={workspace.project.id}
              processedArtifactId={muralMasterArtifact.id}
              masterImageUrl={muralMasterArtifact.download_url}
              previewJobId={preview?.job.id ?? null}
              expectedDraftGeneration={workspace.draft?.generation ?? 0}
              previewCurrent={previewMatchesActiveDraft}
              profile={muralProfile}
              disabled={submitting || editingDisabled}
              disabledReason={editingDisabled
                ? 'Resolve draft recovery before building the mural.'
                : submitting
                  ? 'Wait for the current preview action to finish.'
                  : undefined}
            />
          </div>
        </details>
      ) : null}

      <details className="studio-disclosure">
        <summary>Color statistics</summary>
        <div className="studio-disclosure__body">
          <PaletteMetricsPanel
            colors={config.palette.colors}
            loading={isWorking}
            metrics={preview?.palette_metrics ?? null}
            stale={dirty || isWorking}
          />
        </div>
      </details>
    </div>
  )

  return (
    <EditorShell
      className="studio-editor-shell"
      canvasId={STUDIO_CANVAS_ID}
      showSkipLink={false}
      workspaceDisabled={editingDisabled}
      project={{
        name: workspace.project.name,
        assetName: asset.original_filename,
        detail: `${asset.width_px} × ${asset.height_px} px · ${config.canvas.width_mm} × ${config.canvas.height_mm} mm`,
        status: projectStatus,
        statusTone: projectStatusTone,
      }}
      leftRail={cleanupRail}
      leftRailLabel="Cleanup"
      canvas={center}
      canvasLabel="Image preparation canvas"
      rightRail={inspectionRail}
      rightRailLabel="Setup & tools"
      headerActions={
        <>
          {historyControls}
          <button className="i23-editor-demo-button" type="button" onClick={onReplace}>
            Replace image
          </button>
          {isWorking ? (
            <button className="button button-danger editor-header-button" type="button" onClick={onCancelJob}>
              Cancel
            </button>
          ) : null}
          <button
            className="i23-editor-demo-button"
            data-primary="true"
            type="button"
            disabled={workflowActionDisabled}
            aria-label={`Workflow: ${workflowActionLabel}`}
            aria-describedby={workflowAction?.disabledReason ? 'header-workflow-action-reason' : undefined}
            onClick={() => {
              if (workflowAction && !workflowAction.disabled) runOutputAction(workflowAction.id)
            }}
          >
            {workflowActionLabel}
          </button>
          {workflowAction?.disabledReason ? (
            <span id="header-workflow-action-reason" className="visually-hidden">
              {workflowAction.disabledReason}
            </span>
          ) : null}
        </>
      }
      canvasToolbar={
        <div className="editor-output-toolbar">
          <OutputReadiness
            model={outputReadiness}
            onAction={runOutputAction}
            showAction={false}
            activeJob={outputState.activeStep === 'geometry' ? outputState.geometryJob
              : outputState.activeStep === 'export' ? outputState.exportJob : null}
          />
          {previewJobState.phase !== 'idle' && previewJobState.phase !== 'succeeded' ? previewStatus : null}
          <OutputEvidence
            projectId={workspace.project.id}
            result={outputState.result}
            current={outputState.phase === 'succeeded' && outputState.resultFreshness === 'current'}
          />
          <div className="editor-canvas-status" aria-label="Current print setup">
            <span>{previewFreshness === 'current' ? 'Preview current' : 'Working draft'}</span>
            <span>{config.palette.colors.length} colors</span>
            <span>{config.printer.nozzle_mm} mm nozzle</span>
          </div>
        </div>
      }
      canvasFooter={
        <div className="editor-canvas-footer">
          <span>{config.canvas.width_mm} × {config.canvas.height_mm} mm</span>
          <span>{config.printer.layer_height_mm} mm layers</span>
          <span>{preview?.statistics ? `${preview.statistics.preview.width} px preview` : 'Preview pending'}</span>
        </div>
      }
      viewState={{ kind: 'ready' }}
      notificationSlot={
        notice ? (
          <div className="notice notice-error editor-notice" role="alert">
            <div>
              <strong>{notice.title}</strong>
              <p>{notice.message}</p>
              {notice.suggestion ? <small>{notice.suggestion}</small> : null}
            </div>
            <button type="button" aria-label="Dismiss error" onClick={onClearNotice}>
              ×
            </button>
          </div>
        ) : null
      }
    />
  )
}
