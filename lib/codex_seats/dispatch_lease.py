"""Owner-only atomic leases for claimed dispatch frames.

Leases are coordinator policy records, not worker output.  Callers construct them only
from validated request metadata and registered repository paths.  Worker payloads and
receipts are never merged into a lease.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from .dispatch_admission import AdmissionFrame
from .dispatch_protocol import content_hash


def lease_path(processing: str | Path, request_id: str) -> Path:
    return Path(processing) / f"{request_id}.lease.json"


def build_lease(
    frame: dict[str, Any],
    admission: AdmissionFrame,
    *,
    coordinator_pid: int,
    job_pid: int,
    process_group_id: int | None = None,
    session_id: int | None = None,
    now: float,
    settings_reader: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the fixed policy record; intentionally ignore all unknown frame fields."""
    lease: dict[str, Any] = {
        "request_id": admission.request_id,
        "coordinator_pid": int(coordinator_pid),
        "job_pid": int(job_pid),
        "process_group_id": int(process_group_id or job_pid),
        "session_id": int(session_id or job_pid),
        "request_hash": content_hash(frame),
        "repo": admission.repo,
        "scopes": list(admission.scopes),
        "serial": admission.serial,
        "parallel_safe": admission.parallel_safe,
        "identities": [
            {"kind": kind, "id": identity}
            for kind, identity in sorted(admission.identities)
        ],
        "origin": {"head": admission.origin_head, "seat": admission.origin_seat},
        "started_at": now,
        "heartbeat_at": now,
        "schema": frame.get("schema"),
    }
    # A later pinned-release executor consumes the original claimed frame.  The lease
    # merely records its already-validated release identity; it never selects a release.
    for key in (
        "release_id",
        "protocol_version",
        "protocol_min",
        "protocol_max",
    ):
        if key in frame:
            lease[key] = frame[key]
    if settings_reader is not None:
        # Additive compatibility metadata; old readers continue to ignore it.
        lease["settings_reader"] = json.loads(json.dumps(settings_reader))
    return lease


def write_atomic(path: str | Path, lease: dict[str, Any]) -> Path:
    """Atomically replace an owner-only JSON lease and fsync file + directory."""
    final = Path(path)
    data = json.dumps(lease, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tmp = final.with_name(
        f".{final.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, final)
    dir_fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final


def read(path: str | Path, *, uid: int | None = None) -> dict[str, Any] | None:
    """Securely read a lease; malformed, symlinked, or permissive files are untrusted."""
    if uid is None:
        uid = os.geteuid()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or info.st_size > 64 * 1024
        ):
            return None
        data = os.read(fd, 64 * 1024 + 1)
    finally:
        os.close(fd)
    try:
        lease = json.loads(data)
    except (ValueError, json.JSONDecodeError):
        return None
    return lease if isinstance(lease, dict) else None


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def heartbeat(
    path: str | Path,
    *,
    request_id: str,
    job_pid: int,
    now: float,
) -> bool:
    """Update only the heartbeat after matching the immutable lease ownership fields."""
    current = read(path)
    if (
        current is None
        or current.get("request_id") != request_id
        or current.get("job_pid") != job_pid
    ):
        return False
    updated = dict(current)
    updated["heartbeat_at"] = now
    if isinstance(updated.get("settings_reader"), dict):
        updated["settings_reader"] = dict(updated["settings_reader"])
        updated["settings_reader"]["heartbeat_at"] = now
    write_atomic(path, updated)
    return True


def live(path: str | Path, *, alive_fn: Callable[[Any], bool] = pid_alive) -> bool:
    lease = read(path)
    return lease is not None and alive_fn(lease.get("job_pid"))


def admission_from_lease(lease: dict[str, Any]) -> AdmissionFrame | None:
    """Recover admission metadata without consulting an untrusted worker payload."""
    try:
        identities = frozenset(
            (item["kind"], item["id"])
            for item in lease.get("identities", [])
            if isinstance(item, dict)
            and isinstance(item.get("kind"), str)
            and isinstance(item.get("id"), str)
        )
        scopes = tuple(lease.get("scopes") or ())
        if not all(isinstance(scope, str) for scope in scopes):
            return None
        return AdmissionFrame(
            request_id=lease["request_id"],
            repo=lease["repo"],
            scopes=scopes,
            parallel_safe=lease.get("parallel_safe") is True,
            serial=lease.get("serial") is not False,
            identities=identities,
            origin_head=(
                lease.get("origin", {}).get("head")
                if isinstance(lease.get("origin"), dict)
                else None
            ),
            origin_seat=(
                lease.get("origin", {}).get("seat")
                if isinstance(lease.get("origin"), dict)
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None
