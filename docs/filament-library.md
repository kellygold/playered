# Filament library

Linear issue: `24K-37`

Palette colors describe intent; filament records describe physical stock. A palette color can be a
free hex color while experimenting, or carry a stable `filament_id` when it is tied to a real spool.
The library persists in SQLite rather than in image files, while project bundles copy only records
actually referenced by the exported project.

## Record and duplicate policy

Each record contains manufacturer, family, display name, six-digit sRGB hex color, material,
finish, owned state, metadata, and timestamps. Inputs are trimmed, material and hex are canonicalized
to uppercase, and identity matching ignores case in manufacturer/family/name. A duplicate create or
identity-changing update returns an explicit conflict without partially changing either record.

List order is deterministic: owned first, then manufacturer, family, name, hex, and opaque ID.
Callers can combine `owned`, exact manufacturer/material, and free-text filters. Custom records are
first-class and do not need catalog provenance.

## Bundled catalog

`bambu-lab-starter` v2026.07.16 contains the four verified colors used by the Wager print workflow:

- Bambu Lab Matte Bone White — `#CBC6B8`
- Bambu Lab Matte Mandarin Orange — `#F99963`
- Bambu Lab Matte Marine Blue — `#0078BF`
- Bambu Lab Matte Charcoal — `#000000`

The catalog JSON is schema-validated, deterministically fingerprinted, and selectively importable.
Unknown entry IDs reject the entire request. Reimport reuses matching rows, merges catalog
provenance, and retains/promotes owned state instead of creating duplicates. This small verified
starter is intentionally not presented as the manufacturer's complete or live catalog.

## Integrity and portability

Draft autosave and immutable revision publication reject missing filament IDs within their existing
atomic transaction. Deleting a referenced color returns a conflict identifying that it is in use.
Bundle manifests already require a closed set of referenced filament records; restore deduplicates
physical identities and rewrites palette IDs before configuration fingerprints are recomputed.

Tests cover custom CRUD, filtering, casing duplicates, atomic collision updates, selected and
idempotent catalog import, unknown catalog entries, save/publish reference validation, guarded
deletion, HTTP user flow, and existing bundle round trips.
