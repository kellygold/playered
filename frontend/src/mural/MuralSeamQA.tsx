import { useMemo, useState, type CSSProperties } from 'react'
import type {
  MuralSeamQaReport,
  SeamQaStatus,
  SharedEdgeEvidence,
  TileSeamEvidence,
} from './seamTypes'
import './MuralSeamQA.css'

export type MuralSeamQAProps = {
  masterImageUrl: string
  report: MuralSeamQaReport
}

function statusLabel(status: SeamQaStatus): string {
  if (status === 'pass') return 'Exact'
  if (status === 'warning') return 'Review'
  return 'Blocked'
}

function shortHash(value: string): string {
  return `${value.slice(0, 8)}…${value.slice(-6)}`
}

function pxBounds(tile: TileSeamEvidence): string {
  const bounds = tile.master_pixel_bounds
  return `x ${bounds.x_start}–${bounds.x_end} · y ${bounds.y_start}–${bounds.y_end}`
}

function seamCoordinate(seam: SharedEdgeEvidence): string {
  const axis = seam.orientation === 'vertical' ? 'x' : 'y'
  return `${axis}=${seam.coordinate_px} px · span ${seam.span_start_px}–${seam.span_end_px}`
}

function gapPercent(gapMm: number, assembledMm: number): string {
  if (gapMm <= 0 || assembledMm <= 0) return '0px'
  return `${gapMm / assembledMm * 100}%`
}

export function MuralSeamQA({ masterImageUrl, report }: MuralSeamQAProps) {
  const [showDiagnostics, setShowDiagnostics] = useState(true)
  const [selectedTileId, setSelectedTileId] = useState(report.tiles[0]?.tile_id ?? null)
  const selected = report.tiles.find((tile) => tile.tile_id === selectedTileId)
    ?? report.tiles[0]
    ?? null
  const selectedSeams = useMemo(
    () => report.seams.filter(
      (seam) => seam.first_tile_id === selected?.tile_id || seam.second_tile_id === selected?.tile_id,
    ),
    [report.seams, selected?.tile_id],
  )
  const canvasStyle = {
    '--mural-qa-columns': report.columns,
    '--mural-qa-rows': report.rows,
    '--mural-qa-column-gap': gapPercent(
      report.horizontal_gap_mm,
      report.assembled_size_mm.width,
    ),
    '--mural-qa-row-gap': gapPercent(
      report.vertical_gap_mm,
      report.assembled_size_mm.height,
    ),
    aspectRatio: `${report.assembled_size_mm.width} / ${report.assembled_size_mm.height}`,
  } as CSSProperties

  return (
    <section className="mural-seam-qa" aria-labelledby="mural-seam-qa-heading">
      <header className="mural-seam-qa__heading">
        <div>
          <span className="eyebrow">Assembly QA</span>
          <h2 id="mural-seam-qa-heading">Shared-edge inspector</h2>
          <p>Review exact crop boundaries without drawing guides into printable art.</p>
        </div>
        <span className="mural-seam-qa__status" data-status={report.status}>
          {statusLabel(report.status)}
        </span>
      </header>

      <div className="mural-seam-qa__summary" aria-label="Mural seam summary">
        <div>
          <span>Exact topology clips</span>
          <strong>
            {report.topology.every_master_pixel_represented
              ? `${report.topology.represented_pixel_count} pixels closed`
              : 'Coverage mismatch'}
          </strong>
        </div>
        <div>
          <span>Raster boundaries</span>
          <strong>{report.exact_shared_edge_count}/{report.expected_seam_count} gap-free</strong>
        </div>
        <div>
          <span>Visible art</span>
          <strong>{report.artwork.visible_art_unchanged ? 'Byte-identical' : 'Changed'}</strong>
        </div>
        <div>
          <span>Assembly</span>
          <strong>{report.assembled_size_mm.width} × {report.assembled_size_mm.height} mm</strong>
        </div>
      </div>

      <div className="mural-seam-qa__toolbar">
        <div>
          <strong>{report.columns} × {report.rows} · {report.tiles.length} plates</strong>
          <span>{report.panel_size_mm.width} × {report.panel_size_mm.height} mm each</span>
        </div>
        <button
          type="button"
          aria-pressed={showDiagnostics}
          onClick={() => setShowDiagnostics((current) => !current)}
        >
          {showDiagnostics ? 'Hide diagnostic overlay' : 'Show exaggerated seams'}
        </button>
      </div>

      <figure className="mural-seam-qa__figure">
        <div
          className="mural-seam-qa__canvas"
          data-diagnostics={showDiagnostics}
          style={canvasStyle}
          aria-label="Assembled mural preview"
        >
          {report.tiles.map((tile) => (
            <button
              type="button"
              className="mural-seam-qa__tile"
              data-status={tile.risk.status}
              data-selected={selected?.tile_id === tile.tile_id}
              aria-label={`Plate ${tile.plate_number}, row ${tile.row}, column ${tile.column}, ${tile.rotation_degrees === 90 ? 'rotate 90 degrees' : 'native orientation'}, ${tile.risk.finding_count} risks`}
              key={tile.tile_id}
              onClick={() => setSelectedTileId(tile.tile_id)}
            >
              <img
                src={masterImageUrl}
                alt=""
                draggable="false"
                style={{
                  width: `${report.columns * 100}%`,
                  height: `${report.rows * 100}%`,
                  left: `${-(tile.column - 1) * 100}%`,
                  top: `${-(tile.row - 1) * 100}%`,
                }}
              />
              <span className="mural-seam-qa__tile-label" aria-hidden="true">
                <b>{tile.plate_number}</b>
                <i>{tile.rotation_degrees === 90 ? '↻ 90°' : '0°'}</i>
              </span>
            </button>
          ))}
        </div>
        <figcaption>
          <span data-overlay-proof="metadata-only">Overlay only · not printed</span>
          <span>
            {report.horizontal_gap_mm} mm horizontal · {report.vertical_gap_mm} mm vertical gap
          </span>
        </figcaption>
      </figure>

      <aside className="mural-seam-qa__purity" data-status={report.artwork.visible_art_unchanged ? 'pass' : 'fail'}>
        <strong>
          {report.artwork.visible_art_unchanged
            ? 'No seam guides are baked into visible art'
            : 'Visible artwork changed during partitioning'}
        </strong>
        <p>{report.artwork.proof}</p>
        <dl>
          <div>
            <dt>Master</dt>
            <dd>{shortHash(report.artwork.authoritative_master_sha256)}</dd>
          </div>
          <div>
            <dt>Recomposed</dt>
            <dd>{shortHash(report.artwork.recomposed_visible_art_sha256)}</dd>
          </div>
          <div>
            <dt>Overlay pixels</dt>
            <dd>{report.artwork.overlay_pixels_written}</dd>
          </div>
          <div>
            <dt>Topology</dt>
            <dd>{shortHash(report.topology.topology_partition_sha256)}</dd>
          </div>
        </dl>
      </aside>

      {selected ? (
        <section className="mural-seam-qa__selection" aria-live="polite">
          <header>
            <div>
              <span>Selected tile</span>
              <h3>Plate {selected.plate_number} · {selected.tile_id}</h3>
            </div>
            <span data-status={selected.risk.status}>{statusLabel(selected.risk.status)}</span>
          </header>
          <dl>
            <div>
              <dt>Master crop</dt>
              <dd>{pxBounds(selected)}</dd>
            </div>
            <div>
              <dt>Orientation</dt>
              <dd>{selected.rotation_degrees === 90 ? 'Rotate 90° on plate' : 'Native on plate'}</dd>
            </div>
            <div>
              <dt>Protected sides</dt>
              <dd>{selected.protected_sides.join(', ') || 'Outer edges only'}</dd>
            </div>
            <div>
              <dt>Shared edges</dt>
              <dd>{selected.shared_edge_count}</dd>
            </div>
            <div>
              <dt>Topology clip</dt>
              <dd>{shortHash(selected.tile_topology_artifact_sha256)}</dd>
            </div>
          </dl>
          {selected.risk.findings.length ? (
            <ul className="mural-seam-qa__risks" aria-label={`Plate ${selected.plate_number} risks`}>
              {selected.risk.findings.map((finding) => (
                <li key={`${finding.code}-${finding.message}`} data-severity={finding.severity}>
                  <strong>{finding.code.replaceAll('_', ' ')}</strong>
                  <span>{finding.message}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mural-seam-qa__no-risk">No tile-specific risks in this report.</p>
          )}
          <details className="mural-seam-qa__edges">
            <summary>Raster boundary evidence · {selectedSeams.length}</summary>
            <ul>
              {selectedSeams.map((seam) => (
                <li key={seam.seam_id}>
                  <div>
                    <strong>{seam.first_tile_id} ↔ {seam.second_tile_id}</strong>
                    <span>{seamCoordinate(seam)}</span>
                  </div>
                  <span data-exact={seam.no_gap_no_overlap}>
                    {seam.no_gap_no_overlap ? 'No gap / overlap' : 'Mismatch'}
                  </span>
                </li>
              ))}
            </ul>
          </details>
        </section>
      ) : null}
    </section>
  )
}
