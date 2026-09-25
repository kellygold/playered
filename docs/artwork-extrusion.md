# Artwork extrusion contract

Linear: `24K-63`

`image23mf.geometry.extrusion` turns validated Geometry IR v1 islands into physical,
per-color solids. It does not infer regions from raster pixels and does not generate the
structural base.

## Z strategies

Both strategies require `base_top_z_mm` to be an exact whole multiple of
`layer_height_mm`. Every emitted artwork mesh begins at that exact Z plane, so the
structural base and artwork touch without positive-volume overlap.

- `flush_inlay`: every present label, including `background`, receives the same positive
  whole-layer thickness. Together the color parts form one flush colored layer above the
  structural substrate.
- `shallow_raised`: every present label receives a positive foundation. `artwork` and
  `support` labels receive additional whole layers; `background` remains at the flush
  foundation height.

The Geometry IR contract requires every source label to own at least one island, but it
allows palette materials that are absent from the processed label field. Present islands
produce exactly one mesh and part each. Absent materials produce explicit
`MaterialCoverage(present=False)` evidence and no empty or zero-volume object.

## Topology and interfaces

- Exterior paths are consumed counter-clockwise and holes clockwise.
- Concave polygons and multiple holes are triangulated deterministically.
- Every solid is independently watertight, consistently outward-wound, and checked for
  positive volume equal to filled 2D area times physical height.
- Atomic `SharedEdge` endpoints are inserted into both adjacent contours before meshing.
  A subdivided boundary therefore cannot leave an unmatched T-junction in its neighbor.
- Adjacent material solids necessarily carry opposite faces on their common vertical
  interface so that each remains independently watertight. These zero-volume contacts are
  the only intentional coplanar interfaces and are recorded as `SharedInterfaceEvidence`
  with canonical ownership.

There can be no unintended volumetric or coplanar face overlap between artwork parts:
Geometry IR validation forbids filled-region overlap and requires shared-edge declarations
to exhaust every boundary overlap; direct Z extrusion preserves that 2D partition. During
assembly, the structural base's maximum Z must equal the common artwork minimum Z, so the
base and artwork interiors are disjoint as well.

## Provenance and assembly

Each output `Part` references exactly one source island, source label, material, and
content-addressed mesh. `ArtworkExtrusionResult.source_geometry_fingerprint` binds the
result to the exact topology document, and the result fingerprint covers meshes, parts,
physical evidence, material coverage, shared interfaces, and the chosen strategy.

`assemble_mesh_document` accepts any independently generated canonical base `Mesh` and
base `Part`, proves exact Z contact and provenance, computes a deterministic combined mesh
artifact SHA-256, and returns a fully revalidated `GeometryDocument` with mesh capability
available and downstream package/slicer/download capabilities reset to not requested.

## Verification

```shell
.venv/bin/pytest -q tests/test_artwork_extrusion.py
```

The adversarial suite covers flush and raised layers, absent colors, concavity, holes,
subdivided shared boundaries, non-aligned heights, below-tolerance slivers, deterministic
identity, exact volume, exact base contact, and complete Geometry IR provenance.
