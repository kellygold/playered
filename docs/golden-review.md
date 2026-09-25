# Golden label, mask, and statistic review

Linear issue: `24K-27`

Golden payloads version one exhaustive label image, one binary mask per palette label, measured
statistics, and explicit pixel/stat tolerances. The loader cross-checks all redundant data: masks
must be binary, mutually exclusive, exhaustive, and equal to the label field; counts and dimensions
must agree with the manifest.

## Normal comparison is read-only

An actual output is represented using the same payload schema. Compare it without changing either
directory:

```bash
python scripts/golden_review.py compare tests/goldens/example /tmp/actual-example
```

The command exits nonzero when dimensions, palette/value mapping, labels, statistics, or tolerances
fail. A missing baseline is an actionable error directing the caller to create a proposal. There is
no update environment variable and no test mode that rewrites expected files.

## Propose, visually review, then accept

```bash
python scripts/golden_review.py propose tests/goldens/example /tmp/actual-example /tmp/example-review
```

This leaves `tests/goldens/example` byte-for-byte unchanged and writes:

- `before/` — the exact baseline, when one exists;
- `after/` — the proposed complete payload;
- `contact-sheet.png` — colorized **BEFORE / AFTER / DIFFERENCE** panels, with changed pixels in
  magenta;
- `review.json` — comparison details and SHA-256 identities for the baseline, proposal, and contact
  sheet.

After reviewing the image and metrics, copy the exact acceptance command from `review.json`. It has
the form:

```bash
python scripts/golden_review.py accept tests/goldens/example /tmp/example-review \
  --sha256 <reviewed-proposal-sha256>
```

Acceptance refuses a changed proposal, changed contact sheet, incorrect digest, or baseline modified
since the review was created. The accepted payload replaces the baseline atomically. The review
bundle remains available as the before/after evidence for the code review or commit.

## Tolerance semantics

Both `max_changed_pixels` and `max_changed_ratio` are hard ceilings; a label comparison must satisfy
both. Each named statistic can define an absolute and relative tolerance, with the larger allowed
delta applied. Missing/extra statistics fail explicitly. Tolerances live beside the expected output
and therefore change only through the same proposal/review process.
