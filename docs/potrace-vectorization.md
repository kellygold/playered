# Canonical Potrace vectorization

`image23mf.vectorization.PotraceVectorizer` is the only supported boundary between processed binary
masks and Potrace. It produces deterministic physical SVG plus closed Geometry IR construction
paths. It does not infer exterior/hole ownership; the topology stage owns that classification.

## Inputs and physical controls

- Masks are non-empty two-dimensional arrays containing only `0` and `1`; active pixels become PBM
  black pixels. P4 PBM bytes and their SHA-256 are deterministic.
- Canvas width and height are millimetres. SVG output is normalized to an exact physical `viewBox`
  and a single explicit lower-left/Y-up transform.
- `turd_area_mm2` is converted to Potrace pixels from the actual X/Y pixel area. The persisted
  evidence records both physical input and effective integer `--turdsize`.
- `curve_tolerance_mm` is converted using the larger physical pixel pitch (so neither axis can
  exceed the requested physical error) and persisted beside the
  effective `--opttolerance`. Corner threshold, turn policy, curve optimization, nozzle diameter,
  mask dimensions, adapter version, and Potrace version all participate in the cache key.
- `PotraceParameters.for_nozzle` provides deterministic 0.2/0.4 mm nozzle-aware defaults without
  hiding the resulting physical parameters.

## Execution and evidence

The adapter discovers Potrace through `ExternalToolRegistry` (minimum 1.16) and invokes it through
`ToolRunner` with an argument array and `shell=False`. It inherits process-group timeout and
cancellation, isolated temporary workspaces, minimal deterministic locale, bounded stdout/stderr,
and literal path handling. The result retains executable, version, safe display command, bounded
logs, duration, effective physical conversions, mask hash, normalized SVG hash, and cache key.

## Output trust boundary

Potrace output is untrusted. The adapter caps SVG size, element count, and path-data length; accepts
only the known Potrace SVG doctype; rejects entities, foreign namespaces/elements, unsupported
transforms and path commands, non-finite/oversized coordinates, malformed XML, open/empty subpaths,
and geometry outside the requested canvas. Metadata and source formatting are discarded.

Accepted `M/L/H/V/C/Z` geometry is transformed through the source `viewBox`, quantized to the
Geometry IR coordinate precision, sorted by deterministic content ID, and serialized into a minimal
canonical SVG. Downstream topology consumes `PotraceResult.paths`; it must publish a separate
topology capability before mesh generation.

Potrace's six-decimal SVG scale can put full-canvas edges a fraction of a micron
outside the requested bounds. Adapter v2 permits only a bounded 0.001 mm outside
rounding margin and snaps those points to the exact edge. Larger escapes still
fail. Interior points retain the previous 0.0001 mm boundary normalization, so the
wider outside margin does not flatten valid interior details. The adapter version
participates in both vectorization and production-geometry cache identities.
