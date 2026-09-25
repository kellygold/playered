import { useId } from 'react'
import { firstRunStageIndex, type FirstRunStageId } from './firstRunModel'
import './FirstRunGuide.css'

type FirstRunGuideProps = {
  open: boolean
  stage: FirstRunStageId
  sampleActive: boolean
  previewWorking: boolean
  outputWorking: boolean
  canGenerate: boolean
  canDownload: boolean
  onClose: () => void
  onOpen: () => void
  onGenerate: () => void
  onDownload: () => void
}

const STAGES: ReadonlyArray<{
  id: FirstRunStageId
  label: string
  summary: string
}> = [
  {
    id: 'source',
    label: 'Source',
    summary: 'The original image is preserved as immutable input.',
  },
  {
    id: 'processed',
    label: 'Processed',
    summary: 'Palette, cleanup, crop, and print-risk evidence are resolved in real dimensions.',
  },
  {
    id: 'geometry',
    label: 'Geometry',
    summary: 'The current processed pixels become printable bodies and layer heights.',
  },
  {
    id: 'validated',
    label: 'Slicer',
    summary: 'Bambu Studio opens and slices the retained 3MF before it is offered to you.',
  },
]

const STAGE_COPY: Record<FirstRunStageId, { title: string; body: string }> = {
  source: {
    title: 'Start with a source you can reason about',
    body: 'Use PNG, JPEG, or WebP up to 64 MB. Broad, separated color regions are the easiest first print; transparency is preserved and the source file is never overwritten.',
  },
  processed: {
    title: 'Read the processed proof, not just the pretty picture',
    body: 'The processed view is the exact color assignment used downstream. Tiny islands, narrow gaps, and small enclosed holes can become detached dots or rings after slicing, so tune cleanup against the selected nozzle and physical millimetres.',
  },
  geometry: {
    title: 'Geometry is derived from current evidence',
    body: 'PLAyered freezes the saved draft and current preview, generates solid bodies, then packages them. If you change the palette, crop, dimensions, or cleanup settings, regenerate the preview before trusting this stage.',
  },
  validated: {
    title: 'Validated means structurally sliceable',
    body: 'The retained report proves Bambu Studio opened and sliced this exact package. It does not guarantee bed adhesion, color order, surface finish, or that every tiny feature will survive a real extrusion path.',
  },
}

export function FirstRunGuide({
  open,
  stage,
  sampleActive,
  previewWorking,
  outputWorking,
  canGenerate,
  canDownload,
  onClose,
  onOpen,
  onGenerate,
  onDownload,
}: FirstRunGuideProps) {
  const headingId = useId()
  const activeIndex = firstRunStageIndex(stage)
  const copy = STAGE_COPY[stage]

  if (!open) {
    return (
      <button className="first-run-guide-trigger" type="button" onClick={onOpen}>
        Workflow guide
      </button>
    )
  }

  return (
    <aside className="first-run-guide" aria-labelledby={headingId} data-stage={stage}>
      <header>
        <div>
          <span>{sampleActive ? 'Guided sample' : 'First-run guide'}</span>
          <h2 id={headingId}>Image → validated 3MF</h2>
        </div>
        <button type="button" aria-label="Close workflow guide" onClick={onClose}>×</button>
      </header>

      <ol className="first-run-guide__steps" aria-label="First-run workflow">
        {STAGES.map((item, index) => (
          <li
            key={item.id}
            data-state={index < activeIndex ? 'complete' : index === activeIndex ? 'current' : 'waiting'}
            aria-current={index === activeIndex ? 'step' : undefined}
          >
            <span aria-hidden="true">{index < activeIndex ? '✓' : index + 1}</span>
            <div><strong>{item.label}</strong><small>{item.summary}</small></div>
          </li>
        ))}
      </ol>

      <section className="first-run-guide__current" aria-live="polite">
        <span>Current stage · {STAGES[activeIndex].label}</span>
        <h3>{copy.title}</h3>
        <p>{copy.body}</p>
        {previewWorking ? <p role="status">Building the processed proof…</p> : null}
        {outputWorking ? <p role="status">Generating geometry and validating the package…</p> : null}
        {canGenerate ? (
          <button type="button" onClick={onGenerate}>Build and validate 3MF</button>
        ) : null}
        {canDownload ? (
          <button type="button" onClick={onDownload}>Download verified 3MF</button>
        ) : null}
      </section>

      <details>
        <summary>Palette and physical cleanup</summary>
        <p>
          Each assigned color becomes a material region. Keep the palette intentional, map it to
          the loaded filament slots in the slicer, and inspect warnings for islands, holes, narrow
          bridges, or gaps below the nozzle-aware thresholds. Cleanup changes the printable design;
          zoom alone does not.
        </p>
      </details>
      <details>
        <summary>Before the physical print</summary>
        <ul>
          <li>Confirm the printer, 0.2 or 0.4 mm nozzle, layer height, and final millimetres.</li>
          <li>Open the 3MF in Bambu Studio and inspect every layer, filament mapping, and prime tower.</li>
          <li>Check the first layer and choose face-up or face-down for the plate and finish you want.</li>
          <li>Run a small proof when fine islands, enclosed holes, or color boundaries matter.</li>
        </ul>
      </details>
      <details>
        <summary>Saving, revisions, and backups</summary>
        <p>
          Draft edits autosave into the local workspace. Publish a named revision before a major
          experiment so its settings and evidence stay immutable. A downloaded 3MF is an output,
          not a complete project backup; back up your workspace directory with your normal
          local backup system.
        </p>
      </details>
      <details>
        <summary>Current limits</summary>
        <p>
          Validation is automated slicer evidence, not a physical-print promise. Very small eyes,
          dots, gaps, and anti-aliased edges may still collapse to a nozzle path. The app does not
          yet understand a natural-language request such as “make the eyes bigger”; use the cleanup,
          palette, crop, and source-image controls and verify the final sliced layers.
        </p>
      </details>
    </aside>
  )
}
