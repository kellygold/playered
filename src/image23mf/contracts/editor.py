"""Strict, replayable editor-command and replay-result contracts."""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from image23mf.engine.contours import (
    ContourCleanupRequest,
    LabelAreaChange,
)
from image23mf.engine.holes import HoleAnalysisSettings, HoleCorrectionPolicy
from image23mf.engine.islands import IslandAnalysisSettings, IslandMergePolicy
from image23mf.engine.regions import RegionGraph, RegionLineage

EDITOR_COMMAND_SCHEMA_VERSION = 1
EDITOR_REPLAY_SCHEMA_VERSION = 1
EDITOR_STORAGE_OPERATION_TYPE = "editor_command_v1"
CANVAS_SELECTION_STORAGE_OPERATION_TYPE = "canvas_selection_v1"
RegionId = Annotated[str, Field(pattern=r"^region_[0-9a-f]{24}$")]
FeatureId = Annotated[str, Field(pattern=r"^feature_[0-9a-f]{24}$")]
EditorCommandType = Literal[
    "canvas_selection",
    "local_raster_edit",
    "keep_regions",
    "island_merge",
    "hole_correction",
    "contour_cleanup",
    "thicken_region",
    "region_operation",
]


class EditorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def canonical_json(value: Any) -> str:
    """Serialize a model-compatible value without formatting or key-order ambiguity."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def derive_command_id(command_type: str, payload: Mapping[str, Any]) -> str:
    """Derive a stable ID when the caller lacks an existing durable command identity."""

    reserved = {"schema_version", "command_type", "command_id"} & payload.keys()
    if reserved:
        raise ValueError(f"command identity payload contains reserved keys: {sorted(reserved)}")
    identity = canonical_fingerprint(
        {"schema_version": EDITOR_COMMAND_SCHEMA_VERSION, "command_type": command_type, **payload}
    )
    return f"cmd_{identity[:24]}"


class EditorCommandSource(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    MODEL = "model"


class GraphSelector(EditorModel):
    """Selection binding that makes stale graph/config references impossible to ignore."""

    graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelectionCombineMode(str, Enum):
    ADD = "add"
    SUBTRACT = "subtract"


class SourceSelectionPoint(EditorModel):
    """Continuous normalized-source pixel-edge coordinate."""

    x: float = Field(ge=0, le=250_000)
    y: float = Field(ge=0, le=250_000)


class SourceRectangleSelection(EditorModel):
    kind: Literal["rectangle"] = "rectangle"
    primitive_id: str = Field(pattern=r"^selection_[A-Za-z0-9_-]{8,80}$")
    combine: SelectionCombineMode = SelectionCombineMode.ADD
    x: float = Field(ge=0, le=250_000)
    y: float = Field(ge=0, le=250_000)
    width: float = Field(gt=0, le=250_000)
    height: float = Field(gt=0, le=250_000)


class SourceLassoSelection(EditorModel):
    kind: Literal["lasso"] = "lasso"
    primitive_id: str = Field(pattern=r"^selection_[A-Za-z0-9_-]{8,80}$")
    combine: SelectionCombineMode = SelectionCombineMode.ADD
    points: tuple[SourceSelectionPoint, ...] = Field(min_length=3, max_length=4096)


class SourceBrushSelection(EditorModel):
    kind: Literal["brush"] = "brush"
    primitive_id: str = Field(pattern=r"^selection_[A-Za-z0-9_-]{8,80}$")
    combine: SelectionCombineMode = SelectionCombineMode.ADD
    points: tuple[SourceSelectionPoint, ...] = Field(min_length=1, max_length=4096)
    radius_mm: float = Field(gt=0, le=100)


class SourceRegionSelection(EditorModel):
    """Accessible graph/list equivalent to drawing a spatial primitive."""

    kind: Literal["regions"] = "regions"
    primitive_id: str = Field(pattern=r"^selection_[A-Za-z0-9_-]{8,80}$")
    combine: SelectionCombineMode = SelectionCombineMode.ADD
    region_ids: tuple[RegionId, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def region_ids_are_canonical(self) -> SourceRegionSelection:
        if self.region_ids != tuple(sorted(set(self.region_ids))):
            raise ValueError("selection region IDs must be unique and canonically sorted")
        return self


CanvasSelectionPrimitive = Annotated[
    Union[
        SourceRectangleSelection,
        SourceLassoSelection,
        SourceBrushSelection,
        SourceRegionSelection,
    ],
    Field(discriminator="kind"),
]


class CanvasSelectionState(EditorModel):
    """Complete saved selection state in canonical post-EXIF source coordinates."""

    schema_version: Literal[1] = 1
    coordinate_space: Literal["normalized_source"] = "normalized_source"
    source_width_px: int = Field(ge=1, le=250_000)
    source_height_px: int = Field(ge=1, le=250_000)
    primitives: tuple[CanvasSelectionPrimitive, ...] = Field(default=(), max_length=256)
    expand_mm: float = Field(default=0, ge=-100, le=100)
    feather_mm: float = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def primitives_are_bounded_and_unique(self) -> CanvasSelectionState:
        primitive_ids = tuple(item.primitive_id for item in self.primitives)
        if len(primitive_ids) != len(set(primitive_ids)):
            raise ValueError("selection primitive IDs must be unique")
        for item in self.primitives:
            if isinstance(item, SourceRectangleSelection):
                points = (
                    SourceSelectionPoint(x=item.x, y=item.y),
                    SourceSelectionPoint(x=item.x + item.width, y=item.y + item.height),
                )
            elif isinstance(item, (SourceLassoSelection, SourceBrushSelection)):
                points = item.points
            else:
                points = ()
            if any(
                point.x > self.source_width_px or point.y > self.source_height_px
                for point in points
            ):
                raise ValueError("selection geometry must stay inside normalized source bounds")
        return self


class CanvasSelectionSelector(GraphSelector):
    selection: CanvasSelectionState


class RegionSelector(GraphSelector):
    region_ids: tuple[RegionId, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def region_ids_are_canonical(self) -> RegionSelector:
        if self.region_ids != tuple(sorted(set(self.region_ids))):
            raise ValueError("region selector IDs must be unique and canonically sorted")
        return self


class RegionScopeMode(str, Enum):
    SELECTED = "selected"
    SIMILAR = "similar"


class SimilarRegionPredicate(EditorModel):
    """Bounded comparison on a cross-language-safe one-thousandth grid."""

    area_ratio_tolerance: float = Field(default=0.25, ge=0, le=1, multiple_of=0.001)
    minimum_width_ratio_tolerance: float = Field(default=0.25, ge=0, le=1, multiple_of=0.001)
    compactness_tolerance: float = Field(default=0.15, ge=0, le=1, multiple_of=0.001)
    match_border_contact: bool = True
    match_neighbor_labels: bool = False


class RegionScope(EditorModel):
    """Persist the authored anchors and exact expansion accepted by the user."""

    mode: RegionScopeMode = RegionScopeMode.SELECTED
    anchor_region_ids: tuple[RegionId, ...] = Field(min_length=1, max_length=256)
    predicate: Optional[SimilarRegionPredicate] = None
    resolved_region_ids: tuple[RegionId, ...] = Field(min_length=1, max_length=256)
    resolution_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def scope_is_canonical(self) -> RegionScope:
        for name, values in (
            ("anchor", self.anchor_region_ids),
            ("resolved", self.resolved_region_ids),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"scope {name} region IDs must be unique and canonically sorted")
        if not set(self.anchor_region_ids).issubset(self.resolved_region_ids):
            raise ValueError("scope resolution must include every anchor region")
        if self.mode == RegionScopeMode.SELECTED:
            if self.predicate is not None:
                raise ValueError("selected scope cannot include a similarity predicate")
            if self.resolved_region_ids != self.anchor_region_ids:
                raise ValueError("selected scope must resolve exactly to its anchors")
        elif self.predicate is None:
            raise ValueError("similar scope requires an explicit predicate")
        return self


def derive_region_scope_fingerprint(graph_fingerprint: str, scope: RegionScope) -> str:
    """Fingerprint an exact graph-bound region-scope resolution."""

    return canonical_fingerprint(
        {
            "schema_version": EDITOR_COMMAND_SCHEMA_VERSION,
            "graph_fingerprint": graph_fingerprint,
            "mode": scope.mode.value,
            "anchor_region_ids": list(scope.anchor_region_ids),
            "predicate": (
                scope.predicate.model_dump(mode="json") if scope.predicate is not None else None
            ),
            "resolved_region_ids": list(scope.resolved_region_ids),
        }
    )


class ScopedRegionSelector(GraphSelector):
    scope: RegionScope

    @model_validator(mode="after")
    def resolution_fingerprint_is_exact(self) -> ScopedRegionSelector:
        expected = derive_region_scope_fingerprint(self.graph_fingerprint, self.scope)
        if self.scope.resolution_fingerprint != expected:
            raise ValueError("region scope resolution fingerprint does not match its payload")
        return self


class IslandSelector(RegionSelector):
    classification_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class HoleSelector(GraphSelector):
    feature_ids: tuple[FeatureId, ...] = Field(min_length=1, max_length=256)
    classification_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def feature_ids_are_canonical(self) -> HoleSelector:
        if self.feature_ids != tuple(sorted(set(self.feature_ids))):
            raise ValueError("hole selector IDs must be unique and canonically sorted")
        return self


class EditorCommandModel(EditorModel):
    schema_version: Literal[1] = EDITOR_COMMAND_SCHEMA_VERSION
    command_id: str = Field(pattern=r"^cmd_[A-Za-z0-9_-]{8,80}$")
    source: EditorCommandSource = EditorCommandSource.MANUAL
    created_at: datetime
    provenance: dict[str, JsonValue] = Field(default_factory=dict)

    def canonical_json(self) -> str:
        return canonical_json(self)

    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


class CanvasSelectionCommand(EditorCommandModel):
    """Non-destructive selection snapshot persisted in the revision command log."""

    command_type: Literal["canvas_selection"] = "canvas_selection"
    selector: CanvasSelectionSelector


class KeepRegionsCommand(EditorCommandModel):
    command_type: Literal["keep_regions"] = "keep_regions"
    selector: RegionSelector
    reason: str = Field(default="Explicitly kept by the editor.", min_length=1, max_length=500)


class IslandMergeCommand(EditorCommandModel):
    command_type: Literal["island_merge"] = "island_merge"
    selector: IslandSelector
    analysis_options: IslandAnalysisSettings
    policy: IslandMergePolicy
    explicit_target_label: Optional[int] = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def merge_policy_is_actionable(self) -> IslandMergeCommand:
        if self.policy in {IslandMergePolicy.REVIEW, IslandMergePolicy.KEEP}:
            raise ValueError("island merge commands require an actionable merge policy")
        if self.policy == IslandMergePolicy.EXPLICIT_COLOR:
            if self.explicit_target_label is None:
                raise ValueError("explicit-color island merge requires a target label")
        elif self.explicit_target_label is not None:
            raise ValueError("only explicit-color island merge accepts a target label")
        return self


class HoleCorrectionCommand(EditorCommandModel):
    command_type: Literal["hole_correction"] = "hole_correction"
    selector: HoleSelector
    analysis_options: HoleAnalysisSettings
    policy: HoleCorrectionPolicy
    explicit_target_label: Optional[int] = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def correction_policy_is_actionable(self) -> HoleCorrectionCommand:
        if self.policy in {HoleCorrectionPolicy.REVIEW, HoleCorrectionPolicy.KEEP}:
            raise ValueError("hole correction commands require an actionable correction policy")
        if self.policy == HoleCorrectionPolicy.RECOLOR_CENTER:
            if self.explicit_target_label is None:
                raise ValueError("recolor-center correction requires a target label")
        elif self.explicit_target_label is not None:
            raise ValueError("only recolor-center correction accepts a target label")
        return self


class ContourCleanupCommand(EditorCommandModel):
    command_type: Literal["contour_cleanup"] = "contour_cleanup"
    selector: GraphSelector
    request: ContourCleanupRequest


class ThickenRegionCommand(EditorCommandModel):
    """Physically bounded dilation, suitable for widening a line or enlarging a detail."""

    command_type: Literal["thicken_region"] = "thicken_region"
    selector: RegionSelector
    radius_mm: float = Field(gt=0, le=10)
    editable_labels: tuple[int, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def thickening_is_unambiguous(self) -> ThickenRegionCommand:
        if len(self.selector.region_ids) != 1:
            raise ValueError("thicken-region commands select exactly one source region")
        if self.editable_labels != tuple(sorted(set(self.editable_labels))):
            raise ValueError("thicken editable labels must be unique and canonically sorted")
        return self


class RegionOperationKind(str, Enum):
    PROTECT = "protect"
    MERGE = "merge"
    DELETE = "delete"
    RECOLOR = "recolor"
    THICKEN = "thicken"


class RegionOperationCommand(EditorCommandModel):
    """Exact manual operation with an optional, persisted apply-to-similar expansion."""

    command_type: Literal["region_operation"] = "region_operation"
    selector: ScopedRegionSelector
    operation: RegionOperationKind
    target_label: Optional[int] = Field(default=None, ge=0, le=255)
    radius_mm: Optional[float] = Field(default=None, gt=0, le=10)
    editable_labels: tuple[int, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def operation_parameters_are_unambiguous(self) -> RegionOperationCommand:
        if self.editable_labels != tuple(sorted(set(self.editable_labels))):
            raise ValueError(
                "region-operation editable labels must be unique and canonically sorted"
            )
        if self.operation in {RegionOperationKind.PROTECT, RegionOperationKind.DELETE}:
            if self.target_label is not None or self.radius_mm is not None or self.editable_labels:
                raise ValueError(f"{self.operation.value} does not accept mutation parameters")
        elif self.operation in {RegionOperationKind.MERGE, RegionOperationKind.RECOLOR}:
            if self.target_label is None:
                raise ValueError(f"{self.operation.value} requires a target label")
            if self.radius_mm is not None or self.editable_labels:
                raise ValueError(f"{self.operation.value} accepts only a target label")
        else:
            if self.target_label is not None:
                raise ValueError("thicken does not accept a target label")
            if self.radius_mm is None or not self.editable_labels:
                raise ValueError("thicken requires a radius and editable labels")
        return self


class LocalFillEdit(EditorModel):
    """Paint one exact label into the saved selection, optionally activating transparency."""

    kind: Literal["fill"] = "fill"
    target_label: int = Field(ge=0, le=255)
    activate_transparent: bool = True


class LocalColorCleanupEdit(EditorModel):
    """Replace only explicit source labels inside the saved selection."""

    kind: Literal["color_cleanup"] = "color_cleanup"
    source_labels: tuple[int, ...] = Field(min_length=1, max_length=255)
    target_label: int = Field(ge=0, le=255)

    @model_validator(mode="after")
    def labels_are_actionable(self) -> LocalColorCleanupEdit:
        if self.source_labels != tuple(sorted(set(self.source_labels))):
            raise ValueError("color-cleanup source labels must be unique and canonically sorted")
        if self.target_label in self.source_labels:
            raise ValueError("color-cleanup target label must differ from every source label")
        return self


class LocalMorphologyKind(str, Enum):
    DILATE = "dilate"
    ERODE = "erode"
    OPEN = "open"
    CLOSE = "close"


class LocalMorphologyEdit(EditorModel):
    """Physically bounded binary morphology of one label selected by the user."""

    kind: Literal["morphology"] = "morphology"
    operation: LocalMorphologyKind
    source_label: int = Field(ge=0, le=255)
    radius_mm: float = Field(gt=0, le=25)
    editable_labels: tuple[int, ...] = Field(default=(), max_length=255)
    replacement_label: Optional[int] = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def parameters_match_operation(self) -> LocalMorphologyEdit:
        if self.editable_labels != tuple(sorted(set(self.editable_labels))):
            raise ValueError("morphology editable labels must be unique and canonically sorted")
        if self.source_label in self.editable_labels:
            raise ValueError("morphology editable labels must exclude the source label")
        if self.replacement_label == self.source_label:
            raise ValueError("morphology replacement label must differ from the source label")
        if self.operation in {LocalMorphologyKind.DILATE, LocalMorphologyKind.CLOSE}:
            if not self.editable_labels or self.replacement_label is not None:
                raise ValueError(
                    f"{self.operation.value} requires editable labels and no replacement label"
                )
        elif self.replacement_label is None or self.editable_labels:
            raise ValueError(
                f"{self.operation.value} requires a replacement label and no editable labels"
            )
        return self


class LocalCloneEdit(EditorModel):
    """Clone the selected raster state by an exact physical translation."""

    kind: Literal["clone"] = "clone"
    offset_x_mm: float = Field(ge=-10_000, le=10_000)
    offset_y_mm: float = Field(ge=-10_000, le=10_000)
    overwrite_labels: tuple[int, ...] = Field(min_length=1, max_length=256)
    include_transparent: bool = False
    clip_to_canvas: bool = False

    @model_validator(mode="after")
    def clone_is_actionable(self) -> LocalCloneEdit:
        if self.offset_x_mm == 0 and self.offset_y_mm == 0:
            raise ValueError("clone requires a non-zero physical offset")
        if self.overwrite_labels != tuple(sorted(set(self.overwrite_labels))):
            raise ValueError("clone overwrite labels must be unique and canonically sorted")
        return self


class LocalAffineEdit(EditorModel):
    """Nearest-neighbour resize/warp of the selected raster around its physical bounds."""

    kind: Literal["affine"] = "affine"
    scale_x: float = Field(default=1, ge=0.05, le=20)
    scale_y: float = Field(default=1, ge=0.05, le=20)
    shear_x: float = Field(default=0, ge=-5, le=5)
    shear_y: float = Field(default=0, ge=-5, le=5)
    translate_x_mm: float = Field(default=0, ge=-10_000, le=10_000)
    translate_y_mm: float = Field(default=0, ge=-10_000, le=10_000)
    origin_x: float = Field(default=0.5, ge=0, le=1)
    origin_y: float = Field(default=0.5, ge=0, le=1)
    overwrite_labels: tuple[int, ...] = Field(min_length=1, max_length=256)
    clear_source: bool = True
    source_background_label: Optional[int] = Field(default=None, ge=0, le=255)
    include_transparent: bool = False
    clip_to_canvas: bool = False

    @model_validator(mode="after")
    def affine_is_actionable_and_invertible(self) -> LocalAffineEdit:
        if self.overwrite_labels != tuple(sorted(set(self.overwrite_labels))):
            raise ValueError("affine overwrite labels must be unique and canonically sorted")
        determinant = self.scale_x * self.scale_y - self.shear_x * self.shear_y
        if abs(determinant) < 1e-6:
            raise ValueError("affine edit matrix must be invertible")
        if (
            self.scale_x == 1
            and self.scale_y == 1
            and self.shear_x == 0
            and self.shear_y == 0
            and self.translate_x_mm == 0
            and self.translate_y_mm == 0
        ):
            raise ValueError("affine edit must resize, warp, or translate the selection")
        return self


LocalRasterEdit = Annotated[
    Union[
        LocalFillEdit,
        LocalColorCleanupEdit,
        LocalMorphologyEdit,
        LocalCloneEdit,
        LocalAffineEdit,
    ],
    Field(discriminator="kind"),
]


class LocalRasterEditCommand(EditorCommandModel):
    """Deterministic destructive edit bound to an embedded canonical selection snapshot."""

    command_type: Literal["local_raster_edit"] = "local_raster_edit"
    selector: CanvasSelectionSelector
    edit: LocalRasterEdit


EditorCommand = Annotated[
    Union[
        CanvasSelectionCommand,
        KeepRegionsCommand,
        IslandMergeCommand,
        HoleCorrectionCommand,
        ContourCleanupCommand,
        ThickenRegionCommand,
        RegionOperationCommand,
        LocalRasterEditCommand,
    ],
    Field(discriminator="command_type"),
]
EDITOR_COMMAND_ADAPTER = TypeAdapter(EditorCommand)


class EditorCommandSequence(EditorModel):
    schema_version: Literal[1] = EDITOR_COMMAND_SCHEMA_VERSION
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    commands: tuple[EditorCommand, ...] = Field(default=(), max_length=512)

    @model_validator(mode="after")
    def sequence_is_bound_and_unique(self) -> EditorCommandSequence:
        command_ids = tuple(command.command_id for command in self.commands)
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("editor command IDs must be unique within a sequence")
        for command in self.commands:
            if command.selector.config_fingerprint != self.config_fingerprint:
                raise ValueError("every command selector must match the sequence config")
        return self

    def canonical_json(self) -> str:
        return canonical_json(self)

    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


class EditorPixelTransition(EditorModel):
    source_active: bool
    source_label: Optional[int] = Field(default=None, ge=0, le=255)
    target_active: bool
    target_label: Optional[int] = Field(default=None, ge=0, le=255)
    pixel_count: int = Field(gt=0)
    area_mm2: float = Field(gt=0)

    @model_validator(mode="after")
    def active_states_have_labels(self) -> EditorPixelTransition:
        if self.source_active != (self.source_label is not None):
            raise ValueError("source labels are present exactly when source pixels are active")
        if self.target_active != (self.target_label is not None):
            raise ValueError("target labels are present exactly when target pixels are active")
        if self.source_active == self.target_active and self.source_label == self.target_label:
            raise ValueError("pixel transitions must describe an actual state change")
        return self


class EditorReplayStep(EditorModel):
    index: int = Field(ge=0)
    command_id: str = Field(pattern=r"^cmd_[A-Za-z0-9_-]{8,80}$")
    command_type: EditorCommandType
    command_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_state_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_state_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_pixel_count: int = Field(ge=0)
    changed_area_mm2: float = Field(ge=0)
    changed_mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transitions: tuple[EditorPixelTransition, ...]
    label_areas: tuple[LabelAreaChange, ...]
    protected_pixel_count: int = Field(ge=0)
    newly_protected_pixel_count: int = Field(ge=0)
    engine_record_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    lineage: RegionLineage

    @model_validator(mode="after")
    def step_is_exact(self) -> EditorReplayStep:
        if self.changed_pixel_count != sum(item.pixel_count for item in self.transitions):
            raise ValueError("step transition counts must equal its changed-pixel count")
        if not math.isclose(
            self.changed_area_mm2,
            sum(item.area_mm2 for item in self.transitions),
            abs_tol=1e-12,
        ):
            raise ValueError("step transition areas must equal its changed area")
        if self.transitions != tuple(sorted(self.transitions, key=_transition_sort_key)):
            raise ValueError("step transitions must use canonical ordering")
        if self.label_areas != tuple(sorted(self.label_areas, key=lambda item: item.label)):
            raise ValueError("step label areas must use canonical label ordering")
        if self.lineage.before_graph_fingerprint != self.before_graph_fingerprint:
            raise ValueError("step lineage before graph does not match")
        if self.lineage.after_graph_fingerprint != self.after_graph_fingerprint:
            raise ValueError("step lineage after graph does not match")
        return self


class EditorReplayRecord(EditorModel):
    schema_version: Literal[1] = EDITOR_REPLAY_SCHEMA_VERSION
    sequence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_state_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_state_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_count: int = Field(ge=0)
    steps: tuple[EditorReplayStep, ...]
    operation_changed_pixel_count: int = Field(ge=0)
    changed_pixel_count: int = Field(ge=0)
    changed_area_mm2: float = Field(ge=0)
    changed_mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transitions: tuple[EditorPixelTransition, ...]
    label_areas: tuple[LabelAreaChange, ...]
    protected_pixel_count: int = Field(ge=0)
    final_graph: RegionGraph
    lineage: RegionLineage

    @model_validator(mode="after")
    def replay_is_exact(self) -> EditorReplayRecord:
        if self.command_count != len(self.steps):
            raise ValueError("replay steps must cover every command")
        if tuple(step.index for step in self.steps) != tuple(range(self.command_count)):
            raise ValueError("replay steps must preserve sequential command ordering")
        step_ids = tuple(step.command_id for step in self.steps)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("replay step command IDs must be unique")
        if self.operation_changed_pixel_count != sum(
            step.changed_pixel_count for step in self.steps
        ):
            raise ValueError("operation change total must equal the step change total")
        if self.changed_pixel_count != sum(item.pixel_count for item in self.transitions):
            raise ValueError("net transition counts must equal the changed-pixel count")
        if not math.isclose(
            self.changed_area_mm2,
            sum(item.area_mm2 for item in self.transitions),
            abs_tol=1e-12,
        ):
            raise ValueError("net transition areas must equal the changed area")
        if self.transitions != tuple(sorted(self.transitions, key=_transition_sort_key)):
            raise ValueError("replay transitions must use canonical ordering")
        if self.label_areas != tuple(sorted(self.label_areas, key=lambda item: item.label)):
            raise ValueError("replay label areas must use canonical label ordering")
        if self.final_graph.fingerprint() != self.after_graph_fingerprint:
            raise ValueError("embedded final graph does not match the after fingerprint")
        if self.lineage.before_graph_fingerprint != self.before_graph_fingerprint:
            raise ValueError("replay lineage before graph does not match")
        if self.lineage.after_graph_fingerprint != self.after_graph_fingerprint:
            raise ValueError("replay lineage after graph does not match")
        return self

    def canonical_json(self) -> str:
        return canonical_json(self)

    def fingerprint(self) -> str:
        return canonical_fingerprint(self)


def validate_editor_command(value: Any) -> EditorCommand:
    return EDITOR_COMMAND_ADAPTER.validate_python(value)


def _transition_sort_key(
    transition: EditorPixelTransition,
) -> tuple[bool, int, bool, int]:
    return (
        transition.source_active,
        -1 if transition.source_label is None else transition.source_label,
        transition.target_active,
        -1 if transition.target_label is None else transition.target_label,
    )
