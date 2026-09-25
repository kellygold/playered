# Studio UI cleanup

The preparation workspace now gives the processed preview priority once it exists. Source crop and print colors remain available in an expandable section. Inspection, repair tools, mural planning, profile explanations, and statistics are folded initially. Selection settings open when a drawing tool is chosen; existing editing behavior and persisted configuration are unchanged.

A shared CSS reset had overridden canvas button typography. Lowering its specificity restores component styling. Buttons and form controls now align, the workspace uses more desktop width, and navigation wraps on small screens.

The header holds the single primary workflow action. The build checklist retains its five stages, with detailed explanations available on demand. Active preview/build jobs show elapsed time and the last reported checkpoint. The timer is not a heartbeat or an estimated finish time; percentages are not interpolated. Slicing explicitly explains that Bambu Studio may report no new checkpoint for several minutes. Preview analysis distinguishes color coverage, regions, widths, holes, risk overlays, and file writing. Result loading is distinguished from completed processing.

## Verification

- 345 frontend tests passed, including import/preview/build/download, cancellation, retry, elapsed time, checkpoint stability, and result loading.
- Type checking, ESLint, and production build passed. Vite still reports its existing large-chunk advisory.
- Chrome checks at 1440, 768, and 390 px: saved preview, source controls, selection disclosure, simulated slow slicing, and retryable failure. No page overflow or uncaught browser errors.
- Visual checks use read-only access to the existing workspace and mocked geometry/export requests. The background mural preview request was blocked. No new real slicing job or saved-draft mutation was used for these checks.

## Hardware review scope

This change does not alter processing algorithms or claim a speed improvement. Local hardware reports Apple M5 Max, 18 logical CPUs, and 128 GiB RAM.

- The local job manager has two thread workers. That permits concurrent jobs but does not make all Python work within one preview parallel.
- Preview normalization, quantization, cleanup, editor replay, and analysis follow a sequential pipeline. Native numerical operations may use native execution, but there is no adaptive process pool for a preview.
- Independent geometry extrusion already uses a bounded spawn pool: serial for small inputs, otherwise at most eight processes and at least two reported CPUs reserved. Topology, assembly, validation, and serialization are not all parallelized by that pool.
- Slicing runs through the external Bambu Studio CLI. This UI pass does not measure its core utilization or tune its internal scheduling.

Further performance work should profile representative images stage by stage, including memory and process startup costs, before increasing worker counts. The earlier geometry speedup is documented separately in `geometry-performance.md`.
