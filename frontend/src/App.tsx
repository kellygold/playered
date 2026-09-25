import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from 'react'
import { createPortal } from 'react-dom'
import {
  ApiRequestError,
  branchRevision,
  fetchHealth,
  fetchStartupRecovery,
  fetchPreviewResult,
  fetchProject,
  importProjectWithProgress,
  moveDraftHistory,
  planGarbageCollection,
  publishRevision,
  reconcileWorkspace,
  resolvePrintabilityProfile,
  saveDraft,
  type Health,
} from './api'
import type {
  JobConfigV1,
  EditorHistoryCommand,
  EditorHistorySummary,
  ProjectWorkspace,
  RegionOperation,
  ResolvedPrintabilitySettings,
  RevisionResource,
} from './contracts'
import {
  ConfigChangeGuard,
  HistoryControls,
  historyRequestId,
  incompatibleManualOperationCount,
  previewMatchesDraft,
  RevisionLifecycle,
  paletteActionLabel,
  type EditorChangeIntent,
  useEditorHistory,
  useOutputJobCoordinator,
  usePreviewJobCoordinator,
  withoutIncompatibleManualOperations,
} from './editor'
import { ImportPanel } from './ImportPanel'
import {
  FirstRunGuide,
  firstRunStage,
  importGuidedSample,
} from './onboarding'
import { ProjectBrowser } from './ProjectBrowser'
import { CalibrationWorkspace } from './calibration/CalibrationWorkspace'
import {
  WorkspaceRecoveryNotice,
  WorkspaceRecoveryPanel,
  type StartupReconciliation,
} from './recovery'
import {
  STUDIO_CANVAS_ID,
  StudioWorkspaceView,
  type StudioNotice,
} from './StudioWorkspace'

type EngineStatus = 'loading' | 'ready' | 'offline'
type UploadState = { filename: string; progress: number; replacing: boolean }
type DraftSaveSnapshot = {
  projectId: string
  config: JobConfigV1
  operations: RegionOperation[]
  editVersion: number
  historyIntent: EditorChangeIntent | null
  requestId: string | null
  historyCommand?: EditorHistoryCommand
}
type ConfigChangePersistence = 'provisional' | 'debounced' | 'immediate'
type EditorMutationGate = 'idle' | 'provisional' | 'history' | 'conflict' | 'resync'
type DraftRecovery = 'retry' | 'resync' | 'correct' | null
type PendingConfigChange = {
  projectId: string
  config: JobConfigV1
  operations: RegionOperation[]
  incompatibleOperationCount: number
  persistence: ConfigChangePersistence
  historyIntent: EditorChangeIntent
}

const MAX_CLIENT_IMAGE_BYTES = 64 * 1024 * 1024
const RECENT_PROJECT_KEY = 'image23mf.recentProjectId'
const MODAL_FOCUSABLE = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled]):not([tabindex="-1"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')
const EMPTY_HISTORY: EditorHistorySummary = {
  lineage_id: '',
  cursor_node_id: '',
  tip_node_id: '',
  cursor: 0,
  total: 0,
  limit: 100,
  can_undo: false,
  can_redo: false,
  undo_label: null,
  redo_label: null,
  state_sha256: '0'.repeat(64),
}

function noticeFor(error: unknown): StudioNotice {
  if (error instanceof ApiRequestError) {
    const code = error.problem?.error.code
    const details = error.problem?.error.details ?? {}
    const titles: Record<string, string> = {
      unsupported_media: 'That file is not a supported image',
      resource_limit: 'That image is too large to process safely',
      validation_error: 'The image or settings need attention',
      stale_draft: 'Your project changed in another request',
      incompatible_editor_history: 'Those settings cannot reuse the current manual edits',
      blob_unavailable: 'The saved preview could not be read',
    }
    return {
      title: titles[code ?? ''] ?? 'The local engine could not complete that request',
      message: error.problem?.error.message ?? error.message,
      suggestion:
        code === 'incompatible_editor_history'
          ? 'Keep the current settings, or explicitly clear the bound manual edits before changing setup.'
          : typeof details.suggestion === 'string'
            ? details.suggestion
            : undefined,
    }
  }
  if (error instanceof Error) {
    return {
      title: 'The local engine could not be reached',
      message: error.message,
      suggestion: 'Check that `make api` is running, then try again.',
    }
  }
  return { title: 'Something unexpected happened', message: 'Try the action again.' }
}

function draftRecoveryFor(error: unknown): DraftRecovery {
  if (!(error instanceof ApiRequestError)) return 'retry'
  if (error.status === 409 || error.problem?.error.code === 'stale_draft') return 'resync'
  if (error.problem?.error.retryable || error.status >= 500) return 'retry'
  return 'correct'
}

function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [engineStatus, setEngineStatus] = useState<EngineStatus>('loading')
  const [workspace, setWorkspace] = useState<ProjectWorkspace | null>(null)
  const [config, setConfig] = useState<JobConfigV1 | null>(null)
  const [sourceUrl, setSourceUrl] = useState('')
  const [dirty, setDirty] = useState(false)
  const [, setOperations] = useState<RegionOperation[]>([])
  const [resolvedPrintabilityState, setResolvedPrintabilityState] = useState<{
    key: string
    value: ResolvedPrintabilitySettings
  } | null>(null)
  const [draftPersistence, setDraftPersistence] = useState<'saved' | 'saving' | 'error'>('saved')
  const [draftRecovery, setDraftRecovery] = useState<DraftRecovery>(null)
  const [mutationGate, setMutationGateState] = useState<EditorMutationGate>('idle')
  const [upload, setUpload] = useState<UploadState | null>(null)
  const [notice, setNotice] = useState<StudioNotice | null>(null)
  const [replacement, setReplacement] = useState<File | null>(null)
  const [pendingConfigChange, setPendingConfigChange] =
    useState<PendingConfigChange | null>(null)
  const [dragActive, setDragActive] = useState(false)
  const [projectBrowserOpen, setProjectBrowserOpen] = useState(false)
  const [calibrationOpen, setCalibrationOpen] = useState(false)
  const [firstRunOpen, setFirstRunOpen] = useState(false)
  const [guidedSampleActive, setGuidedSampleActive] = useState(false)
  const [samplePending, setSamplePending] = useState(false)
  const [startupRecovery, setStartupRecovery] = useState<StartupReconciliation | null>(null)
  const [recoveryOpen, setRecoveryOpen] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const modalDialogRef = useRef<HTMLElement>(null)
  const modalInitialFocusRef = useRef<HTMLButtonElement>(null)
  const modalOpenerRef = useRef<HTMLElement | null>(null)
  const modalCloseRef = useRef<(() => void) | null>(null)
  const uploadControllerRef = useRef<AbortController | null>(null)
  const restoreControllerRef = useRef<AbortController | null>(null)
  const {
    state: previewState,
    start: startPreviewJob,
    cancel: cancelPreviewJob,
    retry: retryPreviewJob,
    markStale: markPreviewStale,
    restore: restorePreviewJob,
  } = usePreviewJobCoordinator()
  const {
    state: outputState,
    start: startOutputJob,
    cancel: cancelOutputJob,
    retry: retryOutputJob,
    markStale: markOutputStale,
    reset: resetOutputJob,
  } = useOutputJobCoordinator()
  const editVersionRef = useRef(0)
  const submittedEditVersionRef = useRef(0)
  const acceptedPreviewJobRef = useRef<string | null>(null)
  const activeProjectIdRef = useRef<string | null>(null)
  const draftGenerationsRef = useRef(new Map<string, number>())
  const draftHistoryRef = useRef(new Map<string, EditorHistorySummary>())
  const operationsRef = useRef<RegionOperation[]>([])
  const pendingSavesRef = useRef(0)
  const saveChainRef = useRef<Promise<void>>(Promise.resolve())
  const draftSaveTimerRef = useRef<number | null>(null)
  const scheduledDraftRef = useRef<DraftSaveSnapshot | null>(null)
  const failedDraftRef = useRef<DraftSaveSnapshot | null>(null)
  const draftFailureNoticeRef = useRef(false)
  const draftRecoveryRef = useRef<DraftRecovery>(null)
  const mutationGateRef = useRef<EditorMutationGate>('idle')
  const flushDraftSaveRef = useRef<() => void>(() => undefined)
  const dragDepthRef = useRef(0)
  const printabilityCatalogFingerprint =
    config?.cleanup.printability_profile_catalog_fingerprint ?? null
  const printabilityProfileKey = config
    ? `${config.printer.printer_id}:${config.printer.nozzle_id}:${printabilityCatalogFingerprint ?? 'active'}`
    : ''
  const printabilityPrinterId = config?.printer.printer_id ?? ''
  const printabilityNozzleId = config?.printer.nozzle_id ?? ''
  const resolvedPrintability =
    resolvedPrintabilityState?.key === printabilityProfileKey
      ? resolvedPrintabilityState.value
      : null
  const modalKind = upload
    ? 'upload'
    : replacement
      ? 'replacement'
      : recoveryOpen
        ? 'recovery'
        : null
  const modalOpen = modalKind !== null

  const setMutationGate = useCallback((next: EditorMutationGate) => {
    mutationGateRef.current = next
    setMutationGateState(next)
  }, [])

  const setDraftRecoveryState = useCallback((next: DraftRecovery) => {
    draftRecoveryRef.current = next
    setDraftRecovery(next)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetchHealth(controller.signal)
      .then((value) => {
        setHealth(value)
        setEngineStatus('ready')
        void (async () => {
          try {
            setStartupRecovery(await fetchStartupRecovery(controller.signal))
          } catch {
            // Recovery evidence is additive; a missing/older endpoint must not mark the engine offline.
          }
        })()
      })
      .catch((error: unknown) => {
        if ((error as Error).name !== 'AbortError') setEngineStatus('offline')
      })
    return () => controller.abort()
  }, [])

  useEffect(
    () => () => {
      uploadControllerRef.current?.abort()
      restoreControllerRef.current?.abort()
      if (draftSaveTimerRef.current !== null) window.clearTimeout(draftSaveTimerRef.current)
    },
    [],
  )

  useEffect(() => {
    const flush = () => flushDraftSaveRef.current()
    const flushWhenHidden = () => {
      if (document.visibilityState === 'hidden') flushDraftSaveRef.current()
    }
    window.addEventListener('pagehide', flush)
    document.addEventListener('visibilitychange', flushWhenHidden)
    return () => {
      window.removeEventListener('pagehide', flush)
      document.removeEventListener('visibilitychange', flushWhenHidden)
    }
  }, [])

  useEffect(() => {
    modalCloseRef.current = modalKind === 'upload'
      ? () => uploadControllerRef.current?.abort()
      : modalKind === 'replacement'
        ? () => setReplacement(null)
        : modalKind === 'recovery'
          ? () => setRecoveryOpen(false)
        : null
    return () => {
      modalCloseRef.current = null
    }
  }, [modalKind])

  useEffect(() => {
    if (!modalOpen) return
    const app = document.querySelector<HTMLElement>('.app-shell')
    const ownedInert = app !== null && !app.hasAttribute('inert')
    const previousOverflow = document.body.style.overflow
    const opener = modalOpenerRef.current
    if (ownedInert) app.setAttribute('inert', '')
    document.body.style.overflow = 'hidden'

    const keydown = (event: KeyboardEvent) => {
      const dialog = modalDialogRef.current
      if (!dialog) return
      if (event.key === 'Escape') {
        const close = modalCloseRef.current
        if (!close) return
        event.preventDefault()
        event.stopPropagation()
        close()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(MODAL_FOCUSABLE))
      if (!focusable.length) {
        event.preventDefault()
        dialog.focus()
        return
      }
      const first = focusable[0]
      const last = focusable.at(-1) ?? first
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', keydown, true)
    return () => {
      document.removeEventListener('keydown', keydown, true)
      if (ownedInert) app?.removeAttribute('inert')
      document.body.style.overflow = previousOverflow
      queueMicrotask(() => {
        if (opener?.isConnected && !opener.closest('[inert]')) opener.focus()
        else {
          document.querySelector<HTMLElement>('.i23-editor-canvas')?.focus()
          if (!(document.activeElement instanceof HTMLElement) || document.activeElement === document.body) {
            document.querySelector<HTMLElement>('.brand')?.focus()
          }
        }
        if (modalOpenerRef.current === opener) modalOpenerRef.current = null
      })
    }
  }, [modalOpen])

  useEffect(() => {
    if (!modalKind) return
    queueMicrotask(() => {
      if (modalInitialFocusRef.current) {
        modalInitialFocusRef.current.focus()
      } else {
        modalDialogRef.current?.focus()
      }
    })
  }, [modalKind])

  useEffect(() => {
    const projectId = window.localStorage.getItem(RECENT_PROJECT_KEY)
    if (!projectId) return
    const controller = new AbortController()
    restoreControllerRef.current = controller
    fetchProject(projectId, controller.signal)
      .then(async (restored) => {
        if (controller.signal.aborted) return
        if (!restored.draft || !restored.source_asset) return
        activeProjectIdRef.current = restored.project.id
        resetOutputJob(restored.project.id)
        draftGenerationsRef.current.set(restored.project.id, restored.draft.generation)
        draftHistoryRef.current.set(restored.project.id, restored.draft.history)
        operationsRef.current = restored.draft.operations
        setOperations(restored.draft.operations)
        setWorkspace(restored)
        setConfig(restored.draft.config)
        setSourceUrl(`/api/assets/${encodeURIComponent(restored.source_asset.id)}`)
        const retryRequest = {
          projectId: restored.project.id,
          config: restored.draft.config,
          expectedDraftGeneration: restored.draft.generation,
        }
        if (restored.latest_preview_job?.state === 'succeeded') {
          const result = await fetchPreviewResult(restored.latest_preview_job.id, controller.signal)
          if (controller.signal.aborted) return
          const recoverablePreviewCacheComplete = result.artifacts.some(
            (artifact) => artifact.kind === 'palette-preview-image',
          )
          if (!recoverablePreviewCacheComplete) setDirty(true)
          restorePreviewJob(
            restored.latest_preview_job,
            result,
            recoverablePreviewCacheComplete && previewMatchesDraft(result, restored.draft),
            retryRequest,
          )
        } else if (
          restored.latest_preview_job &&
          ['queued', 'running'].includes(restored.latest_preview_job.state)
        ) {
          void restorePreviewJob(restored.latest_preview_job, null, false, retryRequest)
        }
      })
      .catch((error: unknown) => {
        if ((error as Error).name === 'AbortError') return
        window.localStorage.removeItem(RECENT_PROJECT_KEY)
        setNotice(noticeFor(error))
      })
    return () => {
      controller.abort()
      if (restoreControllerRef.current === controller) restoreControllerRef.current = null
    }
  }, [resetOutputJob, restorePreviewJob])

  useEffect(() => {
    if (!printabilityProfileKey) return
    const controller = new AbortController()
    resolvePrintabilityProfile(
      {
        printer_id: printabilityPrinterId,
        nozzle_id: printabilityNozzleId,
        material_class: 'pla',
        catalog_fingerprint: printabilityCatalogFingerprint,
      },
      controller.signal,
    )
      .then((value) => setResolvedPrintabilityState({ key: printabilityProfileKey, value }))
      .catch((error: unknown) => {
        if ((error as Error).name !== 'AbortError') setNotice(noticeFor(error))
      })
    return () => controller.abort()
  }, [
    printabilityCatalogFingerprint,
    printabilityNozzleId,
    printabilityPrinterId,
    printabilityProfileKey,
  ])

  const submitPreview = useCallback(
    async (baseWorkspace: ProjectWorkspace, nextConfig: JobConfigV1) => {
      const draft = baseWorkspace.draft
      if (!draft) return
      flushDraftSaveRef.current()
      await saveChainRef.current
      submittedEditVersionRef.current = editVersionRef.current
      setNotice(null)
      markOutputStale()
      await startPreviewJob({
        projectId: baseWorkspace.project.id,
        config: nextConfig,
        expectedDraftGeneration:
          draftGenerationsRef.current.get(baseWorkspace.project.id) ?? draft.generation,
      })
    },
    [markOutputStale, startPreviewJob],
  )

  useEffect(() => {
    const started = previewState.acceptedStart
    if (
      !started ||
      acceptedPreviewJobRef.current === started.job.id ||
      workspace?.project.id !== started.job.project_id
    )
      return
    acceptedPreviewJobRef.current = started.job.id
    draftGenerationsRef.current.set(started.job.project_id, started.draft.generation)
    draftHistoryRef.current.set(started.job.project_id, started.draft.history)
    setWorkspace((current) =>
      current
        ? { ...current, draft: started.draft, latest_preview_job: started.job }
        : current,
    )
    if (editVersionRef.current === submittedEditVersionRef.current) {
      setConfig(started.draft.config)
      setDirty(false)
    }
  }, [previewState.acceptedStart, workspace?.project.id])

  const uploadFile = useCallback(
    async (file: File, projectName?: string) => {
      if (file.size > MAX_CLIENT_IMAGE_BYTES) {
        setNotice({
          title: 'That image is larger than 64 MB',
          message: `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB.`,
          suggestion: 'Export a smaller PNG, JPEG, or WebP and try again.',
        })
        return false
      }
      uploadControllerRef.current?.abort()
      restoreControllerRef.current?.abort()
      const controller = new AbortController()
      uploadControllerRef.current = controller
      setReplacement(null)
      setPendingConfigChange(null)
      setUpload({ filename: file.name || 'Pasted image', progress: 0, replacing: !!workspace })
      setNotice(null)
      try {
        const imported = await importProjectWithProgress(
          file,
          file.name || 'pasted-image.png',
          projectName,
          (progress) =>
            setUpload((current) => (current ? { ...current, progress } : current)),
          controller.signal,
        )
        editVersionRef.current = 0
        activeProjectIdRef.current = imported.project.id
        resetOutputJob(imported.project.id)
        if (imported.draft) {
          draftGenerationsRef.current.set(imported.project.id, imported.draft.generation)
          draftHistoryRef.current.set(imported.project.id, imported.draft.history)
        }
        operationsRef.current = imported.draft?.operations ?? []
        setOperations(imported.draft?.operations ?? [])
        setMutationGate('idle')
        setDraftRecoveryState(null)
        setDraftPersistence('saved')
        window.localStorage.setItem(RECENT_PROJECT_KEY, imported.project.id)
        setSourceUrl(
          imported.source_asset
            ? `/api/assets/${encodeURIComponent(imported.source_asset.id)}`
            : '',
        )
        setWorkspace(imported)
        setConfig(imported.draft?.config ?? null)
        setDirty(false)
        setUpload(null)
        if (imported.draft) void submitPreview(imported, imported.draft.config)
        return true
      } catch (error) {
        setUpload(null)
        if ((error as Error).name !== 'AbortError') setNotice(noticeFor(error))
        return false
      }
    },
    [resetOutputJob, setDraftRecoveryState, setMutationGate, submitPreview, workspace],
  )

  const rememberModalOpener = useCallback(() => {
    if (modalOpenerRef.current) return
    const active = document.activeElement
    if (
      active instanceof HTMLElement &&
      active !== document.body &&
      active !== fileInputRef.current &&
      !active.closest('[inert]')
    ) {
      modalOpenerRef.current = active
    }
  }, [])

  const queueFile = useCallback(
    (file: File) => {
      rememberModalOpener()
      setPendingConfigChange(null)
      setGuidedSampleActive(false)
      if (workspace) setReplacement(file)
      else void uploadFile(file)
    },
    [rememberModalOpener, uploadFile, workspace],
  )

  const startGuidedSample = useCallback(async () => {
    setFirstRunOpen(true)
    setGuidedSampleActive(true)
    setSamplePending(true)
    setNotice(null)
    try {
      const imported = await importGuidedSample((sample, projectName) =>
        uploadFile(sample, projectName),
      )
      setGuidedSampleActive(imported)
    } catch (error) {
      setGuidedSampleActive(false)
      setNotice(noticeFor(error))
    } finally {
      setSamplePending(false)
    }
  }, [uploadFile])

  useEffect(() => {
    const paste = (event: ClipboardEvent) => {
      const target = event.target
      if (
        target instanceof Element &&
        target.matches('input, textarea, select, [contenteditable="true"]')
      )
        return
      const file = Array.from(event.clipboardData?.files ?? []).find(
        (item) => item.type.startsWith('image/') || !item.type,
      )
      if (!file) return
      event.preventDefault()
      queueFile(file)
    }
    window.addEventListener('paste', paste)
    return () => window.removeEventListener('paste', paste)
  }, [queueFile])

  const chooseFile = () => {
    rememberModalOpener()
    fileInputRef.current?.click()
  }
  const selectedFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (file) queueFile(file)
  }
  const dropFile = (event: DragEvent<HTMLElement>) => {
    event.preventDefault()
    dragDepthRef.current = 0
    setDragActive(false)
    const file = Array.from(event.dataTransfer.files).find(
      (item) => item.type.startsWith('image/') || !item.type,
    )
    if (file) queueFile(file)
    else
      setNotice({
        title: 'No image found in that drop',
        message: 'Drop a PNG, JPEG, or WebP file to begin.',
      })
  }
  const enqueueDraftSave = (
    projectId: string,
    nextConfig: JobConfigV1,
    nextOperations: RegionOperation[],
    editVersion: number,
    historyIntent: EditorChangeIntent | null,
    requestId = historyIntent ? historyRequestId() : null,
    exactHistoryCommand?: EditorHistoryCommand,
  ) => {
    const snapshot: DraftSaveSnapshot = {
      projectId,
      config: nextConfig,
      operations: nextOperations,
      editVersion,
      historyIntent,
      requestId,
      historyCommand: exactHistoryCommand,
    }
    setDraftPersistence('saving')
    pendingSavesRef.current += 1
    saveChainRef.current = saveChainRef.current
      .catch(() => undefined)
      .then(async () => {
        const generation = draftGenerationsRef.current.get(projectId) ?? 0
        if (snapshot.historyIntent && !snapshot.historyCommand) {
          const history = draftHistoryRef.current.get(projectId)
          if (!history || !snapshot.requestId) {
            throw new Error('The saved edit-history cursor is unavailable. Reload the project and try again.')
          }
          snapshot.historyCommand = {
            schema_version: 1,
            id: snapshot.requestId,
            command_type: snapshot.historyIntent.commandType,
            label: snapshot.historyIntent.label,
            before_state_sha256: history.state_sha256,
            expected_cursor_node_id: history.cursor_node_id,
          }
        }
        const saved = await saveDraft(
          projectId,
          snapshot.config,
          snapshot.operations,
          generation,
          snapshot.historyCommand,
        )
        draftGenerationsRef.current.set(projectId, saved.generation)
        draftHistoryRef.current.set(projectId, saved.history)
        setWorkspace((current) =>
          current?.project.id === projectId ? { ...current, draft: saved } : current,
        )
        if (editVersion === editVersionRef.current) {
          failedDraftRef.current = null
          setDraftRecoveryState(null)
          if (draftFailureNoticeRef.current) {
            draftFailureNoticeRef.current = false
            setNotice(null)
          }
        }
      })
      .catch((error: unknown) => {
        if (editVersion === editVersionRef.current) {
          const recovery = draftRecoveryFor(error)
          failedDraftRef.current = { ...snapshot }
          setDraftRecoveryState(recovery)
          if (recovery === 'resync') setMutationGate('conflict')
          draftFailureNoticeRef.current = true
          setDraftPersistence('error')
          setNotice(noticeFor(error))
        }
      })
      .finally(() => {
        pendingSavesRef.current = Math.max(0, pendingSavesRef.current - 1)
        if (pendingSavesRef.current === 0 && scheduledDraftRef.current === null) {
          const currentFailure = failedDraftRef.current
          setDraftPersistence(
            currentFailure?.editVersion === editVersionRef.current ? 'error' : 'saved',
          )
        }
      })
  }

  const cancelScheduledDraftSave = () => {
    if (draftSaveTimerRef.current !== null) window.clearTimeout(draftSaveTimerRef.current)
    draftSaveTimerRef.current = null
    scheduledDraftRef.current = null
  }

  const flushScheduledDraftSave = () => {
    const scheduled = scheduledDraftRef.current
    if (!scheduled) return
    cancelScheduledDraftSave()
    enqueueDraftSave(
      scheduled.projectId,
      scheduled.config,
      scheduled.operations,
      scheduled.editVersion,
      scheduled.historyIntent,
      scheduled.requestId,
      scheduled.historyCommand,
    )
  }
  useEffect(() => {
    flushDraftSaveRef.current = flushScheduledDraftSave
  })

  const scheduleDraftSave = (
    nextConfig: JobConfigV1,
    editVersion: number,
    historyIntent: EditorChangeIntent,
  ) => {
    if (!workspace || activeProjectIdRef.current !== workspace.project.id) return
    if (draftSaveTimerRef.current !== null) window.clearTimeout(draftSaveTimerRef.current)
    scheduledDraftRef.current = {
      projectId: workspace.project.id,
      config: nextConfig,
      operations: [...operationsRef.current],
      editVersion,
      historyIntent,
      requestId: historyRequestId(),
    }
    setDraftPersistence('saving')
    draftSaveTimerRef.current = window.setTimeout(flushScheduledDraftSave, 300)
  }

  const changeConfigLocally = (next: JobConfigV1, invalidatesRender = true) => {
    if (!workspace || activeProjectIdRef.current !== workspace.project.id) {
      return editVersionRef.current
    }
    editVersionRef.current += 1
    if (invalidatesRender) {
      markPreviewStale()
      markOutputStale()
    }
    setConfig(next)
    if (invalidatesRender) setDirty(true)
    return editVersionRef.current
  }

  const persistDraftState = (
    nextConfig: JobConfigV1,
    nextOperations: RegionOperation[],
    historyIntent: EditorChangeIntent,
    invalidatesRender = true,
  ) => {
    if (
      !workspace ||
      activeProjectIdRef.current !== workspace.project.id ||
      ['history', 'conflict', 'resync'].includes(mutationGateRef.current)
    ) return
    flushScheduledDraftSave()
    operationsRef.current = nextOperations
    setOperations(nextOperations)
    const editVersion = changeConfigLocally(nextConfig, invalidatesRender)
    enqueueDraftSave(
      workspace.project.id,
      nextConfig,
      nextOperations,
      editVersion,
      historyIntent,
    )
  }
  const applyConfigChange = (
    nextConfig: JobConfigV1,
    nextOperations: RegionOperation[],
    persistence: ConfigChangePersistence,
    historyIntent: EditorChangeIntent,
  ) => {
    if (persistence === 'provisional') {
      changeConfigLocally(nextConfig)
      return
    }
    if (persistence === 'immediate') {
      persistDraftState(nextConfig, nextOperations, historyIntent)
      return
    }
    operationsRef.current = nextOperations
    setOperations(nextOperations)
    const editVersion = changeConfigLocally(nextConfig)
    scheduleDraftSave(nextConfig, editVersion, historyIntent)
  }
  const requestConfigChange = (
    nextConfig: JobConfigV1,
    nextOperations: RegionOperation[] = operationsRef.current,
    persistence: ConfigChangePersistence = 'debounced',
    historyIntent: EditorChangeIntent = {
      commandType: 'config_change',
      label: 'Change editor settings',
    },
  ) => {
    if (!workspace || activeProjectIdRef.current !== workspace.project.id) return false
    if (['history', 'conflict', 'resync'].includes(mutationGateRef.current)) return false
    if (
      config &&
      JSON.stringify(nextConfig) === JSON.stringify(config) &&
      JSON.stringify(nextConfig) === JSON.stringify(workspace.draft?.config)
    ) {
      if (persistence !== 'provisional' && mutationGateRef.current === 'provisional') {
        setMutationGate('idle')
      }
      return false
    }
    const incompatibleOperationCount = incompatibleManualOperationCount(operationsRef.current)
    if (incompatibleOperationCount > 0) {
      if (persistence === 'provisional') return false
      setPendingConfigChange({
        projectId: workspace.project.id,
        config: nextConfig,
        operations: withoutIncompatibleManualOperations(nextOperations),
        incompatibleOperationCount,
        persistence: 'immediate',
        historyIntent: {
          commandType: 'clear_manual_and_apply',
          label: `${historyIntent.label} + clear ${incompatibleOperationCount} manual ${incompatibleOperationCount === 1 ? 'edit' : 'edits'}`,
        },
      })
      return true
    }
    if (persistence === 'provisional') setMutationGate('provisional')
    else if (mutationGateRef.current === 'provisional') setMutationGate('idle')
    applyConfigChange(nextConfig, nextOperations, persistence, historyIntent)
    return true
  }
  const retryDraftSave = () => {
    const failed = failedDraftRef.current
    if (
      !failed ||
      failed.editVersion !== editVersionRef.current ||
      draftRecoveryRef.current !== 'retry'
    ) return
    draftFailureNoticeRef.current = false
    setNotice(null)
    enqueueDraftSave(
      failed.projectId,
      failed.config,
      failed.operations,
      failed.editVersion,
      failed.historyIntent,
      failed.requestId,
      failed.historyCommand,
    )
  }
  const waitForSavedDraft = async () => {
    flushDraftSaveRef.current()
    await saveChainRef.current
    const failed = failedDraftRef.current
    if (failed?.editVersion === editVersionRef.current) {
      throw new Error('The latest draft is not saved. Retry the draft save, then try again.')
    }
  }
  const publishCurrentRevision = async ({
    label,
    notes,
    previewJobId,
  }: {
    label: string
    notes: string
    previewJobId: string | null
  }) => {
    if (!workspace?.draft) throw new Error('This project does not have a draft to publish.')
    const projectId = workspace.project.id
    const initialGeneration = workspace.draft.generation
    await waitForSavedDraft()
    if (activeProjectIdRef.current !== projectId) {
      throw new DOMException('Revision publication was superseded by project replacement.', 'AbortError')
    }
    const generation = draftGenerationsRef.current.get(projectId) ?? initialGeneration
    try {
      const published = await publishRevision(projectId, {
        label,
        notes,
        expected_draft_generation: generation,
        preview_job_id: previewJobId,
      })
      if (activeProjectIdRef.current !== projectId) return published
      draftGenerationsRef.current.set(projectId, published.draft.generation)
      draftHistoryRef.current.set(projectId, published.draft.history)
      operationsRef.current = published.draft.operations
      setOperations(published.draft.operations)
      setConfig(published.draft.config)
      setWorkspace((current) =>
        current?.project.id === projectId
          ? { ...current, project: published.project, draft: published.draft }
          : current,
      )
      setDirty(false)
      return published
    } catch (error) {
      if (error instanceof ApiRequestError && error.problem?.error.code === 'stale_draft') {
        const refreshed = await fetchProject(projectId)
        if (activeProjectIdRef.current === projectId && refreshed.draft) {
          draftGenerationsRef.current.set(projectId, refreshed.draft.generation)
          draftHistoryRef.current.set(projectId, refreshed.draft.history)
          operationsRef.current = refreshed.draft.operations
          setOperations(refreshed.draft.operations)
          setConfig(refreshed.draft.config)
          setWorkspace(refreshed)
          setDirty(true)
          markPreviewStale()
        }
      }
      throw error
    }
  }
  const branchFromRevision = async (revision: RevisionResource) => {
    if (!workspace?.draft) throw new Error('This project does not have a draft to replace.')
    const projectId = workspace.project.id
    const initialGeneration = workspace.draft.generation
    await waitForSavedDraft()
    if (activeProjectIdRef.current !== projectId) {
      throw new DOMException('Revision branch was superseded by project replacement.', 'AbortError')
    }
    const generation = draftGenerationsRef.current.get(projectId) ?? initialGeneration
    try {
      const branched = await branchRevision(projectId, revision.id, generation)
      if (activeProjectIdRef.current !== projectId) return branched
      draftGenerationsRef.current.set(projectId, branched.draft.generation)
      draftHistoryRef.current.set(projectId, branched.draft.history)
      operationsRef.current = branched.draft.operations
      setOperations(branched.draft.operations)
      setConfig(branched.draft.config)
      setWorkspace((current) =>
        current?.project.id === projectId
          ? { ...current, project: branched.project, draft: branched.draft }
          : current,
      )
      setDirty(true)
      markPreviewStale()
      return branched
    } catch (error) {
      if (error instanceof ApiRequestError && error.problem?.error.code === 'stale_draft') {
        const refreshed = await fetchProject(projectId)
        if (activeProjectIdRef.current === projectId && refreshed.draft) {
          draftGenerationsRef.current.set(projectId, refreshed.draft.generation)
          draftHistoryRef.current.set(projectId, refreshed.draft.history)
          operationsRef.current = refreshed.draft.operations
          setOperations(refreshed.draft.operations)
          setConfig(refreshed.draft.config)
          setWorkspace(refreshed)
          markPreviewStale()
        }
      }
      throw error
    }
  }
  const commitPalette = (
    nextConfig: JobConfigV1,
    action: string,
    colorId: string | null,
    source: 'automatic' | 'manual' = 'manual',
  ) => {
    if (!config) return
    const operation: RegionOperation = {
      operation_type: 'palette-edit',
      selection: colorId ? { color_id: colorId } : {},
      parameters: {
        action,
        before_palette: config.palette.colors,
        after_palette: nextConfig.palette.colors,
      },
      source,
      provenance: { ui: 'palette-editor-v1' },
    }
    requestConfigChange(
      nextConfig,
      [...operationsRef.current, operation],
      'immediate',
      { commandType: 'palette_change', label: paletteActionLabel(action) },
    )
  }
  const moveHistory = async (direction: 'undo' | 'redo', requestId: string) => {
    if (!workspace?.draft) throw new Error('This project does not have an editable draft.')
    if (mutationGateRef.current !== 'idle') return
    const projectId = workspace.project.id
    setMutationGate('history')
    try {
      await waitForSavedDraft()
      if (activeProjectIdRef.current !== projectId) {
        throw new DOMException('History action was superseded by project replacement.', 'AbortError')
      }
      const history = draftHistoryRef.current.get(projectId)
      if (!history) throw new Error('The edit-history cursor is unavailable. Reload the project and try again.')
      const generation = draftGenerationsRef.current.get(projectId) ?? workspace.draft.generation
      const moved = await moveDraftHistory(projectId, direction, {
        request_id: requestId,
        expected_draft_generation: generation,
        expected_cursor_node_id: history.cursor_node_id,
      })
      if (activeProjectIdRef.current !== projectId) return
      editVersionRef.current += 1
      draftGenerationsRef.current.set(projectId, moved.generation)
      draftHistoryRef.current.set(projectId, moved.history)
      operationsRef.current = moved.operations
      setOperations(moved.operations)
      setConfig(moved.config)
      setWorkspace((current) =>
        current?.project.id === projectId ? { ...current, draft: moved } : current,
      )
      const currentProjectPreview =
        previewState.result?.job.project_id === projectId ? previewState.result : null
      const preservesRenderDerivation =
        previewState.resultFreshness === 'current' &&
        previewMatchesDraft(currentProjectPreview, moved)
      setDirty(!preservesRenderDerivation)
      setDraftPersistence('saved')
      setMutationGate('idle')
      if (!preservesRenderDerivation) {
        markPreviewStale()
        markOutputStale()
      }
    } catch (error) {
      const conflict = error instanceof ApiRequestError && error.status === 409
      setMutationGate(conflict ? 'conflict' : 'idle')
      throw error
    }
  }
  const resyncHistory = async () => {
    const projectId = activeProjectIdRef.current
    if (!projectId) throw new Error('There is no active project to reload.')
    cancelScheduledDraftSave()
    const previousGate = mutationGateRef.current
    const expectedEditVersion = editVersionRef.current
    setMutationGate('resync')
    try {
      await saveChainRef.current
      if (
        activeProjectIdRef.current !== projectId ||
        mutationGateRef.current !== 'resync' ||
        editVersionRef.current !== expectedEditVersion
      ) {
        throw new DOMException('Draft reload was superseded by a newer editor session.', 'AbortError')
      }
      const refreshed = await fetchProject(projectId)
      if (
        activeProjectIdRef.current !== projectId ||
        mutationGateRef.current !== 'resync' ||
        editVersionRef.current !== expectedEditVersion
      ) {
        throw new DOMException('Draft reload was superseded by a newer editor session.', 'AbortError')
      }
      if (!refreshed.draft) throw new Error('The project does not have an editable draft to reload.')
      editVersionRef.current += 1
      failedDraftRef.current = null
      setDraftRecoveryState(null)
      draftGenerationsRef.current.set(projectId, refreshed.draft.generation)
      draftHistoryRef.current.set(projectId, refreshed.draft.history)
      operationsRef.current = refreshed.draft.operations
      setOperations(refreshed.draft.operations)
      setConfig(refreshed.draft.config)
      setWorkspace(refreshed)
      setDirty(true)
      setDraftPersistence('saved')
      setNotice(null)
      setMutationGate('idle')
      markPreviewStale()
    } catch (error) {
      if (
        activeProjectIdRef.current === projectId &&
        mutationGateRef.current === 'resync'
      ) {
        setMutationGate(previousGate === 'conflict' ? 'conflict' : 'idle')
      }
      throw error
    }
  }
  const historyController = useEditorHistory({
    projectId: workspace?.project.id ?? '',
    history: workspace?.draft?.history ?? EMPTY_HISTORY,
    onMove: moveHistory,
    onResync: resyncHistory,
    blocked: draftPersistence !== 'saved' || mutationGate === 'provisional',
  })
  const renderPreview = () => {
    if (workspace && config) void submitPreview(workspace, config)
  }
  const activePreview =
    previewState.result?.job.project_id === workspace?.project.id ? previewState.result : null
  const activeJob =
    previewState.job?.project_id === workspace?.project.id ? previewState.job : null
  const submitting = previewState.phase === 'submitting'
  const activeOutputState = outputState.projectId === workspace?.project.id ? outputState : {
    ...outputState,
    phase: 'idle' as const,
    result: null,
    resultFreshness: 'none' as const,
    failure: null,
  }
  const outputWorking = [
    'submitting-geometry',
    'geometry-queued',
    'geometry-running',
    'submitting-export',
    'export-queued',
    'export-running',
    'canceling',
  ].includes(activeOutputState.phase)
  const previewWorking = ['submitting', 'queued', 'running', 'canceling'].includes(previewState.phase)
  const guideStage = firstRunStage({
    hasProject: Boolean(workspace),
    previewCurrent: previewState.resultFreshness === 'current',
    geometryReady: Boolean(activeOutputState.geometryResult),
    outputWorking,
    outputCurrent:
      activeOutputState.phase === 'succeeded' && activeOutputState.resultFreshness === 'current',
  })
  const generateOutput = async () => {
    if (!workspace?.draft || !config || !activePreview) return
    const projectId = workspace.project.id
    await waitForSavedDraft()
    if (
      activeProjectIdRef.current !== projectId ||
      previewState.resultFreshness !== 'current' ||
      !previewMatchesDraft(activePreview, workspace.draft)
    ) {
      setNotice({
        title: 'Update the preview before generating output',
        message: 'The exact preview no longer matches the current saved draft.',
      })
      return
    }
    const nozzleDiameterMm = config.printer.nozzle_mm === 0.2 ? 0.2 : 0.4
    await startOutputJob({
      projectId,
      previewJobId: activePreview.job.id,
      expectedDraftGeneration:
        draftGenerationsRef.current.get(projectId) ?? workspace.draft.generation,
      name: workspace.project.name,
      nozzleDiameterMm,
      layerHeightMm: nozzleDiameterMm === 0.2 ? 0.1 : 0.2,
      minimumPartThicknessMm: Math.min(
        config.geometry.base_thickness_mm,
        config.geometry.art_thickness_mm,
      ),
    })
  }
  const downloadOutput = () => {
    const url = activeOutputState.result?.package?.download_url
    if (!url || activeOutputState.resultFreshness !== 'current') return
    const link = document.createElement('a')
    link.href = url
    link.download = ''
    link.rel = 'noopener'
    document.body.append(link)
    link.click()
    link.remove()
  }

  return (
    <main
      className="app-shell"
      onDragEnter={(event) => {
        if (calibrationOpen) return
        event.preventDefault()
        dragDepthRef.current += 1
        setDragActive(true)
      }}
      onDragOver={(event) => {
        if (!calibrationOpen) event.preventDefault()
      }}
      onDragLeave={(event) => {
        if (calibrationOpen) return
        event.preventDefault()
        dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)
        if (dragDepthRef.current === 0) setDragActive(false)
      }}
      onDrop={(event) => {
        if (!calibrationOpen) dropFile(event)
      }}
    >
      {workspace && config && sourceUrl && !projectBrowserOpen && !calibrationOpen ? (
        <a className="i23-editor-skip-link" href={`#${STUDIO_CANVAS_ID}`}>
          Skip to canvas
        </a>
      ) : null}
      <header className="topbar">
        <a className="brand" href="/" aria-label="PLAyered home">
          <svg className="brand-mark" viewBox="0 0 64 64" aria-hidden="true">
            <rect x="4" y="10" width="56" height="50" rx="11" fill="#b8410f" />
            <rect x="4" y="4" width="56" height="50" rx="11" fill="#ff6a2b" />
            <path fill="#15131a" d="M20 14h14.5a11.5 11.5 0 0 1 0 23H28v9h-8zm8 7v9h6a4.5 4.5 0 0 0 0-9z" />
          </svg>
          <span>
            <strong>PLAyered</strong>
            <small>Print preparation studio</small>
          </span>
        </a>
        <div className="topbar-meta">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => {
              setCalibrationOpen(false)
              setProjectBrowserOpen(true)
            }}
          >
            Projects
          </button>
          <button
            className="button button-secondary"
            type="button"
            aria-pressed={calibrationOpen}
            onClick={() => {
              setProjectBrowserOpen(false)
              setCalibrationOpen((current) => !current)
            }}
          >
            {calibrationOpen ? 'Back to studio' : 'Calibration'}
          </button>
          {workspace && !calibrationOpen ? (
            <>
              {workspace.draft ? (
                <RevisionLifecycle
                  key={workspace.project.id}
                  projectId={workspace.project.id}
                  activeRevisionId={workspace.project.active_revision_id}
                  draftBaseRevisionId={workspace.draft.base_revision_id}
                  persistence={draftPersistence}
                  artifactFreshness={
                    activePreview &&
                    previewState.resultFreshness === 'current' &&
                    previewMatchesDraft(activePreview, workspace.draft)
                      ? 'current'
                      : activePreview
                        ? 'stale'
                        : 'missing'
                  }
                  previewJobId={
                    activePreview &&
                    previewState.resultFreshness === 'current' &&
                    previewMatchesDraft(activePreview, workspace.draft)
                      ? activePreview.job.id
                      : null
                  }
                  workingArtifacts={activePreview?.artifacts ?? []}
                  onWorkingArtifactDeleted={() => {
                    markPreviewStale()
                    markOutputStale()
                    setDirty(true)
                  }}
                  onPublish={publishCurrentRevision}
                  onBranch={branchFromRevision}
                />
              ) : null}
              <div className="autosave-control" data-state={draftPersistence}>
                <span className="autosave-state" role="status">
                  {draftPersistence === 'saving'
                    ? 'Saving draft…'
                    : draftPersistence === 'error'
                      ? 'Draft save needs attention'
                      : 'Draft saved locally'}
                </span>
                {draftPersistence === 'error' && draftRecovery === 'retry' ? (
                  <button type="button" className="autosave-retry" onClick={retryDraftSave}>
                    Retry save
                  </button>
                ) : null}
                {draftPersistence === 'error' && draftRecovery === 'resync' ? (
                  <button
                    type="button"
                    className="autosave-retry"
                    onClick={historyController.resync}
                  >
                    Reload current draft
                  </button>
                ) : null}
              </div>
            </>
          ) : null}
          <span className={`connection connection-${engineStatus}`} role="status">
            {engineStatus !== 'ready' ? <i aria-hidden="true" /> : null}
            {engineStatus === 'ready' ? (
              <button
                type="button"
                className="workspace-status-button"
                disabled={!startupRecovery}
                aria-label="Workspace status"
                title="Workspace status"
                onClick={() => {
                  rememberModalOpener()
                  setRecoveryOpen(true)
                }}
              >
                <i aria-hidden="true" />
                <span>Local engine · v{health?.version}</span>
              </button>
            ) : null}
            {engineStatus === 'loading' ? 'Checking local engine…' : null}
            {engineStatus === 'offline' ? 'Local engine unavailable' : null}
          </span>
        </div>
      </header>

      <input
        className="visually-hidden"
        ref={fileInputRef}
        type="file"
        tabIndex={-1}
        accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
        aria-label="Choose source image"
        onChange={selectedFile}
      />

      {startupRecovery ? (
        <WorkspaceRecoveryNotice
          report={startupRecovery}
          onOpen={() => {
            rememberModalOpener()
            setRecoveryOpen(true)
          }}
        />
      ) : null}

      {calibrationOpen ? (
        <CalibrationWorkspace onClose={() => setCalibrationOpen(false)} />
      ) : projectBrowserOpen ? (
        <ProjectBrowser
          onClose={() => setProjectBrowserOpen(false)}
          onOpen={(projectId) => {
            window.localStorage.setItem(RECENT_PROJECT_KEY, projectId)
            window.location.reload()
          }}
        />
      ) : !workspace || !config || !sourceUrl ? (
        <>
          <ImportPanel
            engineReady={engineStatus === 'ready'}
            samplePending={samplePending}
            onChoose={chooseFile}
            onTrySample={() => void startGuidedSample()}
            onOpenGuide={() => setFirstRunOpen(true)}
          />
          {notice ? (
            <div className="notice notice-error global-notice" role="alert">
              <div>
                <strong>{notice.title}</strong>
                <p>{notice.message}</p>
                {notice.suggestion ? <small>{notice.suggestion}</small> : null}
              </div>
              <button type="button" aria-label="Dismiss error" onClick={() => setNotice(null)}>
                ×
              </button>
            </div>
          ) : null}
        </>
      ) : (
        <StudioWorkspaceView
          key={workspace.project.id}
          config={config}
          dirty={dirty}
          persistence={draftPersistence}
          job={activeJob}
          notice={notice}
          preview={activePreview}
          previewJobState={previewState}
          outputState={activeOutputState}
          resolvedPrintability={resolvedPrintability}
          sourceUrl={sourceUrl}
          submitting={submitting}
          editingDisabled={['history', 'conflict', 'resync'].includes(mutationGate)}
          workspace={workspace}
          historyControls={
            workspace.draft ? (
              <HistoryControls
                history={workspace.draft.history}
                working={historyController.working}
                error={historyController.error}
                conflict={historyController.conflict}
                retryable={historyController.retryable}
                announcement={historyController.announcement}
                blockedReason={
                  mutationGate === 'provisional'
                    ? 'Finish the current crop or cleanup gesture before undo or redo'
                    : mutationGate === 'conflict'
                      ? 'Reload the current draft before editing, undoing, or redoing'
                      : draftPersistence === 'saving'
                    ? 'Finish saving before undo or redo'
                    : draftPersistence === 'error'
                      ? draftRecovery === 'retry'
                        ? 'Retry the draft save before undo or redo'
                        : draftRecovery === 'resync'
                          ? 'Reload the current draft before undo or redo'
                          : 'Correct the invalid settings before undo or redo'
                      : null
                }
                onMove={(direction) => void historyController.move(direction)}
                onRetry={historyController.retry}
                onResync={historyController.resync}
              />
            ) : null
          }
          onCancelJob={() => void cancelPreviewJob()}
          onChange={(next, intent, persistence = 'debounced') =>
            requestConfigChange(next, operationsRef.current, persistence, intent)
          }
          onProvisionalChange={(next) =>
            requestConfigChange(next, operationsRef.current, 'provisional')
          }
          onCleanupCommit={(next, intent) =>
            requestConfigChange(next, operationsRef.current, 'immediate', intent)
          }
          onPaletteCommit={commitPalette}
          onOperationCommit={(operation, intent) => {
            if (!config || ['history', 'conflict', 'resync'].includes(mutationGateRef.current)) return
            persistDraftState(
              config,
              [...operationsRef.current, operation],
              intent,
              operation.operation_type !== 'canvas_selection_v1',
            )
          }}
          onClearNotice={() => setNotice(null)}
          onPreview={renderPreview}
          onRetryPreview={() => void retryPreviewJob()}
          onGenerateOutput={() => void generateOutput()}
          onCancelOutput={() => void cancelOutputJob()}
          onRetryOutput={() => void retryOutputJob()}
          onDownloadOutput={downloadOutput}
          onReplace={chooseFile}
          onPromptedAccepted={resyncHistory}
        />
      )}

      {!projectBrowserOpen && !calibrationOpen ? (
        <FirstRunGuide
          open={firstRunOpen}
          stage={guideStage}
          sampleActive={guidedSampleActive}
          previewWorking={previewWorking}
          outputWorking={outputWorking}
          canGenerate={Boolean(
            workspace?.draft
            && activePreview
            && previewState.resultFreshness === 'current'
            && !dirty
            && !outputWorking
            && activeOutputState.phase !== 'succeeded'
          )}
          canDownload={Boolean(
            activeOutputState.result?.package
            && activeOutputState.resultFreshness === 'current'
          )}
          onClose={() => setFirstRunOpen(false)}
          onOpen={() => setFirstRunOpen(true)}
          onGenerate={() => void generateOutput()}
          onDownload={downloadOutput}
        />
      ) : null}

      <footer>
        <span>Private and local by default</span>
        <span>{workspace ? 'M3 · Image-to-3MF workflow' : 'PLAyered'}</span>
      </footer>

      {dragActive ? (
        <div className="drop-overlay" aria-hidden="true">
          <div>
            <span>+</span>
            <strong>{workspace ? 'Drop to replace the source' : 'Drop to import this image'}</strong>
          </div>
        </div>
      ) : null}

      {upload ? createPortal(
        <div className="modal-backdrop" role="presentation">
          <section
            ref={modalDialogRef}
            className="upload-dialog"
            role="dialog"
            aria-modal="true"
            aria-busy="true"
            aria-label="Importing image"
            tabIndex={-1}
          >
            <div className="upload-spinner" aria-hidden="true" />
            <span className="eyebrow">{upload.replacing ? 'Replacing source' : 'Creating project'}</span>
            <h2>Importing {upload.filename}</h2>
            <p>Checking the file, normalizing color, and preserving transparency.</p>
            <div className="upload-progress-label">
              <span>Uploading locally</span>
              <span>{Math.round(upload.progress * 100)}%</span>
            </div>
            <progress max="1" value={upload.progress} aria-label="Image upload progress" />
            <button
              ref={modalInitialFocusRef}
              className="button button-secondary"
              type="button"
              onClick={() => uploadControllerRef.current?.abort()}
            >
              Cancel import
            </button>
          </section>
        </div>,
        document.body,
      ) : null}

      {replacement ? createPortal(
        <div className="modal-backdrop" role="presentation">
          <section
            ref={modalDialogRef}
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="replace-heading"
            aria-describedby="replace-description"
            tabIndex={-1}
          >
            <span className="dialog-icon" aria-hidden="true">
              ↻
            </span>
            <span className="eyebrow">Confirm replacement</span>
            <h2 id="replace-heading">Replace the current source?</h2>
            <p id="replace-description">
              Importing <strong>{replacement.name}</strong> starts a new project. Your current project
              stays safely stored in the local workspace.
            </p>
            <div className="dialog-actions">
              <button
                ref={modalInitialFocusRef}
                className="button button-secondary"
                type="button"
                onClick={() => setReplacement(null)}
              >
                Keep current image
              </button>
              <button
                className="button button-primary"
                type="button"
                onClick={() => void uploadFile(replacement)}
              >
                Import replacement
              </button>
            </div>
          </section>
        </div>,
        document.body,
      ) : null}

      {pendingConfigChange ? (
        <ConfigChangeGuard
          operationCount={pendingConfigChange.incompatibleOperationCount}
          retainedOperationCount={pendingConfigChange.operations.length}
          onCancel={() => {
            setPendingConfigChange(null)
            if (mutationGateRef.current === 'provisional') setMutationGate('idle')
          }}
          onConfirm={() => {
            const pending = pendingConfigChange
            setPendingConfigChange(null)
            if (activeProjectIdRef.current !== pending.projectId) return
            if (mutationGateRef.current === 'provisional') setMutationGate('idle')
            applyConfigChange(
              pending.config,
              pending.operations,
              pending.persistence,
              pending.historyIntent,
            )
          }}
        />
      ) : null}

      {recoveryOpen && startupRecovery ? createPortal(
        <WorkspaceRecoveryPanel
          report={startupRecovery}
          onReconcile={async () => {
            const report = await reconcileWorkspace()
            setStartupRecovery(report)
            return report
          }}
          onReviewCleanup={() => planGarbageCollection(24 * 60 * 60)}
          onClose={() => setRecoveryOpen(false)}
        />,
        document.body,
      ) : null}
    </main>
  )
}

export default App
