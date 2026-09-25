import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { fetchCapabilities } from '../../api'
import type { ArtifactResource } from '../../contracts'
import {
  deleteArtifact,
  inspectArtifact,
  revealArtifact,
  type ArtifactDeletion,
  type ArtifactInspection,
} from './artifactClient'
import './RevisionArtifactPanel.css'

type RevisionArtifactPanelProps = {
  projectId: string
  artifacts: ArtifactResource[]
  onDeleted?: (artifact: ArtifactResource, result: ArtifactDeletion) => void
  title?: string
  ariaLabel?: string
  emptyMessage?: string
  autoInspect?: boolean
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`
}

function artifactLabel(kind: string): string {
  return kind.replaceAll('-', ' ').replaceAll('_', ' ')
}

export function RevisionArtifactPanel({
  projectId,
  artifacts,
  onDeleted,
  title = 'Artifacts',
  ariaLabel = 'Revision artifacts',
  emptyMessage = 'This snapshot was published without retained preview evidence.',
  autoInspect = true,
}: RevisionArtifactPanelProps) {
  const hasRevealCandidate = artifacts.some((artifact) => artifact.kind === 'bambu-3mf')
  const [finderRevealAvailable, setFinderRevealAvailable] = useState<boolean | null>(
    hasRevealCandidate ? null : false,
  )

  useEffect(() => {
    if (!hasRevealCandidate) return
    const controller = new AbortController()
    fetchCapabilities(controller.signal)
      .then((value) => setFinderRevealAvailable(value.features.finder_reveal === true))
      .catch((error: unknown) => {
        if ((error as Error).name !== 'AbortError') setFinderRevealAvailable(false)
      })
    return () => controller.abort()
  }, [hasRevealCandidate])

  if (!artifacts.length) {
    return (
      <section className="revision-artifacts" aria-label={ariaLabel}>
        <div className="revision-artifacts-heading"><h4>{title}</h4><span>None retained</span></div>
        <p className="revision-artifact-empty">{emptyMessage}</p>
      </section>
    )
  }
  return (
    <section className="revision-artifacts" aria-label={ariaLabel}>
      <div className="revision-artifacts-heading"><h4>{title}</h4><span>{artifacts.length} retained</span></div>
      <ul>
        {artifacts.map((artifact) => (
          <ArtifactRow
            key={`${projectId}:${artifact.id}`}
            projectId={projectId}
            artifact={artifact}
            onDeleted={onDeleted}
            autoInspect={autoInspect}
            finderRevealAvailable={finderRevealAvailable}
          />
        ))}
      </ul>
    </section>
  )
}

function ArtifactRow({
  projectId,
  artifact,
  onDeleted,
  autoInspect,
  finderRevealAvailable,
}: {
  projectId: string
  artifact: ArtifactResource
  onDeleted?: RevisionArtifactPanelProps['onDeleted']
  autoInspect: boolean
  finderRevealAvailable: boolean | null
}) {
  const [inspection, setInspection] = useState<ArtifactInspection | null>(null)
  const [working, setWorking] = useState<'inspect' | 'reveal' | 'delete' | null>(
    autoInspect ? 'inspect' : null,
  )
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [deleted, setDeleted] = useState(false)
  const deleteButtonRef = useRef<HTMLButtonElement>(null)
  const cancelDeleteRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!autoInspect) return
    const controller = new AbortController()
    inspectArtifact(projectId, artifact.id, controller.signal)
      .then((value) => setInspection(value))
      .catch((error: unknown) => {
        if ((error as Error).name !== 'AbortError') {
          setMessage(error instanceof Error ? error.message : 'Integrity check failed.')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setWorking(null)
      })
    return () => controller.abort()
  }, [artifact.id, autoInspect, projectId])

  if (deleted) return null

  const check = async () => {
    setWorking('inspect')
    setMessage(null)
    try {
      setInspection(await inspectArtifact(projectId, artifact.id))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Integrity check failed.')
    } finally {
      setWorking(null)
    }
  }

  const reveal = async () => {
    setWorking('reveal')
    setMessage(null)
    try {
      const result = await revealArtifact(projectId, artifact.id)
      setMessage(result.revealed ? 'Revealed in Finder.' : result.reason ?? 'Finder reveal is unavailable.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Finder reveal failed.')
    } finally {
      setWorking(null)
    }
  }

  const remove = async () => {
    setWorking('delete')
    setMessage(null)
    try {
      const result = await deleteArtifact(projectId, artifact.id)
      setDeleted(true)
      onDeleted?.(artifact, result)
    } catch (error) {
      setConfirmDelete(false)
      setMessage(error instanceof Error ? error.message : 'Artifact cleanup failed.')
    } finally {
      setWorking(null)
    }
  }

  const cancelDelete = () => {
    setConfirmDelete(false)
    queueMicrotask(() => deleteButtonRef.current?.focus())
  }

  const requestDelete = () => {
    setConfirmDelete(true)
    queueMicrotask(() => cancelDeleteRef.current?.focus())
  }

  const handleDeleteKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      cancelDelete()
    }
  }

  return (
    <li className="revision-artifact" data-integrity={inspection?.integrity ?? 'unchecked'}>
      <div className="revision-artifact-title">
        <div><strong>{artifactLabel(artifact.kind)}</strong><small>{formatBytes(artifact.byte_size)}</small></div>
        {inspection ? <span>{inspection.integrity}</span> : <span>unchecked</span>}
      </div>
      <div className="revision-artifact-actions">
        <a href={artifact.download_url} download>Download</a>
        <button type="button" disabled={working !== null} onClick={() => void check()}>
          {working === 'inspect' ? 'Checking…' : inspection ? 'Check again' : 'Check integrity'}
        </button>
        {inspection?.revealable && finderRevealAvailable ? (
          <button type="button" disabled={working !== null || inspection.integrity !== 'verified'} onClick={() => void reveal()}>
            {working === 'reveal' ? 'Revealing…' : 'Reveal in Finder'}
          </button>
        ) : null}
        {inspection?.revealable && finderRevealAvailable === false ? (
          <span className="revision-artifact-unavailable">Finder reveal unavailable here</span>
        ) : null}
        {inspection?.regenerable && !inspection.immutable ? (
          <button ref={deleteButtonRef} type="button" className="revision-artifact-delete" disabled={working !== null} onClick={requestDelete}>
            Delete cached artifact
          </button>
        ) : null}
      </div>
      {inspection?.immutable ? <p>Locked with this published revision.</p> : null}
      {message ? <p role="status">{message}</p> : null}
      {confirmDelete ? (
        <div
          className="revision-artifact-confirm"
          role="group"
          aria-label={`Confirm deletion of ${artifactLabel(artifact.kind)}`}
        >
          <strong>Delete this regenerable artifact?</strong>
          <p>{inspection?.recovery_action ?? 'Run the originating job again to recreate it.'}</p>
          <div>
            <button ref={cancelDeleteRef} type="button" disabled={working !== null} onClick={cancelDelete} onKeyDown={handleDeleteKeyDown}>Keep artifact</button>
            <button type="button" disabled={working !== null} onClick={() => void remove()} onKeyDown={handleDeleteKeyDown}>
              {working === 'delete' ? 'Deleting…' : 'Delete artifact'}
            </button>
          </div>
        </div>
      ) : null}
    </li>
  )
}
