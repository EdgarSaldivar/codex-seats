"""The dispatch runner: the durable head-to-worker bridge in the dual-head design.

A head drops a typed request in ``dispatch/inbox``; ``launchd`` activates this process when
the queue is non-empty. Full-access heads use it for durable queueing, isolated workers, and
receipts. An opt-in ``--safe`` Codex head also needs it because a seatbelted process cannot
``sandbox-exec`` another profile (exit 71). The runner validates each frame against policy +
the trusted project registry, runs the existing sandboxed worker path in a retained
worktree, writes a receipt, and exits when the queue drains. It holds one privilege —
"validate a typed request and start the existing worker code" — and nothing of the deleted
broker's session/campaign machinery.

Security posture, in order of operations per frame:
1. secure read (O_NOFOLLOW, regular file, owner, mode, size cap) — before trusting a byte;
2. idempotency check BEFORE claim (replay a terminal receipt; refuse id reuse);
3. atomic claim (inbox→processing rename; the losing racer gets FileNotFoundError);
4. full ``validate_request`` AFTER claim — a frame that fails policy gets a *rejected*
   receipt, never execution;
5. envelope→internal translation that can only produce a sandboxed worker task — the repo
   path comes from the registry, never the request; no command/env/acceptance can survive.

Execution is injected (``execute``) so tests exercise the whole pipeline without spawning a
real worker or a real sandbox.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import availability, bounded_process, changesets, worker_config
from . import dispatch_admission as admission
from . import dispatch_lease as leases
from . import dispatch_protocol as dp
from . import releases
from .managed import with_release_metadata
from .seat_registry import registered_seat_ids
from .state import (
    PoolSnapshotError,
    claim_alive,
    has_auth,
    load_claude_rate_limits,
    load_settings,
    read_pool_snapshot,
)
from .task_policy import resolve_task_policy, timeout_seconds
from .workers import RunResult
from .worktree_task import TaskArtifact

ROOT = Path(os.environ.get("CODEX_SEATS_ROOT", Path.home() / ".codex-seats"))

# terminal worker statuses we surface verbatim; anything else the worker returns is
# recorded but still terminal for the receipt.
_WORKER_TERMINAL = frozenset({"completed", "failed", "blocked_needs_controller"})
_RETRYABLE_CAPACITY = frozenset({"unavailable", "reserved", "queued"})
_MAX_CAPACITY_BACKOFF_SECONDS = 60
_PINNED_CHILD_CLEANUP_SECONDS = 60
_CANCEL_TERM_SECONDS = 5.0


ExecuteFn = Callable[..., TaskArtifact]


def _cancel_on_sigterm(_signum: int, _frame: Any) -> None:
    """Turn managed cancellation into normal nested lifecycle unwinding."""
    raise bounded_process.ProcessCancelledError("dispatch job cancellation requested")


def _log(paths: dict[str, Path], message: str) -> None:
    line = f"{int(time.time())} {message}\n"
    with open(paths["logs"] / "runner.log", "a", encoding="utf-8") as handle:
        handle.write(line)


def _quarantine(ready: Path, paths: dict[str, Path], reason: str, suffix: str) -> None:
    """Move a frame we will not execute out of the inbox and record why."""
    dest = paths["logs"] / f"{ready.stem}.{suffix}"
    try:
        os.rename(ready, dest)
    except FileNotFoundError:
        return
    _log(paths, f"{suffix} {ready.name}: {reason}")


def _internal_request(
    frame: dict[str, Any], repo_path: Path, settings: dict[str, Any]
) -> dict[str, Any]:
    """Translate a typed envelope into an internal ``dispatch_run`` request.

    Only known-safe keys are produced; the repo path is the registry-resolved one, and the
    prompt is the only head-supplied content — it can only run inside the sandboxed worker.
    """
    profile = resolve_task_policy(
        settings,
        frame.get("kind", "implementation"),
        size=frame.get("size", "auto"),
        capability=frame.get("capability_tier", "auto"),
        task_shape=frame.get("task_shape"),
        validation=frame.get("validation"),
        effort=frame.get("effort"),
        estimated_minutes=frame.get("estimated_minutes"),
        parallel_safe=bool(frame.get("parallel_safe", False)),
        scope_hints=frame.get("scope_hints", []),
        risk_flags=frame.get("risk_flags", []),
        override_reason=frame.get("override_reason"),
    )
    internal = {
        "prompt": frame["prompt"],
        "cd": str(repo_path),
        "provider": frame.get("provider", "auto"),
        "kind": frame.get("kind", "implementation"),
        "task_shape": profile.task_shape,
        "validation": profile.validation,
        "task_id": frame.get("task_id", frame["request_id"]),
        "attempt_id": frame["request_id"],
        "size": frame.get("size", "auto"),
        "capability_tier": frame.get("capability_tier", "auto"),
        "effort": frame.get("effort"),
        "estimated_minutes": frame.get("estimated_minutes"),
        "parallel_safe": frame.get("parallel_safe", False),
        "scope_hints": list(frame.get("scope_hints", [])),
        "risk_flags": list(frame.get("risk_flags", [])),
        "override_reason": frame.get("override_reason"),
        "label": frame["request_id"],
        "timeout": timeout_seconds(profile),
        # The filesystem dispatch queue is the sole owner of delegated work.  Providers
        # may report capacity, but must neither append to the ledger queue nor run
        # a usage probe on behalf of the non-interactive runner.
        "no_queue": True,
        "no_probe": True,
        "release_id": frame["release_id"],
        "protocol_version": frame["protocol_version"],
    }
    reserve_authority = frame.get("reserve_spend_authority")
    if reserve_authority is not None:
        # Protocol validation has proved the exact typed shape. Keep it typed
        # internally and force precisely that seat; never translate to an
        # allow_reserve switch, which would skip unrelated capacity checks.
        internal["seat"] = reserve_authority["seat"]
        internal["_reserve_spend_authority"] = dict(reserve_authority)
    internal = {key: value for key, value in internal.items() if value is not None}
    if frame.get("read_only") is True:
        internal["sandbox"] = "read-only"
    mcp = list(frame.get("mcp", []))
    if mcp:
        # Validate the controller-authored catalog before worktree creation for an
        # immediate diagnostic. The provider validates the committed worktree copy again.
        worker_config.validated_worker_mcp_specs(mcp, repo_path)
        internal["mcp"] = mcp
    # Origin-seat holdout: a Codex head must never delegate to its own seat — that identity
    # is busy heading (and, for the primary, is the reserved phone/terminal cockpit). Reuses
    # the existing ``_excluded_seats`` plumbing that threads through to ``pick_seat``.
    # The router may deliberately clear it only for the isolated last-resort head worker.
    origin = frame.get("origin") or {}
    internal["_origin_head"] = origin.get("head")
    if isinstance(origin.get("seat"), str):
        internal["_origin_seat"] = origin["seat"]
    if origin.get("head") == "codex" and isinstance(origin.get("seat"), str):
        internal["_excluded_seats"] = [origin["seat"]]
    return internal


def _resolve_task_origin(
    frame: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    """Return a task frame whose origin matches the current seat registry."""
    if frame.get("operation") != "task":
        return frame
    raw = frame.get("origin")
    origin = raw if isinstance(raw, dict) else {}
    resolved = availability.resolve_schema3_origin(
        settings,
        availability.HeadOrigin(
            origin.get("head") if isinstance(origin.get("head"), str) else "",
            origin.get("seat") if isinstance(origin.get("seat"), str) else None,
        ),
    )
    normalized = dict(frame)
    normalized["origin"] = {"head": resolved.head, "seat": resolved.seat}
    reserve_authority = normalized.get("reserve_spend_authority")
    if (
        isinstance(reserve_authority, dict)
        and resolved.head == "codex"
        and reserve_authority.get("seat") == resolved.seat
    ):
        raise availability.OriginError(
            "reserve spending cannot target the active Codex head seat"
        )
    return normalized


def _clean_summary(result: Any) -> tuple[str, list[Any], list[Any]]:
    """Prefer the worker's structured summary over its raw last message.

    A Codex worker's ``message`` is the raw last agent line (often embedded JSONL); the
    validated ``structured_result`` carries a clean ``summary`` plus test evidence and
    warnings, which is what a head wants to read.
    """
    structured = getattr(result, "structured_result", None)
    if isinstance(structured, dict):
        summary = str(structured.get("summary") or "").strip()
        evidence = structured.get("test_evidence") or []
        warnings = structured.get("warnings") or []
        if summary:
            return summary, list(evidence), list(warnings)
    return (getattr(result, "message", None) or "").strip(), [], []


def _terminal_receipt(
    frame: dict[str, Any],
    artifact: TaskArtifact,
    paths: dict[str, Path],
    *,
    request_hash: str | None = None,
) -> dict[str, Any]:
    result = artifact.result
    patch = artifact.diff_text.encode("utf-8")
    diff_path: str | None = None
    diff_sha = "sha256:" + hashlib.sha256(patch).hexdigest()
    changeset_id: str | None = None
    changeset_sha: str | None = None
    effective_request_hash = request_hash or dp.content_hash(frame)
    if patch or artifact.changed_files:
        change = changesets.create(
            paths["base"].parent,
            request_id=frame["request_id"],
            request_hash=effective_request_hash,
            release_id=frame["release_id"],
            protocol_version=frame["protocol_version"],
            repo_id=frame["repo_id"],
            base_commit=str(artifact.base or ""),
            changed_files=list(artifact.changed_files),
            terminal_status=str(getattr(result, "status", "unknown")),
            patch=patch,
        )
        diff_path = str(change.directory / changesets.PATCH_NAME)
        changeset_id = change.id
        changeset_sha = change.manifest_sha256
    summary, test_evidence, warnings = _clean_summary(result)
    structured_result = with_release_metadata(
        getattr(result, "structured_result", None),
        frame["release_id"],
        frame["protocol_version"],
    )
    return {
        "request_id": frame["request_id"],
        "task_id": frame.get("task_id", frame["request_id"]),
        "attempt_id": frame["request_id"],
        "request_hash": effective_request_hash,
        "release_id": frame["release_id"],
        "protocol_version": frame["protocol_version"],
        "protocol_min": dp.MIN_PROTOCOL_VERSION,
        "protocol_max": dp.MAX_PROTOCOL_VERSION,
        "status": getattr(result, "status", "unknown"),
        "provider": getattr(result, "provider", "") or "",
        "seat": getattr(result, "worker", "") or "",
        "summary": summary,
        "test_evidence": test_evidence,
        "warnings": warnings,
        "worker_access": (
            structured_result.get("worker_access")
            if isinstance(structured_result, dict)
            else None
        ),
        "tokens": int(getattr(result, "tokens", 0) or 0),
        **(
            {"local_usage_available": result.tokens is not None}
            if getattr(result, "provider", None) == "local"
            else {}
        ),
        "duration": int(getattr(result, "duration", 0) or 0),
        "model": getattr(result, "model", None),
        "requested_effort": frame.get("effort"),
        "effort": getattr(result, "effort", None),
        "effort_downgraded_from": getattr(
            result, "effort_downgraded_from", None
        ),
        "override_reason": frame.get("override_reason"),
        "changed_files": list(artifact.changed_files),
        "worktree_path": str(artifact.worktree_path) if artifact.worktree_path else None,
        "base": artifact.base,
        "branch": artifact.branch,
        "diff_path": diff_path,
        "diff_sha": diff_sha,
        "changeset_id": changeset_id,
        "changeset_sha": changeset_sha,
        "structured_result": structured_result,
        "origin": frame.get("origin"),
        "completed_at": int(time.time()),
    }


def _rejected_receipt(frame: dict[str, Any], reason: str) -> dict[str, Any]:
    receipt = {
        "request_id": frame["request_id"],
        "request_hash": dp.content_hash(frame),
        "status": "rejected",
        "reason": reason,
        "release_id": frame.get("release_id"),
        "protocol_version": frame.get("protocol_version", dp.PROTOCOL_VERSION),
        "protocol_min": dp.MIN_PROTOCOL_VERSION,
        "protocol_max": dp.MAX_PROTOCOL_VERSION,
        "origin": frame.get("origin"),
        "completed_at": int(time.time()),
    }
    if frame.get("operation") == "task":
        task_id = frame.get("task_id")
        receipt["task_id"] = (
            task_id
            if isinstance(task_id, str) and dp.IDENTITY_RE.fullmatch(task_id)
            else frame["request_id"]
        )
        receipt["attempt_id"] = frame["request_id"]
    return receipt


def _expired_receipt(frame: dict[str, Any]) -> dict[str, Any]:
    receipt = _rejected_receipt(frame, "queue deadline expires_at has passed")
    receipt["status"] = "expired"
    return receipt


def _error_receipt(
    frame: dict[str, Any], reason: str, *, request_hash: str | None = None
) -> dict[str, Any]:
    receipt = _rejected_receipt(frame, reason)
    if request_hash is not None:
        receipt["request_hash"] = request_hash
    receipt["status"] = "error"
    return receipt


def _retry_path(paths: dict[str, Path], request_id: str) -> Path:
    # Heads may write request frames only to inbox. Retry policy belongs to the
    # unsandboxed runner, so keep its sidecar in the runner-owned processing tree.
    return paths["processing"] / f".{request_id}.retry.json"


def _clear_retry(paths: dict[str, Path], request_id: str) -> None:
    _retry_path(paths, request_id).unlink(missing_ok=True)


def _retry_state(
    paths: dict[str, Path], frame: dict[str, Any]
) -> dict[str, Any] | None:
    state = leases.read(_retry_path(paths, frame["request_id"]))
    if (
        state is None
        or state.get("request_id") != frame["request_id"]
        or state.get("request_hash") != dp.content_hash(frame)
    ):
        _clear_retry(paths, frame["request_id"])
        return None
    return state


def _retry_is_delayed(
    paths: dict[str, Path], frame: dict[str, Any], *, now: float
) -> bool:
    state = _retry_state(paths, frame)
    not_before = state.get("not_before") if state else None
    expires_at = frame.get("expires_at")
    return (
        isinstance(not_before, (int, float))
        and not isinstance(not_before, bool)
        and isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and now < float(not_before)
        and now < float(expires_at)
    )


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _requeue_capacity(
    claimed: Path,
    frame: dict[str, Any],
    paths: dict[str, Path],
    *,
    now: float,
) -> int:
    """Durably defer the byte-identical outer frame after provider backpressure."""
    previous = _retry_state(paths, frame) or {}
    old_attempts = previous.get("attempts", 0)
    attempts = (
        min(old_attempts + 1, 64)
        if isinstance(old_attempts, int)
        and not isinstance(old_attempts, bool)
        and old_attempts >= 0
        else 1
    )
    delay = min(_MAX_CAPACITY_BACKOFF_SECONDS, 2 ** min(attempts - 1, 6))
    leases.write_atomic(
        _retry_path(paths, frame["request_id"]),
        {
            "request_id": frame["request_id"],
            "request_hash": dp.content_hash(frame),
            "attempts": attempts,
            "not_before": now + delay,
        },
    )
    ready = paths["inbox"] / f"{frame['request_id']}.ready"
    if ready.exists():
        try:
            ready_frame = dp.read_request_secure(ready)
            same = dp.content_hash(ready_frame) == dp.content_hash(frame)
        except dp.DispatchError:
            same = False
        if not same:
            _quarantine(ready, paths, "conflicts with capacity retry", "conflict")
        else:
            claimed.unlink(missing_ok=True)
            return delay
    os.rename(claimed, ready)
    _fsync_directory(paths["processing"])
    _fsync_directory(paths["inbox"])
    return delay


def _artifact_from_record(record: Any) -> TaskArtifact:
    if not isinstance(record, dict) or not isinstance(record.get("result"), dict):
        raise RuntimeError("pinned child returned an invalid artifact")
    values = record["result"]
    result = RunResult(
        code=int(values.get("code", 1)),
        status=str(values.get("status", "failed")),
        provider=str(values.get("provider", "")),
        worker=str(values.get("worker", "")),
        label=str(values.get("label", "")),
        duration=int(values.get("duration", 0) or 0),
        tokens=(
            None
            if values.get("provider") == "local" and values.get("tokens") is None
            else int(values.get("tokens", 0) or 0)
        ),
        log=values.get("log"),
        message=values.get("message"),
        fallback_safe=bool(values.get("fallback_safe", False)),
        model=values.get("model"),
        structured_result=values.get("structured_result"),
        effort=values.get("effort"),
        effort_downgraded_from=values.get("effort_downgraded_from"),
    )
    return TaskArtifact(
        result=result,
        changed_files=list(record.get("changed_files") or []),
        diff_text=str(record.get("diff_text") or ""),
        worktree_path=(
            Path(record["worktree_path"]) if record.get("worktree_path") else None
        ),
        base=record.get("base"),
        branch=record.get("branch"),
        retained=bool(record.get("retained", False)),
    )


def _runner_python(root: Path) -> Path:
    interpreter = root / ".venv" / "bin" / "python"
    return interpreter if interpreter.is_file() else Path(sys.executable)


def run_pinned_release_task(
    root: Path,
    request: dict[str, Any],
    release_id: str,
    *,
    retain: bool,
) -> TaskArtifact:
    """Execute one task through its immutable release-specific child module."""
    release_id = releases.validate_release_id(release_id)
    releases.verify_release(root, release_id)
    child_request = dict(request)
    child_request["release_id"] = release_id
    argv = releases.release_module_argv(
        root,
        release_id,
        "codex_seats.dispatch_child",
        python=_runner_python(root),
    )
    completed = bounded_process.run(
        argv,
        input_bytes=json.dumps(
            {"request": child_request, "retain": bool(retain)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        env=releases.release_environment(root, release_id),
        cwd=str(releases.release_path(root, release_id)),
        timeout=int(child_request.get("timeout", 1800)) + _PINNED_CHILD_CLEANUP_SECONDS,
        term_grace=_CANCEL_TERM_SECONDS - 1,
    )
    if completed.returncode != 0:
        detail = completed.stderr_text(errors="replace").strip()[-1000:] or f"exit {completed.returncode}"
        raise RuntimeError(f"pinned release child failed: {detail}")
    try:
        return _artifact_from_record(json.loads(completed.stdout))
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("pinned release child returned invalid JSON") from exc


def _process_claimed(
    root: Path,
    settings: dict[str, Any],
    claimed: Path,
    frame: dict[str, Any],
    *,
    now: float,
    execute: ExecuteFn | None,
    paths: dict[str, Path],
) -> str:
    """Validate a claim, execute it, or return its unchanged frame to this queue."""
    try:
        repo_path, normalized = dp.validate_request(frame, settings, now=now)
        normalized = _resolve_task_origin(normalized, settings)
        if execute is None:
            releases.release_path(root, normalized["release_id"])
    except (dp.DispatchError, releases.ReleaseError, availability.OriginError) as exc:
        expired = isinstance(exc, dp.DispatchError) and str(exc) == "request has expired"
        dp.write_receipt(
            root, _expired_receipt(frame) if expired else _rejected_receipt(frame, str(exc))
        )
        _clear_retry(paths, frame["request_id"])
        claimed.unlink(missing_ok=True)
        outcome = "expired" if expired else "rejected"
        _log(paths, f"{outcome} {frame['request_id']}: {exc}")
        return outcome

    try:
        # retain=False: the full diff is captured in the receipt artifact, so the head
        # reviews that — no need to keep the worktree around (they'd accumulate). A future
        # "follow-up in the same worktree" feature can opt back into retention + a GC.
        internal = _internal_request(normalized, repo_path, settings)
        if execute is None:
            artifact = run_pinned_release_task(
                root, internal, normalized["release_id"], retain=False
            )
        else:
            artifact = execute(root, settings, internal, retain=False)
    except Exception as exc:  # worktree error, provider crash — stay alive, record it
        dp.write_receipt(
            root,
            _error_receipt(
                normalized,
                f"execution error: {exc}",
                request_hash=dp.content_hash(frame),
            ),
        )
        _clear_retry(paths, frame["request_id"])
        claimed.unlink(missing_ok=True)
        _log(paths, f"error {frame['request_id']}: {exc}")
        return "error"

    status = str(getattr(artifact.result, "status", "unknown"))
    if status in _RETRYABLE_CAPACITY:
        if now >= float(normalized["expires_at"]):
            dp.write_receipt(root, _expired_receipt(frame))
            _clear_retry(paths, frame["request_id"])
            claimed.unlink(missing_ok=True)
            _log(paths, f"expired {frame['request_id']} after capacity result")
            return "expired"
        delay = _requeue_capacity(claimed, frame, paths, now=now)
        _log(
            paths,
            f"capacity {frame['request_id']} status={status}; retry in {delay}s",
        )
        return "requeued"

    try:
        receipt = _terminal_receipt(
            normalized,
            artifact,
            paths,
            request_hash=dp.content_hash(frame),
        )
    except (changesets.ChangeSetError, OSError) as exc:
        dp.write_receipt(
            root,
            _error_receipt(
                normalized,
                f"ChangeSet persistence error: {exc}",
                request_hash=dp.content_hash(frame),
            ),
        )
        _clear_retry(paths, frame["request_id"])
        claimed.unlink(missing_ok=True)
        _log(paths, f"error {frame['request_id']}: ChangeSet persistence failed: {exc}")
        return "error"
    _receipt_path, won_receipt = dp.publish_receipt(root, receipt)
    if not won_receipt and receipt.get("changeset_id") and receipt.get("changeset_sha"):
        existing = dp.read_receipt(root, frame["request_id"])
        already_anchored = bool(
            existing
            and existing.get("changeset_id") == receipt["changeset_id"]
            and existing.get("changeset_sha") == receipt["changeset_sha"]
        )
        if not already_anchored:
            try:
                changesets.abandon_unpublished(
                    root, receipt["changeset_id"], receipt["changeset_sha"]
                )
            except (changesets.ChangeSetError, OSError) as exc:
                _log(
                    paths,
                    f"error {frame['request_id']}: could not tombstone unpublished "
                    f"ChangeSet: {exc}",
                )
    _clear_retry(paths, frame["request_id"])
    claimed.unlink(missing_ok=True)
    _log(paths, f"completed {frame['request_id']} status={getattr(artifact.result,'status','?')}")
    return "completed"


def reconcile_processing(
    root: Path,
    paths: dict[str, Path],
    *,
    alive_fn: Callable[[Any], bool] = leases.pid_alive,
) -> None:
    """Preserve live children and requeue only dead/missing lease owners."""
    for claimed in sorted(paths["processing"].glob("*.claimed")):
        rid = claimed.stem
        lease_path = leases.lease_path(paths["processing"], rid)
        if dp.read_receipt(root, rid) is not None:
            claimed.unlink(missing_ok=True)
            lease_path.unlink(missing_ok=True)
            _clear_retry(paths, rid)
            continue
        lease = leases.read(lease_path)
        if lease is not None and alive_fn(lease.get("job_pid")):
            continue
        requeued = paths["inbox"] / f"{rid}.ready"
        lease_path.unlink(missing_ok=True)
        if requeued.exists():
            try:
                claimed_frame = dp.read_request_secure(claimed)
                ready_frame = dp.read_request_secure(requeued)
                same = dp.content_hash(claimed_frame) == dp.content_hash(ready_frame)
            except dp.DispatchError:
                same = False
            if same:
                claimed.unlink(missing_ok=True)
                _log(paths, f"orphaned {rid} was already queued")
                continue
            _quarantine(
                requeued,
                paths,
                "request_id conflicts with recovered claim",
                "conflict",
            )
        try:
            os.rename(claimed, requeued)
            _log(paths, f"requeued orphaned {rid} after crash")
        except OSError:
            claimed.unlink(missing_ok=True)

    for lease_path in sorted(paths["processing"].glob("*.lease.json")):
        rid = lease_path.name.removesuffix(".lease.json")
        claimed = paths["processing"] / f"{rid}.claimed"
        if claimed.exists():
            continue
        lease = leases.read(lease_path)
        if (
            dp.read_receipt(root, rid) is not None
            or lease is None
            or not alive_fn(lease.get("job_pid"))
        ):
            lease_path.unlink(missing_ok=True)
            if dp.read_receipt(root, rid) is not None:
                _clear_retry(paths, rid)

    # Retry metadata is meaningful only beside its ready frame or a crash-recoverable
    # claim.  Removing an orphan prevents QueueDirectories from repeatedly activating a
    # runner that has no actual work to claim.
    for retry_path in paths["processing"].glob(".*.retry.json"):
        rid = retry_path.name.removeprefix(".").removesuffix(".retry.json")
        ready = paths["inbox"] / f"{rid}.ready"
        claimed = paths["processing"] / f"{rid}.claimed"
        if not ready.exists() and not claimed.exists():
            retry_path.unlink(missing_ok=True)

    # A crash during atomic lease replacement can leave only a hidden temporary.
    # It is never admission state; remove only owner-controlled regular files
    # matching our exact generated suffix.
    for temporary in paths["processing"].glob(".*.lease.json.*.tmp"):
        try:
            info = temporary.lstat()
        except OSError:
            continue
        if (
            not temporary.is_symlink()
            and info.st_uid == os.getuid()
            and info.st_mode & 0o170000 == 0o100000
        ):
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class _Prepared:
    ready: Path
    frame: dict[str, Any]
    policy: admission.AdmissionFrame
    order: tuple[float, str]


def _cancelled_receipt(
    target: dict[str, Any], cancel: dict[str, Any]
) -> dict[str, Any]:
    receipt = {
        "request_id": target["request_id"],
        "request_hash": dp.content_hash(target),
        "release_id": target.get("release_id"),
        "protocol_version": target.get("protocol_version", dp.PROTOCOL_VERSION),
        "protocol_min": dp.MIN_PROTOCOL_VERSION,
        "protocol_max": dp.MAX_PROTOCOL_VERSION,
        "status": "cancelled",
        "summary": f"cancelled by request {cancel['request_id']}",
        "origin": target.get("origin"),
        "cancel_request_id": cancel["request_id"],
        "completed_at": int(time.time()),
    }
    if target.get("operation") == "task":
        receipt["task_id"] = target.get("task_id", target["request_id"])
        receipt["attempt_id"] = target["request_id"]
    return receipt


def _cancel_result_receipt(
    cancel: dict[str, Any], result: str, *, reason: str | None = None
) -> dict[str, Any]:
    receipt = {
        "request_id": cancel["request_id"],
        "request_hash": dp.content_hash(cancel),
        "release_id": cancel.get("release_id"),
        "protocol_version": cancel.get("protocol_version", dp.PROTOCOL_VERSION),
        "protocol_min": dp.MIN_PROTOCOL_VERSION,
        "protocol_max": dp.MAX_PROTOCOL_VERSION,
        "status": "completed",
        "operation": "cancel",
        "target_request_id": cancel.get("target_request_id"),
        "result": result,
        "origin": cancel.get("origin"),
        "completed_at": int(time.time()),
    }
    if reason:
        receipt["reason"] = reason
    return receipt


def _verified_process_group(
    claimed_frame: dict[str, Any], lease: dict[str, Any] | None
) -> int | None:
    """Resolve and verify a job group only from its claim plus owner-only lease."""
    request_id = claimed_frame.get("request_id")
    if (
        lease is None
        or lease.get("request_id") != request_id
        or lease.get("request_hash") != dp.content_hash(claimed_frame)
    ):
        return None
    pid = lease.get("job_pid")
    pgid = lease.get("process_group_id")
    session_id = lease.get("session_id")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or pgid != pid
        or session_id != pid
    ):
        return None
    try:
        if os.getpgid(pid) != pgid or os.getsid(pid) != session_id:
            return None
    except (ProcessLookupError, PermissionError, OSError):
        return None
    return pgid


def _process_group_alive(pgid: int) -> bool:
    try:
        return os.getpgid(pgid) == pgid and os.getsid(pgid) == pgid
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _terminate_process_group(pgid: int) -> None:
    """TERM one verified worker session, then apply a short bounded KILL fallback."""
    if not _process_group_alive(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise RuntimeError("verified worker process group could not be signaled") from exc
    deadline = time.monotonic() + _CANCEL_TERM_SECONDS
    while time.monotonic() < deadline:
        try:
            reaped, _status = os.waitpid(pgid, os.WNOHANG)
            if reaped == pgid:
                return
        except ChildProcessError:
            pass
        if not _process_group_alive(pgid):
            return
        time.sleep(0.05)
    # The original leader is the only safe proof that this numeric group still
    # identifies the leased session. If it exited, never target a possibly reused id.
    if not _process_group_alive(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise RuntimeError("verified worker process group could not be killed") from exc


def _cancel_target(
    root: Path,
    paths: dict[str, Path],
    cancel: dict[str, Any],
    *,
    uid: int | None,
) -> tuple[str, str | None]:
    target_id = cancel["target_request_id"]
    if dp.read_receipt(root, target_id) is not None:
        return "already_terminal", None

    ready = paths["inbox"] / f"{target_id}.ready"
    if ready.exists():
        try:
            target = dp.read_request_secure(ready, uid=uid)
        except dp.DispatchError as exc:
            return "error", f"target ready frame is untrusted: {exc}"
        if target.get("request_id") != target_id:
            return "error", "target ready frame identity mismatch"
        _path, won = dp.publish_receipt(root, _cancelled_receipt(target, cancel))
        if won:
            ready.unlink(missing_ok=True)
            _clear_retry(paths, target_id)
            _log(paths, f"cancelled queued {target_id} by {cancel['request_id']}")
            return "cancelled", None
        return "already_terminal", None

    claimed = paths["processing"] / f"{target_id}.claimed"
    lease_path = leases.lease_path(paths["processing"], target_id)
    if claimed.exists() or lease_path.exists():
        try:
            target = dp.read_request_secure(claimed, uid=uid)
        except dp.DispatchError as exc:
            return "error", f"target claimed frame is untrusted: {exc}"
        if target.get("request_id") != target_id:
            return "error", "target claimed frame identity mismatch"
        pgid = _verified_process_group(target, leases.read(lease_path, uid=uid))
        if pgid is None:
            return "error", "target process ownership could not be verified"
        _path, won = dp.publish_receipt(root, _cancelled_receipt(target, cancel))
        if not won:
            return "already_terminal", None
        try:
            _terminate_process_group(pgid)
        finally:
            claimed.unlink(missing_ok=True)
            lease_path.unlink(missing_ok=True)
            _clear_retry(paths, target_id)
        _log(paths, f"cancelled running {target_id} by {cancel['request_id']}")
        return "cancelled", None
    return "unknown", None


def _process_cancellations(
    root: Path,
    settings: dict[str, Any],
    paths: dict[str, Path],
    *,
    now: float,
    uid: int | None,
) -> list[tuple[str, str]]:
    """Claim and finish every control request before considering task admission."""
    outcomes: list[tuple[str, str]] = []
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for ready in sorted(paths["inbox"].glob("*.ready")):
        try:
            frame = dp.read_request_secure(ready, uid=uid)
        except dp.DispatchError:
            continue
        if frame.get("operation") == "cancel":
            candidates.append((ready, frame))
    for ready, frame in candidates:
        rid = frame.get("request_id")
        if not isinstance(rid, str) or not dp.REQUEST_ID_RE.fullmatch(rid):
            continue
        verb, _prior = dp.idempotency_decision(root, frame)
        if verb == "replay":
            ready.unlink(missing_ok=True)
            outcomes.append((rid, "replay"))
            continue
        if verb == "conflict":
            _quarantine(ready, paths, "request_id reuse with different body", "conflict")
            outcomes.append((rid, "conflict"))
            continue
        try:
            claimed = dp.claim(root, ready)
        except (FileNotFoundError, FileExistsError):
            continue
        try:
            dp.validate_request(frame, settings, now=now)
        except dp.DispatchError as exc:
            dp.publish_receipt(root, _rejected_receipt(frame, str(exc)))
            claimed.unlink(missing_ok=True)
            outcomes.append((rid, "rejected"))
            continue
        try:
            result, reason = _cancel_target(root, paths, frame, uid=uid)
        except Exception as exc:
            result, reason = "error", f"cancellation failed: {exc}"
            _log(paths, f"cancel error {rid}: {exc}")
        dp.publish_receipt(root, _cancel_result_receipt(frame, result, reason=reason))
        claimed.unlink(missing_ok=True)
        outcomes.append((rid, result))
    return outcomes


def _active_policies(
    paths: dict[str, Path],
    *,
    alive_fn: Callable[[Any], bool] = leases.pid_alive,
) -> list[admission.AdmissionFrame]:
    active: list[admission.AdmissionFrame] = []
    for claimed in sorted(paths["processing"].glob("*.claimed")):
        rid = claimed.stem
        lease = leases.read(leases.lease_path(paths["processing"], rid))
        if lease is None or not alive_fn(lease.get("job_pid")):
            continue
        policy = leases.admission_from_lease(lease)
        if policy is None:
            policy = admission.AdmissionFrame(
                request_id=rid,
                repo=f"<unknown:{rid}>",
                serial=True,
            )
        active.append(policy)
    return active


def _reject_ready(
    root: Path,
    ready: Path,
    frame: dict[str, Any],
    reason: str,
    paths: dict[str, Path],
) -> str:
    try:
        claimed = dp.claim(root, ready)
    except (FileNotFoundError, FileExistsError):
        return "lost"
    dp.write_receipt(root, _rejected_receipt(frame, reason))
    _clear_retry(paths, frame["request_id"])
    claimed.unlink(missing_ok=True)
    _log(paths, f"rejected {frame['request_id']}: {reason}")
    return "rejected"


def _expire_ready(
    root: Path,
    ready: Path,
    frame: dict[str, Any],
    paths: dict[str, Path],
) -> str:
    try:
        claimed = dp.claim(root, ready)
    except (FileNotFoundError, FileExistsError):
        return "lost"
    dp.write_receipt(root, _expired_receipt(frame))
    _clear_retry(paths, frame["request_id"])
    claimed.unlink(missing_ok=True)
    leases.lease_path(paths["processing"], frame["request_id"]).unlink(
        missing_ok=True
    )
    _log(paths, f"expired {frame['request_id']}: queue deadline passed")
    return "expired"


def _prepare_ready(
    root: Path,
    settings: dict[str, Any],
    paths: dict[str, Path],
    *,
    now: float,
    uid: int | None,
) -> tuple[list[_Prepared], list[tuple[str, str]]]:
    """Secure-read, idempotency-check, and fully validate before any spawn."""
    prepared: list[_Prepared] = []
    outcomes: list[tuple[str, str]] = []
    for ready in sorted(paths["inbox"].glob("*.ready")):
        try:
            frame = dp.read_request_secure(ready, uid=uid)
        except dp.DispatchError as exc:
            _clear_retry(paths, ready.name.removesuffix(".ready"))
            _quarantine(ready, paths, str(exc), "unreadable")
            continue
        rid = frame.get("request_id")
        if not isinstance(rid, str) or not dp.REQUEST_ID_RE.match(rid):
            _clear_retry(paths, ready.name.removesuffix(".ready"))
            _quarantine(ready, paths, "missing/invalid request_id", "unreadable")
            continue
        verb, _prior = dp.idempotency_decision(root, frame)
        if verb == "replay":
            ready.unlink(missing_ok=True)
            _clear_retry(paths, rid)
            outcomes.append((rid, "replay"))
            continue
        if verb == "conflict":
            _quarantine(ready, paths, "request_id reuse with different body", "conflict")
            _clear_retry(paths, rid)
            outcomes.append((rid, "conflict"))
            continue
        in_flight = paths["processing"] / f"{rid}.claimed"
        if in_flight.exists():
            try:
                claimed_frame = dp.read_request_secure(in_flight, uid=uid)
                same = dp.content_hash(claimed_frame) == dp.content_hash(frame)
            except dp.DispatchError:
                same = False
            if same:
                ready.unlink(missing_ok=True)
                outcomes.append((rid, "inflight"))
            else:
                _quarantine(
                    ready,
                    paths,
                    "request_id reuse while a different body is processing",
                    "conflict",
                )
                outcomes.append((rid, "conflict"))
            continue
        try:
            repo_path, validated = dp.validate_request(frame, settings, now=now)
            validated = _resolve_task_origin(validated, settings)
            policy = admission.AdmissionFrame.from_frame(
                validated, str(repo_path.resolve())
            )
        except (dp.DispatchError, admission.ScopeError, availability.OriginError) as exc:
            if isinstance(exc, dp.DispatchError) and str(exc) == "request has expired":
                outcome = _expire_ready(root, ready, frame, paths)
            else:
                outcome = _reject_ready(root, ready, frame, str(exc), paths)
            if outcome != "lost":
                outcomes.append((rid, outcome))
            continue
        if _retry_is_delayed(paths, frame, now=now):
            continue
        created = frame.get("created_at")
        order_time = (
            float(created)
            if isinstance(created, (int, float))
            and not isinstance(created, bool)
            else float("inf")
        )
        prepared.append(_Prepared(ready, frame, policy, (order_time, rid)))
    prepared.sort(key=lambda item: item.order)
    return prepared, outcomes


def _job_process(
    root: Path,
    settings: dict[str, Any],
    claimed: Path,
    frame: dict[str, Any],
    *,
    now: float,
    execute: ExecuteFn | None,
    paths: dict[str, Path],
    lease_path: Path,
    gate_fd: int,
) -> None:
    """Wait for the durable lease, then own execution, receipt, and cleanup."""
    # Raising lets bounded_process stop the pinned child before this job exits.
    signal.signal(signal.SIGTERM, _cancel_on_sigterm)
    try:
        token = os.read(gate_fd, 1)
    finally:
        os.close(gate_fd)
    if token != b"1":
        return
    leases.heartbeat(
        lease_path,
        request_id=frame["request_id"],
        job_pid=os.getpid(),
        now=time.time(),
    )
    try:
        _process_claimed(
            root,
            settings,
            claimed,
            frame,
            now=now,
            execute=execute,
            paths=paths,
        )
    finally:
        lease_path.unlink(missing_ok=True)


def _spawn_claimed(
    root: Path,
    settings: dict[str, Any],
    claimed: Path,
    frame: dict[str, Any],
    policy: admission.AdmissionFrame,
    *,
    now: float,
    execute: ExecuteFn | None,
    paths: dict[str, Path],
    close_in_child: tuple[int, ...] = (),
    settings_reader: Any | None = None,
) -> int:
    """Fork a new session behind barriers so execution follows its durable lease."""
    read_fd, write_fd = os.pipe()
    ready_read_fd, ready_write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - covered through process-contract tests
        os.close(write_fd)
        os.close(ready_read_fd)
        for inherited_fd in close_in_child:
            try:
                os.close(inherited_fd)
            except OSError:
                pass
        try:
            os.setsid()
            if os.getpgrp() != os.getpid() or os.getsid(0) != os.getpid():
                raise RuntimeError("job child did not become its own process group/session")
            os.write(ready_write_fd, b"1")
            os.close(ready_write_fd)
            _job_process(
                root,
                settings,
                claimed,
                frame,
                now=now,
                execute=execute,
                paths=paths,
                lease_path=leases.lease_path(
                    paths["processing"], frame["request_id"]
                ),
                gate_fd=read_fd,
            )
        finally:
            os._exit(0)

    os.close(read_fd)
    os.close(ready_write_fd)
    lease_path = leases.lease_path(paths["processing"], frame["request_id"])
    try:
        if os.read(ready_read_fd, 1) != b"1":
            raise RuntimeError("job child failed to establish its process session")
        if os.getpgid(pid) != pid or os.getsid(pid) != pid:
            raise RuntimeError("job child process group/session identity mismatch")
        lease = leases.build_lease(
            frame,
            policy,
            coordinator_pid=os.getpid(),
            job_pid=pid,
            process_group_id=pid,
            session_id=pid,
            now=now,
            settings_reader=(
                settings_reader.for_process("dispatch-job", pid)
                if settings_reader is not None
                else None
            ),
        )
        leases.write_atomic(lease_path, lease)
        os.write(write_fd, b"1")
    except Exception:
        os.close(write_fd)
        os.waitpid(pid, 0)
        lease_path.unlink(missing_ok=True)
        raise
    else:
        os.close(write_fd)
    finally:
        os.close(ready_read_fd)
    return pid


def dispatch_parallel_once(
    root: Path,
    settings: dict[str, Any],
    *,
    now_fn: Callable[[], float],
    execute: ExecuteFn | None = None,
    uid: int | None = None,
    close_in_child: tuple[int, ...] = (),
    settings_reader: Any | None = None,
) -> tuple[list[tuple[str, str]], set[int]]:
    """Validate a queue snapshot and spawn every currently-admissible frame."""
    paths = dp.ensure_queue(root)
    now = now_fn()
    del settings
    pool_snapshot: dict[str, Any] | None = None
    authenticated: set[str] = set()
    claude_limits: dict[str, Any] = {}
    # A coordinator must never continue from its startup snapshot. Settings
    # that are missing or cannot be read fail to zero new work.
    cycle_settings, settings_warnings = (
        settings_reader.load(load_settings)
        if settings_reader is not None
        else load_settings(root)
    )
    settings_failed = any(
        warning.startswith(("settings missing", "invalid settings"))
        for warning in settings_warnings
    )
    if not settings_failed:
        # Missing/invalid settings or an unreadable ledger mean zero new work
        # for this cycle; existing provider claims remain authoritative.
        try:
            pool_snapshot = read_pool_snapshot(root)
            confirmed_settings, confirmed_warnings = (
                settings_reader.load(load_settings)
                if settings_reader is not None
                else load_settings(root)
            )
            if confirmed_settings != cycle_settings or any(
                warning.startswith(("settings missing", "invalid settings"))
                for warning in confirmed_warnings
            ):
                raise ValueError("settings changed during the admission snapshot")
            authenticated = {
                seat_id
                for seat_id in registered_seat_ids(cycle_settings)
                if has_auth(root, seat_id, cycle_settings)
            }
            claude_limits = load_claude_rate_limits(root, at=now)
        except (OSError, PoolSnapshotError, TypeError, ValueError):
            pool_snapshot = None
    cancellation_outcomes = _process_cancellations(
        root, cycle_settings, paths, now=now, uid=uid
    )
    if settings_failed or pool_snapshot is None:
        return cancellation_outcomes, set()
    prepared, outcomes = _prepare_ready(
        root, cycle_settings, paths, now=now, uid=uid
    )
    outcomes = cancellation_outcomes + outcomes
    active = _active_policies(paths)
    max_active = admission.MAX_ACTIVE_JOBS
    origins = {
        availability.HeadOrigin(policy.origin_head or "", policy.origin_seat)
        for policy in (*active, *(item.policy for item in prepared))
    }
    runtime_seats = pool_snapshot.get("seats", {})
    live_codex = {
        seat_id
        for seat_id, runtime in runtime_seats.items()
        if isinstance(runtime, dict) and claim_alive(runtime.get("in_flight"))
    }
    claude = pool_snapshot.get("providers", {}).get("claude", {})
    report_kwargs = {
        "current": now,
        "codex_live_claims": live_codex,
        "claude_live_claim": (
            isinstance(claude, dict) and claim_alive(claude.get("in_flight"))
        ),
        "authenticated_codex": authenticated,
        "claude_limits": claude_limits,
    }
    candidate_max_active: dict[str, int] = {}
    for item in prepared:
        authority = item.frame.get("reserve_spend_authority")
        reserve_targets = (
            {str(authority["seat"])} if isinstance(authority, dict) else set()
        )
        report = availability.schema3_concurrency_report(
            cycle_settings,
            pool_snapshot,
            origins,
            reserve_spend_seats=reserve_targets,
            **report_kwargs,
        )
        candidate_max_active[item.policy.request_id] = min(
            admission.MAX_ACTIVE_JOBS,
            int(report["conservative_mixed_origin_ceiling"]),
        )
    chosen = admission.select_admissible(
        (item.policy for item in prepared),
        active,
        max_active=max_active,
        candidate_max_active=candidate_max_active,
    )
    chosen_ids = {item.request_id for item in chosen}
    children: set[int] = set()
    for item in prepared:
        if item.policy.request_id not in chosen_ids:
            continue
        try:
            claimed = dp.claim(root, item.ready)
        except (FileNotFoundError, FileExistsError):
            continue
        try:
            pid = _spawn_claimed(
                root,
                cycle_settings,
                claimed,
                item.frame,
                item.policy,
                now=now,
                execute=execute,
                paths=paths,
                close_in_child=close_in_child,
                settings_reader=settings_reader,
            )
        except Exception as exc:
            requeued = paths["inbox"] / f"{item.policy.request_id}.ready"
            try:
                os.rename(claimed, requeued)
            except OSError:
                claimed.unlink(missing_ok=True)
            _log(paths, f"spawn failed {item.policy.request_id}: {exc}")
            outcomes.append((item.policy.request_id, "spawn_failed"))
            continue
        children.add(pid)
        outcomes.append((item.policy.request_id, "spawned"))
    return outcomes, children


def _reap(children: set[int], *, block: bool) -> None:
    options = 0 if block else os.WNOHANG
    while children:
        try:
            pid, _status = os.waitpid(-1, options)
        except ChildProcessError:
            children.clear()
            return
        if pid == 0:
            return
        children.discard(pid)
        if block:
            return


def run_coordinator(
    root: Path,
    settings: dict[str, Any],
    *,
    now_fn: Callable[[], float] = time.time,
    execute: ExecuteFn | None = None,
    idle_wait: float = 1.0,
    close_in_child: tuple[int, ...] = (),
    settings_reader: Any | None = None,
) -> list[tuple[str, str]]:
    """Drain to inbox+processing quiescence without polling either provider."""
    paths = dp.ensure_queue(root)
    outcomes: list[tuple[str, str]] = []
    children: set[int] = set()
    reconcile_processing(root, paths)
    while True:
        if settings_reader is not None:
            settings_reader.heartbeat()
        cycle, spawned = dispatch_parallel_once(
            root,
            settings,
            now_fn=now_fn,
            execute=execute,
            close_in_child=close_in_child,
            settings_reader=settings_reader,
        )
        outcomes.extend(cycle)
        children.update(spawned)
        _reap(children, block=False)

        ready = any(paths["inbox"].glob("*.ready"))
        processing = any(paths["processing"].glob("*.claimed")) or any(
            paths["processing"].glob("*.lease.json")
        )
        if not ready and not processing:
            if children:
                time.sleep(min(max(idle_wait, 0.01), 0.25))
                continue
            break
        # Stay responsive to newly-published cancellation frames while children run.
        # A short bounded sleep avoids both waitpid starvation and a busy-spin.
        time.sleep(min(max(idle_wait, 0.01), 0.25) if children else idle_wait)
        reconcile_processing(root, paths)
    return outcomes


def drain_once(
    root: Path,
    settings: dict[str, Any],
    *,
    now_fn: Callable[[], float],
    execute: ExecuteFn | None = None,
    uid: int | None = None,
) -> list[tuple[str, str]]:
    """Process every currently-ready frame once. Returns (request_id, outcome) pairs."""
    paths = dp.ensure_queue(root)
    outcomes = _process_cancellations(
        root, settings, paths, now=now_fn(), uid=uid
    )
    for ready in sorted(paths["inbox"].glob("*.ready")):
        try:
            frame = dp.read_request_secure(ready, uid=uid)
        except dp.DispatchError as exc:
            _clear_retry(paths, ready.name.removesuffix(".ready"))
            _quarantine(ready, paths, str(exc), "unreadable")
            continue
        rid = frame.get("request_id")
        if not isinstance(rid, str) or not dp.REQUEST_ID_RE.match(rid):
            _clear_retry(paths, ready.name.removesuffix(".ready"))
            _quarantine(ready, paths, "missing/invalid request_id", "unreadable")
            continue
        verb, _prior = dp.idempotency_decision(root, frame)
        if verb == "replay":
            ready.unlink(missing_ok=True)
            _clear_retry(paths, rid)
            outcomes.append((rid, "replay"))
            continue
        if verb == "conflict":
            _quarantine(ready, paths, "request_id reuse with different body", "conflict")
            _clear_retry(paths, rid)
            outcomes.append((rid, "conflict"))
            continue
        attempt_now = now_fn()
        if _retry_is_delayed(paths, frame, now=attempt_now):
            continue
        try:
            claimed = dp.claim(root, ready)
        except (FileNotFoundError, FileExistsError):
            continue  # a concurrent runner won the race
        outcome = _process_claimed(
            root, settings, claimed, frame, now=attempt_now, execute=execute, paths=paths
        )
        outcomes.append((rid, outcome))
    return outcomes


def main() -> int:
    root = ROOT
    from .runtime_readers import SettingsReader

    with SettingsReader(root, "dispatch-coordinator", module_file=__file__) as reader:
        settings, _warnings = reader.load(load_settings)
        paths = dp.ensure_queue(root)
        lock_fd = os.open(paths["lock"], os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock_fd)
            return 0  # another runner already draining
        try:
            reconcile_processing(root, paths)
            run_coordinator(
                root,
                settings,
                close_in_child=(lock_fd,),
                settings_reader=reader,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
