import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { RegionGraph, RiskReport, RiskSuggestion } from '../../contracts'
import {
  measurementLabel,
  measurementValue,
  otherRegionId,
  regionTopology,
  riskCodeLabel,
  shortRegionId,
  type RegionNode,
  type RiskWarning,
} from './inspectorModel'
import './RegionInspector.css'

export type RegionInspectorSuggestionSelection = {
  warning: RiskWarning
  suggestion: RiskSuggestion
}

export type RegionInspectorProps = {
  graph: RegionGraph | null
  report: RiskReport | null
  selectedRegionId: string | null
  selectedRiskId: string | null
  onRegionSelect: (regionId: string) => void
  onRiskSelect: (riskId: string, regionId: string | null) => void
  onSuggestionSelect?: (selection: RegionInspectorSuggestionSelection) => void
  canvasSelectionStatus?: 'ready' | 'loading' | 'unavailable' | 'error'
  canvasSelectionMessage?: string
  disabled?: boolean
}

const REGION_PAGE_SIZE = 80
const WARNING_PAGE_SIZE = 60

function WarningDetails({
  warning,
  onSuggestionSelect,
  disabled,
}: {
  warning: RiskWarning
  onSuggestionSelect?: (selection: RegionInspectorSuggestionSelection) => void
  disabled: boolean
}) {
  return (
    <div className="region-inspector-warning-details">
      <p>{warning.explanation}</p>
      <dl className="region-inspector-rule">
        <div>
          <dt>Applied rule</dt>
          <dd>{riskCodeLabel(warning.code)}</dd>
        </div>
        <div>
          <dt>Classifier</dt>
          <dd><code>{warning.classifier_id}@{warning.classifier_version}</code></dd>
        </div>
        {warning.measurements.map((measurement, index) => (
          <div key={`${measurement.key}-${measurement.role}-${index}`}>
            <dt>{measurement.role === 'threshold' ? `${measurementLabel(measurement)} threshold` : measurementLabel(measurement)}</dt>
            <dd>{measurementValue(measurement)}</dd>
          </div>
        ))}
      </dl>
      {warning.suggestions.length ? (
        <div className="region-inspector-suggestions">
          <strong>Suggestions</strong>
          <ul>
            {warning.suggestions.map((suggestion, index) => (
              <li key={`${suggestion.kind}-${index}`}>
                {onSuggestionSelect ? (
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => onSuggestionSelect({ warning, suggestion })}
                  >
                    <span>{suggestion.title}</span>
                    <small>{suggestion.explanation}</small>
                    {suggestion.destructive || suggestion.requires_confirmation ? (
                      <em>{suggestion.destructive ? 'Destructive · confirmation required' : 'Confirmation required'}</em>
                    ) : null}
                  </button>
                ) : (
                  <div>
                    <span>{suggestion.title}</span>
                    <small>{suggestion.explanation}</small>
                    {suggestion.destructive || suggestion.requires_confirmation ? (
                      <em>{suggestion.destructive ? 'Destructive · confirmation required' : 'Confirmation required'}</em>
                    ) : null}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="region-inspector-empty-note">No automatic suggestion is available; review this finding manually.</p>
      )}
    </div>
  )
}

function RegionDetails({ graph, region, riskCount }: { graph: RegionGraph; region: RegionNode; riskCount: number }) {
  const topology = regionTopology(graph, region)
  return (
    <div className="region-inspector-region-details" aria-live="polite">
      <div className="region-inspector-region-title">
        <span className="region-inspector-swatch" style={{ backgroundColor: region.color }} aria-label={`Color ${region.color}`} />
        <div>
          <strong>{region.id}</strong>
          <small>Palette label {region.label} · {region.color}</small>
        </div>
      </div>
      <dl className="region-inspector-metrics">
        <div><dt>Topology</dt><dd>{topology.label}</dd></div>
        <div><dt>Area</dt><dd>{region.area_mm2.toFixed(3)} mm²</dd></div>
        <div><dt>Pixels</dt><dd>{region.pixel_count.toLocaleString()}</dd></div>
        <div><dt>Perimeter</dt><dd>{region.perimeter_mm.toFixed(3)} mm</dd></div>
        <div><dt>Compactness</dt><dd>{region.compactness.toFixed(3)}</dd></div>
        <div><dt>Minimum width</dt><dd>{region.width_estimate.minimum_mm.toFixed(3)} mm</dd></div>
        <div><dt>Median width</dt><dd>{region.width_estimate.median_mm.toFixed(3)} mm</dd></div>
        <div><dt>95th percentile</dt><dd>{region.width_estimate.p95_mm.toFixed(3)} mm</dd></div>
        <div><dt>Maximum width</dt><dd>{region.width_estimate.maximum_mm.toFixed(3)} mm</dd></div>
        <div><dt>Warnings</dt><dd>{riskCount}</dd></div>
      </dl>
      <div className="region-inspector-neighbors">
        <strong>Neighbors ({topology.adjacency.length})</strong>
        {topology.adjacency.length ? (
          <ul>
            {topology.adjacency.map((edge) => {
              const neighborId = otherRegionId(edge, region.id)
              const neighbor = graph.regions.find((candidate) => candidate.id === neighborId)
              return (
                <li key={neighborId}>
                  {neighbor ? <span className="region-inspector-swatch" style={{ backgroundColor: neighbor.color }} aria-hidden="true" /> : null}
                  <code>{shortRegionId(neighborId)}</code>
                  <span>{edge.boundary_length_mm.toFixed(3)} mm shared</span>
                </li>
              )
            })}
          </ul>
        ) : <p>No adjacent printable region.</p>}
      </div>
    </div>
  )
}

export function RegionInspector({
  graph,
  report,
  selectedRegionId,
  selectedRiskId,
  onRegionSelect,
  onRiskSelect,
  onSuggestionSelect,
  canvasSelectionStatus = 'ready',
  canvasSelectionMessage,
  disabled = false,
}: RegionInspectorProps) {
  const headingId = useId()
  const [query, setQuery] = useState('')
  const [regionLimit, setRegionLimit] = useState(REGION_PAGE_SIZE)
  const [warningLimit, setWarningLimit] = useState(WARNING_PAGE_SIZE)
  const [warningSeverity, setWarningSeverity] = useState<'all' | 'info' | 'warning' | 'error'>('all')
  const selectedWarningRef = useRef<HTMLButtonElement | null>(null)
  const selectedRegion = graph?.regions.find((region) => region.id === selectedRegionId) ?? null
  const warnings = useMemo(() => report?.warnings ?? [], [report])
  const selectedWarning = warnings.find((warning) => warning.id === selectedRiskId) ?? null
  const matchingWarnings = useMemo(
    () => warningSeverity === 'all'
      ? warnings
      : warnings.filter((warning) => warning.severity === warningSeverity),
    [warningSeverity, warnings],
  )
  const visibleWarnings = useMemo(() => {
    const page = matchingWarnings.slice(0, warningLimit)
    if (!selectedWarning || page.includes(selectedWarning)) return page
    return [selectedWarning, ...page.slice(0, Math.max(0, warningLimit - 1))]
  }, [matchingWarnings, selectedWarning, warningLimit])
  const graphRegionIds = useMemo(() => new Set(graph?.regions.map((region) => region.id) ?? []), [graph])
  const riskCountsByRegion = useMemo(() => {
    const counts = new Map<string, number>()
    for (const warning of warnings) {
      for (const regionId of warning.affected_region_ids) {
        counts.set(regionId, (counts.get(regionId) ?? 0) + 1)
      }
    }
    return counts
  }, [warnings])
  const normalizedQuery = query.trim().toLowerCase()
  const matchingRegions = useMemo(() => {
    if (!graph) return []
    if (!normalizedQuery) return graph.regions
    return graph.regions.filter((region) =>
      `${region.id} ${region.label} ${region.color}`.toLowerCase().includes(normalizedQuery),
    )
  }, [graph, normalizedQuery])
  const visibleRegions = useMemo(() => {
    const page = matchingRegions.slice(0, regionLimit)
    if (
      !selectedRegion ||
      !matchingRegions.includes(selectedRegion) ||
      page.includes(selectedRegion)
    ) {
      return page
    }
    return [selectedRegion, ...page.slice(0, Math.max(0, regionLimit - 1))]
  }, [matchingRegions, regionLimit, selectedRegion])

  useEffect(() => {
    // Chromium may return a Promise from scrollIntoView. An effect must never expose that
    // value to React, which would treat it as a cleanup function and crash on the next update.
    selectedWarningRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [selectedRiskId])

  if (!graph) {
    return (
      <section
        className="region-inspector"
        aria-labelledby={headingId}
        data-canvas-selection-status={canvasSelectionStatus}
      >
        <div className="region-inspector-heading">
          <div><span>Inspection</span><h3 id={headingId}>Regions & warnings</h3></div>
        </div>
        <div className="region-inspector-unavailable" role="status">
          <strong>Region analysis is not available yet.</strong>
          <p>Render a current preview to inspect physical topology and printability findings.</p>
        </div>
      </section>
    )
  }

  return (
    <section
      className="region-inspector"
      aria-labelledby={headingId}
      data-canvas-selection-status={canvasSelectionStatus}
    >
      <div className="region-inspector-heading">
        <div><span>Inspection</span><h3 id={headingId}>Regions & warnings</h3></div>
        <output aria-label="Region and warning totals">{graph.regions.length} regions · {warnings.length} warnings</output>
      </div>

      {canvasSelectionStatus !== 'ready' ? (
        <p className="region-inspector-canvas-status" role="status" data-state={canvasSelectionStatus}>
          {canvasSelectionMessage ??
            (canvasSelectionStatus === 'loading'
              ? 'Loading exact canvas region selection…'
              : canvasSelectionStatus === 'error'
                ? 'Exact canvas selection could not be loaded. Use the region and warning lists.'
                : 'This preview predates exact canvas selection data. Use the region and warning lists.')}
        </p>
      ) : null}

      {selectedRegion ? (
        <RegionDetails graph={graph} region={selectedRegion} riskCount={riskCountsByRegion.get(selectedRegion.id) ?? 0} />
      ) : (
        <p className="region-inspector-selection-hint">Select a region on the canvas or from the region list to inspect its physical measurements.</p>
      )}

      <div className="region-inspector-warning-section">
        <div className="region-inspector-section-heading">
          <strong>Print warnings</strong>
          <span>{warnings.length}</span>
        </div>
        {warnings.length ? (
          <div className="region-inspector-warning-controls">
            <div aria-label="Warning severity totals">
              <span data-severity="error">{report?.summary.error ?? 0} error</span>
              <span data-severity="warning">{report?.summary.warning ?? 0} warning</span>
              <span data-severity="info">{report?.summary.info ?? 0} info</span>
            </div>
            <label>
              <span>Show severity</span>
              <select
                value={warningSeverity}
                onChange={(event) => {
                  setWarningSeverity(event.target.value as typeof warningSeverity)
                  setWarningLimit(WARNING_PAGE_SIZE)
                }}
              >
                <option value="all">All</option>
                <option value="error">Errors</option>
                <option value="warning">Warnings</option>
                <option value="info">Info</option>
              </select>
            </label>
            <output aria-live="polite">
              Showing {visibleWarnings.length} of {matchingWarnings.length} matching warnings
              {selectedWarning && !matchingWarnings.includes(selectedWarning) ? ' plus the selected warning' : ''}
            </output>
          </div>
        ) : null}
        {warnings.length ? (
          <ul className="region-inspector-warning-list" aria-label="Print warnings">
            {visibleWarnings.map((warning) => {
              const selected = warning.id === selectedRiskId
              const regionId = warning.affected_region_ids.find((id) => graphRegionIds.has(id)) ?? null
              return (
                <li key={warning.id} data-severity={warning.severity}>
                  <button
                    ref={selected ? selectedWarningRef : undefined}
                    type="button"
                    aria-pressed={selected}
                    disabled={disabled}
                    onClick={() => onRiskSelect(warning.id, regionId)}
                  >
                    <span className="region-inspector-warning-summary">
                      <em>{warning.severity}</em>
                      <strong>{warning.title}</strong>
                      <small>{riskCodeLabel(warning.code)} · {warning.affected_region_ids.length} region{warning.affected_region_ids.length === 1 ? '' : 's'}</small>
                    </span>
                  </button>
                  {selected ? <WarningDetails warning={warning} onSuggestionSelect={onSuggestionSelect} disabled={disabled} /> : null}
                </li>
              )
            })}
          </ul>
        ) : (
          <p className="region-inspector-clear">No printability warnings in the current preview.</p>
        )}
        {warningLimit < matchingWarnings.length ? (
          <button
            className="region-inspector-load-more"
            type="button"
            onClick={() => setWarningLimit((current) => current + WARNING_PAGE_SIZE)}
          >
            Show {Math.min(WARNING_PAGE_SIZE, matchingWarnings.length - warningLimit)} more warnings
          </button>
        ) : null}
      </div>

      <div className="region-inspector-region-section">
        <label className="region-inspector-search">
          <span>Find region</span>
          <input
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
              setRegionLimit(REGION_PAGE_SIZE)
            }}
            placeholder="ID, label, or #color"
          />
        </label>
        <ul className="region-inspector-region-list" aria-label="Printable regions">
          {visibleRegions.map((region) => {
            const selected = region.id === selectedRegionId
            const riskCount = riskCountsByRegion.get(region.id) ?? 0
            return (
              <li key={region.id}>
                <button type="button" aria-pressed={selected} disabled={disabled} onClick={() => onRegionSelect(region.id)}>
                  <span className="region-inspector-swatch" style={{ backgroundColor: region.color }} aria-hidden="true" />
                  <span><strong>{shortRegionId(region.id)}</strong><small>{region.area_mm2.toFixed(3)} mm² · {riskCount} warning{riskCount === 1 ? '' : 's'}</small></span>
                </button>
              </li>
            )
          })}
        </ul>
        {!visibleRegions.length ? <p className="region-inspector-clear">No regions match “{query}”.</p> : null}
        {regionLimit < matchingRegions.length ? (
          <button className="region-inspector-load-more" type="button" onClick={() => setRegionLimit((current) => current + REGION_PAGE_SIZE)}>
            Show {Math.min(REGION_PAGE_SIZE, matchingRegions.length - regionLimit)} more regions
          </button>
        ) : null}
      </div>
    </section>
  )
}
