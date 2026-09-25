# Editable Bambu downloads

A generated intermediate 3MF is not the downloadable artifact. Bambu Studio
2.8.2's GUI ignores project settings when the root model's Application metadata
is not a parseable BambuStudio version. Our intermediate identifies its actual
writer as Image23MF. It therefore loaded as geometry only, reported "invalid
config", and could lose the intended printer settings and colors.

The old CLI validation hid this: it supplied complete machine/process/filament
profiles and color overrides. A successful slice did not prove GUI import.

Single-plate export now:

1. Builds and structurally checks the intermediate geometry package.
2. Resolves and pins installed profiles, artwork process overrides, colors and
   the authored purge-tower position.
3. Uses Bambu Studio to serialize an **unsliced editable project**. This embeds
   full settings and Bambu's actual writer/version metadata; we do not forge it.
4. Checks the import prerequisite and palette/profile coherence.
5. Slices that exact project without external profiles, color overrides,
   placement overrides, or disabled collision checks.
6. Publishes only the verified editable bytes, whose hash is the validation
   report's source hash. Preparation command/output is retained in the report.

Export adapter version 4 invalidates prior cached exports. Existing installed
app builds need rebuilding to receive this change. The low-level package reader
checks our intermediate format, not Bambu's serialized output. Mural export uses
its separate existing pipeline; this fix does not claim a mural GUI import audit.

Regression checks cover ignored Application metadata, preparation failure,
self-contained slicing failure, output publication and exact validated hashes.
Example releases must additionally open representative downloads in the desktop
GUI, and validate every downloadable package using its embedded configuration.

## Photo detail versus sliced coverage

Thunderhead Auto 8 uses two near-black shades, #0C0D08 and #1B1D10. Its structural
base uses palette slot 1 (#F1AD50). Very fine boundaries between the dark regions
can lose coverage during slicing and reveal the orange base. Solid mesh previews
do not show this reliably. Successful import/extrusion checks do not certify
visual fidelity or physical print quality. Keep original examples; assess palette
merging, cleanup and a suitable base color separately from package compatibility.
