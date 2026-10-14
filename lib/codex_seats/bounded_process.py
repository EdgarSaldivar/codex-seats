"""Bounded synchronous subprocess execution for macOS and Linux.

Every command gets a new session (and therefore a process group whose id is the
child pid).  A timeout addresses only that group: TERM, a bounded grace period,
then KILL.  The direct child is reaped before this module returns or raises.
"""

from __future__ import annotations

import math
import os
import signal
import selectors
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

DEFAULT_TERM_GRACE_SECONDS = 1.0


class ProcessCancelledError(RuntimeError):
    """Managed shutdown requested; never a retryable provider failure."""


@dataclass(frozen=True)
class ProcessResult:
    """Exact output and status from one completed command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    pid: int

    def stdout_text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        """Decode stdout explicitly while retaining the original bytes."""
        return self.stdout.decode(encoding, errors)

    def stderr_text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        """Decode stderr explicitly while retaining the original bytes."""
        return self.stderr.decode(encoding, errors)


class ProcessError(RuntimeError):
    """Base class for a bounded process failure."""

    def __init__(self, message: str, argv: Sequence[str]) -> None:
        super().__init__(message)
        self.argv = tuple(argv)


class ProcessLaunchError(ProcessError):
    """The command could not be started or isolated in its own process group."""


class ProcessOutputLimitError(ProcessError):
    """Combined captured output exceeded the caller's memory budget."""


class ProcessCleanupError(ProcessError):
    """A killed direct child could not be reaped inside the cleanup deadline."""

    def __init__(self, argv: Sequence[str], pid: int) -> None:
        super().__init__(
            f"could not reap killed child {pid} inside the cleanup deadline", argv
        )
        self.pid = pid


class ProcessTimeoutError(ProcessError):
    """The command exceeded its deadline after bounded isolated-group cleanup."""

    def __init__(self, result: ProcessResult, timeout: float) -> None:
        super().__init__(
            f"{' '.join(result.argv)} timed out after {timeout:g}s", result.argv
        )
        self.result = result
        self.timeout = timeout


class ProcessCommandError(ProcessError):
    """The command completed with a non-zero status."""

    def __init__(self, result: ProcessResult) -> None:
        detail = result.stderr or result.stdout
        rendered = detail.decode("utf-8", "replace").rstrip("\n")
        super().__init__(
            f"{' '.join(result.argv)} failed: "
            f"{rendered or f'exit {result.returncode}'}",
            result.argv,
        )
        self.result = result


def _positive(value: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive and finite") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return number


def _argv(arguments: Sequence[str]) -> tuple[str, ...]:
    if isinstance(arguments, (str, bytes)):
        raise TypeError("process argv must be a sequence of NUL-free strings")
    argv = tuple(arguments)
    if not argv or not all(
        isinstance(item, str) and "\x00" not in item for item in argv
    ):
        raise TypeError("process argv must be a non-empty sequence of NUL-free strings")
    return argv


def _signal_group(
    process: subprocess.Popen[bytes], group: Optional[int], sig: int
) -> None:
    """Signal only the new group verified for this child at launch."""
    # A numeric PGID can be reused after its leader exits. Never signal it unless
    # the original child is still alive and still proves membership immediately
    # before killpg; bounding cleanup is safer than touching an unrelated group.
    if group is None or process.poll() is not None:
        return
    try:
        if os.getpgid(process.pid) != group:
            return
    except OSError:
        return
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        pass


def _result(
    process: subprocess.Popen[bytes],
    argv: tuple[str, ...],
    stdout: Optional[bytes],
    stderr: Optional[bytes],
) -> ProcessResult:
    return ProcessResult(
        argv,
        int(process.returncode),
        stdout if isinstance(stdout, bytes) else b"",
        stderr if isinstance(stderr, bytes) else b"",
        process.pid,
    )


def _close_capture_streams(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _final_communicate(
    process: subprocess.Popen[bytes], argv: tuple[str, ...], cleanup_timeout: float
) -> tuple[bytes, bytes]:
    """Bound pipe drain after KILL, even if an escaped descendant holds the FDs."""
    try:
        stdout, stderr = process.communicate(timeout=cleanup_timeout)
        return (
            stdout if isinstance(stdout, bytes) else b"",
            stderr if isinstance(stderr, bytes) else b"",
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output if isinstance(exc.output, bytes) else b""
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        _close_capture_streams(process)
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=cleanup_timeout)
            except subprocess.TimeoutExpired as wait_error:
                raise ProcessCleanupError(argv, process.pid) from wait_error
        return stdout, stderr


def _terminate_and_reap(
    process: subprocess.Popen[Any],
    argv: tuple[str, ...],
    group: Optional[int],
    term_grace: float,
) -> tuple[bytes, bytes]:
    """Apply the bounded group shutdown and reap the direct child."""
    _signal_group(process, group, signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=term_grace)
        return (
            stdout if isinstance(stdout, bytes) else b"",
            stderr if isinstance(stderr, bytes) else b"",
        )
    except subprocess.TimeoutExpired:
        _signal_group(process, group, signal.SIGKILL)
        return _final_communicate(process, argv, term_grace)


def terminate_and_reap(
    process: subprocess.Popen[Any],
    process_group_id: int,
    arguments: Sequence[str],
    *,
    term_grace: float = DEFAULT_TERM_GRACE_SECONDS,
) -> None:
    """Boundedly stop one verified direct child's isolated process group.

    Callers retain the launch-time group identity and this module re-verifies the
    live child immediately before each signal, so a reused numeric group is never
    targeted.
    """
    argv = _argv(arguments)
    term_grace = _positive(term_grace, "process TERM grace")
    if process_group_id != process.pid:
        raise ValueError("process group must be the isolated direct child pid")
    _terminate_and_reap(process, argv, process_group_id, term_grace)


def run(
    arguments: Sequence[str],
    *,
    timeout: float,
    input_bytes: Optional[bytes] = None,
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    check: bool = False,
    term_grace: float = DEFAULT_TERM_GRACE_SECONDS,
    capture_output: bool = True,
    max_output_bytes: int | None = None,
    cancel_event: Any | None = None,
) -> ProcessResult:
    """Run an argv without a shell, bound its whole group, and preserve exact bytes.

    With ``capture_output=False`` and no explicit input, all three standard
    streams are inherited.  That mode is intentionally suitable for interactive
    commands such as a browser login: callers cannot accidentally pipe or
    inspect credential material while retaining bounded process cleanup.

    ``max_output_bytes`` limits combined stdout/stderr during capture, including
    cleanup. It requires captured output and no input_bytes.
    """
    argv = _argv(arguments)
    timeout = _positive(timeout, "process timeout")
    term_grace = _positive(term_grace, "process TERM grace")
    if input_bytes is not None and not isinstance(input_bytes, bytes):
        raise TypeError("process stdin must be exact bytes")
    if not isinstance(capture_output, bool):
        raise TypeError("capture_output must be a boolean")
    if max_output_bytes is not None:
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive integer")
        if not capture_output or input_bytes is not None:
            raise ValueError("bounded capture requires capture_output and no input_bytes")
    if cancel_event is not None:
        if max_output_bytes is None or not callable(getattr(cancel_event, "is_set", None)):
            raise ValueError("cancel_event requires bounded capture and an is_set method")
        if cancel_event.is_set():
            raise ProcessCancelledError("process cancellation requested before launch")

    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=(
                subprocess.PIPE
                if input_bytes is not None
                else (subprocess.DEVNULL if capture_output else None)
            ),
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise ProcessLaunchError(f"cannot run {argv[0]}: {exc}", argv) from exc

    group: Optional[int]
    try:
        group = os.getpgid(process.pid)
    except OSError as exc:
        # A child that exited before verification is harmless, but descendants
        # may still hold its pipes. Keep the ordinary communicate deadline below.
        if process.poll() is None:
            process.kill()
            try:
                _final_communicate(process, argv, term_grace)
            except ProcessCleanupError:
                raise
            raise ProcessLaunchError(
                f"cannot verify isolated process group for {argv[0]}: {exc}", argv
            ) from exc
        group = None

    if group is not None and group != process.pid:
        process.kill()
        _final_communicate(process, argv, term_grace)
        raise ProcessLaunchError(
            f"child {process.pid} did not start in a new process group", argv
        )

    if max_output_bytes is not None:
        result = _bounded_capture(
            process,
            argv,
            group,
            timeout,
            term_grace,
            max_output_bytes,
            cancel_event,
        )
        if check and result.returncode:
            raise ProcessCommandError(result)
        return result

    try:
        stdout, stderr = process.communicate(input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_and_reap(
            process, argv, group, term_grace
        )
        raise ProcessTimeoutError(_result(process, argv, stdout, stderr), timeout)
    except BaseException:
        # Cancellation and unexpected communication failures must not orphan the
        # isolated group or leave the direct child as a zombie.
        _terminate_and_reap(process, argv, group, term_grace)
        raise

    result = _result(process, argv, stdout, stderr)
    if check and result.returncode:
        raise ProcessCommandError(result)
    return result


def _bounded_capture(process, argv, group, timeout, term_grace, limit, cancel_event=None):
    """Drain pipes with a fixed memory ceiling and a single whole-command deadline."""
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream in buffers:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                if cancel_event is not None and cancel_event.is_set():
                    raise ProcessCancelledError("process cancellation requested")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                wait = min(remaining, 0.25) if cancel_event is not None else remaining
                for key, _ in selector.select(wait):
                    chunk = os.read(key.fileobj.fileno(), min(8192, limit - total + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise ProcessOutputLimitError("process output limit exceeded", argv)
                    buffers[key.fileobj].extend(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return _result(process, argv, bytes(buffers[process.stdout]), bytes(buffers[process.stderr]))
    except BaseException as exc:
        # Never use communicate here: cleanup must retain the same memory bound.
        _signal_group(process, group, signal.SIGTERM)
        try:
            process.wait(timeout=term_grace)
        except subprocess.TimeoutExpired:
            _signal_group(process, group, signal.SIGKILL)
            try:
                process.wait(timeout=term_grace)
            except subprocess.TimeoutExpired as cleanup:
                raise ProcessCleanupError(argv, process.pid) from cleanup
        if isinstance(exc, subprocess.TimeoutExpired):
            raise ProcessTimeoutError(
                _result(process, argv, bytes(buffers[process.stdout]), bytes(buffers[process.stderr])),
                timeout,
            ) from exc
        raise
    finally:
        _close_capture_streams(process)
