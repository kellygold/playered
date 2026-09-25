# Image ingestion contract

Linear issue: `24K-34`

The engine accepts still PNG, JPEG, and WebP bytes. It never trusts a filename extension: the byte
signature selects an allow-listed decoder and the decoder's reported format must agree. Empty,
unsupported, corrupt, truncated, animated, oversized, and unsafe decompression inputs fail with a
stable error code, a human message, a suggested recovery, and measured limit details where useful.

## Safety sequence

1. Enforce encoded-byte limits before constructing a decoder.
2. Identify an allow-listed signature (PNG/JPEG/WebP).
3. Read dimensions/frame count and enforce width, height, and total-pixel limits from the header.
4. Verify the complete encoded stream without retaining decoded pixels.
5. Bound embedded ICC profile bytes, reject animation, then decode.
6. Apply EXIF orientation, convert embedded ICC color to the canonical sRGB working space (or
   record the explicit no-profile conversion), preserve real transparency, and encode a
   deterministic RGBA PNG.

The default limits are 64 MiB encoded bytes, 50 million pixels, 32,768 pixels per dimension, and
4 MiB of ICC data. Callers can lower or deliberately raise the bounded values through the frozen
`ImageIngestionLimits` contract; Pillow's own decompression safety ceiling remains an independent
upper guard.

## Provenance

`SourceImageMetadata` records schema version, safe original filename and declared extension,
detected format/media type, source bytes/dimensions/mode/hash, frame count, EXIF orientation,
profile presence/hash/conversion, alpha/transparency, and the normalized PNG dimensions/mode/hash.
The normalized bytes and metadata are returned together and are ready for the content-addressed
asset store; persistence is added at the processing API boundary in 24K-41.
