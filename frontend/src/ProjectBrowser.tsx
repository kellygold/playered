import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  fetchProjectBundle,
  fetchProjects,
  importProjectBundle,
  setProjectArchived,
} from './api'
import type { ProjectBundleImport } from './api'
import type { ProjectSummary } from './contracts'
import './ProjectBrowser.css'

type ProjectBrowserProps = {
  onOpen: (projectId: string) => void
  onClose?: () => void
}

type SortOrder = 'updated_desc' | 'updated_asc' | 'name_asc'

const STATUS_LABELS: Record<ProjectSummary['status'], string> = {
  archived: 'Archived',
  draft: 'Draft',
  preview_processing: 'Previewing',
  preview_ready: 'Preview ready',
  needs_attention: 'Needs attention',
}

const VALIDATION_LABELS: Record<ProjectSummary['validation'], string> = {
  not_requested: 'Not validated',
  processing: 'Validating',
  validated: 'Validated',
  failed: 'Validation failed',
}

function formatDimension(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

export function ProjectBrowser({ onOpen, onClose }: ProjectBrowserProps) {
  const [items, setItems] = useState<ProjectSummary[]>([])
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<SortOrder>('updated_desc')
  const [showArchived, setShowArchived] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [workingId, setWorkingId] = useState<string | null>(null)
  const [bundleImporting, setBundleImporting] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [bundleResult, setBundleResult] = useState<ProjectBundleImport | null>(
    null,
  )
  const searchRef = useRef<HTMLInputElement>(null)
  const bundleInputRef = useRef<HTMLInputElement>(null)
  const bundleImportButtonRef = useRef<HTMLButtonElement>(null)
  const browserRef = useRef<HTMLElement>(null)
  const importDialogRef = useRef<HTMLElement>(null)
  const keepBrowsingRef = useRef<HTMLButtonElement>(null)
  const operationControllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetchProjects(true, controller.signal)
      .then((result) => {
        setItems(result.items)
        setError(null)
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') {
          setError(
            reason instanceof Error
              ? reason.message
              : 'Could not load saved projects.',
          )
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    searchRef.current?.focus()
    return () => operationControllerRef.current?.abort()
  }, [])

  useEffect(() => {
    if (!bundleResult) return
    const background = browserRef.current
    const importButton = bundleImportButtonRef.current
    const previousOverflow = document.body.style.overflow
    background?.setAttribute('inert', '')
    document.body.style.overflow = 'hidden'
    queueMicrotask(() => keepBrowsingRef.current?.focus())
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setBundleResult(null)
        return
      }
      if (event.key !== 'Tab') return
      const focusable = [
        ...(importDialogRef.current?.querySelectorAll<HTMLElement>('button') ??
          []),
      ].filter((element) => !element.hasAttribute('disabled'))
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable.at(-1) ?? first
      if (
        event.shiftKey &&
        (document.activeElement === first ||
          !importDialogRef.current?.contains(document.activeElement))
      ) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', keydown, true)
    return () => {
      document.removeEventListener('keydown', keydown, true)
      background?.removeAttribute('inert')
      document.body.style.overflow = previousOverflow
      queueMicrotask(() => importButton?.focus())
    }
  }, [bundleResult])

  const startOperation = () => {
    operationControllerRef.current?.abort()
    const controller = new AbortController()
    operationControllerRef.current = controller
    return controller
  }

  const visible = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase()
    const filtered = items.filter((item) => {
      if ((item.project.archived_at !== null) !== showArchived) return false
      if (!normalizedQuery) return true
      return `${item.project.name} ${item.source_filename}`
        .toLocaleLowerCase()
        .includes(normalizedQuery)
    })
    return [...filtered].sort((left, right) => {
      if (sort === 'name_asc')
        return left.project.name.localeCompare(right.project.name)
      const comparison = left.project.updated_at.localeCompare(
        right.project.updated_at,
      )
      return sort === 'updated_asc' ? comparison : -comparison
    })
  }, [items, query, showArchived, sort])

  const changeArchiveState = async (
    item: ProjectSummary,
    archived: boolean,
  ) => {
    setWorkingId(item.project.id)
    setError(null)
    try {
      const updated = await setProjectArchived(item.project.id, archived)
      setItems((current) =>
        current.map((entry) =>
          entry.project.id === updated.project.id ? updated : entry,
        ),
      )
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : 'Could not update the project.',
      )
    } finally {
      setWorkingId(null)
    }
  }

  const exportBundle = async (item: ProjectSummary) => {
    const controller = startOperation()
    setWorkingId(item.project.id)
    setError(null)
    setNotice(null)
    try {
      const result = await fetchProjectBundle(
        item.project.id,
        'full_history',
        controller.signal,
      )
      const url = URL.createObjectURL(result.blob)
      const link = document.createElement('a')
      link.href = url
      link.download = result.filename
      link.rel = 'noopener'
      document.body.append(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 0)
      setNotice(`Exported ${item.project.name} with full Undo/Redo history.`)
    } catch (reason) {
      if ((reason as Error).name !== 'AbortError') {
        setError(
          reason instanceof Error
            ? reason.message
            : 'Could not export the project bundle.',
        )
      }
    } finally {
      if (operationControllerRef.current === controller) {
        operationControllerRef.current = null
        if (!controller.signal.aborted) setWorkingId(null)
      }
    }
  }

  const importBundle = async (file: File) => {
    const controller = startOperation()
    setBundleImporting(true)
    setError(null)
    setNotice(null)
    try {
      const result = await importProjectBundle(file, controller.signal)
      setBundleResult(result)
    } catch (reason) {
      if ((reason as Error).name !== 'AbortError') {
        setError(
          reason instanceof Error
            ? reason.message
            : 'Could not import the project bundle.',
        )
      }
    } finally {
      if (operationControllerRef.current === controller) {
        operationControllerRef.current = null
        if (!controller.signal.aborted) setBundleImporting(false)
        if (bundleInputRef.current) bundleInputRef.current.value = ''
      }
    }
  }

  return (
    <>
      <section
        ref={browserRef}
        className="project-browser"
        aria-labelledby="project-browser-heading"
      >
        <div className="project-browser-heading-row">
          <div>
            <span className="eyebrow">Local project library</span>
            <h1 id="project-browser-heading">Your recent work</h1>
            <p>Search, reopen, and organize projects stored on this machine.</p>
          </div>
          <div className="project-browser-heading-actions">
            <input
              ref={bundleInputRef}
              className="visually-hidden"
              type="file"
              accept=".image23mf,application/vnd.image23mf.project+zip,application/zip"
              aria-label="Choose project bundle"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void importBundle(file)
              }}
            />
            <button
              ref={bundleImportButtonRef}
              className="button button-secondary"
              type="button"
              disabled={bundleImporting}
              onClick={() => bundleInputRef.current?.click()}
            >
              {bundleImporting ? 'Importing bundle…' : 'Import project bundle'}
            </button>
            {onClose ? (
              <button
                className="button button-secondary"
                type="button"
                onClick={onClose}
              >
                Back to editor
              </button>
            ) : null}
          </div>
        </div>

        <div className="project-browser-toolbar" role="search">
          <label>
            <span>Search projects</span>
            <input
              ref={searchRef}
              type="search"
              value={query}
              placeholder="Name or source file"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <label>
            <span>Sort</span>
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as SortOrder)}
            >
              <option value="updated_desc">Recently updated</option>
              <option value="updated_asc">Oldest updated</option>
              <option value="name_asc">Name A–Z</option>
            </select>
          </label>
          <label className="project-browser-archive-toggle">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(event) => setShowArchived(event.target.checked)}
            />
            Show archived
          </label>
        </div>

        {error ? (
          <div className="project-browser-error" role="alert">
            {error}
          </div>
        ) : null}
        {notice ? (
          <div className="project-browser-notice" role="status">
            {notice}
          </div>
        ) : null}
        {loading ? (
          <p className="project-browser-state" role="status">
            Loading saved projects…
          </p>
        ) : null}
        {!loading && visible.length === 0 ? (
          <div className="project-browser-empty">
            <strong>
              {query
                ? 'No projects match that search.'
                : showArchived
                  ? 'No archived projects.'
                  : 'No saved projects yet.'}
            </strong>
            <p>
              {query
                ? 'Try another name or source filename.'
                : showArchived
                  ? 'Archived projects remain available here when you need them.'
                  : 'Choose an image above to create your first local project.'}
            </p>
          </div>
        ) : null}

        <ul
          className="project-grid"
          aria-label={showArchived ? 'Archived projects' : 'Active projects'}
        >
          {visible.map((item) => (
            <li
              className="project-card"
              data-project-id={item.project.id}
              key={item.project.id}
            >
              <img
                src={item.thumbnail_url}
                alt=""
                data-thumbnail-kind={item.thumbnail_kind}
              />
              <div className="project-card-body">
                <div className="project-card-title-row">
                  <div>
                    <h2>{item.project.name}</h2>
                    <small>{item.source_filename}</small>
                  </div>
                  <span data-status={item.status}>
                    {STATUS_LABELS[item.status]}
                  </span>
                </div>
                <dl>
                  <div>
                    <dt>Canvas</dt>
                    <dd>
                      {formatDimension(item.canvas_width_mm)} ×{' '}
                      {formatDimension(item.canvas_height_mm)} mm
                    </dd>
                  </div>
                  <div>
                    <dt>Source</dt>
                    <dd>
                      {item.source_width_px} × {item.source_height_px} px
                    </dd>
                  </div>
                  <div>
                    <dt>Colors</dt>
                    <dd>{item.color_count}</dd>
                  </div>
                  <div>
                    <dt>Revision</dt>
                    <dd>{item.current_revision_label ?? 'Draft only'}</dd>
                  </div>
                </dl>
                <div className="project-card-footer">
                  <span data-validation={item.validation}>
                    {VALIDATION_LABELS[item.validation]}
                  </span>
                  <div>
                    {showArchived ? (
                      <button
                        className="button button-secondary"
                        type="button"
                        disabled={workingId === item.project.id}
                        onClick={() => void changeArchiveState(item, false)}
                      >
                        {workingId === item.project.id
                          ? 'Restoring…'
                          : 'Restore'}
                      </button>
                    ) : (
                      <>
                        <button
                          className="button button-secondary"
                          type="button"
                          disabled={workingId === item.project.id}
                          onClick={() => void changeArchiveState(item, true)}
                        >
                          {workingId === item.project.id
                            ? 'Archiving…'
                            : 'Archive'}
                        </button>
                        <button
                          className="button button-secondary"
                          type="button"
                          disabled={workingId === item.project.id}
                          aria-label={`Export ${item.project.name} with full history`}
                          onClick={() => void exportBundle(item)}
                        >
                          {workingId === item.project.id
                            ? 'Working…'
                            : 'Export bundle'}
                        </button>
                        <button
                          className="button button-primary"
                          type="button"
                          onClick={() => onOpen(item.project.id)}
                        >
                          Open project
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>
            </li>
          ))}
        </ul>
      </section>
      {bundleResult
        ? createPortal(
            <div className="project-browser-import-backdrop">
              <section
                ref={importDialogRef}
                className="project-browser-import-result"
                role="dialog"
                aria-modal="true"
                aria-labelledby="bundle-import-heading"
              >
                <span className="eyebrow">Import complete</span>
                <h2 id="bundle-import-heading">Project bundle is ready</h2>
                <p>
                  {bundleResult.history_import === 'full_history'
                    ? 'The complete Undo/Redo graph was restored, including abandoned descendants retained for audit.'
                    : 'This older or current-state-only bundle starts from a new local history anchor.'}
                </p>
                <dl>
                  <div>
                    <dt>History</dt>
                    <dd>
                      {bundleResult.history_import === 'full_history'
                        ? 'Full history'
                        : 'Local anchor'}
                    </dd>
                  </div>
                  <div>
                    <dt>Imported nodes</dt>
                    <dd>{bundleResult.imported_history_nodes}</dd>
                  </div>
                  <div>
                    <dt>Abandoned audit nodes</dt>
                    <dd>{bundleResult.abandoned_history_nodes}</dd>
                  </div>
                  <div>
                    <dt>Restore</dt>
                    <dd>
                      {bundleResult.duplicate
                        ? 'Already imported'
                        : 'New project copy'}
                    </dd>
                  </div>
                </dl>
                <div className="project-browser-import-actions">
                  <button
                    ref={keepBrowsingRef}
                    className="button button-secondary"
                    type="button"
                    onClick={() => setBundleResult(null)}
                  >
                    Keep browsing
                  </button>
                  <button
                    className="button button-primary"
                    type="button"
                    onClick={() => onOpen(bundleResult.project_id)}
                  >
                    Open imported project
                  </button>
                </div>
              </section>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
