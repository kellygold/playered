import { useEffect, useId, useRef, useState } from 'react'
import { fetchRevision, fetchRevisions } from '../../api'
import type {
  ArtifactResource,
  RegionOperation,
  RevisionBranch,
  RevisionPublication,
  RevisionResource,
  RevisionSummary,
} from '../../contracts'
import {
  evidenceLabel,
  publicationGate,
  regionalEditEvidence,
  relationshipLabel,
  revisionRelationship,
  type ArtifactFreshness,
  type DraftPersistence,
} from './revisionModel'
import { RevisionArtifactPanel } from './RevisionArtifactPanel'
import { RevisionComparison } from './RevisionComparison'
import './RevisionLifecycle.css'

type PublishInput = {
  label: string
  notes: string
  previewJobId: string | null
}

function RegionalEditProvenance({ operations }: { operations: RegionOperation[] }) {
  const entries = operations.flatMap((operation, index) => {
    const evidence = regionalEditEvidence(operation)
    return evidence ? [{ evidence, operation, index }] : []
  })
  if (!entries.length) return null
  return (
    <section className="revision-edit-provenance" aria-label="Regional edit reproducibility">
      <div><span className="eyebrow">Recorded provenance</span><h4>Regional edit reproducibility</h4></div>
      <ul>
        {entries.map(({ evidence, operation, index }) => (
          <li key={`${operation.operation_type}:${index}`} data-reproducibility={evidence.classification}>
            <div><strong>{evidence.label}</strong><small>{operation.operation_type.replaceAll('_', ' ')}</small></div>
            <p>{evidence.reason}</p>
            {evidence.providerModel ? <small>{evidence.providerModel}</small> : <small>Local image23mf engine</small>}
            {evidence.outputSha256 ? <code>Output {evidence.outputSha256.slice(0, 12)}…</code> : null}
          </li>
        ))}
      </ul>
    </section>
  )
}

type RevisionLifecycleProps = {
  projectId: string
  activeRevisionId: string | null
  draftBaseRevisionId: string | null
  persistence: DraftPersistence
  artifactFreshness: ArtifactFreshness
  previewJobId: string | null
  workingArtifacts?: ArtifactResource[]
  onWorkingArtifactDeleted?: () => void
  onPublish: (input: PublishInput) => Promise<RevisionPublication>
  onBranch: (revision: RevisionResource) => Promise<RevisionBranch>
}

type RetryAction =
  | { kind: 'publish'; input: PublishInput }
  | { kind: 'branch'; revision: RevisionResource }

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'The local engine could not complete this action.'
}

function publishedDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      }).format(date)
}

function focusableElements(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(
    'button, input, textarea, select, [href], [tabindex]',
  )].filter((element) => {
    const formControl = element as HTMLButtonElement | HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
    return !formControl.disabled && element.tabIndex !== -1 && !element.hidden && element.getAttribute('aria-hidden') !== 'true'
  }).sort((left, right) => (
    left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1
  ))
}

function containTab(
  event: Pick<KeyboardEvent, 'preventDefault' | 'shiftKey'>,
  container: HTMLElement,
): void {
  const focusable = focusableElements(container)
  if (!focusable.length) {
    event.preventDefault()
    container.focus()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement
  if (event.shiftKey && (active === first || !container.contains(active))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (active === last || !container.contains(active))) {
    event.preventDefault()
    first.focus()
  }
}

export function RevisionLifecycle(props: RevisionLifecycleProps) {
  return <RevisionLifecycleSession key={props.projectId} {...props} />
}

function RevisionLifecycleSession({
  projectId,
  activeRevisionId,
  draftBaseRevisionId,
  persistence,
  artifactFreshness,
  previewJobId,
  workingArtifacts = [],
  onWorkingArtifactDeleted,
  onPublish,
  onBranch,
}: RevisionLifecycleProps) {
  const titleId = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  const publishButtonRef = useRef<HTMLButtonElement>(null)
  const branchButtonRef = useRef<HTMLButtonElement>(null)
  const publishConfirmRef = useRef<HTMLDivElement>(null)
  const publishConfirmCancelRef = useRef<HTMLButtonElement>(null)
  const branchConfirmRef = useRef<HTMLDivElement>(null)
  const branchConfirmCancelRef = useRef<HTMLButtonElement>(null)
  const loadControllerRef = useRef<AbortController | null>(null)
  const detailControllerRef = useRef<AbortController | null>(null)
  const listSequenceRef = useRef(0)
  const detailSequenceRef = useRef(0)
  const actionLockRef = useRef(false)
  const mountedRef = useRef(true)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null)
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [revisionTotal, setRevisionTotal] = useState(0)
  const [revisions, setRevisions] = useState<RevisionSummary[]>([])
  const [selected, setSelected] = useState<RevisionResource | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [label, setLabel] = useState('')
  const [notes, setNotes] = useState('')
  const [working, setWorking] = useState<'publish' | 'branch' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [retryAction, setRetryAction] = useState<RetryAction | null>(null)
  const [confirmWithoutArtifacts, setConfirmWithoutArtifacts] = useState(false)
  const [branchCandidate, setBranchCandidate] = useState<RevisionResource | null>(null)
  const [deletedWorkingArtifactIds, setDeletedWorkingArtifactIds] = useState<Set<string>>(
    () => new Set(),
  )
  const gate = publicationGate(label, persistence, artifactFreshness)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      listSequenceRef.current += 1
      detailSequenceRef.current += 1
      loadControllerRef.current?.abort()
      detailControllerRef.current?.abort()
    }
  }, [])

  const load = (cursor: string | null = null) => {
    const append = cursor !== null
    loadControllerRef.current?.abort()
    const controller = new AbortController()
    const sequence = ++listSequenceRef.current
    loadControllerRef.current = controller
    if (append) {
      setLoadingMore(true)
      setLoadMoreError(null)
    } else {
      setLoading(true)
      setListError(null)
      setLoadMoreError(null)
    }
    fetchRevisions(projectId, { limit: 50, cursor }, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted && sequence === listSequenceRef.current) {
          setRevisions((current) => {
            if (!append) return result.items
            const known = new Set(current.map((item) => item.id))
            return [...current, ...result.items.filter((item) => !known.has(item.id))]
          })
          setNextCursor(result.next_cursor ?? null)
          setRevisionTotal(result.total)
        }
      })
      .catch((error: unknown) => {
        if (
          (error as Error).name !== 'AbortError' &&
          !controller.signal.aborted &&
          sequence === listSequenceRef.current
        ) {
          if (append) setLoadMoreError(errorMessage(error))
          else setListError(errorMessage(error))
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && sequence === listSequenceRef.current) {
          if (append) setLoadingMore(false)
          else setLoading(false)
        }
      })
    return controller
  }

  useEffect(() => {
    if (!open) return
    closeRef.current?.focus()
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      listSequenceRef.current += 1
      detailSequenceRef.current += 1
      loadControllerRef.current?.abort()
      detailControllerRef.current?.abort()
      document.body.style.overflow = previousOverflow
    }
  }, [open])

  const close = () => {
    setOpen(false)
    setSelected(null)
    setConfirmWithoutArtifacts(false)
    setBranchCandidate(null)
    queueMicrotask(() => triggerRef.current?.focus())
  }

  const nestedConfirmationOpen = confirmWithoutArtifacts || branchCandidate !== null

  const keepFocusInNestedConfirmation = (event: { preventDefault: () => void; stopPropagation: () => void }) => {
    if (!nestedConfirmationOpen) return
    event.preventDefault()
    event.stopPropagation()
    if (confirmWithoutArtifacts) publishConfirmCancelRef.current?.focus()
    else branchConfirmCancelRef.current?.focus()
  }

  const dismissPublishConfirmation = () => {
    setConfirmWithoutArtifacts(false)
    queueMicrotask(() => publishButtonRef.current?.focus())
  }

  const dismissBranchConfirmation = () => {
    setBranchCandidate(null)
    queueMicrotask(() => branchButtonRef.current?.focus())
  }

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      const activeModal = confirmWithoutArtifacts
        ? publishConfirmRef.current
        : branchCandidate
          ? branchConfirmRef.current
          : drawerRef.current
      if (event.key === 'Tab' && activeModal) {
        containTab(event, activeModal)
        return
      }
      if (event.key !== 'Escape') return
      event.preventDefault()
      if (confirmWithoutArtifacts && working === null) {
        setConfirmWithoutArtifacts(false)
        queueMicrotask(() => publishButtonRef.current?.focus())
      } else if (branchCandidate && working === null) {
        setBranchCandidate(null)
        queueMicrotask(() => branchButtonRef.current?.focus())
      } else if (!confirmWithoutArtifacts && !branchCandidate && working === null) {
        setOpen(false)
        setSelected(null)
        queueMicrotask(() => triggerRef.current?.focus())
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [open, confirmWithoutArtifacts, branchCandidate, working])

  useEffect(() => {
    if (confirmWithoutArtifacts) publishConfirmCancelRef.current?.focus()
  }, [confirmWithoutArtifacts])

  useEffect(() => {
    if (branchCandidate) branchConfirmCancelRef.current?.focus()
  }, [branchCandidate])

  const openDrawer = () => {
    setSelected(null)
    setActionError(null)
    setRetryAction(null)
    setRevisions([])
    setNextCursor(null)
    setRevisionTotal(0)
    setOpen(true)
    load()
  }

  const openRevision = async (revision: RevisionSummary) => {
    detailControllerRef.current?.abort()
    const controller = new AbortController()
    const sequence = ++detailSequenceRef.current
    detailControllerRef.current = controller
    setActionError(null)
    setDetailLoading(true)
    try {
      const exact = await fetchRevision(projectId, revision.id, controller.signal)
      if (!controller.signal.aborted && sequence === detailSequenceRef.current) setSelected(exact)
    } catch (error) {
      if (
        (error as Error).name !== 'AbortError' &&
        !controller.signal.aborted &&
        sequence === detailSequenceRef.current
      ) setActionError(errorMessage(error))
    } finally {
      if (!controller.signal.aborted && sequence === detailSequenceRef.current) {
        setDetailLoading(false)
      }
    }
  }

  const performPublish = async (input: PublishInput) => {
    if (actionLockRef.current) return
    actionLockRef.current = true
    setWorking('publish')
    setActionError(null)
    setRetryAction(null)
    try {
      const result = await onPublish(input)
      if (!mountedRef.current) return
      setRevisions((current) => [{
        id: result.revision.id,
        project_id: result.revision.project_id,
        parent_revision_id: result.revision.parent_revision_id,
        label: result.revision.label,
        config_sha256: result.revision.config_sha256,
        editor_sequence_sha256: result.revision.editor_sequence_sha256,
        preview_evidence: result.revision.preview_evidence,
        published_at: result.revision.published_at,
        operation_count: result.revision.operations.length,
        artifact_count: result.revision.artifacts.length,
        is_active: true,
      }, ...current.filter((item) => item.id !== result.revision.id)])
      setRevisionTotal((current) => current + 1)
      setSelected(result.revision)
      setLabel('')
      setNotes('')
      setConfirmWithoutArtifacts(false)
    } catch (error) {
      if (mountedRef.current) {
        setActionError(errorMessage(error))
        setRetryAction({ kind: 'publish', input })
        setConfirmWithoutArtifacts(false)
      }
    } finally {
      actionLockRef.current = false
      if (mountedRef.current) setWorking(null)
    }
  }

  const requestPublish = () => {
    if (!gate.allowed || actionLockRef.current) return
    const input = {
      label: label.trim(),
      notes: notes.trim(),
      previewJobId: artifactFreshness === 'current' ? previewJobId : null,
    }
    if (gate.requiresArtifactConfirmation || !input.previewJobId) {
      setConfirmWithoutArtifacts(true)
      return
    }
    void performPublish(input)
  }

  const performBranch = async (revision: RevisionResource) => {
    if (actionLockRef.current) return
    actionLockRef.current = true
    setWorking('branch')
    setActionError(null)
    setRetryAction(null)
    try {
      await onBranch(revision)
      if (mountedRef.current) setBranchCandidate(null)
    } catch (error) {
      if (mountedRef.current) {
        setActionError(errorMessage(error))
        setRetryAction({ kind: 'branch', revision })
        setBranchCandidate(null)
      }
    } finally {
      actionLockRef.current = false
      if (mountedRef.current) setWorking(null)
    }
  }

  const retry = () => {
    if (!retryAction || actionLockRef.current) return
    if (retryAction.kind === 'branch') {
      void performBranch(retryAction.revision)
      return
    }
    const refreshedInput = {
      ...retryAction.input,
      previewJobId: artifactFreshness === 'current' ? previewJobId : null,
    }
    if (!refreshedInput.previewJobId) {
      setConfirmWithoutArtifacts(true)
      return
    }
    void performPublish(refreshedInput)
  }

  return (
    <div className="revision-lifecycle">
      <button
        ref={triggerRef}
        className="revision-trigger"
        type="button"
        aria-label="Revisions"
        aria-expanded={open}
        aria-controls="revision-drawer"
        onClick={openDrawer}
      >
        <span aria-hidden="true">◇</span>
        Revisions
        {activeRevisionId ? <i aria-hidden="true" /> : null}
      </button>

      {open ? (
        <div className="revision-backdrop" role="presentation">
          <section
            ref={drawerRef}
            id="revision-drawer"
            className="revision-drawer"
            role="dialog"
            aria-modal={nestedConfirmationOpen ? undefined : true}
            aria-hidden={nestedConfirmationOpen ? true : undefined}
            inert={nestedConfirmationOpen}
            aria-labelledby={titleId}
            onClickCapture={keepFocusInNestedConfirmation}
            onFocusCapture={keepFocusInNestedConfirmation}
          >
            <header className="revision-drawer-header">
              <div>
                <span className="eyebrow">Immutable checkpoints</span>
                <h2 id={titleId}>Project revisions</h2>
                <p>Publish a named snapshot, then keep editing from that exact point.</p>
              </div>
              <button ref={closeRef} type="button" aria-label="Close revisions" onClick={close}>×</button>
            </header>

            <div className="revision-draft-state" data-state={persistence}>
              <span aria-hidden="true" />
              <div>
                <strong>
                  {persistence === 'saved'
                    ? 'Draft saved'
                    : persistence === 'saving'
                      ? 'Saving latest draft…'
                      : 'Draft save needs attention'}
                </strong>
                <small>{gate.message}</small>
              </div>
              <em data-freshness={artifactFreshness}>
                {artifactFreshness === 'current'
                  ? 'Preview current'
                  : artifactFreshness === 'stale'
                    ? 'Preview stale'
                    : 'Preview missing'}
              </em>
            </div>

            <form
              className="revision-publish"
              onSubmit={(event) => {
                event.preventDefault()
                requestPublish()
              }}
            >
              <div className="revision-section-heading">
                <div>
                  <span className="eyebrow">New checkpoint</span>
                  <h3>Publish this draft</h3>
                </div>
                <span>Immutable after publish</span>
              </div>
              <label>
                <span>Revision name</span>
                <input
                  value={label}
                  maxLength={160}
                  placeholder="e.g. Cleanup approved"
                  onChange={(event) => setLabel(event.target.value)}
                />
              </label>
              <label>
                <span>Notes <small>optional</small></span>
                <textarea
                  value={notes}
                  maxLength={4000}
                  rows={2}
                  placeholder="What changed, or what should be checked next?"
                  onChange={(event) => setNotes(event.target.value)}
                />
              </label>
              <button ref={publishButtonRef} className="button button-primary" type="submit" disabled={!gate.allowed || working !== null}>
                {working === 'publish' ? 'Publishing…' : 'Publish revision'}
              </button>
            </form>

            {actionError ? (
              <div className="revision-action-error" role="alert">
                <div><strong>Revision action did not finish</strong><p>{actionError}</p></div>
                {retryAction ? <button type="button" onClick={retry}>Retry action</button> : null}
              </div>
            ) : null}

            <RevisionArtifactPanel
              projectId={projectId}
              artifacts={workingArtifacts.filter((artifact) => !deletedWorkingArtifactIds.has(artifact.id))}
              title="Current preview cache"
              ariaLabel="Current preview artifacts"
              emptyMessage="Render a preview to create regenerable working artifacts."
              onDeleted={(artifact) => {
                setDeletedWorkingArtifactIds((current) => new Set(current).add(artifact.id))
                onWorkingArtifactDeleted?.()
              }}
            />

            <section className="revision-history" aria-label="Published revisions">
              <div className="revision-section-heading">
                <div>
                  <span className="eyebrow">Saved history</span>
                  <h3>Published revisions</h3>
                </div>
                <span>
                  {revisions.length < revisionTotal
                    ? `${revisions.length} of ${revisionTotal} loaded`
                    : `${revisionTotal} saved`}
                </span>
              </div>
              {loading ? <p className="revision-empty" role="status">Loading exact revisions…</p> : null}
              {listError ? (
                <div className="revision-list-error" role="alert">
                  <p>{listError}</p>
                  <button type="button" onClick={() => load()}>Retry list</button>
                </div>
              ) : null}
              {!loading && !listError && revisions.length === 0 ? (
                <p className="revision-empty">No published revisions yet. Your autosaved draft is still safe.</p>
              ) : null}
              <ol className="revision-list">
                {revisions.map((revision) => {
                  const relationship = revisionRelationship(revision, activeRevisionId, draftBaseRevisionId)
                  return (
                    <li key={revision.id} data-current={relationship !== 'older'}>
                      <button type="button" onClick={() => void openRevision(revision)}>
                        <span className="revision-list-main">
                          <strong>{revision.label}</strong>
                          <small>{publishedDate(revision.published_at)}</small>
                        </span>
                        <span className="revision-list-meta">
                          <em>{relationshipLabel(relationship)}</em>
                          <small>{evidenceLabel(revision)}</small>
                        </span>
                      </button>
                    </li>
                  )
                })}
              </ol>
              {loadMoreError ? (
                <div className="revision-list-error" role="alert">
                  <p>{loadMoreError}</p>
                  <button type="button" onClick={() => nextCursor && load(nextCursor)}>Retry older revisions</button>
                </div>
              ) : null}
              {nextCursor && !loadMoreError ? (
                <button
                  className="revision-load-more"
                  type="button"
                  disabled={loadingMore}
                  onClick={() => load(nextCursor)}
                >
                  {loadingMore ? 'Loading older revisions…' : 'Load older revisions'}
                </button>
              ) : null}
            </section>

            {detailLoading ? <div className="revision-detail-loading" role="status">Opening immutable revision…</div> : null}
            {selected && !detailLoading ? (
              <section className="revision-detail" aria-label={`Revision details for ${selected.label}`}>
                <div className="revision-detail-title">
                  <div><span className="eyebrow">Read-only snapshot</span><h3>{selected.label}</h3></div>
                  <span className="revision-lock">Locked</span>
                </div>
                {selected.notes ? <p>{selected.notes}</p> : <p className="revision-muted">No publication notes.</p>}
                <dl>
                  <div><dt>Published</dt><dd>{publishedDate(selected.published_at)}</dd></div>
                  <div><dt>Canvas</dt><dd>{selected.config.canvas.width_mm} × {selected.config.canvas.height_mm} mm</dd></div>
                  <div><dt>Palette</dt><dd>{selected.config.palette.colors.length} colors</dd></div>
                  <div><dt>Operations</dt><dd>{selected.operations.length}</dd></div>
                  <div><dt>Evidence</dt><dd>{evidenceLabel(selected)}</dd></div>
                  <div><dt>Parent</dt><dd><code>{selected.parent_revision_id ?? 'Root revision'}</code></dd></div>
                  <div><dt>Revision ID</dt><dd><code>{selected.id}</code></dd></div>
                </dl>
                <p className="revision-evidence-reason">Preview provenance: {selected.preview_evidence.reason}</p>
                <RegionalEditProvenance operations={selected.operations} />
                <RevisionArtifactPanel projectId={projectId} artifacts={selected.artifacts} />
                <RevisionComparison projectId={projectId} revision={selected} revisions={revisions} />
                {selected.id === draftBaseRevisionId ? (
                  <p className="revision-current-base">The working draft already continues from this revision.</p>
                ) : (
                  <button
                    ref={branchButtonRef}
                    className="button button-secondary revision-branch-button"
                    type="button"
                    disabled={working !== null || persistence !== 'saved'}
                    onClick={() => setBranchCandidate(selected)}
                  >
                    Branch from this revision
                  </button>
                )}
              </section>
            ) : null}
          </section>

          {confirmWithoutArtifacts ? (
            <div ref={publishConfirmRef} className="revision-confirm" role="alertdialog" aria-modal="true" aria-label="Publish without current preview artifacts" tabIndex={-1}>
              <span className="revision-confirm-icon" aria-hidden="true">!</span>
              <h3>Publish without current preview artifacts?</h3>
              <p>The configuration and operations will be preserved exactly, but this revision will not contain a verified current preview. You can keep editing and render again afterward.</p>
              <div>
                <button ref={publishConfirmCancelRef} type="button" className="button button-secondary" disabled={working !== null} onClick={dismissPublishConfirmation}>Go back</button>
                <button type="button" className="button button-primary" disabled={working !== null} onClick={() => void performPublish({ label: label.trim(), notes: notes.trim(), previewJobId: null })}>{working === 'publish' ? 'Publishing…' : 'Publish without artifacts'}</button>
              </div>
            </div>
          ) : null}

          {branchCandidate ? (
            <div ref={branchConfirmRef} className="revision-confirm" role="alertdialog" aria-modal="true" aria-label="Confirm revision branch" tabIndex={-1}>
              <span className="revision-confirm-icon" aria-hidden="true">↙</span>
              <h3>Branch from “{branchCandidate.label}”?</h3>
              <p>Your current draft will be replaced by an editable copy of this immutable revision. Published revisions stay unchanged, including the current published checkpoint.</p>
              <div>
                <button ref={branchConfirmCancelRef} type="button" className="button button-secondary" disabled={working !== null} onClick={dismissBranchConfirmation}>Keep current draft</button>
                <button type="button" className="button button-primary" disabled={working !== null} onClick={() => void performBranch(branchCandidate)}>{working === 'branch' ? 'Creating branch…' : 'Create branch draft'}</button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
