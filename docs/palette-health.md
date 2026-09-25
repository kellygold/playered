# Palette ambiguity and resolution

Linear issue: `24K-42`

The palette engine classifies every pixel deterministically even when a requested palette is not
meaningfully printable. `analyze_palette_health` turns those technically valid but ambiguous cases
into a stable review report instead of silently hiding them or failing downstream.

## Findings

The report operates on the exact saved `QuantizationResult`, its alpha bytes, the caller's locked
indices, and versioned health options. It detects, in deterministic order:

1. an automatic fit that collapsed to fewer distinct colors than requested;
2. exact duplicate hex colors, grouped as connected palette slots;
3. distinct colors below the configurable CIE76 separation threshold (default ΔE 3.0);
4. palette colors with no visible pixel assignments; and
5. duplicate/near-identical conflicts whose involved colors are all locked.

Transparent or sub-threshold pixels do not create false coverage for label zero. Each color carries
its visible pixel count, coverage ratio, lock state, and ordered palette index. The report also
includes assigned/unique counts, minimum pair distance, stable issue IDs, option fingerprint,
canonical JSON, and a reproducibility fingerprint.

## Review actions

Every issue exposes the same explicit choices:

- **Merge** removes one unlocked slot and deterministically targets the nearest remaining assigned
  color. It is disabled if every affected slot is locked or removal would leave fewer than two.
- **Replace** keeps palette position and count but requires a new normalized hex color. It is
  disabled for a locked source.
- **Continue** acknowledges the risk and preserves the exact palette. It is always available.

`resolve_palette` applies these choices without mutating the source. A merge returns the full old to
new index map and remapped locks so label/config callers can explicitly reclassify or rewrite their
state; replace and continue retain identity maps. Invalid indices, locked edits, incomplete action
arguments, and illegal two-to-one merges fail before returning partial state.

Tests cover monochrome auto-fit collapse, repeated saved hex values, near-equal Lab colors, healthy
distinct colors, all-locked impossible fits, alpha-aware absence, deterministic reports, merge
mapping/lock remapping, replacement, continuation, and invalid action contracts.
