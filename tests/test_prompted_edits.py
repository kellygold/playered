from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from image23mf.prompted_edits import (
    ConsentDecision,
    ConsentDeniedError,
    PromptedEditAsset,
    PromptedEditCanceledError,
    PromptedEditRequest,
    PromptedEditService,
    ProviderAlternative,
    ProviderAlternativeFailure,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderDescriptor,
    ProviderEditResponse,
    ProviderExecutionError,
    ProviderPrivacy,
    ProviderTimeoutError,
)


def _asset(value: bytes = b"source", media_type: str = "image/png") -> PromptedEditAsset:
    return PromptedEditAsset.from_bytes(value, media_type)


def _descriptor(**capability_overrides) -> ProviderDescriptor:
    capabilities = {
        "supports_mask": True,
        "supports_references": True,
        "supports_multiple_alternatives": True,
        "supports_seed": True,
        "guarantees_seeded_replay": True,
        "maximum_references": 4,
        "maximum_alternatives": 4,
        "accepted_media_types": ("image/png", "image/jpeg"),
        "output_media_types": ("image/png",),
        **capability_overrides,
    }
    return ProviderDescriptor(
        provider_id="mock-provider",
        display_name="Mock Image Lab",
        model_id="mock-image-edit",
        model_version="2026-07-17",
        capabilities=ProviderCapabilities(**capabilities),
        privacy=ProviderPrivacy(
            policy_version="privacy-v1",
            data_residency="Australia",
            retention="Inputs deleted after processing.",
            training_use="none",
            subprocessors=("Mock Compute",),
            policy_url="https://example.test/privacy",
        ),
    )


def _request(**overrides) -> PromptedEditRequest:
    values = {
        "project_id": "project_prompted",
        "parent_revision_id": "revision_parent",
        "selection_sha256": "a" * 64,
        "source": _asset(b"source pixels"),
        "mask": _asset(b"selection mask"),
        "prompt": "Make the selected eyes larger while preserving linework.",
        "references": (_asset(b"reference", "image/jpeg"),),
        "options": {"temperature": 0, "output_format": "png"},
        "seed": 42,
        "alternative_count": 2,
        **overrides,
    }
    return PromptedEditRequest(**values)


class RecordingConsent:
    def __init__(self, granted: bool = True) -> None:
        self.granted = granted
        self.disclosures = []

    def authorize(self, disclosure):
        self.disclosures.append(disclosure)
        if not self.granted:
            return ConsentDecision(granted=False, denial_reason="user_denied")
        return ConsentDecision(
            granted=True,
            event_id="consent_prompted01",
            recorded_at=datetime(2026, 7, 17, 8, tzinfo=timezone.utc),
        )


class MockProvider:
    def __init__(self, response=None, *, error=None, descriptor=None) -> None:
        self._descriptor = descriptor or _descriptor()
        self.response = response or ProviderEditResponse(
            alternatives=(
                ProviderAlternative(index=0, output=_asset(b"alternative zero"), seed=42),
                ProviderAlternative(index=1, output=_asset(b"alternative one"), seed=42),
            )
        )
        self.error = error
        self.calls = []

    @property
    def descriptor(self):
        return self._descriptor

    def edit(self, request, cancellation):
        self.calls.append((request, cancellation))
        if self.error is not None:
            raise self.error
        return self.response


def test_consent_precedes_first_egress_and_builds_exact_seeded_provenance() -> None:
    provider = MockProvider()
    consent = RecordingConsent()
    request = _request()

    result = PromptedEditService((provider,)).execute(
        provider_id="mock-provider",
        request=request,
        consent=consent,
        timeout_seconds=1,
    )

    assert len(consent.disclosures) == 1
    assert len(provider.calls) == 1
    sent, _ = provider.calls[0]
    assert sent.source == request.source
    assert sent.mask == request.mask
    assert sent.prompt == request.prompt
    assert sent.references == request.references
    assert sent.options == request.options
    assert sent.seed == 42
    assert result.status == "complete"
    assert len(result.alternatives) == 2
    provenance = result.alternatives[0].provenance
    assert provenance.reproducibility == "exact_seeded"
    assert provenance.provider_id == "mock-provider"
    assert provenance.model_version == "2026-07-17"
    assert provenance.reference_sha256 == (request.references[0].sha256,)
    assert provenance.output_sha256 == result.alternatives[0].output.sha256
    assert provenance.consent_event.event_id == "consent_prompted01"
    disclosure = consent.disclosures[0]
    assert disclosure.prompt == request.prompt
    assert disclosure.provider.privacy.training_use == "none"
    assert disclosure.data_categories == (
        "source_pixels",
        "selection_mask",
        "prompt",
        "generation_options",
        "references",
    )


def test_provider_method_cannot_begin_before_explicit_consent() -> None:
    events = []

    class OrderedConsent(RecordingConsent):
        def authorize(self, disclosure):
            events.append("consent")
            return super().authorize(disclosure)

    class OrderedProvider(MockProvider):
        def edit(self, request, cancellation):
            assert events == ["consent"]
            events.append("egress")
            return super().edit(request, cancellation)

    PromptedEditService((OrderedProvider(),)).execute(
        provider_id="mock-provider",
        request=_request(),
        consent=OrderedConsent(),
        timeout_seconds=1,
    )

    assert events == ["consent", "egress"]


def test_denied_consent_never_calls_provider() -> None:
    provider = MockProvider()

    with pytest.raises(ConsentDeniedError, match="No image data was sent"):
        PromptedEditService((provider,)).execute(
            provider_id="mock-provider",
            request=_request(),
            consent=RecordingConsent(granted=False),
            timeout_seconds=1,
        )

    assert provider.calls == []


def test_prepare_disclosure_performs_no_egress() -> None:
    provider = MockProvider()
    disclosure = PromptedEditService((provider,)).prepare_disclosure(
        provider_id="mock-provider",
        request=_request(),
    )

    assert disclosure.provider.provider_id == "mock-provider"
    assert provider.calls == []


def test_capability_rejection_happens_before_consent_or_provider() -> None:
    provider = MockProvider(
        descriptor=_descriptor(
            supports_references=False,
            maximum_references=0,
        )
    )
    consent = RecordingConsent()

    with pytest.raises(ProviderCapabilityError, match="reference images"):
        PromptedEditService((provider,)).execute(
            provider_id="mock-provider",
            request=_request(),
            consent=consent,
            timeout_seconds=1,
        )

    assert consent.disclosures == []
    assert provider.calls == []


def test_timeout_sets_provider_cancellation_without_waiting_for_adapter_shutdown() -> None:
    entered = threading.Event()
    cancelled = threading.Event()

    class SlowProvider(MockProvider):
        def edit(self, request, cancellation):
            self.calls.append((request, cancellation))
            entered.set()
            if cancellation.wait(2):
                cancelled.set()
            return self.response

    provider = SlowProvider()
    with pytest.raises(ProviderTimeoutError, match="before timeout"):
        PromptedEditService((provider,)).execute(
            provider_id="mock-provider",
            request=_request(),
            consent=RecordingConsent(),
            timeout_seconds=0.02,
        )

    assert entered.wait(0.2)
    assert cancelled.wait(0.2)


def test_external_cancellation_reaches_provider_adapter() -> None:
    entered = threading.Event()
    cancelled = threading.Event()

    class SlowProvider(MockProvider):
        def edit(self, request, cancellation):
            self.calls.append((request, cancellation))
            entered.set()
            if cancellation.wait(2):
                cancelled.set()
            return self.response

    provider = SlowProvider()
    with pytest.raises(PromptedEditCanceledError):
        PromptedEditService((provider,)).execute(
            provider_id="mock-provider",
            request=_request(),
            consent=RecordingConsent(),
            timeout_seconds=1,
            canceled=lambda: entered.is_set(),
        )

    assert cancelled.wait(0.2)


def test_partial_alternatives_are_preserved_with_stable_failure_codes() -> None:
    response = ProviderEditResponse(
        alternatives=(
            ProviderAlternative(index=0, output=_asset(b"successful alternative"), seed=42),
        ),
        failures=(ProviderAlternativeFailure(index=1, code="filtered", retryable=False),),
    )
    result = PromptedEditService((MockProvider(response),)).execute(
        provider_id="mock-provider",
        request=_request(),
        consent=RecordingConsent(),
        timeout_seconds=1,
    )

    assert result.status == "partial"
    assert [item.index for item in result.alternatives] == [0]
    assert result.failures == response.failures


def test_provider_exception_is_redacted_and_request_secrets_are_rejected() -> None:
    secret = "sk-provider-secret-123456789"
    provider = MockProvider(error=RuntimeError(f"Authorization: Bearer {secret}"))

    with pytest.raises(ProviderExecutionError) as raised:
        PromptedEditService((provider,)).execute(
            provider_id="mock-provider",
            request=_request(),
            consent=RecordingConsent(),
            timeout_seconds=1,
        )
    assert secret not in str(raised.value)
    assert "Authorization" not in str(raised.value)

    with pytest.raises(ValidationError, match="sensitive field"):
        _request(options={"api_key": secret})
    with pytest.raises(ValidationError, match="credential-like"):
        _request(prompt=f"Please send this accidental credential {secret} to the editor.")


def test_nondeterministic_provider_is_truthfully_best_effort() -> None:
    provider = MockProvider(
        response=ProviderEditResponse(
            alternatives=(ProviderAlternative(index=0, output=_asset(b"best effort")),),
            failures=(ProviderAlternativeFailure(index=1, code="unavailable", retryable=True),),
        ),
        descriptor=_descriptor(guarantees_seeded_replay=False),
    )
    result = PromptedEditService((provider,)).execute(
        provider_id="mock-provider",
        request=_request(),
        consent=RecordingConsent(),
        timeout_seconds=1,
    )

    assert result.alternatives[0].provenance.reproducibility == "best_effort"
    assert "does not guarantee" in result.alternatives[0].provenance.reproducibility_reason


def test_asset_hashes_and_consent_decisions_fail_closed() -> None:
    with pytest.raises(ValidationError, match="hash does not match"):
        PromptedEditAsset(sha256="0" * 64, media_type="image/png", data=b"pixels")
    with pytest.raises(ValidationError, match="granted consent requires"):
        ConsentDecision(granted=True)
    with pytest.raises(ValidationError, match="denied consent requires"):
        ConsentDecision(granted=False)
