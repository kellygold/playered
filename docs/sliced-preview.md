# Comparing artwork with a slice

Processed is a palette image: every pixel is a solid intended color. Geometry is
also a filled projection. Neither describes the finite-width paths a nozzle can
lay down. Narrow regions, adjacent color boundaries and gaps between paths can
expose the structural backing even when the mesh has complete planar coverage.
Two nearly identical colors make those boundaries hard to see in the artwork.

Exports now use normal outer-wall spacing, retaining Arachne. The Sliced view is
created from the exact Bambu validation G-code for the downloadable native project.
It draws positive extrusion with the recorded line widths and filament colors,
including clockwise/counterclockwise XY arcs, in layer order. Travel, retraction
and seam markers are not colored extrusion. The preview is cropped to the authored
canvas, with the package's placement applied; it does not paint mesh faces over gaps.

This is a top-down footprint approximation, not a simulation of flow, adhesion,
translucency, strand cross-sections or surface finish. It is capped at 1024 pixels
on the long edge in exports, with a drawing grid of at least 24 pixels/mm
independent of source resolution (up to 6144 pixels per side). Inspect the final
project in Bambu Studio after changing printers, filaments or process settings.

The image artifact records both package and sliced-artifact SHA-256 values.
Changing the artwork makes the old slice unavailable in the canvas. Legacy exports
without the artifact remain downloadable; rebuild to obtain a Sliced view. A slice
outside the supported single-plate, unrotated, millimeter/XY-IJ-arc format leaves a
reason in the validation report rather than displaying a fabricated image. The
validated 3MF remains downloadable. Parsing has size/work limits and cancellation.

The catalog audit uses this same renderer and retains original files, exact new
files, slices and before/after coverage measurements. Coverage figures are
screening estimates, not a universal print-success threshold. Existing slice
success, color-presence and mesh checks continue to apply independently.
