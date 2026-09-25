"""Replay strict editor commands into an exhaustive label field."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
from pydantic import ValidationError

from image23mf.contracts.editor import (
    CANVAS_SELECTION_STORAGE_OPERATION_TYPE,
    EDITOR_STORAGE_OPERATION_TYPE,
    CanvasSelectionCommand,
    ContourCleanupCommand,
    EditorCommand,
    EditorCommandSequence,
    EditorPixelTransition,
    EditorReplayRecord,
    EditorReplayStep,
    HoleCorrectionCommand,
    IslandMergeCommand,
    KeepRegionsCommand,
    LocalRasterEditCommand,
    RegionOperationCommand,
    RegionOperationKind,
    RegionScopeMode,
    SimilarRegionPredicate,
    SourceRegionSelection,
    ThickenRegionCommand,
    canonical_fingerprint,
    derive_region_scope_fingerprint,
    validate_editor_command,
)
from image23mf.editor.local_edits import apply_local_raster_edit
from image23mf.editor.selection import rasterize_canvas_selection
from image23mf.engine.contours import (
    LabelAreaChange,
    apply_contour_cleanup,
    physical_kernel_offsets,
)
from image23mf.engine.holes import (
    HoleAnalysisOptions,
    HoleCorrectionRequest,
    apply_hole_policy,
    classify_holes,
)
from image23mf.engine.islands import (
    IslandAnalysisOptions,
    IslandMergeRequest,
    apply_island_policy,
    classify_small_islands,
)
from image23mf.engine.labels import LabelField
from image23mf.engine.regions import (
    RegionAnalysis,
    RegionNode,
    analyze_regions,
    derive_region_lineage,
    first_mismatched_region,
)
from image23mf.engine.transform import CanonicalTransform


class EditorReplayError(ValueError):
    """Base error for an editor log that cannot be replayed safely."""


class StaleSelectorError(EditorReplayError):
    """A selector was authored against a different config, graph, or classification."""

    def __init__(self, command_id: str, binding: str, expected: str, actual: str) -> None:
        self.command_id = command_id
        self.binding = binding
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"command {command_id} has stale {binding}: expected {expected}, got {actual}"
        )


class IncompatibleSelectorError(EditorReplayError):
    """Persisted selectors are bound to a different candidate configuration."""

    def __init__(
        self,
        *,
        expected_config_fingerprint: str,
        conflicts: Sequence[tuple[str, str]],
        operation_count: int,
    ) -> None:
        if not conflicts:
            raise ValueError("incompatible selector errors require at least one conflict")
        self.expected_config_fingerprint = expected_config_fingerprint
        self.conflicts = tuple(conflicts)
        self.operation_count = operation_count
        command_ids = ", ".join(command_id for command_id, _ in self.conflicts)
        super().__init__(
            f"{len(self.conflicts)} editor selector(s) are bound to a different "
            f"configuration ({command_ids})"
        )


class ProtectedPixelError(EditorReplayError):
    """A later command attempted to modify pixels accepted by keep/protect."""

    def __init__(self, command_id: str, pixel_count: int) -> None:
        self.command_id = command_id
        self.pixel_count = pixel_count
        super().__init__(
            f"command {command_id} would modify {pixel_count} explicitly protected pixel(s)"
        )


class NoOpOperationError(EditorReplayError):
    """A typed mutation would make no exact state or protection change."""

    def __init__(self, command_id: str, reason: str) -> None:
        self.command_id = command_id
        self.reason = reason
        super().__init__(f"command {command_id} is a no-op: {reason}")


class LegacyOperationError(EditorReplayError):
    """An untyped legacy operation cannot be replayed without guessing intent."""

    def __init__(self, index: int, operation_type: str, reason: str) -> None:
        self.index = index
        self.operation_type = operation_type
        self.reason = reason
        super().__init__(f"legacy operation {index} ({operation_type!r}) was rejected: {reason}")


@dataclass(frozen=True)
class ReplayResult:
    labels: LabelField
    active: bytes
    analysis: RegionAnalysis
    changed_mask: bytes
    protected_mask: bytes
    record: EditorReplayRecord

    def __post_init__(self) -> None:
        size = self.labels.width * self.labels.height
        planes = (self.active, self.changed_mask, self.protected_mask)
        if any(len(plane) != size for plane in planes):
            raise ValueError("replay planes must match the final label field")
        if self.record.after_graph_fingerprint != self.analysis.graph.fingerprint():
            raise ValueError("replay record does not match final region analysis")
        if sum(bool(value) for value in self.changed_mask) != self.record.changed_pixel_count:
            raise ValueError("replay changed mask does not match exact record statistics")
        if sum(bool(value) for value in self.protected_mask) != self.record.protected_pixel_count:
            raise ValueError("replay protected mask does not match exact record statistics")

    def fingerprint(self) -> str:
        """Fingerprint the complete deterministic result, including binary masks."""

        return canonical_fingerprint(
            {
                "record_fingerprint": self.record.fingerprint(),
                "label_state_fingerprint": _state_fingerprint(self.labels, self.active),
                "changed_mask_sha256": hashlib.sha256(self.changed_mask).hexdigest(),
                "protected_mask_sha256": hashlib.sha256(self.protected_mask).hexdigest(),
            }
        )


def replay_editor_commands(
    labels: LabelField,
    *,
    active: bytes,
    colors: Mapping[int, str],
    width_mm: float,
    height_mm: float,
    config_fingerprint: str,
    commands: Sequence[EditorCommand],
    transform: Optional[CanonicalTransform] = None,
) -> ReplayResult:
    """Replay an ordered command log and return exact final state plus audit evidence.

    Every selector is checked immediately before its command. Upstream edits therefore make
    downstream graph-bound commands fail explicitly instead of targeting the wrong region.
    """

    for command in commands:
        _require_binding(
            command.command_id,
            "config fingerprint",
            command.selector.config_fingerprint,
            config_fingerprint,
        )
    sequence = EditorCommandSequence(
        config_fingerprint=config_fingerprint,
        commands=tuple(commands),
    )
    initial_analysis = analyze_regions(
        labels,
        colors=colors,
        width_mm=width_mm,
        height_mm=height_mm,
        active=active,
    )
    _validate_state(labels, active, initial_analysis)
    initial_labels = labels
    initial_active = bytes(active)
    current_labels = labels
    current_active = bytes(active)
    current_analysis = initial_analysis
    protected = np.zeros((labels.height, labels.width), dtype=bool)
    steps: list[EditorReplayStep] = []
    pixel_area = initial_analysis.graph.pixel_width_mm * initial_analysis.graph.pixel_height_mm

    for index, command in enumerate(sequence.commands):
        _validate_selector(command, config_fingerprint, current_analysis)
        before_labels = current_labels
        before_active = current_active
        before_analysis = current_analysis
        before_protected = protected.copy()
        engine_record_fingerprint: Optional[str] = None

        if isinstance(command, CanvasSelectionCommand):
            # Selection snapshots are revision-visible authoring state. They intentionally do not
            # mutate the processed label field; downstream regional edit commands consume them.
            pass
        elif isinstance(command, LocalRasterEditCommand):
            if transform is None:
                raise EditorReplayError(
                    f"command {command.command_id} requires the canonical source transform"
                )
            selection = rasterize_canvas_selection(
                command.selector.selection,
                transform,
                region_masks=_selection_region_masks(command, current_analysis),
            )
            applied = apply_local_raster_edit(
                current_labels,
                active=current_active,
                selection=selection,
                transform=transform,
                edit=command.edit,
            )
            current_labels = applied.labels
            current_active = applied.active
            current_analysis = _analyze_current(
                current_labels,
                current_active,
                current_analysis,
            )
            if not np.any(
                _changed_mask(
                    before_labels,
                    before_active,
                    current_labels,
                    current_active,
                )
            ):
                raise NoOpOperationError(
                    command.command_id,
                    "the local edit did not change any selected destination pixel",
                )
            engine_record_fingerprint = canonical_fingerprint(
                {
                    "selection_raster_fingerprint": selection.fingerprint(),
                    "edit": command.edit.model_dump(mode="json"),
                    "selected_pixel_count": applied.selected_pixel_count,
                    "destination_pixel_count": applied.destination_pixel_count,
                }
            )
        elif isinstance(command, KeepRegionsCommand):
            selected = _selected_region_mask(command.selector.region_ids, current_analysis)
            protected |= selected
        elif isinstance(command, RegionOperationCommand):
            selected_region_ids = _resolve_region_scope(command, current_analysis)
            selected = _selected_region_mask(selected_region_ids, current_analysis)
            if command.operation == RegionOperationKind.PROTECT:
                newly_selected = selected & ~protected
                if not np.any(newly_selected):
                    raise NoOpOperationError(
                        command.command_id,
                        "every resolved pixel is already protected",
                    )
                protected |= selected
            elif command.operation == RegionOperationKind.DELETE:
                current_active = _delete_selected_pixels(
                    current_active,
                    selected,
                )
                current_analysis = _analyze_current(
                    current_labels,
                    current_active,
                    current_analysis,
                )
            elif command.operation in {
                RegionOperationKind.MERGE,
                RegionOperationKind.RECOLOR,
            }:
                assert command.target_label is not None
                current_labels, current_analysis = _apply_region_relabel(
                    current_labels,
                    current_active,
                    current_analysis,
                    selected_region_ids,
                    selected,
                    target_label=command.target_label,
                    require_adjacent=command.operation == RegionOperationKind.MERGE,
                    command_id=command.command_id,
                )
            else:
                current_labels, current_analysis = _apply_scoped_thicken(
                    current_labels,
                    current_active,
                    current_analysis,
                    command,
                    selected_region_ids,
                    selected,
                )
        elif isinstance(command, IslandMergeCommand):
            classification = classify_small_islands(
                current_analysis,
                options=IslandAnalysisOptions(**command.analysis_options.model_dump()),
            )
            _require_binding(
                command.command_id,
                "island classification",
                command.selector.classification_fingerprint,
                classification.fingerprint(),
            )
            applied = apply_island_policy(
                current_labels,
                current_analysis,
                classification,
                IslandMergeRequest(
                    policy=command.policy,
                    region_ids=command.selector.region_ids,
                    explicit_target_label=command.explicit_target_label,
                ),
            )
            current_labels = applied.labels
            current_active = _active_from_analysis(applied.analysis)
            current_analysis = applied.analysis
            engine_record_fingerprint = applied.record.fingerprint()
        elif isinstance(command, HoleCorrectionCommand):
            classification = classify_holes(
                current_analysis,
                options=HoleAnalysisOptions(**command.analysis_options.model_dump()),
            )
            _require_binding(
                command.command_id,
                "hole classification",
                command.selector.classification_fingerprint,
                classification.fingerprint(),
            )
            applied = apply_hole_policy(
                current_labels,
                current_analysis,
                classification,
                HoleCorrectionRequest(
                    policy=command.policy,
                    feature_ids=command.selector.feature_ids,
                    explicit_target_label=command.explicit_target_label,
                ),
            )
            current_labels = applied.labels
            current_active = applied.active
            current_analysis = applied.analysis
            engine_record_fingerprint = canonical_fingerprint(applied.record)
        elif isinstance(command, ContourCleanupCommand):
            applied = apply_contour_cleanup(
                current_labels,
                current_analysis,
                command.request,
            )
            current_labels = applied.labels
            current_active = applied.active
            current_analysis = applied.analysis
            engine_record_fingerprint = applied.record.fingerprint()
        elif isinstance(command, ThickenRegionCommand):
            current_labels, current_analysis = _apply_thicken(
                current_labels,
                current_active,
                current_analysis,
                command,
            )
        else:  # pragma: no cover - discriminated union keeps this unreachable
            raise TypeError(f"unsupported editor command: {type(command).__name__}")

        _validate_state(current_labels, current_active, current_analysis)
        changed = _changed_mask(
            before_labels,
            before_active,
            current_labels,
            current_active,
        )
        protected_overlap = int(np.count_nonzero(changed & before_protected))
        if protected_overlap:
            raise ProtectedPixelError(command.command_id, protected_overlap)
        steps.append(
            _step_record(
                index=index,
                command=command,
                before_labels=before_labels,
                before_active=before_active,
                before_analysis=before_analysis,
                after_labels=current_labels,
                after_active=current_active,
                after_analysis=current_analysis,
                protected=protected,
                newly_protected=int(np.count_nonzero(protected & ~before_protected)),
                engine_record_fingerprint=engine_record_fingerprint,
                pixel_area=pixel_area,
            )
        )

    net_changed = _changed_mask(
        initial_labels,
        initial_active,
        current_labels,
        current_active,
    )
    transitions = _transitions(
        initial_labels,
        initial_active,
        current_labels,
        current_active,
        net_changed,
        pixel_area,
    )
    record = EditorReplayRecord(
        sequence_fingerprint=sequence.fingerprint(),
        config_fingerprint=config_fingerprint,
        initial_state_fingerprint=_state_fingerprint(initial_labels, initial_active),
        final_state_fingerprint=_state_fingerprint(current_labels, current_active),
        before_graph_fingerprint=initial_analysis.graph.fingerprint(),
        after_graph_fingerprint=current_analysis.graph.fingerprint(),
        command_count=len(sequence.commands),
        steps=tuple(steps),
        operation_changed_pixel_count=sum(step.changed_pixel_count for step in steps),
        changed_pixel_count=int(np.count_nonzero(net_changed)),
        changed_area_mm2=int(np.count_nonzero(net_changed)) * pixel_area,
        changed_mask_sha256=hashlib.sha256(net_changed.astype(np.uint8).tobytes()).hexdigest(),
        transitions=transitions,
        label_areas=_label_areas(
            initial_labels,
            initial_active,
            current_labels,
            current_active,
            pixel_area,
        ),
        protected_pixel_count=int(np.count_nonzero(protected)),
        final_graph=current_analysis.graph,
        lineage=derive_region_lineage(initial_analysis, current_analysis),
    )
    return ReplayResult(
        labels=current_labels,
        active=current_active,
        analysis=current_analysis,
        changed_mask=net_changed.astype(np.uint8).tobytes(),
        protected_mask=protected.astype(np.uint8).tobytes(),
        record=record,
    )


def command_to_storage_payload(command: EditorCommand) -> dict[str, Any]:
    """Bridge a typed command through the existing generic operation storage seam."""

    return {
        "operation_type": (
            CANVAS_SELECTION_STORAGE_OPERATION_TYPE
            if isinstance(command, CanvasSelectionCommand)
            else EDITOR_STORAGE_OPERATION_TYPE
        ),
        "selection": command.selector.model_dump(mode="json"),
        "parameters": {"command": command.model_dump(mode="json")},
        "source": command.source.value,
        "provenance": {
            "editor_schema_version": 1,
            "command_fingerprint": command.fingerprint(),
        },
    }


def validate_canvas_selection_storage_payload(value: Mapping[str, Any]) -> CanvasSelectionCommand:
    """Validate a saved authoring selection without adding it to render invalidation."""

    operation_type = str(value.get("operation_type", ""))
    if operation_type != CANVAS_SELECTION_STORAGE_OPERATION_TYPE:
        raise LegacyOperationError(-1, operation_type or "unknown", "not a selection envelope")
    parameters = value.get("parameters")
    if not isinstance(parameters, Mapping) or set(parameters) != {"command"}:
        raise LegacyOperationError(
            -1,
            operation_type,
            "the selection envelope requires exactly parameters.command",
        )
    try:
        command = validate_editor_command(parameters["command"])
    except ValidationError as error:
        raise LegacyOperationError(
            -1,
            operation_type,
            f"typed selection validation failed: {error.errors(include_url=False)}",
        ) from error
    if not isinstance(command, CanvasSelectionCommand):
        raise LegacyOperationError(-1, operation_type, "the envelope command is not a selection")
    _validate_storage_envelope(-1, value, command)
    return command


def load_persisted_commands(values: Sequence[Mapping[str, Any]]) -> tuple[EditorCommand, ...]:
    """Load v1 commands or explicitly reject ambiguous legacy operation payloads.

    Direct typed command objects and the lossless ``editor_command_v1`` storage envelope are
    accepted. Earlier free-form operations are never guessed into a destructive command.
    """

    commands = []
    for index, value in enumerate(values):
        operation_type = str(value.get("operation_type", ""))
        if "command_type" in value:
            payload: Any = value
        elif operation_type == EDITOR_STORAGE_OPERATION_TYPE:
            parameters = value.get("parameters")
            if not isinstance(parameters, Mapping) or set(parameters) != {"command"}:
                raise LegacyOperationError(
                    index,
                    operation_type,
                    "the v1 envelope requires exactly parameters.command",
                )
            payload = parameters["command"]
        else:
            raise LegacyOperationError(
                index,
                operation_type or "unknown",
                "free-form selection/parameters lack versioned graph and config bindings",
            )
        try:
            command = validate_editor_command(payload)
        except ValidationError as error:
            raise LegacyOperationError(
                index,
                operation_type or str(value.get("command_type", "unknown")),
                f"typed command validation failed: {error.errors(include_url=False)}",
            ) from error
        if operation_type == EDITOR_STORAGE_OPERATION_TYPE:
            _validate_storage_envelope(index, value, command)
        commands.append(command)
    if len({command.command_id for command in commands}) != len(commands):
        raise LegacyOperationError(-1, "editor_command_v1", "command IDs must be unique")
    return tuple(commands)


def validate_persisted_sequence(
    values: Sequence[Mapping[str, Any]],
    *,
    config_fingerprint: str,
) -> EditorCommandSequence:
    """Parse and bind a complete persisted command sequence to its candidate config.

    Draft autosave must call this before persistence. Validating operations in isolation is not
    enough because every exact selector is authored against the configuration that produced its
    region graph.
    """

    commands = load_persisted_commands(values)
    conflicts = tuple(
        (command.command_id, command.selector.config_fingerprint)
        for command in commands
        if command.selector.config_fingerprint != config_fingerprint
    )
    if conflicts:
        raise IncompatibleSelectorError(
            expected_config_fingerprint=config_fingerprint,
            conflicts=conflicts,
            operation_count=len(values),
        )
    return EditorCommandSequence(
        config_fingerprint=config_fingerprint,
        commands=commands,
    )


def _validate_storage_envelope(
    index: int, value: Mapping[str, Any], command: EditorCommand
) -> None:
    selection = value.get("selection")
    if selection != command.selector.model_dump(mode="json"):
        raise LegacyOperationError(index, EDITOR_STORAGE_OPERATION_TYPE, "selector envelope drift")
    source = value.get("source", "manual")
    if source != command.source.value:
        raise LegacyOperationError(index, EDITOR_STORAGE_OPERATION_TYPE, "source envelope drift")
    provenance = value.get("provenance", {})
    if not isinstance(provenance, Mapping):
        raise LegacyOperationError(index, EDITOR_STORAGE_OPERATION_TYPE, "invalid provenance")
    recorded = provenance.get("command_fingerprint")
    if recorded is not None and recorded != command.fingerprint():
        raise LegacyOperationError(index, EDITOR_STORAGE_OPERATION_TYPE, "fingerprint drift")


def _validate_selector(
    command: EditorCommand, config_fingerprint: str, analysis: RegionAnalysis
) -> None:
    _require_binding(
        command.command_id,
        "config fingerprint",
        command.selector.config_fingerprint,
        config_fingerprint,
    )
    _require_binding(
        command.command_id,
        "graph fingerprint",
        command.selector.graph_fingerprint,
        analysis.graph.fingerprint(),
    )
    if isinstance(
        command,
        (KeepRegionsCommand, IslandMergeCommand, ThickenRegionCommand),
    ):
        available = {region.id for region in analysis.graph.regions}
        for region_id in command.selector.region_ids:
            if region_id not in available:
                raise StaleSelectorError(
                    command.command_id,
                    "region selector",
                    region_id,
                    "missing from bound graph",
                )
    elif isinstance(command, RegionOperationCommand):
        available = {region.id for region in analysis.graph.regions}
        referenced = set(command.selector.scope.anchor_region_ids) | set(
            command.selector.scope.resolved_region_ids
        )
        missing = sorted(referenced - available)
        if missing:
            raise StaleSelectorError(
                command.command_id,
                "region scope",
                ",".join(missing),
                "missing from bound graph",
            )
    elif isinstance(command, (CanvasSelectionCommand, LocalRasterEditCommand)):
        available = {region.id for region in analysis.graph.regions}
        referenced = {
            region_id
            for primitive in command.selector.selection.primitives
            if isinstance(primitive, SourceRegionSelection)
            for region_id in primitive.region_ids
        }
        missing = sorted(referenced - available)
        if missing:
            raise StaleSelectorError(
                command.command_id,
                "selection regions",
                ",".join(missing),
                "missing from bound graph",
            )


def _selection_region_masks(
    command: LocalRasterEditCommand,
    analysis: RegionAnalysis,
) -> dict[str, bytes]:
    referenced = {
        region_id
        for primitive in command.selector.selection.primitives
        if isinstance(primitive, SourceRegionSelection)
        for region_id in primitive.region_ids
    }
    if not referenced:
        return {}
    graph_index = {region.id: index for index, region in enumerate(analysis.graph.regions)}
    return {
        region_id: (analysis.assignment == graph_index[region_id]).astype(np.uint8).tobytes()
        for region_id in sorted(referenced)
    }


def _require_binding(command_id: str, name: str, expected: str, actual: str) -> None:
    if expected != actual:
        raise StaleSelectorError(command_id, name, expected, actual)


def _selected_region_mask(region_ids: Sequence[str], analysis: RegionAnalysis) -> np.ndarray:
    graph_index = {region.id: index for index, region in enumerate(analysis.graph.regions)}
    missing = sorted(set(region_ids) - graph_index.keys())
    if missing:
        raise EditorReplayError(f"selector references unknown regions: {missing}")
    return np.isin(analysis.assignment, [graph_index[region_id] for region_id in region_ids])


def _resolve_region_scope(
    command: RegionOperationCommand,
    analysis: RegionAnalysis,
) -> tuple[str, ...]:
    scope = command.selector.scope
    if scope.mode == RegionScopeMode.SELECTED:
        resolved = scope.anchor_region_ids
    else:
        assert scope.predicate is not None
        graph_index = {region.id: region for region in analysis.graph.regions}
        anchors = tuple(graph_index[region_id] for region_id in scope.anchor_region_ids)
        resolved = tuple(
            sorted(
                region.id
                for region in analysis.graph.regions
                if any(
                    _region_matches_predicate(region, anchor, scope.predicate, graph_index)
                    for anchor in anchors
                )
            )
        )
    actual_scope = scope.model_copy(update={"resolved_region_ids": resolved})
    actual_fingerprint = derive_region_scope_fingerprint(
        command.selector.graph_fingerprint,
        actual_scope,
    )
    if resolved != scope.resolved_region_ids or actual_fingerprint != scope.resolution_fingerprint:
        raise StaleSelectorError(
            command.command_id,
            "scope resolution",
            scope.resolution_fingerprint,
            actual_fingerprint,
        )
    return resolved


def _region_matches_predicate(
    region: RegionNode,
    anchor: RegionNode,
    predicate: SimilarRegionPredicate,
    graph_index: Mapping[str, RegionNode],
) -> bool:
    if region.label != anchor.label:
        return False
    if _relative_delta(region.area_mm2, anchor.area_mm2) > predicate.area_ratio_tolerance:
        return False
    if (
        _relative_delta(region.width_estimate.minimum_mm, anchor.width_estimate.minimum_mm)
        > predicate.minimum_width_ratio_tolerance
    ):
        return False
    if abs(region.compactness - anchor.compactness) > predicate.compactness_tolerance:
        return False
    if (
        predicate.match_border_contact
        and region.border_contact.touches_canvas_border
        != anchor.border_contact.touches_canvas_border
    ):
        return False
    return not predicate.match_neighbor_labels or _neighbor_labels(
        region, graph_index
    ) == _neighbor_labels(anchor, graph_index)


def _relative_delta(first: float, second: float) -> float:
    return abs(first - second) / max(first, second)


def _neighbor_labels(
    region: RegionNode,
    graph_index: Mapping[str, RegionNode],
) -> tuple[int, ...]:
    return tuple(sorted({graph_index[region_id].label for region_id in region.neighbor_region_ids}))


def _delete_selected_pixels(active: bytes, selected: np.ndarray) -> bytes:
    active_array = np.frombuffer(active, dtype=np.uint8).reshape(selected.shape).astype(bool).copy()
    active_array[selected] = False
    return active_array.astype(np.uint8).tobytes()


def _analyze_current(
    labels: LabelField,
    active: bytes,
    analysis: RegionAnalysis,
) -> RegionAnalysis:
    colors = {item.label: item.color for item in analysis.graph.palette}
    return analyze_regions(
        labels,
        colors=colors,
        width_mm=analysis.graph.width_mm,
        height_mm=analysis.graph.height_mm,
        active=active,
    )


def _apply_region_relabel(
    labels: LabelField,
    active: bytes,
    analysis: RegionAnalysis,
    selected_region_ids: Sequence[str],
    selected: np.ndarray,
    *,
    target_label: int,
    require_adjacent: bool,
    command_id: str,
) -> tuple[LabelField, RegionAnalysis]:
    if target_label not in labels.label_values:
        raise EditorReplayError(f"target label {target_label} is absent from the palette")
    regions = {region.id: region for region in analysis.graph.regions}
    selected_regions = tuple(regions[region_id] for region_id in selected_region_ids)
    unchanged = tuple(region.id for region in selected_regions if region.label == target_label)
    if unchanged:
        raise NoOpOperationError(
            command_id,
            f"target label {target_label} already owns resolved regions {unchanged}",
        )
    if require_adjacent:
        for region in selected_regions:
            neighbor_labels = {regions[region_id].label for region_id in region.neighbor_region_ids}
            if target_label not in neighbor_labels:
                raise EditorReplayError(
                    f"merge target label {target_label} is not adjacent to region {region.id}"
                )
    source = np.frombuffer(labels.pixels, dtype=np.uint8).reshape(selected.shape)
    updated = source.copy()
    updated[selected] = target_label
    result = LabelField(
        width=labels.width,
        height=labels.height,
        label_values=labels.label_values,
        pixels=updated.tobytes(),
    )
    return result, _analyze_current(result, active, analysis)


def _apply_scoped_thicken(
    labels: LabelField,
    active: bytes,
    analysis: RegionAnalysis,
    command: RegionOperationCommand,
    selected_region_ids: Sequence[str],
    selected: np.ndarray,
) -> tuple[LabelField, RegionAnalysis]:
    regions = {region.id: region for region in analysis.graph.regions}
    source_labels = {regions[region_id].label for region_id in selected_region_ids}
    if len(source_labels) != 1:
        raise EditorReplayError("scoped thicken requires every resolved region to share one label")
    source_label = next(iter(source_labels))
    if source_label in command.editable_labels:
        raise EditorReplayError("thicken editable labels must exclude the source label")
    unknown = set(command.editable_labels) - set(labels.label_values)
    if unknown:
        raise EditorReplayError(f"thicken editable labels are absent from the palette: {unknown}")
    assert command.radius_mm is not None
    offsets = physical_kernel_offsets(
        command.radius_mm,
        pixel_width_mm=analysis.graph.pixel_width_mm,
        pixel_height_mm=analysis.graph.pixel_height_mm,
    )
    grown = np.zeros(selected.shape, dtype=bool)
    for dy, dx in offsets:
        _shift_or(selected, grown, dy, dx)
    label_array = np.frombuffer(labels.pixels, dtype=np.uint8).reshape(selected.shape)
    active_array = np.frombuffer(active, dtype=np.uint8).reshape(selected.shape).astype(bool)
    editable = active_array & np.isin(label_array, command.editable_labels)
    changed = grown & editable
    if not np.any(changed):
        raise NoOpOperationError(command.command_id, "no editable pixel is within the radius")
    updated = label_array.copy()
    updated[changed] = source_label
    result = LabelField(
        width=labels.width,
        height=labels.height,
        label_values=labels.label_values,
        pixels=updated.tobytes(),
    )
    return result, _analyze_current(result, active, analysis)


def _apply_thicken(
    labels: LabelField,
    active: bytes,
    analysis: RegionAnalysis,
    command: ThickenRegionCommand,
) -> tuple[LabelField, RegionAnalysis]:
    region_id = command.selector.region_ids[0]
    graph_index = {region.id: index for index, region in enumerate(analysis.graph.regions)}
    if region_id not in graph_index:
        raise EditorReplayError(f"selector references unknown region: {region_id}")
    region = analysis.graph.regions[graph_index[region_id]]
    if region.label in command.editable_labels:
        raise EditorReplayError("thicken editable labels must exclude the source label")
    unknown = set(command.editable_labels) - set(labels.label_values)
    if unknown:
        raise EditorReplayError(f"thicken editable labels are absent from the palette: {unknown}")
    offsets = physical_kernel_offsets(
        command.radius_mm,
        pixel_width_mm=analysis.graph.pixel_width_mm,
        pixel_height_mm=analysis.graph.pixel_height_mm,
    )
    source = analysis.assignment == graph_index[region_id]
    grown = np.zeros(source.shape, dtype=bool)
    for dy, dx in offsets:
        _shift_or(source, grown, dy, dx)
    source_labels = np.frombuffer(labels.pixels, dtype=np.uint8).reshape(source.shape)
    active_array = np.frombuffer(active, dtype=np.uint8).reshape(source.shape).astype(bool)
    editable = active_array & np.isin(source_labels, command.editable_labels)
    changed = grown & editable
    if not np.any(changed):
        raise NoOpOperationError(command.command_id, "no editable pixel is within the radius")
    updated = source_labels.copy()
    updated[changed] = region.label
    result = LabelField(
        width=labels.width,
        height=labels.height,
        label_values=labels.label_values,
        pixels=updated.tobytes(),
    )
    colors = {item.label: item.color for item in analysis.graph.palette}
    return result, analyze_regions(
        result,
        colors=colors,
        width_mm=analysis.graph.width_mm,
        height_mm=analysis.graph.height_mm,
        active=active,
    )


def _shift_or(source: np.ndarray, target: np.ndarray, dy: int, dx: int) -> None:
    height, width = source.shape
    source_y = slice(max(0, -dy), min(height, height - dy))
    target_y = slice(max(0, dy), min(height, height + dy))
    source_x = slice(max(0, -dx), min(width, width - dx))
    target_x = slice(max(0, dx), min(width, width + dx))
    target[target_y, target_x] |= source[source_y, source_x]


def _step_record(
    *,
    index: int,
    command: EditorCommand,
    before_labels: LabelField,
    before_active: bytes,
    before_analysis: RegionAnalysis,
    after_labels: LabelField,
    after_active: bytes,
    after_analysis: RegionAnalysis,
    protected: np.ndarray,
    newly_protected: int,
    engine_record_fingerprint: Optional[str],
    pixel_area: float,
) -> EditorReplayStep:
    changed = _changed_mask(before_labels, before_active, after_labels, after_active)
    changed_bytes = changed.astype(np.uint8).tobytes()
    return EditorReplayStep(
        index=index,
        command_id=command.command_id,
        command_type=command.command_type,
        command_fingerprint=command.fingerprint(),
        before_state_fingerprint=_state_fingerprint(before_labels, before_active),
        after_state_fingerprint=_state_fingerprint(after_labels, after_active),
        before_graph_fingerprint=before_analysis.graph.fingerprint(),
        after_graph_fingerprint=after_analysis.graph.fingerprint(),
        changed_pixel_count=int(np.count_nonzero(changed)),
        changed_area_mm2=int(np.count_nonzero(changed)) * pixel_area,
        changed_mask_sha256=hashlib.sha256(changed_bytes).hexdigest(),
        transitions=_transitions(
            before_labels,
            before_active,
            after_labels,
            after_active,
            changed,
            pixel_area,
        ),
        label_areas=_label_areas(
            before_labels,
            before_active,
            after_labels,
            after_active,
            pixel_area,
        ),
        protected_pixel_count=int(np.count_nonzero(protected)),
        newly_protected_pixel_count=newly_protected,
        engine_record_fingerprint=engine_record_fingerprint,
        lineage=derive_region_lineage(before_analysis, after_analysis),
    )


def _changed_mask(
    before_labels: LabelField,
    before_active: bytes,
    after_labels: LabelField,
    after_active: bytes,
) -> np.ndarray:
    if (before_labels.width, before_labels.height) != (after_labels.width, after_labels.height):
        raise ValueError("editor commands cannot change raster dimensions")
    shape = (before_labels.height, before_labels.width)
    old_labels = np.frombuffer(before_labels.pixels, dtype=np.uint8).reshape(shape)
    new_labels = np.frombuffer(after_labels.pixels, dtype=np.uint8).reshape(shape)
    old_active = np.frombuffer(before_active, dtype=np.uint8).reshape(shape).astype(bool)
    new_active = np.frombuffer(after_active, dtype=np.uint8).reshape(shape).astype(bool)
    return (old_active != new_active) | ((old_active | new_active) & (old_labels != new_labels))


def _transitions(
    before_labels: LabelField,
    before_active: bytes,
    after_labels: LabelField,
    after_active: bytes,
    changed: np.ndarray,
    pixel_area: float,
) -> tuple[EditorPixelTransition, ...]:
    shape = changed.shape
    old_labels = np.frombuffer(before_labels.pixels, dtype=np.uint8).reshape(shape)
    new_labels = np.frombuffer(after_labels.pixels, dtype=np.uint8).reshape(shape)
    old_active = np.frombuffer(before_active, dtype=np.uint8).reshape(shape).astype(bool)
    new_active = np.frombuffer(after_active, dtype=np.uint8).reshape(shape).astype(bool)
    counts: dict[tuple[bool, Optional[int], bool, Optional[int]], int] = {}
    for y, x in np.argwhere(changed):
        key = (
            bool(old_active[y, x]),
            int(old_labels[y, x]) if old_active[y, x] else None,
            bool(new_active[y, x]),
            int(new_labels[y, x]) if new_active[y, x] else None,
        )
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        EditorPixelTransition(
            source_active=key[0],
            source_label=key[1],
            target_active=key[2],
            target_label=key[3],
            pixel_count=count,
            area_mm2=count * pixel_area,
        )
        for key, count in sorted(
            counts.items(),
            key=lambda item: (
                item[0][0],
                -1 if item[0][1] is None else item[0][1],
                item[0][2],
                -1 if item[0][3] is None else item[0][3],
            ),
        )
    )


def _label_areas(
    before_labels: LabelField,
    before_active: bytes,
    after_labels: LabelField,
    after_active: bytes,
    pixel_area: float,
) -> tuple[LabelAreaChange, ...]:
    shape = (before_labels.height, before_labels.width)
    old_labels = np.frombuffer(before_labels.pixels, dtype=np.uint8).reshape(shape)
    new_labels = np.frombuffer(after_labels.pixels, dtype=np.uint8).reshape(shape)
    old_active = np.frombuffer(before_active, dtype=np.uint8).reshape(shape).astype(bool)
    new_active = np.frombuffer(after_active, dtype=np.uint8).reshape(shape).astype(bool)
    changes = []
    for label in before_labels.label_values:
        before_count = int(np.count_nonzero(old_active & (old_labels == label)))
        after_count = int(np.count_nonzero(new_active & (new_labels == label)))
        changes.append(
            LabelAreaChange(
                label=label,
                before_pixel_count=before_count,
                after_pixel_count=after_count,
                delta_pixel_count=after_count - before_count,
                before_area_mm2=before_count * pixel_area,
                after_area_mm2=after_count * pixel_area,
                delta_area_mm2=(after_count - before_count) * pixel_area,
            )
        )
    return tuple(changes)


def _validate_state(labels: LabelField, active: bytes, analysis: RegionAnalysis) -> None:
    size = labels.width * labels.height
    if len(active) != size:
        raise ValueError("editor active plane must match the label field")
    if set(active) - {0, 1}:
        raise ValueError("editor active plane must contain only zero and one bytes")
    if labels.label_values != tuple(sorted(labels.label_values)):
        raise ValueError("editor label values must use canonical ordering")
    active_array = (
        np.frombuffer(active, dtype=np.uint8).reshape((labels.height, labels.width)).astype(bool)
    )
    if not np.array_equal(analysis.assignment >= 0, active_array):
        raise ValueError("every active pixel must have exactly one region assignment")
    if first_mismatched_region(labels, analysis) is not None:
        raise ValueError("region assignments must match exhaustive label bytes")


def _state_fingerprint(labels: LabelField, active: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(labels.width.to_bytes(8, "big"))
    digest.update(labels.height.to_bytes(8, "big"))
    digest.update(len(labels.label_values).to_bytes(2, "big"))
    digest.update(bytes(labels.label_values))
    digest.update(labels.pixels)
    digest.update(active)
    return digest.hexdigest()


def _active_from_analysis(analysis: RegionAnalysis) -> bytes:
    return (analysis.assignment >= 0).astype(np.uint8).tobytes()
