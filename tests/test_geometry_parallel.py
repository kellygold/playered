import multiprocessing
import os
import time

import pytest

from image23mf.geometry.parallel import geometry_worker_count, process_map


def _work(delay):
    time.sleep(delay)
    return os.getpid(), delay


def _fail(value):
    raise ValueError("worker failed")


def test_workers_use_distinct_processes_and_preserve_order():
    result = process_map(_work, [0.1, 0.15, 0.1, 0.15], max_workers=2)
    assert len({pid for pid, _ in result}) == 2
    assert all(pid != os.getpid() for pid, _ in result)
    assert [delay for _, delay in result] == [0.1, 0.15, 0.1, 0.15]


def test_worker_failure_is_propagated_and_children_are_reaped():
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(ValueError, match="worker failed"):
        process_map(_fail, [1, 2], max_workers=2)
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_cancel_terminates_running_children():
    before = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()

    def check():
        if time.monotonic() - started > 0.4:
            raise RuntimeError("canceled")

    with pytest.raises(RuntimeError, match="canceled"):
        process_map(_work, [10, 10], max_workers=2, check_canceled=check)
    assert time.monotonic() - started < 5
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_small_work_stays_serial_and_large_work_is_bounded(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 18)
    assert geometry_worker_count(2, 10000) == 1
    assert geometry_worker_count(400, 100) == 1
    assert geometry_worker_count(400, 10000) == 8
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    assert geometry_worker_count(400, 10000) == 1


def _crash(value):
    os._exit(7)


def test_abrupt_worker_exit_fails_without_hanging_or_leaking_children():
    before = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()

    def deadline():
        if time.monotonic() - started > 5:
            raise TimeoutError("crash detection did not finish")

    with pytest.raises(RuntimeError, match="worker exited"):
        process_map(_crash, [1, 2], max_workers=2, check_canceled=deadline)
    assert {p.pid for p in multiprocessing.active_children()} == before
