# Deterministic palette engine

Linear issue: `24K-36`

Ambiguous but valid palette outcomes are analyzed separately by the deterministic review contract
in [palette-health.md](palette-health.md); classification itself remains exhaustive and unchanged.
Coverage, reconstruction, adjacency, and topology are measured from those same exhaustive labels in
[palette-metrics.md](palette-metrics.md).
The crop-aware UI, lock semantics, physical-filament mapping, autosave, and durable undo contract is
documented in [palette-editor.md](palette-editor.md).

The palette engine converts sRGB into CIELAB with the D65 reference white and uses CIE76 distance
for palette fitting and classification. It deliberately applies no dithering: every output pixel
has exactly one ordered palette label, and exact ties retain the earliest palette index.

## Automatic palette fitting

`fit_auto_palette` accepts two through eight output colors. It makes two bounded passes over alpha
to count visible pixels and select a spatially systematic sample. The sample phase is derived from
the explicit 32-bit seed; it does not depend on global random state, thread timing, or hash order.
The default sample contains at most 65,536 pixels.

Centroids are initialized with deterministic farthest-point selection and refined with
alpha-weighted Lab k-means. Palette positions may be locked to exact `#RRGGBB` values; locked
centroids never move. Final floating centroids are converted to normalized six-digit sRGB colors.
Flat or low-color images can therefore return collapsed/unused swatches without producing an
invalid label field. The later Palette Lab degeneracy flow owns merge/replace/continue decisions.

## Reproducible classification

`classify_palette` converts the *saved six-digit palette* back to Lab and assigns pixels in bounded
chunks. `quantize_auto_palette` fits first and then invokes this same saved-palette path. No hidden
floating centroid is required to reproduce a revision. Chunk size does not change labels.

Fully transparent pixels are excluded from fitting and assigned palette index zero while retaining
their original alpha in the quantized preview. Partially transparent pixels participate with alpha
weight and also retain alpha. This prevents arbitrary hidden RGB in transparent source pixels from
changing the suggested palette.

The result contains an exhaustive `LabelField`, ordered normalized palette, original alpha bytes,
fit evidence, deterministic PNG rendering, and a content fingerprint. Option fingerprints include
sample/chunk bounds, iteration/convergence controls, seed, and alpha threshold so future cache keys
can pin the exact algorithm inputs.

## Performance and evidence

NumPy vectorizes color conversion and nearest-color distance while the engine bounds its sample and
classification working chunks. Unit coverage includes D65 reference colors, perceptual nearest
colors, exact ties, gradients, flat input, seeded noise, alpha, locked positions, chunk invariance,
validation errors, and a timed 1024×768 eight-color fit/classification. The committed benchmark
suite measures fixed-palette and automatic palette work on the native 2520×1680 Wager fixture.
