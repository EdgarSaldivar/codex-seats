"""Hardened Git execution for trusted, local repository operations.

This runner is deliberately unsuitable for arbitrary user commands.  It accepts an
exact argv, a small typed set of internal storage overrides, and always returns bytes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import bounded_process


_CONFIG_OVERRIDES = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.pager=cat",
    "pager.branch=false",
    "pager.diff=false",
    "pager.log=false",
    "diff.external=/usr/bin/false",
    "interactive.singleKey=false",
)
_STRIPPED_ENVIRONMENT = frozenset(
    {"PAGER", "LESS", "LV", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}
)


@dataclass(frozen=True)
class GitOverrides:
    """The only Git environment redirects internal callers may request."""

    index_file: Path | None = None
    object_directory: Path | None = None
    alternate_object_directories: tuple[Path, ...] = ()

    def environment(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.index_file is not None:
            values["GIT_INDEX_FILE"] = _absolute(self.index_file, "index file")
        if self.object_directory is not None:
            values["GIT_OBJECT_DIRECTORY"] = _absolute(
                self.object_directory, "object directory"
            )
        if self.alternate_object_directories:
            encoded = [
                _absolute(path, "alternate object directory")
                for path in self.alternate_object_directories
            ]
            if any(os.pathsep in item for item in encoded):
                raise ValueError("alternate object directory contains a path separator")
            values["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(encoded)
        return values


@dataclass(frozen=True)
class GitResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class GitError(RuntimeError):
    """Base class for an internal Git invocation failure."""

    def __init__(self, message: str, argv: Sequence[str]) -> None:
        super().__init__(message)
        self.argv = tuple(argv)


class GitLaunchError(GitError):
    """Git could not be executed."""


class GitTimeoutError(GitError):
    """Git exceeded its caller-selected bound."""


class GitCommandError(GitError):
    """Git executed and returned a non-zero status."""

    def __init__(self, result: GitResult) -> None:
        detail = result.stderr or result.stdout
        rendered = detail.decode("utf-8", "replace")
        if rendered.endswith("\n"):
            rendered = rendered[:-1]
        super().__init__(
            f"{' '.join(result.argv)} failed: {rendered or f'exit {result.returncode}'}",
            result.argv,
        )
        self.result = result


def _absolute(path: Path, label: str) -> str:
    value = os.fspath(path)
    if not os.path.isabs(value) or "\x00" in value:
        raise ValueError(f"Git {label} must be an absolute path")
    return value


def _environment(repo: Path, overrides: GitOverrides | None) -> dict[str, str]:
    # Git treats numerous GIT_* variables as command-line-equivalent redirects.  None
    # are inherited; the small controlled set below is rebuilt from constants and
    # typed paths.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in _STRIPPED_ENVIRONMENT
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS_REQUIRE": "never",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CEILING_DIRECTORIES": os.fspath(repo),
        }
    )
    if overrides is not None:
        environment.update(overrides.environment())
    return environment


def run(
    repo: str | Path,
    arguments: Sequence[str],
    *,
    timeout: float,
    input_bytes: bytes | None = None,
    overrides: GitOverrides | None = None,
    check: bool = False,
) -> GitResult:
    """Execute one bounded, non-interactive Git command and preserve exact bytes."""
    if timeout <= 0:
        raise ValueError("Git timeout must be positive")
    repository = Path(repo).expanduser().absolute()
    if "\x00" in os.fspath(repository):
        raise ValueError("Git repository path contains NUL")
    if isinstance(arguments, (str, bytes)) or not all(
        isinstance(item, str) and "\x00" not in item for item in arguments
    ):
        raise TypeError("Git arguments must be NUL-free strings")
    if input_bytes is not None and not isinstance(input_bytes, bytes):
        raise TypeError("Git stdin must be exact bytes")
    argv = ["git"]
    for setting in _CONFIG_OVERRIDES:
        argv.extend(("-c", setting))
    argv.extend(("-C", os.fspath(repository), *arguments))
    try:
        completed = bounded_process.run(
            argv,
            timeout=timeout,
            input_bytes=input_bytes,
            env=_environment(repository, overrides),
        )
    except bounded_process.ProcessTimeoutError as exc:
        raise GitTimeoutError(
            f"{' '.join(argv)} timed out after {timeout:g}s", argv
        ) from exc
    except bounded_process.ProcessLaunchError as exc:
        raise GitLaunchError(f"cannot run Git: {exc}", argv) from exc
    result = GitResult(
        tuple(argv), completed.returncode, completed.stdout, completed.stderr
    )
    if check and result.returncode:
        raise GitCommandError(result)
    return result


def line(output: bytes, *, label: str) -> str:
    """Decode Git's one-line output, removing exactly its single LF delimiter."""
    if not output.endswith(b"\n") or b"\n" in output[:-1] or b"\x00" in output:
        raise ValueError(f"Git returned malformed {label}")
    return output[:-1].decode("utf-8", "surrogateescape")
