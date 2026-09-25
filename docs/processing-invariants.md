# Processing invariants and property tests

Linear issue: `24K-28`

The processing pipeline uses one canonical `LabelField`: immutable dimensions, an ordered set of
declared byte labels, and exactly one label byte per pixel. Construction rejects empty/duplicate
palettes, invalid dimensions, incorrect byte counts, and undeclared pixel values.

## Invariants

- Every pixel has exactly one declared label.
- One binary mask is derived for every label; masks are exhaustive and pairwise disjoint.
- Label image conversion is lossless and preserves dimensions and values.
- Region edits name a versioned operation, target, optional source-label filter, and integer pixel
  rectangle wholly inside the field.
- Operations return a new field, never mutate the source, never alter a pixel outside their region,
  never create an undeclared label, and are deterministic/idempotent for the same input.
- Operation JSON forbids unknown fields and round-trips to the exact contract.
- Versioned job configurations round-trip through canonical JSON with stable SHA-256 fingerprints,
  independent of object key order.

## Generative proof

`tests/test_label_properties.py` uses Hypothesis to exercise 150 generated examples per property,
including 1–4 labels, dimensions from 1–32 pixels per axis, arbitrary exhaustive label layouts,
source-filtered and unfiltered regions, every valid crop mode/nozzle/merge policy/art style, 2–8
unique palette colors, physical cleanup/geometry ranges, and invalid labels/bounds.

Hypothesis records and shrinks a failing example so CI failures are reproducible. The tests run in
the normal `make test` and `make check` gates; no random fixture files or mutable seed state enter the
repository.
