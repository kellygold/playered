"""Bounded, cancellable process workers for independent CPU-heavy tasks."""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable, Sequence
from typing import Optional, TypeVar

# ruff: noqa: UP045 -- supported Python 3.9 annotations.

T = TypeVar("T")
R = TypeVar("R")


def process_map(
    function: Callable[[T], R],
    tasks: Sequence[T],
    *,
    max_workers: int,
    check_canceled: Optional[Callable[[], None]] = None,
) -> list[R]:
    """Keep input order and terminate child work on cancellation or a worker error."""
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    if check_canceled:
        check_canceled()
    if max_workers == 1 or len(tasks) < 2:
        results = []
        for task in tasks:
            if check_canceled:
                check_canceled()
            results.append(function(task))
        return results
    # macOS must spawn: forking a threaded API process can deadlock native libraries.
    pool = multiprocessing.get_context("spawn").Pool(min(max_workers, len(tasks)))
    try:
        # Pool replaces crashed workers silently, leaving their result slots unresolved.
        # With no worker recycling configured, any original worker exit is unexpected.
        workers = tuple(pool._pool)
        pending = pool.imap(function, tasks, chunksize=1)
        results = []
        while len(results) < len(tasks):
            if check_canceled:
                check_canceled()
            try:
                results.append(pending.next(timeout=0.1))
            except multiprocessing.TimeoutError:
                if any(worker.exitcode is not None for worker in workers):
                    raise RuntimeError(
                        "processing worker exited before returning a result"
                    ) from None
                continue
        pool.close()
        return results
    except BaseException:
        pool.terminate()
        raise
    finally:
        pool.join()
