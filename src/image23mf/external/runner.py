"""No-shell subprocess execution with bounded logs, timeout, and cancellation."""

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_LOG_BYTES = 1024 * 1024
POLL_SECONDS = 0.02
TERMINATE_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class ToolRunResult:
    command: tuple[str, ...]
    return_code: Optional[int]
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    canceled: bool = False


class ToolRunnerError(RuntimeError):
    def __init__(self, message: str, result: Optional[ToolRunResult] = None) -> None:
        super().__init__(message)
        self.result = result


class ToolUnavailableError(ToolRunnerError):
    pass


class ToolExecutionError(ToolRunnerError):
    pass


class ToolTimeoutError(ToolRunnerError):
    pass


class ToolCanceledError(ToolRunnerError):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def canceled(self) -> bool:
        return self._event.is_set()


class ToolRunner:
    def __init__(
        self,
        *,
        temp_root: Optional[Path] = None,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    ) -> None:
        if max_log_bytes < 1024:
            raise ValueError("max_log_bytes must be at least 1024")
        self.temp_root = temp_root.expanduser().resolve() if temp_root else None
        self.max_log_bytes = max_log_bytes

    @contextmanager
    def temporary_workspace(self, *, prefix: str = "tool-") -> Iterator[Path]:
        if self.temp_root is not None:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=prefix, dir=self.temp_root) as directory:
            yield Path(directory).resolve()

    def run(
        self,
        executable: Path,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cancellation: Optional[CancellationToken] = None,
        working_directory: Optional[Path] = None,
        environment: Optional[Mapping[str, str]] = None,
        check: bool = True,
        redact_argument_indexes: Sequence[int] = (),
    ) -> ToolRunResult:
        executable = executable.expanduser().resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ToolUnavailableError(f"executable is missing or not executable: {executable}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        normalized_arguments = tuple(str(item) for item in arguments)
        if any("\x00" in item for item in normalized_arguments):
            raise ValueError("tool arguments cannot contain NUL bytes")
        redacted = frozenset(redact_argument_indexes)
        if any(index < 0 or index >= len(normalized_arguments) for index in redacted):
            raise ValueError("redacted argument index is out of range")
        display_command = (str(executable),) + tuple(
            "<redacted>" if index in redacted else value
            for index, value in enumerate(normalized_arguments)
        )

        if cancellation is not None and cancellation.canceled:
            result = ToolRunResult(
                command=display_command,
                return_code=None,
                stdout="",
                stderr="",
                duration_seconds=0,
                canceled=True,
            )
            raise ToolCanceledError("tool execution was canceled before launch", result)

        if working_directory is None:
            with self.temporary_workspace() as temporary:
                return self._run_in_directory(
                    executable,
                    normalized_arguments,
                    display_command,
                    timeout_seconds=timeout_seconds,
                    cancellation=cancellation,
                    working_directory=temporary,
                    environment=environment,
                    check=check,
                )
        directory = working_directory.expanduser().resolve()
        if not directory.is_dir():
            raise ValueError("working_directory must be an existing directory")
        return self._run_in_directory(
            executable,
            normalized_arguments,
            display_command,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
            working_directory=directory,
            environment=environment,
            check=check,
        )

    def _run_in_directory(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        display_command: tuple[str, ...],
        *,
        timeout_seconds: float,
        cancellation: Optional[CancellationToken],
        working_directory: Path,
        environment: Optional[Mapping[str, str]],
        check: bool,
    ) -> ToolRunResult:
        stdout_path: Optional[Path] = None
        stderr_path: Optional[Path] = None
        started = time.monotonic()
        process: Optional[subprocess.Popen[bytes]] = None
        timed_out = False
        canceled = False
        try:
            with (
                tempfile.NamedTemporaryFile(
                    mode="w+b", prefix="stdout-", dir=working_directory, delete=False
                ) as stdout_file,
                tempfile.NamedTemporaryFile(
                    mode="w+b", prefix="stderr-", dir=working_directory, delete=False
                ) as stderr_file,
            ):
                stdout_path = Path(stdout_file.name)
                stderr_path = Path(stderr_file.name)
                process = subprocess.Popen(
                    [str(executable), *arguments],
                    cwd=working_directory,
                    env=_execution_environment(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=True,
                )
                while process.poll() is None:
                    if cancellation is not None and cancellation.canceled:
                        canceled = True
                        _terminate_process(process)
                        break
                    if time.monotonic() - started >= timeout_seconds:
                        timed_out = True
                        _terminate_process(process)
                        break
                    time.sleep(POLL_SECONDS)
                return_code = process.wait()

            result = ToolRunResult(
                command=display_command,
                return_code=return_code,
                stdout=self._read_log(stdout_path),
                stderr=self._read_log(stderr_path),
                duration_seconds=time.monotonic() - started,
                timed_out=timed_out,
                canceled=canceled,
            )
            if canceled:
                raise ToolCanceledError("tool execution was canceled", result)
            if timed_out:
                raise ToolTimeoutError(
                    f"tool execution exceeded {timeout_seconds:g} seconds", result
                )
            if check and result.return_code != 0:
                raise ToolExecutionError(f"tool exited with status {result.return_code}", result)
            return result
        finally:
            if process is not None and process.poll() is None:
                _terminate_process(process)
            if stdout_path is not None:
                stdout_path.unlink(missing_ok=True)
            if stderr_path is not None:
                stderr_path.unlink(missing_ok=True)

    def _read_log(self, path: Path) -> str:
        size = path.stat().st_size
        if size <= self.max_log_bytes:
            payload = path.read_bytes()
        else:
            half = self.max_log_bytes // 2
            with path.open("rb") as source:
                head = source.read(half)
                source.seek(-half, os.SEEK_END)
                tail = source.read(half)
            omitted = size - len(head) - len(tail)
            payload = head + f"\n... {omitted} log bytes omitted ...\n".encode() + tail
        return payload.decode("utf-8", errors="replace")


def _execution_environment(overrides: Optional[Mapping[str, str]]) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    if overrides:
        for key, value in overrides.items():
            if "\x00" in key or "=" in key or "\x00" in value:
                raise ValueError("invalid environment override")
            environment[key] = value
    return environment


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    process.wait()
