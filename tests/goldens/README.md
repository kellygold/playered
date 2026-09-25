# Versioned expected processing outputs

Golden cases use the schema and two-step review flow documented in
`docs/golden-review.md`. `workflow-specimen/` is a small committed proof containing one exhaustive
label field, four disjoint binary masks, measured statistics, and explicit tolerances.

The source actual-output payload is reproducible without touching the baseline:

```bash
python scripts/generate_golden_specimen.py /tmp/workflow-actual
python scripts/golden_review.py compare tests/goldens/workflow-specimen /tmp/workflow-actual
```

Any intentional change must use `golden_review.py propose`, visually review its contact sheet, and
accept the exact proposal digest. Normal tests never update this directory.

`bambu_slicer_report_v1.json` freezes the schema-v1 report extracted from the compact,
representative Bambu Studio 2.7 G-code in `tests/fixtures/slicer/`. It covers three used colors,
per-filament usage, layer and tool-change markers, model-only extrusion bounds, warning
classification, and exact retained process logs. It is synthetic evidence patterned after local
production slices; private production G-code is never committed.
