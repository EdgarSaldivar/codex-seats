"""Pure admission policy for bounded parallel dispatch.

This module deliberately knows nothing about processes, providers, or the filesystem.
The runner supplies already-validated frames and registered repository paths; the policy
returns a deterministic yes/no decision.  Keeping this boundary pure makes the
concurrency rules reviewable without creating a second provider-claim system.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

MAX_ACTIVE_JOBS = 3
SERIAL_MINUTES = 8.0
_IDENTITY_FIELDS = ("goal_id", "coordination_id")


class ScopeError(ValueError):
    """A scope hint cannot be proved to name a repository-relative component path."""


def normalize_scope(scope: str) -> str:
    """Return a canonical repository-relative path suitable for prefix comparison.

    Scope hints are paths, not shell globs.  Rejecting glob syntax and parent traversal
    is important: an uncertain scope must never be used as proof that two writers are
    disjoint.  Component tuples (rather than string prefixes) make ``src/app`` disjoint
    from ``src/apple`` while preserving ancestor/descendant overlap.
    """
    if not isinstance(scope, str):
        raise ScopeError("scope hint must be a string")
    raw = scope.strip().replace("\\", "/")
    if not raw:
        raise ScopeError("scope hint must not be empty")
    if raw.startswith("/") or "\x00" in raw:
        raise ScopeError("scope hint must be a relative path")
    if any(char in raw for char in "*?[]{}"):
        raise ScopeError("scope hint must not contain glob syntax")
    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ScopeError("scope hint must not traverse a parent")
        parts.append(part)
    # "." intentionally denotes the whole repository and overlaps every other scope.
    return "." if not parts else PurePosixPath(*parts).as_posix()


def normalize_scopes(scopes: Any) -> tuple[str, ...]:
    """Normalize, deduplicate, and sort a scope-hint sequence."""
    if scopes is None:
        return ()
    if not isinstance(scopes, (list, tuple)):
        raise ScopeError("scope_hints must be an array")
    return tuple(sorted({normalize_scope(scope) for scope in scopes}))


def scopes_overlap(left: str, right: str) -> bool:
    """Whether two normalized scopes overlap by component ancestry."""
    if left == "." or right == ".":
        return True
    a = PurePosixPath(left).parts
    b = PurePosixPath(right).parts
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


def scopes_provably_disjoint(left: Iterable[str], right: Iterable[str]) -> bool:
    """True only when both nonempty scope sets have no overlapping pair."""
    a = tuple(left)
    b = tuple(right)
    return bool(a and b) and all(not scopes_overlap(x, y) for x in a for y in b)


@dataclass(frozen=True)
class AdmissionFrame:
    request_id: str
    repo: str
    scopes: tuple[str, ...] = ()
    parallel_safe: bool = False
    serial: bool = True
    identities: frozenset[tuple[str, str]] = frozenset()
    origin_head: str | None = None
    origin_seat: str | None = None

    @classmethod
    def from_frame(cls, frame: dict[str, Any], repo: str) -> "AdmissionFrame":
        estimate = frame.get("estimated_minutes")
        serial = (
            not isinstance(estimate, (int, float))
            or isinstance(estimate, bool)
            or float(estimate) < SERIAL_MINUTES
        )
        identities = frozenset(
            (field, frame[field])
            for field in _IDENTITY_FIELDS
            if isinstance(frame.get(field), str) and frame[field]
        )
        # Every validated request is a known independent identity. Shared
        # goal identities remain additional intersection keys that
        # force one writer across related requests.
        identities = identities | {("request_id", frame["request_id"])}
        origin = frame.get("origin") if isinstance(frame.get("origin"), dict) else {}
        return cls(
            request_id=frame["request_id"],
            repo=repo,
            scopes=normalize_scopes(frame.get("scope_hints")),
            parallel_safe=frame.get("parallel_safe") is True,
            serial=serial,
            identities=identities,
            origin_head=origin.get("head") if isinstance(origin.get("head"), str) else None,
            origin_seat=origin.get("seat") if isinstance(origin.get("seat"), str) else None,
        )


def can_admit(
    candidate: AdmissionFrame,
    active: Iterable[AdmissionFrame],
    *,
    max_active: int = MAX_ACTIVE_JOBS,
) -> bool:
    """Apply the global, short-job, identity, and same-repository admission matrix."""
    running = tuple(active)
    if len(running) >= max_active:
        return False
    if candidate.serial:
        return not running
    if any(job.serial for job in running):
        return False

    for job in running:
        # A goal/coordination writer stays singular even if its work spans repos.
        if candidate.identities & job.identities:
            return False
        if candidate.repo != job.repo:
            continue
        # Unknown coordination identity fails closed for writers in the same repository.
        if not candidate.identities or not job.identities:
            return False
        if not (candidate.parallel_safe and job.parallel_safe):
            return False
        if not scopes_provably_disjoint(candidate.scopes, job.scopes):
            return False
    return True


def select_admissible(
    candidates: Iterable[AdmissionFrame],
    active: Iterable[AdmissionFrame],
    *,
    max_active: int = MAX_ACTIVE_JOBS,
    candidate_max_active: Mapping[str, int] | None = None,
) -> tuple[AdmissionFrame, ...]:
    """Select in caller-provided deterministic order, treating serial work as a barrier."""
    selected: list[AdmissionFrame] = []
    running = list(active)
    limits = candidate_max_active or {}
    for candidate in candidates:
        if len(running) >= max_active:
            break
        candidate_limit = min(
            max_active,
            max(0, int(limits.get(candidate.request_id, max_active))),
        )
        if candidate.serial:
            if can_admit(candidate, running, max_active=candidate_limit):
                selected.append(candidate)
            # Do not let younger work continually jump a waiting serial frame.
            break
        if can_admit(candidate, running, max_active=candidate_limit):
            selected.append(candidate)
            running.append(candidate)
    return tuple(selected)
