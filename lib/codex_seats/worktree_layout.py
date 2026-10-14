"""Protected placement for managed job worktrees."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

JOB_WORKTREES_NAME = "job-worktrees"


class WorktreeLayoutError(RuntimeError):
    """A managed worktree placement is unsafe."""


@dataclass(frozen=True)
class ManagedPlacement:
    """A newly allocated, empty checkout location owned by one controller job."""

    checkout: Path
    job_parent: Path

    def discard_empty(self) -> None:
        """Remove only this allocation when checkout creation did not use it."""
        try:
            self.job_parent.rmdir()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorktreeLayoutError(
                f"cannot remove unused managed worktree parent {self.job_parent}: {exc}"
            ) from exc


def _absolute_resolved(path: str | Path) -> Path:
    expanded = Path(path).expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    return absolute.resolve(strict=False)


def _owner_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except OSError as exc:
        raise WorktreeLayoutError(f"{label} is unavailable: {path}: {exc}") from exc
    if (
        resolved != path.absolute()
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise WorktreeLayoutError(
            f"{label} must be a canonical owner-controlled directory: {path}"
        )
    return resolved


def _ensure_protected_parent(pool_root: str | Path) -> Path:
    root = _owner_directory(_absolute_resolved(pool_root), label="pool root")
    parent = root / JOB_WORKTREES_NAME
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise WorktreeLayoutError(
            f"cannot create protected managed-worktree parent {parent}: {exc}"
        ) from exc
    return _owner_directory(parent, label="managed-worktree parent")


def allocate_managed_placement(pool_root: str | Path) -> ManagedPlacement:
    """Allocate an owner-only checkout parent below the protected pool root."""
    parent = _ensure_protected_parent(pool_root)
    raw_job_parent: str | None = None
    try:
        raw_job_parent = tempfile.mkdtemp(prefix="job-", dir=parent)
        job_parent = _owner_directory(
            Path(raw_job_parent), label="managed job-worktree parent"
        )
    except (OSError, WorktreeLayoutError) as exc:
        cleanup_detail = ""
        if raw_job_parent is not None:
            try:
                Path(raw_job_parent).rmdir()
            except OSError as cleanup:
                cleanup_detail = f"; allocation rollback failed: {cleanup}"
        raise WorktreeLayoutError(
            f"cannot allocate protected managed worktree below {parent}: "
            f"{exc}{cleanup_detail}"
        ) from exc
    return ManagedPlacement(checkout=job_parent / "checkout", job_parent=job_parent)


def managed_job_parent(path: str | Path) -> Path | None:
    """Recognize the parent of a checkout made by :func:`allocate_managed_placement`."""
    checkout = _absolute_resolved(path)
    candidate = checkout.parent
    if (
        checkout.name != "checkout"
        or candidate.parent.name != JOB_WORKTREES_NAME
        or not candidate.name.startswith("job-")
    ):
        return None
    try:
        return _owner_directory(candidate, label="managed job-worktree parent")
    except WorktreeLayoutError:
        return None
