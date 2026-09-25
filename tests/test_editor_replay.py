from __future__ import annotations

import hashlib

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from image23mf.contracts.editor import (
    ContourCleanupCommand,
    EditorCommandSequence,
    EditorReplayRecord,
    GraphSelector,
    HoleCorrectionCommand,
    HoleSelector,
    IslandMergeCommand,
    IslandSelector,
    KeepRegionsCommand,
    RegionOperationCommand,
    RegionOperationKind,
    RegionScope,
    RegionScopeMode,
    RegionSelector,
    ScopedRegionSelector,
    SimilarRegionPredicate,
    ThickenRegionCommand,
    derive_command_id,
    derive_region_scope_fingerprint,
)
from image23mf.editor.replay import (
    EditorReplayError,
    IncompatibleSelectorError,
    LegacyOperationError,
    NoOpOperationError,
    ProtectedPixelError,
    StaleSelectorError,
    command_to_storage_payload,
    load_persisted_commands,
    replay_editor_commands,
    validate_persisted_sequence,
)
from image23mf.engine.contours import (
    ContourCleanupRequest,
    ContourOperation,
    ContourOperationKind,
)
from image23mf.engine.holes import (
    HoleAnalysisOptions,
    HoleCorrectionPolicy,
    classify_holes,
)
from image23mf.engine.islands import (
    IslandAnalysisOptions,
    IslandMergePolicy,
    classify_small_islands,
)
from image23mf.engine.labels import LabelField
from image23mf.engine.regions import analyze_regions

CONFIG = hashlib.sha256(b"editor-config").hexdigest()
OTHER_CONFIG = hashlib.sha256(b"other-config").hexdigest()
COLORS = {0: "#FFFFFF", 1: "#000000", 2: "#FF6600"}
COMMAND_METADATA = {
    "created_at": "2026-07-16T03:30:00Z",
    "provenance": {"ui": "test-editor", "gesture": "explicit"},
}


def _fixture(
    rows: list[list[int]],
    *,
    labels: tuple[int, ...] = (0, 1),
    active: bytes | None = None,
):
    array = np.asarray(rows, dtype=np.uint8)
    field = LabelField(
        width=array.shape[1],
        height=array.shape[0],
        label_values=labels,
        pixels=array.tobytes(),
    )
    active = active or bytes([1]) * array.size
    analysis = analyze_regions(
        field,
        colors={label: COLORS[label] for label in labels},
        width_mm=field.width,
        height_mm=field.height,
        active=active,
    )
    return field, active, analysis


def _replay(field, active, commands=()):
    return replay_editor_commands(
        field,
        active=active,
        colors={label: COLORS[label] for label in field.label_values},
        width_mm=field.width,
        height_mm=field.height,
        config_fingerprint=CONFIG,
        commands=commands,
    )


def _region_selector(analysis, *region_ids: str, config: str = CONFIG) -> RegionSelector:
    return RegionSelector(
        graph_fingerprint=analysis.graph.fingerprint(),
        config_fingerprint=config,
        region_ids=tuple(sorted(region_ids)),
    )


def _scoped_selector(
    analysis,
    *anchor_region_ids: str,
    mode: RegionScopeMode = RegionScopeMode.SELECTED,
    predicate: SimilarRegionPredicate | None = None,
    resolved_region_ids: tuple[str, ...] | None = None,
) -> ScopedRegionSelector:
    anchors = tuple(sorted(anchor_region_ids))
    resolved = tuple(sorted(resolved_region_ids or anchors))
    scope = RegionScope(
        mode=mode,
        anchor_region_ids=anchors,
        predicate=predicate,
        resolved_region_ids=resolved,
        resolution_fingerprint="0" * 64,
    )
    scope = scope.model_copy(
        update={
            "resolution_fingerprint": derive_region_scope_fingerprint(
                analysis.graph.fingerprint(), scope
            )
        }
    )
    return ScopedRegionSelector(
        graph_fingerprint=analysis.graph.fingerprint(),
        config_fingerprint=CONFIG,
        scope=scope,
    )


def _island_options() -> IslandAnalysisOptions:
    return IslandAnalysisOptions(
        minimum_area_mm2=2,
        minimum_equivalent_diameter_mm=1.5,
        preserve_long_lines=True,
        long_line_minimum_aspect_ratio=6,
        long_line_minimum_length_mm=4,
    )


def _hole_options() -> HoleAnalysisOptions:
    return HoleAnalysisOptions(
        maximum_area_mm2=2,
        maximum_equivalent_diameter_mm=2,
        minimum_surviving_ring_width_mm=2,
    )


def test_empty_and_repeated_replay_are_byte_and_contract_deterministic() -> None:
    field, active, _analysis = _fixture([[0, 1], [1, 0]])

    first = _replay(field, active)
    second = _replay(field, active)

    assert first.labels == field
    assert first.active == active
    assert first.changed_mask == bytes(4)
    assert first.record == second.record
    assert first.fingerprint() == second.fingerprint()
    assert first.record.command_count == 0
    assert first.record.changed_pixel_count == 0
    assert EditorReplayRecord.model_validate_json(first.record.canonical_json()) == first.record


def test_island_command_recomputes_bound_classification_and_records_exact_lineage() -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[2, 2] = 1
    field, active, analysis = _fixture(rows.tolist())
    classification = classify_small_islands(analysis, options=_island_options())
    island = next(item for item in classification.candidates if item.label == 1)
    command = IslandMergeCommand(
        **COMMAND_METADATA,
        command_id="cmd_island01",
        selector=IslandSelector(
            **_region_selector(analysis, island.region_id).model_dump(),
            classification_fingerprint=classification.fingerprint(),
        ),
        analysis_options=classification.options,
        policy=IslandMergePolicy.DOMINANT_NEIGHBOR,
    )
    result = _replay(field, active, (command,))

    assert set(result.labels.pixels) == {0}
    assert result.record.changed_pixel_count == 1
    assert sum(result.changed_mask) == 1
    assert result.record.steps[0].engine_record_fingerprint is not None
    assert result.record.steps[0].lineage.after_graph_fingerprint == (
        result.analysis.graph.fingerprint()
    )
    assert result.record.transitions[0].source_label == 1
    assert result.record.transitions[0].target_label == 0


def test_transparent_hole_fill_records_activation_as_a_typed_transition() -> None:
    rows = np.zeros((9, 9), dtype=np.uint8)
    rows[2:7, 2:7] = 1
    rows[4, 4] = 0
    active_array = np.ones(rows.shape, dtype=np.uint8)
    active_array[4, 4] = 0
    field, active, analysis = _fixture(rows.tolist(), active=active_array.tobytes())
    classification = classify_holes(analysis, options=_hole_options())
    feature = classification.features[0]
    command = HoleCorrectionCommand(
        **COMMAND_METADATA,
        command_id="cmd_holefill1",
        selector=HoleSelector(
            graph_fingerprint=analysis.graph.fingerprint(),
            config_fingerprint=CONFIG,
            feature_ids=(feature.id,),
            classification_fingerprint=classification.fingerprint(),
        ),
        analysis_options=classification.options,
        policy=HoleCorrectionPolicy.FILL_HOLE,
    )

    result = _replay(field, active, (command,))

    assert result.active[4 * field.width + 4] == 1
    assert result.record.changed_pixel_count == 1
    transition = result.record.transitions[0]
    assert not transition.source_active
    assert transition.source_label is None
    assert transition.target_active
    assert transition.target_label == 1


def test_thicken_is_physically_bounded_and_preserves_noneditable_labels() -> None:
    rows = np.zeros((7, 7), dtype=np.uint8)
    rows[3, 3] = 1
    rows[3, 4] = 2
    field, active, analysis = _fixture(rows.tolist(), labels=(0, 1, 2))
    source = next(region for region in analysis.graph.regions if region.label == 1)
    command = ThickenRegionCommand(
        **COMMAND_METADATA,
        command_id="cmd_thicken1",
        selector=_region_selector(analysis, source.id),
        radius_mm=1,
        editable_labels=(0,),
    )

    result = _replay(field, active, (command,))
    output = np.frombuffer(result.labels.pixels, dtype=np.uint8).reshape(rows.shape)

    assert output[3, 4] == 2
    assert output[3, 3] == 1
    assert output[2, 3] == output[4, 3] == output[3, 2] == 1
    assert result.record.changed_pixel_count == 3
    assert result.record.changed_area_mm2 == 3


def test_keep_regions_protects_exact_pixels_from_later_destructive_commands() -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[2, 2] = 1
    field, active, analysis = _fixture(rows.tolist())
    dot = next(region for region in analysis.graph.regions if region.label == 1)
    keep = KeepRegionsCommand(
        **COMMAND_METADATA,
        command_id="cmd_keep0001",
        selector=_region_selector(analysis, dot.id),
        reason="This dot is intentionally printable.",
    )
    cleanup = ContourCleanupCommand(
        **COMMAND_METADATA,
        command_id="cmd_cleanup01",
        selector=GraphSelector(
            graph_fingerprint=analysis.graph.fingerprint(),
            config_fingerprint=CONFIG,
        ),
        request=ContourCleanupRequest(
            operations=(
                ContourOperation(
                    kind=ContourOperationKind.OPEN,
                    radius_mm=1,
                    subject_label=1,
                    replacement_label=0,
                ),
            )
        ),
    )

    with pytest.raises(ProtectedPixelError, match="1 explicitly protected") as caught:
        _replay(field, active, (keep, cleanup))
    assert caught.value.command_id == cleanup.command_id


def test_scoped_protect_is_exact_and_rejects_duplicate_or_destructive_replay() -> None:
    field, active, analysis = _fixture([[0, 0, 0], [0, 1, 0], [0, 0, 0]])
    dot = next(region for region in analysis.graph.regions if region.label == 1)
    protect = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_scopeprotect",
        selector=_scoped_selector(analysis, dot.id),
        operation=RegionOperationKind.PROTECT,
    )
    recolor = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_scoperecolor",
        selector=_scoped_selector(analysis, dot.id),
        operation=RegionOperationKind.RECOLOR,
        target_label=0,
    )

    protected = _replay(field, active, (protect,))
    assert protected.protected_mask == bytes([0, 0, 0, 0, 1, 0, 0, 0, 0])
    assert protected.record.steps[0].changed_pixel_count == 0
    assert protected.record.steps[0].newly_protected_pixel_count == 1
    with pytest.raises(NoOpOperationError, match="already protected"):
        _replay(field, active, (protect, protect.model_copy(update={"command_id": "cmd_again000"})))
    with pytest.raises(ProtectedPixelError, match="1 explicitly protected"):
        _replay(field, active, (protect, recolor))


def test_protected_pixels_block_every_scoped_destructive_operation_atomically() -> None:
    field, active, analysis = _fixture([[0, 1, 0]])
    dot = next(region for region in analysis.graph.regions if region.label == 1)
    background = next(region for region in analysis.graph.regions if region.label == 0)
    protect_dot = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_protectdot",
        selector=_scoped_selector(analysis, dot.id),
        operation=RegionOperationKind.PROTECT,
    )
    destructive = (
        RegionOperationCommand(
            **COMMAND_METADATA,
            command_id="cmd_blockmerge",
            selector=_scoped_selector(analysis, dot.id),
            operation=RegionOperationKind.MERGE,
            target_label=0,
        ),
        RegionOperationCommand(
            **COMMAND_METADATA,
            command_id="cmd_blockdelete",
            selector=_scoped_selector(analysis, dot.id),
            operation=RegionOperationKind.DELETE,
        ),
        RegionOperationCommand(
            **COMMAND_METADATA,
            command_id="cmd_blockrecolor",
            selector=_scoped_selector(analysis, dot.id),
            operation=RegionOperationKind.RECOLOR,
            target_label=0,
        ),
    )
    for command in destructive:
        with pytest.raises(ProtectedPixelError, match="1 explicitly protected"):
            _replay(field, active, (protect_dot, command))

    protect_background = protect_dot.model_copy(
        update={
            "command_id": "cmd_protectbackground",
            "selector": _scoped_selector(analysis, background.id),
        }
    )
    thicken = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_blockthicken",
        selector=_scoped_selector(analysis, dot.id),
        operation=RegionOperationKind.THICKEN,
        radius_mm=1,
        editable_labels=(0,),
    )
    with pytest.raises(ProtectedPixelError, match="explicitly protected"):
        _replay(field, active, (protect_background, thicken))


def test_region_merge_recolor_and_delete_have_distinct_exact_semantics() -> None:
    field, active, analysis = _fixture([[0, 1, 0, 2]], labels=(0, 1, 2))
    middle = next(region for region in analysis.graph.regions if region.label == 1)
    invalid_merge = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_mergefar1",
        selector=_scoped_selector(analysis, middle.id),
        operation=RegionOperationKind.MERGE,
        target_label=2,
    )
    recolor = invalid_merge.model_copy(
        update={"command_id": "cmd_recolor01", "operation": RegionOperationKind.RECOLOR}
    )
    merge = invalid_merge.model_copy(update={"command_id": "cmd_mergenear", "target_label": 0})
    delete = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_delete001",
        selector=_scoped_selector(analysis, middle.id),
        operation=RegionOperationKind.DELETE,
    )

    with pytest.raises(EditorReplayError, match="not adjacent"):
        _replay(field, active, (invalid_merge,))
    recolored = _replay(field, active, (recolor,))
    assert recolored.labels.pixels == bytes([0, 2, 0, 2])
    assert recolored.active == active
    merged = _replay(field, active, (merge,))
    assert merged.labels.pixels == bytes([0, 0, 0, 2])
    assert len([region for region in merged.analysis.graph.regions if region.label == 0]) == 1
    deleted = _replay(field, active, (delete,))
    assert deleted.labels.pixels == field.pixels
    assert deleted.active == bytes([1, 0, 1, 1])
    assert deleted.analysis.graph.active_pixel_count == 3
    transition = deleted.record.transitions[0]
    assert transition.source_active and transition.source_label == 1
    assert not transition.target_active and transition.target_label is None


def test_apply_to_similar_recomputes_exact_scope_and_replays_deterministically() -> None:
    rows = np.zeros((7, 9), dtype=np.uint8)
    rows[2, 2] = 1
    rows[2, 6] = 1
    rows[5, 4] = 2
    field, active, analysis = _fixture(rows.tolist(), labels=(0, 1, 2))
    label_one = tuple(region for region in analysis.graph.regions if region.label == 1)
    predicate = SimilarRegionPredicate(
        area_ratio_tolerance=0,
        minimum_width_ratio_tolerance=0,
        compactness_tolerance=0,
        match_border_contact=True,
        match_neighbor_labels=True,
    )
    selector = _scoped_selector(
        analysis,
        label_one[0].id,
        mode=RegionScopeMode.SIMILAR,
        predicate=predicate,
        resolved_region_ids=tuple(region.id for region in label_one),
    )
    command = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_similar01",
        selector=selector,
        operation=RegionOperationKind.RECOLOR,
        target_label=2,
    )

    first = _replay(field, active, (command,))
    second = _replay(field, active, (command,))
    output = np.frombuffer(first.labels.pixels, dtype=np.uint8).reshape(rows.shape)
    assert output[2, 2] == output[2, 6] == 2
    assert output[5, 4] == 2
    assert first.record.changed_pixel_count == 2
    assert first.fingerprint() == second.fingerprint()
    assert command_to_storage_payload(command)["parameters"]["command"]["selector"]["scope"][
        "resolved_region_ids"
    ] == sorted(region.id for region in label_one)
    assert load_persisted_commands((command_to_storage_payload(command),)) == (command,)


def test_region_scope_fingerprint_has_a_cross_language_golden_vector() -> None:
    scope = RegionScope(
        mode=RegionScopeMode.SIMILAR,
        anchor_region_ids=("region_111111111111111111111111",),
        predicate=SimilarRegionPredicate(
            area_ratio_tolerance=0.25,
            minimum_width_ratio_tolerance=0.5,
            compactness_tolerance=0.125,
            match_border_contact=True,
            match_neighbor_labels=False,
        ),
        resolved_region_ids=(
            "region_111111111111111111111111",
            "region_222222222222222222222222",
        ),
        resolution_fingerprint="0" * 64,
    )

    assert derive_region_scope_fingerprint("a" * 64, scope) == (
        "1aa11bab4bcdc2555a4d827ed8d780f84819b07b8e6726d38ae2b98e4aebaff6"
    )

    endpoint_scope = scope.model_copy(
        update={
            "predicate": SimilarRegionPredicate(
                area_ratio_tolerance=0,
                minimum_width_ratio_tolerance=1,
                compactness_tolerance=0,
                match_border_contact=False,
                match_neighbor_labels=True,
            ),
            "resolved_region_ids": ("region_111111111111111111111111",),
        }
    )
    assert derive_region_scope_fingerprint("a" * 64, endpoint_scope) == (
        "f9b6acee2e653a38085748e5f7ede8bae595fb6ad4432d44d55f95022ca77c2d"
    )


def test_region_operation_contract_rejects_ambiguous_or_noncanonical_payloads() -> None:
    field, active, analysis = _fixture([[0, 1]])
    selected = next(region for region in analysis.graph.regions if region.label == 1)
    selector = _scoped_selector(analysis, selected.id)
    base = {
        **COMMAND_METADATA,
        "command_id": "cmd_invalid01",
        "selector": selector,
    }

    with pytest.raises(ValidationError, match="requires a target label"):
        RegionOperationCommand(**base, operation=RegionOperationKind.MERGE)
    with pytest.raises(ValidationError, match="does not accept mutation parameters"):
        RegionOperationCommand(
            **base,
            operation=RegionOperationKind.DELETE,
            target_label=0,
        )
    with pytest.raises(ValidationError, match="requires a radius and editable labels"):
        RegionOperationCommand(**base, operation=RegionOperationKind.THICKEN)
    with pytest.raises(ValidationError, match="fingerprint does not match"):
        ScopedRegionSelector(
            graph_fingerprint=analysis.graph.fingerprint(),
            config_fingerprint=CONFIG,
            scope=selector.scope.model_copy(update={"resolution_fingerprint": "f" * 64}),
        )
    with pytest.raises(ValidationError, match="multiple of 0.001"):
        SimilarRegionPredicate(area_ratio_tolerance=0.0005)

    same_label = RegionOperationCommand(
        **base,
        operation=RegionOperationKind.RECOLOR,
        target_label=1,
    )
    unknown_label = same_label.model_copy(update={"command_id": "cmd_invalid02", "target_label": 2})
    with pytest.raises(NoOpOperationError, match="already owns"):
        _replay(field, active, (same_label,))
    with pytest.raises(EditorReplayError, match="absent from the palette"):
        _replay(field, active, (unknown_label,))


def test_apply_to_similar_rejects_tampered_resolution_even_when_self_consistent() -> None:
    field, active, analysis = _fixture([[0, 1, 0, 1, 0]])
    regions = tuple(region for region in analysis.graph.regions if region.label == 1)
    predicate = SimilarRegionPredicate(
        area_ratio_tolerance=0,
        minimum_width_ratio_tolerance=0,
        compactness_tolerance=0,
    )
    selector = _scoped_selector(
        analysis,
        regions[0].id,
        mode=RegionScopeMode.SIMILAR,
        predicate=predicate,
        resolved_region_ids=(regions[0].id,),
    )
    command = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_stalescope",
        selector=selector,
        operation=RegionOperationKind.DELETE,
    )

    with pytest.raises(StaleSelectorError, match="stale scope resolution"):
        _replay(field, active, (command,))


def test_scoped_thicken_expands_every_resolved_region_and_rejects_true_noops() -> None:
    rows = np.zeros((5, 9), dtype=np.uint8)
    rows[2, 2] = 1
    rows[2, 6] = 1
    field, active, analysis = _fixture(rows.tolist())
    regions = tuple(region for region in analysis.graph.regions if region.label == 1)
    predicate = SimilarRegionPredicate(
        area_ratio_tolerance=0,
        minimum_width_ratio_tolerance=0,
        compactness_tolerance=0,
    )
    thicken = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_scopethick",
        selector=_scoped_selector(
            analysis,
            regions[0].id,
            mode=RegionScopeMode.SIMILAR,
            predicate=predicate,
            resolved_region_ids=tuple(region.id for region in regions),
        ),
        operation=RegionOperationKind.THICKEN,
        radius_mm=1,
        editable_labels=(0,),
    )
    result = _replay(field, active, (thicken,))
    assert result.record.changed_pixel_count == 8
    assert set(result.labels.pixels) == {0, 1}

    solid, solid_active, solid_analysis = _fixture([[1]], labels=(0, 1))
    source = solid_analysis.graph.regions[0]
    no_op = RegionOperationCommand(
        **COMMAND_METADATA,
        command_id="cmd_thicknoop",
        selector=_scoped_selector(solid_analysis, source.id),
        operation=RegionOperationKind.THICKEN,
        radius_mm=1,
        editable_labels=(0,),
    )
    with pytest.raises(NoOpOperationError, match="no editable pixel"):
        _replay(solid, solid_active, (no_op,))

    legacy_no_op = ThickenRegionCommand(
        **COMMAND_METADATA,
        command_id="cmd_legacynoop",
        selector=_region_selector(solid_analysis, source.id),
        radius_mm=1,
        editable_labels=(0,),
    )
    with pytest.raises(NoOpOperationError, match="no editable pixel"):
        _replay(solid, solid_active, (legacy_no_op,))


def test_sequential_selectors_must_bind_to_the_graph_at_their_exact_position() -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[2, 2] = 1
    field, active, analysis = _fixture(rows.tolist())
    dot = next(region for region in analysis.graph.regions if region.label == 1)
    thicken = ThickenRegionCommand(
        **COMMAND_METADATA,
        command_id="cmd_order001",
        selector=_region_selector(analysis, dot.id),
        radius_mm=1,
        editable_labels=(0,),
    )
    first = _replay(field, active, (thicken,))
    expanded = next(region for region in first.analysis.graph.regions if region.label == 1)
    keep_fresh = KeepRegionsCommand(
        **COMMAND_METADATA,
        command_id="cmd_order002",
        selector=_region_selector(first.analysis, expanded.id),
    )
    keep_stale = keep_fresh.model_copy(update={"selector": _region_selector(analysis, dot.id)})

    result = _replay(field, active, (thicken, keep_fresh))
    assert [step.command_id for step in result.record.steps] == [
        "cmd_order001",
        "cmd_order002",
    ]
    assert result.record.protected_pixel_count == 5
    with pytest.raises(StaleSelectorError, match="stale graph fingerprint"):
        _replay(field, active, (thicken, keep_stale))


def test_stale_config_and_classification_bindings_are_explicit_errors() -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[2, 2] = 1
    field, active, analysis = _fixture(rows.tolist())
    dot = next(region for region in analysis.graph.regions if region.label == 1)
    keep = KeepRegionsCommand(
        **COMMAND_METADATA,
        command_id="cmd_stale001",
        selector=_region_selector(analysis, dot.id, config=OTHER_CONFIG),
    )
    with pytest.raises(ValidationError, match="sequence config"):
        EditorCommandSequence(config_fingerprint=CONFIG, commands=(keep,))
    with pytest.raises(StaleSelectorError, match="stale config fingerprint"):
        _replay(field, active, (keep,))

    classification = classify_small_islands(analysis, options=_island_options())
    command = IslandMergeCommand(
        **COMMAND_METADATA,
        command_id="cmd_stale002",
        selector=IslandSelector(
            **_region_selector(analysis, dot.id).model_dump(),
            classification_fingerprint="0" * 64,
        ),
        analysis_options=classification.options,
        policy=IslandMergePolicy.DOMINANT_NEIGHBOR,
    )
    with pytest.raises(StaleSelectorError, match="stale island classification"):
        _replay(field, active, (command,))


@given(
    candidate=st.from_regex(r"[0-9a-f]{64}", fullmatch=True).filter(lambda value: value != CONFIG)
)
@settings(max_examples=30, deadline=None)
def test_persisted_sequence_rejects_every_different_candidate_config(candidate: str) -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[2, 2] = 1
    _field, _active, analysis = _fixture(rows.tolist())
    dot = next(region for region in analysis.graph.regions if region.label == 1)
    command = KeepRegionsCommand(
        **COMMAND_METADATA,
        command_id="cmd_config_property",
        selector=_region_selector(analysis, dot.id),
    )

    with pytest.raises(IncompatibleSelectorError) as caught:
        validate_persisted_sequence(
            (command_to_storage_payload(command),),
            config_fingerprint=candidate,
        )

    assert caught.value.operation_count == 1
    assert caught.value.conflicts == ((command.command_id, CONFIG),)


def test_sequence_reports_one_incompatible_selector_among_multiple_exact_commands() -> None:
    rows = np.zeros((5, 5), dtype=np.uint8)
    rows[2, 2] = 1
    _field, _active, analysis = _fixture(rows.tolist())
    dot = next(region for region in analysis.graph.regions if region.label == 1)
    stale = KeepRegionsCommand(
        **COMMAND_METADATA,
        command_id="cmd_config_stale",
        selector=_region_selector(analysis, dot.id),
    )
    current = KeepRegionsCommand(
        **COMMAND_METADATA,
        command_id="cmd_config_current",
        selector=_region_selector(analysis, dot.id, config=OTHER_CONFIG),
    )

    with pytest.raises(IncompatibleSelectorError) as caught:
        validate_persisted_sequence(
            (command_to_storage_payload(stale), command_to_storage_payload(current)),
            config_fingerprint=OTHER_CONFIG,
        )

    assert caught.value.operation_count == 2
    assert caught.value.conflicts == ((stale.command_id, CONFIG),)


def test_storage_bridge_round_trips_without_weakening_and_rejects_legacy_operations() -> None:
    field, _active, analysis = _fixture([[0, 1]])
    selected = next(region for region in analysis.graph.regions if region.label == 1)
    command = KeepRegionsCommand(
        **COMMAND_METADATA,
        command_id="cmd_storage1",
        selector=_region_selector(analysis, selected.id),
    )
    payload = command_to_storage_payload(command)

    identity_payload = {
        "selector": command.selector.model_dump(mode="json"),
        "reason": command.reason,
    }
    assert derive_command_id("keep_regions", identity_payload) == derive_command_id(
        "keep_regions", dict(reversed(tuple(identity_payload.items())))
    )
    assert load_persisted_commands((payload,)) == (command,)
    assert load_persisted_commands((command.model_dump(mode="json"),)) == (command,)
    drifted = {**payload, "selection": {}}
    with pytest.raises(LegacyOperationError, match="selector envelope drift"):
        load_persisted_commands((drifted,))
    with pytest.raises(LegacyOperationError, match="lack versioned graph and config bindings"):
        load_persisted_commands(
            (
                {
                    "operation_type": "replace_label",
                    "selection": {"x": 0, "y": 0},
                    "parameters": {"target": 1},
                },
            )
        )


@given(
    width=st.integers(min_value=1, max_value=10),
    height=st.integers(min_value=1, max_value=10),
    values=st.data(),
)
@settings(max_examples=40, deadline=None)
def test_property_empty_replay_preserves_exhaustive_active_labels_and_fingerprint(
    width: int, height: int, values: st.DataObject
) -> None:
    pixels = values.draw(
        st.lists(
            st.integers(min_value=0, max_value=2),
            min_size=width * height,
            max_size=width * height,
        )
    )
    active = values.draw(
        st.lists(
            st.integers(min_value=0, max_value=1),
            min_size=width * height,
            max_size=width * height,
        )
    )
    array = np.asarray(pixels, dtype=np.uint8).reshape((height, width))
    field, active_bytes, _analysis = _fixture(
        array.tolist(), labels=(0, 1, 2), active=bytes(active)
    )

    first = _replay(field, active_bytes)
    second = _replay(field, active_bytes)

    assert first.labels.pixels == field.pixels
    assert first.active == active_bytes
    assert first.record.final_graph.active_pixel_count == sum(active)
    assert first.fingerprint() == second.fingerprint()
