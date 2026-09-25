import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react'
import { ApiRequestError } from '../api'
import {
  acceptCalibrationProposal,
  attestCalibrationDraft,
  createCalibrationDraft,
  createCalibrationProposal,
  fetchCalibrationCatalogs,
  fetchCalibrationDrafts,
  fetchCalibrationDraft,
  fetchCalibrationProposals,
  fetchCalibrationRuns,
  fetchCalibrationSpecimen,
  finalizeCalibrationDraft,
  removeCalibrationDraftMember,
  rejectCalibrationProposal,
  updateCalibrationDraft,
  uploadCalibrationDraftMember,
} from './api'
import type {
  CalibrationCatalogCollection,
  CalibrationDraft,
  CalibrationDraftMetadata,
  CalibrationEvidenceRole,
  CalibrationFeature,
  CalibrationFeatureKind,
  CalibrationObservation,
  CalibrationProposal,
  CalibrationRun,
  CalibrationRunRecord,
  CalibrationSpecimen,
  PrintabilityProfile,
} from './contracts'
import {
  draftEvidencePresentation,
  draftProgress,
  canonicalProposalRunIds,
  evidenceStatusPresentation,
} from './model'
import { CalibrationProposalReview } from './CalibrationProposalReview'
import './CalibrationWorkspace.css'

type CalibrationWorkspaceProps = {
  onClose: () => void
}

type LoadState = 'loading' | 'ready' | 'error'
type DraftTab = 'setup' | 'observations' | 'attachments' | 'seal'

const HASH_PATTERN = /^[0-9a-f]{64}$/
const ATTACHMENTS: {
  role: CalibrationEvidenceRole
  label: string
  accept: string
  help: string
}[] = [
  {
    role: 'project_3mf',
    label: 'Slicer project (.3mf)',
    accept: '.3mf,model/3mf,application/vnd.ms-package.3dmanufacturing-3dmodel+xml',
    help: 'The exact project used for this print.',
  },
  {
    role: 'sliced_gcode',
    label: 'Sliced toolpath (.gcode)',
    accept: '.gcode,.gco,text/x-gcode,application/octet-stream',
    help: 'The exact G-code sent to the printer.',
  },
  {
    role: 'slicer_settings',
    label: 'Exported slicer settings',
    accept: '.json,.ini,.config,application/json,text/plain',
    help: 'A machine-readable settings export.',
  },
  {
    role: 'photo',
    label: 'Physical print photo',
    accept: '.png,.jpg,.jpeg,image/png,image/jpeg',
    help: 'At least one clear photo of the print. Add more after the first.',
  },
]

const METADATA_FIELDS: {
  key: keyof CalibrationDraftMetadata
  label: string
  placeholder?: string
  hash?: boolean
  optional?: boolean
}[] = [
  { key: 'slicer_application', label: 'Slicer application', placeholder: 'Bambu Studio' },
  { key: 'slicer_version', label: 'Slicer version', placeholder: '2.3.1' },
  { key: 'slicer_executable_sha256', label: 'Slicer executable SHA-256', hash: true },
  { key: 'machine_profile_name', label: 'Machine profile name' },
  { key: 'machine_profile_sha256', label: 'Machine profile SHA-256', hash: true },
  { key: 'process_profile_name', label: 'Process profile name' },
  { key: 'process_profile_sha256', label: 'Process profile SHA-256', hash: true },
  { key: 'filament_profile_name', label: 'Filament profile name' },
  { key: 'filament_profile_sha256', label: 'Filament profile SHA-256', hash: true },
  { key: 'filament_id', label: 'Local filament ID', optional: true },
  { key: 'filament_manufacturer', label: 'Filament manufacturer' },
  { key: 'filament_family', label: 'Filament family', optional: true },
  { key: 'filament_name', label: 'Filament name' },
  { key: 'filament_material', label: 'Filament material', placeholder: 'pla' },
  { key: 'filament_finish', label: 'Filament finish', optional: true },
  { key: 'filament_color_hex', label: 'Filament color', placeholder: '#000000' },
]

function shortHash(value: string | null | undefined) {
  return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : '—'
}

function bytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function displayDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
    new Date(value),
  )
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : 'The local calibration service could not respond.'
}

function profileStatus(profile: PrintabilityProfile) {
  return evidenceStatusPresentation(profile.evidence_status)
}

export function CalibrationWorkspace({ onClose }: CalibrationWorkspaceProps) {
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [catalogs, setCatalogs] = useState<CalibrationCatalogCollection | null>(null)
  const [drafts, setDrafts] = useState<CalibrationDraft[]>([])
  const [runs, setRuns] = useState<CalibrationRun[]>([])
  const [proposals, setProposals] = useState<CalibrationProposal[]>([])
  const [selectedDraft, setSelectedDraft] = useState<CalibrationDraft | null>(null)
  const [selectedProposal, setSelectedProposal] = useState<CalibrationProposal | null>(null)
  const [specimen, setSpecimen] = useState<CalibrationSpecimen | null>(null)
  const [record, setRecord] = useState<CalibrationRunRecord | null>(null)
  const [metadata, setMetadata] = useState<CalibrationDraftMetadata | null>(null)
  const [draftTab, setDraftTab] = useState<DraftTab>('setup')
  const [working, setWorking] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const [attestationConfirmed, setAttestationConfirmed] = useState(false)
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([])
  const headingRef = useRef<HTMLHeadingElement>(null)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [catalogResult, draftResult, runResult, proposalResult] = await Promise.all([
      fetchCalibrationCatalogs(signal),
      fetchCalibrationDrafts(signal),
      fetchCalibrationRuns(undefined, signal),
      fetchCalibrationProposals(undefined, signal),
    ])
    setCatalogs(catalogResult)
    setDrafts(draftResult.items)
    setRuns(runResult.items)
    setProposals(proposalResult.items)
    return { draftResult, proposalResult }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([
      fetchCalibrationCatalogs(controller.signal),
      fetchCalibrationDrafts(controller.signal),
      fetchCalibrationRuns(undefined, controller.signal),
      fetchCalibrationProposals(undefined, controller.signal),
    ])
      .then(([catalogResult, draftResult, runResult, proposalResult]) => {
        setCatalogs(catalogResult)
        setDrafts(draftResult.items)
        setRuns(runResult.items)
        setProposals(proposalResult.items)
        setLoadState('ready')
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') {
          setError(errorMessage(reason))
          setLoadState('error')
        }
      })
    queueMicrotask(() => headingRef.current?.focus())
    return () => controller.abort()
  }, [])

  const activeCatalog = catalogs?.items.find((item) => item.active) ?? null
  const activeProfiles = activeCatalog?.catalog.profiles ?? []
  const progress = selectedDraft ? draftProgress(selectedDraft) : null

  const replaceDraft = (next: CalibrationDraft, message: string) => {
    setSelectedDraft(next)
    setRecord(next.record)
    setMetadata(next.metadata)
    setAttestationConfirmed(false)
    setDrafts((current) => [next, ...current.filter((item) => item.id !== next.id)])
    setAnnouncement(message)
  }

  const runAction = async (label: string, action: () => Promise<void>) => {
    setWorking(label)
    setError(null)
    try {
      await action()
    } catch (reason) {
      if (reason instanceof ApiRequestError && reason.status === 409 && selectedDraft) {
        try {
          const recovered = await fetchCalibrationDraft(selectedDraft.id)
          replaceDraft(recovered, 'Reloaded the newer retained calibration draft.')
          setError('This draft changed in another request. The latest retained generation was reloaded; review it before retrying.')
        } catch (recoveryError) {
          setError(`${errorMessage(reason)} Recovery also failed: ${errorMessage(recoveryError)}`)
        }
      } else {
        setError(errorMessage(reason))
      }
    } finally {
      setWorking(null)
    }
  }

  const startRun = (profile: PrintabilityProfile) => {
    if (!activeCatalog) return
    void runAction('start', async () => {
      const [created, generated] = await Promise.all([
        createCalibrationDraft({
          profile_id: profile.id,
          expected_catalog_fingerprint: activeCatalog.fingerprint,
        }),
        fetchCalibrationSpecimen(profile.id),
      ])
      setSpecimen(generated)
      replaceDraft(created, `Started physical run for ${profile.display_name}.`)
      setDraftTab('setup')
    })
  }

  const deriveProposal = () => {
    if (!activeCatalog) return
    const runIds = canonicalProposalRunIds(selectedRunIds)
    const selectedRuns = runs.filter((run) => runIds.includes(run.id))
    const profileId = selectedRuns[0]?.profile_id
    if (!profileId || selectedRuns.some((run) => run.profile_id !== profileId)) {
      setError('Select one or more verified runs from the same calibration profile.')
      return
    }
    void runAction('derive-proposal', async () => {
      const proposal = await createCalibrationProposal({
        profile_id: profileId,
        run_ids: runIds,
        expected_catalog_fingerprint: activeCatalog.fingerprint,
      })
      setProposals((current) => [
        proposal,
        ...current.filter((item) => item.proposal.id !== proposal.proposal.id),
      ])
      setSelectedProposal(proposal)
      setSelectedRunIds([])
      setAnnouncement('Created a deterministic catalog proposal for explicit review.')
    })
  }

  const openDraft = (draft: CalibrationDraft) => {
    setSelectedDraft(draft)
    setRecord(draft.record)
    setMetadata(draft.metadata)
    setAttestationConfirmed(false)
    setSelectedProposal(null)
    setDraftTab('setup')
    setError(null)
    setSpecimen(null)
  }

  const saveDraft = async () => {
    if (!selectedDraft || !record || !metadata) return
    await runAction('save', async () => {
      const saved = await updateCalibrationDraft(selectedDraft.id, {
        expected_generation: selectedDraft.generation,
        record,
        metadata,
      })
      replaceDraft(saved, 'Calibration draft saved. Any previous attestation was cleared.')
    })
  }

  const upload = (role: CalibrationEvidenceRole, event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!selectedDraft || !file) return
    const ordinal =
      role === 'photo'
        ? selectedDraft.members.filter((member) => member.role === 'photo').length
        : 0
    void runAction(`upload-${role}`, async () => {
      const updated = await uploadCalibrationDraftMember(
        selectedDraft.id,
        role,
        ordinal,
        selectedDraft.generation,
        file,
      )
      replaceDraft(updated, `${file.name} staged and hash-verified.`)
    })
  }

  const removeMember = (role: CalibrationEvidenceRole, ordinal: number) => {
    if (!selectedDraft) return
    void runAction(`remove-${role}-${ordinal}`, async () => {
      const updated = await removeCalibrationDraftMember(
        selectedDraft.id,
        role,
        ordinal,
        selectedDraft.generation,
      )
      replaceDraft(updated, 'Attachment removed. Any previous attestation was cleared.')
    })
  }

  const attest = () => {
    if (!selectedDraft || !attestationConfirmed) return
    void runAction('attest', async () => {
      const attested = await attestCalibrationDraft(selectedDraft.id, {
        expected_generation: selectedDraft.generation,
        confirmed: true,
      })
      replaceDraft(attested, 'Physical observation attested and bound to this candidate hash.')
      setAttestationConfirmed(false)
    })
  }

  const finalize = () => {
    if (!selectedDraft) return
    void runAction('finalize', async () => {
      const result = await finalizeCalibrationDraft(selectedDraft.id, selectedDraft.generation)
      replaceDraft(result.draft, `Sealed immutable run ${result.run.id}.`)
      const refreshed = await refresh()
      const retained = refreshed.draftResult.items.find((item) => item.id === selectedDraft.id)
      if (retained) setSelectedDraft(retained)
    })
  }

  const updateRecord = <K extends keyof CalibrationRunRecord>(
    key: K,
    value: CalibrationRunRecord[K],
  ) => setRecord((current) => (current ? { ...current, [key]: value } : current))

  const updateObservation = (
    featureId: string,
    change: Partial<CalibrationObservation>,
  ) => setRecord((current) => current ? {
    ...current,
    observations: current.observations.map((item) =>
      item.feature_id === featureId ? { ...item, ...change } : item,
    ),
  } : current)

  const updateMetadata = (key: keyof CalibrationDraftMetadata, value: string) => {
    setMetadata((current) => current ? { ...current, [key]: value || null } : current)
  }

  const useSuggestedRunId = () => {
    if (!selectedDraft) return
    const date = new Date().toISOString().slice(0, 10).replaceAll('-', '')
    updateRecord('record_id', `calibration-run-${date}-${selectedDraft.id.slice(-8)}`)
  }

  if (loadState === 'loading') {
    return <section className="calibration-loading" role="status">Loading calibration authority…</section>
  }

  return (
    <section className="calibration-workspace" aria-labelledby="calibration-heading">
      <header className="calibration-hero">
        <div>
          <span className="eyebrow">Physical evidence lab</span>
          <h1 id="calibration-heading" ref={headingRef} tabIndex={-1}>Calibration review center</h1>
          <p>
            Generate a known specimen, retain the exact print setup, score what you physically
            observe, and promote defaults only after an explicit evidence review.
          </p>
        </div>
        <button className="button button-secondary" type="button" onClick={onClose}>
          Close calibration
        </button>
      </header>

      <div className="visually-hidden" aria-live="polite">{announcement}</div>
      {error ? (
        <div className="notice notice-error calibration-error" role="alert">
          <div><strong>Calibration action needs attention</strong><p>{error}</p></div>
          <button type="button" aria-label="Dismiss calibration error" onClick={() => setError(null)}>×</button>
        </div>
      ) : null}

      {!selectedDraft && !selectedProposal ? (
        <CalibrationDashboard
          activeCatalog={activeCatalog}
          profiles={activeProfiles}
          drafts={drafts}
          runs={runs}
          proposals={proposals}
          selectedRunIds={selectedRunIds}
          working={working}
          onStart={startRun}
          onOpenDraft={openDraft}
          onOpenProposal={(proposal) => setSelectedProposal(proposal)}
          onSelectRun={(runId, selected) => setSelectedRunIds((current) =>
            selected ? canonicalProposalRunIds([...current, runId]) : current.filter((id) => id !== runId),
          )}
          onDeriveProposal={deriveProposal}
        />
      ) : null}

      {selectedProposal ? (
        <div className="calibration-detail">
          <button className="calibration-back" type="button" onClick={() => setSelectedProposal(null)}>
            ← Back to calibration dashboard
          </button>
          <CalibrationProposalReview
            resource={selectedProposal}
            catalog={{ active_fingerprint: catalogs?.active_fingerprint ?? '' }}
            onAccept={async (request) => {
              const result = await acceptCalibrationProposal(request.proposal_id, {
                expected_catalog_fingerprint: request.expected_catalog_fingerprint,
                reviewer: request.reviewer,
                reason: request.reason,
              })
              setCatalogs((current) => current ? {
                ...current,
                active_fingerprint: result.active_catalog.fingerprint,
                items: [
                  result.active_catalog,
                  ...current.items
                    .filter((item) => item.fingerprint !== result.active_catalog.fingerprint)
                    .map((item) => ({ ...item, active: false })),
                ],
              } : current)
              setSelectedProposal(result.proposal)
              setProposals((current) => current.map((item) =>
                item.proposal.id === result.proposal.proposal.id ? result.proposal : item,
              ))
              setAnnouncement('Catalog proposal accepted and the active catalog was promoted.')
            }}
            onReject={async (request) => {
              const rejected = await rejectCalibrationProposal(request.proposal_id, {
                reviewer: request.reviewer,
                reason: request.reason,
              })
              setSelectedProposal(rejected)
              setProposals((current) => current.map((item) =>
                item.proposal.id === rejected.proposal.id ? rejected : item,
              ))
              setAnnouncement('Catalog proposal rejected. Active defaults were not changed.')
            }}
          />
        </div>
      ) : null}

      {selectedDraft && record && metadata ? (
        <div className="calibration-detail">
          <button className="calibration-back" type="button" onClick={() => setSelectedDraft(null)}>
            ← Back to calibration dashboard
          </button>
          <header className="calibration-run-header">
            <div>
              <span className="eyebrow">Resumable physical run</span>
              <h2>{selectedDraft.profile.display_name}</h2>
              <p>
                Draft {selectedDraft.id} · generation {selectedDraft.generation} · catalog{' '}
                <code title={selectedDraft.catalog_fingerprint}>{shortHash(selectedDraft.catalog_fingerprint)}</code>
              </p>
            </div>
            <EvidenceBadge draft={selectedDraft} />
          </header>

          <div className="calibration-progress" aria-label="Calibration completion">
            <span><strong>{progress?.scored ?? 0}</strong> of {progress?.total ?? 0} observations scored</span>
            <div><i style={{ width: `${progress?.total ? (progress.scored / progress.total) * 100 : 0}%` }} /></div>
          </div>

          <nav className="calibration-tabs" aria-label="Calibration run steps">
            {([
              ['setup', '1. Setup'],
              ['observations', '2. Observations'],
              ['attachments', '3. Evidence files'],
              ['seal', '4. Review & seal'],
            ] as [DraftTab, string][]).map(([tab, label]) => (
              <button
                key={tab}
                type="button"
                aria-current={draftTab === tab ? 'step' : undefined}
                onClick={() => setDraftTab(tab)}
              >
                {label}
              </button>
            ))}
          </nav>

          {draftTab === 'setup' ? (
            <SetupStep
              draft={selectedDraft}
              specimen={specimen}
              record={record}
              metadata={metadata}
              working={working}
              onRecord={updateRecord}
              onMetadata={updateMetadata}
              onSuggestId={useSuggestedRunId}
              onSave={() => void saveDraft()}
            />
          ) : null}
          {draftTab === 'observations' ? (
            <ObservationStep
              draft={selectedDraft}
              record={record}
              working={working}
              onObservation={updateObservation}
              onSave={() => void saveDraft()}
            />
          ) : null}
          {draftTab === 'attachments' ? (
            <AttachmentStep
              draft={selectedDraft}
              working={working}
              onUpload={upload}
              onRemove={removeMember}
            />
          ) : null}
          {draftTab === 'seal' ? (
            <SealStep
              draft={selectedDraft}
              confirmed={attestationConfirmed}
              working={working}
              onConfirmed={setAttestationConfirmed}
              onAttest={attest}
              onFinalize={finalize}
            />
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

function CalibrationDashboard({
  activeCatalog,
  profiles,
  drafts,
  runs,
  proposals,
  selectedRunIds,
  working,
  onStart,
  onOpenDraft,
  onOpenProposal,
  onSelectRun,
  onDeriveProposal,
}: {
  activeCatalog: CalibrationCatalogCollection['items'][number] | null
  profiles: PrintabilityProfile[]
  drafts: CalibrationDraft[]
  runs: CalibrationRun[]
  proposals: CalibrationProposal[]
  selectedRunIds: string[]
  working: string | null
  onStart: (profile: PrintabilityProfile) => void
  onOpenDraft: (draft: CalibrationDraft) => void
  onOpenProposal: (proposal: CalibrationProposal) => void
  onSelectRun: (runId: string, selected: boolean) => void
  onDeriveProposal: () => void
}) {
  return (
    <div className="calibration-dashboard">
      <section className="calibration-authority-card" aria-labelledby="authority-heading">
        <div>
          <span className="eyebrow">Active authority</span>
          <h2 id="authority-heading">{activeCatalog?.catalog.catalog_version ?? 'Unavailable'}</h2>
          <p>{activeCatalog?.catalog.source.method}</p>
        </div>
        <dl>
          <div><dt>Catalog</dt><dd><code>{shortHash(activeCatalog?.fingerprint)}</code></dd></div>
          <div><dt>Profiles</dt><dd>{profiles.length}</dd></div>
          <div><dt>Verified runs</dt><dd>{runs.length}</dd></div>
          <div><dt>Open proposals</dt><dd>{proposals.filter((item) => item.state === 'pending').length}</dd></div>
        </dl>
      </section>

      <section className="calibration-section" aria-labelledby="profiles-heading">
        <div className="calibration-section-heading">
          <div><span className="eyebrow">Printer profiles</span><h2 id="profiles-heading">Start from a known specimen</h2></div>
          <p>Generated files are preparation only—not physical evidence.</p>
        </div>
        <div className="calibration-profile-grid">
          {profiles.map((profile) => {
            const status = profileStatus(profile)
            const profileRuns = runs.filter((run) => run.profile_id === profile.id)
            return (
              <article className="calibration-profile-card" key={profile.id}>
                <div className="calibration-card-topline">
                  <span className="calibration-status" data-tone={status.tone}>{status.label}</span>
                  <span>{profile.nozzle_diameter_mm} mm · {profile.material_class.toUpperCase()}</span>
                </div>
                <h3>{profile.display_name}</h3>
                <p>{status.description}</p>
                <dl>
                  <div><dt>Reviewed</dt><dd>{profile.reviewed_on}</dd></div>
                  <div><dt>Sealed runs</dt><dd>{profileRuns.length}</dd></div>
                  <div><dt>Samples</dt><dd>{profile.sweep.dot_diameters_mm.length * 5}</dd></div>
                </dl>
                <div className="calibration-card-actions">
                  <a className="button button-secondary" href={`/api/calibration/specimens/${encodeURIComponent(profile.id)}/bundle`} download>
                    Download specimen
                  </a>
                  <button className="button button-primary" type="button" disabled={working !== null} onClick={() => onStart(profile)}>
                    Start physical run
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      </section>

      <section className="calibration-section" aria-labelledby="drafts-heading">
        <div className="calibration-section-heading">
          <div><span className="eyebrow">Retained work</span><h2 id="drafts-heading">Calibration runs</h2></div>
          <p>Incomplete drafts survive application restarts and workspace restore.</p>
        </div>
        {drafts.length ? (
          <div className="calibration-list">
            {drafts.map((draft) => (
              <button type="button" className="calibration-list-row" key={draft.id} onClick={() => onOpenDraft(draft)}>
                <span><strong>{draft.profile.display_name}</strong><small>{draft.id}</small></span>
                <EvidenceBadge draft={draft} />
                <span><small>Updated</small>{displayDate(draft.updated_at)}</span>
                <span aria-hidden="true">→</span>
              </button>
            ))}
          </div>
        ) : <p className="calibration-empty">No retained calibration drafts yet.</p>}
      </section>

      <section className="calibration-section" aria-labelledby="runs-heading">
        <div className="calibration-section-heading">
          <div><span className="eyebrow">Sealed source evidence</span><h2 id="runs-heading">Verified physical runs</h2></div>
          <p>Select compatible runs to derive one deterministic before/after proposal.</p>
        </div>
        {runs.length ? (
          <div className="calibration-run-selection">
            {runs.map((run) => (
              <label key={run.id}>
                <input
                  type="checkbox"
                  checked={selectedRunIds.includes(run.id)}
                  onChange={(event) => onSelectRun(run.id, event.target.checked)}
                />
                <span><strong>{run.id}</strong><small>{run.profile_id} · imported {displayDate(run.imported_at)}</small></span>
                <span className="calibration-status" data-tone="positive">Verified physical run</span>
                <a href={run.source_bundle.download_url} onClick={(event) => event.stopPropagation()}>Evidence ZIP</a>
              </label>
            ))}
            <div className="calibration-step-actions">
              <button className="button button-primary" type="button" disabled={!selectedRunIds.length || working !== null} onClick={onDeriveProposal}>
                {working === 'derive-proposal' ? 'Deriving…' : `Derive proposal from ${selectedRunIds.length || 0} run${selectedRunIds.length === 1 ? '' : 's'}`}
              </button>
            </div>
          </div>
        ) : <p className="calibration-empty">No sealed physical runs yet.</p>}
      </section>

      <section className="calibration-section" aria-labelledby="proposals-heading">
        <div className="calibration-section-heading">
          <div><span className="eyebrow">Human review gate</span><h2 id="proposals-heading">Catalog proposals</h2></div>
          <p>Imported evidence never changes defaults by itself.</p>
        </div>
        {proposals.length ? (
          <div className="calibration-list">
            {proposals.map((proposal) => (
              <button type="button" className="calibration-list-row" key={proposal.proposal.id} onClick={() => onOpenProposal(proposal)}>
                <span><strong>{proposal.proposal.profile_id}</strong><small>{proposal.proposal.id}</small></span>
                <span className="calibration-status" data-tone={proposal.state === 'pending' ? 'warning' : 'positive'}>{proposal.state}</span>
                <span>{proposal.proposal.recommendation_changes.length} proposed changes</span>
                <span aria-hidden="true">→</span>
              </button>
            ))}
          </div>
        ) : <p className="calibration-empty">No catalog proposals. Seal physical evidence first.</p>}
      </section>
    </div>
  )
}

function EvidenceBadge({ draft }: { draft: CalibrationDraft }) {
  const status = draftEvidencePresentation(draft.readiness.evidence_state)
  return <span className="calibration-status" data-tone={status.tone}>{status.label}</span>
}

function SetupStep({
  draft,
  specimen,
  record,
  metadata,
  working,
  onRecord,
  onMetadata,
  onSuggestId,
  onSave,
}: {
  draft: CalibrationDraft
  specimen: CalibrationSpecimen | null
  record: CalibrationRunRecord
  metadata: CalibrationDraftMetadata
  working: string | null
  onRecord: <K extends keyof CalibrationRunRecord>(key: K, value: CalibrationRunRecord[K]) => void
  onMetadata: (key: keyof CalibrationDraftMetadata, value: string) => void
  onSuggestId: () => void
  onSave: () => void
}) {
  const couponPng = draft.members.find((member) => member.role === 'coupon_png')
  const couponSvg = draft.members.find((member) => member.role === 'coupon_svg')
  const matchingPreparationBundle = specimen?.catalog_fingerprint === draft.catalog_fingerprint
  return (
    <div className="calibration-step-grid">
      <section className="calibration-panel specimen-panel">
        <div className="calibration-panel-heading"><span className="eyebrow">Preparation artifact</span><h3>Print at 100% scale</h3></div>
        <div className="specimen-preview">
          <img src={couponPng?.download_url ?? specimen?.png_url} alt={`Calibration coupon for ${draft.profile.display_name}`} />
        </div>
        <p className="calibration-honesty"><strong>Not physically observed.</strong> This preview and its generated files do not validate a printer.</p>
        <div className="calibration-card-actions">
          <a className="button button-secondary" href={couponSvg?.download_url ?? specimen?.svg_url} download>SVG</a>
          <a className="button button-secondary" href={couponPng?.download_url ?? specimen?.png_url} download>PNG</a>
          {matchingPreparationBundle ? <a className="button button-primary" href={specimen.bundle_url} download>Preparation bundle</a> : null}
        </div>
        {!matchingPreparationBundle ? <small className="pinned-specimen-note">This reopened draft uses its hash-verified retained coupon files. The active catalog’s preparation bundle is intentionally not substituted.</small> : null}
      </section>
      <section className="calibration-panel">
        <div className="calibration-panel-heading"><span className="eyebrow">Printed setup</span><h3>Run identity</h3></div>
        <div className="calibration-form-grid">
          <label className="span-two">Stable run ID<span><input value={record.record_id ?? ''} onChange={(event) => onRecord('record_id', event.target.value || null)} placeholder="calibration-run-20260718-p2s-01" /><button type="button" onClick={onSuggestId}>Suggest</button></span></label>
          <label>Layer height (mm)<input type="number" min="0.01" max="2" step="0.01" value={record.layer_height_mm ?? ''} onChange={(event) => onRecord('layer_height_mm', event.target.value === '' ? null : Number(event.target.value))} /></label>
          <label>Build plate<input value={record.plate_id ?? ''} onChange={(event) => onRecord('plate_id', event.target.value || null)} placeholder="textured-pei" /></label>
          <label>Filament name<input value={record.filament ?? ''} onChange={(event) => onRecord('filament', event.target.value || null)} /></label>
          <label>Operator<input value={record.operator ?? ''} onChange={(event) => onRecord('operator', event.target.value || null)} /></label>
          <label>Printed on<input type="date" value={record.printed_on ?? ''} onChange={(event) => onRecord('printed_on', event.target.value || null)} /></label>
          <label>Process profile<input value={record.slicer_profile ?? ''} onChange={(event) => onRecord('slicer_profile', event.target.value || null)} /></label>
          <label className="span-two">Run notes<textarea value={record.notes} onChange={(event) => onRecord('notes', event.target.value)} rows={3} /></label>
        </div>
      </section>
      <section className="calibration-panel span-two">
        <div className="calibration-panel-heading"><span className="eyebrow">Immutable provenance</span><h3>Slicer, profiles, and filament snapshot</h3></div>
        <div className="calibration-form-grid calibration-metadata-grid">
          {METADATA_FIELDS.map((field) => {
            const value = metadata[field.key]
            const stringValue = typeof value === 'string' ? value : ''
            const invalidHash = field.hash && stringValue !== '' && !HASH_PATTERN.test(stringValue)
            return (
              <label key={field.key}>{field.label}{field.optional ? <small>Optional</small> : null}
                <input
                  value={stringValue}
                  placeholder={field.placeholder}
                  aria-invalid={invalidHash || undefined}
                  onChange={(event) => onMetadata(field.key, event.target.value)}
                />
                {invalidHash ? <small className="field-error">Use 64 lowercase hexadecimal characters.</small> : null}
              </label>
            )
          })}
        </div>
        <div className="calibration-step-actions">
          <button className="button button-primary" type="button" disabled={working !== null} onClick={onSave}>{working === 'save' ? 'Saving…' : 'Save setup'}</button>
        </div>
      </section>
    </div>
  )
}

function ObservationStep({ draft, record, working, onObservation, onSave }: {
  draft: CalibrationDraft
  record: CalibrationRunRecord
  working: string | null
  onObservation: (featureId: string, change: Partial<CalibrationObservation>) => void
  onSave: () => void
}) {
  const byKind = useMemo(
    () => draft.artifact.features.reduce<Record<CalibrationFeatureKind, CalibrationFeature[]>>(
      (groups, feature) => {
        ;(groups[feature.kind] ??= []).push(feature)
        return groups
      },
      { dot: [], hole: [], line: [], neck: [], gap: [] },
    ),
    [draft.artifact.features],
  )
  const observations = new Map(record.observations.map((item) => [item.feature_id, item]))
  return (
    <section className="calibration-panel calibration-observations">
      <div className="calibration-panel-heading"><span className="eyebrow">Physical inspection</span><h3>Score every feature</h3><p>Use uncertain when inspection is ambiguous. Failed features require a measurement; enter 0 when the feature vanished.</p></div>
      {(['dot', 'hole', 'line', 'neck', 'gap'] as const).map((kind) => (
        <fieldset key={kind}>
          <legend>{kind}s</legend>
          <div className="observation-table" role="group" aria-label={`${kind} observations`}>
            <div className="observation-row observation-head" aria-hidden="true"><span>Feature</span><span>Nominal</span><span>Outcome</span><span>Measured mm</span><span>Notes</span></div>
            {(byKind[kind] ?? []).map((feature) => {
              const observation = observations.get(feature.id)!
              return (
                <div className="observation-row" key={feature.id}>
                  <label><span>Feature</span><code>{feature.id}</code></label>
                  <span><small>Nominal</small>{feature.nominal_dimension_mm.toFixed(2)} mm</span>
                  <label><span>Outcome</span><select value={observation.outcome} onChange={(event) => onObservation(feature.id, { outcome: event.target.value as CalibrationObservation['outcome'] })}><option value="untested">Untested</option><option value="pass">Pass</option><option value="fail">Fail</option><option value="uncertain">Uncertain</option></select></label>
                  <label><span>Measured mm</span><input type="number" min="0" step="0.01" value={observation.measured_dimension_mm ?? ''} onChange={(event) => onObservation(feature.id, { measured_dimension_mm: event.target.value === '' ? null : Number(event.target.value) })} /></label>
                  <label><span>Notes</span><input value={observation.notes} onChange={(event) => onObservation(feature.id, { notes: event.target.value })} /></label>
                </div>
              )
            })}
          </div>
        </fieldset>
      ))}
      <div className="calibration-step-actions"><button className="button button-primary" type="button" disabled={working !== null} onClick={onSave}>{working === 'save' ? 'Saving…' : 'Save observations'}</button></div>
    </section>
  )
}

function AttachmentStep({ draft, working, onUpload, onRemove }: {
  draft: CalibrationDraft
  working: string | null
  onUpload: (role: CalibrationEvidenceRole, event: ChangeEvent<HTMLInputElement>) => void
  onRemove: (role: CalibrationEvidenceRole, ordinal: number) => void
}) {
  return (
    <section className="calibration-panel">
      <div className="calibration-panel-heading"><span className="eyebrow">Source evidence</span><h3>Attach the exact printable inputs and physical result</h3><p>Files are validated by role, stored by content hash, and rechecked before every read and seal.</p></div>
      <div className="attachment-grid">
        {ATTACHMENTS.map((attachment) => {
          const members = draft.members.filter((member) => member.role === attachment.role)
          return (
            <article className="attachment-card" key={attachment.role}>
              <div><h4>{attachment.label}</h4><p>{attachment.help}</p></div>
              {members.map((member) => (
                <div className="attachment-member" key={member.id}>
                  <span><a href={member.download_url}>{member.filename}</a><small>{bytes(member.byte_size)} · <code title={member.sha256}>{shortHash(member.sha256)}</code></small></span>
                  <button type="button" disabled={working !== null} onClick={() => onRemove(member.role, member.ordinal)}>Remove</button>
                </div>
              ))}
              <label className="button button-secondary attachment-upload">{members.length && attachment.role !== 'photo' ? 'Replace file' : 'Choose file'}<input type="file" accept={attachment.accept} onChange={(event) => onUpload(attachment.role, event)} /></label>
            </article>
          )
        })}
      </div>
    </section>
  )
}

function SealStep({ draft, confirmed, working, onConfirmed, onAttest, onFinalize }: {
  draft: CalibrationDraft
  confirmed: boolean
  working: string | null
  onConfirmed: (value: boolean) => void
  onAttest: () => void
  onFinalize: () => void
}) {
  const attestationBlocker = draft.readiness.blockers.find((item) => item.code === 'attestation_missing')
  const evidenceBlockers = draft.readiness.blockers.filter((item) => item.code !== 'attestation_missing')
  return (
    <div className="calibration-step-grid">
      <section className="calibration-panel">
        <div className="calibration-panel-heading"><span className="eyebrow">Candidate identity</span><h3>Review what will be sealed</h3></div>
        <dl className="hash-review">
          <div><dt>Candidate SHA-256</dt><dd><code>{draft.candidate_sha256}</code></dd></div>
          <div><dt>Catalog SHA-256</dt><dd><code>{draft.catalog_fingerprint}</code></dd></div>
          <div><dt>Artifact SHA-256</dt><dd><code>{draft.record.artifact_fingerprint}</code></dd></div>
          <div><dt>Attachments</dt><dd>{draft.members.length} verified members</dd></div>
        </dl>
      </section>
      <section className="calibration-panel">
        <div className="calibration-panel-heading"><span className="eyebrow">Readiness</span><h3>{evidenceBlockers.length ? `${evidenceBlockers.length} items need attention` : 'Evidence assembly is complete'}</h3></div>
        {evidenceBlockers.length ? <ul className="blocker-list">{evidenceBlockers.map((blocker) => <li key={`${blocker.code}-${blocker.field}`}><strong>{blocker.message}</strong><code>{blocker.field}</code></li>)}</ul> : <p className="calibration-success">All setup, observation, and attachment checks passed.</p>}
      </section>
      <section className="calibration-panel span-two attestation-panel">
        <div className="calibration-panel-heading"><span className="eyebrow">Human attestation</span><h3>Generated and sliced files are not enough</h3><p>Only confirm this after you personally inspect the physical print and finish the record.</p></div>
        {draft.attested_at ? (
          <div className="attested-proof"><strong>Attested {displayDate(draft.attested_at)}</strong><span>Bound candidate <code>{shortHash(draft.attested_candidate_sha256)}</code></span></div>
        ) : (
          <label className="attestation-check"><input aria-label="Confirm physical observation attestation" type="checkbox" checked={confirmed} disabled={!draft.readiness.ready_to_attest} onChange={(event) => onConfirmed(event.target.checked)} /><span><strong>{draft.attestation_statement}</strong><small>This confirmation is invalidated by any later change.</small></span></label>
        )}
        <div className="calibration-step-actions">
          {!draft.attested_at ? <button className="button button-secondary" type="button" disabled={!confirmed || !draft.readiness.ready_to_attest || working !== null} onClick={onAttest}>{working === 'attest' ? 'Attesting…' : 'Attest physical observation'}</button> : null}
          <button className="button button-primary" type="button" disabled={!draft.readiness.ready_to_finalize || working !== null || Boolean(attestationBlocker)} onClick={onFinalize}>{working === 'finalize' ? 'Sealing…' : draft.finalized_run_id ? 'Evidence sealed' : 'Seal immutable run'}</button>
        </div>
      </section>
    </div>
  )
}
