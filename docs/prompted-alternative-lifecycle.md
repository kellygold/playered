# Prompted-edit alternative lifecycle

Prompted alternatives are durable review candidates, not draft mutations. Provider output never
replaces a project source, modifies a parent revision, or enters the normal preview pipeline until
the user explicitly accepts one candidate.

## Authority and identities

One prompted-edit session is bound to:

- one project and immutable parent revision;
- the parent's source asset hash and configuration fingerprint;
- one canonical selection hash and lossless mask hash;
- one provider/model/version descriptor;
- the exact prompt, options, references, seed, and requested alternative count;
- one egress disclosure hash and one explicit consent event; and
- a deterministic request fingerprint over all of the above.

The source and reference images remain normal immutable assets. Selection masks, changed-area
masks, and provider outputs are content-addressed blobs. SQLite stores lifecycle metadata and blob
identities, never image bytes.

## State machine

```text
prepared -> queued -> running -> complete | partial | failed | canceled
                                  |    |
                                  |    +-> rejected
                                  +------> accepted
```

- `prepared` contains disclosure evidence but has made no provider call.
- `queued`/`running` use the local job system and are cancelable.
- `complete` has one or more valid alternatives and no per-position failures.
- `partial` preserves valid alternatives alongside stable per-position failures.
- `failed` and `canceled` never expose an acceptable candidate.
- `rejected` is terminal for the review session but does not delete retained evidence.
- `accepted` names exactly one alternative and exactly one immutable child revision.

Retries create a new session linked to the prior session. They never overwrite the old response or
reuse an old consent event when the disclosure changes.

## Alternative evidence

Each successful candidate retains its ordinal, normalized output asset, full 24K-91 provider
provenance, exact output hash, exact changed-area binary mask and PNG preview, changed-pixel count,
and comparison dimensions. Provider failures retain only a stable non-sensitive code and retryable
flag.

Changed-area evidence compares normalized parent and candidate RGBA pixels at identical
dimensions. A provider output with a different size or invalid image encoding is rejected before
publication. The overlay is review evidence; the candidate image remains the authority.

## Consent and execution

The client first requests a disclosure. The API constructs source/mask/reference assets locally,
validates provider capabilities, and returns the exact disclosure without invoking the provider.
Execution repeats the request plus the disclosure hash and explicit approval. The server recomputes
the disclosure and refuses stale or mismatched approval before any provider thread begins.

No-provider and denied-consent paths remain fully local and produce zero egress. Provider errors are
redacted at the existing adapter boundary.

## Accept and reject

Reject marks the session terminal and leaves its parent, draft, and assets untouched.

Accept is one SQLite transaction that:

1. verifies the session, candidate, blobs, parent revision, and current draft generation;
2. registers or resolves the candidate as an immutable normalized source asset;
3. publishes a child revision whose source is that asset and whose sole regional-edit operation
   carries the candidate's provider provenance;
4. records the accepted session/alternative/revision relationship; and
5. advances the mutable draft to the new child revision with a fresh history lineage.

Any failure rolls back the revision and lifecycle transition together. Accepting twice, accepting a
rejected/failed candidate, cross-project acceptance, or accepting over a stale draft is a conflict.

## UI reuse

The review surface reuses `EditorCanvas` sources, shared camera, fit/zoom, split slider, and
reduced-motion-aware flicker. It adds candidate tabs, changed-area overlay, prompt/options/provider
provenance, per-position failure/retry states, reject, and a guarded accept confirmation. Keyboard
users receive the same candidate selection, comparison modes, consent, reject, and accept actions.

## HTTP workflow

The UI discovers configured adapters with `GET /api/prompted-edit/providers`. A review then uses:

1. `POST /api/projects/{project_id}/prompted-edits/prepare` to validate the immutable parent,
   current preview evidence, selection, provider capability, and disclosure;
2. `POST /api/prompted-edits/{session_id}/execute` with the request/disclosure hashes and explicit
   approval;
3. `GET /api/prompted-edits/{session_id}` while the local worker owns execution;
4. candidate and mask URLs from the returned alternative resources for exact comparison; and
5. either the session `reject` endpoint or the project-scoped alternative `accept` endpoint.

Cancellation is cooperative through the session `cancel` endpoint. `retry` creates a linked,
separately consented session. Project session listing restores review state after a browser restart.
If no adapter is configured, provider discovery returns an empty collection and the editor quietly
keeps all prompted-edit controls unavailable; local editing and export remain unaffected.

## Current boundary

- The application ships the provider-neutral boundary and an injectable mock/test adapter, but no
  network provider is enabled by default.
- Rectangle, lasso, and brush selections use their exact canonical raster mask. Region-list-only
  selections are refused until the provider request can carry the graph's exact region mask.
- Provider output must decode successfully and preserve the source pixel dimensions. Resizing is a
  separate explicit editor operation, never an implicit provider side effect.
- Candidate comparison is raster evidence. Normal quantization, cleanup, geometry, and 3MF build
  only begin after acceptance creates the immutable child revision.
