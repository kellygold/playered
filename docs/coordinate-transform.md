# Canonical crop and physical coordinate transform

Linear issue: `24K-33`

`CanonicalTransform` is the versioned source of truth for this stage chain:

```text
original pixels → EXIF-normalized pixels → crop-local pixels → working raster → millimetres
```

All raster coordinates use continuous pixel-edge space: a `W × H` image spans `(0, 0)` through
`(W, H)`, while pixel `(x, y)` has center `(x + 0.5, y + 0.5)`. Rectangles are half-open when they
select pixels, although their continuous outer boundaries map exactly. Millimetres retain the
image/SVG top-left origin and downward-positive Y axis; geometry adapters must perform any explicit
mesh-axis flip rather than inventing a second image transform.

## Stages and fitting

- The original-to-normalized matrix implements all eight EXIF orientations, including mirrored
  variants and dimension swaps.
- The crop is stored in normalized pixel-edge coordinates and translated to a local crop origin.
- `contain` uniformly fits all content with padding; `cover` uniformly fills and clips overflow;
  `stretch` scales axes independently; `extend` uses the contain geometry while preserving the
  semantic instruction that padding is editable canvas extension rather than ordinary letterbox.
- The complete working raster maps exactly to configured physical width and height. Pixel bounds
  can therefore be expressed directly in millimetres for later risk, vector, and mesh stages.

Every adjacent and composed mapping is invertible. The serialized contract contains only its
versioned source dimensions, orientation, crop, fit mode, working dimensions, and physical canvas;
derived matrices cannot drift from those inputs. Crop bounds and orientation/dimension consistency
are validated when the contract is loaded.

## Aspect lock

`resize_canvas` models one width or height edit. With aspect lock on, the other dimension changes by
the canvas's current ratio. With it off, the other dimension remains fixed. Values must be positive
and finite; printer/plate maximum validation belongs to the data-driven profile in 24K-40.
