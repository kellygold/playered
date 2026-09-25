import './onboarding/OnboardingEntry.css'

type ImportPanelProps = {
  engineReady: boolean
  samplePending: boolean
  onChoose: () => void
  onTrySample: () => void
  onOpenGuide: () => void
}

export function ImportPanel({
  engineReady,
  samplePending,
  onChoose,
  onTrySample,
  onOpenGuide,
}: ImportPanelProps) {
  return (
    <section className="import-hero" aria-labelledby="import-heading">
      <div className="import-copy">
        <span className="eyebrow">Image Lab · Private by default</span>
        <h1 id="import-heading">Turn artwork into geometry you can trust.</h1>
        <p>
          Start with a PNG, JPEG, or WebP. We normalize it locally, preserve transparency, and keep
          every crop decision tied to real print dimensions.
        </p>
        <div className="format-row" aria-label="Supported image formats">
          <span>PNG</span>
          <span>JPEG</span>
          <span>WebP</span>
          <small>Up to 64 MB</small>
        </div>
      </div>
      <div className="drop-card" data-testid="drop-card">
        <div className="drop-illustration" aria-hidden="true">
          <span>+</span>
          <i />
        </div>
        <h2>Drop an image anywhere</h2>
        <p>or paste directly from your clipboard</p>
        <button className="button button-primary" type="button" onClick={onChoose}>
          Choose an image
        </button>
        <kbd>⌘ V</kbd>
        <div className="guided-entry">
          <strong>New here? Start with the bundled sample.</strong>
          <small>
            Follow source → processed proof → geometry → slicer validation using a bundled
            high-contrast image with broad features designed around a 0.4 mm nozzle.
          </small>
          <div className="guided-entry__actions">
            <button
              type="button"
              data-primary="true"
              disabled={!engineReady || samplePending}
              onClick={onTrySample}
            >
              {samplePending ? 'Preparing sample…' : 'Try guided sample'}
            </button>
            <button type="button" onClick={onOpenGuide}>Read the workflow</button>
          </div>
        </div>
        {!engineReady ? (
          <div className="engine-warning" role="status">
            The local engine is offline. Start it before importing.
          </div>
        ) : null}
      </div>
      <div className="trust-row">
        <span>
          <i aria-hidden="true">01</i> Signature checked
        </span>
        <span>
          <i aria-hidden="true">02</i> Color normalized
        </span>
        <span>
          <i aria-hidden="true">03</i> Source preserved
        </span>
      </div>
    </section>
  )
}
