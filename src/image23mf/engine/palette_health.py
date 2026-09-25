"""Deterministic palette ambiguity detection and reversible resolution plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

import numpy as np

from image23mf.engine.palette import (
    QuantizationResult,
    delta_e_76,
    normalize_palette,
    rgb_to_lab,
)


class PaletteHealthCode(str, Enum):
    AUTO_PALETTE_COLLAPSE = "auto_palette_collapse"
    DUPLICATE_COLORS = "duplicate_colors"
    NEAR_IDENTICAL_COLORS = "near_identical_colors"
    ABSENT_COLORS = "absent_colors"
    LOCKED_CONFLICT = "locked_conflict"


class PaletteResolutionKind(str, Enum):
    MERGE = "merge"
    REPLACE = "replace"
    CONTINUE = "continue"


@dataclass(frozen=True)
class PaletteHealthOptions:
    minimum_delta_e: float = 3.0
    alpha_threshold: int = 0

    def __post_init__(self) -> None:
        if not np.isfinite(self.minimum_delta_e) or not 0 < self.minimum_delta_e <= 100:
            raise ValueError("minimum_delta_e must be finite, greater than zero, and at most 100")
        if self.alpha_threshold < 0 or self.alpha_threshold > 254:
            raise ValueError("alpha_threshold must be between 0 and 254")

    def fingerprint(self) -> str:
        return _sha256_json(
            {
                "alpha_threshold": self.alpha_threshold,
                "minimum_delta_e": self.minimum_delta_e,
            }
        )


DEFAULT_PALETTE_HEALTH_OPTIONS = PaletteHealthOptions()


@dataclass(frozen=True)
class PaletteColorCoverage:
    index: int
    color: str
    pixel_count: int
    coverage_ratio: float
    locked: bool


@dataclass(frozen=True)
class PaletteResolutionAction:
    kind: PaletteResolutionKind
    enabled: bool
    source_index: int | None
    target_index: int | None
    requires_replacement_color: bool
    message: str


@dataclass(frozen=True)
class PaletteHealthIssue:
    id: str
    code: PaletteHealthCode
    indices: tuple[int, ...]
    locked_indices: tuple[int, ...]
    minimum_delta_e: float | None
    message: str
    actions: tuple[PaletteResolutionAction, ...]


@dataclass(frozen=True)
class PaletteHealthReport:
    palette: tuple[str, ...]
    colors: tuple[PaletteColorCoverage, ...]
    issues: tuple[PaletteHealthIssue, ...]
    visible_pixel_count: int
    assigned_color_count: int
    unique_color_count: int
    minimum_pair_delta_e: float
    options_fingerprint: str

    @property
    def needs_review(self) -> bool:
        return bool(self.issues)

    def issue_codes(self) -> tuple[PaletteHealthCode, ...]:
        return tuple(issue.code for issue in self.issues)

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "assigned_color_count": self.assigned_color_count,
            "colors": [
                {
                    "color": color.color,
                    "coverage_ratio": color.coverage_ratio,
                    "index": color.index,
                    "locked": color.locked,
                    "pixel_count": color.pixel_count,
                }
                for color in self.colors
            ],
            "issues": [
                {
                    "actions": [
                        {
                            "enabled": action.enabled,
                            "kind": action.kind.value,
                            "message": action.message,
                            "requires_replacement_color": action.requires_replacement_color,
                            "source_index": action.source_index,
                            "target_index": action.target_index,
                        }
                        for action in issue.actions
                    ],
                    "code": issue.code.value,
                    "id": issue.id,
                    "indices": list(issue.indices),
                    "locked_indices": list(issue.locked_indices),
                    "message": issue.message,
                    "minimum_delta_e": issue.minimum_delta_e,
                }
                for issue in self.issues
            ],
            "minimum_pair_delta_e": self.minimum_pair_delta_e,
            "options_fingerprint": self.options_fingerprint,
            "palette": list(self.palette),
            "unique_color_count": self.unique_color_count,
            "visible_pixel_count": self.visible_pixel_count,
        }


@dataclass(frozen=True)
class PaletteResolutionRequest:
    kind: PaletteResolutionKind
    source_index: int | None = None
    target_index: int | None = None
    replacement_color: str | None = None


@dataclass(frozen=True)
class ResolvedPalette:
    palette: tuple[str, ...]
    old_to_new_indices: tuple[int, ...]
    locked_indices: tuple[int, ...]
    action: PaletteResolutionKind


def analyze_palette_health(
    result: QuantizationResult,
    *,
    locked_indices: tuple[int, ...] = (),
    options: PaletteHealthOptions = DEFAULT_PALETTE_HEALTH_OPTIONS,
) -> PaletteHealthReport:
    """Detect palette collapse, duplication, absent assignments, and weak separation."""

    palette = normalize_palette(result.palette)
    locked = _normalize_locked_indices(locked_indices, len(palette))
    labels = np.frombuffer(result.labels.pixels, dtype=np.uint8)
    alpha = np.frombuffer(result.alpha, dtype=np.uint8)
    visible = alpha > options.alpha_threshold
    visible_pixel_count = int(np.count_nonzero(visible))
    counts = (
        np.bincount(labels[visible], minlength=len(palette))
        if visible_pixel_count
        else np.zeros(len(palette), dtype=np.int64)
    )
    coverage = tuple(
        PaletteColorCoverage(
            index=index,
            color=color,
            pixel_count=int(counts[index]),
            coverage_ratio=(
                float(counts[index]) / visible_pixel_count if visible_pixel_count else 0
            ),
            locked=index in locked,
        )
        for index, color in enumerate(palette)
    )
    labs = tuple(rgb_to_lab(_hex_to_rgb(color)) for color in palette)
    pair_distances = {
        (first, second): delta_e_76(labs[first], labs[second])
        for first in range(len(palette))
        for second in range(first + 1, len(palette))
    }
    minimum_pair = min(pair_distances.values())
    issues: list[PaletteHealthIssue] = []

    if result.fit is not None and result.fit.unique_color_count < len(palette):
        indices = tuple(range(len(palette)))
        issues.append(
            _issue(
                PaletteHealthCode.AUTO_PALETTE_COLLAPSE,
                indices,
                locked,
                None,
                f"The automatic fit produced only {result.fit.unique_color_count} distinct "
                f"colors for {len(palette)} requested slots.",
                palette,
                counts,
                labs,
            )
        )

    duplicate_groups = _groups_from_pairs(
        len(palette),
        tuple(pair for pair, distance in pair_distances.items() if distance == 0),
    )
    for indices in duplicate_groups:
        issues.append(
            _issue(
                PaletteHealthCode.DUPLICATE_COLORS,
                indices,
                locked,
                0.0,
                "Palette slots "
                f"{', '.join(str(index + 1) for index in indices)} use the same color.",
                palette,
                counts,
                labs,
            )
        )

    near_groups = _groups_from_pairs(
        len(palette),
        tuple(
            pair
            for pair, distance in pair_distances.items()
            if 0 < distance < options.minimum_delta_e
        ),
    )
    for indices in near_groups:
        distance = min(
            pair_distances[(first, second)]
            for position, first in enumerate(indices)
            for second in indices[position + 1 :]
        )
        issues.append(
            _issue(
                PaletteHealthCode.NEAR_IDENTICAL_COLORS,
                indices,
                locked,
                distance,
                f"Palette slots {', '.join(str(index + 1) for index in indices)} are separated "
                f"by only ΔE {distance:.2f}.",
                palette,
                counts,
                labs,
            )
        )

    absent = tuple(index for index, count in enumerate(counts) if count == 0)
    if absent:
        issues.append(
            _issue(
                PaletteHealthCode.ABSENT_COLORS,
                absent,
                locked,
                None,
                f"{len(absent)} palette {'color has' if len(absent) == 1 else 'colors have'} "
                "no visible pixel assignments.",
                palette,
                counts,
                labs,
            )
        )

    conflict_indices = tuple(
        sorted(
            {
                index
                for issue in issues
                if issue.code
                in {PaletteHealthCode.DUPLICATE_COLORS, PaletteHealthCode.NEAR_IDENTICAL_COLORS}
                and issue.indices
                and all(index in locked for index in issue.indices)
                for index in issue.indices
            }
        )
    )
    if conflict_indices:
        issues.append(
            _issue(
                PaletteHealthCode.LOCKED_CONFLICT,
                conflict_indices,
                locked,
                None,
                "Conflicting colors are all locked; unlock one before merging or replacing it.",
                palette,
                counts,
                labs,
            )
        )

    return PaletteHealthReport(
        palette=palette,
        colors=coverage,
        issues=tuple(issues),
        visible_pixel_count=visible_pixel_count,
        assigned_color_count=int(np.count_nonzero(counts)),
        unique_color_count=len(set(palette)),
        minimum_pair_delta_e=minimum_pair,
        options_fingerprint=options.fingerprint(),
    )


def resolve_palette(
    palette: tuple[str, ...],
    request: PaletteResolutionRequest,
    *,
    locked_indices: tuple[int, ...] = (),
) -> ResolvedPalette:
    """Apply merge/replace/continue without mutating saved input or label bytes."""

    normalized = normalize_palette(palette)
    locked = _normalize_locked_indices(locked_indices, len(normalized))
    if request.kind == PaletteResolutionKind.CONTINUE:
        if any(
            value is not None
            for value in (request.source_index, request.target_index, request.replacement_color)
        ):
            raise ValueError("continue does not accept source, target, or replacement values")
        return ResolvedPalette(
            palette=normalized,
            old_to_new_indices=tuple(range(len(normalized))),
            locked_indices=locked,
            action=request.kind,
        )

    source = _require_palette_index(request.source_index, len(normalized), "source")
    if source in locked:
        raise ValueError("a locked palette color cannot be merged or replaced")
    if request.kind == PaletteResolutionKind.REPLACE:
        if request.target_index is not None:
            raise ValueError("replace does not accept a target index")
        if request.replacement_color is None:
            raise ValueError("replace requires a replacement color")
        comparison = normalized[(source + 1) % len(normalized)]
        replacement = normalize_palette((request.replacement_color, comparison))[0]
        changed = list(normalized)
        changed[source] = replacement
        return ResolvedPalette(
            palette=tuple(changed),
            old_to_new_indices=tuple(range(len(normalized))),
            locked_indices=locked,
            action=request.kind,
        )

    if request.kind != PaletteResolutionKind.MERGE:
        raise ValueError(f"unsupported palette resolution: {request.kind}")
    if request.replacement_color is not None:
        raise ValueError("merge does not accept a replacement color")
    target = _require_palette_index(request.target_index, len(normalized), "target")
    if source == target:
        raise ValueError("merge source and target must be different")
    if len(normalized) <= 2:
        raise ValueError("a palette cannot be merged below two colors")
    changed = tuple(color for index, color in enumerate(normalized) if index != source)
    target_after = target - int(target > source)
    mapping = tuple(
        target_after if index == source else index - int(index > source)
        for index in range(len(normalized))
    )
    remapped_locks = tuple(sorted({mapping[index] for index in locked}))
    return ResolvedPalette(
        palette=changed,
        old_to_new_indices=mapping,
        locked_indices=remapped_locks,
        action=request.kind,
    )


def _issue(
    code: PaletteHealthCode,
    indices: tuple[int, ...],
    locked: tuple[int, ...],
    minimum_delta_e: float | None,
    message: str,
    palette: tuple[str, ...],
    counts: np.ndarray,
    labs: tuple[tuple[float, float, float], ...],
) -> PaletteHealthIssue:
    locked_in_issue = tuple(index for index in indices if index in locked)
    unlocked = tuple(index for index in indices if index not in locked)
    absent_unlocked = tuple(index for index in unlocked if counts[index] == 0)
    source_pool = absent_unlocked or unlocked
    source = next((index for index in reversed(source_pool)), None)
    target = _merge_target(source, indices, counts, labs) if source is not None else None
    merge_enabled = source is not None and target is not None and len(palette) > 2
    replace_enabled = source is not None
    return PaletteHealthIssue(
        id=f"{code.value}:{','.join(str(index) for index in indices)}",
        code=code,
        indices=indices,
        locked_indices=locked_in_issue,
        minimum_delta_e=minimum_delta_e,
        message=message,
        actions=(
            PaletteResolutionAction(
                kind=PaletteResolutionKind.MERGE,
                enabled=merge_enabled,
                source_index=source,
                target_index=target,
                requires_replacement_color=False,
                message=(
                    "Remove the selected slot and map it to its nearest remaining color."
                    if merge_enabled
                    else (
                        "Merge is unavailable while every affected slot is locked or only two "
                        "colors remain."
                    )
                ),
            ),
            PaletteResolutionAction(
                kind=PaletteResolutionKind.REPLACE,
                enabled=replace_enabled,
                source_index=source,
                target_index=None,
                requires_replacement_color=True,
                message=(
                    "Choose a more distinct filament color for the selected slot."
                    if replace_enabled
                    else "Replace is unavailable while every affected slot is locked."
                ),
            ),
            PaletteResolutionAction(
                kind=PaletteResolutionKind.CONTINUE,
                enabled=True,
                source_index=None,
                target_index=None,
                requires_replacement_color=False,
                message="Keep the palette unchanged and acknowledge the printability risk.",
            ),
        ),
    )


def _merge_target(
    source: int | None,
    issue_indices: tuple[int, ...],
    counts: np.ndarray,
    labs: tuple[tuple[float, float, float], ...],
) -> int | None:
    if source is None:
        return None
    candidates = tuple(index for index in range(len(counts)) if index != source)
    if not candidates:
        return None
    preferred = tuple(index for index in candidates if counts[index] > 0)
    pool = preferred or tuple(index for index in issue_indices if index != source) or candidates
    return min(pool, key=lambda index: (delta_e_76(labs[source], labs[index]), index))


def _groups_from_pairs(
    color_count: int, pairs: tuple[tuple[int, int], ...]
) -> tuple[tuple[int, ...], ...]:
    neighbors = {index: set() for index in range(color_count)}
    for first, second in pairs:
        neighbors[first].add(second)
        neighbors[second].add(first)
    groups = []
    visited = set()
    for start in range(color_count):
        if start in visited or not neighbors[start]:
            continue
        pending = [start]
        component = set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(sorted(neighbors[current] - component, reverse=True))
        visited.update(component)
        groups.append(tuple(sorted(component)))
    return tuple(groups)


def _normalize_locked_indices(indices: tuple[int, ...], color_count: int) -> tuple[int, ...]:
    if len(set(indices)) != len(indices):
        raise ValueError("locked palette indices must be unique")
    for index in indices:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < color_count:
            raise ValueError(f"locked palette index is outside the palette: {index!r}")
    return tuple(sorted(indices))


def _require_palette_index(value: int | None, color_count: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < color_count:
        raise ValueError(f"{label} palette index is required and must be inside the palette")
    return value


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))


def _sha256_json(value: dict[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
