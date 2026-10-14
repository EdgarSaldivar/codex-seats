"""Completion-driven, owner-safe filesystem notification backends."""

from __future__ import annotations

import ctypes
import errno
import os
import select
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Callable

from .dispatch_protocol import DispatchError

_CHILDREN = ("inbox", "processing", "receipts")

_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_UNMOUNT = 0x00002000
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_EVENT_HEADER = struct.Struct("iIII")
_MAX_DRAIN_BATCHES = 16


def production_backend() -> str:
    """Name the event backend selected on this production platform."""
    if sys.platform.startswith("linux"):
        return "inotify"
    if hasattr(select, "kqueue"):
        return "kqueue"
    return "poll"


def _secure_directory_fds(root: str | Path) -> tuple[int, list[int]]:
    """Open dispatch and observable children without following directory symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        base_fd = os.open(Path(root) / "dispatch", flags)
    except OSError as exc:
        raise DispatchError(f"cannot securely open dispatch directory: {exc}") from exc
    children: list[int] = []
    try:
        for fd, label in ((base_fd, "dispatch"),):
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise DispatchError(f"{label} directory must be owner-only")
        for name in _CHILDREN:
            try:
                fd = os.open(name, flags, dir_fd=base_fd)
            except OSError as exc:
                raise DispatchError(
                    f"cannot securely open dispatch/{name} directory: {exc}"
                ) from exc
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                os.close(fd)
                raise DispatchError(f"dispatch/{name} directory must be owner-only")
            children.append(fd)
        return base_fd, children
    except Exception:
        for fd in children:
            os.close(fd)
        os.close(base_fd)
        raise


class QueueEventWaiter:
    """Arm queue notifications before callers recheck durable state."""

    def __init__(
        self,
        root: str | Path,
        *,
        backend: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval: float = 0.25,
    ) -> None:
        default_backend = backend is None
        self.backend = production_backend() if default_backend else backend
        if self.backend not in {"kqueue", "inotify", "poll"}:
            raise ValueError(f"unknown queue event backend: {self.backend}")
        self._sleeper = sleeper
        self._poll_interval = max(0.001, float(poll_interval))
        self._base_fd: int | None = None
        self._directory_fds: list[int] = []
        self._watch_fds: list[int] = []
        self._event_fd: int | None = None
        self._kqueue = None
        try:
            # Unsupported production platforms still validate and pin the queue
            # directories. Only an explicitly injected deterministic poller skips
            # directory setup so clock-only timeout tests stay side-effect free.
            if self.backend != "poll" or default_backend:
                self._open_watch_fds(root)
            if self.backend == "kqueue":
                self._arm_kqueue()
            elif self.backend == "inotify":
                self._arm_inotify()
        except Exception:
            self.close()
            raise

    def _open_watch_fds(self, root: str | Path) -> None:
        self._base_fd, self._directory_fds = _secure_directory_fds(root)
        self._watch_fds = [self._base_fd, *self._directory_fds]

    def _arm_kqueue(self) -> None:
        if not hasattr(select, "kqueue"):
            raise DispatchError("kqueue backend is unavailable")
        queue = select.kqueue()
        flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
        fflags = (
            select.KQ_NOTE_WRITE
            | select.KQ_NOTE_EXTEND
            | select.KQ_NOTE_RENAME
            | select.KQ_NOTE_DELETE
        )
        changes = [
            select.kevent(fd, filter=select.KQ_FILTER_VNODE, flags=flags, fflags=fflags)
            for fd in self._watch_fds
        ]
        self._kqueue = queue
        queue.control(changes, 0, 0)

    def _arm_inotify(self) -> None:
        if not sys.platform.startswith("linux"):
            raise DispatchError("inotify backend is unavailable")
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        event_fd = init(os.O_CLOEXEC | os.O_NONBLOCK)
        if event_fd < 0:
            error = ctypes.get_errno()
            raise DispatchError(f"cannot initialize inotify: {os.strerror(error)}")
        add = libc.inotify_add_watch
        add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add.restype = ctypes.c_int
        mask = (
            _IN_CREATE
            | _IN_MOVED_TO
            | _IN_CLOSE_WRITE
            | _IN_ATTRIB
            | _IN_DELETE
            | _IN_DELETE_SELF
            | _IN_MOVE_SELF
        )
        try:
            for directory_fd in self._watch_fds:
                path = f"/proc/self/fd/{directory_fd}".encode()
                if add(event_fd, path, mask) < 0:
                    error = ctypes.get_errno()
                    raise DispatchError(
                        f"cannot watch secure dispatch directory: {os.strerror(error)}"
                    )
        except Exception:
            os.close(event_fd)
            raise
        self._event_fd = event_fd

    def wait(self, timeout: float) -> bool:
        """Block once and coalesce every notification currently available."""
        timeout = max(0.0, float(timeout))
        if self.backend == "poll":
            self._sleeper(min(self._poll_interval, timeout))
            return True
        if self.backend == "kqueue":
            assert self._kqueue is not None
            deadline = time.monotonic() + timeout
            while True:
                try:
                    events = self._kqueue.control(
                        None,
                        max(1, len(self._watch_fds)),
                        max(0.0, deadline - time.monotonic()),
                    )
                    break
                except InterruptedError:
                    if time.monotonic() >= deadline:
                        return False
            self._validate_kqueue_events(events)
            if events:
                for _ in range(_MAX_DRAIN_BATCHES):
                    drained = self._kqueue.control(
                        None, max(1, len(self._watch_fds)), 0
                    )
                    if not drained:
                        break
                    self._validate_kqueue_events(drained)
            return bool(events)
        assert self._event_fd is not None
        deadline = time.monotonic() + timeout
        while True:
            try:
                readable, _, _ = select.select(
                    [self._event_fd], [], [], max(0.0, deadline - time.monotonic())
                )
                break
            except InterruptedError:
                if time.monotonic() >= deadline:
                    return False
        if not readable:
            return False
        for _ in range(_MAX_DRAIN_BATCHES):
            try:
                data = os.read(self._event_fd, 64 * 1024)
                if not data:
                    break
                self._validate_inotify_events(data)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
        return True

    @staticmethod
    def _validate_kqueue_events(events) -> None:
        invalidation = select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
        invalidation |= getattr(select, "KQ_NOTE_REVOKE", 0)
        for event in events:
            if event.flags & select.KQ_EV_ERROR:
                error = int(event.data or errno.EIO)
                raise DispatchError(
                    f"dispatch directory watch failed: {os.strerror(error)}"
                )
            if event.fflags & invalidation:
                raise DispatchError("watched dispatch directory was replaced or revoked")

    @staticmethod
    def _validate_inotify_events(data: bytes) -> None:
        offset = 0
        invalidation = _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT | _IN_IGNORED
        while offset + _IN_EVENT_HEADER.size <= len(data):
            _watch, mask, _cookie, name_length = _IN_EVENT_HEADER.unpack_from(data, offset)
            offset += _IN_EVENT_HEADER.size + name_length
            if mask & invalidation:
                raise DispatchError("watched dispatch directory was replaced or revoked")
            # Overflow does not lose correctness: callers re-read every durable
            # artifact after this wakeup. The watch itself remains valid.
            if mask & _IN_Q_OVERFLOW:
                continue

    def close(self) -> None:
        if self._kqueue is not None:
            self._kqueue.close()
            self._kqueue = None
        if self._event_fd is not None:
            os.close(self._event_fd)
            self._event_fd = None
        for fd in getattr(self, "_directory_fds", []):
            os.close(fd)
        self._directory_fds = []
        self._watch_fds = []
        if getattr(self, "_base_fd", None) is not None:
            os.close(self._base_fd)
            self._base_fd = None

    def __enter__(self) -> "QueueEventWaiter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
