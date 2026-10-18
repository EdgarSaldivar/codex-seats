from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import dispatch_client, local_seats, pool_view
from .capacity import seat_capacity_report
from .claude_manager import resolve_subscription_tier
from .dispatch_protocol import DispatchError
from .provider_claude import claude_capacity_report
from .operator_reporting import schema3_operator_report
from .seat_registry import derive_roles, registered_seat_ids
from .state import (
    Ledger,
    age_h,
    claude_usage_age_seconds,
    claude_usage_fresh,
    effective_claude_mode,
    load_claude_rate_limits,
    parse_iso,
    read_pool_snapshot,
    refresh_transients,
    reserve_for,
    usable_headroom,
)
from .telemetry import refresh_passive_usage
from .tui_style import (
    ANSI,
    TMUX,
    Chip,
    age as _age,
    clip,
    clock,
    countdown,
    display_width,
    duration,
    human,
    one_line,
    paint,
    render_parts,
    status_chip,
    usage_bar,
    window_label,
)

__all__ = ["ANSI", "TMUX", "DashboardViewState", "dashboard", "segment"]


def _codex_plan_name(settings: dict[str, Any], seat_id: str) -> str:
    """Display the configured plan profile, never infer it from a multiplier."""
    entry = settings.get("pool", {}).get("seats", {}).get(seat_id, {})
    profile = entry.get("capacity_profile") if isinstance(entry, dict) else None
    if not isinstance(profile, str) or not profile:
        return "Unverified"
    return human("capacity_profile", profile)


def _view_seat_ids(settings: dict[str, Any]) -> list[str]:
    """Non-retired seats available to status/history-oriented readers."""
    return registered_seat_ids(settings, lifecycles=("active", "draining", "inactive"))


def _operational_seat_ids(settings: dict[str, Any]) -> list[str]:
    return registered_seat_ids(settings, lifecycles=("active", "draining"))


def _active_seat_ids(settings: dict[str, Any]) -> list[str]:
    return registered_seat_ids(settings, lifecycles=("active",))


def fmt_tokens(value: float | int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    return f"{value / 1_000:.0f}k"


def _countdown(reset_at: Any, *, compact: bool = True, at: float | None = None) -> str:
    return countdown(reset_at, at, compact=compact)


def _window_label(name: str) -> str:
    return {"5h": "5h", "weekly": "w", "monthly": "m", "five_hour": "5h", "seven_day": "7d"}.get(
        name, name
    )


def _reset_windows(limits: dict[str, Any]) -> list[tuple[str, float]]:
    windows: list[tuple[str, float]] = []
    for name, raw in limits.items():
        if not isinstance(raw, dict):
            continue
        reset_at = raw.get("resets_at")
        if isinstance(reset_at, (int, float)):
            windows.append((name, float(reset_at)))
    return sorted(windows, key=lambda item: item[1])


def _next_reset_badge(limits: dict[str, Any]) -> str:
    windows = _reset_windows(limits)
    if not windows:
        return "@?"
    name, reset_at = windows[0]
    return f"@{_window_label(name)}:{_countdown(reset_at)}"


def role_badges(settings: dict[str, Any]) -> dict[str, str]:
    """Compact role badge per visible seat; seats are always selected by seatN."""
    roles = derive_roles(settings)
    # Roles are primary and worker; a legacy "deputy" renders as a worker.
    badges = {"primary": "P", "worker": "W", "deputy": "W"}
    return {
        seat_id: badges.get(roles.get(seat_id, ""), "-")
        for seat_id in _view_seat_ids(settings)
    }


def seat_color(seat_id: str, seat: dict[str, Any], settings: dict[str, Any]) -> str:
    if seat.get("status") not in {"ok"}:
        return "red"
    if seat.get("in_flight"):
        return "cyan"
    stale_seconds = int(settings["pool"].get("probe_stale_seconds", 21600))
    age = age_h(seat.get("probed_at"))
    if seat.get("pct_left") is None:
        return "gray"
    if age is not None and age * 3600 > stale_seconds:
        return "amber"
    reserve = reserve_for(settings, seat_id, seat)
    headroom = usable_headroom(settings, seat_id, seat)
    if headroom <= 0:
        return "red"
    if headroom < float(settings["pool"].get("small_task_min_headroom_pct", 10)):
        return "amber"
    if reserve and float(seat["pct_left"]) <= reserve + 10:
        return "amber"
    return "green"


def seat_activity_symbol(seat: dict[str, Any]) -> str:
    if seat.get("in_flight"):
        return "●"
    if seat.get("status") == "ok":
        return "○"
    if seat.get("status") == "capped":
        return "◐"
    return "×"


def claude_activity_symbol(mode: str, provider: dict[str, Any]) -> str:
    if mode == "off":
        return "·"
    if provider.get("in_flight"):
        return "●"
    if provider.get("status") == "ok":
        return "○"
    if provider.get("status") == "cooldown":
        return "◐"
    return "×"


def _claude_remaining(limits: dict[str, Any], name: str) -> float | None:
    window = limits.get(name)
    if not isinstance(window, dict):
        return None
    value = window.get("remaining_percentage")
    return float(value) if isinstance(value, (int, float)) else None


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}"


def claude_color(mode: str, provider: dict[str, Any], limits: dict[str, Any] | None = None) -> str:
    if mode == "off":
        return "gray"
    if provider.get("in_flight"):
        return "cyan"
    if provider.get("status") in {"cooldown", "needs-auth", "missing"}:
        return "red" if provider.get("status") != "cooldown" else "amber"
    remaining = [
        value
        for value in (
            _claude_remaining(limits or {}, "five_hour"),
            _claude_remaining(limits or {}, "seven_day"),
        )
        if value is not None
    ]
    if remaining and min(remaining) <= 0:
        return "red"
    if remaining and min(remaining) <= 15:
        return "amber"
    return "purple"


def _model_family(model: str) -> str:
    lowered = model.lower()
    if "fable" in lowered:
        return "F"
    if "opus" in lowered:
        return "O"
    if "sonnet" in lowered:
        return "S"
    return model[:1].upper() or "?"


def _claude_model_health(provider: dict[str, Any]) -> str:
    states = provider.get("models", {})
    if not isinstance(states, dict):
        return ""
    capped = [
        _model_family(model)
        for model, state in states.items()
        if isinstance(state, dict) and state.get("status") == "capped"
    ]
    return "" if not capped else "/".join(sorted(set(capped))) + "!"


def _control_claim(
    snapshot: dict[str, Any], settings: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    for seat_id in _operational_seat_ids(settings):
        claim = snapshot.get("seats", {}).get(seat_id, {}).get("in_flight")
        if isinstance(claim, dict) and claim.get("kind") == "control":
            return seat_id, claim
    return None


def _control_indicators(
    snapshot: dict[str, Any], settings: dict[str, Any]
) -> list[tuple[str, str]]:
    active = _control_claim(snapshot, settings)
    if active is None:
        return []
    _, claim = active
    session = claim.get("session") if isinstance(claim.get("session"), dict) else {}
    mode = str(session.get("mode") or "boot").lower()
    reviewer = str(session.get("approvals_reviewer") or "").lower()
    sandbox = str(session.get("sandbox") or "").lower()
    if mode == "plan":
        mode_label, mode_color = "PLAN", "purple"
    elif mode == "boot":
        mode_label, mode_color = "BOOT", "gray"
    elif sandbox == "read-only":
        mode_label, mode_color = "RO", "amber"
    elif reviewer == "auto_review":
        mode_label, mode_color = "AUTO", "green"
    else:
        mode_label, mode_color = "MAN", "amber"
    effort = str(session.get("effort") or claim.get("effort") or "?").upper()
    effort_color = "red" if effort == "ULTRA" else "amber" if effort in {"MAX", "XHIGH"} else "cyan"
    return [(f"MODE:{mode_label}", mode_color), (f"E:{effort}", effort_color)]


def _rotation_warning(
    snapshot: dict[str, Any], settings: dict[str, Any]
) -> tuple[str, str, str] | None:
    active = _control_claim(snapshot, settings)
    if active is None:
        return None
    seat_id, claim = active
    current = snapshot["seats"][seat_id].get("pct_left")
    if not isinstance(current, (int, float)):
        return None
    usable = usable_headroom(settings, seat_id, snapshot["seats"][seat_id])
    if usable > 10:
        return None
    return seat_id, f"ROTATE:{seat_id}", f"{usable:g}% usable remains (completed-turn threshold <=10%)"


def _notify_rotation_once(root: Path, warning: tuple[str, str, str] | None) -> None:
    # Rotation is display-only: never a prompt block, handoff, or OS alert.
    del root, warning


def _observed_reset_summary(seat: dict[str, Any], *, at: float) -> str:
    cycles = seat.get("usage_cycles")
    if not isinstance(cycles, dict):
        return "-"
    parts = []
    limits = seat.get("last_probe_limits")
    if not isinstance(limits, dict):
        return "-"
    for name in sorted(limits):
        window = limits[name]
        if not isinstance(window, dict):
            continue
        reset = window.get("resets_at")
        if (
            isinstance(reset, (int, float))
            and not isinstance(reset, bool)
            and float(reset) <= at
        ):
            continue
        cycle = cycles.get(name)
        if isinstance(cycle, dict):
            count = cycle.get("observed_resets", 0)
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                parts.append(f"{_window_label(name)}×{int(count)}")
    return ",".join(parts) or "-"


def _usage_age_text(seconds: float | None) -> str:
    return _age(seconds)


@dataclass
class DashboardViewState:
    """Ephemeral watcher state used to show exactly one-generation balance deltas."""

    _balances: dict[str, float | None] = field(default_factory=dict, init=False)
    _deltas: dict[str, float] = field(default_factory=dict, init=False)
    _generation: int | None = field(default=None, init=False)
    _initialized: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def observe(
        self, balances: dict[str, float | None], generation: int | None
    ) -> dict[str, float]:
        with self._lock:
            current = dict(balances)
            if not self._initialized:
                self._balances = current
                self._generation = generation
                self._initialized = True
                return {}
            # A cached/non-watcher render establishes a baseline without
            # inventing a refresh boundary. The first real generation compares
            # against that baseline.
            if generation is None:
                return dict(self._deltas)
            if self._generation is None:
                deltas: dict[str, float] = {}
                for account, value in current.items():
                    previous = self._balances.get(account)
                    if value is not None and previous is not None and value != previous:
                        deltas[account] = value - previous
                self._balances = current
                self._generation = generation
                self._deltas = deltas
                return dict(self._deltas)
            if generation != self._generation:
                deltas: dict[str, float] = {}
                for account, value in current.items():
                    previous = self._balances.get(account)
                    if value is not None and previous is not None and value != previous:
                        deltas[account] = value - previous
                self._balances = current
                self._generation = generation
                self._deltas = deltas
            return dict(self._deltas)


@dataclass(frozen=True)
class _AttentionItem:
    """One operator problem, with compact and diagnostic representations."""

    style: str
    owner: str
    problem: str
    action: str
    diagnostic: str
    priority: bool = False


def _snapshot_value(snapshot: Any, name: str, default: Any = None) -> Any:
    if snapshot is None:
        return default
    if isinstance(snapshot, dict):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def _terminal_text(text: Any) -> str:
    return one_line(text)


def _display_width(text: str) -> int:
    return display_width(text)


def _plain_clip(text: Any, width: int) -> str:
    return clip(text, width)


def _paint(text: str, name: str | None, color: bool) -> str:
    return paint(text, name, color)


def _line(parts: list[tuple[str, str | None]], width: int, color: bool) -> str:
    return render_parts(parts, width, color)


def live_header(
    refresh_snapshot: Any, *, current: float, monotonic_at: float | None = None
) -> tuple[list[tuple[str, str | None]], str]:
    """``codex-seats ● LIVE 14:32`` parts and the refresh freshness text.

    Shared by the dashboard and the seat manager's summary strip so both say
    LIVE / REFRESHING / CACHED and "updated 12s ago" the same way.
    """
    current_monotonic = time.monotonic() if monotonic_at is None else float(monotonic_at)
    refreshing = bool(_snapshot_value(refresh_snapshot, "refreshing", False))
    if refreshing:
        live_text, live_color = "↻ REFRESHING", "live"
    elif refresh_snapshot is not None:
        live_text, live_color = "● LIVE", "live"
    else:
        live_text, live_color = "○ CACHED", "muted"
    succeeded = _snapshot_value(refresh_snapshot, "last_succeeded", "unset")
    success_at = _snapshot_value(refresh_snapshot, "last_success_at")
    if not isinstance(success_at, (int, float)):
        result = _snapshot_value(refresh_snapshot, "last_result")
        recent_ok = succeeded is True if succeeded != "unset" else _refresh_succeeded(result)
        if recent_ok:
            success_at = _snapshot_value(refresh_snapshot, "last_finished_at")
    last_ok = "—" if not isinstance(success_at, (int, float)) else _usage_age_text(current - float(success_at))
    next_due = _snapshot_value(refresh_snapshot, "next_due")
    if refreshing:
        next_text = "after refresh"
    elif isinstance(next_due, (int, float)):
        next_text = _countdown(current + max(0.0, float(next_due) - current_monotonic), at=current)
    else:
        next_text = "pending"
    local_time = datetime.fromtimestamp(current).astimezone().strftime("%H:%M")
    live_parts: list[tuple[str, str | None]] = [
        ("codex-seats", "heading"),
        ("  ", None),
        (live_text, live_color),
        (f" {local_time}", "muted"),
    ]
    updated = "not refreshed yet" if last_ok == "—" else f"updated {last_ok} ago"
    return live_parts, updated if next_text == "pending" else f"{updated} · next {next_text}"


def header_parts(
    live_parts: list[tuple[str, str | None]],
    summary_parts: list[tuple[str, str | None]],
    freshness_text: str,
    width: int,
) -> list[list[tuple[str, str | None]]]:
    """One header line when it fits, else live+freshness over the summary."""
    left_width = sum(display_width(text) for text, _ in live_parts)
    summary_width = sum(display_width(text) for text, _ in summary_parts)
    if left_width + 3 + summary_width + 2 + display_width(freshness_text) <= width:
        gap = width - left_width - 3 - summary_width - display_width(freshness_text)
        return [
            [*live_parts, ("   ", None), *summary_parts, (" " * gap, None), (freshness_text, "muted")]
        ]
    return [[*live_parts, (" · ", "muted"), (freshness_text, "muted")], summary_parts]


def _cell(
    text: Any,
    width: int,
    *,
    align: str = "left",
    style: str | None = None,
) -> list[tuple[str, str | None]]:
    value = _plain_clip(text, width)
    gap = max(0, width - _display_width(value))
    if align == "right":
        return [(" " * gap, None), (value, style)]
    return [(value, style), (" " * gap, None)]


def _seat_limits(seat: dict[str, Any]) -> dict[str, Any]:
    limits = seat.get("last_probe_limits")
    return limits if isinstance(limits, dict) else {}


def _signal_resets(
    limits: dict[str, Any], *, at: float, expected_names: set[str] | None = None
) -> tuple[str, bool]:
    windows = [(name, reset) for name, reset in _reset_windows(limits) if reset > at]
    if not windows:
        return "reported ?", True
    parts = [
        f"{_window_label(name)} {_countdown(reset_at, at=at)}"
        for name, reset_at in windows[:2]
    ]
    present = {name for name, _reset in windows}
    missing = bool(expected_names and not expected_names.issubset(present))
    return " · ".join(parts), missing


def _delta_text(delta: float | None) -> str:
    if delta is None or delta == 0:
        return ""
    symbol = "▲" if delta > 0 else "▼"
    return f"{symbol}{abs(delta):g}"


def _window_cell(
    value: float | None,
    delta: float | None,
    *,
    fresh: bool,
    bar: int,
    delta_width: int,
) -> list[tuple[str, str | None]]:
    """One usage window: threshold-coloured bar, ``NN%`` and a refresh delta."""
    parts = usage_bar(value, bar, fresh=fresh)
    if delta_width:
        text = clip(f" {_delta_text(delta)}" if _delta_text(delta) else "", delta_width)
        parts.append((text, "muted"))
        parts.append((" " * (delta_width - display_width(text)), None))
    return parts


def _capacity_window_values(
    capacity: dict[str, Any],
) -> dict[str, tuple[float | None, float | None]]:
    """Short window label (``5h``/``7d``) → (remaining %, reset) for one seat."""
    values: dict[str, tuple[float | None, float | None]] = {}
    for window in capacity.get("capacity_windows") or []:
        if not isinstance(window, dict):
            continue
        short = window_label(window.get("name"), window.get("window_minutes"), short=True)
        remaining = window.get("remaining_pct")
        reset = window.get("resets_at")
        values.setdefault(
            short,
            (
                float(remaining) if isinstance(remaining, (int, float)) else None,
                float(reset) if isinstance(reset, (int, float)) else None,
            ),
        )
    return values


def _exhausted_window(windows: dict[str, tuple[float | None, float | None]]) -> str | None:
    for short, (remaining, _reset) in sorted(windows.items()):
        if remaining is not None and remaining <= 0:
            return short
    return None


def _table_layout(width: int) -> dict[str, int] | None:
    """Column widths for the seat table, or ``None`` for the stacked layout.

    Bars, the delta field and the FIT wording shrink first; the seat and
    status columns share whatever remains, and the sum never exceeds ``width``.
    """
    for bar, delta, fit in ((10, 4, 9), (8, 4, 9), (8, 4, 4), (6, 4, 4), (5, 0, 4), (4, 0, 4)):
        window = bar + 5 + delta
        gap = 1 if delta else 2
        fixed = 2 + 2 + window + gap + window + gap + 6 + 2 + 2 + fit + 1 + 6
        spare = width - fixed
        if spare >= 30:
            # Names get the room first; status keeps at least 15 cells.
            seat = min(28, max(16, spare - 15, spare // 2))
            return {
                "seat": seat,
                "bar": bar,
                "delta": delta,
                "window": window,
                "gap": gap,
                "status": min(28, spare - seat),
                "fit": fit,
            }
    return None


def _table_header(layout: dict[str, int]) -> list[tuple[str, str | None]]:
    gap = (" " * layout["gap"], None)
    return [
        ("  ", None),
        *_cell("SEAT", layout["seat"], style="muted"),
        ("  ", None),
        *_cell("5H LEFT", layout["window"], style="muted"),
        gap,
        *_cell("7D LEFT", layout["window"], style="muted"),
        gap,
        *_cell("RESET", 6, style="muted"),
        ("  ", None),
        *_cell("STATUS", layout["status"], style="muted"),
        ("  ", None),
        *_cell("FIT", layout["fit"], style="muted"),
        (" ", None),
        *_cell("AGE", 6, align="right", style="muted"),
    ]


def _name_cell(name: str, plan: str, width: int) -> list[tuple[str, str | None]]:
    """Seat name with its plan in muted text, padded to ``width`` cells."""
    name_text = clip(name, width)
    used = display_width(name_text)
    plan_text = clip(f" {plan}", width - used) if plan and width - used > 2 else ""
    used += display_width(plan_text)
    return [(name_text, None), (plan_text, "muted"), (" " * max(0, width - used), None)]


def _local_name(row: dict[str, Any]) -> str:
    return f"{row['model']} on {row['host']}"


def _local_table_row(row: dict[str, Any], name: str) -> dict[str, Any]:
    """Adapt one shared local status row to the seat table (no usage windows)."""
    working = row["key"] == "working"
    return {
        "kind": "local",
        "role": "L",
        "account": row["name"],
        "name": name,
        "plan": "",
        "windows": {},
        "fresh": True,
        "reset": None,
        "chip": status_chip(
            "local",
            row["key"],
            task=row["detail"] if working else None,
            age=row["age_text"] if row["stale"] and row["age_text"] != "—" else None,
        ),
        "fit": "—",
        "age": row["age_text"],
        "stale": row["stale"],
    }


def _local_attention(local_rows: list[dict[str, Any]]) -> list["_AttentionItem"]:
    """Local seats that need an operator: stale probes, failures, wrong models."""
    items: list[_AttentionItem] = []
    for row in local_rows:
        name = _local_name(row)
        detail = str(row.get("detail") or "")
        if detail and detail == row.get("reason"):
            detail = human("reason", detail)
        diagnostic = (
            f"local {row['name']} {row['key']}: {detail or '-'}; reason={row['reason'] or '-'}"
        )
        if row["key"] == "stale":
            items.append(
                _AttentionItem(
                    "amber",
                    name,
                    f"Local seat {name} has not been probed for {row['age_text']}",
                    "probe its host from `codex-seat dash --manage`",
                    diagnostic,
                )
            )
        elif row["key"] == "degraded":
            items.append(
                _AttentionItem(
                    "red",
                    name,
                    f"Local seat {name} is degraded"
                    + (f": {detail[:1].lower()}{detail[1:]}" if detail else ""),
                    "check the host, then probe it again",
                    diagnostic,
                )
            )
        elif row["key"] == "model_mismatch":
            items.append(
                _AttentionItem(
                    "red",
                    name,
                    f"Local seat {name} is {detail or 'serving another model'}",
                    "load the configured model or update the seat",
                    diagnostic,
                )
            )
    return items


def _seat_row_lines(
    row: dict[str, Any],
    layout: dict[str, int] | None,
    deltas: dict[str, float],
    *,
    width: int,
    at: float,
) -> list[list[tuple[str, str | None]]]:
    """One table line per seat, or two stacked lines when the table cannot fit."""
    chip: Chip = row["chip"]
    reset_text = "—" if row.get("reset") is None else countdown(row["reset"], at)
    reset_role = "warn" if row.get("missing") else "muted"
    fit = str(row.get("fit") or "—")
    fit_role = "muted" if fit in {"—", "none"} else None
    age_text = ("~" if row["stale"] and row["age"] != "—" else "") + row["age"]
    age_role = "warn" if row["stale"] else "muted"
    if layout is not None:
        gap = (" " * layout["gap"], None)
        parts: list[tuple[str, str | None]] = [(row["role"], None), (" ", None)]
        parts += _name_cell(row["name"], row.get("plan") or "", layout["seat"])
        parts.append(("  ", None))
        for index, short in enumerate(("5h", "7d")):
            if index:
                parts.append(gap)
            remaining, _reset = row["windows"].get(short, (None, None))
            parts += _window_cell(
                remaining,
                deltas.get(f"{row['account']}:{short}"),
                fresh=row["fresh"],
                bar=layout["bar"],
                delta_width=layout["delta"],
            )
        parts.append(gap)
        parts += _cell(reset_text, 6, style=reset_role)
        parts.append(("  ", None))
        parts += _cell(chip.text, layout["status"], style=chip.role)
        parts.append(("  ", None))
        parts += _cell(_fit_text(fit, expanded=layout["fit"] > 4), layout["fit"], style=fit_role)
        parts.append((" ", None))
        parts += _cell(age_text, 6, align="right", style=age_role)
        return [parts]
    if width < 60:
        # One line per seat: the tightest window, then the status chip.
        known = [
            (remaining, short)
            for short, (remaining, _reset) in sorted(row.get("windows", {}).items())
            if remaining is not None
        ]
        line: list[tuple[str, str | None]] = [
            (row["role"], None),
            (" ", None),
            *_name_cell(row["name"], row.get("plan") or "", max(12, min(22, width - 30))),
            (" ", None),
        ]
        if row["kind"] == "local":
            line += [(chip.text, chip.role), ("  ", None), (age_text, age_role)]
            return [line]
        remaining, short = min(known) if known else (None, "  ")
        line += [
            (f"{short} ", "muted"),
            *usage_bar(remaining, 5, fresh=row["fresh"]),
            ("  ", None),
            (chip.text, chip.role),
        ]
        return [line]
    name_width = max(10, min(26, width - 20))
    first: list[tuple[str, str | None]] = [
        (row["role"], None),
        (" ", None),
        *_name_cell(row["name"], row.get("plan") or "", name_width),
        ("  ", None),
        (chip.text, chip.role),
    ]
    if row["kind"] == "local":
        # A stale chip already carries the probe age.
        return [first if row["stale"] else [*first, ("  ", None), (age_text, age_role)]]
    second: list[tuple[str, str | None]] = [("    ", None)]
    for short in ("5h", "7d"):
        remaining, _reset = row["windows"].get(short, (None, None))
        second += [(f"{short} ", "muted"), *usage_bar(remaining, 5, fresh=row["fresh"]), ("  ", None)]
    second += [
        (f"{reset_text:<5}", reset_role),
        ("  ", None),
        (f"{_fit_text(fit, expanded=False):<4}", fit_role),
        ("  ", None),
        (age_text, age_role),
    ]
    return [first, second]


def _rule(
    title: str, right: str, width: int, color: bool, *, role: str = "heading"
) -> str:
    """Section heading drawn as ``── TITLE ─────── right ──``."""
    head = f"── {title} "
    tail = f" {right} ──" if right else "──"
    fill = width - display_width(head) - display_width(tail)
    if fill < 2:
        return _line([(title, role), (f" {right}" if right else "", "muted")], width, color)
    return _line(
        [("── ", "muted"), (title, role), (" " + "─" * fill, "muted"), (tail, "muted")],
        width,
        color,
    )


def _refresh_failed(result: Any) -> bool:
    if not isinstance(result, str) or not result:
        return False
    lowered = result.lower()
    if any(word in lowered for word in ("failed", "failure", "needs-auth", "error")):
        return True
    match = re.search(r"codex\s+(\d+)/(\d+)", lowered)
    return bool(match and match.group(1) != match.group(2))


def _refresh_succeeded(result: Any) -> bool:
    if not isinstance(result, str):
        return False
    match = re.search(r"codex\s+(\d+)/(\d+)", result.lower())
    return bool(match and match.group(1) == match.group(2))


def _fit_label(report: dict[str, Any]) -> str:
    """Compact routing fit label, independent of provider activity."""
    if not report.get("applicable", report.get("auth_ok", False)):
        return "—"
    if report.get("fits_small") and report.get("fits_big"):
        return "S+B"
    if report.get("fits_small"):
        return "S"
    if report.get("fits_big"):
        return "B"
    return "none"


def _fit_text(label: str, *, expanded: bool) -> str:
    return human("fit", label) if expanded else label


def _binding_window(
    capacity: dict[str, Any],
) -> tuple[str | None, float | None, float | None]:
    window = capacity.get("binding_window")
    if not isinstance(window, dict):
        return None, None, None
    remaining = window.get("remaining_pct")
    reset = window.get("resets_at")
    return (
        str(window.get("name")) if window.get("name") else None,
        float(remaining) if isinstance(remaining, (int, float)) else None,
        float(reset) if isinstance(reset, (int, float)) else None,
    )


def _claude_binding_window(
    limits: dict[str, Any],
) -> tuple[str | None, float | None, float | None]:
    candidates: list[tuple[str, float, float | None]] = []
    for name in ("five_hour", "seven_day"):
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        remaining = window.get("remaining_percentage")
        if not isinstance(remaining, (int, float)):
            continue
        reset = window.get("resets_at")
        candidates.append(
            (
                name,
                float(remaining),
                float(reset) if isinstance(reset, (int, float)) else None,
            )
        )
    return min(candidates, key=lambda item: (item[1], item[0])) if candidates else (None, None, None)


def _claim_request_id(claim: dict[str, Any]) -> str | None:
    for key in ("request_id", "label"):
        value = claim.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
            return value
    return None


def _group_attention(
    items: list[_AttentionItem], *, forensic: bool
) -> tuple[list[tuple[str, str]], int]:
    """Group seat/provider diagnostics while preserving priority global warnings."""
    unique: list[_AttentionItem] = []
    seen: set[tuple[str, str, str, str, str, bool]] = set()
    for item in items:
        identity = (
            item.style,
            item.owner,
            item.problem,
            item.action,
            item.diagnostic,
            item.priority,
        )
        if identity not in seen:
            seen.add(identity)
            unique.append(item)

    priority = [item for item in unique if item.priority or item.owner in {"Global", "Security"}]
    regular = [item for item in unique if item not in priority]
    ordered = [*priority, *regular]
    owners: list[str] = []
    for item in regular:
        if item.owner not in owners:
            owners.append(item.owner)
    problem_count = len(priority) + len(owners)
    def joined(text: str, action: str) -> str:
        return f"{text} · {action}" if action else text

    if forensic:
        return [
            (item.style, f"{item.owner}: {item.diagnostic}" + (f" · Next: {item.action}" if item.action else ""))
            for item in ordered
        ], problem_count

    grouped: list[tuple[str, str]] = [
        (item.style, joined(item.diagnostic, item.action)) for item in priority
    ]
    for owner in owners:
        owned = [item for item in ordered if item.owner == owner]
        main = max(
            owned,
            key=lambda item: (
                item.style == "red",
                "authentication" in item.problem,
                "provider" in item.problem,
            ),
        )
        extra = len(owned) - 1
        suffix = f" · {extra} more (--forensic)" if extra else ""
        grouped.append((main.style, joined(main.problem, main.action) + suffix))
    return grouped, problem_count


def _bound_dashboard_lines(
    lines: list[str],
    *,
    height: int | None,
    width: int,
    color: bool,
    sections: dict[str, tuple[int, int]],
) -> list[str]:
    """Bound a watch frame while retaining its operational spine.

    History is discarded first, followed by forensic/secondary material and
    excess attention/ongoing-work detail.  Any bounded omission is made
    explicit in the final row.
    """
    if height is None:
        return lines
    rows = max(1, int(height))
    if len(lines) <= rows:
        return lines

    provider_start, provider_end = sections["providers"]
    essential = sum(
        bool(lines[index]) for index in range(provider_start, provider_end)
    )
    for name in ("attention", "ongoing"):
        start, end = sections.get(name, (0, 0))
        if end > start:
            # The heading and first observable row are essential; blank
            # separators remain available to the normal trimming pass.
            visible = sum(bool(lines[index]) for index in range(start, end))
            essential += min(2, visible)
    if rows < essential + 1:
        marker = _line(
            [(f"RESIZE: {len(lines)} rows need more terminal height", "amber")],
            width,
            color,
        )
        if rows == 1:
            return [marker]
        return [*lines[: rows - 1], marker]

    target = rows - 1  # reserve an explicit omission marker
    remove: set[int] = set()

    def discard(indices: list[int]) -> None:
        for index in indices:
            if len(lines) - len(remove) <= target:
                break
            remove.add(index)

    # Least operationally useful material goes first. The local-seat section is
    # informational and is released whole before any warning or live-work row.
    for name in ("history", "forensic", "secondary", "local"):
        start, end = sections.get(name, (0, 0))
        discard(list(range(end - 1, start - 1, -1)))

    # Retain one warning plus its heading when possible; later warnings are
    # represented by the common omitted-count marker.
    start, end = sections.get("attention", (0, 0))
    discard(list(range(end - 1, min(end, start + 3) - 1, -1)))

    # Preserve each live-work heading and at least one row, trimming only
    # excess jobs/claims.
    for name in ("ongoing",):
        start, end = sections.get(name, (0, 0))
        discard(list(range(end - 1, min(end, start + 3) - 1, -1)))

    # Empty separator rows are cosmetic and safe to remove last.
    discard([index for index in range(len(lines) - 1, -1, -1) if not lines[index]])
    kept = [line for index, line in enumerate(lines) if index not in remove]
    if len(kept) > target:
        # This is only reachable with an unexpectedly dense mandatory spine.
        kept = kept[:target]
    omitted = len(lines) - len(kept)
    kept.append(
        _line(
            [(f"… {omitted} row{'s' if omitted != 1 else ''} omitted; resize for details", "amber")],
            width,
            color,
        )
    )
    return kept


def _provider_problem(status: Any) -> str:
    """Plain wording for a non-ok provider status, after ``<owner> provider``."""
    chip = status_chip("codex", status)
    return {
        "needs sign-in": "needs a new sign-in",
        "capped": "is capped",
        "cooldown": "is cooling down",
        "not probed": "has not been probed yet",
    }.get(chip.label, f"reports {one_line(status or 'unknown')}")


def _status_is_error(status: Any) -> bool:
    return str(status or "unknown").lower() not in {"ok", "off"}


def _background_count_text(counts: dict[str, int]) -> str:
    text = f"{counts['queued']}q/{counts['running']}r/{counts['capacity']}c"
    if counts.get("recovering"):
        text += f"/{counts['recovering']}!"
    if counts.get("expired"):
        text += f"/{counts['expired']}e"
    return text


def _terminal_history(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nonterminal = {"queued", "running", "pending", "in_progress", "refreshing"}
    ordered = sorted(
        enumerate(jobs), key=lambda item: (_job_ts(item[1]), item[0]), reverse=True
    )
    groups: list[dict[str, Any]] = []
    for _, job in ordered:
        status = str(job.get("status") or "unknown")
        if status.lower() in nonterminal:
            continue
        key = (
            job.get("seat"),
            job.get("provider"),
            status,
            job.get("label"),
            job.get("model"),
        )
        if groups and groups[-1]["key"] == key:
            groups[-1]["count"] += 1
            continue
        if len(groups) == 5:
            break
        groups.append({"key": key, "count": 1, "job": job})
    return groups


def _history_label(label: Any) -> str:
    """Request-id labels shrink to ``job abcd1234`` as in ONGOING WORK."""
    text = str(label or "-")
    return f"job {text[:8]}" if re.fullmatch(r"[0-9a-f]{32}", text) else text


def _history_status(status: str) -> tuple[str, str]:
    lowered = status.lower()
    if lowered in {"completed", "succeeded", "success", "ok"}:
        return "✓", "ok"
    if "block" in lowered or lowered in {"canceled", "cancelled"}:
        return f"! {status}", "warn"
    return f"× {status}", "bad"


_LOCAL_STATE_STYLES: dict[str, tuple[str, str]] = {
    "ready": ("○ available", "green"),
    "gaming": ("◐ gaming", "amber"),
    "asleep": ("○ asleep", "gray"),
    "loading": ("↻ loading", "amber"),
    "busy": ("◐ busy", "amber"),
    "degraded": ("× degraded", "red"),
}
_LOCAL_GATE_REASONS = frozenset(
    {"mode_off", "unknown_host", "lifecycle_ineligible", "no_enabled_seats"}
)


def _local_observation(
    root: Path, settings: dict[str, Any], host_id: str, *, at: float
) -> dict[str, Any]:
    """Read-only view of one host's cached availability. Never probes.

    A fresh cache entry is returned as-is. An expired entry is still read so the
    dashboard can show the last state and its age as ``stale`` instead of
    collapsing every expired observation into ``unknown``; it is bound to the
    current host configuration exactly like the control plane's own reader.
    """
    cached = local_seats.cached_availability(root, settings, host_id, now=at)
    reason = str(cached.get("reason") or "unknown")
    observed = cached.get("observed_at")
    fresh = isinstance(observed, (int, float)) and not isinstance(observed, bool)
    result = {
        "state": cached.get("state") if fresh else None,
        "reason": reason,
        "observed_at": float(observed) if fresh else None,
        "fresh": fresh,
        "detail": cached.get("detail") if fresh else None,
        "model_profile": cached.get("model_profile") if fresh else None,
    }
    if reason not in {"availability_missing_or_invalid", "availability_stale"}:
        return result
    host = settings.get("local_hosts", {}).get(host_id)
    if not isinstance(host, dict):
        return result
    try:
        # Same owner-only, no-follow anchoring as the control plane reader.
        with local_seats._runtime(root) as directory:
            descriptor = local_seats._open_file(directory, local_seats._cache_name(host_id))
            with os.fdopen(descriptor, "rb") as handle:
                payload = handle.read(local_seats.MAX_PROBE_BYTES * 2 + 1)
        if len(payload) > local_seats.MAX_PROBE_BYTES * 2:
            return result
        value = json.loads(payload)
    except (OSError, ValueError, TypeError, RecursionError):
        return result
    if not isinstance(value, dict):
        return result
    stamp = value.get("observed_at")
    if (
        value.get("host") != host_id
        or value.get("config_digest") != local_seats._host_digest(host)
        or type(stamp) not in {int, float}
        or not math.isfinite(stamp)
    ):
        return result
    state = value.get("state")
    detail = value.get("detail")
    profile = value.get("model_profile")
    result.update(
        reason="availability_stale",
        observed_at=float(stamp),
        state=state if state in local_seats.STATES else None,
        detail=detail if isinstance(detail, str) else None,
        model_profile=profile if isinstance(profile, str) else None,
    )
    return result


def _local_running_jobs(background_jobs: list[dict[str, Any]]) -> dict[str, str]:
    """Seat → label for running local jobs; the only source of ``working``."""
    running: dict[str, str] = {}
    for job in background_jobs:
        if job.get("provider") == "local" and job.get("state") == "running" and job.get("seat"):
            running.setdefault(str(job["seat"]), str(job.get("label") or "managed job"))
    return running


def _local_observed_status(
    observation: dict[str, Any], observed_at: float | None
) -> tuple[str, str, str, str]:
    """Map one cached observation to ``(key, state, style, detail)``.

    Fresh states keep their probe vocabulary. An expired entry is ``stale`` with
    its last known state rather than a failure; a gate reason is ``off``; a
    known ``configuration_changed`` result and a missing cache are ``unknown``
    (they are not probe failures, so they are never painted ``degraded``).
    """
    reason = str(observation.get("reason") or "")
    if observation["fresh"] and observation["state"] in _LOCAL_STATE_STYLES:
        key = str(observation["state"])
        state, style = _LOCAL_STATE_STYLES[key]
        if key == "degraded":
            detail = str(observation.get("detail") or reason or "")
        else:
            detail = str(observation.get("detail") or "")
        return key, state, style, detail
    if reason in _LOCAL_GATE_REASONS:
        return "off", "· off", "dim", reason.replace("_", " ")
    if observed_at is not None:
        last = observation.get("state")
        return "stale", "~ stale", "amber", f"last {last}" if last else "last observation expired"
    if reason == "configuration_changed":
        return "unknown", "? unknown", "gray", "configuration changed"
    return "unknown", "? unknown", "gray", "no probe observation"


def _local_status(
    root: Path,
    settings: dict[str, Any],
    background_jobs: list[dict[str, Any]],
    *,
    at: float,
) -> dict[str, Any]:
    """One read-only status derivation for every local host and seat.

    Shared by the dashboard and the seat manager's local screens so both show
    the same off/stale/available/working states. It reads the cached
    availability only (never probes); when the host is gated (global mode off,
    ineligible lifecycle, no enabled seats) no runtime cache I/O happens at
    all. ``working`` is asserted only for a running local job attributed to
    the seat; every other state is the host's cached availability. Frontier
    quota accounting is untouched: these rows carry no balance or routing fit.
    """
    seats = settings.get("local_seats")
    hosts = settings.get("local_hosts")
    seats = seats if isinstance(seats, dict) else {}
    hosts = hosts if isinstance(hosts, dict) else {}
    global_mode = str(settings.get("providers", {}).get("local", {}).get("mode", "off"))
    running = _local_running_jobs(background_jobs)
    observations: dict[str, dict[str, Any]] = {}

    def observe(host_id: str) -> dict[str, Any]:
        if host_id not in observations:
            observations[host_id] = _local_observation(root, settings, host_id, at=at)
        return observations[host_id]

    seat_rows: list[dict[str, Any]] = []
    for seat_id in sorted(seats):
        seat = seats[seat_id]
        if not isinstance(seat, dict):
            continue
        host_id = str(seat.get("host") or "?")
        host = hosts.get(host_id)
        host = host if isinstance(host, dict) else {}
        seat_mode = str(seat.get("mode") or "off")
        mode = local_seats.effective_mode(settings, seat)
        lifecycle = str(seat.get("lifecycle") or "unknown")
        host_lifecycle = str(host.get("lifecycle") or "unknown")
        observation = observe(host_id)
        observed_at = observation["observed_at"]
        age = None if observed_at is None else max(0.0, at - observed_at)
        model = str(seat.get("model_profile") or "?")
        served = observation.get("model_profile")
        if global_mode == "off":
            off_reason = "global mode off"
        elif seat_mode == "off":
            off_reason = "seat mode off"
        elif lifecycle != "active":
            off_reason = f"seat {lifecycle}"
        elif host_lifecycle != "active":
            off_reason = f"host {host_lifecycle}"
        else:
            off_reason = None
        if seat_id in running:
            key, state, style = "working", "● working", "cyan"
            detail = running[seat_id]
        elif off_reason is not None or mode == "off":
            key, state, style = "off", "· off", "dim"
            detail = off_reason or "mode off"
        elif observation["fresh"] and observation["state"] == "ready" and mode == "shadow":
            key, state, style = "shadow", "○ shadow", "dim"
            detail = "observing only"
        else:
            key, state, style, detail = _local_observed_status(observation, observed_at)
            if key == "ready" and isinstance(served, str) and served != model:
                key, state = "model_mismatch", "× model mismatch"
                detail = f"serving {served}"
                style = "amber"
        seat_rows.append(
            {
                "seat": seat_id,
                "host": host_id,
                "name": f"{host_id}/{seat_id}",
                "label": str(seat.get("label") or seat_id),
                "resource_group": str(seat.get("resource_group") or "?"),
                "model": model,
                "mode": mode,
                "seat_mode": seat_mode,
                "global_mode": global_mode,
                "lifecycle": lifecycle,
                "key": key,
                "state": state,
                "style": style,
                "age": age,
                "age_text": _usage_age_text(age).replace("never", "—"),
                "observed_at": observed_at,
                "last_state": observation.get("state"),
                "stale": key == "stale",
                "reason": str(observation.get("reason") or ""),
                "detail": _terminal_text(detail),
            }
        )

    host_rows: dict[str, dict[str, Any]] = {}
    for host_id in sorted(hosts):
        host = hosts[host_id]
        if not isinstance(host, dict):
            continue
        observation = observe(host_id)
        observed_at = observation["observed_at"]
        age = None if observed_at is None else max(0.0, at - observed_at)
        lifecycle = str(host.get("lifecycle") or "unknown")
        reason = str(observation.get("reason") or "")
        working = [row["seat"] for row in seat_rows if row["host"] == host_id and row["key"] == "working"]
        if working:
            key, state, style = "working", "● working", "cyan"
            detail = " · ".join(f"{seat_id}: {running[seat_id]}" for seat_id in working)
        else:
            key, state, style, detail = _local_observed_status(observation, observed_at)
            if key == "off":
                if reason == "mode_off":
                    detail = "global mode off"
                elif reason == "lifecycle_ineligible":
                    detail = f"host {lifecycle}"
        host_rows[host_id] = {
            "host": host_id,
            "label": str(host.get("label") or host_id),
            "lifecycle": lifecycle,
            "global_mode": global_mode,
            "key": key,
            "state": state,
            "style": style,
            "age": age,
            "age_text": _usage_age_text(age).replace("never", "—"),
            "observed_at": observed_at,
            "last_state": observation.get("state"),
            "model_profile": observation.get("model_profile"),
            "stale": key == "stale",
            "reason": reason,
            "detail": _terminal_text(detail),
            "working_seats": working,
        }
    return {"hosts": host_rows, "seats": seat_rows}


def _local_seat_rows(
    root: Path,
    settings: dict[str, Any],
    background_jobs: list[dict[str, Any]],
    *,
    at: float,
) -> list[dict[str, Any]]:
    """Dashboard rows for every configured local seat, including off and stale ones."""
    seats = settings.get("local_seats")
    hosts = settings.get("local_hosts")
    if not isinstance(seats, dict) or not seats or not isinstance(hosts, dict):
        return []
    return _local_status(root, settings, background_jobs, at=at)["seats"]


def _settings_warning_lines(warnings: list[str], *, forensic: bool) -> list[str]:
    """Collapse unknown-key details while keeping other failures verbatim."""
    if forensic:
        return [str(warning) for warning in warnings]
    unknown = 0
    other: list[str] = []
    for warning in warnings:
        message = str(warning)
        if message.startswith("unknown setting "):
            unknown += 1
        else:
            other.append(message)
    if unknown:
        other.append(
            f"Settings file has {unknown} unrecognised key{'s' if unknown != 1 else ''} (kept)"
            " · run dash --forensic to see them"
        )
    return other


def dashboard(
    root: Path,
    settings: dict[str, Any],
    warnings: list[str],
    color: bool = True,
    forensic: bool = False,
    probe_interval: float | None = None,
    width: int = 120,
    refresh_snapshot: Any = None,
    view_state: DashboardViewState | None = None,
    *,
    height: int | None = None,
    at: float | None = None,
    monotonic_at: float | None = None,
) -> str:
    """Render the provider pool as a width-bounded, optionally height-bounded table."""
    width = max(1, int(width))
    current = time.time() if at is None else float(at)
    current_monotonic = time.monotonic() if monotonic_at is None else float(monotonic_at)
    snapshot = read_pool_snapshot(root)
    refresh_transients(snapshot, root, settings)
    refresh_passive_usage(snapshot, root, settings)
    badges = role_badges(settings)
    mode, mode_reason = effective_claude_mode(settings)
    provider = snapshot["providers"]["claude"]
    limits = load_claude_rate_limits(root, at=current) if mode != "off" else {}
    operator = schema3_operator_report(
        settings, snapshot, current=current, claude_limits=limits
    )
    del probe_interval  # freshness is shared with routing, not redraw cadence
    background_error = False
    try:
        background_jobs = dispatch_client.live_jobs(root, settings, now=current)
    except DispatchError:
        background_jobs = []
        background_error = True
    background_counts = dispatch_client.background_counts(background_jobs)

    seat_rows: list[dict[str, Any]] = []
    attention: list[_AttentionItem] = []
    if background_error:
        attention.append(
            _AttentionItem(
                "red",
                "Security",
                "Background work is unavailable",
                "check the dispatch queue's owner-only permissions",
                "Background work is unavailable: the dispatch queue failed its security check",
                priority=True,
            )
        )
    balances: dict[str, float | None] = {}
    codex_auth = 0
    codex_fits_small = 0
    codex_fits_big = 0
    # The human dashboard is operational: a draining seat stays visible until
    # idle, then inactive identities remain available to machine status and
    # management without occupying a regular provider row.
    displayed_seat_ids = _operational_seat_ids(settings)
    active_seat_ids = set(_active_seat_ids(settings))
    operational_seat_ids = set(_operational_seat_ids(settings))
    for seat_id in displayed_seat_ids:
        seat = snapshot["seats"][seat_id]
        lifecycle = settings["pool"]["seats"][seat_id]["lifecycle"]
        configured_label = str(settings["pool"]["seats"][seat_id]["label"])
        attention_owner = (
            seat_id if configured_label == seat_id else f"{configured_label} ({seat_id})"
        )
        claim = seat.get("in_flight") if isinstance(seat.get("in_flight"), dict) else None
        observed = parse_iso(seat.get("usage_updated_at") or seat.get("probed_at"))
        age_seconds = None if observed is None else max(0.0, current - observed)
        capacity = (
            operator["seat_capacity"][seat_id]
            if operator
            else seat_capacity_report(
                seat_id,
                seat,
                settings,
                current=current,
                learning=snapshot.get("capacity_learning", {}),
            )
        )
        _binding_name, _value, binding_reset = _binding_window(capacity)
        windows = _capacity_window_values(capacity)
        for short, (remaining, _reset) in windows.items():
            balances[f"{seat_id}:{short}"] = remaining
        tier = str(capacity.get("tier") or "unknown")
        stale = tier != "fresh"
        resets, missing_resets = _signal_resets(_seat_limits(seat), at=current)
        auth_failure = seat.get("auth_failure")
        if seat_id in active_seat_ids and capacity["auth_ok"]:
            codex_auth += 1
        if seat_id in active_seat_ids and capacity["fits_small"]:
            codex_fits_small += 1
        if seat_id in active_seat_ids and capacity["fits_big"]:
            codex_fits_big += 1
        source = str(seat.get("usage_source") or "unknown")
        if seat_id in operational_seat_ids and stale:
            attention.append(
                _AttentionItem(
                    "amber",
                    attention_owner,
                    f"{attention_owner} usage is {'unknown' if tier == 'unknown' else 'out of date'}",
                    "refresh its usage",
                    f"capacity {tier}: {capacity['reason']}; source={source}",
                )
            )
        if seat_id in operational_seat_ids and _status_is_error(seat.get("status")):
            attention.append(
                _AttentionItem(
                    "red",
                    attention_owner,
                    f"{attention_owner} provider {_provider_problem(seat.get('status'))}",
                    "refresh it; if that fails, sign in again via `codex-seat dash --manage`",
                    f"provider {seat.get('status')}; source={source}",
                )
            )
        if seat_id in operational_seat_ids and auth_failure:
            attention.append(
                _AttentionItem(
                    "red",
                    attention_owner,
                    f"{attention_owner} authentication failed",
                    "sign in again via `codex-seat dash --manage`",
                    f"authentication failed: {auth_failure}; source={source}",
                )
            )
        if seat_id in operational_seat_ids and capacity.get("profile_window_drift"):
            attention.append(
                _AttentionItem(
                    "amber",
                    attention_owner,
                    f"{attention_owner} quota windows do not match its configured plan",
                    "check the plan in `codex-seat dash --manage`",
                    f"{settings['pool']['seats'][seat_id]['label']} window drift: "
                    f"{capacity['profile_window_drift_reason']}",
                )
            )
        elif seat_id in operational_seat_ids and missing_resets:
            attention.append(
                _AttentionItem(
                    "amber",
                    attention_owner,
                    f"{attention_owner} has no reported reset time",
                    "refresh its usage",
                    "has no authoritative reset window",
                )
            )
        if seat_id in operational_seat_ids and capacity.get("unverified"):
            attention.append(
                _AttentionItem(
                    "amber",
                    attention_owner,
                    f"{attention_owner} plan capacity is unverified",
                    "confirm the plan in `codex-seat dash --manage`",
                    f"{settings['pool']['seats'][seat_id]['label']} capacity unverified; "
                    "see `codex-seat seats list`",
                )
            )
        claim_id = _claim_request_id(claim) if claim else None
        queued_match = next(
            (
                job
                for job in background_jobs
                if claim_id is not None and job.get("request_id") == claim_id
            ),
            None,
        )
        work_label = (
            str(queued_match.get("label") or "managed job")
            if queued_match
            else str(claim.get("label") or claim.get("kind") or "managed job")
            if claim
            else None
        )
        seat_rows.append(
            {
                "kind": "codex",
                "role": badges[seat_id],
                "account": seat_id,
                "name": configured_label,
                "plan": _codex_plan_name(settings, seat_id),
                "windows": windows,
                "fresh": not stale,
                "reset": binding_reset,
                "resets": resets,
                "missing": missing_resets,
                "chip": status_chip(
                    "codex",
                    seat.get("status"),
                    lifecycle=lifecycle,
                    task=work_label,
                    auth_failure=bool(auth_failure),
                    exhausted=_exhausted_window(windows),
                    stale=stale,
                ),
                "fit": _fit_label(capacity) if lifecycle == "active" else "—",
                "age": _usage_age_text(age_seconds).replace("never", "—"),
                "stale": stale,
                "raw": seat,
                "capacity": capacity,
            }
        )

    _claude_binding_name, _claude_value, claude_binding_reset = _claude_binding_window(
        limits
    )
    claude_windows: dict[str, tuple[float | None, float | None]] = {}
    if mode != "off":
        for name in ("five_hour", "seven_day"):
            window = limits.get(name)
            if isinstance(window, dict):
                reset = window.get("resets_at")
                claude_windows[window_label(name, short=True)] = (
                    _claude_remaining(limits, name),
                    float(reset) if isinstance(reset, (int, float)) else None,
                )
    for short, (remaining, _reset) in claude_windows.items():
        balances[f"claude:{short}"] = remaining
    usage_age = claude_usage_age_seconds(limits, at=current)
    claude_stale = mode != "off" and not claude_usage_fresh(settings, limits, at=current)
    claude_resets, claude_missing = _signal_resets(
        limits, at=current, expected_names={"five_hour", "seven_day"}
    )
    claude_error = provider.get("last_error")
    claude_claim = provider.get("in_flight")
    claude_tier = resolve_subscription_tier(
        settings.get("providers", {}).get("claude", {}).get("subscription_tier")
    )["label"] or "tier ?"
    if mode != "off" and claude_stale:
        telemetry_problem = (
            "telemetry is unavailable"
            if usage_age is None
            else f"telemetry is stale ({_usage_age_text(usage_age)})"
        )
        attention.append(
            _AttentionItem(
                "amber",
                "Claude",
                "Claude usage data is unavailable"
                if usage_age is None
                else f"Claude usage data is {_usage_age_text(usage_age)} old",
                "run a Claude turn or refresh Claude usage",
                f"{telemetry_problem}; source=statusline",
            )
        )
    if mode != "off" and _status_is_error(provider.get("status")):
        attention.append(
            _AttentionItem(
                "red",
                "Claude",
                f"Claude provider {_provider_problem(provider.get('status'))}",
                "sign in to Claude again, then refresh usage",
                f"provider {provider.get('status')}; source=statusline",
            )
        )
    if mode != "off" and claude_error:
        attention.append(
            _AttentionItem(
                "red",
                "Claude",
                "Claude provider reported an error",
                "fix it, then refresh usage",
                f"provider error: {claude_error}; source=statusline",
            )
        )
    if mode != "off" and claude_missing:
        attention.append(
            _AttentionItem(
                "amber",
                "Claude",
                "Claude is missing a reset window",
                "refresh Claude usage",
                "missing reset window",
            )
        )
    claude_capacity = claude_capacity_report(
        settings, provider, limits, mode=mode
    )
    claude_claim_id = _claim_request_id(claude_claim) if isinstance(claude_claim, dict) else None
    claude_queued_match = next(
        (
            job
            for job in background_jobs
            if claude_claim_id is not None and job.get("request_id") == claude_claim_id
        ),
        None,
    )
    claude_work_label = (
        str(claude_queued_match.get("label") or "managed job")
        if claude_queued_match
        else str(claude_claim.get("label") or claude_claim.get("kind") or "managed job")
        if isinstance(claude_claim, dict)
        else None
    )
    seat_rows.append(
        {
            "kind": "claude",
            "role": "C",
            "account": "claude",
            "name": "claude",
            "plan": claude_tier,
            "mode": mode,
            "windows": claude_windows,
            "fresh": not claude_stale,
            "reset": claude_binding_reset if mode != "off" else None,
            "resets": claude_resets if mode != "off" else "—",
            "missing": claude_missing and mode != "off",
            "chip": status_chip(
                "claude",
                provider.get("status"),
                mode=mode,
                task=claude_work_label,
                exhausted=_exhausted_window(claude_windows),
                stale=claude_stale,
            ),
            "fit": _fit_label(claude_capacity),
            "age": _usage_age_text(usage_age).replace("never", "—") if mode != "off" else "—",
            "stale": claude_stale,
            "raw": provider,
        }
    )

    generation_raw = _snapshot_value(refresh_snapshot, "completion_generation")
    generation = int(generation_raw) if isinstance(generation_raw, int) else None
    deltas = view_state.observe(balances, generation) if view_state is not None else {}
    result = _snapshot_value(refresh_snapshot, "last_result")
    # Prefer the structured outcome; fall back to prose parsing only for a
    # snapshot that predates the last_succeeded field.
    succeeded = _snapshot_value(refresh_snapshot, "last_succeeded", "unset")
    if succeeded == "unset":
        refresh_failed = _refresh_failed(result)
    else:
        refresh_failed = succeeded is False
    if refresh_failed:
        attention.append(
            _AttentionItem(
                "red",
                "Refresh",
                "The last refresh failed",
                "check the result, then refresh again",
                f"active refresh failed: {result}",
            )
        )
    warning_attention: list[_AttentionItem] = []
    for warning in _settings_warning_lines(warnings, forensic=forensic):
        housekeeping = warning.startswith("Settings file has ")
        priority = warning.startswith("WARNING:")
        warning_attention.append(
            _AttentionItem(
                "amber" if housekeeping else "red",
                "Global",
                warning,
                "",
                warning,
                priority=priority,
            )
        )
    # Global executable warnings must survive height-constrained trimming ahead
    # of per-provider diagnostics; other settings warnings retain their order.
    priority_warnings = [item for item in warning_attention if item.priority]
    other_warnings = [item for item in warning_attention if not item.priority]
    attention = [*priority_warnings, *attention, *other_warnings]

    live_parts, freshness_text = live_header(
        refresh_snapshot, current=current, monotonic_at=current_monotonic
    )
    background_ids = {
        str(job.get("request_id"))
        for job in background_jobs
        if job.get("request_id")
    }
    active_rows: list[tuple[str, dict[str, Any]]] = []
    for seat_id in _operational_seat_ids(settings):
        claim = snapshot.get("seats", {}).get(seat_id, {}).get("in_flight")
        if (
            isinstance(claim, dict)
            and _claim_request_id(claim) not in background_ids
        ):
            active_rows.append((seat_id, claim))
    if (
        isinstance(claude_claim, dict)
        and _claim_request_id(claude_claim) not in background_ids
    ):
        active_rows.append(("C", claude_claim))

    recovering = background_counts.get("recovering", 0)
    expired = background_counts.get("expired", 0)
    if recovering or expired:
        descriptions = []
        if recovering:
            descriptions.append(f"{recovering} recovering")
        if expired:
            descriptions.append(f"{expired} expired")
        attention.append(
            _AttentionItem(
                "red",
                "Work queue",
                f"Background jobs need attention ({', '.join(descriptions)})",
                "inspect their receipts before retrying",
                f"background states: {', '.join(descriptions)}",
            )
        )
    local_rows = _local_seat_rows(root, settings, background_jobs, at=current)
    attention.extend(_local_attention(local_rows))
    grouped_attention, problem_count = _group_attention(attention, forensic=forensic)

    # The header strip uses the one shared summary rule (capacity, manager).
    strip = pool_view.summary(
        pool_view.collect(root, settings, current, snapshot=snapshot, jobs=background_jobs)
    )
    running_jobs = strip["running"]
    queued_jobs = strip["queued"]
    waiting_jobs = strip["waiting_capacity"]
    active_total = len(active_seat_ids)
    ready, ready_total = strip["ready"], strip["total"]
    compact = width < 100
    dense = width < 60
    layout = _table_layout(width)

    from .account import banked_reset_report

    bank_parts: list[str] = []
    bank_unsure: dict[str, list[str]] = {}
    for row in seat_rows:
        if row["kind"] != "codex":
            continue
        bank = banked_reset_report(row["raw"], settings, current, root=root, seat_id=row["account"])
        if bank["status"] == "unknown" and bank["available_count"] is None:
            continue
        if bank["status"] == "fresh" and isinstance(bank["available_count"], int):
            bank_parts.append(f"{row['account']} {bank['available_count']}")
        else:
            label = human("bank_status", bank["status"]).lower()
            bank_unsure.setdefault(label, []).append(row["account"])
    bank_parts += [
        f"count {label} for {', '.join(accounts)}" for label, accounts in bank_unsure.items()
    ]

    summary_parts: list[tuple[str, str | None]] = [
        (f"Ready {ready}/{ready_total}", "ok" if ready == ready_total else "warn"),
        (" · ", "muted"),
        (f"Running {running_jobs}", "live" if running_jobs else None),
        (" · ", "muted"),
        (f"Queued {queued_jobs}", None),
    ]
    if waiting_jobs:
        summary_parts += [(" · ", "muted"), (f"Waiting {waiting_jobs}", "warn")]
    summary_parts += [
        (" · ", "muted"),
        (f"Problems {problem_count}", "ok" if problem_count == 0 else "bad"),
    ]
    if strip["banked_resets"] is not None:
        summary_parts += [(" · ", "muted"), (f"Resets {strip['banked_resets']}", None)]
    header_lines = [
        _line(parts, width, color)
        for parts in header_parts(live_parts, summary_parts, freshness_text, width)
    ]
    lines = [*header_lines, *([] if dense else [""])]

    if layout is not None:
        lines.append(_line(_table_header(layout), width, color))
    codex_rows = [row for row in seat_rows if row["kind"] == "codex"]
    claude_rows = [row for row in seat_rows if row["kind"] == "claude"]

    def render_rows(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            for parts in _seat_row_lines(row, layout, deltas, width=width, at=current):
                lines.append(_line(parts, width, color))

    primary = next((row["account"] for row in codex_rows if row["role"] == "P"), None)
    lines.append(
        _line(
            [
                (f"CODEX {len(codex_rows)}", "heading"),
                (f" · primary {primary} is used last" if primary else "", "muted"),
            ],
            width,
            color,
        )
    )
    render_rows(codex_rows)
    if bank_parts:
        lines.append(
            _line([("  Banked resets  ", "muted"), (" · ".join(bank_parts), "muted")], width, color)
        )
    if not dense:
        lines.append("")
    claude_mode_text = human("claude_mode", seat_rows[-1]["mode"])
    lines.append(
        _line(
            [(f"CLAUDE {len(claude_rows)}", "heading"), (f" · {claude_mode_text}", "muted")],
            width,
            color,
        )
    )
    render_rows(claude_rows)
    if dense:
        lines.append(_line([("Details: widen or use --forensic", "muted")], width, color))

    sections: dict[str, tuple[int, int]] = {"providers": (0, len(lines))}

    local_start = len(lines)
    if local_rows:
        local_ready = sum(row["key"] == "ready" for row in local_rows)
        local_global = local_rows[0]["global_mode"]
        if not dense:
            lines.append("")
        lines.append(
            _line(
                [
                    (f"LOCAL {len(local_rows)}", "heading"),
                    (f" · ready {local_ready}/{len(local_rows)}", "ok" if local_ready else "muted"),
                    (f" · global {human('local_mode', local_global)}", "muted"),
                ],
                width,
                color,
            )
        )
        names = [_local_name(row) for row in local_rows]
        render_rows(
            [
                _local_table_row(row, name if names.count(name) == 1 else f"{name} ({row['seat']})")
                for row, name in zip(local_rows, names)
            ]
        )
    sections["local"] = (local_start, len(lines))

    attention_start = len(lines)
    if grouped_attention:
        if not dense:
            lines.append("")
        lines.append(_rule("NEEDS ATTENTION", str(len(grouped_attention)), width, color, role="warn"))
        for style, message in grouped_attention:
            lines.append(_line([("! ", style), (message, None)], width, color))
    sections["attention"] = (attention_start, len(lines))

    ongoing_start = len(lines)
    if not dense:
        lines.append("")
    ongoing_total = len(background_jobs) + len(active_rows)
    lines.append(
        _rule(
            "ONGOING WORK",
            f"{running_jobs} running" if ongoing_total else "",
            width,
            color,
            role="accent",
        )
    )
    if background_error:
        lines.append(_line([("  unavailable", "bad")], width, color))
    elif not background_jobs and not active_rows:
        lines.append(_line([("  none", "muted")], width, color))
    state_order = {
        "running": 0,
        "queued": 1,
        "waiting_capacity": 2,
        "recovering": 3,
        "expired": 4,
    }
    ongoing_rows: list[tuple[int, str, list[tuple[str, str | None]]]] = []
    for job in background_jobs:
        provider_text = str(job.get("provider") or job.get("requested_provider") or "?")
        if job.get("seat"):
            provider_text += f"/{job['seat']}"
        elapsed_text = duration(job.get("elapsed_seconds", 0) or 0)
        chip = status_chip("work", job.get("state"))
        task_width = max(
            8,
            width
            - display_width(chip.label)
            - (5 if compact else 45),
        )
        task_name = clip(str(job.get("label") or "managed job"), task_width)
        ongoing_rows.append(
            (
                state_order.get(str(job.get("state")), 5),
                f"0:{str(job.get('request_id') or '')}",
                [
                    (f"{chip.glyph} ", chip.role),
                    (task_name, "accent"),
                    (" · ", None),
                    (chip.label, chip.role),
                    (f" · {provider_text} · {job['kind']}/{job['size']} · {elapsed_text}", None),
                    (f" · job {str(job.get('request_id') or '?')[:8]}", "muted"),
                ],
            )
        )
    for role, claim in active_rows:
        raw_tokens = claim.get("tokens")
        token_text = (
            fmt_tokens(int(raw_tokens))
            if isinstance(raw_tokens, (int, float))
            else "tokens ?"
        )
        context_text = (
            f"context {float(claim['context_pct']):g}%"
            if isinstance(claim.get("context_pct"), (int, float))
            else "context ?"
        )
        claim_id = _claim_request_id(claim)
        raw_label = str(claim.get("label") or "")
        task_name = (
            f"Managed {claim.get('kind') or 'work'}"
            if claim_id and raw_label == claim_id
            else raw_label or f"Managed {claim.get('kind') or 'work'}"
        )
        task_width = max(8, width - len("Running") - (5 if compact else 45))
        task_name = clip(task_name, task_width)
        ongoing_rows.append(
            (
                state_order["running"],
                f"1:{role}:{task_name}",
                [
                    ("● ", "live"),
                    (task_name, "accent"),
                    (" · ", None),
                    ("Running", "live"),
                    (f" · {role} · {claim.get('kind') or 'work'} · ", None),
                    (f"{token_text} · {context_text} · {_elapsed(claim.get('started_at'), at=current)}", None),
                    *(([(f" · job {claim_id[:8]}", "muted")]) if claim_id else []),
                ],
            )
        )
    for _state_rank, _identity, parts in sorted(
        ongoing_rows, key=lambda item: (item[0], item[1])
    ):
        lines.append(_line(parts, width, color))
    sections["ongoing"] = (ongoing_start, len(lines))

    history = _terminal_history(snapshot.get("jobs", []))
    history_start = len(lines)
    if history:
        lines.extend(("", _rule("RECENT", f"last {len(history)}", width, color, role="muted")))
        for group in history:
            job = group["job"]
            seat_ref = str(job.get("seat") or "")
            if job.get("provider") == "local":
                role = f"L:{seat_ref}" if seat_ref else "L"
            else:
                role = "claude" if job.get("provider") == "claude" else (seat_ref or "?")
            status_text, status_color = _history_status(str(job.get("status") or "unknown"))
            repeat = f" ×{group['count']}" if group["count"] > 1 else ""
            lines.append(
                _line(
                    [
                        (f"{clock(_job_ts(job), current)}  ", "muted"),
                        (status_text + repeat, status_color),
                        (f" {role:<8} ", None),
                        (f"{fmt_tokens(int(job.get('tokens', 0) or 0)):>6} tokens  ", None),
                        (f"{job['model']}  " if job.get("model") else "", "muted"),
                        (_history_label(job.get("label")), None),
                    ],
                    width,
                    color,
                )
            )
    sections["history"] = (history_start, len(lines))

    forensic_start = len(lines)
    if forensic:
        lines.extend(("", _line([("FORENSIC", "dim")], width, color)))
        if operator:
            lifecycle = operator["lifecycle"]
            lines.append(
                _line(
                    [
                        (
                            "  Lifecycle "
                            f"active {lifecycle['active']} · draining {lifecycle['draining']} · "
                            f"inactive {lifecycle['inactive']} · retired {lifecycle['retired']}",
                            None,
                        )
                    ],
                    width,
                    color,
                )
            )
            lines.append(
                _line(
                    [
                        (
                            f"  Routing: Codex authenticated {codex_auth}/{active_total} · "
                            f"small fit {codex_fits_small}/{active_total} · big fit {codex_fits_big}/{active_total}",
                            None,
                        )
                    ],
                    width,
                    color,
                )
            )
            lines.append(
                _line(
                    [
                        (
                            f"  Concurrency ceilings: current Codex {operator['current_codex_head_ceiling']} · "
                            f"default primary Codex {operator['default_primary_codex_head_ceiling']} · "
                            f"Claude {operator['claude_head_ceiling']} · mixed "
                            f"{operator['conservative_mixed_origin_ceiling']} · safety "
                            f"{operator['global_safety_ceiling']} · Codex identities "
                            f"{operator['effective_worker_slots']['codex']}",
                            None,
                        )
                    ],
                    width,
                    color,
                )
            )
        else:
            lines.append(
                _line(
                    [
                        (
                            f"  Routing: Codex authenticated {codex_auth}/{active_total} · "
                            f"small fit {codex_fits_small}/{active_total} · big fit {codex_fits_big}/{active_total}",
                            None,
                        )
                    ],
                    width,
                    color,
                )
            )
        for row in seat_rows[:-1]:
            seat = row["raw"]
            claim = seat.get("in_flight") if isinstance(seat.get("in_flight"), dict) else None
            raw_tokens = int(claim.get("tokens", 0) or 0) if claim else int(seat.get("spent_since_probe", 0) or 0)
            lines.append(
                _line(
                    [
                        (
                            f"  {row['role']} reserve={reserve_for(settings, row['account'], seat):g}% · "
                            f"source={seat.get('usage_source') or '-'} · all resets={row['resets']}",
                            None,
                        )
                    ],
                    width,
                    color,
                )
            )
            lines.append(
                _line(
                    [(f"    last pool job={seat.get('last_job_at') or '-'}", None)],
                    width,
                    color,
                )
            )
            lines.append(
                _line(
                    [
                        (
                            f"    raw={('▶' if claim else 'Δ')}{fmt_tokens(raw_tokens)} · "
                            f"observed={_observed_reset_summary(seat, at=current)}",
                            None,
                        )
                    ],
                    width,
                    color,
                )
            )
        lines.append(
            _line(
                [
                    (f"  C source=statusline · all resets={seat_rows[-1]['resets']}", None),
                ],
                width,
                color,
            )
        )
        lines.append(
            _line(
                [(f"    last pool job={provider.get('last_job_at') or '-'}", None)],
                width,
                color,
            )
        )
        lines.append(
            _line(
                [(f"    raw=Σ{fmt_tokens(_provider_tokens_7d(snapshot['jobs']))}", None)],
                width,
                color,
            )
        )
        for row in local_rows:
            lines.append(
                _line(
                    [
                        (
                            f"  L:{row['seat']} host={row['host']}/{row['resource_group']} · "
                            f"mode={row['mode']} (global {row['global_mode']}, seat {row['seat_mode']}) · "
                            f"lifecycle={row['lifecycle']} · reason={row['reason'] or '-'} · "
                            f"probe age={row['age_text']}",
                            None,
                        )
                    ],
                    width,
                    color,
                )
            )
    sections["forensic"] = (forensic_start, len(lines))
    secondary_start = len(lines)
    if mode_reason and mode != "off":
        lines.append(_line([("  Claude mode: ", "dim"), (mode_reason, None)], width, color))
    rotation = _rotation_warning(snapshot, settings)
    if rotation:
        lines.append(_line([("  ! ", "amber"), (f"{rotation[1]} — {rotation[2]}", None)], width, color))
    _notify_rotation_once(root, rotation)
    sections["secondary"] = (secondary_start, len(lines))
    lines = _bound_dashboard_lines(
        lines, height=height, width=width, color=color, sections=sections
    )
    return "\n".join(lines)


def _job_ts(job: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(job.get("ts", "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _provider_tokens_7d(jobs: list[dict[str, Any]]) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    return sum(
        int(job.get("tokens", 0))
        for job in jobs
        if job.get("provider") == "claude" and _job_ts(job) > cutoff
    )


def _elapsed(started_at: str | None, *, at: float | None = None) -> str:
    started = parse_iso(started_at)
    if started is None:
        return "?"
    current = datetime.now(timezone.utc).timestamp() if at is None else at
    seconds = max(0, round(current - started))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def segment(root: Path, settings: dict[str, Any], plain: bool = False) -> str:
    with Ledger(root) as data:
        refresh_transients(data, root, settings)
        refresh_passive_usage(data, root, settings)
        snapshot = json.loads(json.dumps(data))
    items: list[tuple[str, str]] = _control_indicators(snapshot, settings)
    mode, _ = effective_claude_mode(settings)
    provider = snapshot["providers"]["claude"]
    limits = load_claude_rate_limits(root) if mode != "off" else {}
    operator = schema3_operator_report(
        settings, snapshot, current=time.time(), claude_limits=limits
    )
    if operator:
        lifecycle = operator["lifecycle"]
        items.extend(
            [
                ((f"H:{operator['healthy']}/{operator['active']}"), "green"),
                (
                    f"L:A{lifecycle['active']}/D{lifecycle['draining']}/"
                    f"I{lifecycle['inactive']}/R{lifecycle['retired']}",
                    "gray",
                ),
                (
                    f"MAX:C{operator['current_codex_head_ceiling']}/"
                    f"D{operator['default_primary_codex_head_ceiling']}/"
                    f"H{operator['claude_head_ceiling']}/"
                    f"S{operator['global_safety_ceiling']}",
                    "gray",
                ),
            ]
        )
    segment_seat_ids = _operational_seat_ids(settings)
    for seat_id in segment_seat_ids:
        seat = snapshot["seats"][seat_id]
        lifecycle = settings["pool"]["seats"][seat_id]["lifecycle"]
        pct = "?" if seat.get("pct_left") is None else str(seat["pct_left"])
        suffix = seat_activity_symbol(seat)
        reserve = reserve_for(settings, seat_id, seat)
        reserve_mark = "R" if reserve else ""
        color_name = seat_color(seat_id, seat, settings)
        if operator and lifecycle == "active":
            entry = settings["pool"]["seats"][seat_id]
            multiplier = operator["seat_capacity"][seat_id][
                "effective_relative_multiplier"
            ]
            named = seat_id if entry["label"] == seat_id else f"{entry['label']}({seat_id})"
            seat_name = f"{named}@{multiplier:g}x"
        elif lifecycle == "active":
            seat_name = seat_id
        else:
            seat_name = f"{seat_id}[draining]"
        items.append(
            (
                f"{seat_name}:"
                f"{pct}{reserve_mark}{suffix}"
                f"{_next_reset_badge(seat.get('last_probe_limits', {}))}",
                color_name,
            )
        )
    symbol = claude_activity_symbol(mode, provider)
    model_health = _claude_model_health(provider)
    if mode == "off":
        ctext = f"C:OFF{symbol}"
    elif provider.get("status") == "cooldown":
        ctext = f"C:CAP{symbol}"
    elif provider.get("status") not in {"ok", "off"}:
        ctext = f"C:ERR{symbol}"
    elif provider.get("in_flight"):
        active_model = str(provider["in_flight"].get("model") or "?")
        ctext = f"C:{_model_family(active_model)}-RUN{symbol}"
    elif limits:
        stale = "~" if not claude_usage_fresh(settings, limits) else ""
        ctext = (
            f"C:{model_health}{stale}{_pct(_claude_remaining(limits, 'five_hour'))}/"
            f"{_pct(_claude_remaining(limits, 'seven_day'))}{symbol}"
        )
    else:
        ctext = {
            "opportunistic": f"C:OPP{symbol}",
            "review_only": f"C:REV{symbol}",
            "manual_only": f"C:MAN{symbol}",
        }.get(mode, f"C:?{symbol}")
    if mode != "off" and limits:
        ctext += _next_reset_badge(limits)
    items.append((ctext, claude_color(mode, provider, limits)))
    background_error = False
    try:
        background = dispatch_client.live_jobs(root, settings)
    except DispatchError:
        background = []
        background_error = True
    background_ids = {job["request_id"] for job in background}
    active = sum(
        bool(claim)
        and not (
            isinstance(claim, dict) and claim.get("label") in background_ids
        )
        for claim in (
            snapshot["seats"][seat_id].get("in_flight") for seat_id in segment_seat_ids
        )
    )
    claude_active = provider.get("in_flight")
    active += int(
        bool(claude_active)
        and not (
            isinstance(claude_active, dict)
            and claude_active.get("label") in background_ids
        )
    )
    if active:
        items.append((f"J:{active}", "cyan"))
    if background:
        counts = dispatch_client.background_counts(background)
        items.append((f"BG:{_background_count_text(counts)}", "cyan"))
    elif background_error:
        items.append(("BG:ERR", "red"))
    rotation = _rotation_warning(snapshot, settings)
    if rotation:
        items.append((rotation[1], "red"))
    _notify_rotation_once(root, rotation)
    if plain:
        return " ".join(text for text, _ in items)
    return "".join(
        f"#[fg={TMUX[color]},bg=#1e1e2e,bold] {text} #[default]" for text, color in items
    )
