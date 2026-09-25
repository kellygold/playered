# First-run image-to-3MF workflow

Image23MF is a local print-preparation tool. Its first-run path is designed to produce an
automatically validated example without requiring this document: choose **Try guided sample** on
the empty screen and follow the in-app workflow guide. This document records the same contract for
testing, support, and future contributors.

## 1. Source

- Import PNG, JPEG, or WebP up to 64 MB, or use the bundled sample.
- The source asset is preserved. Cleanup, crop, and palette work are stored as draft settings and
  operations rather than destructively rewriting the uploaded file.
- The bundled sample uses broad, high-contrast features sized for the default 0.4 mm-nozzle
  workflow. It is an automated-validation example, not a promise that every printer, plate, or
  filament combination will produce the same physical result.

## 2. Processed proof

Wait for **Preview complete and current** before judging or building output. The processed view is
the exact palette assignment used by geometry; the source view is only a reference.

Review these items in physical units:

- **Palette:** each assigned color becomes a material region. The number of source colors and the
  number of assigned print colors may differ after quantization. Match the final assignments to
  the intended AMS or manual filament slots in Bambu Studio.
- **Tiny islands:** disconnected regions below the nozzle-aware area threshold can disappear or
  become awkward dots.
- **Tiny holes:** enclosed gaps below the selected threshold can close or become small rings.
- **Narrow bridges and gaps:** features near or below the effective extrusion width may merge,
  detach, or be removed by slicing.
- **Crop and dimensions:** verify final millimetres, not only source pixels or zoom level.

Changing crop, palette, physical dimensions, printer/nozzle selection, or cleanup settings makes
old preview and output evidence stale. Rebuild the processed proof before continuing.

## 3. Geometry

Choose **Build and validate 3MF** only when the saved draft and processed proof are current.
Image23MF freezes that evidence, builds printable bodies, checks geometry quality, and packages the
result. A geometry preview shows what was generated; it is not a slicer-layer preview.

## 4. Slicer validation

The retained validation evidence proves that Bambu Studio opened and sliced the exact generated
3MF. Inspect the report and retained log, including warnings. A `validated` result is structural
and automated; it does not guarantee:

- bed adhesion or warping;
- correct physical filament loading or color order;
- surface finish from a particular build plate;
- survival of every tiny eye, dot, hole, gap, or anti-aliased edge;
- a collision-free prime-tower placement for every later slicer edit.

Download the verified 3MF only after reviewing the evidence.

## Before printing

1. Open the downloaded 3MF in the intended Bambu Studio version.
2. Confirm printer, 0.2 or 0.4 mm nozzle, layer height, model size, and filament mapping.
3. Inspect every sliced layer and any prime tower, purge, or collision warnings.
4. Choose face-up or face-down orientation based on the desired plate texture and art finish.
5. Check the first layer and run a smaller proof when fine boundaries matter.

## Saving, revisions, and backups

Draft edits autosave into the local Image23MF workspace. Publish a named revision before a major
experiment to retain immutable settings and evidence. Replacing a source starts another project;
it does not delete the current one.

A downloaded 3MF is an output artifact, not a complete project backup. Back up the Image23MF
workspace directory with the machine's normal local backup system. Keep irreplaceable source
images separately as well.

## Current limitations

- Validation is automated slicer evidence, not a physical-print certification.
- Features smaller than the selected nozzle and profile thresholds can still collapse in the
  final extrusion paths.
- Natural-language local edits such as “make the eyes bigger” are not implemented yet; edit the
  source or use the available crop, palette, cleanup, and region tools.
- If the local engine is offline, bundled and user-image imports remain disabled until it is
  available. Existing local project files are not uploaded to a remote service by this workflow.
