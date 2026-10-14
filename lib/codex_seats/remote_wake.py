"""One detached OS receipt wait with at-most-once capacity and terminal wakes.

No daemon/thread creation or provider access lives here. Durable markers deliberately
prefer a missed wake over duplicate delivery after a crash or ambiguous CLI result.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass

from . import bounded_process, dispatch_client
from .controller_context import current_controller
from .dispatch_protocol import DispatchError
from .worker_environment import minimal_worker_env

REMOTE = "unix://"
MAX_JOBS = 32
MAX_MESSAGE = 4096
MAX_DEADLINE = 7 * 24 * 60 * 60
QUEUE_TIMEOUT = 30
INITIAL_DISCOVERY_GRACE = 2.0
INITIAL_DISCOVERY_RETRY_DELAY = 0.1
THREAD_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
STATUSES = frozenset({
    "completed", "failed", "blocked_needs_controller", "error", "cancelled",
    "expired", "rejected", "timeout", "unknown", "capacity", "disabled",
})


@dataclass(frozen=True)
class RemoteOwner:
    thread: str
    home: str
    binary: str


class QueueLaunchError(RuntimeError):
    """Only a failed Popen is known not to have attempted delivery."""


def _owned(path: Path, *, directory=False, private=False, socket=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISSOCK if socket else stat.S_ISREG
    if (
        not kind(info.st_mode) or info.st_uid != os.geteuid()
        or (private and info.st_mode & 0o077)
        or (directory and info.st_mode & 0o022)
    ):
        raise DispatchError("Remote wake requires an owned, safe local path")
    return info


def _endpoint(home: str) -> None:
    if (
        not isinstance(home, str) or not home or len(home) > 4096
        or not Path(home).is_absolute() or any(ord(c) < 32 for c in home)
    ):
        raise DispatchError("Remote wake requires an absolute CODEX_HOME")
    directory = Path(home) / "app-server-control"
    _owned(Path(home), directory=True)
    _owned(directory, directory=True)
    _owned(directory / "app-server-control.sock", socket=True)


def _default_codex_home() -> Path:
    """Return the installed CLI's account-owned default without trusting ``HOME``."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".codex"


def _remote_controller():
    context = current_controller()
    if (
        not context.is_controller or not context.remote or context.head_kind != "codex"
        or os.environ.get("CODEX_SEATS_HEAD", "codex") not in {"codex", ""}
        or not THREAD_RE.fullmatch(context.thread_id or "")
    ):
        raise DispatchError("--wake-head requires the owning Remote Codex controller")
    return context


def prepare(wait_timeout: float, *, remote: str = REMOTE) -> RemoteOwner:
    """Reject incompatible callers before submission/arming; never probe a provider."""
    context = _remote_controller()
    if remote != REMOTE:
        raise DispatchError("Remote wake supports only the owned unix:// control socket")
    if not math.isfinite(wait_timeout) or not 0 <= wait_timeout <= MAX_DEADLINE:
        raise DispatchError("Remote wake deadline must be between 0 and 604800 seconds")
    if dispatch_client.receipt_wait_backend() not in {"kqueue", "inotify"}:
        raise DispatchError("Remote wake requires kqueue or inotify")
    # A seat launcher supplies CODEX_HOME. ChatGPT Remote supplies CODEX_SESSION_ID
    # instead and uses the installed CLI's account-owned default home.
    home = os.environ.get("CODEX_HOME") or os.fspath(_default_codex_home())
    _endpoint(home)
    from .workers import resolve_executable

    return RemoteOwner(context.thread_id, home, resolve_executable("codex", None))


def _jobs(job_ids) -> list[str]:
    if not 1 <= len(job_ids) <= MAX_JOBS:
        raise DispatchError("Remote wake requires 1 to 32 exact job IDs")
    return sorted({dispatch_client._require_request_id(job) for job in job_ids})


def _write_once(directory: Path, name: str, value: dict) -> bool:
    """Exclusive durable marker. A partial marker after a crash still suppresses retry."""
    _owned(directory, directory=True, private=True)
    try:
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(directory)
    return True


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _event(directory: Path, event: str, **fields) -> bool:
    # Fixed event names and structured allowlisted fields only; no exception text.
    return _write_once(directory, event + ".json", {
        "event": event, "at": time.time(), **fields,
    })


def _read(directory: Path) -> dict:
    _owned(directory, directory=True, private=True)
    fd = os.open(directory / "owner.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise DispatchError("unsafe Remote wake record")
        return json.load(stream)


def arm(root, job_ids, *, wait_timeout) -> dict:
    """Persist the exact boundary and detach promptly. No receipt wait in this process."""
    owner = prepare(wait_timeout)
    jobs = _jobs(job_ids)
    root = Path(root).resolve()
    identity = {"root": str(root), "thread": owner.thread, "home": owner.home, "jobs": jobs}
    wake_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    base = root / "remote-wakes"
    base.mkdir(mode=0o700, exist_ok=True)
    _owned(base, directory=True, private=True)
    # Persist the base link too: losing it after queue acceptance would lose all
    # deduplication markers despite having fsynced files beneath it.
    _sync_directory(root)
    directory = base / wake_id
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        _event(directory, "duplicate_suppressed")
        return {"wake_id": wake_id, "status": "duplicate_suppressed", "job_ids": jobs}
    # Persist the directory before allowing any queue operation in the child.
    _sync_directory(base)
    _write_once(directory, "owner.json", {
        **identity, "binary": owner.binary, "deadline": time.time() + wait_timeout,
    })
    _event(directory, "armed", job_ids=jobs)
    env = minimal_worker_env()
    env.update(CODEX_HOME=owner.home, CODEX_THREAD_ID=owner.thread, CODEX_SEATS_HEAD="codex")
    # -I avoids cwd/PYTHONPATH injection. Pin this installed/source module snapshot;
    # no shell, inherited pipes, credential environment, or provider worker launch.
    bootstrap = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r}); "
        "from codex_seats.remote_wake import main; main()"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-I", "-c", bootstrap, str(directory)],
            cwd="/", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            shell=False,
        )
    except OSError:
        _event(directory, "launch_failed", error="waiter_launch_failed")
        raise DispatchError("Remote wake launch failed; retained record; do not resubmit work") from None
    return {"wake_id": wake_id, "status": "armed", "job_ids": jobs}


def _statuses(jobs, receipts) -> dict[str, str]:
    if not isinstance(receipts, dict):
        receipts = {}
    result = {}
    for job in jobs:
        receipt = receipts.get(job)
        value = receipt.get("status") if isinstance(receipt, dict) else None
        result[job] = value if isinstance(value, str) and value in STATUSES else "unknown"
    return result


def _message(jobs, statuses, outcome) -> str:
    message = (
        f"Codex Seats completion wake: {outcome}. "
        + "; ".join(f"{job}={statuses[job]}" for job in jobs)
        + ". Read the exact durable receipts once without waiting: codex-seat await "
        + " ".join(jobs) + " --wait-timeout 0 --json. Review receipts, diffs, tests, "
        "and warnings before continuing. A deadline or watcher failure does not cancel "
        "or restart jobs. Do not poll or resubmit work."
    )
    if len(message.encode()) > MAX_MESSAGE:
        raise DispatchError("Remote wake message exceeds the fixed bound")
    return message


def _capacity_message(job: str) -> str:
    message = (
        f"Codex Seats capacity wake: {job}=waiting_capacity. Inspect the exact durable "
        f"state once: codex-seat job status {job} --json. The original job remains "
        "queued and was not cancelled or restarted. Decide whether to keep waiting, or "
        "cancel it through the supported command before reassigning or taking over the "
        "work. Do not duplicate a live job."
    )
    if len(message.encode()) > MAX_MESSAGE:
        raise DispatchError("Remote capacity wake message exceeds the fixed bound")
    return message


def _queue(owner: RemoteOwner, message: str) -> int:
    env = minimal_worker_env()
    env["CODEX_HOME"] = owner.home
    # Discard both streams: CLI diagnostics can include arbitrary remote text.
    # Timeout/nonzero may occur after acceptance and are always ambiguous.
    argv = [owner.binary, "queue", "--thread", owner.thread, "--message", message,
            "--remote", REMOTE]
    try:
        process = subprocess.Popen(
            argv, cwd="/", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, close_fds=True, start_new_session=True,
        )
    except OSError:
        raise QueueLaunchError("queue_launch_failed") from None
    try:
        return process.wait(timeout=QUEUE_TIMEOUT)
    except BaseException:
        bounded_process.terminate_and_reap(process, process.pid, argv)
        raise


def _notify_capacity(directory: Path, owner: RemoteOwner, job: str) -> None:
    """Queue one advisory capacity wake, without ending or mutating the job wait."""

    def record(event, **fields):
        try:
            return _event(directory, event, **fields)
        except Exception:
            return False

    # This group-level marker intentionally coalesces simultaneous capacity
    # transitions. The queued message identifies the first observed job and tells
    # the head to inspect durable state before acting.
    if not record("capacity_waiting", job_id=job):
        return
    try:
        _endpoint(owner.home)
    except (DispatchError, OSError):
        record("capacity_queue_failed", error="endpoint_unavailable")
        return
    # Write-ahead claim: a crash or ambiguous CLI result must never retry delivery.
    # If persistence fails, do not call the queue client and do not disturb the wait.
    if not record("capacity_queue_attempt", job_id=job):
        record("capacity_queue_failed", error="notification_unavailable")
        return
    try:
        result = _queue(owner, _capacity_message(job))
    except QueueLaunchError:
        record("capacity_queue_failed", error="queue_launch_failed")
    except Exception:
        record("capacity_queue_uncertain", error="queue_outcome_unknown")
    else:
        if result == 0:
            record("capacity_queue_success")
        else:
            record(
                "capacity_queue_uncertain",
                error="queue_nonzero",
                exit_code=result,
            )


def _wait_with_initial_discovery_grace(root, jobs, *, wait_timeout, event_backend,
                                       on_transition):
    """Retry one immediate all-unknown watcher setup race within a fixed grace."""
    started = time.monotonic()
    kwargs = {
        "wait_timeout": wait_timeout,
        "event_backend": event_backend,
        "on_transition": on_transition,
    }
    try:
        return dispatch_client.wait_for_receipts(root, jobs, **kwargs)
    except (DispatchError, OSError):
        elapsed = time.monotonic() - started
        remaining = wait_timeout - elapsed
        if elapsed > INITIAL_DISCOVERY_GRACE or remaining <= 0:
            raise
        try:
            undiscovered = all(
                dispatch_client.job_status(root, job).get("state") == "unknown"
                for job in jobs
            )
        except (DispatchError, OSError):
            undiscovered = False
        if not undiscovered:
            raise
        delay = min(INITIAL_DISCOVERY_RETRY_DELAY, remaining)
        time.sleep(delay)
        kwargs["wait_timeout"] = max(0.0, remaining - delay)
        return dispatch_client.wait_for_receipts(root, jobs, **kwargs)


def run_waiter(directory: Path) -> None:
    """Internal detached entry point: one wait, one capacity and one terminal wake."""
    context = _remote_controller()
    data = _read(directory)
    # Revalidate authority in the child, before claiming or reading any receipts.
    if (context.thread_id != data["thread"]
            or os.environ.get("CODEX_HOME") != data["home"]):
        raise DispatchError("Remote wake owner mismatch")
    jobs = _jobs(data["jobs"])
    if not THREAD_RE.fullmatch(data["thread"]):
        raise DispatchError("invalid Remote wake thread")
    if not _event(directory, "wait_started"):
        _event(directory, "duplicate_suppressed")
        return
    owner = RemoteOwner(data["thread"], data["home"], data["binary"])

    def on_transition(job, snapshot):
        if isinstance(snapshot, dict) and snapshot.get("state") == "waiting_capacity":
            _notify_capacity(directory, owner, job)

    outcome, receipts = "terminal_set", {}
    try:
        backend = dispatch_client.receipt_wait_backend()
        if backend not in {"kqueue", "inotify"}:
            raise DispatchError("unsupported watcher")
        receipts = _wait_with_initial_discovery_grace(
            data["root"],
            jobs,
            wait_timeout=max(0, data["deadline"] - time.time()),
            event_backend=backend,
            on_transition=on_transition,
        )
        if set(receipts) != set(jobs) or any(
            not isinstance(receipts[job], dict)
            or receipts[job].get("request_id") != job
            or not isinstance(receipts[job].get("status"), str)
            or receipts[job]["status"] not in STATUSES
            for job in jobs
        ):
            raise DispatchError("incomplete receipt set")
    except dispatch_client.ReceiptTimeout as exc:
        outcome, receipts = "deadline", exc.receipts
    except Exception:
        outcome = "watcher_failed"
    statuses = _statuses(jobs, receipts)
    _event(directory, outcome, statuses=statuses)
    try:
        _endpoint(owner.home)
    except (DispatchError, OSError):
        _event(directory, "queue_failed", error="endpoint_unavailable")
        return
    # Write-ahead marker MUST be durable before spawning the CLI. If killed after
    # this point, delivery is unknown. Neither a repeated arm nor entry can retry.
    if not _event(directory, "queue_attempt"):
        _event(directory, "duplicate_suppressed")
        return
    try:
        result = _queue(owner, _message(jobs, statuses, outcome))
    except QueueLaunchError:
        _event(directory, "queue_failed", error="queue_launch_failed")
    except Exception:
        _event(directory, "queue_uncertain", error="queue_outcome_unknown")
    else:
        if result == 0:
            _event(directory, "queue_success")
        else:
            _event(directory, "queue_uncertain", error="queue_nonzero", exit_code=result)


def main() -> None:
    _remote_controller()  # Unauthorized callers cannot even append failure evidence.
    directory = Path(sys.argv[1])
    try:
        run_waiter(directory)
    except Exception:
        # Persistence/identity failure: never deliver without a durable claim.
        # No raw exception text or logs; the existing markers remain authoritative.
        try:
            _event(directory, "waiter_failed", error="waiter_internal_failure")
        except Exception:
            pass
        raise SystemExit(2) from None
