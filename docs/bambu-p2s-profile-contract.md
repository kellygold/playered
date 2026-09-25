# Bambu Lab P2S profile contract

Status: measured contract for Image23MF schema version 1  
Evidence application: Bambu Studio `02.07.01.62` on macOS  
Supported target: Bambu Lab P2S, 0.2 mm and 0.4 mm nozzles

## Decision

An Image23MF 3MF contains two deliberately different kinds of settings:

1. `Metadata/project_settings.config` contains portable **import hints**. It makes the
   intended printer, nozzle, layer, plate, and material names visible, but it is not a
   complete Bambu preset and is never trusted as slicing authority.
2. `Metadata/image23mf_profile.json` names the exact installed machine, process, and
   filament profiles that are the **slice authority**. Before invoking Bambu Studio, the
   adapter recursively resolves each installed profile's `inherits` chain, stages the
   canonical flattened bytes, applies the measured artwork overrides to the resolved
   process, and pins the bytes by SHA-256.

The 3MF continues to identify its `Application` as `Image23MF Studio 0.1`. It must not
claim to be a native Bambu Studio project unless it contains Bambu's complete resolved
configuration.

## Why the boundary is necessary

The following behaviors were reproduced with the installed Bambu Studio CLI:

| Input / CLI strategy | Reopened or sliced result |
| --- | --- |
| Compact Image23MF 3MF, no explicit profiles | Active defaults replace the hints: 0.4 mm nozzle, 0.20 mm layer, Cool Plate, Arachne, thin wall off, precise outer wall off, 85% minimum bead, default green filament. |
| Native Bambu project with its complete configuration | P2S, nozzle, layer, plate, process controls, and filament configuration persist. |
| Compact project relabeled with a native `BambuStudio-*` Application value | Bambu Studio `02.07.01.62` crashes during export/slice. This is forbidden. |
| Installed leaf process supplied directly with `--load-settings` | The leaf name is selected, but inherited fields are not materialized. A P2S 0.2 / 0.10 profile reopened as 0.20 mm layer and 0.40 mm line width. |
| Recursively flattened machine/process/filament profiles | Inherited values survive staging and control the slice. |
| Flattened process plus measured artwork overrides | Textured PEI, Arachne, thin wall, precise outer wall, and 70% minimum bead persist in the sliced 3MF. |

The opt-in evidence test proves the complete strategy for both:

- P2S 0.2 mm nozzle at 0.10 mm layer height; and
- P2S 0.4 mm nozzle at 0.20 mm layer height.

Both outputs contain non-empty plate G-code and reopen with the exact requested nozzle,
layer height, Textured PEI plate, Arachne, thin-wall detection, precise outer wall, and
70% minimum bead width.

## Supported installed profiles

The Resources-relative path is stable and never stores a machine-specific absolute path
inside the 3MF.

| Nozzle | Supported layer heights | Machine profile | Process profile suffix |
| --- | --- | --- | --- |
| 0.2 mm | 0.08, 0.10, 0.12 mm | `profiles/BBL/machine/Bambu Lab P2S 0.2 nozzle.json` | `@BBL P2S 0.2 nozzle` |
| 0.4 mm | 0.08, 0.12, 0.16, 0.20, 0.24 mm | `profiles/BBL/machine/Bambu Lab P2S 0.4 nozzle.json` | `@BBL P2S` |

The exact process names are defined in
`image23mf.bambu.profiles.PROCESS_PROFILE_NAMES` and match the bundled printer catalog.
Schema version 1 supports PLA with installed `Bambu PLA Basic` and `Bambu PLA Matte`
profiles. Unsupported printers, nozzles, layer heights, plates, material types, or presets
fail before a package is written.

## Measured artwork process overrides

The following keys are applied **after** recursive process inheritance is resolved:

```json
{
  "curr_bed_type": "Textured PEI Plate",
  "detect_thin_wall": "1",
  "min_bead_width": "70%",
  "precise_outer_wall": "0",
  "wall_generator": "arachne"
}
```

The resolved process profile remains responsible for the requested layer height, line
widths, shell policy, speeds, and the rest of the installed process. A sparse custom
profile is not an acceptable substitute.

## Embedded schema version 1

`Metadata/image23mf_profile.json` is canonical JSON with a trailing newline. The package
declares the `.json` OPC content type and the strict reader verifies the embedded payload
against the plan derived from canonical project settings and materials.

```json
{
  "authority": {
    "embedded_project_settings": "import_hint",
    "installed_profiles": "slice_authority",
    "requested_filament_colors": "visual_intent"
  },
  "contract": "image23mf.bambu.p2s-profile",
  "evidence": {
    "application": "BambuStudio",
    "platform": "macOS",
    "version": "02.07.01.62"
  },
  "installed_profiles": {
    "machine": {
      "kind": "machine",
      "name": "Bambu Lab P2S 0.4 nozzle",
      "relative_path": "profiles/BBL/machine/Bambu Lab P2S 0.4 nozzle.json",
      "resolution": "recursive_inheritance_merge",
      "source": "installed_system_profile"
    },
    "process": {
      "kind": "process",
      "name": "0.20mm Standard @BBL P2S",
      "relative_path": "profiles/BBL/process/0.20mm Standard @BBL P2S.json",
      "resolution": "recursive_inheritance_merge",
      "source": "installed_system_profile",
      "override_strategy": "resolve_inheritance_then_overlay",
      "overrides": {
        "curr_bed_type": "Textured PEI Plate",
        "detect_thin_wall": "1",
        "min_bead_width": "70%",
        "precise_outer_wall": "0",
        "wall_generator": "arachne"
      }
    },
    "filaments": [
      {
        "base_preset": "Bambu PLA Matte",
        "extruder": 1,
        "filament_type": "PLA",
        "kind": "filament",
        "name": "Bambu PLA Matte @BBL P2S",
        "relative_path": "profiles/BBL/filament/Bambu PLA Matte @BBL P2S.json",
        "requested_color": "#CBC6B8",
        "resolution": "recursive_inheritance_merge",
        "source": "installed_system_profile"
      }
    ]
  },
  "schema_version": 1,
  "target": {
    "bed_type": "Textured PEI Plate",
    "layer_height_mm": 0.2,
    "nozzle_diameter_mm": 0.4,
    "printer_model": "Bambu Lab P2S"
  }
}
```

Requested filament hex colors remain visual/project intent. Bambu's installed filament
profile controls physical material settings, and the CLI does not reliably accept a
patched system filament color. Validation must therefore not claim the sliced output's
filament swatch is authoritative merely because the input 3MF contains the requested
hex value.

## Adapter workflow

```python
from image23mf.bambu import read_bambu_profile_plan, resolve_p2s_profile_set

plan = read_bambu_profile_plan(project_3mf)
resolved = resolve_p2s_profile_set(plan, bambu_resources_directory)

# Pin resolved.machine.payload, resolved.process.payload, and each filament payload by
# resolved.*.sha256, stage those exact bytes, then pass their paths through
# --load-settings and --load-filaments.
```

The validator records the source profile paths and the exact staged-byte fingerprints.
Changing Bambu Studio, any inherited system profile, or any override therefore changes
the evidence/cache identity.

## Verification

Fast deterministic tests:

```shell
.venv/bin/pytest -q tests/test_bambu_profiles.py tests/test_bambu_3mf.py
```

Installed Bambu evidence (explicit opt-in):

```shell
BAMBU_STUDIO_CLI=/Applications/BambuStudio.app/Contents/MacOS/BambuStudio \
  .venv/bin/pytest -q \
  tests/test_bambu_3mf.py::test_installed_bambu_profiles_control_reopened_slice_settings
```

Any Bambu Studio upgrade requires rerunning the opt-in evidence before its version is
added to the supported evidence set.


Photo coverage correction (24 September 2026): use normal outer-wall spacing
(`precise_outer_wall=0`). Forcing precise spacing left more exposed base between
adjacent color regions in Thunderhead's eight-color slice. Slicer-footprint
comparison showed less exposed area with normal spacing, while Classic walls
made coverage worse. Keep Arachne. This does not make sub-nozzle details printable;
near-identical colors and noisy boundaries still benefit from palette simplification
and cleanup. Seam display markers are separate from actual exposed base.
