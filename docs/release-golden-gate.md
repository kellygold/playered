# Release golden gate

`tests/test_release_golden.py` is the software release fixture for the complete discrete-color
geometry path. It starts from an exact 100 × 100 label field covering 20 × 20 mm and carries the
same evidence through shared-boundary topology, structural base generation, flush artwork
extrusion, mesh quality validation, Bambu 3MF packaging, round-trip parsing, and CLI slicing.

The adversarial artwork deliberately contains disconnected regions, forcing the full-canvas
background island through deterministic multi-hole triangulation. It contains:

- a 0.4 × 0.4 mm compact terminal dot;
- a 4 mm ring with a 2 mm enclosed square hole;
- a 0.2 × 6 mm line; and
- two pads joined by a 0.2 mm neck.

The committed `tests/goldens/release_gate_v1.json` freezes label, geometry, quality, and package
fingerprints for the P2S 0.2 mm nozzle at 0.10 mm layers and the P2S 0.4 mm nozzle at 0.20 mm
layers. A change to any fingerprint requires an intentional golden review; the fixture must retain
zero topology gap area and zero overlap area.

## Automated evidence

The default suite uses a deterministic fake Bambu executable. It still exercises executable
detection, pinned profile validation, safe CLI staging, the actual validator, and non-empty sliced
artifact evidence without depending on a workstation installation:

```bash
.venv/bin/pytest -q tests/test_release_golden.py -k 'not installed'
```

On a macOS workstation with Bambu Studio installed, the opt-in gate resolves and pins the bundled
P2S machine, process, and filament profiles and slices both nozzle variants with the installed CLI:

```bash
IMAGE23MF_RUN_INSTALLED_BAMBU=1 \
  .venv/bin/pytest -q tests/test_release_golden.py -k installed
```

## Optional physical characterization

This fixture provides software and installed-slicer evidence only. The optional physical specimen record is
recorded as `not_observed` in the golden file. Dot survival, hole openness, line continuity, neck
strength, surface quality, registration, and dimensional accuracy must remain manual and must not
be described as passing until a human observes prints from both profiles.

This campaign is optional for the free local-web beta. Prior pipeline prints can be described
as user experience without claiming these specific specimens were observed.

Generate the two exact, independently parsed 3MFs and their unsigned observation template with:

```bash
.venv/bin/python scripts/generate_physical_release_specimens.py
```

The default output is `workspace/qa/physical-specimens`. Do not resize either 20 × 20 mm model.
Print the `0.2mm` package with a 0.2 mm nozzle at 0.10 mm layers and the `0.4mm` package with a
0.4 mm nozzle at 0.20 mm layers. Before changing `status`, retain printer, plate, filament, slicer,
operator, date, photo paths, and notes in `physical-observations.json`; every feature starts as
`not_observed` so generating files alone can never satisfy the manual gate.
