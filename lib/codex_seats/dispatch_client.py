"""Head-side client for the secure filesystem dispatch queue.

This module deliberately delegates envelope policy, serialization, and filesystem
operations to :mod:`codex_seats.dispatch_protocol`.  It contains no provider or seat
selection logic.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import unicodedata
from pathlib import Path

from . import dispatch_lease as leases
from . import dispatch_events
from . import dispatch_protocol
from .releases import ReleaseError, current_release_id, loaded_release_id
from .task_policy import resolve_task_policy


_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|auth(?:orization)?|bearer|cookie|credential|password|"
    r"passwd|private[_-]?key|secret|token)\b\s*[:=]\s*(?:\S+|\"[^\"]*\"|'[^']*')"
)
_ENV_ASSIGNMENT_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]{1,63}=(?:\S+|\"[^\"]*\"|'[^']*')"
)
_URL_RE = re.compile(r"\b(?:https?|ssh)://\S+", re.IGNORECASE)
_POSIX_PATH_RE = re.compile(r"(?<![\w:])(?:~?/|/)[^\s\"'`]+")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\s\"'`]+")
_KNOWN_SECRET_RE = re.compile(
    r"\b(?:"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"gh[pousr]_[A-Za-z0-9]{24,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"sk-(?:proj|ant)-[A-Za-z0-9_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")\b"
)
_OPAQUE_VALUE_RE = re.compile(r"\b[A-Za-z0-9_+/=-]{40,}\b")


def _submission_release_id(root) -> str:
    try:
        return loaded_release_id(root, __file__) or current_release_id(root)
    except ReleaseError as exc:
        raise dispatch_protocol.DispatchError(
            f"cannot resolve immutable submission release: {exc}"
        ) from exc


class ReceiptTimeout(TimeoutError):
    """Timeout raised after preserving receipts collected before the deadline."""

    def __init__(self, receipts, pending):
        self.receipts = dict(receipts)
        self.pending = tuple(pending)
        missing = ", ".join(str(request_id) for request_id in self.pending)
        super().__init__(f"timed out waiting for receipt(s): {missing}")


def _origin(head, seat):
    origin = {"head": head}
    if seat is not None:
        origin["seat"] = seat
    return origin


def _require_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not dispatch_protocol.REQUEST_ID_RE.fullmatch(
        request_id
    ):
        raise dispatch_protocol.DispatchError(
            "job id must be 32 lowercase hexadecimal characters"
        )
    return request_id


def submit_cancel(
    root,
    settings,
    *,
    target_request_id,
    origin_head,
    origin_seat=None,
    queue_timeout_seconds=600,
    now,
) -> str:
    """Submit the protocol's deliberately narrow cancellation control frame."""
    target_request_id = _require_request_id(target_request_id)
    request_id = dispatch_protocol.new_request_id()
    request = {
        "schema": dispatch_protocol.SCHEMA_VERSION,
        "release_id": _submission_release_id(root),
        "protocol_version": dispatch_protocol.PROTOCOL_VERSION,
        "protocol_min": dispatch_protocol.PROTOCOL_VERSION,
        "protocol_max": dispatch_protocol.PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": "cancel",
        "target_request_id": target_request_id,
        "origin": _origin(origin_head, origin_seat),
        "created_at": now,
        "expires_at": now + queue_timeout_seconds,
    }
    dispatch_protocol.validate_request(request, settings, now=now)
    dispatch_protocol.write_request(root, request)
    return request_id


def _secure_frame_or_none(path):
    try:
        return dispatch_protocol.read_request_secure(path)
    except dispatch_protocol.DispatchError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise


def job_status(root, request_id: str) -> dict:
    """Derive one durable lifecycle snapshot from fixed queue artifact names."""
    request_id = _require_request_id(request_id)
    receipt = dispatch_protocol.read_receipt(root, request_id)
    if receipt is not None:
        return {
            "request_id": request_id,
            "state": str(receipt.get("status", "terminal")),
            "terminal": True,
            "receipt": receipt,
        }

    paths = dispatch_protocol.queue_paths(root)
    claimed = paths["processing"] / f"{request_id}.claimed"
    lease_path = leases.lease_path(paths["processing"], request_id)
    frame = _secure_frame_or_none(claimed)
    lease = leases.read(lease_path)
    if (
        frame is not None
        and frame.get("request_id") == request_id
        and lease is not None
        and lease.get("request_id") == request_id
        and lease.get("request_hash") == dispatch_protocol.content_hash(frame)
    ):
        provider, seat = _actual_assignments(root).get(request_id, (None, None))
        state = "running" if leases.pid_alive(lease.get("job_pid")) else "recovering"
        result = {"request_id": request_id, "state": state, "terminal": False}
        if provider:
            result.update(provider=provider, seat=seat)
        if isinstance(lease.get("settings_reader"), dict):
            result["settings_reader"] = lease["settings_reader"]
        return result

    ready = paths["inbox"] / f"{request_id}.ready"
    frame = _secure_frame_or_none(ready)
    if frame is not None:
        if frame.get("request_id") != request_id:
            raise dispatch_protocol.DispatchError("queued job id does not match its frame")
        retry = leases.read(paths["processing"] / f".{request_id}.retry.json")
        expired = (
            isinstance(frame.get("expires_at"), (int, float))
            and float(frame["expires_at"]) <= time.time()
        )
        state = "expired" if expired else "waiting"
        if not expired and (
            retry is not None
            and retry.get("request_id") == request_id
            and retry.get("request_hash") == dispatch_protocol.content_hash(frame)
        ):
            state = "waiting_capacity"
        return {"request_id": request_id, "state": state, "terminal": False}
    return {"request_id": request_id, "state": "unknown", "terminal": False}


def _bounded_runner_log(paths, *, max_bytes=1024 * 1024) -> str:
    path = paths["logs"] / "runner.log"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise dispatch_protocol.DispatchError(f"cannot read runner log: {exc}") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise dispatch_protocol.DispatchError("runner log is not an owner-only regular file")
        start = max(0, info.st_size - max_bytes)
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, max_bytes)
    finally:
        os.close(fd)
    text = data.decode("utf-8", "replace")
    return text.split("\n", 1)[-1] if start else text


def tail_job(root, request_id: str, *, lines: int = 50, include_summary=False) -> dict:
    """Return at most ``lines`` exact-id runner log lines, without following."""
    request_id = _require_request_id(request_id)
    if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 1000:
        raise dispatch_protocol.DispatchError("lines must be between 1 and 1000")
    matcher = re.compile(rf"(?<![0-9a-f]){re.escape(request_id)}(?![0-9a-f])")
    matched = [
        line
        for line in _bounded_runner_log(dispatch_protocol.queue_paths(root)).splitlines()
        if matcher.search(line)
    ][-lines:]
    result = {"request_id": request_id, "lines": matched}
    if include_summary:
        receipt = dispatch_protocol.read_receipt(root, request_id)
        if receipt is not None:
            result["terminal"] = {
                "status": receipt.get("status"),
                "summary": receipt.get("summary") or receipt.get("reason"),
            }
    return result


def submit_task(
    root,
    settings,
    *,
    repo_id,
    prompt,
    origin_head,
    origin_seat=None,
    provider="auto",
    kind="implementation",
    task_shape=None,
    validation=None,
    size="auto",
    capability_tier="auto",
    effort=None,
    estimated_minutes=None,
    parallel_safe=False,
    scope_hints=(),
    risk_flags=(),
    override_reason=None,
    mcp=(),
    goal_id=None,
    task_id=None,
    coordination_id=None,
    read_only=False,
    spend_reserve_seat=None,
    from_current_user_request=False,
    queue_timeout_seconds=600,
    now,
) -> str:
    """Validate and atomically submit a natural-language worker task."""
    if spend_reserve_seat is None and from_current_user_request:
        raise dispatch_protocol.DispatchError(
            "--from-current-user-request requires --spend-reserve for ordinary delegation"
        )
    if spend_reserve_seat is not None and not from_current_user_request:
        raise dispatch_protocol.DispatchError(
            "--spend-reserve requires --from-current-user-request"
        )
    if spend_reserve_seat is not None and provider != "codex":
        raise dispatch_protocol.DispatchError(
            "--spend-reserve requires --provider codex"
        )
    request_id = dispatch_protocol.new_request_id()
    release_id = _submission_release_id(root)
    policy = resolve_task_policy(
        settings,
        kind,
        size=size,
        capability=capability_tier,
        task_shape=task_shape,
        validation=validation,
        effort=effort,
        estimated_minutes=estimated_minutes,
        parallel_safe=parallel_safe,
        scope_hints=scope_hints,
        risk_flags=risk_flags,
        override_reason=override_reason,
    )
    request = {
        "schema": dispatch_protocol.SCHEMA_VERSION,
        "release_id": release_id,
        "protocol_version": dispatch_protocol.PROTOCOL_VERSION,
        "protocol_min": dispatch_protocol.PROTOCOL_VERSION,
        "protocol_max": dispatch_protocol.PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": "task",
        "repo_id": repo_id,
        "prompt": prompt,
        "provider": provider,
        "kind": kind,
        "size": size,
        "capability_tier": capability_tier,
        "estimated_minutes": policy.estimated_minutes,
        "parallel_safe": parallel_safe,
        "scope_hints": list(scope_hints),
        "risk_flags": list(risk_flags),
        "read_only": read_only,
        "origin": _origin(origin_head, origin_seat),
        "created_at": now,
        "expires_at": now + queue_timeout_seconds,
    }
    if override_reason is not None:
        request["override_reason"] = override_reason
    if task_shape is not None:
        request["task_shape"] = task_shape
    if validation is not None:
        request["validation"] = validation
    if effort is not None:
        request["effort"] = effort
    if mcp:
        request["mcp"] = list(mcp)
    if spend_reserve_seat is not None:
        request["reserve_spend_authority"] = {
            "version": dispatch_protocol.RESERVE_SPEND_AUTHORITY_VERSION,
            "seat": spend_reserve_seat,
            "source": dispatch_protocol.RESERVE_SPEND_AUTHORITY_SOURCE,
        }
    for key, value in (
        ("goal_id", goal_id),
        ("task_id", task_id),
        ("coordination_id", coordination_id),
    ):
        if value is not None:
            request[key] = value
    dispatch_protocol.validate_request(request, settings, now=now)
    dispatch_protocol.write_request(root, request)
    return request_id


def _clock(time_fn):
    return time.monotonic if time_fn is None else time_fn


def _sleep_for_poll(poll_interval, remaining):
    time.sleep(max(0.0, min(poll_interval, remaining)))


def receipt_wait_backend() -> str:
    """Expose the production notification backend for diagnostics and tests."""
    return dispatch_events.production_backend()


def _wait_backend(time_fn, event_backend):
    # An injected clock is an explicit deterministic-test surface. Production never
    # selects this bounded fallback on supported platforms.
    return "poll" if event_backend is None and time_fn is not None else event_backend


def _emit_transition(callback, request_id, snapshot):
    if callback is not None:
        callback(request_id, snapshot)


def _poll_sleeper(sleeper, poll_interval):
    def sleep(remaining):
        sleeper(poll_interval, remaining)

    return sleep


def wait_for_receipt(
    root,
    request_id,
    *,
    wait_timeout,
    poll_interval=0.25,
    time_fn=None,
    event_backend=None,
    sleeper=_sleep_for_poll,
    on_transition=None,
) -> dict:
    """Wait until one receipt appears, using an armed OS queue notification."""
    clock = _clock(time_fn)
    deadline = clock() + wait_timeout
    backend = _wait_backend(time_fn, event_backend)
    with dispatch_events.QueueEventWaiter(
        root,
        backend=backend,
        sleeper=_poll_sleeper(sleeper, poll_interval),
        poll_interval=poll_interval,
    ) as waiter:
        last_state = None
        while True:
            # The notification source was armed before this recheck. A publication
            # racing with the read therefore either appears here or remains queued.
            receipt = dispatch_protocol.read_receipt(root, request_id)
            if receipt is not None:
                _emit_transition(
                    on_transition,
                    request_id,
                    {"state": str(receipt.get("status", "terminal")), "receipt": receipt},
                )
                return receipt
            status = job_status(root, request_id)
            if status["state"] != last_state:
                _emit_transition(on_transition, request_id, status)
                last_state = status["state"]
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for receipt {request_id}")
            waiter.wait(remaining)


def wait_for_receipts(
    root,
    request_ids,
    *,
    wait_timeout,
    poll_interval=0.25,
    time_fn=None,
    event_backend=None,
    sleeper=_sleep_for_poll,
    on_transition=None,
) -> dict:
    """Wait for all receipts using one armed watcher and one shared deadline."""
    clock = _clock(time_fn)
    deadline = clock() + wait_timeout
    pending = dict.fromkeys(request_ids)
    receipts = {}
    backend = _wait_backend(time_fn, event_backend)
    with dispatch_events.QueueEventWaiter(
        root,
        backend=backend,
        sleeper=_poll_sleeper(sleeper, poll_interval),
        poll_interval=poll_interval,
    ) as waiter:
        last_states = {}
        while pending:
            for request_id in tuple(pending):
                receipt = dispatch_protocol.read_receipt(root, request_id)
                if receipt is not None:
                    receipts[request_id] = receipt
                    del pending[request_id]
                    _emit_transition(
                        on_transition,
                        request_id,
                        {"state": str(receipt.get("status", "terminal")), "receipt": receipt},
                    )
                    continue
                status = job_status(root, request_id)
                if status["state"] != last_states.get(request_id):
                    _emit_transition(on_transition, request_id, status)
                    last_states[request_id] = status["state"]
            if not pending:
                return receipts
            remaining = deadline - clock()
            if remaining <= 0:
                raise ReceiptTimeout(receipts, pending)
            waiter.wait(remaining)
    return receipts


def _safe_label(prompt: object, limit: int = 80) -> str:
    first = str(prompt or "").splitlines()[0] if prompt else ""
    first = _ANSI_ESCAPE_RE.sub(" ", first)
    clean = "".join(
        " " if character in "\r\n\t" else character
        for character in first
        if not unicodedata.category(character).startswith("C")
    )
    clean = _KNOWN_SECRET_RE.sub("[redacted]", clean)
    clean = _SECRET_VALUE_RE.sub("[redacted]", clean)
    clean = _ENV_ASSIGNMENT_RE.sub("[env]", clean)
    clean = _URL_RE.sub("[url]", clean)
    clean = _WINDOWS_PATH_RE.sub("[path]", clean)
    clean = _POSIX_PATH_RE.sub("[path]", clean)
    clean = _OPAQUE_VALUE_RE.sub("[value]", clean)
    clean = " ".join(clean.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _actual_assignments(
    root, settings=None
) -> dict[str, tuple[str, str | None]]:
    """Join request-id labels to the read-only provider ledger snapshot."""
    try:
        from .seat_registry import registered_seat_ids
        from .state import load_settings, read_pool_snapshot

        snapshot = read_pool_snapshot(root)
    except (OSError, RuntimeError, ValueError):
        return {}
    effective = settings
    if (Path(root) / "settings.json").exists() or effective is None:
        effective, _warnings = load_settings(root)
    seat_ids = registered_seat_ids(effective, lifecycles=("active", "draining"))
    result = {}
    for seat_id in seat_ids:
        claim = snapshot.get("seats", {}).get(seat_id, {}).get("in_flight")
        if isinstance(claim, dict) and dispatch_protocol.REQUEST_ID_RE.fullmatch(
            str(claim.get("label", ""))
        ):
            result[str(claim["label"])] = ("codex", seat_id)
    claim = snapshot.get("providers", {}).get("claude", {}).get("in_flight")
    if isinstance(claim, dict) and dispatch_protocol.REQUEST_ID_RE.fullmatch(
        str(claim.get("label", ""))
    ):
        result[str(claim["label"])] = ("claude", None)
    return result


def _read_json_at(directory_fd, name):
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except OSError:
        return None
    try:
        return dispatch_protocol._read_request_descriptor(
            fd, uid=os.geteuid(), max_bytes=dispatch_protocol.MAX_FRAME_BYTES
        )
    except dispatch_protocol.DispatchError:
        return None
    finally:
        os.close(fd)


def _validated_live_frame(directory_fd, name, request_id, settings, *, now):
    try:
        frame = _read_json_at(directory_fd, name)
        if frame is None:
            return None
        if frame.get("request_id") != request_id or frame.get("operation") != "task":
            return None
        created = frame.get("created_at")
        validation_time = float(created) if isinstance(created, (int, float)) else now
        dispatch_protocol.validate_request(frame, settings, now=validation_time)
        return frame
    except (dispatch_protocol.DispatchError, OSError, TypeError, ValueError):
        return None


def live_jobs(root, settings, *, now=None) -> list[dict]:
    """Return sanitized live task snapshots from validated queue artifacts only."""
    current = time.time() if now is None else float(now)
    paths = dispatch_protocol.queue_paths(root)
    try:
        os.lstat(paths["base"])
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise dispatch_protocol.DispatchError(
            f"cannot inspect dispatch directory: {exc}"
        ) from exc
    # Reuse the event layer's no-follow, owner-only directory validation. The fds
    # remain open throughout enumeration so a directory swap cannot redirect reads.
    base_fd, directory_fds = dispatch_events._secure_directory_fds(root)
    try:
        assignments = _actual_assignments(root, settings)
        jobs = []
        for state, directory, suffix in (
            ("queued", paths["inbox"], ".ready"),
            ("running", paths["processing"], ".claimed"),
        ):
            directory_fd = directory_fds[0 if state == "queued" else 1]
            for name in os.listdir(directory_fd):
                if not name.endswith(suffix):
                    continue
                request_id = name.removesuffix(suffix)
                if not dispatch_protocol.REQUEST_ID_RE.fullmatch(request_id):
                    continue
                frame = _validated_live_frame(
                    directory_fd, name, request_id, settings, now=current
                )
                if frame is None:
                    continue
                request_hash = dispatch_protocol.content_hash(frame)
                retry = None
                lease = None
                if state == "queued":
                    retry = _read_json_at(
                        directory_fds[1], f".{request_id}.retry.json"
                    )
                    if not (
                        retry
                        and retry.get("request_id") == request_id
                        and retry.get("request_hash") == request_hash
                    ):
                        retry = None
                else:
                    lease = _read_json_at(
                        directory_fds[1], f"{request_id}.lease.json"
                    )
                    if not (
                        lease
                        and lease.get("request_id") == request_id
                        and lease.get("request_hash") == request_hash
                    ):
                        continue
                actual_provider, actual_seat = assignments.get(request_id, (None, None))
                created = float(frame["created_at"])
                started = lease.get("started_at") if lease else None
                expired = (
                    state == "queued"
                    and isinstance(frame.get("expires_at"), (int, float))
                    and float(frame["expires_at"]) <= current
                )
                lease_alive = lease is None or leases.pid_alive(lease.get("job_pid"))
                if expired:
                    effective_state = "expired"
                elif retry:
                    effective_state = "waiting_capacity"
                elif state == "running" and not lease_alive:
                    effective_state = "recovering"
                else:
                    effective_state = state
                since = float(started) if isinstance(started, (int, float)) else created
                origin = frame.get("origin") if isinstance(frame.get("origin"), dict) else {}
                jobs.append(
                    {
                        "request_id": request_id,
                        "state": effective_state,
                        "repo": frame.get("repo_id"),
                        "requested_provider": frame.get("provider", "auto"),
                        "provider": actual_provider,
                        "seat": actual_seat,
                        "kind": frame.get("kind", "implementation"),
                        "size": frame.get("size", "auto"),
                        "label": _safe_label(frame.get("prompt")),
                        "created_at": created,
                        "started_at": started,
                        "elapsed_seconds": max(0.0, current - since),
                        "retry": bool(retry),
                        "not_before": retry.get("not_before") if retry else None,
                        "origin": {
                            "head": origin.get("head"),
                            "seat": origin.get("seat"),
                        },
                    }
                )
        # Atomic claim publication can briefly leave both names visible to one
        # directory scan. Prefer the verified running record and expose one row.
        deduplicated = {}
        for job in jobs:
            prior = deduplicated.get(job["request_id"])
            if prior is None or job["state"] in {"running", "recovering"}:
                deduplicated[job["request_id"]] = job
        return sorted(
            deduplicated.values(),
            key=lambda item: (item["created_at"], item["request_id"]),
        )
    finally:
        for fd in directory_fds:
            os.close(fd)
        os.close(base_fd)


def _live_jobs_signature(jobs) -> str:
    stable = [
        {key: value for key, value in job.items() if key != "elapsed_seconds"}
        for job in jobs
    ]
    return json.dumps(stable, sort_keys=True, separators=(",", ":"))


def watch_live_jobs(
    root,
    settings,
    *,
    now_fn=time.time,
    event_backend=None,
    poll_interval=0.25,
    event_timeout=3600.0,
    waiter_factory=None,
):
    """Yield an initial live snapshot and then only observable state changes."""
    factory = waiter_factory or dispatch_events.QueueEventWaiter
    with factory(
        root,
        backend=event_backend,
        poll_interval=poll_interval,
    ) as waiter:
        previous = None
        while True:
            # Arm first, then read. An event racing with this snapshot stays queued
            # and makes the following wait return immediately.
            jobs = live_jobs(root, settings, now=now_fn())
            signature = _live_jobs_signature(jobs)
            if signature != previous:
                previous = signature
                yield jobs
            waiter.wait(event_timeout)


def background_counts(jobs) -> dict:
    return {
        "queued": sum(job.get("state") == "queued" for job in jobs),
        "running": sum(job.get("state") == "running" for job in jobs),
        "capacity": sum(job.get("state") == "waiting_capacity" for job in jobs),
        "recovering": sum(job.get("state") == "recovering" for job in jobs),
        "expired": sum(job.get("state") == "expired" for job in jobs),
    }
