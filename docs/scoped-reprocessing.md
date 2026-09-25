# Scoped preview reprocessing

Preview cache reuse is an optimization, never an authority. Every preview keeps its complete
current derivation key and publishes fresh result artifacts. A prior successful preview may supply
verified, content-addressed intermediate planes only when the planner can prove that the source,
crop/canvas transform, palette, cleanup and printer settings, engine, and adapter versions are
unchanged.

## Safe scoped path

The scoped path currently accepts exactly one appended deterministic `fill` or `color_cleanup`
local-raster command. Its canonical selection must be active, contained within one label, and at
least one working pixel away (including diagonals) from every label, transparency, and canvas
boundary. The engine then:

1. verifies the prior cache artifacts by content hash;
2. reuses the exact quantized and automatic-cleanup label/active planes;
3. replays the complete current editor command sequence from that cleanup baseline; and
4. recomputes all region, island, clearance, hole, and risk analysis.

Replaying the full command log from the same immutable baseline makes the result equivalent by
construction to an unconditional render. Cache artifacts retain the current job's derivation key;
old derivation identities are never relabeled as current outputs.

## Full-render fallbacks

The planner deliberately falls back to full quantization, cleanup, replay, and analysis for:

- no complete verified baseline;
- source, crop, canvas, or transform changes;
- palette changes;
- cleanup, nozzle, layer, plate, or printability-setting changes;
- engine, adapter, or dependency upgrades;
- removed, reordered, replaced, multiple, or non-local commands; and
- any selection touching a label, transparency, or canvas boundary.

False negatives cost only processing time. An ambiguous cache is treated as missing and can never
fail the preview itself.

## Evidence

Every preview publishes a `reprocessing-plan`, its exact binary
`reprocessing-affected-mask`, and a PNG mask preview. The typed API result exposes the plan, and the
preview status panel labels the run as **Scoped reprocessing** or **Full reprocessing**, including
the reason, reused stages, and affected-pixel count. Full runs publish an all-zero affected mask so
consumers never mistake a local hint for the authority of a globally recomputed result.
