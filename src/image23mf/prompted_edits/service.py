"""Consent-first orchestration over a provider-neutral prompted-edit protocol."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable
from typing import Protocol

from pydantic import ValidationError

from image23mf.contracts.provenance import ConsentEvent, ProviderRegionalEditProvenance
from image23mf.prompted_edits.models import (
    ConsentDecision,
    EgressDisclosure,
    PromptedEditAlternative,
    PromptedEditExecution,
    PromptedEditRequest,
    ProviderDescriptor,
    ProviderEditRequest,
    ProviderEditResponse,
    disclosure_fingerprint,
)


class PromptedEditProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def edit(
        self, request: ProviderEditRequest, cancellation: threading.Event
    ) -> ProviderEditResponse: ...


class ConsentAuthorizer(Protocol):
    def authorize(self, disclosure: EgressDisclosure) -> ConsentDecision: ...


class PromptedEditError(RuntimeError):
    code = "prompted_edit_failed"
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ProviderNotFoundError(PromptedEditError):
    code = "provider_not_found"


class ProviderCapabilityError(PromptedEditError):
    code = "provider_capability_mismatch"


class ConsentDeniedError(PromptedEditError):
    code = "consent_denied"


class ProviderTimeoutError(PromptedEditError):
    code = "provider_timeout"
    retryable = True


class ProviderExecutionError(PromptedEditError):
    code = "provider_execution_failed"
    retryable = True


class PromptedEditCanceledError(PromptedEditError):
    code = "prompted_edit_canceled"


class ProviderContractError(PromptedEditError):
    code = "provider_contract_invalid"


class PromptedEditService:
    """Validate locally, obtain consent, then make exactly one provider call."""

    def __init__(self, providers: Iterable[PromptedEditProvider]) -> None:
        self._providers = {}
        for provider in providers:
            identifier = provider.descriptor.provider_id
            if identifier in self._providers:
                raise ValueError(f"duplicate prompted-edit provider: {identifier}")
            self._providers[identifier] = provider

    def providers(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(self._providers[key].descriptor for key in sorted(self._providers))

    def prepare_disclosure(
        self,
        *,
        provider_id: str,
        request: PromptedEditRequest,
    ) -> EgressDisclosure:
        """Validate locally and describe egress without calling the provider."""

        provider = self._providers.get(provider_id)
        if provider is None:
            raise ProviderNotFoundError("The requested image-edit provider is not configured.")
        descriptor = provider.descriptor
        self._validate_capabilities(descriptor, request)
        return self._disclosure(descriptor, request)

    def execute(
        self,
        *,
        provider_id: str,
        request: PromptedEditRequest,
        consent: ConsentAuthorizer,
        timeout_seconds: float = 120,
        canceled: Callable[[], bool] | None = None,
    ) -> PromptedEditExecution:
        if timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ProviderNotFoundError("The requested image-edit provider is not configured.")
        descriptor = provider.descriptor
        self._validate_capabilities(descriptor, request)
        disclosure = self._disclosure(descriptor, request)
        decision = consent.authorize(disclosure)
        if not decision.granted:
            raise ConsentDeniedError("No image data was sent because consent was not granted.")
        consent_event = ConsentEvent(
            event_id=decision.event_id,
            recorded_at=decision.recorded_at,
            disclosure=(
                f"Source pixels, selection mask, prompt, generation options"
                f"{', and references' if request.references else ''} were approved for "
                f"{descriptor.display_name} ({descriptor.model_id})."
            ),
            policy_version=descriptor.privacy.policy_version,
        )
        provider_request = ProviderEditRequest(
            source=request.source,
            mask=request.mask,
            prompt=request.prompt,
            references=request.references,
            options=request.options,
            seed=request.seed,
            alternative_count=request.alternative_count,
        )
        response = self._call_provider(
            provider,
            provider_request,
            timeout_seconds=timeout_seconds,
            canceled=canceled,
        )
        self._validate_response(descriptor, request, response)
        reproducibility = (
            "exact_seeded"
            if descriptor.capabilities.guarantees_seeded_replay and request.seed is not None
            else "best_effort"
        )
        reason = (
            "The pinned provider and model version guarantee replay for the recorded seed."
            if reproducibility == "exact_seeded"
            else "The provider does not guarantee byte-identical replay for this request."
        )
        alternatives = tuple(
            PromptedEditAlternative(
                index=item.index,
                output=item.output,
                provenance=ProviderRegionalEditProvenance(
                    selection_sha256=request.selection_sha256,
                    parent_revision_id=request.parent_revision_id,
                    prompt=request.prompt,
                    provider_id=descriptor.provider_id,
                    model_id=descriptor.model_id,
                    model_version=descriptor.model_version,
                    seed=request.seed,
                    options=dict(request.options),
                    reference_sha256=tuple(reference.sha256 for reference in request.references),
                    output_sha256=item.output.sha256,
                    consent_event=consent_event,
                    reproducibility=reproducibility,
                    reproducibility_reason=reason,
                ),
            )
            for item in response.alternatives
        )
        status = (
            "partial"
            if alternatives and response.failures
            else "complete"
            if alternatives
            else "failed"
        )
        return PromptedEditExecution(
            status=status,
            provider=descriptor,
            disclosure=disclosure,
            consent_event=consent_event,
            alternatives=alternatives,
            failures=response.failures,
        )

    @staticmethod
    def _validate_capabilities(
        descriptor: ProviderDescriptor, request: PromptedEditRequest
    ) -> None:
        capabilities = descriptor.capabilities
        inputs = (request.source, request.mask, *request.references)
        if any(item.media_type not in capabilities.accepted_media_types for item in inputs):
            raise ProviderCapabilityError("The provider does not accept one of the image formats.")
        if not capabilities.supports_mask:
            raise ProviderCapabilityError("The provider does not support regional masks.")
        if request.references and not capabilities.supports_references:
            raise ProviderCapabilityError("The provider does not support reference images.")
        if len(request.references) > capabilities.maximum_references:
            raise ProviderCapabilityError("The request exceeds the provider reference limit.")
        if request.seed is not None and not capabilities.supports_seed:
            raise ProviderCapabilityError("The provider does not support explicit seeds.")
        if request.alternative_count > capabilities.maximum_alternatives:
            raise ProviderCapabilityError("The request exceeds the provider alternative limit.")

    @staticmethod
    def _disclosure(
        descriptor: ProviderDescriptor, request: PromptedEditRequest
    ) -> EgressDisclosure:
        categories = ["source_pixels", "selection_mask", "prompt", "generation_options"]
        if request.references:
            categories.append("references")
        payload = {
            "schema_version": 1,
            "project_id": request.project_id,
            "provider_id": descriptor.provider_id,
            "model_id": descriptor.model_id,
            "model_version": descriptor.model_version,
            "privacy_policy_version": descriptor.privacy.policy_version,
            "source_sha256": request.source.sha256,
            "mask_sha256": request.mask.sha256,
            "reference_sha256": [item.sha256 for item in request.references],
            "prompt": request.prompt,
            "options": request.options,
            "data_categories": categories,
        }
        return EgressDisclosure(
            disclosure_sha256=disclosure_fingerprint(payload),
            project_id=request.project_id,
            provider=descriptor,
            source_sha256=request.source.sha256,
            mask_sha256=request.mask.sha256,
            reference_sha256=tuple(item.sha256 for item in request.references),
            prompt=request.prompt,
            options=dict(request.options),
            data_categories=tuple(categories),
        )

    @staticmethod
    def _call_provider(
        provider: PromptedEditProvider,
        request: ProviderEditRequest,
        *,
        timeout_seconds: float,
        canceled: Callable[[], bool] | None = None,
    ) -> ProviderEditResponse:
        provider_cancellation = threading.Event()
        result_queue = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_queue.put((True, provider.edit(request, provider_cancellation)))
            except BaseException:
                # Provider exceptions may contain credentials, URLs, or vendor diagnostics.
                result_queue.put((False, None))

        worker = threading.Thread(target=run, name="prompted-edit-provider", daemon=True)
        worker.start()
        deadline = time.monotonic() + timeout_seconds
        while True:
            if canceled is not None and canceled():
                provider_cancellation.set()
                raise PromptedEditCanceledError("The prompted edit was canceled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                provider_cancellation.set()
                raise ProviderTimeoutError(
                    "The image-edit provider did not respond before timeout."
                )
            try:
                succeeded, raw_response = result_queue.get(timeout=min(0.05, remaining))
                break
            except queue.Empty:
                continue
        if not succeeded:
            raise ProviderExecutionError(
                "The image-edit provider failed without exposing diagnostics."
            )
        try:
            return ProviderEditResponse.model_validate(raw_response)
        except ValidationError as error:
            raise ProviderContractError(
                "The image-edit provider returned an invalid response."
            ) from error

    @staticmethod
    def _validate_response(
        descriptor: ProviderDescriptor,
        request: PromptedEditRequest,
        response: ProviderEditResponse,
    ) -> None:
        positions = [item.index for item in (*response.alternatives, *response.failures)]
        if any(position >= request.alternative_count for position in positions):
            raise ProviderContractError("The provider returned an undeclared alternative position.")
        if len(response.alternatives) > descriptor.capabilities.maximum_alternatives:
            raise ProviderContractError("The provider exceeded its declared alternative limit.")
        if any(
            item.output.media_type not in descriptor.capabilities.output_media_types
            for item in response.alternatives
        ):
            raise ProviderContractError("The provider returned an unsupported output format.")
        hashes = tuple(item.output.sha256 for item in response.alternatives)
        if len(hashes) != len(set(hashes)):
            raise ProviderContractError("The provider returned duplicate alternatives.")
        if (
            descriptor.capabilities.guarantees_seeded_replay
            and request.seed is not None
            and any(item.seed != request.seed for item in response.alternatives)
        ):
            raise ProviderContractError("Seeded provider output did not echo the requested seed.")
