"""Conservative bounding-box candidates; callers retain their exact geometry predicates."""

from __future__ import annotations

# ruff: noqa: UP045 -- keep annotations compatible with Python 3.9.
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

Bounds = tuple[float, float, float, float]


def bounds_overlap(first: Bounds, second: Bounds, tolerance: float) -> bool:
    return not (
        first[2] < second[0] - tolerance
        or second[2] < first[0] - tolerance
        or first[3] < second[1] - tolerance
        or second[3] < first[1] - tolerance
    )


@dataclass(frozen=True)
class _Node:
    bounds: Bounds
    indices: tuple[int, ...] = ()
    left: Optional[_Node] = None
    right: Optional[_Node] = None


class BoundsIndex:
    """Static median-split tree with stable, original-order query results."""

    def __init__(self, bounds: Sequence[Bounds], *, tolerance: float) -> None:
        self.bounds = tuple(bounds)
        self.tolerance = tolerance
        self.root = self._build(tuple(range(len(bounds)))) if bounds else None

    def _build(self, indices: tuple[int, ...]) -> _Node:
        box = (
            min(self.bounds[i][0] for i in indices),
            min(self.bounds[i][1] for i in indices),
            max(self.bounds[i][2] for i in indices),
            max(self.bounds[i][3] for i in indices),
        )
        if len(indices) <= 16:
            return _Node(box, indices)
        axis = 0 if box[2] - box[0] >= box[3] - box[1] else 1
        ordered = tuple(
            sorted(
                indices,
                key=lambda i: (
                    self.bounds[i][axis] + self.bounds[i][axis + 2],
                    i,
                ),
            )
        )
        middle = len(ordered) // 2
        return _Node(box, left=self._build(ordered[:middle]), right=self._build(ordered[middle:]))

    def query(self, bounds: Bounds) -> list[int]:
        found: list[int] = []
        pending = [self.root] if self.root is not None else []
        while pending:
            node = pending.pop()
            if not bounds_overlap(node.bounds, bounds, self.tolerance):
                continue
            if node.indices:
                found.extend(
                    i
                    for i in node.indices
                    if bounds_overlap(self.bounds[i], bounds, self.tolerance)
                )
            else:
                if node.left is not None:
                    pending.append(node.left)
                if node.right is not None:
                    pending.append(node.right)
        return sorted(found)
