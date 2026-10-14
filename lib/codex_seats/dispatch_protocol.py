"""Typed dispatch envelope + secure filesystem queue for head→worker delegation.

This is the capability boundary of the dual-head design. A head drops a *typed request*
into ``dispatch/inbox``; a ``launchd``-activated runner validates it and calls the
existing router/worker path. This remains useful for full-access heads as a durable,
auditable queue, and it is required for an opt-in ``--safe`` head because macOS refuses
nested seatbelt profiles (exit 71). The request is the only thing crossing into the
runner, so its schema is deliberately **narrower than a GoalSpec**:

- ``operation="task"`` carries a natural-language ``prompt`` that runs ONLY inside the
  existing sandboxed worker. It cannot carry an executable, environment, ``CODEX_HOME``,
  output path, external write scope, git-write flag, production flag, or acceptance
  command. ``run_acceptance`` runs GoalSpec shell OUTSIDE the worker sandbox, so letting
  a remotely-prompted head invent acceptance commands would be unsandboxed RCE through
  the runner. It cannot.
- A task may carry MCP server *names* from the repository's committed worker catalog. It
  still cannot carry a command, environment, URL, or filesystem grant.
- A task may carry one versioned, current-user-authorized request to spend the selected
  Codex seat's local reserve. It does not grant any other authority.
- A task may carry one enumerated, reason-audited effort override. Exact model IDs remain
  forbidden and provider-owned effort ceilings still clamp unsupported levels.
- A task may carry minimal task shape/validation facts and a stable logical task id. A
  task without them is ``execute``/``unspecified`` and therefore not eligible for
  automatic local routing.
- ``operation="cancel"`` carries only the target request id plus ordinary envelope
  identity. Process identity is resolved by the runner from its owner-only claim/lease.

Every frame must carry complete release/protocol metadata for the current protocol
version. A frame without it, from an older protocol, or with any other operation is
refused, which the runner records as a terminal rejected receipt; it is never executed
or requeued.

Unknown keys are rejected, not ignored: a field the runner does not understand is a
smuggling attempt, not forward-compat. Everything here is stdlib-only and free of
import-time side effects (``now`` is injected) so it is trivially unit-testable and
cannot be tricked by a clock.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .efforts import EFFORTS
from .task_policy import (
    DEFAULT_TASK_SHAPE,
    DEFAULT_VALIDATION,
    DELEGATE_KINDS,
    REQUESTED_CAPABILITIES,
    REQUESTED_SIZES,
    RISK_FLAGS,
    TASK_SHAPES,
    VALIDATION_LEVELS,
    resolve_task_policy,
)

SCHEMA_VERSION = 1
# The current wire protocol. This runtime accepts exactly this version; release
# manifests declare the same range so incompatible releases are refused.
PROTOCOL_VERSION = 8
MIN_PROTOCOL_VERSION = PROTOCOL_VERSION
MAX_PROTOCOL_VERSION = PROTOCOL_VERSION
RESERVE_SPEND_AUTHORITY_VERSION = 1
RESERVE_SPEND_AUTHORITY_SOURCE = "current_user_request"

OPERATIONS = frozenset({"task", "cancel"})
PROVIDERS = frozenset({"auto", "claude", "codex", "local"})
KINDS = frozenset(DELEGATE_KINDS)
SIZES = frozenset(REQUESTED_SIZES)
CAPABILITY_TIERS = frozenset(REQUESTED_CAPABILITIES)
HEADS = frozenset({"codex", "claude"})

MAX_PROMPT_BYTES = 16 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_TTL_SECONDS = 24 * 60 * 60
REQUEST_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
SEAT_RE = re.compile(r"\Aseat[1-9][0-9]*\Z")
RELEASE_ID_RE = re.compile(r"\A[0-9a-f]{64}\Z")
IDENTITY_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")

# Fields permitted per operation. Anything outside the union is a hard reject — this
# allow-list is the anti-smuggling boundary, so err strict.
_COMMON_FIELDS = frozenset(
    {"schema", "request_id", "operation", "repo_id", "provider", "kind",
     "size", "capability_tier", "estimated_minutes", "parallel_safe",
     "scope_hints", "risk_flags", "override_reason", "read_only", "origin",
     "created_at", "expires_at", "release_id", "protocol_version",
     "protocol_min", "protocol_max", "goal_id",
     "coordination_id"}
)
_TASK_FIELDS = _COMMON_FIELDS | {
    "prompt",
    "mcp",
    "reserve_spend_authority",
    "effort",
    "task_shape",
    "validation",
    "task_id",
}
_CANCEL_FIELDS = frozenset(
    {
        "schema",
        "release_id",
        "protocol_version",
        "protocol_min",
        "protocol_max",
        "request_id",
        "operation",
        "target_request_id",
        "origin",
        "created_at",
        "expires_at",
    }
)

# Fields a request must NEVER contain — named explicitly so a smuggling attempt gets a
# precise refusal rather than a generic "unknown key", and so the intent is documented.
FORBIDDEN_FIELDS = frozenset(
    {"executable", "command", "cmd", "argv", "env", "environment", "CODEX_HOME",
     "codex_home", "output", "output_path", "cd", "path", "write_scope",
     "external_write_scope", "external_scope", "allow_git_write", "git_write",
     "production", "approve_prod", "acceptance", "review", "seat", "model",
     "side_effect_class", "language", "domain",
     "allow_reserve", "protected_paths", "sandbox",
     "sandbox_permissions", "hooks", "pid",
     "signal", "process_group_id", "session_id"}
)


class DispatchError(ValueError):
    """A request that must be refused — malformed, out of policy, or a smuggling attempt."""


def queue_paths(root: str | Path) -> dict[str, Path]:
    base = Path(root).expanduser() / "dispatch"
    return {
        "base": base,
        "inbox": base / "inbox",
        "processing": base / "processing",
        "receipts": base / "receipts",
        "logs": base / "logs",
        "lock": base / "runner.lock",
    }


def ensure_queue(root: str | Path) -> dict[str, Path]:
    """Create the owner-only queue tree (0700). Safe to call repeatedly."""
    paths = queue_paths(root)
    for key in ("base", "inbox", "processing", "receipts", "logs"):
        paths[key].mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir honors umask; force the mode so a permissive umask can't widen it.
        os.chmod(paths[key], 0o700)
    return paths


def new_request_id() -> str:
    return os.urandom(16).hex()


def canonical_bytes(request: dict[str, Any]) -> bytes:
    """Stable serialization for hashing/idempotency — key order independent."""
    return json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(request: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(request)).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DispatchError(message)


def _validate_scope_hint(value: Any) -> str:
    _require(
        isinstance(value, str) and bool(value),
        "scope_hints entries must be strings",
    )
    _require("\x00" not in value, "scope_hints entries cannot contain NUL")
    _require(
        "\\" not in value and not any(char in value for char in "*?[]{}"),
        "scope_hints entries must be literal POSIX paths without glob syntax",
    )
    path = Path(value)
    _require(not path.is_absolute(), "scope_hints entries must be repo-relative")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    _require(parts and ".." not in parts, "scope_hints entries cannot escape the repo")
    return "/".join(parts)


def validate_request(
    request: Any,
    settings: dict[str, Any],
    *,
    now: float,
    supported_protocol_min: int = MIN_PROTOCOL_VERSION,
    supported_protocol_max: int = MAX_PROTOCOL_VERSION,
) -> tuple[Path | None, dict[str, Any]]:
    """Validate a request against policy and the project registry.

    Returns ``(repo_path, normalized_request)`` (``repo_path`` is ``None`` for control
    frames). Raises :class:`DispatchError` on any violation. ``now`` is epoch seconds,
    injected so expiry is testable and clock-safe. A returned repo path is resolved ONLY
    through ``settings['projects']`` — the request can never supply a filesystem root.
    """
    _require(isinstance(request, dict), "request must be a JSON object")
    normalized = dict(request)

    # Reject named smuggling fields with a precise message before the generic sweep.
    smuggled = sorted(set(request) & FORBIDDEN_FIELDS)
    _require(not smuggled, f"forbidden field(s): {', '.join(smuggled)}")

    _require(request.get("schema") == SCHEMA_VERSION, f"schema must be {SCHEMA_VERSION}")

    operation = request.get("operation")
    _require(
        operation in OPERATIONS,
        f"operation must be one of {sorted(OPERATIONS)}",
    )
    allowed = _TASK_FIELDS if operation == "task" else _CANCEL_FIELDS
    extra = sorted(set(request) - allowed)
    _require(not extra, f"unknown field(s): {', '.join(extra)}")

    metadata_fields = {
        "release_id",
        "protocol_version",
        "protocol_min",
        "protocol_max",
    }
    _require(
        metadata_fields <= set(request),
        "release/protocol compatibility metadata is required: "
        + ", ".join(sorted(metadata_fields)),
    )
    release_id = request.get("release_id")
    _require(
        isinstance(release_id, str) and bool(RELEASE_ID_RE.fullmatch(release_id)),
        "release_id must be exactly 64 lowercase hexadecimal characters",
    )
    protocol_version = request.get("protocol_version")
    protocol_min = request.get("protocol_min")
    protocol_max = request.get("protocol_max")
    _require(
        all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in (protocol_version, protocol_min, protocol_max)
        ),
        "protocol compatibility values must be positive integers",
    )
    _require(
        protocol_min <= protocol_version <= protocol_max,
        "protocol_version must be inside the declared compatibility range",
    )
    _require(
        supported_protocol_min <= protocol_version <= supported_protocol_max,
        (
            f"protocol {protocol_version} is unsupported; this runtime accepts "
            f"{supported_protocol_min}..{supported_protocol_max}"
        ),
    )

    rid = request.get("request_id")
    _require(isinstance(rid, str) and bool(REQUEST_ID_RE.match(rid)),
             "request_id must be 32 lowercase hex chars")

    origin = request.get("origin")
    _require(isinstance(origin, dict), "origin must be an object")
    _require(origin.get("head") in HEADS, f"origin.head must be one of {sorted(HEADS)}")
    seat = origin.get("seat")
    _require(seat is None or (isinstance(seat, str) and bool(SEAT_RE.match(seat))),
             "origin.seat must be null or seatN")
    _require(set(origin) <= {"head", "seat"}, "origin has unexpected keys")

    created = request.get("created_at")
    expires = request.get("expires_at")
    _require(isinstance(created, (int, float)) and isinstance(expires, (int, float)),
             "created_at/expires_at must be epoch numbers")
    _require(expires > created, "expires_at must be after created_at")
    _require(expires - created <= MAX_TTL_SECONDS,
             f"ttl exceeds {MAX_TTL_SECONDS}s cap")
    _require(now < expires, "request has expired")

    if operation == "cancel":
        target = request.get("target_request_id")
        _require(
            isinstance(target, str) and bool(REQUEST_ID_RE.match(target)),
            "target_request_id must be 32 lowercase hex chars",
        )
        _require(target != rid, "a cancellation request cannot cancel itself")
        _require(len(canonical_bytes(request)) <= MAX_FRAME_BYTES,
                 f"request frame exceeds {MAX_FRAME_BYTES} bytes")
        return None, normalized

    repo_id = request.get("repo_id")
    projects = settings.get("projects") or {}
    _require(isinstance(repo_id, str) and repo_id in projects,
             "repo_id must name a registered project")
    entry = projects[repo_id]
    _require(isinstance(entry, dict) and isinstance(entry.get("path"), str),
             f"project {repo_id!r} has no registered path")
    repo_path = Path(entry["path"]).expanduser()

    provider = request.get("provider", "auto")
    _require(provider in PROVIDERS, f"provider must be one of {sorted(PROVIDERS)}")
    reserve_authority = request.get("reserve_spend_authority")
    if "reserve_spend_authority" in request:
        _require(
            isinstance(reserve_authority, dict),
            "reserve_spend_authority must be an object",
        )
        _require(
            set(reserve_authority) == {"version", "seat", "source"},
            "reserve_spend_authority must contain exactly version, seat, and source",
        )
        _require(
            type(reserve_authority.get("version")) is int
            and reserve_authority["version"] == RESERVE_SPEND_AUTHORITY_VERSION,
            f"reserve_spend_authority.version must be {RESERVE_SPEND_AUTHORITY_VERSION}",
        )
        reserve_seat = reserve_authority.get("seat")
        _require(
            isinstance(reserve_seat, str) and bool(SEAT_RE.fullmatch(reserve_seat)),
            "reserve_spend_authority.seat must be seatN",
        )
        _require(
            reserve_authority.get("source") == RESERVE_SPEND_AUTHORITY_SOURCE,
            "reserve_spend_authority.source must be current_user_request",
        )
        _require(
            provider == "codex",
            "reserve spending requires the explicit codex provider",
        )
        _require(
            not (
                origin.get("head") == "codex"
                and origin.get("seat") == reserve_seat
            ),
            "reserve spending cannot target the active Codex head seat",
        )
        normalized["reserve_spend_authority"] = dict(reserve_authority)
    raw_mcp = request.get("mcp", [])
    _require(
        isinstance(raw_mcp, list) and len(raw_mcp) <= 32,
        "mcp must be a list with at most 32 catalog names",
    )
    _require(
        all(
            isinstance(name, str)
            and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name))
            for name in raw_mcp
        ),
        "mcp entries must be 1-128 character catalog names",
    )
    _require(len(raw_mcp) == len(set(raw_mcp)), "mcp entries must not contain duplicates")
    if raw_mcp:
        normalized["mcp"] = list(raw_mcp)
    kind = request.get("kind", "implementation")
    _require(kind in KINDS, f"kind must be one of {sorted(KINDS)}")
    task_shape = request.get("task_shape", DEFAULT_TASK_SHAPE)
    validation = request.get("validation", DEFAULT_VALIDATION)
    _require(
        isinstance(task_shape, str) and task_shape in TASK_SHAPES,
        f"task_shape must be one of {sorted(TASK_SHAPES)}",
    )
    if "validation" in request:
        _require(
            isinstance(validation, str) and validation in VALIDATION_LEVELS,
            f"validation must be one of {sorted(VALIDATION_LEVELS)}",
        )
    normalized["task_shape"] = task_shape
    normalized["validation"] = validation
    size = request.get("size", "auto")
    _require(size in SIZES, f"size must be one of {sorted(SIZES)}")
    capability = request.get("capability_tier", "auto")
    _require(
        capability in CAPABILITY_TIERS,
        f"capability_tier must be one of {sorted(CAPABILITY_TIERS)}",
    )
    effort = request.get("effort")
    _require(
        effort is None or (isinstance(effort, str) and effort in EFFORTS),
        f"effort must be one of {sorted(EFFORTS)}",
    )
    estimated = request.get("estimated_minutes")
    _require(
        estimated is None
        or (
            isinstance(estimated, (int, float))
            and not isinstance(estimated, bool)
            and 0 < float(estimated) <= 480
        ),
        "estimated_minutes must be a number from 0 to 480",
    )
    parallel_safe = request.get("parallel_safe", False)
    _require(isinstance(parallel_safe, bool), "parallel_safe must be a boolean")
    raw_scopes = request.get("scope_hints", [])
    _require(
        isinstance(raw_scopes, list) and len(raw_scopes) <= 64,
        "scope_hints must be a list with at most 64 entries",
    )
    normalized_scopes = tuple(_validate_scope_hint(value) for value in raw_scopes)
    _require(
        len(normalized_scopes) == len(set(normalized_scopes)),
        "scope_hints must not contain duplicates",
    )
    if parallel_safe:
        _require(bool(normalized_scopes), "parallel_safe requires scope_hints")
    raw_risks = request.get("risk_flags", [])
    _require(
        isinstance(raw_risks, list) and len(raw_risks) <= len(RISK_FLAGS),
        "risk_flags must be a list",
    )
    _require(
        all(isinstance(value, str) and value in RISK_FLAGS for value in raw_risks),
        f"risk_flags entries must be one of {sorted(RISK_FLAGS)}",
    )
    reason = request.get("override_reason")
    _require(
        reason is None
        or (
            isinstance(reason, str)
            and bool(reason.strip())
            and len(reason.encode("utf-8")) <= 1024
        ),
        "override_reason must be a non-empty string up to 1024 bytes",
    )
    try:
        resolve_task_policy(
            settings,
            kind,
            size=size,
            capability=capability,
            task_shape=task_shape,
            validation=validation,
            effort=effort,
            estimated_minutes=float(estimated) if estimated is not None else None,
            parallel_safe=parallel_safe,
            scope_hints=normalized_scopes,
            risk_flags=raw_risks,
            override_reason=reason,
            require_override_reason=True,
        )
    except ValueError as exc:
        raise DispatchError(str(exc)) from exc

    for identity_field in ("goal_id", "coordination_id", "task_id"):
        if identity_field not in request:
            continue
        identity = request[identity_field]
        _require(
            isinstance(identity, str) and bool(IDENTITY_RE.match(identity)),
            f"{identity_field} must be a short identity token",
        )

    if "read_only" in request:
        _require(isinstance(request["read_only"], bool), "read_only must be a boolean")

    prompt = request.get("prompt")
    _require(isinstance(prompt, str) and prompt.strip() != "", "task needs a prompt")
    _require(len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES,
             f"prompt exceeds {MAX_PROMPT_BYTES} bytes")

    _require(len(canonical_bytes(request)) <= MAX_FRAME_BYTES,
             f"request frame exceeds {MAX_FRAME_BYTES} bytes")
    return repo_path, normalized


def write_request(root: str | Path, request: dict[str, Any]) -> Path:
    """Atomically publish a request into the inbox (client side).

    temp-file + fsync(file) + fsync(dir) + rename to ``<request_id>.ready`` so the
    runner never observes a partial frame. The client is trusted to have validated;
    the runner re-validates regardless.
    """
    paths = queue_paths(root)
    # An opt-in sandboxed head can write into the inbox but must not mkdir/chmod the
    # read-only pool dir. Only build the tree when it is actually absent (first run or the
    # launchd runner), so the normal head path touches nothing but the inbox.
    if not paths["inbox"].is_dir():
        ensure_queue(root)
    rid = request["request_id"]
    frame = canonical_bytes(request)
    tmp = paths["inbox"] / f".{rid}.tmp"
    final = paths["inbox"] / f"{rid}.ready"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, frame)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        # Publishing must not replace an already-ready request id.
        os.link(tmp, final, follow_symlinks=False)
    finally:
        tmp.unlink(missing_ok=True)
    dir_fd = os.open(paths["inbox"], os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final


def read_request_secure(
    path: str | Path,
    *,
    uid: int | None = None,
    max_bytes: int = MAX_FRAME_BYTES,
) -> dict[str, Any]:
    """Read a queued frame with anti-tamper checks (runner side).

    Enforces, before trusting a byte: O_NOFOLLOW (no symlink swap), regular file,
    owner == ``uid`` (default current euid), no group/other write bit, and a size cap.
    Parsing failure or any check failure raises :class:`DispatchError`.
    """
    if uid is None:
        uid = os.geteuid()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        # O_NOFOLLOW makes a symlink swap ELOOP here — treat as a refused frame.
        raise DispatchError(f"cannot open queued frame: {exc}") from exc
    try:
        return _read_request_descriptor(fd, uid=uid, max_bytes=max_bytes)
    finally:
        os.close(fd)


def _read_request_descriptor(fd: int, *, uid: int, max_bytes: int) -> dict[str, Any]:
    """Validate and decode one already-open, no-follow queue descriptor."""
    info = os.fstat(fd)
    _require(stat.S_ISREG(info.st_mode), "queued path is not a regular file")
    _require(info.st_uid == uid, "queued file has wrong owner")
    _require(not (info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)),
             "queued file is group/other-writable")
    _require(info.st_size <= max_bytes, "queued file exceeds frame cap")
    data = os.read(fd, max_bytes + 1)
    _require(len(data) <= max_bytes, "queued file exceeds frame cap")
    try:
        parsed = json.loads(data)
    except (ValueError, json.JSONDecodeError) as exc:
        raise DispatchError(f"queued file is not valid JSON: {exc}") from exc
    _require(isinstance(parsed, dict), "queued frame must be a JSON object")
    return parsed


def count_ready_frames(root: str | Path) -> int:
    """Count owner-safe, internally identified frames in the durable inbox.

    Status is observational: malformed names, symlinks, and unreadable/tampered files
    are ignored here and remain for the runner to reject through its normal path.
    """
    inbox = queue_paths(root)["inbox"]
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
    )
    try:
        directory_fd = os.open(inbox, directory_flags)
    except OSError:
        return 0
    try:
        info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return 0
        try:
            names = os.listdir(directory_fd)
        except OSError:
            return 0
        count = 0
        for name in names:
            if not name.endswith(".ready"):
                continue
            request_id = name.removesuffix(".ready")
            if not REQUEST_ID_RE.fullmatch(request_id):
                continue
            try:
                frame_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except OSError:
                continue
            try:
                frame = _read_request_descriptor(
                    frame_fd, uid=os.geteuid(), max_bytes=MAX_FRAME_BYTES
                )
            except DispatchError:
                continue
            finally:
                os.close(frame_fd)
            if frame.get("request_id") == request_id:
                count += 1
        return count
    finally:
        os.close(directory_fd)


def claim(root: str | Path, ready_path: str | Path) -> Path:
    """Atomically move a ready frame inbox→processing (runner, under the runner flock).

    ``os.rename`` is atomic on the same filesystem, so two activated runners racing the
    same file: exactly one rename wins; the loser gets ``FileNotFoundError`` which the
    caller treats as "already claimed". Returns the processing path.
    """
    paths = queue_paths(root)
    ready = Path(ready_path)
    dest = paths["processing"] / ready.name.replace(".ready", ".claimed")
    if not ready.exists():
        raise FileNotFoundError(ready)
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"request is already claimed: {dest.name}")
    os.rename(ready, dest)  # raises FileNotFoundError to the losing racer
    return dest


def receipt_path(root: str | Path, request_id: str) -> Path:
    return queue_paths(root)["receipts"] / f"{request_id}.json"


def publish_receipt(
    root: str | Path, receipt: dict[str, Any]
) -> tuple[Path, bool]:
    """Publish a terminal receipt exactly once; return ``(path, won_race)``."""
    _require(isinstance(receipt.get("request_id"), str), "receipt needs request_id")
    ensure_queue(root)
    final = receipt_path(root, receipt["request_id"])
    frame = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tmp = final.with_name(
        f".{final.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, frame)
            os.fsync(fd)
        finally:
            os.close(fd)
        won = False
        try:
            os.link(tmp, final, follow_symlinks=False)
            won = True
        except FileExistsError:
            pass
    finally:
        tmp.unlink(missing_ok=True)
    dir_fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final, won


def write_receipt(root: str | Path, receipt: dict[str, Any]) -> Path:
    """Idempotently persist a write-once terminal receipt keyed by request id."""
    path, _won = publish_receipt(root, receipt)
    return path


def read_receipt(root: str | Path, request_id: str) -> dict[str, Any] | None:
    path = receipt_path(root, request_id)
    if not path.exists():
        return None
    try:
        return read_request_secure(path)
    except DispatchError:
        return None


def idempotency_decision(
    root: str | Path,
    request: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Decide how a runner should treat an incoming request id it may have seen.

    Returns one of:
    - ``("fresh", None)``           — no prior receipt; process normally.
    - ``("replay", receipt)``       — a terminal receipt exists for the SAME content; replay it.
    - ``("conflict", receipt)``     — same request_id, DIFFERENT content; refuse (id reuse).

    The stored receipt records the content hash it was produced for, so a client that
    reuses an id with a mutated body cannot overwrite or impersonate the first job.
    """
    prior = read_receipt(root, request["request_id"])
    if prior is None:
        return "fresh", None
    if prior.get("request_hash") == content_hash(request):
        return "replay", prior
    return "conflict", prior
