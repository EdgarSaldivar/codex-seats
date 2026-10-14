"""launchd on-demand activation for the dispatch runner — NOT a daemon.

The runner starts in the user's ordinary launchd context so an opt-in seatbelted ``--safe``
head never has to nest the worker's profile. Full-access heads use the same path for durable
queueing and receipts. A user LaunchAgent with ``QueueDirectories`` starts the runner when
``dispatch/inbox`` or ``dispatch/processing`` is non-empty and lets it exit when both states
drain. There is no
``RunAtLoad``, no ``KeepAlive``, no listening socket, and no background poll — the only
activation signal is "queued or claimed work exists", which is persistent state (crash-safe:
``launchd.plist(5)`` calls ``WatchPaths`` race-prone, ``QueueDirectories`` is not).

``render_plist``/``doctor`` are pure and unit-tested. ``install``/``uninstall``/``status``
shell out to ``launchctl`` and perform a persistent, reversible system change; callers gate
those on explicit user intent.
"""

from __future__ import annotations

import os
import plistlib
import stat
import sys
from pathlib import Path
from typing import Any

from . import bounded_process, dispatch_protocol as dp, releases

LABEL = "io.codex-seats.dispatch"
# 0o077 -> owner-only new files; launchd wants the decimal form of the octal mask.
_UMASK_DECIMAL = 0o077
LAUNCHCTL_TIMEOUT_SECONDS = 15.0
_ABSENT_SERVICE_MARKERS = (
    "could not find service",
    "no such process",
    "service not found",
)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def runner_python(root: Path) -> Path:
    """Prefer the pinned venv interpreter so the agent can't drift onto a stray python."""
    venv = Path(root) / ".venv" / "bin" / "python"
    return venv if venv.exists() else Path(sys.executable)


def render_plist(root: str | Path) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    paths = dp.queue_paths(root)
    release_id = releases.current_release_id(root)
    release_lib = releases.release_lib_path(
        root, release_id, module="codex_seats.dispatch_runner"
    )
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(runner_python(root)),
            "-m",
            "codex_seats.dispatch_runner",
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            # launchd does not load the installing developer's shell PATH.
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONPATH": str(release_lib),
            "CODEX_SEATS_ROOT": str(root),
            "CODEX_SEATS_RELEASE_ID": release_id,
        },
        # Processing remains activation state so launchd restarts a coordinator
        # after a crash while job children or recoverable claims still exist.
        "QueueDirectories": [str(paths["inbox"]), str(paths["processing"])],
        "RunAtLoad": False,
        "ProcessType": "Standard",
        "Umask": _UMASK_DECIMAL,
        "StandardOutPath": str(paths["logs"] / "launchd.out"),
        "StandardErrorPath": str(paths["logs"] / "launchd.err"),
    }


def plist_bytes(root: str | Path) -> bytes:
    return plistlib.dumps(render_plist(root))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("write returned no progress")
        view = view[written:]


def write_plist(root: str | Path) -> Path:
    dp.ensure_queue(root)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) & 0o022
        or path.parent.resolve() != path.parent.absolute()
    ):
        raise RuntimeError(
            "dispatch plist parent must be an owner-controlled real directory"
        )
    data = plist_bytes(root)
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink dispatch plist: {path}")
    if path.exists():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise RuntimeError(
                f"dispatch plist is not an owner-controlled regular file: {path}"
            )
    tmp = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(12).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(tmp, flags, 0o644)
    try:
        try:
            os.fchmod(descriptor, 0o644)
            _write_all(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _launchctl(*args: str) -> bounded_process.ProcessResult:
    try:
        return bounded_process.run(
            ["launchctl", *args],
            timeout=LAUNCHCTL_TIMEOUT_SECONDS,
        )
    except bounded_process.ProcessTimeoutError as exc:
        raise RuntimeError(
            f"launchctl {' '.join(args)} timed out after {LAUNCHCTL_TIMEOUT_SECONDS:g}s"
        ) from exc
    except bounded_process.ProcessLaunchError as exc:
        raise RuntimeError(f"cannot run launchctl {' '.join(args)}: {exc}") from exc


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _output_text(value: bytes | str) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _loaded() -> bool:
    result = _launchctl("print", f"{_domain()}/{LABEL}")
    if result.returncode == 0:
        return True
    stderr = _output_text(result.stderr)
    stdout = _output_text(result.stdout)
    detail = f"{stderr}\n{stdout}".lower()
    if any(marker in detail for marker in _ABSENT_SERVICE_MARKERS):
        return False
    raise RuntimeError(
        "launchctl print failed without proving the service is absent: "
        + (stderr or stdout).strip()
    )


def bootout(path: str | Path, *, allow_absent: bool = False) -> None:
    """Unload the dispatch job and surface every unexpected launchctl failure."""
    if allow_absent:
        if not _loaded():
            return
    result = _launchctl("bootout", _domain(), str(path))
    if result.returncode != 0:
        raise RuntimeError(
            f"launchctl bootout failed: {_output_text(result.stderr).strip()}"
        )


def bootstrap(path: str | Path) -> None:
    """Load one already-written plist and surface launchctl failure."""
    result = _launchctl("bootstrap", _domain(), str(path))
    if result.returncode != 0:
        raise RuntimeError(
            f"launchctl bootstrap failed: {_output_text(result.stderr).strip()}"
        )


def install(root: str | Path) -> Path:
    """Write the plist and bootstrap it. Persistent, reversible system change."""
    path = write_plist(root)
    bootout(path, allow_absent=True)  # idempotent: clear any prior instance
    bootstrap(path)
    return path


def uninstall(root: str | Path) -> None:
    del root
    path = plist_path()
    bootout(path, allow_absent=True)
    path.unlink(missing_ok=True)
    if path.parent.exists():
        _fsync_directory(path.parent)


def status(root: str | Path) -> dict[str, Any]:
    del root
    path = plist_path()
    try:
        path.lstat()
    except FileNotFoundError:
        installed = False
    except OSError as exc:
        raise RuntimeError(f"cannot inspect dispatch plist: {exc}") from exc
    else:
        installed = True
    return {
        "label": LABEL,
        "plist": str(path),
        "installed": installed,
        "loaded": _loaded(),
    }


def doctor(root: str | Path) -> list[tuple[bool, str]]:
    """Verify the on-disk plist matches the safe activation contract. Pure/read-only."""
    root = Path(root).expanduser().resolve()
    checks: list[tuple[bool, str]] = []
    path = plist_path()
    if not path.exists() and not path.is_symlink():
        return [(False, f"plist not installed at {path}")]
    try:
        info = path.lstat()
    except OSError as exc:
        return [(False, f"plist cannot be inspected safely: {exc}")]
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        return [(False, "plist is not an owner-controlled single-link regular file")]
    try:
        plist = plistlib.loads(path.read_bytes())
    except Exception as exc:  # noqa: BLE001 - a corrupt plist is a single failed check
        return [(False, f"plist unreadable: {exc}")]

    queue_dirs = [
        str(dp.queue_paths(root)["inbox"]),
        str(dp.queue_paths(root)["processing"]),
    ]
    program = plist.get("ProgramArguments") or []
    checks.append((plist.get("Label") == LABEL, f"Label == {LABEL}"))
    checks.append(
        (
            plist.get("RunAtLoad") is False,
            "RunAtLoad is false (activation only via queue)",
        )
    )
    checks.append(("KeepAlive" not in plist, "no KeepAlive (not a daemon)"))
    checks.append(("Sockets" not in plist, "no network sockets"))
    checks.append(
        (
            plist.get("QueueDirectories") == queue_dirs,
            f"QueueDirectories == {queue_dirs}",
        )
    )
    checks.append((plist.get("Umask") == _UMASK_DECIMAL, "Umask is owner-only (0o077)"))
    checks.append(
        (
            len(program) >= 3 and program[1:3] == ["-m", "codex_seats.dispatch_runner"],
            "runs codex_seats.dispatch_runner",
        )
    )
    py_ok = (
        bool(program) and Path(program[0]).exists() and os.access(program[0], os.X_OK)
    )
    checks.append(
        (py_ok, f"interpreter exists+executable: {program[0] if program else '<none>'}")
    )
    checks.append(
        (
            plist.get("EnvironmentVariables", {}).get("CODEX_SEATS_ROOT") == str(root),
            "CODEX_SEATS_ROOT pinned to this root",
        )
    )
    try:
        release_id = releases.current_release_id(root)
        expected_lib = str(
            releases.release_lib_path(
                root, release_id, module="codex_seats.dispatch_runner"
            )
        )
    except releases.ReleaseError:
        release_id = ""
        expected_lib = ""
    environment = plist.get("EnvironmentVariables", {})
    checks.append(
        (
            environment.get("CODEX_SEATS_RELEASE_ID") == release_id
            and environment.get("PYTHONPATH") == expected_lib,
            "runner import path is pinned to the installed immutable release",
        )
    )
    return checks
