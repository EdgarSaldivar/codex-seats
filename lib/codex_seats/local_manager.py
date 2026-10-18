"""Guarded local host/seat action layer for the interactive seat manager.

This is the local-provider counterpart of :mod:`seat_manager` and shares its
error types and contract. It never writes settings JSON directly: every write
flows through :func:`local_cli.mutate` using the distinct internal
``MANAGER_AUTHORITY`` path, which re-reads and digest-pins the settings inside
the settings lock and is accepted only when
:func:`seat_manager.ensure_not_managed_worker` passes (any context except a
managed worker; the interactive manager itself is gated by a real TTY) and the
caller passes the explicit current-request confirmation marker. The existing
CLI guards are untouched: this layer is not reachable from ``codex-seat local``.

Design contract
---------------
* **Structured reads.** :meth:`LocalSeatManager.report` returns the global
  mode, every registered host and seat with its configuration, cached
  availability/eligibility, the dashboard's shared display status (off, stale
  with last known state and age, available, working, ...), and the live probe
  snapshot. Reads are strict (invalid settings raise) and never probe or write.
* **Immutable previews.** :class:`LocalActionPreview` freezes the action, its
  target, the requested values, the exact canonical before/after documents and
  the full settings-byte ``expected_digest``. :meth:`LocalSeatManager.commit`
  replays that digest so a concurrent change since the preview aborts with
  :class:`StalePreviewError` and nothing written. Unrelated settings are
  preserved by the shared transaction.
* **Probes never run on the caller's thread.** :meth:`request_probe` queues a
  coalesced read-only probe through ``WatchRefresher.request_local_probe``;
  results are observed through :meth:`local_snapshot`.

Public API
----------
* ``LocalSeatManager(root, *, refresher=None, clock=time.time, settings_loader=None)``
* ``manager.report(now=None)`` / ``manager.host_report(host_id)`` / ``manager.seat_report(seat_id)``
* ``manager.local_snapshot()`` / ``manager.request_probe(host_id)``
* ``manager.preview(action, target_id=None, **option) -> LocalActionPreview``
  and the ``preview_*`` conveniences for each supported action, including the
  manager-only ``preview_remove_seat`` (retire in place, never delete)
* ``manager.commit(preview, *, from_current_user_request) -> LocalMutationResult``
* ``manager.reload()`` / ``manager.replace_refresher(refresher)`` / ``manager.close()``
"""

from __future__ import annotations

import dataclasses
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import dispatch_client, display, local_cli, local_seats
from .dispatch_protocol import DispatchError
from .seat_manager import (
    ManagerClosedError,
    ManagerEligibilityError,
    ManagerError,
    StalePreviewError,
    _freeze,
    _thaw,
    ensure_not_managed_worker,
)

LOCAL_ACTIONS = frozenset(local_cli.STRUCTURED_ACTIONS)


def _kind(action: str) -> str:
    if action == "set-global-mode":
        return "global"
    return "host" if "host" in action else "seat"


def _entry(document: Mapping[str, Any], kind: str, target_id: str | None) -> Any:
    if kind == "global":
        return document.get("providers", {}).get("local")
    registry = document.get("local_hosts" if kind == "host" else "local_seats", {})
    return registry.get(target_id) if isinstance(registry, Mapping) else None


@dataclass(frozen=True)
class LocalActionPreview:
    """Immutable local-registry preview bound to a full settings digest."""

    action: str
    kind: str  # "global", "host" or "seat"
    target_id: str | None
    changed: bool
    enabling: bool  # the write makes local execution less restrictive
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    warnings: tuple[str, ...]
    expected_digest: str
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "before", _freeze(self.before))
        object.__setattr__(self, "after", _freeze(self.after))
        object.__setattr__(self, "options", _freeze(self.options))

    @property
    def entry_before(self) -> Any:
        """The affected host/seat/provider entry before the write (thawed copy)."""
        return _thaw(_entry(self.before, self.kind, self.target_id))

    @property
    def entry_after(self) -> Any:
        return _thaw(_entry(self.after, self.kind, self.target_id))

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "kind": self.kind,
            "target_id": self.target_id,
            "changed": self.changed,
            "enabling": self.enabling,
            "entry_before": self.entry_before,
            "entry_after": self.entry_after,
            "warnings": list(self.warnings),
            "expected_digest": self.expected_digest,
            "options": _thaw(self.options),
        }


@dataclass(frozen=True)
class LocalMutationResult:
    """Outcome of one committed local write."""

    action: str
    kind: str
    target_id: str | None
    changed: bool
    enabling: bool
    before_digest: str
    after_digest: str
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"warnings": list(self.warnings)}


class LocalSeatManager:
    """Serialize guarded local host/seat management for one trusted session."""

    def __init__(
        self,
        root: Path,
        *,
        refresher: Any | None = None,
        clock: Callable[[], float] = time.time,
        settings_loader: Callable[[], tuple[dict[str, Any], list[str]]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.refresher = refresher
        self._clock = clock
        self._settings_loader = settings_loader
        self._lock = threading.RLock()
        self._closed = False
        self.settings: dict[str, Any] | None = None
        self.warnings: list[str] = []

    # -- inspection ---------------------------------------------------------
    def report(
        self,
        *,
        now: float | None = None,
        background_jobs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Structured, read-only view of the global mode and every host/seat.

        Uses the cached availability only (never probes) and the live probe
        snapshot when a refresher is attached. Each host and seat also carries
        a ``status`` row from the dashboard's shared derivation
        (:func:`display._local_status`): off, stale with the last known state
        and age, available/ready, gaming, busy, loading, asleep, degraded,
        unknown, or working when a running local job is attributed to the
        seat. ``background_jobs`` defaults to the validated live-job snapshot
        (read-only queue artifacts); a dispatch read failure leaves every seat
        non-working and is reported in ``jobs_error``.
        """
        settings, warnings, digest = self._read()
        current = self._clock() if now is None else now
        try:
            status = local_seats.status(self.root, settings, now=current)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ManagerError(str(exc)) from exc
        jobs_error: str | None = None
        if background_jobs is None:
            try:
                background_jobs = dispatch_client.live_jobs(self.root, settings, now=current)
            except (DispatchError, OSError, ValueError) as exc:
                background_jobs = []
                jobs_error = str(exc)
        try:
            derived = display._local_status(self.root, settings, background_jobs, at=current)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ManagerError(str(exc)) from exc
        seat_status = {row["seat"]: row for row in derived["seats"]}
        snapshot = self.local_snapshot()
        probe_hosts = snapshot.hosts if snapshot is not None else {}
        eligibility = {seat["seat"]: seat for seat in status["seats"]}
        hosts: dict[str, Any] = {}
        for host_id, host in settings.get("local_hosts", {}).items():
            hosts[host_id] = {
                "config": deepcopy(host),
                "availability": status["hosts"].get(host_id),
                "status": derived["hosts"].get(host_id),
                "probe": deepcopy(probe_hosts.get(host_id)),
                "seats": sorted(
                    seat_id for seat_id, seat in settings.get("local_seats", {}).items()
                    if seat.get("host") == host_id
                ),
            }
        seats: dict[str, Any] = {}
        for seat_id, seat in settings.get("local_seats", {}).items():
            seats[seat_id] = {
                "config": deepcopy(seat),
                "effective_mode": local_seats.effective_mode(settings, seat),
                "eligibility": eligibility.get(seat_id),
                "status": seat_status.get(seat_id),
            }
        return {
            "global_mode": status["global_mode"],
            "provider": deepcopy(settings.get("providers", {}).get("local", {})),
            "hosts": hosts,
            "seats": seats,
            "warnings": list(warnings),
            "settings_digest": digest,
            "probe": dataclasses.asdict(snapshot) if snapshot is not None else None,
            "jobs_error": jobs_error,
        }

    def host_report(self, host_id: str, *, now: float | None = None) -> dict[str, Any]:
        try:
            return self.report(now=now)["hosts"][host_id]
        except KeyError as exc:
            raise ManagerError(f"unknown local host {host_id!r}") from exc

    def seat_report(self, seat_id: str, *, now: float | None = None) -> dict[str, Any]:
        try:
            return self.report(now=now)["seats"][seat_id]
        except KeyError as exc:
            raise ManagerError(f"unknown local seat {seat_id!r}") from exc

    # -- probes -------------------------------------------------------------
    def local_snapshot(self) -> Any | None:
        """The refresher's ``LocalProbeSnapshot``; ``None`` without a refresher."""
        if self.refresher is None:
            return None
        return self.refresher.local_snapshot()

    def request_probe(self, host_id: str) -> str:
        """Queue one coalesced read-only probe through the watch refresher.

        The probe runs on the refresher's worker, never here. Requires the same
        eligibility as a write so an excluded context cannot trigger the
        controller-authored probe argv.
        """
        with self._lock:
            self._ensure_open()
            self._authorize("local probe request")
            if self.refresher is None:
                raise ManagerError("no active refresher is available for local probes")
            settings, _warnings, _digest = self._read()
            if host_id not in settings.get("local_hosts", {}):
                raise ManagerError(f"unknown local host {host_id!r}")
            try:
                self.refresher.request_local_probe(host_id)
            except RuntimeError as exc:
                raise ManagerError(str(exc)) from exc
            return host_id

    # -- previews -----------------------------------------------------------
    def preview(self, action: str, target_id: str | None = None, **option: Any) -> LocalActionPreview:
        """Return an immutable, digest-pinned preview; nothing is written."""
        if action not in LOCAL_ACTIONS:
            raise ManagerError(f"action {action!r} is not a local management action")
        try:
            result = local_cli.preview_action(self.root, action, target_id, **option)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ManagerError(str(exc)) from exc
        return LocalActionPreview(
            action=action,
            kind=_kind(action),
            target_id=target_id,
            changed=bool(result["changed"]),
            enabling=bool(result["enabling"]),
            before=result["before"],
            after=result["after"],
            warnings=tuple(result["warnings"]),
            expected_digest=result["before_digest"],
            options=option,
        )

    def preview_global_mode(self, mode: str) -> LocalActionPreview:
        return self.preview("set-global-mode", None, mode=mode)

    def preview_seat_mode(self, seat_id: str, mode: str) -> LocalActionPreview:
        return self.preview("set-seat-mode", seat_id, mode=mode)

    def preview_add_host(self, host_id: str, config: Mapping[str, Any]) -> LocalActionPreview:
        return self.preview("add-host", host_id, config=dict(config))

    def preview_add_seat(self, seat_id: str, config: Mapping[str, Any]) -> LocalActionPreview:
        return self.preview("add-seat", seat_id, config=dict(config))

    def preview_edit_host(self, host_id: str, changes: Mapping[str, Any]) -> LocalActionPreview:
        return self.preview("edit-host", host_id, changes=dict(changes))

    def preview_edit_seat(self, seat_id: str, changes: Mapping[str, Any]) -> LocalActionPreview:
        return self.preview("edit-seat", seat_id, changes=dict(changes))

    def preview_host_lifecycle(self, host_id: str, lifecycle: str) -> LocalActionPreview:
        return self.preview("set-host-lifecycle", host_id, lifecycle=lifecycle)

    def preview_seat_lifecycle(self, seat_id: str, lifecycle: str) -> LocalActionPreview:
        return self.preview("set-seat-lifecycle", seat_id, lifecycle=lifecycle)

    def preview_remove_seat(self, seat_id: str) -> LocalActionPreview:
        """Preview retiring an inactive, off seat with no running local job.

        The entry stays as a retired tombstone (id, configuration and history
        are kept) and the transaction backs up the prior settings; an
        already retired seat previews as unchanged.
        """
        return self.preview("remove-seat", seat_id)

    # -- commit -------------------------------------------------------------
    def commit(self, preview: LocalActionPreview, *, from_current_user_request: bool) -> LocalMutationResult:
        """Apply a previewed local write with the explicit confirmation marker.

        Refused after close, for managed workers, and without
        ``from_current_user_request=True`` from the UI confirmation. The write
        is pinned to ``preview.expected_digest``; any settings change since the
        preview raises :class:`StalePreviewError` with nothing written.
        """
        with self._lock:
            self._ensure_open()
            if from_current_user_request is not True:
                raise ManagerError(
                    f"local {preview.action} requires explicit confirmation of the current request"
                )
            self._authorize(f"local {preview.action}")
            self._verify_current(preview)
            try:
                result = local_cli.apply_action(
                    self.root, preview.action, preview.target_id,
                    expected_digest=preview.expected_digest,
                    from_current_user_request=True,
                    authority=local_cli.MANAGER_AUTHORITY,
                    **_thaw(preview.options),
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise self._translate(exc) from exc
            self.reload()
            return LocalMutationResult(
                action=preview.action,
                kind=preview.kind,
                target_id=preview.target_id,
                changed=bool(result["changed"]),
                enabling=bool(result["enabling"]),
                before_digest=result["before_digest"],
                after_digest=result["after_digest"],
                warnings=tuple(result["warnings"]),
            )

    # -- lifecycle ----------------------------------------------------------
    def reload(self) -> dict[str, Any]:
        """Reload settings after a write and keep the refresher's view in step."""
        if self._settings_loader is not None:
            settings, warnings = self._settings_loader()
        else:
            settings, warnings, _digest = self._read()
        self.settings = settings
        self.warnings = list(warnings)
        if self.refresher is not None:
            self.refresher.settings = settings
        return settings

    def replace_refresher(self, refresher: Any | None) -> None:
        with self._lock:
            self._ensure_open()
            self.refresher = refresher
            if refresher is not None and self.settings is not None:
                refresher.settings = self.settings

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # -- internals ----------------------------------------------------------
    def _ensure_open(self) -> None:
        if self._closed:
            raise ManagerClosedError("the local seat manager has closed and cannot mutate settings")

    @staticmethod
    def _authorize(action: str) -> None:
        """Distinct manager authority: anything but a managed worker."""
        try:
            ensure_not_managed_worker()
        except ManagerEligibilityError as exc:
            raise ManagerEligibilityError(f"{action} refused: {exc}") from exc

    def _read(self) -> tuple[dict[str, Any], list[str], str]:
        try:
            settings, warnings, digest = local_cli.load_local_settings(self.root)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ManagerError(str(exc)) from exc
        self.settings = settings
        self.warnings = list(warnings)
        return settings, warnings, digest

    def _verify_current(self, preview: LocalActionPreview) -> None:
        """Friendly pre-check; the authoritative digest check runs inside the lock."""
        _settings, _warnings, digest = self._read()
        if digest != preview.expected_digest:
            raise StalePreviewError(
                "settings changed since this local action was previewed; re-preview before applying"
            )

    @staticmethod
    def _translate(exc: Exception) -> ManagerError:
        text = str(exc)
        if "digest changed" in text:
            return StalePreviewError(f"settings changed since this local action was previewed: {text}")
        if " refused: " in text:  # local_cli's manager-authority refusal
            return ManagerEligibilityError(text)
        return ManagerError(text)
