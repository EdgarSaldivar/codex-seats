"""One fail-closed view of the process that may exercise controller authority."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ControllerContext:
    """Controller identity and request binding derived from trusted launch markers."""

    head_kind: str
    controller_id: str | None
    seat: str | None
    thread_id: str | None
    turn_id: str | None
    remote: bool
    managed_worker: bool

    @property
    def is_controller(self) -> bool:
        return self.controller_id is not None and not self.managed_worker

    def dispatch_identity(self, settings: Mapping[str, Any]) -> tuple[str, str | None]:
        """Return protocol origin, attributing seatless controllers to primary."""
        seat = self.seat
        if self.is_controller and self.head_kind == "codex" and seat is None:
            pool = settings.get("pool")
            primary = pool.get("primary", "seat1") if isinstance(pool, Mapping) else "seat1"
            seat = primary if isinstance(primary, str) and primary else "seat1"
        return self.head_kind, seat


def _value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value if isinstance(value, str) and value else None


def current_controller(
    env: Mapping[str, str] | None = None,
) -> ControllerContext:
    """Derive controller identity without treating a plain shell as a controller.

    ``CODEX_CONTROL_SEAT`` and ``CODEX_SEATS_HEAD`` are explicit head-launch markers.
    The Codex app's Remote Control path has no control-seat marker. Depending on the
    host/runtime generation it supplies ``CODEX_THREAD_ID`` with either an explicit
    ``CODEX_HOME`` or ``CODEX_SESSION_ID`` marker. Those narrow pairs are accepted
    unless the process explicitly identifies as a Claude head. Managed-worker
    containment always wins.
    """
    source = os.environ if env is None else env
    managed = _value(source, "CODEX_SEATS_MANAGED_WORKER") == "1"
    declared_head = (_value(source, "CODEX_SEATS_HEAD") or "").lower()
    head_kind = declared_head if declared_head in {"codex", "claude"} else "codex"
    seat = _value(source, "CODEX_CONTROL_SEAT")
    thread_id = _value(source, "CODEX_THREAD_ID")
    turn_id = _value(source, "CODEX_TURN_ID")
    codex_home = _value(source, "CODEX_HOME")
    session_id = _value(source, "CODEX_SESSION_ID")

    remote = (
        seat is None
        and declared_head != "claude"
        and thread_id is not None
        and (codex_home is not None or session_id is not None)
    )
    if managed:
        controller_id = None
        remote = False
    elif seat is not None:
        controller_id = seat
    elif declared_head == "claude":
        controller_id = "claude-head"
    elif remote:
        controller_id = "codex-primary"
    else:
        controller_id = None

    return ControllerContext(
        head_kind=head_kind,
        controller_id=controller_id,
        seat=seat,
        thread_id=thread_id,
        turn_id=turn_id,
        remote=remote,
        managed_worker=managed,
    )
