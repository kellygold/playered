# Printer profile catalog

Image23MF keeps printer, nozzle, layer, plate, bed, and art-thickness choices in a versioned data
catalog. The API and web client consume that catalog; adding a supported machine or nozzle must not
require a printer-specific UI branch.

## Bundled P2S profile

`src/image23mf/profiles/catalog-v1.json` currently contains:

- Bambu Lab P2S with a 256 × 256 × 256 mm printable volume;
- installed hardened-steel 0.2 mm and 0.4 mm nozzle variants;
- the exact min/max layer-height ranges and named installed process heights for each variant; and
- the default Textured PEI Plate and its finish metadata.

The machine facts were captured on 2026-07-16 from the official profiles bundled with locally
installed Bambu Studio `02.07.01.62`. The exact source paths are recorded in the catalog itself.
This is intentionally a local, reviewable snapshot rather than runtime coupling to Bambu Studio.

The `thickness_policy` block is an **Image23MF art-plate policy**, not a claim about Bambu hardware.
It provides safe product bounds and defaults for base, artwork, combined thickness, and layer
alignment. Keeping product policy separate from sourced machine facts makes future review explicit.

## API contract

- `GET /api/profiles` returns the complete catalog, including source provenance and defaults.
- `POST /api/profiles/validate` accepts selected profile IDs plus physical dimensions.
- A valid response resolves the selected printer/nozzle/plate and includes a SHA-256 fingerprint of
  the exact catalog.
- An incompatible request returns the standard 422 problem envelope. Every compatibility issue has
  a stable code, field, human-readable message, concrete suggestion, and machine-readable details.

Validation reports independent problems together when possible. For example, an unknown nozzle and
unknown plate are returned in one response, while a layer-height failure includes the supported
range and installed preset suggestions.

Saved job configurations pin the catalog ID/version and both the nozzle ID and resolved physical
diameter. The diameter remains a general positive number rather than a fixed TypeScript/Python
union; profile compatibility is determined from catalog data.

## Updating profiles

1. Inspect the current official Bambu Studio system profiles and record the app version and exact
   source paths.
2. Update only sourced machine facts that changed. Review Image23MF thickness policy separately.
3. Advance `catalog_version` and `captured_on`.
4. Run `tests/test_profiles.py`; intentionally update its reviewed fingerprint only after auditing
   the canonical catalog diff.
5. Run `make check` and verify the built wheel contains `catalog-v1.json`.

Catalog models are frozen and reject unknown fields, duplicate child IDs, missing defaults,
unsorted or out-of-range layer choices, and invalid bed exclusions. This turns a malformed profile
update into a startup/test failure instead of a silent UI discrepancy.
