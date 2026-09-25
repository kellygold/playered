"""Geometry sizing policy and shared cancellable process execution."""

import os

from image23mf.parallel import process_map as process_map


def geometry_worker_count(task_count: int, complexity: int) -> int:
    """Leave headroom for the UI; avoid spawn overhead for small artwork."""
    if task_count < 4 or complexity < 2000:
        return 1
    return min(task_count, 8, max(1, (os.cpu_count() or 1) - 2))
