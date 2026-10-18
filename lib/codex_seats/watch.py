from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import local_seats
from .account import refresh_account_usage
from .claude_usage_sidecar import ClaudeUsageSidecar
from .seat_registry import registered_seat_ids
from .state import effective_claude_mode, iso


WATCH_LOG_SCHEMA = "codex-seat.watch-refresh.v1"
LOCAL_PROBE_LOG_EVENT = "watch-local-probe"


def enabled_local_hosts(settings: dict[str, Any]) -> list[str]:
    """Hosts the read-only availability probe may observe.

    Mirrors the control plane's own gate: a global ``off`` disables every
    probe, and only an active host carrying at least one active seat whose
    effective mode is not ``off`` is ever observed.
    """
    provider = settings.get("providers", {}).get("local", {})
    if not isinstance(provider, dict) or provider.get("mode", "off") == "off":
        return []
    hosts = settings.get("local_hosts", {})
    seats = settings.get("local_seats", {})
    if not isinstance(hosts, dict) or not isinstance(seats, dict):
        return []
    enabled: list[str] = []
    for host_id, host in hosts.items():
        if not isinstance(host, dict) or host.get("lifecycle") != "active":
            continue
        if any(
            isinstance(seat, dict)
            and seat.get("host") == host_id
            and seat.get("lifecycle") == "active"
            and local_seats.effective_mode(settings, seat) != "off"
            for seat in seats.values()
        ):
            enabled.append(str(host_id))
    return sorted(enabled)


def _watch_seat_ids(settings: dict[str, Any]) -> list[str]:
    return registered_seat_ids(settings, lifecycles=("active", "draining"))


def acquire_refresh_lock(
    root: Path,
    cancel_event: threading.Event,
    *,
    timeout: float = 5.0,
    retry_delay: float = 0.1,
) -> int | None:
    """Acquire the process-shared provider lock with bounded cancelable waits."""
    deadline = time.monotonic() + max(0.0, timeout)
    path = Path(root) / ".watch-refresh.lock"
    while not cancel_event.is_set():
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except OSError:
            os.close(descriptor)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        cancel_event.wait(min(retry_delay, remaining))
    return None


def release_refresh_lock(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


@dataclass(frozen=True)
class RefreshSnapshot:
    """Immutable, point-in-time view of the background refresh state.

    ``next_due`` uses the refresher's monotonic clock, matching the scheduler
    and avoiding wall-clock adjustments. ``last_finished_at`` is wall time so
    callers can render a timestamp or age alongside provider telemetry.
    """

    refreshing: bool
    last_result: str | None
    # Structured outcome of the last real attempt: True=ok, False=failed,
    # None=none yet. The display must read THIS, not regex-parse last_result
    # (which drifts with the human message format).
    last_succeeded: bool | None
    last_finished_at: float | None
    last_success_at: float | None
    next_due: float | None
    completion_generation: int


@dataclass(frozen=True)
class SelectedRefreshSnapshot:
    """Immutable state of the coalesced single-seat Codex refresh request.

    This is deliberately independent of ``RefreshSnapshot``: a selected Codex
    refresh never advances the periodic schedule, so the display renders
    queued/refreshing/failed for one seat without disturbing the whole-pool
    figures.
    """

    queued: str | None
    active: str | None
    last_target: str | None
    last_result: str | None
    last_succeeded: bool | None
    last_finished_at: float | None
    generation: int


@dataclass(frozen=True)
class LocalProbeSnapshot:
    """Immutable view of the local-host probe service for redraws and the TUI.

    ``hosts`` maps a host id to its last recorded observation (``state``,
    ``reason``, ``observed_at``, ``finished_at``, ``source``). The dashboard
    itself renders from the on-disk availability cache; this snapshot exists
    so a UI can show probe activity and request an explicit probe.
    """

    enabled: bool
    periodic: bool
    probing: str | None
    pending: tuple[str, ...]
    hosts: dict[str, dict[str, Any]]
    generation: int
    last_error: str | None


class LocalProbeService:
    """Bounded, periodic, read-only local-host availability probing.

    One worker thread probes each enabled host at roughly the availability
    cache TTL so the dashboard's cached view stays fresh without ever probing
    from the key/render loop. Every probe goes through
    :func:`local_seats.probe_host`, which keeps the controller-authored argv
    contract, the bounded subprocess, and the global ``off`` gate intact.
    Explicit ``request_local_probe`` calls are coalesced per host and skip a
    host whose probe is already in flight.
    """

    def __init__(
        self,
        root: Path,
        settings: dict[str, Any],
        *,
        interval: float = local_seats.CACHE_TTL_SECONDS,
        periodic: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        waiter: Callable[[float], bool] | None = None,
        probe: Callable[..., dict[str, Any]] = local_seats.probe_host,
        settings_loader: Callable[[], dict[str, Any]] | None = None,
        audit: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> None:
        self.root = root
        self.settings = settings
        self.interval = max(1.0, float(interval))
        self.periodic = bool(periodic)
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.cancel_event = threading.Event()
        self._wake = threading.Event()
        self.waiter = waiter or self._wait
        self.probe = probe
        self.settings_loader = settings_loader
        self.audit = audit
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._pending: list[str] = []
        self._active: str | None = None
        self._next_due: dict[str, float] = {}
        self._hosts: dict[str, dict[str, Any]] = {}
        self._generation = 0
        self.last_error: str | None = None

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("local probe service is closed")
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.run_forever, name="cxwatch-local-probe", daemon=True
        )
        self._thread.start()

    def _wait(self, delay: float) -> bool:
        self._wake.wait(delay)
        self._wake.clear()
        return self.cancel_event.is_set()

    def request_local_probe(self, host_id: str) -> None:
        """Queue one explicit probe for ``host_id`` and wake the worker.

        A request for a host already being probed is satisfied by that probe;
        repeated requests for one host coalesce into a single pending entry.
        The global ``off`` mode is still honored by the probe itself.
        """
        host_id = str(host_id)
        with self._state_lock:
            if self._closed or self.cancel_event.is_set():
                raise RuntimeError("local probe service is closed")
            if self._active == host_id:
                return
            if host_id not in self._pending:
                self._pending.append(host_id)
        self._wake.set()

    def _take_pending(self) -> str | None:
        with self._state_lock:
            if not self._pending:
                return None
            return self._pending.pop(0)

    def _current_settings(self) -> dict[str, Any] | None:
        """Reload settings each cycle; a failed reload probes nothing (fail closed)."""
        try:
            settings = self.settings_loader() if self.settings_loader else self.settings
        except Exception as exc:
            self.last_error = f"failed closed loading current settings: {exc}"
            return None
        if not isinstance(settings, dict):
            self.last_error = "failed closed loading current settings: invalid document"
            return None
        self.settings = settings
        self.last_error = None
        return settings

    def _record(self, host_id: str, result: dict[str, Any], *, source: str) -> None:
        finished = self.wall_time()
        entry = {
            "state": result.get("state"),
            "reason": result.get("reason"),
            "observed_at": result.get("observed_at"),
            "detail": result.get("detail"),
            "model_profile": result.get("model_profile"),
            "finished_at": finished,
            "source": source,
        }
        with self._state_lock:
            previous = self._hosts.get(host_id)
            self._hosts[host_id] = entry
            self._generation += 1
        changed = previous is None or (
            (previous.get("state"), previous.get("reason"))
            != (entry["state"], entry["reason"])
        )
        if changed and self.audit is not None:
            # Only transitions are logged: a per-TTL heartbeat per host would
            # flood the shared audit file without adding operator information.
            self.audit(
                {
                    "schema": WATCH_LOG_SCHEMA,
                    "event": LOCAL_PROBE_LOG_EVENT,
                    "pid": os.getpid(),
                    "host": host_id,
                    "source": source,
                    "state": entry["state"],
                    "reason": entry["reason"],
                    "finished_at": iso(finished),
                }
            )

    def _schedule(self, host_id: str, delay: float) -> None:
        with self._state_lock:
            self._next_due[host_id] = self.monotonic() + max(0.0, delay)

    def run_probe(
        self, host_id: str, *, source: str = "requested", settings: dict[str, Any] | None = None
    ) -> str:
        """Probe one host now. Returns ``ok``, ``reused``, ``failed`` or the reason."""
        with self._state_lock:
            if self._closed:
                raise RuntimeError("local probe service is closed")
            self._active = host_id
            if host_id in self._pending:
                self._pending.remove(host_id)
        try:
            if settings is None:
                settings = self._current_settings()
            if settings is None:
                self._record(
                    host_id,
                    {"host": host_id, "state": "degraded", "reason": "settings_unavailable"},
                    source=source,
                )
                self._schedule(host_id, self.interval)
                return "failed"
            if source == "periodic":
                # Routing or another watcher may have just observed this host;
                # reuse a recent cache entry instead of duplicating the probe.
                now = self.wall_time()
                cached = local_seats.cached_availability(self.root, settings, host_id, now=now)
                observed = cached.get("observed_at")
                if isinstance(observed, (int, float)) and 0 <= now - observed < self.interval / 2:
                    self._record(host_id, cached, source="cache")
                    self._schedule(host_id, self.interval - (now - observed))
                    return "reused"
            try:
                result = self.probe(self.root, settings, host_id)
            except Exception as exc:  # a broken probe must never wedge the worker
                result = {
                    "host": host_id,
                    "state": "degraded",
                    "reason": f"probe_error:{type(exc).__name__}",
                }
            if not isinstance(result, dict):
                result = {"host": host_id, "state": "degraded", "reason": "probe_invalid_result"}
            self._record(host_id, result, source=source)
            self._schedule(host_id, self.interval)
            if result.get("state") == "ready":
                return "ok"
            return str(result.get("reason") or "failed")
        finally:
            with self._state_lock:
                self._active = None

    def run_forever(self) -> None:
        while not self.cancel_event.is_set():
            requested = self._take_pending()
            if requested is not None:
                self.run_probe(requested, source="requested")
                continue
            if not self.periodic:
                if self.waiter(3600.0):
                    break
                continue
            settings = self._current_settings()
            hosts = enabled_local_hosts(settings) if settings is not None else []
            with self._state_lock:
                for stale in [host for host in self._next_due if host not in hosts]:
                    del self._next_due[stale]
                now = self.monotonic()
                due = [host for host in hosts if self._next_due.get(host, 0.0) <= now]
                delays = [self._next_due[host] - now for host in hosts if host not in due]
            if due:
                for host in due:
                    if self.cancel_event.is_set():
                        break
                    self.run_probe(host, source="periodic", settings=settings)
                continue
            delay = min(delays) if delays else self.interval
            if self.waiter(max(0.0, min(delay, self.interval))):
                break

    def snapshot(self) -> LocalProbeSnapshot:
        with self._state_lock:
            return LocalProbeSnapshot(
                enabled=not self._closed,
                periodic=self.periodic,
                probing=self._active,
                pending=tuple(self._pending),
                hosts={host: dict(entry) for host, entry in self._hosts.items()},
                generation=self._generation,
                last_error=self.last_error,
            )

    def stop(self, *, timeout: float = 5.0) -> bool:
        with self._state_lock:
            self._closed = True
            self._pending.clear()
        self.cancel_event.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        return thread is None or not thread.is_alive()


def _disabled_local_snapshot() -> LocalProbeSnapshot:
    return LocalProbeSnapshot(
        enabled=False,
        periodic=False,
        probing=None,
        pending=(),
        hosts={},
        generation=0,
        last_error=None,
    )


class WatchRefresher:
    """Single-flight active usage refresh scheduled after each completed attempt."""

    def __init__(
        self,
        root: Path,
        settings: dict[str, Any],
        interval: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        waiter: Callable[[float], bool] | None = None,
        account_refresh: Callable[..., list[str]] = refresh_account_usage,
        periodic: bool = True,
        local_probes: bool = False,
        local_probe: LocalProbeService | None = None,
        local_probe_interval: float = local_seats.CACHE_TTL_SECONDS,
        claude_sidecar: ClaudeUsageSidecar | None = None,
    ) -> None:
        self.root = root
        self.settings = settings
        self.interval = float(interval)
        self.periodic = bool(periodic)
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.cancel_event = threading.Event()
        self.claude = claude_sidecar or ClaudeUsageSidecar(root, settings, self.cancel_event)
        # Local-host probing executes controller-authored argv, so it is only
        # attached when the caller (the trusted interactive dashboard) opts in.
        # ``periodic`` (cleared by --no-active-probe) also disables its
        # scheduled probes; explicit requests remain serviceable.
        self.local: LocalProbeService | None = local_probe
        if self.local is None and local_probes:
            self.local = LocalProbeService(
                root,
                settings,
                interval=local_probe_interval,
                periodic=periodic,
                monotonic=monotonic,
                wall_time=wall_time,
                settings_loader=self._latest_settings,
                audit=self._append_audit,
            )
        # Wakes the scheduler loop early for a selected-seat request or for a
        # cancel; the default waiter blocks on it instead of the raw cancel
        # event so a queued single-seat refresh is serviced promptly.
        self._wake = threading.Event()
        self.waiter = waiter or self._wait
        self.account_refresh = account_refresh
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self.refreshing = False
        self.last_result: str | None = None
        self.last_succeeded: bool | None = None
        self.last_finished_at: float | None = None
        self.last_success_at: float | None = None
        self.next_due: float | None = None
        self.completion_generation = 0
        # Coalesced selected-seat Codex refresh state. Only the latest request
        # is retained; contention retains intent instead of dropping it.
        self._selected_pending: str | None = None
        self._selected_active: str | None = None
        self._selected_last_target: str | None = None
        self._selected_last_result: str | None = None
        self._selected_last_succeeded: bool | None = None
        self._selected_last_finished_at: float | None = None
        self._selected_generation = 0
        self._full_pending = False
        # Bounded, cancelable shared-lock contention retry for a selected
        # refresh. The intent is retained across exhaustion for a later cycle.
        self._selected_lock_attempts = 3
        self._selected_retry_delay = 0.2

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("refresh service is closed")
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run_forever, name="cxwatch-refresh", daemon=True)
        self._thread.start()
        if self.local is not None:
            self.local.start()

    def request_local_probe(self, host_id: str) -> None:
        """Queue one explicit read-only host probe for the local probe worker."""
        if self.local is None:
            raise RuntimeError("local host probing is unavailable in this watch")
        self.local.request_local_probe(host_id)

    def local_snapshot(self) -> LocalProbeSnapshot:
        if self.local is None:
            return _disabled_local_snapshot()
        return self.local.snapshot()

    def _acquire_lock(self) -> int | None:
        return acquire_refresh_lock(self.root, self.cancel_event, timeout=0)

    def _append_audit(self, record: dict[str, Any]) -> str | None:
        """Append one complete attempt atomically across concurrent watchers."""
        path = self.root / "logs" / "watch-refresh.jsonl"
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.chmod(path, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                payload = (json.dumps(record, sort_keys=True, default=str) + "\n").encode()
                os.write(descriptor, payload)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        except OSError as exc:
            return str(exc)
        return None

    def _finish(
        self,
        result: str,
        *,
        started_at: float,
        started_monotonic: float,
        details: dict[str, Any] | None = None,
        success: bool = False,
        advance_generation: bool = True,
    ) -> None:
        finished_at = self.wall_time()
        finished_monotonic = self.monotonic()
        record = {
            "schema": WATCH_LOG_SCHEMA,
            "event": "watch-refresh",
            "pid": os.getpid(),
            "started_at": iso(started_at),
            "finished_at": iso(finished_at),
            "duration_seconds": max(0.0, finished_monotonic - started_monotonic),
            "probe_interval_seconds": self.interval,
            "result": result,
            **(details or {}),
        }
        audit_error = self._append_audit(record)
        if audit_error:
            result = f"{result}; audit log failed: {audit_error}"
        with self._state_lock:
            self.refreshing = False
            self.last_result = result
            self.last_finished_at = finished_at
            if success:
                self.last_success_at = finished_at
            self.next_due = finished_monotonic + self.interval
            if advance_generation:
                self.completion_generation += 1
                # Only a real attempt updates the outcome; a contention no-op
                # (advance_generation=False) keeps the last real result.
                self.last_succeeded = success

    def run_attempt(self) -> None:
        started_at = self.wall_time()
        started_monotonic = self.monotonic()
        with self._state_lock:
            if self.refreshing:
                return
            self.refreshing = True
            self.next_due = None
        descriptor = self._acquire_lock()
        if descriptor is None:
            self._finish(
                "shared refresh already in progress",
                started_at=started_at,
                started_monotonic=started_monotonic,
                details={"lock_acquired": False, "codex_messages": []},
                advance_generation=False,
            )
            return
        messages: list[str] = []
        try:
            try:
                settings = self._latest_settings()
            except Exception as exc:
                self._finish(
                    f"failed closed loading current settings: {exc}",
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    details={"lock_acquired": True, "codex_messages": []},
                )
                return
            self.settings = settings
            targets = _watch_seat_ids(settings)
            messages = self.account_refresh(
                self.root, settings, targets, cancel_event=self.cancel_event
            )
            succeeded = [
                message for message in messages if " account -> ok" in f" {message}"
            ]
            codex = f"Codex {len(succeeded)}/{len(targets)}"
            if self.cancel_event.is_set():
                self._finish(
                    f"{codex}; canceled",
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    details={"lock_acquired": True, "codex_messages": messages},
                )
                return
            failures = [message for message in messages if message not in succeeded]
            compact = [message if len(message) <= 140 else message[:137] + "..." for message in failures]
            detail = f"; {'; '.join(compact)}" if compact else ""
            claude_ok = True
            mode, _reason = effective_claude_mode(settings)
            if mode != "off" and settings.get("providers", {}).get("claude", {}).get("active_usage_refresh", True):
                self.claude.settings = settings
                try:
                    claude_ok, claude_detail = self.claude.probe()
                finally:
                    self.claude.close()
                detail += f"; Claude {'ok' if claude_ok else 'failed: ' + claude_detail}"
            self._finish(
                f"{codex}{detail}",
                started_at=started_at,
                started_monotonic=started_monotonic,
                details={"lock_acquired": True, "codex_messages": messages},
                success=len(succeeded) == len(targets) and claude_ok,
            )
        except Exception as exc:  # keep the redraw loop alive on an unexpected provider error
            self._finish(
                f"failed: {exc}",
                started_at=started_at,
                started_monotonic=started_monotonic,
                details={
                    "lock_acquired": True,
                    "codex_messages": messages,
                    "exception_type": type(exc).__name__,
                },
            )
        finally:
            release_refresh_lock(descriptor)

    def _wait(self, delay: float) -> bool:
        """Default waiter: wake early on cancel or a selected request.

        Returns True only for cancellation so the scheduler loop breaks; a
        selected-refresh wake returns False so the loop services the request
        and reschedules the periodic wait without losing its place.
        """
        self._wake.wait(delay)
        self._wake.clear()
        return self.cancel_event.is_set()

    def request_selected_refresh(self, seat_id: str) -> None:
        """Queue a coalesced Codex-only refresh for one seat and wake the loop.

        A later request supersedes an unstarted one (single selected target).
        """
        with self._state_lock:
            if self._closed or self.cancel_event.is_set():
                raise RuntimeError("refresh service is closed")
            if self._selected_active == seat_id:
                return
            self._selected_pending = seat_id
        self._wake.set()

    def _retain_selected(self, seat_id: str) -> None:
        with self._state_lock:
            if not self._closed and self._selected_pending is None:
                self._selected_pending = seat_id

    def _record_selected(
        self,
        seat_id: str,
        result: str,
        *,
        success: bool | None,
        advance: bool,
    ) -> None:
        finished = self.wall_time()
        with self._state_lock:
            self._selected_last_target = seat_id
            self._selected_last_result = result
            self._selected_last_finished_at = finished
            if advance:
                self._selected_generation += 1
                self._selected_last_succeeded = success

    def _latest_settings(self) -> dict[str, Any]:
        """Reload the current settings so a selected refresh targets fresh state."""
        from .state import load_settings

        # A missing file is the supported first-run state and has
        # no on-disk revision to race. Once settings.json exists, every read
        # failure is propagated so provider work fails closed.
        if not (self.root / "settings.json").exists():
            return self.settings
        settings, _warnings = load_settings(self.root)
        return settings

    def _take_and_run_selected(self) -> str | None:
        with self._state_lock:
            target = self._selected_pending
            if target is not None:
                self._selected_pending = None
        if target is None:
            return None
        self._wake.clear()
        return self.run_selected_attempt(target)

    def run_selected_attempt(self, seat_id: str) -> str:
        """Refresh one Codex seat's usage outside the periodic schedule.

        Returns a structured status: ``ok``/``failed``/``invalid``/
        ``contended``/``canceled``. A retired or unregistered target is
        rejected before any provider call; shared-lock contention retains the
        pending intent with bounded, cancelable retry and preserves last-good.
        """
        with self._state_lock:
            if self._closed:
                raise RuntimeError("refresh service is closed")
            self._selected_active = seat_id
        try:
            descriptor: int | None = None
            for attempt in range(self._selected_lock_attempts):
                if self.cancel_event.is_set():
                    self._retain_selected(seat_id)
                    return "canceled"
                descriptor = self._acquire_lock()
                if descriptor is not None:
                    break
                if attempt + 1 < self._selected_lock_attempts:
                    if self.cancel_event.wait(self._selected_retry_delay):
                        self._retain_selected(seat_id)
                        return "canceled"
            if descriptor is None:
                # Contended: keep the request queued and preserve last-good.
                self._retain_selected(seat_id)
                self._record_selected(
                    seat_id,
                    "shared refresh already in progress; will retry",
                    success=None,
                    advance=False,
                )
                return "contended"
            try:
                if self.cancel_event.is_set():
                    self._retain_selected(seat_id)
                    return "canceled"
                try:
                    settings = self._latest_settings()
                except Exception as exc:
                    self._record_selected(
                        seat_id, f"failed closed loading current settings: {exc}",
                        success=False, advance=True,
                    )
                    return "failed"
                self.settings = settings
                try:
                    registered = set(registered_seat_ids(settings))
                    probeable = set(
                        registered_seat_ids(
                            settings,
                            lifecycles=("active", "draining", "inactive"),
                        )
                    )
                except Exception as exc:
                    self._record_selected(
                        seat_id, f"invalid: {exc}", success=False, advance=True
                    )
                    return "invalid"
                if seat_id not in registered:
                    self._record_selected(
                        seat_id, "invalid: seat is not registered",
                        success=False, advance=True,
                    )
                    return "invalid"
                if seat_id not in probeable:
                    self._record_selected(
                        seat_id, "invalid: seat is retired and cannot be probed",
                        success=False, advance=True,
                    )
                    return "invalid"
                messages = self.account_refresh(
                    self.root, settings, [seat_id], cancel_event=self.cancel_event
                )
                if self.cancel_event.is_set():
                    self._record_selected(
                        seat_id, "canceled", success=None, advance=False
                    )
                    return "canceled"
                refreshed = any(
                    " account -> ok" in f" {message}" for message in messages
                )
                result = "; ".join(messages) if messages else "no account message"
                self._record_selected(
                    seat_id, result, success=refreshed, advance=True
                )
                return "ok" if refreshed else "failed"
            except Exception as exc:  # a provider error must not wedge the loop
                self._record_selected(
                    seat_id, f"failed: {exc}", success=False, advance=True
                )
                return "failed"
            finally:
                release_refresh_lock(descriptor)
        finally:
            with self._state_lock:
                self._selected_active = None

    def selected_snapshot(self) -> SelectedRefreshSnapshot:
        """Return one coherent selected-refresh state safe to read from redraws."""
        with self._state_lock:
            return SelectedRefreshSnapshot(
                queued=self._selected_pending,
                active=self._selected_active,
                last_target=self._selected_last_target,
                last_result=self._selected_last_result,
                last_succeeded=self._selected_last_succeeded,
                last_finished_at=self._selected_last_finished_at,
                generation=self._selected_generation,
            )

    def request_full_refresh(self) -> None:
        """Ask for one full refresh attempt as soon as the worker is free.

        Works with ``periodic`` off too; an attempt already running satisfies it.
        """
        with self._state_lock:
            if self._closed or self.cancel_event.is_set():
                raise RuntimeError("refresh service is closed")
            self._full_pending = True
        self._wake.set()

    def run_forever(self) -> None:
        while not self.cancel_event.is_set():
            selected_result = self._take_and_run_selected()
            if selected_result is not None:
                # Exhausted lock contention retains intent, but do not
                # immediately spin through another retry batch.
                if selected_result == "contended" and self.waiter(1.0):
                    break
                continue
            with self._state_lock:
                full, self._full_pending = self._full_pending, False
            if full:
                self.run_attempt()
                continue
            if not self.periodic:
                if self.waiter(3600.0):
                    break
                continue
            with self._state_lock:
                due = self.next_due
            now = self.monotonic()
            if due is None or now >= due:
                self.run_attempt()
                continue
            delay = max(0.0, due - now)
            if self.waiter(delay):
                break

    def status_line(self) -> str:
        snapshot = self.snapshot()
        if snapshot.refreshing:
            return "Usage refresh: refreshing · next refresh scheduled after completion"
        last = "not completed yet" if snapshot.last_result is None else snapshot.last_result
        if snapshot.last_finished_at is not None:
            last += f" at {iso(snapshot.last_finished_at)}"
        if snapshot.next_due is None:
            countdown = "pending"
        else:
            seconds = max(0, int(snapshot.next_due - self.monotonic() + 0.999))
            minutes, remainder = divmod(seconds, 60)
            countdown = f"{minutes}m{remainder:02d}s" if minutes else f"{remainder}s"
        return f"Usage refresh: last {last} · next in {countdown}"

    def snapshot(self) -> RefreshSnapshot:
        """Return one coherent refresh state safe to consume from redraws."""
        with self._state_lock:
            return RefreshSnapshot(
                refreshing=self.refreshing,
                last_result=self.last_result,
                last_succeeded=self.last_succeeded,
                last_finished_at=self.last_finished_at,
                last_success_at=self.last_success_at,
                next_due=self.next_due,
                completion_generation=self.completion_generation,
            )

    # The explicit name is convenient for callers that already use "snapshot"
    # for the seat ledger while ``snapshot()`` remains the concise public API.
    def refresh_snapshot(self) -> RefreshSnapshot:
        return self.snapshot()

    def stop(self, *, timeout: float = 5.0) -> bool:
        """Cancel and join within ``timeout``.

        A false result means provider work is still running; callers must
        refuse replacement/login until it returns.
        """
        with self._state_lock:
            self._closed = True
            self._selected_pending = None
        self.cancel_event.set()
        self._wake.set()  # release a default waiter blocked on _wake
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if self.local is not None and not self.local.stop(timeout=timeout):
            stopped = False
        if stopped:
            self.claude.close()
        return stopped
