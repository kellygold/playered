import threading
import time
from pathlib import Path

import pytest

from image23mf.external import (
    CancellationToken,
    ToolCanceledError,
    ToolExecutionError,
    ToolRunner,
    ToolTimeoutError,
    ToolUnavailableError,
)


def script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_arguments_are_passed_literally_without_shell_interpolation(tmp_path) -> None:
    tool = script(tmp_path, "echo-args", "printf '<%s>\\n' \"$@\"")
    marker = tmp_path / "must-not-exist"
    malicious = f"$(touch {marker})"

    result = ToolRunner(temp_root=tmp_path / "runs").run(
        tool, ("value with spaces", malicious, "semi;colon")
    )

    assert result.return_code == 0
    assert result.stdout.splitlines() == [
        "<value with spaces>",
        f"<{malicious}>",
        "<semi;colon>",
    ]
    assert not marker.exists()


def test_nonzero_exit_captures_bounded_stdout_stderr_and_redacts_display_command(
    tmp_path,
) -> None:
    tool = script(
        tmp_path,
        "fail",
        "printf 'helpful stdout\\n'; printf 'bad input\\n' >&2; exit 23",
    )

    with pytest.raises(ToolExecutionError) as raised:
        ToolRunner(temp_root=tmp_path / "runs").run(
            tool, ("--token", "secret-value"), redact_argument_indexes=(1,)
        )

    result = raised.value.result
    assert result is not None
    assert result.return_code == 23
    assert result.stdout == "helpful stdout\n"
    assert result.stderr == "bad input\n"
    assert result.command[-1] == "<redacted>"
    assert "secret-value" not in str(raised.value)


def test_timeout_terminates_process_group_and_cleans_automatic_workspace(tmp_path) -> None:
    tool = script(tmp_path, "slow", "sleep 10")
    temp_root = tmp_path / "runs"

    with pytest.raises(ToolTimeoutError) as raised:
        ToolRunner(temp_root=temp_root).run(tool, timeout_seconds=0.05)

    assert raised.value.result is not None
    assert raised.value.result.timed_out
    assert raised.value.result.duration_seconds < 2
    assert list(temp_root.iterdir()) == []


def test_cancellation_stops_running_process_and_prelaunch_cancel_starts_nothing(tmp_path) -> None:
    tool = script(tmp_path, "cancel", "sleep 10")
    runner = ToolRunner(temp_root=tmp_path / "runs")
    token = CancellationToken()

    timer = threading.Timer(0.05, token.cancel)
    timer.start()
    try:
        with pytest.raises(ToolCanceledError) as raised:
            runner.run(tool, cancellation=token)
    finally:
        timer.cancel()
    assert raised.value.result is not None
    assert raised.value.result.canceled

    already_canceled = CancellationToken()
    already_canceled.cancel()
    before = time.monotonic()
    with pytest.raises(ToolCanceledError) as prelaunch:
        runner.run(tool, cancellation=already_canceled)
    assert prelaunch.value.result is not None
    assert prelaunch.value.result.return_code is None
    assert time.monotonic() - before < 0.5


def test_missing_executable_invalid_metadata_and_log_truncation(tmp_path) -> None:
    runner = ToolRunner(temp_root=tmp_path / "runs", max_log_bytes=1024)
    with pytest.raises(ToolUnavailableError):
        runner.run(tmp_path / "missing")

    tool = script(
        tmp_path,
        "large-log",
        "i=0; while [ $i -lt 3000 ]; do printf x; i=$((i+1)); done",
    )
    result = runner.run(tool)
    assert "log bytes omitted" in result.stdout
    assert len(result.stdout) < 1200

    with pytest.raises(ValueError, match="NUL"):
        runner.run(tool, ("bad\x00argument",))
