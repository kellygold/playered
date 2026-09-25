import type { PaletteColor, PaletteMetrics } from './contracts'

type PaletteMetricsPanelProps = {
  colors: PaletteColor[]
  loading: boolean
  metrics: PaletteMetrics | null
  stale: boolean
}

function formatDeltaE(value: number | null): string {
  return value === null ? '—' : `ΔE ${value.toFixed(1)}`
}

function colorName(colors: PaletteColor[], index: number): string {
  return colors[index]?.name || `Color ${index + 1}`
}

export function PaletteMetricsPanel({
  colors,
  loading,
  metrics,
  stale,
}: PaletteMetricsPanelProps) {
  const status = loading ? 'Analyzing' : stale && metrics ? 'Out of date' : metrics ? 'Current' : 'Waiting'
  const closest = metrics?.adjacency.reduce<(typeof metrics.adjacency)[number] | null>(
    (selected, item) => (!selected || item.delta_e < selected.delta_e ? item : selected),
    null,
  )

  return (
    <section className="metrics-panel" aria-labelledby="palette-metrics-heading">
      <div className="metrics-heading">
        <div>
          <span className="eyebrow">Palette signals</span>
          <h2 id="palette-metrics-heading">What the reduction is doing</h2>
        </div>
        <span className={`metrics-status ${stale ? 'is-stale' : ''}`}>{status}</span>
      </div>

      {loading && !metrics ? (
        <div className="metrics-state" role="status" aria-live="polite">
          <span className="metrics-spinner" aria-hidden="true" />
          <div>
            <strong>Analyzing palette relationships…</strong>
            <p>Measuring color distance, touching boundaries, and separate regions.</p>
          </div>
        </div>
      ) : null}

      {!loading && !metrics ? (
        <div className="metrics-state metrics-state-empty">
          <span aria-hidden="true">⌁</span>
          <div>
            <strong>No palette analysis yet</strong>
            <p>Render a preview to see how the selected filament colors divide this image.</p>
          </div>
        </div>
      ) : null}

      {metrics ? (
        <div className="metrics-body" aria-live="polite">
          {loading ? (
            <p className="metrics-updating">Updating these signals for the latest settings…</p>
          ) : null}
          <div className="metric-summary-grid">
            <article>
              <span>Reconstruction</span>
              <strong>{formatDeltaE(metrics.reconstruction.alpha_weighted_mean_delta_e)}</strong>
              <p>Average perceptual distance from the source. Compare revisions; this is not a score.</p>
            </article>
            <article>
              <span>Closest touching pair</span>
              <strong>{formatDeltaE(metrics.minimum_adjacent_delta_e)}</strong>
              <p>
                {closest
                  ? `${colorName(colors, closest.first_index)} meets ${colorName(colors, closest.second_index)}.`
                  : 'No differently colored regions touch in this preview.'}
              </p>
            </article>
            <article>
              <span>Separate regions</span>
              <strong>{metrics.fragmentation.component_count.toLocaleString()}</strong>
              <p>
                {metrics.fragmentation.excess_component_count.toLocaleString()} beyond one region per used color;
                inspect tiny islands before printing.
              </p>
            </article>
          </div>

          <div className="coverage-section">
            <div className="coverage-heading">
              <strong>Visible coverage</strong>
              <span>{metrics.visible_pixel_count.toLocaleString()} analyzed pixels</span>
            </div>
            <div className="coverage-list">
              {metrics.colors.map((metric) => (
                <div className="coverage-row" key={`${metric.index}-${metric.color}`}>
                  <span
                    className="coverage-swatch"
                    style={{ backgroundColor: metric.color }}
                    aria-hidden="true"
                  />
                  <div className="coverage-label">
                    <strong>{colorName(colors, metric.index)}</strong>
                    <span>{metric.color}</span>
                  </div>
                  <div className="coverage-track" aria-hidden="true">
                    <i style={{ width: `${Math.max(0, Math.min(100, metric.coverage_ratio * 100))}%` }} />
                  </div>
                  <span className="coverage-value">{(metric.coverage_ratio * 100).toFixed(1)}%</span>
                  <span className="coverage-regions">
                    {metric.component_count.toLocaleString()} {metric.component_count === 1 ? 'region' : 'regions'}
                  </span>
                </div>
              ))}
            </div>
          </div>
          <p className="metrics-footnote">
            These describe the current raster preview. No single value guarantees printability; nozzle-aware
            geometry checks come later.
          </p>
        </div>
      ) : null}
    </section>
  )
}
