"""Short lived, ledger owned identities for read only review model calls."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .controller_context import current_controller
from .workers import DEFAULT_MODEL
from .provider_claude import claude_model_available, claude_task_fits
from .seat_registry import active_seat_ids
from .seat_selection import pick_seat
from .state import (
    Ledger, effective_claude_mode, has_auth, iso,
    load_claude_rate_limits, load_settings, now, runtime_seat_home,
)
from .worker_environment import minimal_worker_env


class ReviewUnavailable(RuntimeError):
    """No declared, eligible provider identity can run this review call."""


@dataclass(frozen=True)
class ReviewIdentity:
    family: str
    seat: str | None
    home: Path | None
    env: dict[str, str]


def pool_root() -> Path:
    return Path(os.environ.get("CODEX_SEATS_ROOT", Path.home() / ".codex-seats")).expanduser()


@contextmanager
def claim_review(
    family: str, *, model: str | None = None, effort: str | None = None,
    seat: str | None = None, home: str | Path | None = None,
    root: Path | None = None,
) -> Iterator[ReviewIdentity]:
    """Claim under the ledger lock; never retain that lock during execution.

    Explicit homes and seats are selectors for registered identities, not permission
    to launch on an arbitrary Codex home. The random claim id protects a successor's
    claim if the original is cleared or replaced while its subprocess exits.
    """
    context = current_controller()
    if context.managed_worker or os.environ.get("CLAUDE_LEAD_WORKER") == "1":
        raise ReviewUnavailable("managed workers cannot claim review capacity")
    root = pool_root() if root is None else Path(root)
    try:
        configured = (root / "settings.json").is_file() and (root / "seats.json").is_file()
    except OSError:
        configured = False
    if not configured:
        raise ReviewUnavailable("review pool is not configured")
    try:
        settings, warnings = load_settings(root)
        if any(warning.startswith("invalid settings") for warning in warnings):
            raise ReviewUnavailable("review pool settings are invalid")
    except (OSError, ValueError) as exc:
        raise ReviewUnavailable(f"review pool settings unavailable: {exc}") from exc
    env = minimal_worker_env()
    token = f"review-{uuid.uuid4().hex}"
    target: str | None = None
    selected_home: Path | None = None
    with Ledger(root) as data:
        # Reload while holding the ledger lock, matching worker claim ordering.
        settings, warnings = load_settings(root)
        if any(warning.startswith("invalid settings") for warning in warnings):
            raise ReviewUnavailable("review pool settings are invalid")
        if family == "codex":
            ids = active_seat_ids(settings)
            head_kind, control_seat = context.dispatch_identity(settings)
            excluded = (
                {control_seat} if context.is_controller and head_kind == "codex" and control_seat
                else set()
            )
            if seat is not None and seat not in ids:
                raise ReviewUnavailable(f"{seat} is not an active registered seat")
            if seat in excluded:
                raise ReviewUnavailable(f"{seat} is the active controller seat")
            requested = Path(home).expanduser().resolve() if home is not None else None
            if requested is not None:
                matches = [
                    candidate for candidate in ids
                    if runtime_seat_home(root, candidate, settings).resolve() == requested
                ]
                if not matches or (seat is not None and seat not in matches):
                    raise ReviewUnavailable("requested CODEX_HOME is not an active registered identity")
                if seat is None and len(matches) != 1:
                    raise ReviewUnavailable("requested CODEX_HOME matches multiple registered seats")
                seat = seat or matches[0]
            if seat in excluded:
                raise ReviewUnavailable(f"{seat} is the active controller seat")
            seats = data["seats"]
            eligible = {}
            for candidate in ids:
                state = seats.get(candidate)
                if not isinstance(state, dict):
                    continue
                if not has_auth(root, candidate, settings) or state.get("auth_quarantined_at"):
                    eligible[candidate] = {**state, "status": "needs-login"}
                else:
                    eligible[candidate] = state
            # pick_seat is the authoritative reserve, capacity and busy policy.
            try:
                target = pick_seat(
                    eligible, settings, size="small",
                    excluded=(set(ids) - {seat} if seat else set()) | excluded,
                    learning=data.get("capacity_learning"),
                    model=model or DEFAULT_MODEL, effort=effort or "default",
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ReviewUnavailable(f"Codex review capacity unavailable: {exc}") from exc
            if target is None:
                raise ReviewUnavailable("no eligible Codex review seat has available capacity")
            selected_home = runtime_seat_home(root, target, settings)
            for other in ids:
                if other == target:
                    continue
                other_state = seats.get(other)
                if (
                    isinstance(other_state, dict) and other_state.get("in_flight")
                    and runtime_seat_home(root, other, settings).resolve()
                    == selected_home.resolve()
                ):
                    raise ReviewUnavailable("registered CODEX_HOME is already in use")
            claim = seats[target]
        elif family == "claude":
            if seat is not None or home is not None:
                raise ReviewUnavailable("Claude review cannot select a Codex seat")
            mode, reason = effective_claude_mode(settings)
            if mode == "off":
                raise ReviewUnavailable(reason or "Claude provider is off")
            claim = data["providers"]["claude"]
            if claim.get("status") != "ok" or claim.get("in_flight"):
                raise ReviewUnavailable("Claude provider is unavailable or busy")
            if model and not claude_model_available(claim, model):
                raise ReviewUnavailable(f"Claude model {model} is unavailable")
            fits, reason = claude_task_fits(
                settings, "small", load_claude_rate_limits(root),
                require_fresh_known=True,
            )
            if not fits:
                raise ReviewUnavailable(reason or "Claude review capacity unavailable")
        else:
            raise ValueError(f"unknown review family: {family}")
        claim["in_flight"] = {
            "job_id": token, "dispatcher_pid": os.getpid(), "child_pid": None,
            "kind": "review", "size": "small", "label": "read-only review",
            "model": model, "started_at": iso(), "started_ts": now(),
        }
    if selected_home is not None:
        env["CODEX_HOME"] = str(selected_home)
    identity = ReviewIdentity(family, target, selected_home, env)
    try:
        yield identity
    finally:
        with Ledger(root) as data:
            owner = (
                data["seats"].get(target) if target is not None
                else data["providers"].get("claude")
            )
            if isinstance(owner, dict) and isinstance(owner.get("in_flight"), dict):
                if owner["in_flight"].get("job_id") == token:
                    owner["in_flight"] = None
