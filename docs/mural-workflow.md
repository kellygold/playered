# Multi-plate mural workflow

Image23MF builds every mural from one processed master image. The planner divides that master into
ordered physical panels, preserves exact shared-edge ownership, and packages one unique panel per
3MF build plate.

## Preview, save, and build

1. Open a processed project and expand **Multi-plate mural**.
2. Set the row and column count, finished panel dimensions, gaps, and any reserved build-plate
   rectangles. The planner immediately reports assembled size, rotations, and bed fit.
3. Choose **Save mural plan** when the layout is worth retaining. Saved plans can be reopened without
   changing the source project or processed pixels.
4. Optionally enable **External assembly aids** and select the useful guide elements.
5. Choose **Build _n_-plate 3MF**. This is the production action: it partitions the master, generates
   every tile, packages the plates, runs seam evidence, and requires retained Bambu Studio validation.
6. When validation succeeds, use **Download verified multi-plate 3MF**. If assembly aids were enabled,
   the same result also offers **Download assembly sheet** and **Download assembly metadata**.

The planner and processed canvas are previews; they do not silently create a file on disk. A 3MF is
available only after the explicit build reaches **Verified package**.

## External assembly aids

Assembly aids are deliberately external to print geometry. Enabling them generates:

- an A4 SVG assembly sheet with panel order, crop marks, and TOP orientation;
- canonical JSON containing rear identifiers, neighbour/edge relationships, orientation, and optional
  spacer/jig dimensions.

They do not add pixels, labels, marks, or triangles to any visible art face. The build records exact
master/recomposition hashes and geometry fingerprints, and the automated gate proves that enabling
the guides produces byte-identical 3MF, label-manifest, and topology-manifest artifacts.

Rear and edge identifiers are instructions for labels applied after printing; they are not embossed
into the model. Jig dimensions describe the intended finished layout and are not a substitute for a
printer-scale calibration coupon.

## Verification

`make qa-mural` drives the complete workflow in a real Chrome instance. It saves and reloads a
non-square six-panel plan, builds the package, fetches all enabled downloads, validates ZIP/3MF and
artifact signatures, checks exact artwork purity, and proves compact-viewport scrolling. The focused
Python integration suite additionally compares builds with assembly aids disabled and enabled byte
for byte.
