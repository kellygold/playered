# Regional edit provenance

Regional edits use the existing immutable operation envelope, with a strict
`provenance.regional_edit` record. This keeps draft autosave, undo history, published revisions,
and project bundles byte-for-byte aligned without a database migration.

Every regional provenance record is bound to the canonical canvas selection by
`selection_sha256` and records the parent revision. Local edits identify the versioned engine and
are classified as `deterministic`. Provider edits must additionally preserve:

- the exact prompt, provider, model ID, and model version;
- seed and safe generation options;
- content hashes for every reference and the output;
- the timestamped consent event that authorized selected pixels to leave the device; and
- either `exact_seeded` or `best_effort`, with a user-readable reason.

`exact_seeded` is rejected without a seed. A recorded seed does not by itself upgrade a provider
to exact reproducibility; callers must use `best_effort` unless the pinned provider/model version
actually guarantees deterministic seeded output.

## Secret boundary

Operations are scanned before API acceptance, repository persistence, bundle export, and bundle
import. Credentials, authorization headers, private keys, cookies, signed URLs, and tokenized URLs
are rejected. Durable provenance stores content hashes and stable public identifiers, never raw
credentials or expiring download references. The same rule applies to draft and undo-history
snapshots embedded in a bundle.

Older operations without `regional_edit` remain readable. Once that field is present, its schema
is strict and unknown fields are rejected.

The provider-neutral execution and consent flow that produces this metadata is documented in
[prompted-edit-provider-boundary.md](prompted-edit-provider-boundary.md).
