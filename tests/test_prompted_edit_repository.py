import hashlib
import io
from datetime import datetime, timezone

import pytest
from PIL import Image

from image23mf.contracts.editor import CanvasSelectionState, canonical_fingerprint
from image23mf.contracts.job import CanvasConfig, JobConfig, PaletteColor, PaletteConfig
from image23mf.contracts.prompted import PromptedSessionStatus
from image23mf.contracts.provenance import ConsentEvent, ProviderRegionalEditProvenance
from image23mf.prompted_edits import (
    EgressDisclosure,
    PromptedEditAlternative,
    PromptedEditAsset,
    PromptedEditExecution,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderPrivacy,
)
from image23mf.prompted_edits.repository import (
    PromptedEditConflictError,
    PromptedEditRepository,
    StoredPromptedAlternative,
)
from image23mf.storage import (
    AssetRepository,
    ContentAddressedStore,
    DraftRepository,
    ProjectRepository,
    RevisionPublisher,
    RevisionRepository,
    StaleDraftError,
    open_database,
)


def png(color: tuple[int, int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


def descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id="mock_provider",
        display_name="Mock Provider",
        model_id="mock-edit",
        model_version="2026-07-17",
        capabilities=ProviderCapabilities(
            supports_multiple_alternatives=True,
            maximum_alternatives=4,
        ),
        privacy=ProviderPrivacy(
            policy_version="1",
            data_residency="local test",
            retention="none",
            training_use="none",
        ),
    )


@pytest.mark.parametrize("review_action", ("reject", "accept"))
def test_prompted_session_persists_verified_alternatives_and_finishes_terminally(
    tmp_path, review_action
) -> None:
    store = ContentAddressedStore(tmp_path / "workspace")
    connection = open_database(tmp_path / "workspace" / "image23mf.sqlite3")
    try:
        source_bytes = png((20, 80, 140, 255))
        source_blob = store.put_bytes(
            source_bytes, namespace="assets", extension=".png", media_type="image/png"
        )
        source = AssetRepository(connection, store).register(
            source_blob,
            original_filename="source.png",
            width_px=8,
            height_px=8,
            metadata={"fixture": True},
        )
        project = ProjectRepository(connection).create("Prompted lifecycle")
        config = JobConfig(
            source_asset_id=source.id,
            canvas=CanvasConfig(width_mm=100, height_mm=100),
            palette=PaletteConfig(
                colors=(
                    PaletteColor(id="blue", name="Blue", hex="#14508C"),
                    PaletteColor(id="white", name="White", hex="#FFFFFF"),
                )
            ),
        )
        parent = (
            RevisionPublisher(connection, store)
            .publish(
                project_id=project.id,
                source_asset_id=source.id,
                config=config,
                engine_version="test",
                label="Parent",
            )
            .revision
        )
        DraftRepository(connection).save(
            project_id=project.id,
            config=config,
            base_revision_id=parent.id,
            expected_generation=0,
        )
        mask = store.put_bytes(
            png((255, 255, 255, 255)),
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        provider = descriptor()
        selection = CanvasSelectionState(source_width_px=8, source_height_px=8)
        selection_sha256 = canonical_fingerprint(selection)
        disclosure = EgressDisclosure(
            disclosure_sha256="a" * 64,
            project_id=project.id,
            provider=provider,
            source_sha256=source.sha256,
            mask_sha256=mask.sha256,
            reference_sha256=(),
            prompt="Make the center larger.",
            options={},
            data_categories=(
                "source_pixels",
                "selection_mask",
                "prompt",
                "generation_options",
            ),
        )
        repository = PromptedEditRepository(connection, store)
        prepared = repository.create_prepared(
            project_id=project.id,
            parent_revision_id=parent.id,
            request_sha256="b" * 64,
            selection_sha256=selection_sha256,
            source_asset_id=source.id,
            mask=mask,
            provider=provider,
            disclosure=disclosure,
            request={
                "selection": selection.model_dump(mode="json"),
                "prompt": "Make the center larger.",
                "options": {},
                "alternative_count": 1,
            },
        )
        repository.transition(prepared.id, PromptedSessionStatus.QUEUED)
        repository.transition(prepared.id, PromptedSessionStatus.RUNNING)

        output_bytes = png((30, 90, 150, 255))
        output_blob = store.put_bytes(
            output_bytes, namespace="assets", extension=".png", media_type="image/png"
        )
        output = AssetRepository(connection, store).register(
            output_blob,
            original_filename="alternative-1.png",
            width_px=8,
            height_px=8,
            metadata={"prompted_session_id": prepared.id},
        )
        changed = bytes([1] * 64)
        changed_blob = store.put_bytes(
            changed,
            namespace="artifacts",
            extension=".mask",
            media_type="application/octet-stream",
        )
        changed_preview = store.put_bytes(
            png((255, 255, 255, 255)),
            namespace="artifacts",
            extension=".png",
            media_type="image/png",
        )
        consent = ConsentEvent(
            event_id="consent_repository01",
            recorded_at=datetime.now(timezone.utc),
            disclosure="Approved test pixels.",
            policy_version="1",
        )
        provenance = ProviderRegionalEditProvenance(
            selection_sha256=selection_sha256,
            parent_revision_id=parent.id,
            prompt="Make the center larger.",
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            model_version=provider.model_version,
            options={},
            reference_sha256=(),
            output_sha256=hashlib.sha256(output_bytes).hexdigest(),
            consent_event=consent,
            reproducibility="best_effort",
            reproducibility_reason="Mock provider does not guarantee byte replay.",
        )
        execution = PromptedEditExecution(
            status="complete",
            provider=provider,
            disclosure=disclosure,
            consent_event=consent,
            alternatives=(
                PromptedEditAlternative(
                    index=0,
                    output=PromptedEditAsset.from_bytes(output_bytes, "image/png"),
                    provenance=provenance,
                ),
            ),
            failures=(),
        )
        completed = repository.complete(
            prepared.id,
            execution=execution,
            alternatives=(
                StoredPromptedAlternative(
                    index=0,
                    output_asset=output,
                    changed_mask=changed_blob,
                    changed_mask_preview=changed_preview,
                    changed_pixel_count=64,
                    width_px=8,
                    height_px=8,
                    provenance=provenance,
                ),
            ),
        )

        assert completed.status == PromptedSessionStatus.COMPLETE
        assert completed.alternatives[0].output_sha256 == output.sha256
        assert store.verify(completed.alternatives[0].changed_mask)
        if review_action == "reject":
            terminal = repository.reject(completed.id, reason="Not the direction I want.")
            assert terminal.status == PromptedSessionStatus.REJECTED
            assert terminal.alternatives[0].status.value == "rejected"
        else:
            with pytest.raises(StaleDraftError):
                RevisionPublisher(connection, store).accept_prompted_alternative(
                    project_id=project.id,
                    session_id=completed.id,
                    alternative_id=completed.alternatives[0].id,
                    expected_generation=0,
                    engine_version="test",
                    label="Stale acceptance must roll back",
                )
            unchanged = repository.get(completed.id)
            assert unchanged.status == PromptedSessionStatus.COMPLETE
            assert unchanged.alternatives[0].status.value == "review"
            assert RevisionRepository(connection).get(parent.id).source_asset_id == source.id
            accepted = RevisionPublisher(connection, store).accept_prompted_alternative(
                project_id=project.id,
                session_id=completed.id,
                alternative_id=completed.alternatives[0].id,
                expected_generation=1,
                engine_version="test",
                label="Accepted provider edit",
            )
            terminal = repository.get(completed.id)
            assert terminal.status == PromptedSessionStatus.ACCEPTED
            assert terminal.alternatives[0].status.value == "accepted"
            assert terminal.accepted_revision_id == accepted.revision.id
            assert accepted.revision.parent_revision_id == parent.id
            assert accepted.revision.source_asset_id == output.id
            assert RevisionRepository(connection).get(parent.id).source_asset_id == source.id
            assert accepted.continuation_draft is not None
            assert accepted.continuation_draft.base_revision_id == accepted.revision.id
            assert accepted.continuation_draft.generation == 2
            assert accepted.continuation_draft.config["source_asset_id"] == output.id
            assert accepted.continuation_draft.operations[-1].operation_type == "prompted_edit"
        try:
            repository.transition(terminal.id, PromptedSessionStatus.RUNNING)
        except PromptedEditConflictError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError("terminal prompted session was reopened")
    finally:
        connection.close()
