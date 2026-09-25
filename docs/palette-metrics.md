# Palette metrics and interpretation

Linear issue: `24K-39`

Every current preview job preserves nine related, independently checksummed artifacts:

1. the continuous-color cropped preview;
2. its canonical transform/statistics JSON;
3. the exact palette-classified preview;
4. a versioned palette-metrics JSON document; and
5. the exact connected-region graph used by the Printability Engine; and
6. its versioned, classifier-coverage-aware risk report; and
7. the physical small-island classification that supplies exact island risk evidence; and
8. the clearance-ridge analysis for thin lines, necks, and narrow gaps; and
9. the enclosed-hole/local-ring analysis and correction evidence.

The result resource verifies and parses the metrics artifact before returning it. Older completed
previews remain readable with `palette_metrics: null`; metrics-bearing derivation keys began at
`preview-v2`; `preview-v3` added the region graph, `preview-v4` added its bound risk report,
`preview-v5` added the bound island analysis, `preview-v6` added clearance evidence, and
`preview-v7` added enclosed-hole and local-ring evidence. Current preview-v10 additionally binds
automatic cleanup and exact typed editor replay.

## Measurements

Metrics operate exhaustively on the same label bytes and alpha plane used to render the palette
preview. They include:

- visible pixel count and per-color coverage;
- alpha-weighted mean and 95th-percentile CIE76 reconstruction distance, overall and per color;
- every horizontally/vertically touching color pair, its Lab distance, and boundary-edge count;
- four-connected component counts, largest component share, smallest component size in preview
  pixels and square millimetres, excess components beyond one per used color, one-pixel components,
  and components per 100 mm².

Transparent pixels are excluded from coverage, error, adjacency, and topology. Physical component
area uses the full preview pixel scale rather than stretching visible pixels across transparent
margins. Reconstruction work is chunk-bounded; connected components use deterministic scanline runs
and union/find, which preserves exact four-connectivity without one Python object per pixel.

## Honest UI language

The Palette Signals panel deliberately avoids a pass/fail score. It rounds ΔE and coverage only for
display, keeps exact values in the artifact, and describes each signal in comparative terms:

- reconstruction distance is for comparing revisions, not declaring quality;
- closest touching-pair contrast says which colors meet, not whether a printer can resolve them;
- separate-region counts invite inspection of tiny islands rather than calling every component a
  defect;
- a footnote states that nozzle-aware geometry checks are a later gate.

The panel has explicit waiting, analyzing, updating/stale, and current states. It remains useful on
old preview results and responsive at mobile widths.

## Verification

Controlled fixtures prove exact split coverage, zero reconstruction error, known Lab error,
boundary-edge counts, checkerboard four-connectivity, alpha exclusion, physical pixel area,
chunk-independence, deterministic serialization, and input failures. The API user flow proves that
all nine artifacts download, metrics match the saved config, counts close over every visible pixel,
and metrics survive application restart. Frontend tests cover empty, loading, current, stale, rounded
display, and interpretation copy.
