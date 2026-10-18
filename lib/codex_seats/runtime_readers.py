"""Secure diagnostics for long-lived settings readers.

Long-lived readers publish only topology identity and immutable build metadata.
The files deliberately contain no settings payload, provider identity, or auth data.
Reader startup/reload takes a shared lock so registration is never torn.
"""

from __future__ import annotations

import calendar
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import bounded_process, releases


METADATA_SCHEMA = 1
SETTINGS_MIN = 3
SETTINGS_MAX = 3
MAX_READER_BYTES = 64 * 1024
MAX_RUNTIME_BYTES = 4 * 1024 * 1024
STALE_READER_SECONDS = 15 * 60
PROCESS_START_LOOKUP_TIMEOUT_SECONDS = 2.0
# ``ps lstart`` has whole-second precision. Require a wider separation so
# rounding at the registration boundary cannot turn a current reader into a
# reused-PID record.
PROCESS_START_EVIDENCE_MARGIN_SECONDS = 1.0
_READER_KIND_RE = re.compile(r"\A[a-z][a-z0-9-]{0,63}\Z")
_PROCESS_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_HASH_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_READER_FILE_RE = re.compile(r"\Areader-[0-9a-f]{32}\.json\Z")


class ReaderMetadataError(ValueError):
    """Reader state is malformed, untrusted, or cannot be coordinated safely."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def settings_identity(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic, non-secret identity for one settings topology."""
    if settings.get("schema") != SETTINGS_MAX:
        raise ReaderMetadataError("observed settings schema must be 3")
    pool = settings.get("pool")
    if not isinstance(pool, Mapping):
        raise ReaderMetadataError("schema-3 settings pool must be an object")
    seats = pool.get("seats")
    if not isinstance(seats, Mapping):
        raise ReaderMetadataError("schema-3 settings pool.seats must be an object")
    # Registry identity intentionally excludes every non-topology settings section.
    registry = {
        "primary": pool.get("primary"),
        "seats": seats,
    }
    return {
        "schema": 3,
        "topology": "dynamic-seat-registry",
        "registry_sha256": "sha256:" + hashlib.sha256(_canonical_bytes(registry)).hexdigest(),
    }


def _loaded_release(root: Path, module_file: str | Path) -> dict[str, Any]:
    try:
        release_id = releases.loaded_release_id(root, module_file)
    except releases.ReleaseError:
        release_id = None
    return {
        "id": release_id,
        "source": "immutable" if release_id is not None else "unpinned",
    }


def build_metadata(
    reader_kind: str,
    settings: Mapping[str, Any] | None,
    *,
    root: str | Path,
    pid: int | None = None,
    process_identity: str | None = None,
    process_started_at: float | None = None,
    observed_at: float | None = None,
    heartbeat_at: float | None = None,
    module_file: str | Path = __file__,
    release: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the one canonical metadata shape used by status, leases, and files."""
    if not isinstance(reader_kind, str) or not _READER_KIND_RE.fullmatch(reader_kind):
        raise ReaderMetadataError("reader kind must be a lowercase diagnostic slug")
    current = time.time()
    heartbeat = current if heartbeat_at is None else float(heartbeat_at)
    observed = heartbeat if observed_at is None else float(observed_at)
    identity = process_identity or os.urandom(16).hex()
    process_pid = os.getpid() if pid is None else pid
    started = heartbeat if process_started_at is None else float(process_started_at)
    observation = (
        {
            "schema": None,
            "topology": "not-loaded",
            "registry_sha256": None,
        }
        if settings is None
        else settings_identity(settings)
    )
    metadata = {
        "metadata_schema": METADATA_SCHEMA,
        "reader_kind": reader_kind,
        "process": {
            "pid": process_pid,
            "identity": identity,
            "started_at": started,
        },
        "release": dict(release or _loaded_release(Path(root), module_file)),
        "supported_settings": {"min": SETTINGS_MIN, "max": SETTINGS_MAX},
        "observed_settings": observation,
        "observed_at": observed,
        "heartbeat_at": heartbeat,
    }
    validate_metadata(metadata, allow_not_loaded=settings is None)
    return metadata


def validate_metadata(value: Any, *, allow_not_loaded: bool = False) -> dict[str, Any]:
    """Strictly validate canonical reader metadata; unknown fields fail closed."""
    if not isinstance(value, dict) or set(value) != {
        "metadata_schema",
        "reader_kind",
        "process",
        "release",
        "supported_settings",
        "observed_settings",
        "observed_at",
        "heartbeat_at",
    }:
        raise ReaderMetadataError("reader metadata has missing or unknown fields")
    if value.get("metadata_schema") != METADATA_SCHEMA:
        raise ReaderMetadataError("reader metadata schema is unsupported")
    kind = value.get("reader_kind")
    if not isinstance(kind, str) or not _READER_KIND_RE.fullmatch(kind):
        raise ReaderMetadataError("reader kind is malformed")
    process = value.get("process")
    if not isinstance(process, dict) or set(process) != {"pid", "identity", "started_at"}:
        raise ReaderMetadataError("reader process identity is malformed")
    pid = process.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ReaderMetadataError("reader pid is malformed")
    identity = process.get("identity")
    if not isinstance(identity, str) or not _PROCESS_ID_RE.fullmatch(identity):
        raise ReaderMetadataError("reader process identity is malformed")
    release = value.get("release")
    if not isinstance(release, dict) or set(release) != {"id", "source"}:
        raise ReaderMetadataError("reader release identity is malformed")
    source, release_id = release.get("source"), release.get("id")
    if source == "immutable":
        try:
            releases.validate_release_id(release_id)
        except releases.ReleaseError as exc:
            raise ReaderMetadataError("reader immutable release id is malformed") from exc
    elif source == "unpinned":
        if release_id is not None:
            raise ReaderMetadataError("unpinned reader must not claim a release id")
    else:
        raise ReaderMetadataError("reader release source is malformed")
    supported = value.get("supported_settings")
    if not isinstance(supported, dict) or set(supported) != {"min", "max"}:
        raise ReaderMetadataError("reader settings range is malformed")
    minimum, maximum = supported.get("min"), supported.get("max")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or minimum < 1
        or maximum < minimum
    ):
        raise ReaderMetadataError("reader settings range is malformed")
    observation = value.get("observed_settings")
    if not isinstance(observation, dict) or set(observation) != {
        "schema", "topology", "registry_sha256"
    }:
        raise ReaderMetadataError("reader settings identity is malformed")
    schema = observation.get("schema")
    topology = observation.get("topology")
    digest = observation.get("registry_sha256")
    if allow_not_loaded and schema is None and topology == "not-loaded" and digest is None:
        pass
    elif schema == 3:
        if (
            topology != "dynamic-seat-registry"
            or not isinstance(digest, str)
            or not _HASH_RE.fullmatch(digest)
        ):
            raise ReaderMetadataError("schema-3 reader registry identity is malformed")
    else:
        raise ReaderMetadataError("reader observed settings schema is malformed")
    for field in (process.get("started_at"), value.get("observed_at"), value.get("heartbeat_at")):
        if (
            not isinstance(field, (int, float))
            or isinstance(field, bool)
            or not math.isfinite(float(field))
            or field < 0
        ):
            raise ReaderMetadataError("reader observation times are malformed")
    return value


def _owner_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ReaderMetadataError(f"reader path must be an owner-controlled real directory: {path}")


def _lock_path(root: Path) -> Path:
    return root / ".settings-readers.lock"


@contextmanager
def readers_lock(root: str | Path, *, exclusive: bool, create: bool) -> Iterator[None]:
    """Take the settings readers lock in shared or exclusive mode."""
    root_path = Path(root)
    _owner_directory(root_path, create=create)
    path = _lock_path(root_path)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ReaderMetadataError(f"cannot open settings readers lock: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ReaderMetadataError("settings readers lock must be owner-controlled")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical_bytes(value) + b"\n"
    if len(payload) > MAX_READER_BYTES:
        raise ReaderMetadataError("reader metadata exceeds the size limit")
    _owner_directory(path.parent, create=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("reader metadata write made no progress")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ReaderMetadataError("refusing to replace unsafe reader metadata")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class SettingsReader:
    """Registration/heartbeat handle for one cooperating long-lived reader."""

    def __init__(
        self,
        root: str | Path,
        reader_kind: str,
        *,
        clock: Callable[[], float] = time.time,
        pid: int | None = None,
        process_identity: str | None = None,
        module_file: str | Path = __file__,
    ) -> None:
        self.root = Path(root)
        self.reader_kind = reader_kind
        self.clock = clock
        self.pid = os.getpid() if pid is None else pid
        self.identity = process_identity or os.urandom(16).hex()
        self.started_at = float(clock())
        self.module_file = module_file
        self.metadata = build_metadata(
            reader_kind,
            None,
            root=self.root,
            pid=self.pid,
            process_identity=self.identity,
            process_started_at=self.started_at,
            heartbeat_at=self.started_at,
            module_file=module_file,
        )
        self.path = self.root / "readers" / f"reader-{self.identity}.json"

    def __enter__(self) -> SettingsReader:
        # Publishing not-loaded under the shared lock keeps registration atomic
        # with respect to any exclusive readers-lock holder.
        with readers_lock(self.root, exclusive=False, create=True):
            _atomic_write(self.path, self.metadata)
        return self

    def __exit__(self, *_args: object) -> None:
        # Records remain durable evidence; dead records are reported/ignored later.
        self.heartbeat()

    def load(self, loader: Callable[[Path], tuple[dict[str, Any], list[str]]]) -> tuple[dict[str, Any], list[str]]:
        """Reload settings and publish their identity under the shared lock."""
        with readers_lock(self.root, exclusive=False, create=True):
            settings, warnings = loader(self.root)
            current = float(self.clock())
            self.metadata = build_metadata(
                self.reader_kind,
                settings,
                root=self.root,
                pid=self.pid,
                process_identity=self.identity,
                process_started_at=self.started_at,
                observed_at=current,
                heartbeat_at=current,
                module_file=self.module_file,
                release=self.metadata["release"],
            )
            _atomic_write(self.path, self.metadata)
            return settings, warnings

    def heartbeat(self) -> dict[str, Any]:
        self.metadata = dict(self.metadata)
        self.metadata["heartbeat_at"] = float(self.clock())
        _atomic_write(self.path, self.metadata)
        return json.loads(json.dumps(self.metadata))

    def for_process(self, reader_kind: str, pid: int) -> dict[str, Any]:
        metadata = json.loads(json.dumps(self.metadata))
        metadata["reader_kind"] = reader_kind
        metadata["process"] = {
            "pid": int(pid),
            "identity": os.urandom(16).hex(),
            "started_at": float(self.clock()),
        }
        metadata["heartbeat_at"] = float(self.clock())
        validate_metadata(metadata, allow_not_loaded=False)
        return metadata


def _secure_json(path: Path, maximum: int) -> Any:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ReaderMetadataError(f"cannot securely read {path.name}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ReaderMetadataError(f"{path.name} is not an owner-controlled regular file")
        if info.st_size > maximum:
            raise ReaderMetadataError(f"{path.name} exceeds the size limit of {maximum} bytes")
        payload = b""
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        if len(payload) > maximum:
            raise ReaderMetadataError(f"{path.name} exceeds the size limit of {maximum} bytes")
        after = os.fstat(descriptor)
        if any(
            getattr(info, field) != getattr(after, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ) or len(payload) != info.st_size:
            raise ReaderMetadataError(f"{path.name} changed while it was read")
        try:
            current = path.lstat()
        except OSError as exc:
            raise ReaderMetadataError(f"{path.name} changed while it was read") from exc
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ReaderMetadataError(f"{path.name} changed while it was read")
    finally:
        os.close(descriptor)
    try:
        return json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReaderMetadataError(f"{path.name} is invalid JSON") from exc


def inspect_readers(
    root: str | Path,
    *,
    current_identity: Mapping[str, Any] | None = None,
    clock: Callable[[], float] = time.time,
    pid_alive: Callable[[Any], bool] | None = None,
    pid_started_at: Callable[[Any], float | None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read reader files without creating, rewriting, or deleting any record."""
    root_path = Path(root)
    directory = root_path / "readers"
    if not directory.exists() and not directory.is_symlink():
        return [], []
    try:
        _owner_directory(directory, create=False)
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except (OSError, ReaderMetadataError) as exc:
        return [], [f"cannot safely inspect settings readers: {exc}"]
    is_alive = pid_alive or _pid_alive
    started_at = pid_started_at or _pid_started_at
    now = float(clock())
    reports: list[dict[str, Any]] = []
    blockers: list[str] = []
    for path in entries:
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue
        if not _READER_FILE_RE.fullmatch(path.name):
            blockers.append(f"untrusted settings reader artifact: {path.name}")
            reports.append({"path": path.name, "state": "malformed"})
            continue
        try:
            metadata = _secure_json(path, MAX_READER_BYTES)
            validate_metadata(metadata, allow_not_loaded=True)
            expected_name = f"reader-{metadata['process']['identity']}.json"
            if path.name != expected_name:
                raise ReaderMetadataError(
                    "reader filename does not match its process identity"
                )
        except ReaderMetadataError as exc:
            blockers.append(f"malformed settings reader {path.name}: {exc}")
            reports.append({"path": path.name, "state": "malformed", "reason": str(exc)})
            continue
        pid = metadata["process"]["pid"]
        live = bool(is_alive(pid))
        age = max(0.0, now - float(metadata["heartbeat_at"]))
        process_birth: float | None = None
        if live:
            try:
                candidate = started_at(pid)
                if (
                    isinstance(candidate, (int, float))
                    and not isinstance(candidate, bool)
                    and math.isfinite(float(candidate))
                    and 0 <= float(candidate) <= now + PROCESS_START_EVIDENCE_MARGIN_SECONDS
                ):
                    process_birth = float(candidate)
            except Exception:
                # Process inspection is advisory evidence. Failure must retain
                # the live-reader guards below rather than weakening them.
                process_birth = None
        registered_through = max(
            float(metadata["process"]["started_at"]),
            float(metadata["observed_at"]),
            float(metadata["heartbeat_at"]),
        )
        reused = bool(
            live
            and process_birth is not None
            and process_birth
            > registered_through + PROCESS_START_EVIDENCE_MARGIN_SECONDS
        )
        state = "pid-reused" if reused else ("live" if live else "dead")
        report = {"path": path.name, "state": state, "heartbeat_age_seconds": age, **metadata}
        if process_birth is not None:
            report["current_process_started_at"] = process_birth
        reports.append(report)
        if not live or reused:
            continue
        if age > STALE_READER_SECONDS:
            blockers.append(f"live settings reader {path.name} has a stale heartbeat")
        bounds = metadata["supported_settings"]
        if not bounds["min"] <= SETTINGS_MAX <= bounds["max"]:
            blockers.append(f"live settings reader {path.name} does not support settings schema 3")
        if metadata["release"]["source"] != "immutable":
            blockers.append(f"live settings reader {path.name} is not pinned to an immutable release")
        observed = metadata["observed_settings"]
        if observed.get("schema") is None:
            blockers.append(f"live settings reader {path.name} has not identified loaded settings")
        elif current_identity is not None and dict(observed) != dict(current_identity):
            blockers.append(f"live settings reader {path.name} observed stale settings identity")
    return reports, blockers


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


def _pid_started_at(pid: Any) -> float | None:
    """Return a live PID's wall-clock start time, or no identity evidence.

    Both macOS and Linux expose ``lstart`` through ``ps``. Its C-locale format
    has only whole-second precision; ``inspect_readers`` applies the conservative
    comparison margin and treats every failure here as unverified/live.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    # UTC avoids ambiguous local wall times during a daylight-saving transition.
    environment = {"LC_ALL": "C", "LANG": "C", "PATH": os.defpath, "TZ": "UTC0"}
    try:
        result = bounded_process.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            timeout=PROCESS_START_LOOKUP_TIMEOUT_SECONDS,
            env=environment,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout_text("ascii").splitlines()
        if len(lines) != 1 or not lines[0].strip():
            return None
        parsed = time.strptime(lines[0].strip(), "%a %b %d %H:%M:%S %Y")
        started = float(calendar.timegm(parsed))
    except (bounded_process.ProcessError, UnicodeError, ValueError, OverflowError, OSError):
        return None
    return started if math.isfinite(started) and started >= 0 else None


def status_snapshot(root: str | Path) -> list[dict[str, Any]]:
    """Read-only reader diagnostics for additive status JSON."""
    readers, blockers = inspect_readers(root)
    if blockers:
        readers.extend({"state": "malformed", "reason": blocker} for blocker in blockers)
    return readers
