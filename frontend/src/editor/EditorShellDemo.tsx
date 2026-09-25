import { EditorShell } from './EditorShell'
import type { EditorShellViewState } from './types'

export type EditorShellDemoProps = {
  viewState?: EditorShellViewState
  compactMode?: 'auto' | 'stacked' | 'desktop'
  showDialog?: boolean
  showNotification?: boolean
}

export function EditorShellDemo({
  viewState = { kind: 'ready' },
  compactMode = 'auto',
  showDialog = false,
  showNotification = false,
}: EditorShellDemoProps) {
  return (
    <EditorShell
      project={{
        name: 'The Wager',
        assetName: 'the-wager-final.png',
        detail: '200 × 200 mm · 4 colors',
        status: 'Draft saved',
        statusTone: 'positive',
      }}
      compactMode={compactMode}
      viewState={viewState}
      onBack={() => undefined}
      headerActions={
        <>
          <button className="i23-editor-demo-button" type="button">
            Compare
          </button>
          <button className="i23-editor-demo-button" data-primary="true" type="button">
            Render preview
          </button>
        </>
      }
      canvasToolbar={
        <div className="i23-editor-demo-toolbar">
          <button type="button" aria-label="Fit canvas">
            Fit
          </button>
          <button type="button" aria-label="Zoom out">
            −
          </button>
          <span>72%</span>
          <button type="button" aria-label="Zoom in">
            +
          </button>
        </div>
      }
      canvasFooter={
        <div className="i23-editor-demo-footer">
          <span>0.4 mm nozzle</span>
          <span>0.2 mm layers</span>
          <span>sRGB preview</span>
        </div>
      }
      canvas={
        <div className="i23-editor-demo-artboard" aria-label="Artwork preview">
          <div className="i23-editor-demo-artwork">
            <span>The Wager</span>
            <i aria-hidden="true" />
            <b aria-hidden="true" />
          </div>
        </div>
      }
      leftRail={
        <div className="i23-editor-demo-sections">
          <section>
            <span className="i23-editor-demo-kicker">Source</span>
            <h3>Crop & canvas</h3>
            <div className="i23-editor-demo-fields">
              <label>
                <span>Width</span>
                <output>200 mm</output>
              </label>
              <label>
                <span>Height</span>
                <output>200 mm</output>
              </label>
            </div>
          </section>
          <section>
            <span className="i23-editor-demo-kicker">Palette</span>
            <h3>Four filaments</h3>
            <div className="i23-editor-demo-swatches" aria-label="Palette colors">
              <i data-color="bone" />
              <i data-color="orange" />
              <i data-color="blue" />
              <i data-color="black" />
            </div>
          </section>
        </div>
      }
      rightRail={
        <div className="i23-editor-demo-sections">
          <section>
            <span className="i23-editor-demo-kicker">Printability</span>
            <h3>2 items to review</h3>
            <div className="i23-editor-demo-risk" data-tone="warning">
              <strong>Small isolated marks</strong>
              <p>12 regions fall below the selected profile.</p>
            </div>
            <div className="i23-editor-demo-risk">
              <strong>Minimum line width</strong>
              <p>Profile baseline: 0.48 mm.</p>
            </div>
          </section>
        </div>
      }
      notificationSlot={
        showNotification ? (
          <div className="i23-editor-demo-notice" role="status">
            Preview rendered from the current draft.
          </div>
        ) : null
      }
      dialogSlot={
        showDialog ? (
          <div className="i23-editor-demo-dialog-backdrop">
            <div role="dialog" aria-modal="true" aria-labelledby="i23-editor-demo-dialog-title">
              <span className="i23-editor-demo-kicker">Confirm change</span>
              <h2 id="i23-editor-demo-dialog-title">Replace source image?</h2>
              <p>The current project remains available if you cancel.</p>
              <div>
                <button className="i23-editor-demo-button" type="button">
                  Cancel
                </button>
                <button className="i23-editor-demo-button" data-primary="true" type="button">
                  Replace image
                </button>
              </div>
            </div>
          </div>
        ) : null
      }
    />
  )
}
