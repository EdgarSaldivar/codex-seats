"""Guarded state/action service for the interactive seat management view.

This module is the seam between the terminal dashboard (worker B) and the
existing guarded lifecycle layer. It never writes settings JSON directly, never
opens an auth file, and never invokes a provider while holding the settings or
ledger locks. Every mutation flows through :mod:`seat_admin`, which re-reads and
digest-pins the settings inside its own lock before any ledger effect.

Design contract
---------------
* **Immutable previews.** :class:`ActionPreview` freezes the stable target id,
  the requested values, the exact rendered before/after, and the full
  settings-byte ``expected_digest`` at preview time. ``commit`` replays that
  digest so a concurrent settings change since the preview aborts cleanly with
  nothing written (raising :class:`StalePreviewError`).
* **Session-scoped pending drains.** Pausing a busy seat initiates the existing
  drain transaction (lifecycle -> ``draining``) and records a
  :class:`PendingDrain` owned by *this* manager. Idle completion is reconciled
  through the guarded action layer only while the manager is alive.
  :meth:`cancel_pending_drain` leaves the seat draining; :meth:`close` never
  auto-completes a drain. No uncancelable waiter runs behind the UI.
* **Nonblocking activation.** :meth:`run_activation_checks` performs the login
  status and fresh usage probe off the main loop with a cancel event, so the UI
  stays responsive; :meth:`commit_activation` applies the confirmed activation
  on the main loop with current runtime validation.
* **Eligibility.** :func:`ensure_management_eligible` requires a real
  stdin/stdout TTY and excludes controller/head and managed-worker contexts.

Public API (for worker B)
--------------------------
* ``ensure_management_eligible(*, stdin, stdout, env)``
* ``SeatManager(root, settings, *, refresher=None, clock=time.time)``
* ``manager.list_report(include_all=True)`` / ``manager.detail_report(selector)``
* ``manager.identity_report(selector)`` (manager-only cached provider identity)
* ``manager.preview(action, selector=None, **options) -> ActionPreview``
* ``manager.preview_add(...)`` / ``manager.preview_remove(selector)``
* ``manager.preview_pause(selector) -> PausePlan``
* ``manager.commit(preview) -> SeatMutationResult``
* ``manager.commit_pause(plan) -> PauseResult``
* ``manager.pending_drains()`` / ``manager.reconcile_pending_drains()``
  / ``manager.cancel_pending_drain(seat_id)``
* ``manager.run_activation_checks(preview, cancel_event) -> ActivationChecks``
* ``manager.commit_activation(preview, checks) -> SeatMutationResult``
* ``manager.preview_login(selector)`` / ``manager.validate_login_plan(plan)``
* ``manager.request_refresh(selector)`` / ``manager.refresh_state()``
* ``manager.replace_refresher(refresher)`` / ``manager.reload()``
* ``manager.local_manager() -> local_manager.LocalSeatManager`` (local
  hosts/seats: structured reports, digest-pinned previews, confirmed writes and
  refresher-queued probes under the same eligibility rules)
* ``manager.close() -> tuple[PendingDrain, ...]``
"""

from __future__ import annotations

import os
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .controller_context import current_controller
from .seat_admin import SeatAdminError, SeatMutationResult
from .policy_settings import PolicyError, PolicyPreview, apply_policy, policy_report, preview_policy


class ManagerError(Exception):
    """A management request cannot be satisfied safely."""


class ManagerEligibilityError(ManagerError):
    """The current context may not open interactive seat management."""


class StalePreviewError(ManagerError):
    """The settings changed since the preview; the caller must re-preview."""


class ManagerClosedError(ManagerError):
    """The manager has shut down and can no longer mutate settings."""


def _freeze(value: Any) -> Any:
    """Return an immutable, caller-detached representation of preview input."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, frozenset)):
        return [_thaw(item) for item in value]
    return deepcopy(value)


_MANAGE_ACTIONS = frozenset(
    {"add", "set", "set-role", "activate", "drain", "deactivate", "retire"}
)
_IDLE_REQUIRED = frozenset({"activate", "deactivate", "retire"})


@dataclass(frozen=True)
class ActionPreview:
    """Immutable, stable-target preview bound to a full settings digest."""

    action: str
    seat_id: str
    changed: bool
    before: str
    after: str
    warnings: tuple[str, ...]
    expected_digest: str
    # Frozen requested values exactly as previewed; commit confirms only these.
    options: Mapping[str, Any] = field(default_factory=dict)
    # The lifecycle/role snapshot the preview was shown against, so commit can
    # give a friendly stale error before it even attempts the guarded write.
    observed_lifecycle: str | None = None
    observed_role: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _freeze(self.options))

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "seat_id": self.seat_id,
            "changed": self.changed,
            "before": self.before,
            "after": self.after,
            "warnings": list(self.warnings),
            "expected_digest": self.expected_digest,
            "options": _thaw(self.options),
            "observed_lifecycle": self.observed_lifecycle,
            "observed_role": self.observed_role,
        }


@dataclass(frozen=True)
class PausePlan:
    """Preview of a pause request, distinguishing an idle stop from a drain."""

    seat_id: str
    busy: bool
    mode: str  # "deactivate" (idle) or "drain" (busy)
    preview: ActionPreview


@dataclass(frozen=True)
class PauseResult:
    """Outcome of committing a pause; a busy pause yields a pending drain."""

    seat_id: str
    result: SeatMutationResult
    pending_drain: "PendingDrain | None"


@dataclass(frozen=True)
class PendingDrain:
    """A drain this session initiated; reconciled to idle while alive."""

    seat_id: str
    started_at: float
    # "draining": awaiting idle; "deactivated": reconciled to inactive by this
    # session; "canceled": user canceled the wait, the seat remains draining.
    state: str
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.state == "deactivated"

    def as_dict(self) -> dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "started_at": self.started_at,
            "state": self.state,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ActivationChecks:
    """Result of the off-loop, cancelable activation checks for one seat."""

    seat_id: str
    home: Path | None
    passed: bool
    verified_login: bool
    successful_probe: bool
    canceled: bool
    detail: str
    expected_digest: str | None = None
    as_primary: bool = False


@dataclass(frozen=True)
class LoginPlan:
    """Confirmed stable target and settings snapshot for a login handoff."""

    seat_id: str
    home: Path
    expected_digest: str
    settings: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "settings", _freeze(self.settings))

    def confirmed_settings(self) -> dict[str, Any]:
        value = _thaw(self.settings)
        if not isinstance(value, dict):  # defensive; settings is always a mapping
            raise ManagerError("confirmed login settings snapshot is malformed")
        return value


def ensure_management_eligible(
    *,
    stdin: Any | None = None,
    stdout: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Fail closed unless a real human terminal outside excluded contexts.

    Requires an interactive stdin/stdout TTY and rejects controller/head and
    managed-worker contexts. Normal captured (non-TTY) watch never calls this,
    so its behavior is unchanged; an explicit management request in an excluded
    context fails clearly here.
    """
    stream_in = sys.stdin if stdin is None else stdin
    stream_out = sys.stdout if stdout is None else stdout
    if not (
        hasattr(stream_in, "isatty")
        and stream_in.isatty()
        and hasattr(stream_out, "isatty")
        and stream_out.isatty()
    ):
        raise ManagerEligibilityError(
            "interactive seat management requires a real stdin/stdout terminal"
        )
    source = os.environ if env is None else env
    context = current_controller(source)
    if context.managed_worker:
        raise ManagerEligibilityError(
            "managed workers cannot open interactive seat management"
        )
    declared_head = (source.get("CODEX_SEATS_HEAD") or "").lower()
    if context.is_controller or declared_head in {"codex", "claude"}:
        raise ManagerEligibilityError(
            "controller/head contexts cannot open interactive seat management"
        )


class SeatManager:
    """Serialize management mutations and reconcile session-scoped drains."""

    def __init__(
        self,
        root: Path,
        settings: dict[str, Any],
        *,
        refresher: Any | None = None,
        clock: Callable[[], float] = time.time,
        settings_loader: Callable[[], tuple[dict[str, Any], list[str]]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.settings = settings
        self.refresher = refresher
        self._clock = clock
        self._settings_loader = settings_loader
        # Serializes management mutations against each other and against close.
        self._lock = threading.RLock()
        self._closed = False
        self._pending: dict[str, PendingDrain] = {}
        self._local: Any | None = None
        self._claude: Any | None = None

    # -- inspection ---------------------------------------------------------
    def list_report(self, *, include_all: bool = True) -> dict[str, Any]:
        """Structured list of registered seats, including inactive/retired."""
        from . import seats_cli

        return seats_cli.seats_list_report(
            self.root, self.settings, include_all=include_all
        )

    def detail_report(self, selector: str) -> dict[str, Any]:
        """Structured detail for one selected stable seat."""
        from . import seats_cli

        try:
            return seats_cli.seats_show_report(self.root, self.settings, selector)
        except seats_cli.SeatsCLIError as exc:
            raise ManagerError(str(exc)) from exc

    def identity_report(self, selector: str) -> dict[str, Any]:
        """Allowlisted cached identity for the interactive selected-seat view."""
        from . import seats_cli
        from .account import manager_identity_report

        seat_id = self._resolve_stable(selector, seats_cli)
        return manager_identity_report(self.root, self.settings, seat_id)

    def global_settings_report(self) -> dict[str, Any]:
        """Return the current validated preferences, including defaults."""
        try:
            settings, _warnings = self._load_settings()
            return policy_report(settings)
        except (PolicyError, ValueError, OSError) as exc:
            raise ManagerError(str(exc)) from exc

    def preview_global_settings(self, changes: Mapping[str, Any]) -> PolicyPreview:
        self._ensure_open()
        try:
            return preview_policy(self.root, changes)
        except (PolicyError, ValueError, OSError) as exc:
            raise ManagerError(str(exc)) from exc

    def commit_global_settings(self, preview: PolicyPreview) -> bool:
        with self._lock:
            self._ensure_open()
            ensure_management_eligible()
            try:
                changed = apply_policy(self.root, preview)
            except (PolicyError, ValueError, OSError) as exc:
                if "stale" in str(exc).lower():
                    raise StalePreviewError(str(exc)) from exc
                raise ManagerError(str(exc)) from exc
            self.reload()
            return changed

    # -- previews -----------------------------------------------------------
    def preview(
        self, action: str, selector: str | None = None, **options: Any
    ) -> ActionPreview:
        """Return an immutable, digest-pinned preview for one management action."""
        from . import seat_admin, seats_cli

        if action not in _MANAGE_ACTIONS:
            raise ManagerError(f"action {action!r} is not a management action")
        seat_id = None if action == "add" else self._resolve_stable(selector, seats_cli)
        plan_options = dict(options)
        if action == "activate":
            # Previews assert the evidence that commit supplies after the real
            # login-status/probe; the digest still pins the settings revision.
            plan_options.setdefault("verified_login", True)
            plan_options.setdefault("successful_probe", True)
        try:
            result = seat_admin.plan_mutation(
                self.root, action, seat_id, **plan_options
            )
        except (SeatAdminError, ValueError) as exc:
            raise ManagerError(str(exc)) from exc
        if result.digest is None:  # defensive: previews always read settings
            raise ManagerError("preview did not produce a settings digest")
        return self._preview_from_result(action, result, options)

    def _preview_from_result(
        self,
        action: str,
        result: SeatMutationResult,
        options: Mapping[str, Any],
    ) -> ActionPreview:
        from .seat_registry import derive_roles

        seat_id = result.seat_id
        entry = self.settings.get("pool", {}).get("seats", {}).get(seat_id, {})
        lifecycle = entry.get("lifecycle") if isinstance(entry, Mapping) else None
        role = derive_roles(self.settings).get(seat_id)
        return ActionPreview(
            action=action,
            seat_id=seat_id,
            changed=result.changed,
            before=result.before,
            after=result.after,
            warnings=tuple(result.warnings),
            expected_digest=result.digest or "",
            options=_freeze(options),
            observed_lifecycle=lifecycle,
            observed_role=role,
        )

    def preview_set(self, selector: str, **fields: Any) -> ActionPreview:
        return self.preview("set", selector, **fields)

    def preview_add(
        self, *, label: str, home: str | None = None, profile: str | None = None
    ) -> ActionPreview:
        """Preview one dynamically allocated, initially inactive identity."""
        return self.preview("add", None, label=label, home=home, profile=profile)

    def preview_remove(self, selector: str) -> ActionPreview:
        """Preview retirement; the stable identity and its history are retained."""
        return self.preview("retire", selector)

    def preview_set_role(self, selector: str, role: str) -> ActionPreview:
        return self.preview("set-role", selector, role=role)

    def preview_pause(self, selector: str) -> PausePlan:
        """Preview pausing a seat: deactivate if idle, otherwise drain."""
        from . import seats_cli

        seat_id = self._resolve_stable(selector, seats_cli)
        busy = self._seat_is_busy(seat_id)
        mode = "drain" if busy else "deactivate"
        preview = self.preview(mode, seat_id)
        return PausePlan(seat_id=seat_id, busy=busy, mode=mode, preview=preview)

    def preview_reactivate(self, selector: str, *, as_primary: bool = False) -> ActionPreview:
        return self.preview("activate", selector, as_primary=as_primary)

    def preview_login(self, selector: str) -> LoginPlan:
        """Freeze the exact settings/home identity shown for login confirmation."""
        from . import seat_admin, seats_cli
        from .seat_registry import resolve_home

        self._ensure_open()
        try:
            before = seat_admin.settings_digest(self.root)
            settings, _warnings = self._load_settings()
            after = seat_admin.settings_digest(self.root)
        except (OSError, ValueError, SeatAdminError) as exc:
            raise ManagerError(str(exc)) from exc
        if before != after:
            raise StalePreviewError(
                "settings changed while preparing login; select the action again"
            )
        try:
            seat_id = seats_cli._stable_selector(selector, settings)
            home = resolve_home(
                self.root, seat_id, settings["pool"]["seats"][seat_id]
            )
        except (seats_cli.SeatsCLIError, KeyError, ValueError) as exc:
            raise ManagerError(str(exc)) from exc
        return LoginPlan(
            seat_id=seat_id,
            home=home,
            expected_digest=after,
            settings=settings,
        )

    def validate_login_plan(self, plan: LoginPlan) -> dict[str, Any]:
        """Revalidate a confirmed login snapshot after provider quiescence.

        No settings or ledger lock is retained after this method returns; the
        interactive login receives the immutable snapshot the user confirmed.
        """
        from . import seat_admin, seats_cli
        from .seat_registry import resolve_home

        self._ensure_open()
        self._mutation_guard(seats_cli)
        try:
            if seat_admin.settings_digest(self.root) != plan.expected_digest:
                raise StalePreviewError(
                    "settings/home changed after login confirmation; confirm again"
                )
            current, _warnings = self._load_settings()
            if seat_admin.settings_digest(self.root) != plan.expected_digest:
                raise StalePreviewError(
                    "settings/home changed during login handoff; confirm again"
                )
            seat_id = seats_cli._stable_selector(plan.seat_id, current)
            home = resolve_home(
                self.root, seat_id, current["pool"]["seats"][seat_id]
            )
        except (OSError, SeatAdminError) as exc:
            raise ManagerError(str(exc)) from exc
        except (seats_cli.SeatsCLIError, KeyError, ValueError) as exc:
            raise StalePreviewError(
                f"confirmed login identity is no longer valid: {exc}"
            ) from exc
        confirmed = plan.confirmed_settings()
        if current != confirmed or home != plan.home:
            raise StalePreviewError(
                "settings/home no longer match the confirmed login snapshot; confirm again"
            )
        return confirmed

    # -- commit -------------------------------------------------------------
    def commit(self, preview: ActionPreview) -> SeatMutationResult:
        """Apply a previewed action on the main loop with runtime validation.

        The action is refused after close, guarded against controller/head and
        managed-worker contexts, and pinned to ``preview.expected_digest`` so a
        settings change since the preview raises :class:`StalePreviewError`
        with nothing written.
        """
        from . import seat_admin, seats_cli

        with self._lock:
            self._ensure_open()
            self._mutation_guard(seats_cli)
            self._verify_current(preview)
            options = dict(preview.options)
            try:
                if preview.action == "set":
                    result = seat_admin.set_seat(
                        self.root, preview.seat_id,
                        expected_digest=preview.expected_digest, **options,
                    )
                elif preview.action == "set-role":
                    result = seat_admin.set_role(
                        self.root, preview.seat_id, options["role"],
                        expected_digest=preview.expected_digest,
                    )
                elif preview.action == "drain":
                    result = seat_admin.drain_seat(
                        self.root, preview.seat_id,
                        allow_empty=bool(options.get("allow_empty", False)),
                        expected_digest=preview.expected_digest,
                    )
                elif preview.action == "deactivate":
                    result = seat_admin.deactivate_seat(
                        self.root, preview.seat_id,
                        allow_empty=bool(options.get("allow_empty", False)),
                        expected_digest=preview.expected_digest,
                    )
                elif preview.action == "add":
                    result = seat_admin.add_seat(
                        self.root,
                        label=options["label"],
                        home=options.get("home"),
                        profile=options.get("profile"),
                        expected_digest=preview.expected_digest,
                        expected_seat_id=preview.seat_id,
                    )
                elif preview.action == "retire":
                    result = seat_admin.remove_seat(
                        self.root,
                        preview.seat_id,
                        expected_digest=preview.expected_digest,
                    )
                else:
                    raise ManagerError(
                        f"action {preview.action!r} cannot be committed directly"
                    )
            except SeatAdminError as exc:
                raise self._translate(exc)
            self.reload()
            return result

    def commit_pause(self, plan: PausePlan) -> PauseResult:
        """Commit a pause; a busy pause records a session-scoped pending drain."""
        with self._lock:
            result = self.commit(plan.preview)
            pending: PendingDrain | None = None
            if plan.busy:
                pending = PendingDrain(
                    seat_id=plan.seat_id,
                    started_at=self._clock(),
                    state="draining",
                    detail="draining; awaiting idle before deactivation",
                )
                self._pending[plan.seat_id] = pending
            return PauseResult(
                seat_id=plan.seat_id, result=result, pending_drain=pending
            )

    # -- pending drains -----------------------------------------------------
    def pending_drains(self) -> tuple[PendingDrain, ...]:
        with self._lock:
            return tuple(self._pending.values())

    def cancel_pending_drain(self, seat_id: str) -> PendingDrain:
        """Stop waiting on a drain; the seat honestly remains draining."""
        with self._lock:
            current = self._pending.get(seat_id)
            if current is None:
                raise ManagerError(f"no pending drain for {seat_id}")
            canceled = PendingDrain(
                seat_id=seat_id,
                started_at=current.started_at,
                state="canceled",
                detail="wait canceled; seat remains draining and can be "
                "deactivated once idle",
            )
            self._pending[seat_id] = canceled
            return canceled

    def reconcile_pending_drains(self) -> tuple[PendingDrain, ...]:
        """Deactivate this session's drained seats once idle, if still alive.

        Canceled drains are left untouched (the seat stays draining). A drain
        whose seat is no longer draining externally is dropped from tracking
        without a mutation. Never auto-completes a drain after close.
        """
        from . import seat_admin, seats_cli

        with self._lock:
            if self._closed:
                return tuple(self._pending.values())
            for seat_id, pending in list(self._pending.items()):
                if pending.state != "draining":
                    continue
                self._mutation_guard(seats_cli)
                try:
                    observed = seat_admin.plan_mutation(
                        self.root, "deactivate", seat_id
                    )
                except SeatAdminError as exc:
                    # A lifecycle change or malformed/currently unreadable state
                    # is never reconciled using an earlier observation.
                    if self._current_lifecycle(seat_id) != "draining":
                        del self._pending[seat_id]
                        continue
                    self._pending[seat_id] = PendingDrain(
                        seat_id=seat_id,
                        started_at=pending.started_at,
                        state="draining",
                        detail=f"deactivation deferred: {exc}",
                    )
                    continue
                if not observed.digest:
                    self._pending[seat_id] = PendingDrain(
                        seat_id=seat_id,
                        started_at=pending.started_at,
                        state="draining",
                        detail="deactivation deferred: no settings revision",
                    )
                    continue
                if self._current_lifecycle(seat_id) != "draining":
                    # Externally resolved (deactivated/reactivated/removed): stop
                    # tracking without mutating.
                    del self._pending[seat_id]
                    continue
                if self._seat_is_busy(seat_id):
                    continue  # still working; keep waiting
                try:
                    result = seat_admin.deactivate_seat(
                        self.root, seat_id, expected_digest=observed.digest
                    )
                except SeatAdminError as exc:
                    self._pending[seat_id] = PendingDrain(
                        seat_id=seat_id,
                        started_at=pending.started_at,
                        state="draining",
                        detail=f"deactivation deferred: {exc}",
                    )
                    continue
                self._pending[seat_id] = PendingDrain(
                    seat_id=seat_id,
                    started_at=pending.started_at,
                    state="deactivated",
                    detail="reconciled to inactive after draining to idle"
                    if result.changed
                    else "already inactive",
                )
            self.reload()
            return tuple(self._pending.values())

    # -- activation (nonblocking, cancelable) -------------------------------
    def run_activation_checks(
        self, preview: ActionPreview, cancel_event: threading.Event
    ) -> ActivationChecks:
        """Run login-status + fresh usage probe off the main loop, cancelably.

        Holds no settings/ledger lock across the provider I/O. Returns a
        structured result the caller confirms before :meth:`commit_activation`.
        Failed or canceled checks leave the seat inactive.
        """
        from . import seat_admin, seats_cli, watch
        from .seat_registry import resolve_home

        if preview.action != "activate":
            raise ManagerError("activation checks require an activate preview")
        self._ensure_open()
        self._mutation_guard(seats_cli)
        self._verify_current(preview)
        seat_id = preview.seat_id
        digest = preview.expected_digest
        home: Path | None = None

        def failed(detail: str, *, canceled: bool = False) -> ActivationChecks:
            return ActivationChecks(
                seat_id=seat_id, home=home, passed=False,
                verified_login=False, successful_probe=False,
                canceled=canceled, detail=detail, expected_digest=digest,
            )

        descriptor = watch.acquire_refresh_lock(
            self.root, cancel_event, timeout=5.0
        )
        if descriptor is None:
            return failed(
                "canceled while waiting for the shared refresh lock"
                if cancel_event.is_set()
                else "shared refresh remained busy; retry activation",
                canceled=cancel_event.is_set(),
            )
        try:
            # Reload only after contention has ended and fail closed. The exact
            # original preview digest must still identify this revision.
            settings, _warnings = self._load_settings()
            if seat_admin.settings_digest(self.root) != digest:
                return failed("settings changed since activation preview; re-preview")
            try:
                resolved = seats_cli._stable_selector(seat_id, settings)
            except seats_cli.SeatsCLIError as exc:
                return failed(str(exc))
            if resolved != seat_id:
                return failed("activation target changed; re-preview")
            entry = settings["pool"]["seats"][seat_id]
            home = resolve_home(self.root, seat_id, entry)
            facts = seats_cli._directory_facts(home)
            if not (
                facts["exists"] and facts["directory"] and not facts["symlink"]
                and facts["owner_ok"] and facts["mode_ok"]
            ):
                return failed(
                    f"activation home for {seat_id} must be an owner-controlled, "
                    "non-symlink directory"
                )
            if cancel_event.is_set():
                return failed("canceled before login status", canceled=True)
            try:
                codex, _version = seats_cli._codex_login_support(exact_version=True)
            except seats_cli.SeatsCLIError as exc:
                return failed(str(exc))
            if not seats_cli._login_status(codex, home):
                return failed(f"`codex login status` did not authenticate {seat_id}")
            if cancel_event.is_set():
                return failed("canceled before usage probe", canceled=True)
            if not self._cancelable_probe(settings, seat_id, cancel_event):
                return failed(
                    "fresh account/capacity probe did not produce an authoritative window"
                )
            if cancel_event.is_set():
                return failed("canceled during usage probe", canceled=True)
            if seat_admin.settings_digest(self.root) != digest:
                return failed("settings changed during activation checks; re-preview")
            return ActivationChecks(
                seat_id=seat_id, home=home, passed=True,
                verified_login=True, successful_probe=True,
                canceled=False, detail="login and fresh usage probe succeeded",
                expected_digest=digest,
                as_primary=bool(preview.options.get("as_primary", False)),
            )
        except (OSError, ValueError, SeatAdminError) as exc:
            return failed(f"activation checks failed closed: {exc}")
        finally:
            watch.release_refresh_lock(descriptor)

    def commit_activation(
        self, preview: ActionPreview, checks: ActivationChecks
    ) -> SeatMutationResult:
        """Apply a confirmed activation on the main loop with runtime validation."""
        from . import seat_admin, seats_cli

        with self._lock:
            self._ensure_open()
            self._mutation_guard(seats_cli)
            if preview.action != "activate":
                raise ManagerError("activation commit requires an activate preview")
            self._verify_current(preview)
            if not checks.passed:
                raise ManagerError(
                    "activation checks did not pass; the seat stays inactive"
                )
            if (
                checks.seat_id != preview.seat_id
                or checks.expected_digest != preview.expected_digest
                or checks.as_primary != bool(preview.options.get("as_primary", False))
            ):
                raise StalePreviewError(
                    "activation checks do not match the confirmed preview; re-preview"
                )
            try:
                result = seat_admin.activate_seat(
                    self.root, checks.seat_id,
                    verified_login=True, successful_probe=True,
                    verified_home=checks.home, as_primary=checks.as_primary,
                    expected_digest=preview.expected_digest,
                )
            except SeatAdminError as exc:
                raise self._translate(exc)
            self.reload()
            return result

    # -- refresh ------------------------------------------------------------
    def request_refresh(self, selector: str) -> str:
        """Queue a coalesced Codex-only refresh for one selected seat."""
        from . import seats_cli

        self._ensure_open()
        if self.refresher is None:
            raise ManagerError("no active refresher is available")
        self._mutation_guard(seats_cli)
        seat_id = self._resolve_stable(selector, seats_cli)
        self.refresher.request_selected_refresh(seat_id)
        return seat_id

    def refresh_state(self) -> Any:
        if self.refresher is None:
            return None
        return self.refresher.selected_snapshot()

    def replace_refresher(self, refresher: Any | None) -> None:
        """Attach a newly constructed refresher after a quiesced TTY handoff."""
        with self._lock:
            self._ensure_open()
            self.refresher = refresher
            if refresher is not None:
                refresher.settings = self.settings
            if self._local is not None:
                self._local.replace_refresher(refresher)

    # -- local hosts and seats ----------------------------------------------
    def local_manager(self) -> Any:
        """The session's :class:`local_manager.LocalSeatManager`, created once.

        It shares this manager's root, refresher and clock, so local probe
        requests go through the same ``WatchRefresher.request_local_probe``
        and its writes obey the same trusted-terminal eligibility.
        """
        from .local_manager import LocalSeatManager

        with self._lock:
            self._ensure_open()
            if self._local is None:
                self._local = LocalSeatManager(
                    self.root, refresher=self.refresher, clock=self._clock,
                    settings_loader=self._reload_local_settings,
                )
            return self._local

    def claude_manager(self) -> Any:
        """The session's guarded Claude account manager, created once."""
        from .claude_manager import ClaudeAccountManager

        with self._lock:
            self._ensure_open()
            if self._claude is None:
                self._claude = ClaudeAccountManager(self.root, clock=self._clock)
            return self._claude

    def _reload_local_settings(self) -> tuple[dict[str, Any], list[str]]:
        """Keep the overview and local manager on the same settings revision."""
        settings, warnings = self._load_settings()
        self.settings = settings
        if self.refresher is not None:
            self.refresher.settings = settings
        return settings, warnings

    # -- lifecycle ----------------------------------------------------------
    def reload(self) -> dict[str, Any]:
        """Reload settings after a mutation and replace stale references."""
        settings, _warnings = self._load_settings()
        self.settings = settings
        if self.refresher is not None:
            # Keep the periodic refresher's targeting in step with new
            # lifecycle without recreating it here (worker B owns recreation).
            self.refresher.settings = settings
        return settings

    def close(self) -> tuple[PendingDrain, ...]:
        """Shut down. Pending drains remain draining; none are auto-completed."""
        with self._lock:
            self._closed = True
            if self._local is not None:
                self._local.close()
            if self._claude is not None:
                self._claude.close()
            return tuple(
                p for p in self._pending.values() if p.state == "draining"
            )

    @property
    def closed(self) -> bool:
        return self._closed

    # -- internals ----------------------------------------------------------
    def _ensure_open(self) -> None:
        if self._closed:
            raise ManagerClosedError(
                "the seat manager has closed and cannot mutate settings"
            )

    def _mutation_guard(self, seats_cli: Any) -> None:
        try:
            seats_cli._mutation_guard()
        except seats_cli.SeatsCLIError as exc:
            raise ManagerEligibilityError(str(exc)) from exc

    def _resolve_stable(self, selector: str | None, seats_cli: Any) -> str:
        if selector is None:
            raise ManagerError("a stable seat selector is required")
        try:
            return seats_cli._stable_selector(selector, self.settings)
        except seats_cli.SeatsCLIError as exc:
            raise ManagerError(str(exc)) from exc

    def _load_settings(self) -> tuple[dict[str, Any], list[str]]:
        if self._settings_loader is not None:
            return self._settings_loader()
        from .state import load_settings

        return load_settings(self.root)

    def _current_lifecycle(self, seat_id: str) -> str | None:
        settings, _warnings = self._load_settings()
        entry = settings.get("pool", {}).get("seats", {}).get(seat_id)
        return entry.get("lifecycle") if isinstance(entry, Mapping) else None

    def _seat_is_busy(self, seat_id: str) -> bool:
        from . import seats_cli
        from .state import claim_alive

        ledger = seats_cli._safe_ledger(self.root)
        runtime = ledger.get("seats", {}).get(seat_id, {})
        if not isinstance(runtime, Mapping):
            return False
        return bool(claim_alive(runtime.get("in_flight")))

    def _verify_current(self, preview: ActionPreview) -> None:
        """Cheap pre-check that the preview still matches disk before writing.

        The authoritative guard is ``expected_digest`` inside the settings lock;
        this only converts an obvious drift into a friendly re-preview signal.
        """
        from . import seat_admin

        try:
            digest = seat_admin.settings_digest(self.root)
        except SeatAdminError as exc:
            raise ManagerError(str(exc)) from exc
        if digest != preview.expected_digest:
            raise StalePreviewError(
                "settings changed since this action was previewed; re-preview "
                "before applying"
            )

    def _cancelable_probe(
        self, settings: Mapping[str, Any], seat_id: str, cancel_event: threading.Event
    ) -> bool:
        from .account import refresh_account_usage
        from .capacity import active_capacity_windows

        started = self._clock()
        try:
            messages = refresh_account_usage(
                self.root, dict(settings), [seat_id], cancel_event=cancel_event
            )
        except Exception:
            return False
        if cancel_event.is_set():
            return False
        if not messages or any(
            "unavailable" in message or "needs-login" in message
            for message in messages
        ):
            return False
        from . import seats_cli

        runtime = seats_cli._safe_ledger(self.root).get("seats", {}).get(seat_id, {})
        if not isinstance(runtime, Mapping):
            return False
        observation = runtime.get("account_window_observation")
        success = (
            observation.get("last_success_observed_at")
            if isinstance(observation, Mapping)
            else None
        )
        active = runtime.get("active_window_observation")
        active_at = active.get("observed_at") if isinstance(active, Mapping) else None
        fresh_at = success if isinstance(success, (int, float)) else active_at
        windows = active_capacity_windows(dict(runtime), current=time.time())
        return (
            isinstance(fresh_at, (int, float))
            and float(fresh_at) >= started
            and bool(windows)
        )

    @staticmethod
    def _translate(exc: SeatAdminError) -> ManagerError:
        text = str(exc)
        if "since this action was previewed" in text:
            return StalePreviewError(text)
        return ManagerError(text)
