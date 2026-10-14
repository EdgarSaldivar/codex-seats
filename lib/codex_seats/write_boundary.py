from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import internal_git
from .scope import effective_repository_write_scope
from .worker_launch import WORKER_LAUNCH_DIRNAME


@dataclass(frozen=True)
class WriteBoundary:
    mode: str
    repository: Path
    declared_scope: tuple[Any, ...]
    effective_scope: tuple[Any, ...]
    external_scope: tuple[Any, ...]
    strict_scope: bool
    parallel_safe: bool
    allow_git_write: bool
    git_metadata: tuple[Path, ...]

    @property
    def balanced(self) -> bool:
        return self.mode == "balanced"


def boundary_plan(
    repository: str | Path,
    write_scope: Iterable[Any],
    external_scope: Iterable[Any],
    *,
    strict_scope: bool = False,
    parallel_safe: bool = False,
    allow_git_write: bool = False,
) -> WriteBoundary:
    repo = Path(repository).expanduser().resolve()
    metadata = tuple(git_metadata_paths(repo))
    declared = tuple(write_scope)
    external = tuple(external_scope)
    effective = tuple(
        effective_repository_write_scope(
            declared,
            strict_scope=bool(strict_scope),
            parallel_safe=bool(parallel_safe),
        )
    )
    mode = (
        "read-only"
        if not declared and not external
        else ("exact" if strict_scope or parallel_safe or not declared else "balanced")
    )
    return WriteBoundary(
        mode=mode,
        repository=repo,
        declared_scope=declared,
        effective_scope=effective,
        external_scope=external,
        strict_scope=bool(strict_scope),
        parallel_safe=bool(parallel_safe),
        # Managed workers never receive Git-metadata write authority.  Keep the
        # request field accepted at this boundary so old envelopes fail closed
        # instead of changing their shape or accidentally selecting a fallback.
        allow_git_write=False,
        git_metadata=metadata,
    )


class GitMetadataError(RuntimeError):
    """Git metadata could not be resolved completely enough to launch a worker."""


def _metadata_path(value: bytes, *, label: str) -> Path:
    try:
        rendered = internal_git.line(value, label=label)
    except ValueError as exc:
        raise GitMetadataError(str(exc)) from exc
    path = Path(rendered).expanduser()
    if not path.is_absolute():
        raise GitMetadataError(f"{label} is not absolute")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise GitMetadataError(f"cannot resolve {label}: {exc}") from exc


def _gitlink_paths(output: bytes) -> list[str]:
    paths: list[str] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise GitMetadataError("Git index returned malformed stage metadata")
        if fields[0] == b"160000":
            paths.append(os.fsdecode(raw_path))
    return paths


def _discover_git_metadata(
    repository: Path,
    paths: set[Path],
    visited: set[Path],
    *,
    expected_git: bool,
) -> None:
    if repository in visited:
        raise GitMetadataError("recursive submodule metadata cycle")
    visited.add(repository)
    try:
        git_dir_result = internal_git.run(
            repository,
            ("rev-parse", "--absolute-git-dir"),
            timeout=3.0,
        )
    except internal_git.GitError as exc:
        raise GitMetadataError(f"cannot discover Git admin directory: {exc}") from exc
    if git_dir_result.returncode:
        if expected_git:
            raise GitMetadataError(
                f"cannot discover submodule Git admin directory: exit {git_dir_result.returncode}"
            )
        return
    git_dir = _metadata_path(git_dir_result.stdout, label="Git admin directory")
    paths.add(git_dir)

    try:
        common_result = internal_git.run(
            repository,
            ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            timeout=3.0,
        )
        index_result = internal_git.run(
            repository,
            ("ls-files", "--stage", "-z"),
            timeout=3.0,
        )
    except internal_git.GitError as exc:
        raise GitMetadataError(f"cannot complete Git metadata discovery: {exc}") from exc
    if common_result.returncode or index_result.returncode:
        raise GitMetadataError("cannot complete Git metadata discovery")
    paths.add(_metadata_path(common_result.stdout, label="Git common directory"))

    for relative in _gitlink_paths(index_result.stdout):
        submodule = (repository / relative).resolve(strict=False)
        try:
            submodule.relative_to(repository)
        except ValueError as exc:
            raise GitMetadataError("submodule path escapes repository") from exc
        submodule_dot_git = submodule / ".git"
        paths.add(submodule_dot_git)
        if submodule_dot_git.exists() or submodule_dot_git.is_symlink():
            _discover_git_metadata(
                submodule, paths, visited, expected_git=True
            )


def git_metadata_paths(repository: str | Path) -> list[Path]:
    repo = Path(repository).expanduser().resolve()
    paths = {repo / ".git", repo / ".git-meta"}
    dot_git = repo / ".git"
    _discover_git_metadata(
        repo,
        paths,
        set(),
        expected_git=dot_git.exists() or dot_git.is_symlink(),
    )
    return sorted(paths, key=str)


def protected_path_variants(path: str | Path) -> tuple[Path, ...]:
    """Lexical absolute alias plus canonical target for one protected path.

    Wildcard patterns stay lexical only: resolving them would treat the pattern
    as a literal file name instead of the glob it denies.
    """
    lexical = Path(os.path.abspath(os.path.expanduser(str(path))))
    if any(marker in str(lexical) for marker in "*?["):
        return (lexical,)
    return tuple(dict.fromkeys((lexical, lexical.resolve(strict=False))))


# Known credential-store containers and provider auth files below HOME. The
# directories deny whole stores; the auth files deny provider credentials
# without hiding the surrounding ~/.codex or ~/.claude configuration.
CREDENTIAL_STORE_HOME_DIRS = (
    str(Path(".codex-seats") / "approvals"),
    ".ssh",
    ".aws",
    ".azure",
    str(Path(".config") / "gcloud"),
    str(Path("Library") / "Keychains"),
)
CREDENTIAL_STORE_HOME_FILES = (
    str(Path(".codex") / "auth.json"),
    str(Path(".claude") / ".credentials.json"),
    str(Path(".claude") / "credentials.json"),
)
CREDENTIAL_STORE_HOME_PATTERNS = (
    str(Path(".codex-seats") / "seat*" / "auth.json"),
)


def known_credential_store_paths(home: str | Path | None = None) -> list[Path]:
    """Every known credential store below HOME, as lexical plus canonical paths.

    A store symlinked into an otherwise-writable root must deny the canonical
    target (actual reads and writes) as well as the lexical alias (unlink and
    rename). Wildcard patterns are never resolved as literal files.
    """
    user_home = Path(home or Path.home()).expanduser().resolve()
    paths: dict[Path, None] = {}
    for relative in (*CREDENTIAL_STORE_HOME_DIRS, *CREDENTIAL_STORE_HOME_FILES):
        for variant in protected_path_variants(user_home / relative):
            paths.setdefault(variant)
    for pattern in CREDENTIAL_STORE_HOME_PATTERNS:
        paths.setdefault(user_home / pattern)
    return list(paths)


def launch_agent_control_paths() -> list[Path]:
    """Control paths outside the control root; the root itself is pool runtime."""
    return [
        # Dispatch runner LaunchAgent — workers must never touch the un-nesting bridge.
        Path.home() / "Library" / "LaunchAgents" / "io.codex-seats.dispatch.plist",
    ]


POOL_RUNTIME_NAMES = (
    # Fail closed for lock files and atomic-write/link temporaries added by future
    # control-plane features; the assigned private-tmp child is re-granted explicitly.
    ".*",
    "approvals",
    "archive",
    "claims",
    "dispatch",
    "handoffs",
    "goals",
    "logs",
    "private-tmp",
    WORKER_LAUNCH_DIRNAME,
    "readers",
    "receipts",
    "releases",
    "reservations",
    "runs",
    "scratch",
    "shipping",
    "seats.json",
    "settings.json",
    "settings.json.backup-*",
    "seat-admin-audit.jsonl",
    "latest-handoff.json",
    "claude-usage.json",
    "claude-statusline.py",
    "codex-seat",
    "current",
    "previous",
    "upgrade-transaction.json",
    "upgrade.lock",
    "worker-hook",
    ".ledger.lock",
    ".venv",
    ".claude-head-settings.json",
    ".current.*",
    ".previous.*",
    ".settings.json.*",
    ".settings.json.rollback",
    ".settings.lock",
    ".settings-readers.lock",
    ".watch-refresh.lock",
    ".scheduler-reservations.lock",
    ".campaign-launch.lock",
)


def configured_seat_home_paths(
    control_root: str | Path | None = None,
    settings: Mapping[str, Any] | None = None,
) -> list[Path]:
    """Return default and configured homes without reading the settings file."""
    root = Path(control_root or (Path.home() / ".codex-seats")).expanduser().resolve()
    homes = {root / "seat*"}
    seats = (
        settings.get("pool", {}).get("seats", {})
        if isinstance(settings, Mapping)
        else {}
    )
    if isinstance(seats, Mapping):
        for seat_id, entry in seats.items():
            if not isinstance(seat_id, str) or not isinstance(entry, Mapping):
                continue
            configured = entry.get("home")
            if isinstance(configured, str) and configured and Path(configured).is_absolute():
                # Keep the configured lexical alias alongside the canonical
                # target so unlink/rename of the alias stays denied too.
                homes.update(protected_path_variants(configured))
            else:
                homes.add(root / seat_id)
    return sorted(homes, key=str)


def pool_runtime_paths(
    control_root: str | Path | None = None,
    settings: Mapping[str, Any] | None = None,
) -> list[Path]:
    root = Path(control_root or (Path.home() / ".codex-seats")).expanduser().resolve()
    paths = [root / name for name in POOL_RUNTIME_NAMES]
    paths.extend(configured_seat_home_paths(root, settings))
    return list(dict.fromkeys(paths))
