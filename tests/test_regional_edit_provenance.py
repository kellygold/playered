from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from image23mf.contracts.editor import (
    CanvasSelectionState,
    SourceRectangleSelection,
    canonical_fingerprint,
)
from image23mf.contracts.processing import RegionOperationResource
from image23mf.contracts.provenance import (
    ConsentEvent,
    LocalRegionalEditProvenance,
    ProviderRegionalEditProvenance,
    assert_no_sensitive_data,
)


def _selection() -> CanvasSelectionState:
    return CanvasSelectionState(
        source_width_px=1024,
        source_height_px=1024,
        primitives=(
            SourceRectangleSelection(
                primitive_id="selection_provenance01",
                x=10,
                y=20,
                width=120,
                height=80,
            ),
        ),
    )


def _consent() -> ConsentEvent:
    return ConsentEvent(
        event_id="consent_regional01",
        recorded_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        disclosure="Selected pixels, prompt, and hashed references may leave this device.",
        policy_version="regional-edit-v1",
    )


def test_provider_provenance_requires_complete_reproducibility_evidence() -> None:
    selection = _selection()
    provenance = ProviderRegionalEditProvenance(
        selection_sha256=canonical_fingerprint(selection),
        parent_revision_id="revision_parent",
        prompt="Make the selected eyes larger while preserving the ink linework.",
        provider_id="example-provider",
        model_id="image-edit-model",
        model_version="2026-07-01",
        seed=42,
        options={"temperature": 0, "output_format": "png"},
        reference_sha256=("a" * 64,),
        output_sha256="b" * 64,
        consent_event=_consent(),
        reproducibility="exact_seeded",
        reproducibility_reason="Provider documents deterministic seeded output for this version.",
    )

    resource = RegionOperationResource(
        operation_type="regional-model-edit",
        selection={"selection": selection.model_dump(mode="json")},
        parameters={"output_asset_sha256": "b" * 64},
        source="model",
        provenance={"regional_edit": provenance.model_dump(mode="json")},
    )

    assert resource.provenance["regional_edit"]["prompt"].startswith("Make the selected eyes")
    assert resource.provenance["regional_edit"]["consent_event"]["event_id"] == (
        "consent_regional01"
    )

    with pytest.raises(ValidationError, match="recorded seed"):
        ProviderRegionalEditProvenance(
            **{
                **provenance.model_dump(mode="python"),
                "seed": None,
            }
        )


def test_local_provenance_is_bound_to_the_exact_selection() -> None:
    selection = _selection()
    provenance = LocalRegionalEditProvenance(
        selection_sha256=canonical_fingerprint(selection),
        parent_revision_id="revision_parent",
        engine_id="image23mf-local-raster",
        engine_version="1",
        reproducibility_reason="Typed operations replay locally.",
    )
    changed = selection.model_copy(update={"expand_mm": 0.2})

    with pytest.raises(ValidationError, match="fingerprint does not match"):
        RegionOperationResource(
            operation_type="editor_command_v1",
            selection={"selection": changed.model_dump(mode="json")},
            parameters={},
            provenance={"regional_edit": provenance.model_dump(mode="json")},
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "not-for-storage"},
        {"headers": {"Authorization": "Bearer abcdefghijklmnop"}},
        {"value": "sk-examplecredential123456789"},
        {"reference": "https://example.test/output.png?X-Amz-Signature=abc"},
        {"private_key": "anything"},
    ],
)
def test_sensitive_tokens_and_signed_references_are_rejected(payload) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        assert_no_sensitive_data(payload)


def test_safe_hashes_seeds_and_token_counts_are_allowed() -> None:
    assert_no_sensitive_data(
        {
            "reference_sha256": ["a" * 64],
            "seed": 42,
            "options": {"max_output_tokens": 2048},
            "public_url": "https://example.test/reference.png",
        }
    )
