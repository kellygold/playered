# Geometry IR v1

Geometry IR is the canonical boundary between image analysis and output generators. It is not a
slicer format and does not imply that a 3MF exists.

- All physical values are millimetres, quantized to `0.000001 mm` and finite.
- The origin is the lower-left of the build plate. X points right, Y points away from the viewer,
  and Z points up. Source rasters begin upper-left with Y down, so conversion inverts Y about the
  canvas height exactly once.
- Exterior contours are counter-clockwise, holes clockwise, and fills use the non-zero rule.
  Mesh faces are counter-clockwise when viewed from outside.
- Positional/topology comparisons use `0.00001 mm`. Mesh degeneracy is scale-independent: every
  triangle edge and its minimum altitude must both exceed the `0.0001 mm` altitude tolerance.
- V1 boundary contours are closed linear paths. Arc and cubic segments are retained for explicit
  construction/source paths; a later schema may define deterministic curved-boundary flattening.
- Boundary paths are simple polygons with unique vertices and begin at the lexicographically
  smallest vertex. Mesh vertices are lexicographically sorted; each oriented triangle begins with
  its smallest index and triangle records are sorted. Consequently cyclic path starts, mesh vertex
  permutations, triangle order, and cyclic triangle rotations have one canonical identity.
- IDs are schema-version-domain content addresses (`<kind>_<24 hex>`), collections are ID-sorted,
  references are closed, and all contour/material/source/mesh relationships are validated. Text is
  Unicode NFC. Hash input is the documented canonical UTF-8 JSON below, including the version
  domain, so non-Python implementations must reproduce those bytes rather than native JSON defaults.
- Every hole lies strictly inside its exterior and holes are disjoint. Filled island regions may
  touch but never overlap. Shared boundaries are intersected into canonical atomic segments, so
  subdivided and within-positional-tolerance edges are supported; this includes exterior-to-hole
  adjacency. Shared-edge declarations are exhaustive, not optional metadata.
- Shared edges store two sorted island IDs. The lexicographically first island is the sole owner;
  endpoints are stored in ascending lexicographic order and edge geometry cannot be duplicated.
- Rectangle, circle, and custom-contour bases are explicit tagged variants. Custom-base contours
  are base-owned provenance and cannot simultaneously belong to an artwork island.
- Each mesh is owned by exactly one part, no mesh is orphaned, and mesh output contains exactly one
  base part plus one part per island. Part material, label, role/classification, and source geometry
  must agree exactly with their lineage.
- The document and every source label carry the SHA-256 of the authoritative processed-label
  artifact. Each label directly names one palette color and material; those references must agree,
  every label must appear in an island, and a palette color cannot ambiguously map to two materials.
  `background` labels produce artwork islands in v1; base geometry is not a source-label class.
- Capability states distinguish available, unavailable, not-requested, and failed work across the
  explicit geometry-IR, vector-geometry, topology, mesh, 3MF, slicer-validation, and download
  stages. Available states require an artifact SHA-256; downstream stages cannot claim availability
  before dependencies. Vector and topology status must agree with path and contour/island content,
  while mesh availability must agree with actual mesh and part evidence.
- Canonical transport is UTF-8 JSON with sorted keys and no insignificant whitespace, duplicate
  keys, non-finite numbers, unknown fields, or unknown schema versions. Canonical bytes define
  equality, fingerprints, and namespaced cache keys.
- Cross-language emitters sort object keys by Unicode code point, preserve array order, emit UTF-8
  strings with JSON control-character escaping and without ASCII-only escaping, and emit physical
  floats as fixed-point decimals with at most six fractional digits (trailing zeroes and a trailing
  decimal point removed, with negative zero emitted as `0`). Integers never carry a decimal point.
- The wire parser rejects documents larger than 256 MiB, JSON nesting deeper than 128 levels, and
  bounded entity/segment/vertex/triangle collections above their schema limits before downstream
  processing. Parser failures are always reported as `GeometryIRDecodeError`.

Use `dump_geometry_ir` and `load_geometry_ir`; do not serialize models with ad-hoc JSON settings.
