"""Transactional immutable release upgrades.

The immutable candidate may remain after a failed transaction.  Everything else
is snapshotted in a small durable journal and restored with compare-and-restore
semantics.  Service operations are deliberately narrow and injectable so tests
never need to invoke launchctl.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import math
import os
import plistlib
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bounded_process, launchd_dispatch, releases

JOURNAL_SCHEMA = 1
JOURNAL_NAME = "upgrade-transaction.json"
LOCK_NAME = "upgrade.lock"
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024
LOCK_TIMEOUT_SECONDS = 10.0
SMOKE_TIMEOUT_SECONDS = 30.0
_MUTABLE_NAMES = ("current", "previous", "codex-seat", "worker-hook", "dispatch_plist")
_PHASES = frozenset(
    {
        "snapshotted",
        "activating-previous",
        "activating-current",
        "installing-shim-1",
        "installing-shim-2",
        "writing-plist",
        "booting-out-dispatch",
        "bootstrapping-dispatch",
        "verifying",
        "rolling-back",
        "rolled-back",
    }
)
_SERVICE_MAY_HAVE_CHANGED = frozenset(
    {
        "booting-out-dispatch",
        "bootstrapping-dispatch",
        "verifying",
        "rolling-back",
        "rolled-back",
    }
)


class UpgradeError(RuntimeError):
    """Upgrade validation, execution, rollback, or recovery failed."""

    def __init__(self, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class DispatchOperations:
    """The only host-service boundary used by an upgrade."""

    plist_path: Callable[[], Path]
    render_bytes: Callable[[str | Path], bytes]
    status: Callable[[str | Path], dict[str, Any]]
    bootout: Callable[[Path], None]
    bootstrap: Callable[[Path], None]
    doctor: Callable[[str | Path], list[tuple[bool, str]]]
    validate_snapshot: Callable[[Path, bytes], None] | None = None


def _validate_dispatch_snapshot(root: Path, payload: bytes) -> None:
    """Permit only the known dispatch contract in a durable snapshot."""
    try:
        existing = plistlib.loads(payload)
    except Exception as exc:
        raise UpgradeError(f"existing dispatch plist is invalid: {exc}") from exc
    expected = launchd_dispatch.render_plist(root)
    if not isinstance(existing, dict) or set(existing) != set(expected):
        raise UpgradeError("existing dispatch plist has missing or unknown fields")
    for key, value in expected.items():
        if key != "EnvironmentVariables" and existing.get(key) != value:
            raise UpgradeError(f"existing dispatch plist has an unsafe {key} value")
    environment = existing.get("EnvironmentVariables")
    safe_keys = {"PATH", "PYTHONPATH", "CODEX_SEATS_ROOT", "CODEX_SEATS_RELEASE_ID"}
    if not isinstance(environment, dict) or set(environment) != safe_keys:
        raise UpgradeError("existing dispatch plist has an unsafe environment map")
    release_id = environment.get("CODEX_SEATS_RELEASE_ID")
    try:
        releases.validate_release_id(release_id)
        expected_lib = releases.release_lib_path(
            root, release_id, module="codex_seats.dispatch_runner"
        )
    except releases.ReleaseError as exc:
        raise UpgradeError(f"existing dispatch plist release is unsafe: {exc}") from exc
    if environment.get("CODEX_SEATS_ROOT") != str(root.resolve()):
        raise UpgradeError("existing dispatch plist root is not this runtime")
    if not isinstance(environment.get("PATH"), str) or "\0" in environment["PATH"]:
        raise UpgradeError("existing dispatch plist PATH is malformed")
    if environment.get("PYTHONPATH") != str(expected_lib):
        raise UpgradeError(
            "existing dispatch plist import path is not a verified release"
        )


def default_dispatch_operations() -> DispatchOperations:
    return DispatchOperations(
        plist_path=launchd_dispatch.plist_path,
        render_bytes=launchd_dispatch.plist_bytes,
        status=launchd_dispatch.status,
        bootout=lambda path: launchd_dispatch.bootout(path, allow_absent=True),
        bootstrap=launchd_dispatch.bootstrap,
        doctor=launchd_dispatch.doctor,
        validate_snapshot=_validate_dispatch_snapshot,
    )


@dataclass
class _Lock:
    path: Path
    timeout: float
    descriptor: int | None = None

    def __enter__(self) -> _Lock:  # noqa: PYI034 - Python 3.9 has no typing.Self
        if (
            not isinstance(self.timeout, (int, float))
            or isinstance(self.timeout, bool)
            or not math.isfinite(self.timeout)
            or not 0 <= self.timeout <= 60
        ):
            raise UpgradeError("upgrade lock timeout must be between 0 and 60 seconds")
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            descriptor = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(self.path, flags)
            except OSError as exc:
                raise UpgradeError(f"cannot open upgrade lock: {exc}") from exc
        except OSError as exc:
            raise UpgradeError(f"cannot open upgrade lock: {exc}") from exc
        if created:
            try:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            except OSError as exc:
                os.close(descriptor)
                raise UpgradeError(f"cannot initialize upgrade lock: {exc}") from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise UpgradeError("upgrade lock must be an owner-only regular file")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise UpgradeError(
                            f"cannot lock upgrade transaction: {exc}"
                        ) from exc
                    if time.monotonic() >= deadline:
                        raise UpgradeError(
                            f"upgrade lock is busy after {self.timeout:g} seconds"
                        ) from exc
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            self.descriptor = descriptor
            _fsync_directory(self.path.parent)
            return self
        except Exception:
            os.close(descriptor)
            raise

    def __exit__(self, *_args: object) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


def _managed_refusal(action: str) -> None:
    if os.environ.get("CODEX_SEATS_MANAGED_WORKER") == "1":
        raise UpgradeError(f"managed workers may not {action} release upgrades")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise UpgradeError(f"directory is not owner-controlled: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _owner_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise UpgradeError(f"{label} is unavailable: {exc}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
        or path.resolve() != path.absolute()
    ):
        raise UpgradeError(f"{label} must be an owner-controlled real directory")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("write returned no progress")
        view = view[written:]


def _canonical_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _atomic_file(
    path: Path, payload: bytes, mode: int, *, exclusive: bool = False
) -> None:
    _owner_directory(path.parent, "artifact parent")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(12).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        try:
            os.fchmod(descriptor, mode)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _journal_path(root: Path) -> Path:
    return root / JOURNAL_NAME


def _write_journal(
    root: Path, journal: dict[str, Any], *, create: bool = False
) -> None:
    path = _journal_path(root)
    payload = _canonical_bytes(journal)
    if len(payload) > MAX_JOURNAL_BYTES:
        raise UpgradeError("upgrade journal exceeds the size limit")
    if create and (path.exists() or path.is_symlink()):
        raise UpgradeError(
            f"incomplete upgrade journal already exists at {path}; run release upgrade-status "
            "and release upgrade-recover"
        )
    if path.is_symlink():
        raise UpgradeError("upgrade journal must not be a symlink")
    if not create:
        try:
            info = path.lstat()
        except OSError as exc:
            raise UpgradeError(
                f"cannot inspect upgrade journal before update: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise UpgradeError("refusing to replace an unsafe upgrade journal")
    try:
        _atomic_file(path, payload, 0o600, exclusive=create)
    except FileExistsError as exc:
        raise UpgradeError(
            "upgrade journal appeared concurrently; refusing to overwrite it"
        ) from exc


def _read_journal(root: Path) -> dict[str, Any] | None:
    path = _journal_path(root)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UpgradeError(f"cannot open upgrade journal: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > MAX_JOURNAL_BYTES
        ):
            raise UpgradeError(
                "upgrade journal must be a single-link owner-only regular file within size limit"
            )
        payload = b""
        while len(payload) <= MAX_JOURNAL_BYTES:
            chunk = os.read(
                descriptor, min(65536, MAX_JOURNAL_BYTES + 1 - len(payload))
            )
            if not chunk:
                break
            payload += chunk
        if len(payload) > MAX_JOURNAL_BYTES:
            raise UpgradeError("upgrade journal exceeds the size limit")
    finally:
        os.close(descriptor)
    try:
        journal = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"upgrade journal is invalid JSON: {exc}") from exc
    if not isinstance(journal, dict) or journal.get("schema") != JOURNAL_SCHEMA:
        raise UpgradeError("upgrade journal has an unsupported schema")
    if _canonical_bytes(journal) != payload:
        raise UpgradeError("upgrade journal is not canonical")
    if set(journal) != {
        "schema",
        "transaction_id",
        "candidate",
        "phase",
        "plist_path",
        "dispatch",
        "original",
        "owned",
    }:
        raise UpgradeError("upgrade journal has missing or unknown fields")
    if (
        not isinstance(journal.get("transaction_id"), str)
        or len(journal["transaction_id"]) != 32
    ):
        raise UpgradeError("upgrade journal transaction id is malformed")
    try:
        int(journal["transaction_id"], 16)
        releases.validate_release_id(journal.get("candidate"))
    except (TypeError, ValueError, releases.ReleaseError) as exc:
        raise UpgradeError("upgrade journal identity is malformed") from exc
    if journal.get("phase") not in _PHASES:
        raise UpgradeError("upgrade journal phase is malformed")
    if (
        not isinstance(journal.get("plist_path"), str)
        or not journal["plist_path"]
        or len(journal["plist_path"]) > 4096
        or "\0" in journal["plist_path"]
    ):
        raise UpgradeError("upgrade journal plist path is malformed")
    dispatch = journal.get("dispatch")
    if (
        not isinstance(dispatch, dict)
        or set(dispatch) != {"installed", "loaded"}
        or not isinstance(dispatch["installed"], bool)
        or not isinstance(dispatch["loaded"], bool)
        or (dispatch["loaded"] and not dispatch["installed"])
    ):
        raise UpgradeError("upgrade journal dispatch snapshot is malformed")
    if set(journal.get("original", {})) != set(_MUTABLE_NAMES):
        raise UpgradeError("upgrade journal snapshot is incomplete")
    if set(journal.get("owned", {})) != set(_MUTABLE_NAMES):
        raise UpgradeError("upgrade journal ownership map is incomplete")
    for collection in (journal["original"], journal["owned"]):
        for name in _MUTABLE_NAMES:
            _validate_snapshot_state(
                collection[name], pointer=name in ("current", "previous")
            )
    return journal


def _remove_journal(root: Path, expected: dict[str, Any]) -> None:
    path = _journal_path(root)
    if _read_journal(root) != expected:
        raise UpgradeError(
            "upgrade journal changed concurrently; refusing to remove it"
        )
    path.unlink()
    _fsync_directory(root)


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _absent() -> dict[str, Any]:
    return {"present": False}


def _validate_snapshot_state(state: Any, *, pointer: bool) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("present"), bool):
        raise UpgradeError("journal artifact snapshot is malformed")
    if not state["present"]:
        if state != _absent():
            raise UpgradeError("journal absent artifact snapshot is malformed")
        return
    if pointer:
        if (
            set(state) != {"present", "type", "target"}
            or state.get("type") != "symlink"
        ):
            raise UpgradeError("journal pointer snapshot is malformed")
        target = state.get("target")
        if not isinstance(target, str):
            raise UpgradeError("journal pointer target is malformed")
        parts = target.split("/")
        if len(parts) != 2 or parts[0] != "releases":
            raise UpgradeError("journal pointer target is unsafe")
        try:
            releases.validate_release_id(parts[1])
        except releases.ReleaseError as exc:
            raise UpgradeError("journal pointer target is unsafe") from exc
        return
    if set(state) != {"present", "type", "bytes", "sha256", "mode"}:
        raise UpgradeError("journal file snapshot is malformed")
    _decode_file(state)


def _snapshot(path: Path, *, pointer: bool = False) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _absent()
    except OSError as exc:
        raise UpgradeError(
            f"cannot inspect transaction artifact {path}: {exc}"
        ) from exc
    if info.st_uid != os.getuid() or info.st_nlink != 1:
        raise UpgradeError(
            f"transaction artifact is not exclusively owner-controlled: {path}"
        )
    if pointer:
        if not stat.S_ISLNK(info.st_mode):
            raise UpgradeError(f"release pointer must be a symlink: {path}")
        target = os.readlink(path)
        # Reuse the strict release-pointer parser rather than recording arbitrary links.
        releases._pointer_hash(path.parent, path.name)
        return {"present": True, "type": "symlink", "target": target}
    if not stat.S_ISREG(info.st_mode):
        raise UpgradeError(f"transaction artifact must be a regular file: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & ~0o777 or mode & 0o022:
        raise UpgradeError(
            f"transaction artifact has unsafe writable or special mode bits: {path}"
        )
    if info.st_size > MAX_ARTIFACT_BYTES:
        raise UpgradeError(f"transaction artifact exceeds size limit: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise UpgradeError(f"transaction artifact changed while inspected: {path}")
        payload = b""
        while len(payload) <= MAX_ARTIFACT_BYTES:
            chunk = os.read(
                descriptor, min(65536, MAX_ARTIFACT_BYTES + 1 - len(payload))
            )
            if not chunk:
                break
            payload += chunk
    finally:
        os.close(descriptor)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise UpgradeError(f"transaction artifact exceeds size limit: {path}")
    return {
        "present": True,
        "type": "file",
        "bytes": base64.b64encode(payload).decode("ascii"),
        "sha256": _hash(payload),
        "mode": mode,
    }


def _file_state(payload: bytes, mode: int) -> dict[str, Any]:
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise UpgradeError("generated transaction artifact exceeds size limit")
    return {
        "present": True,
        "type": "file",
        "bytes": base64.b64encode(payload).decode("ascii"),
        "sha256": _hash(payload),
        "mode": mode,
    }


def _pointer_state(release_id: str) -> dict[str, Any]:
    releases.validate_release_id(release_id)
    return {"present": True, "type": "symlink", "target": f"releases/{release_id}"}


def _decode_file(state: dict[str, Any]) -> bytes:
    if state.get("type") != "file" or not isinstance(state.get("bytes"), str):
        raise UpgradeError("journal file snapshot is malformed")
    try:
        payload = base64.b64decode(state["bytes"], validate=True)
    except ValueError as exc:
        raise UpgradeError("journal file snapshot encoding is malformed") from exc
    if len(payload) > MAX_ARTIFACT_BYTES or _hash(payload) != state.get("sha256"):
        raise UpgradeError("journal file snapshot hash or size is invalid")
    mode = state.get("mode")
    if not isinstance(mode, int) or isinstance(mode, bool) or mode & ~0o777:
        raise UpgradeError("journal file snapshot mode is invalid")
    return payload


def _matches(path: Path, expected: dict[str, Any], *, pointer: bool = False) -> bool:
    try:
        actual = _snapshot(path, pointer=pointer)
    except UpgradeError:
        return False
    return actual == expected


def _set_pointer(path: Path, state: dict[str, Any]) -> None:
    if not state.get("present"):
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        return
    target = state.get("target")
    if not isinstance(target, str):
        raise UpgradeError("journal pointer snapshot is malformed")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(12).hex()}"
    try:
        os.symlink(target, temporary)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _set_file(path: Path, state: dict[str, Any]) -> None:
    if not state.get("present"):
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        return
    _atomic_file(path, _decode_file(state), int(state["mode"]))


def _checkpoint(
    journal: dict[str, Any], root: Path, phase: str, hook: Callable[[str], None]
) -> None:
    journal["phase"] = phase
    _write_journal(root, journal)
    hook(phase)


def _smoke_candidate(root: Path, candidate: str) -> None:
    lib = releases.release_lib_path(root, candidate, module="codex_seats.cli")
    modules = (
        "codex_seats.cli",
        "codex_seats.releases",
        "codex_seats.upgrade",
        "codex_seats.launchd_dispatch",
        "codex_seats.dispatch_runner",
    )
    code = "import importlib,sys;sys.path.insert(0,sys.argv[1]);[importlib.import_module(x) for x in sys.argv[2:]]"
    try:
        result = bounded_process.run(
            [sys.executable, "-I", "-c", code, str(lib), *modules],
            timeout=SMOKE_TIMEOUT_SECONDS,
            env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"},
        )
    except bounded_process.ProcessError as exc:
        raise UpgradeError(
            f"candidate import smoke check could not complete: {exc}"
        ) from exc
    if result.returncode:
        detail = result.stderr_text(errors="replace").strip()[-2000:]
        detail = detail or result.stdout_text(errors="replace").strip()[-2000:]
        raise UpgradeError(
            f"candidate import smoke check failed: {detail or result.returncode}"
        )


def _candidate_doctor(root: Path, candidate: str) -> None:
    """Run the activated candidate's own read-only dispatch doctor."""
    argv = releases.release_module_argv(
        root, candidate, "codex_seats.cli", "dispatch", "doctor"
    )
    environment = releases.release_environment(
        root,
        candidate,
        environ={
            "HOME": str(Path.home()),
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
        },
    )
    try:
        result = bounded_process.run(
            argv,
            timeout=SMOKE_TIMEOUT_SECONDS,
            env=environment,
        )
    except bounded_process.ProcessError as exc:
        raise UpgradeError(
            f"candidate dispatch doctor could not complete: {exc}"
        ) from exc
    if result.returncode:
        detail = result.stderr_text(errors="replace").strip()[-2000:]
        detail = detail or result.stdout_text(errors="replace").strip()[-2000:]
        raise UpgradeError(
            f"candidate dispatch doctor failed: {detail or result.returncode}"
        )


def _validate_candidate(
    root: Path,
    candidate: str,
    *,
    confirm_heads_stopped: bool,
    allow_module_removals: Any,
    smoke: Callable[[Path, str], None],
) -> None:
    manifest = releases.verify_release(root, candidate)
    issues = releases.protocol_issues(manifest)
    if issues:
        raise UpgradeError("incompatible release: " + "; ".join(issues))
    active = releases._pointer_hash(root, "current")
    if active is not None and active != candidate:
        try:
            releases.verify_release(root, active)
        except releases.ReleaseError as exc:
            raise UpgradeError(
                "active release failed verification; restore it or roll back to a "
                "verified release before upgrading"
            ) from exc
    try:
        releases.validate_module_removals(root, candidate, allow_module_removals)
    except releases.ReleaseError as exc:
        raise UpgradeError(str(exc)) from exc
    # Upgrade reboots the queue owner, unlike pointer-only activation. Always require
    # active claims, queue frames, and the runner lock to be drained, including on routine
    # upgrades where releases.preflight(root) would intentionally be a no-op.
    bootstrap = releases._pointer_hash(root, "current") is None
    report = releases.preflight(root, bootstrap=True)
    if not report.ok:
        raise UpgradeError("upgrade preflight failed: " + "; ".join(report.blockers))
    if bootstrap and not confirm_heads_stopped:
        raise UpgradeError("bootstrap upgrade requires --confirm-heads-stopped")
    smoke(root, candidate)


def _snapshot_transaction(
    root: Path, candidate: str, dispatch: DispatchOperations
) -> dict[str, Any]:
    plist = dispatch.plist_path()
    _owner_directory(plist.parent, "dispatch plist parent")
    service = dispatch.status(root)
    original = {
        "current": _snapshot(root / "current", pointer=True),
        "previous": _snapshot(root / "previous", pointer=True),
        "codex-seat": _snapshot(root / "codex-seat"),
        "worker-hook": _snapshot(root / "worker-hook"),
        "dispatch_plist": _snapshot(plist),
    }
    if original["dispatch_plist"]["present"] and dispatch.validate_snapshot is not None:
        dispatch.validate_snapshot(root, _decode_file(original["dispatch_plist"]))
    installed = bool(service.get("installed"))
    loaded = bool(service.get("loaded"))
    if installed != original["dispatch_plist"]["present"]:
        raise UpgradeError("dispatch status disagrees with the plist snapshot")
    if loaded and not installed:
        raise UpgradeError("loaded dispatch service has no restorable plist")
    return {
        "schema": JOURNAL_SCHEMA,
        "transaction_id": os.urandom(16).hex(),
        "candidate": candidate,
        "phase": "snapshotted",
        "plist_path": str(plist.absolute()),
        "dispatch": {"installed": installed, "loaded": loaded},
        "original": original,
        "owned": dict(original),
    }


def _prospective(
    journal: dict[str, Any],
    root: Path,
    name: str,
    path: Path,
    state: dict[str, Any],
    phase: str,
    hook: Callable[[str], None],
    *,
    pointer: bool = False,
) -> None:
    if not _matches(path, journal["owned"][name], pointer=pointer):
        raise UpgradeError(
            f"transaction artifact changed before {phase}: {path}; refusing to overwrite it"
        )
    journal["owned"][name] = state
    _checkpoint(journal, root, phase, hook)


def _restore(
    journal: dict[str, Any],
    root: Path,
    dispatch: DispatchOperations,
    hook: Callable[[str], None],
) -> dict[str, Any]:
    plist = Path(journal["plist_path"])
    expected_plist = dispatch.plist_path().absolute()
    if not plist.is_absolute() or plist != expected_plist:
        raise UpgradeError(
            "journal dispatch plist path does not match the configured service adapter"
        )
    paths = {
        "current": root / "current",
        "previous": root / "previous",
        "codex-seat": root / "codex-seat",
        "worker-hook": root / "worker-hook",
        "dispatch_plist": plist,
    }
    # Compare every byte/target before changing any value.  An original value is
    # accepted to make recovery idempotent after death during rollback.
    conflicts: list[str] = []
    for name, path in paths.items():
        pointer = name in ("current", "previous")
        if not (
            _matches(path, journal["owned"][name], pointer=pointer)
            or _matches(path, journal["original"][name], pointer=pointer)
        ):
            conflicts.append(name)
    if conflicts:
        raise UpgradeError(
            "rollback ownership check failed for "
            + ", ".join(conflicts)
            + "; journal preserved for forensics"
        )

    entry_phase = journal["phase"]
    service_may_have_changed = entry_phase in _SERVICE_MAY_HAVE_CHANGED
    _checkpoint(journal, root, "rolling-back", hook)
    # Before the bootout phase, only durable files/pointers can have changed; do not
    # disturb an unrelated service transition. From bootout onward the journal cannot
    # distinguish a crash immediately before/after launchctl, so reconcile explicitly.
    if service_may_have_changed:
        dispatch.bootout(plist)
    for name in ("current", "previous"):
        path = paths[name]
        if not _matches(path, journal["original"][name], pointer=True):
            _set_pointer(path, journal["original"][name])
    for name in ("codex-seat", "worker-hook", "dispatch_plist"):
        path = paths[name]
        if not _matches(path, journal["original"][name]):
            _set_file(path, journal["original"][name])
    if service_may_have_changed and journal["dispatch"]["loaded"]:
        if not journal["original"]["dispatch_plist"]["present"]:
            raise UpgradeError(
                "journal claims a loaded dispatch service without a plist"
            )
        dispatch.bootstrap(plist)
    final = dispatch.status(root)
    if bool(final.get("installed")) != journal["dispatch"]["installed"]:
        raise UpgradeError("rollback did not restore dispatch installed state")
    if bool(final.get("loaded")) != journal["dispatch"]["loaded"]:
        raise UpgradeError("rollback did not restore dispatch loaded state")
    journal["owned"] = dict(journal["original"])
    _checkpoint(journal, root, "rolled-back", hook)
    _remove_journal(root, journal)
    return {"transaction_id": journal["transaction_id"], "restored": True}


def _final_verify(
    journal: dict[str, Any], root: Path, dispatch: DispatchOperations
) -> dict[str, Any]:
    candidate = journal["candidate"]
    releases.verify_release(root, candidate)
    if releases.current_release_id(root) != candidate:
        raise UpgradeError("final verification found the wrong current release")
    for name in ("codex-seat", "worker-hook"):
        if not _matches(root / name, journal["owned"][name]):
            raise UpgradeError(f"final verification found incorrect {name} shim bytes")
    plist = Path(journal["plist_path"])
    if not _matches(plist, journal["owned"]["dispatch_plist"]):
        raise UpgradeError("final verification found incorrect dispatch plist bytes")
    failed = [message for ok, message in dispatch.doctor(root) if not ok]
    if failed:
        raise UpgradeError("dispatch doctor failed: " + "; ".join(failed))
    service = dispatch.status(root)
    if not service.get("installed") or not service.get("loaded"):
        raise UpgradeError("candidate dispatch service is not installed and loaded")
    return service


def upgrade(
    root: str | Path,
    source: str | Path,
    *,
    confirm_heads_stopped: bool = False,
    allow_module_removals: Any = (),
    dispatch: DispatchOperations | None = None,
    stage: Callable[[str | Path, str | Path], str] = releases.stage_release,
    smoke: Callable[[Path, str], None] = _smoke_candidate,
    candidate_verify: Callable[[Path, str], None] = _candidate_doctor,
    checkpoint: Callable[[str], None] = lambda _phase: None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Stage, validate, activate, reconcile, and verify one release transaction."""
    _managed_refusal("perform")
    root_path = Path(root).expanduser().absolute()
    dispatch_ops = dispatch or default_dispatch_operations()
    _owner_directory(root_path, "upgrade root")
    with _Lock(root_path / LOCK_NAME, lock_timeout):
        if _read_journal(root_path) is not None:
            raise UpgradeError(
                "an incomplete upgrade exists; inspect release upgrade-status and run "
                "release upgrade-recover before retrying"
            )
        candidate = stage(root_path, source)
        _validate_candidate(
            root_path,
            candidate,
            confirm_heads_stopped=confirm_heads_stopped,
            allow_module_removals=allow_module_removals,
            smoke=smoke,
        )
        shim_sources = releases._bootstrap_sources(root_path, candidate)
        journal = _snapshot_transaction(root_path, candidate, dispatch_ops)
        _write_journal(root_path, journal, create=True)
        try:
            old_current = journal["original"]["current"]
            if old_current["present"] and old_current != _pointer_state(candidate):
                _prospective(
                    journal,
                    root_path,
                    "previous",
                    root_path / "previous",
                    old_current,
                    "activating-previous",
                    checkpoint,
                    pointer=True,
                )
                _set_pointer(root_path / "previous", old_current)
            candidate_pointer = _pointer_state(candidate)
            _prospective(
                journal,
                root_path,
                "current",
                root_path / "current",
                candidate_pointer,
                "activating-current",
                checkpoint,
                pointer=True,
            )
            _set_pointer(root_path / "current", candidate_pointer)

            for index, name in enumerate(("codex-seat", "worker-hook"), start=1):
                state = _file_state(shim_sources[name], 0o755)
                _prospective(
                    journal,
                    root_path,
                    name,
                    root_path / name,
                    state,
                    f"installing-shim-{index}",
                    checkpoint,
                )
                _set_file(root_path / name, state)

            plist_payload = dispatch_ops.render_bytes(root_path)
            plist_state = _file_state(plist_payload, 0o644)
            _prospective(
                journal,
                root_path,
                "dispatch_plist",
                Path(journal["plist_path"]),
                plist_state,
                "writing-plist",
                checkpoint,
            )
            plist = Path(journal["plist_path"])
            _set_file(plist, plist_state)
            _checkpoint(journal, root_path, "booting-out-dispatch", checkpoint)
            dispatch_ops.bootout(plist)
            _checkpoint(journal, root_path, "bootstrapping-dispatch", checkpoint)
            dispatch_ops.bootstrap(plist)
            _checkpoint(journal, root_path, "verifying", checkpoint)
            service = _final_verify(journal, root_path, dispatch_ops)
            candidate_verify(root_path, candidate)
            _remove_journal(root_path, journal)
            previous = releases._pointer_hash(root_path, "previous")
            return {
                "ok": True,
                "transaction_id": journal["transaction_id"],
                "candidate": candidate,
                "current": candidate,
                "previous": previous,
                "dispatch": service,
                "staged_release_kept": True,
            }
        except Exception as primary:
            try:
                rollback_result = _restore(journal, root_path, dispatch_ops, checkpoint)
            except Exception as rollback_error:  # noqa: BLE001 - report both failures
                result = {
                    "ok": False,
                    "transaction_id": journal["transaction_id"],
                    "candidate": candidate,
                    "primary_error": str(primary),
                    "rollback_error": str(rollback_error),
                    "journal_preserved": True,
                }
                raise UpgradeError(
                    f"upgrade failed ({primary}); rollback also failed ({rollback_error})",
                    result=result,
                ) from primary
            result = {
                "ok": False,
                "transaction_id": journal["transaction_id"],
                "candidate": candidate,
                "primary_error": str(primary),
                "rollback": rollback_result,
                "staged_release_kept": True,
            }
            raise UpgradeError(
                f"upgrade failed and was rolled back: {primary}", result=result
            ) from primary


def upgrade_status(root: str | Path) -> dict[str, Any]:
    """Read and validate transaction state without creating paths or taking a lock."""
    root_path = Path(root).expanduser().absolute()
    journal = _read_journal(root_path)
    if journal is None:
        return {"incomplete": False, "journal": str(_journal_path(root_path))}
    return {
        "incomplete": True,
        "journal": str(_journal_path(root_path)),
        "transaction_id": journal["transaction_id"],
        "candidate": journal["candidate"],
        "phase": journal["phase"],
    }


def recover(
    root: str | Path,
    *,
    dispatch: DispatchOperations | None = None,
    checkpoint: Callable[[str], None] = lambda _phase: None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Explicitly restore an incomplete transaction's exact original snapshot."""
    _managed_refusal("recover")
    root_path = Path(root).expanduser().absolute()
    dispatch_ops = dispatch or default_dispatch_operations()
    _owner_directory(root_path, "upgrade root")
    with _Lock(root_path / LOCK_NAME, lock_timeout):
        journal = _read_journal(root_path)
        if journal is None:
            raise UpgradeError("no incomplete upgrade journal to recover")
        return _restore(journal, root_path, dispatch_ops, checkpoint)
