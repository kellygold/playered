# Prompted-edit provider boundary

The prompted-edit core depends on a Python protocol, not a provider SDK. A provider adapter exposes
static `ProviderDescriptor` metadata and one `edit(request, cancellation)` method. The request
contains only content-addressed source bytes, a lossless selection mask, the prompt, hashed
references, JSON generation options, an optional seed, and the requested alternative count. Local
project IDs, filesystem paths, signed URLs, and credentials are never passed to the provider.

Descriptors declare mask, reference, seed, and multi-alternative support; limits and accepted input
and output media; plus a versioned privacy statement covering residency, retention, training use,
subprocessors, and a public policy URL. Capability mismatch fails locally before consent.

## First-egress consent

`PromptedEditService` builds a deterministic `EgressDisclosure` containing the exact provider/model,
privacy version, content hashes, prompt, options, and data categories. It calls the local
`ConsentAuthorizer` before starting the provider thread. A denial raises `consent_denied` and the
provider method is never called. A grant becomes the durable `ConsentEvent` embedded in every
accepted alternative's regional-edit provenance.

Provider calls run behind a bounded timeout and receive a cancellation event. Provider exceptions
are never interpolated into retained or user-facing messages. Timeouts and execution failures use
stable safe codes. Responses may contain a mix of successful alternatives and typed failures;
successful outputs are preserved with content hashes and honest `exact_seeded` or `best_effort`
provenance.

Adapters own credential acquisition and vendor-specific transport. They must not place credentials,
authorization headers, vendor diagnostic text, or expiring URLs in requests, descriptors, results,
or exceptions intended for persistence. The core independently rejects credential-shaped prompts
and options before consent.
