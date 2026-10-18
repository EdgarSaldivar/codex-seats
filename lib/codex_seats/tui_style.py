"""One terminal design system for the dashboard and the seat manager.

Everything user-facing in the terminal takes its colours, status vocabulary,
labels, time formats and text fitting from here, so the two surfaces cannot
drift apart.  Colour is always an explicit ``color`` flag; callers decide it
from ``--no-color`` / ``NO_COLOR`` and a TTY check, never from this module.
"""

from __future__ import annotations

import math
import time
import unicodedata
from datetime import datetime
from typing import Any, NamedTuple

from .seat_registry import CAPACITY_PLAN_PRESETS
from .terminal import clean_text, display_width, fit_text, wrap_lines

__all__ = [
    "ANSI",
    "TMUX",
    "ROLES",
    "Chip",
    "ago",
    "age",
    "clean_text",
    "clip",
    "clock",
    "countdown",
    "day",
    "display_width",
    "duration",
    "fit_text",
    "human",
    "one_line",
    "paint",
    "render_parts",
    "stamp",
    "status_chip",
    "strip_ansi",
    "until",
    "usage_bar",
    "usage_role",
    "window_label",
    "wrap_lines",
]

# -- palette -------------------------------------------------------------------

_HEX = {
    "green": "#50fa7b",
    "cyan": "#8be9fd",
    "amber": "#f1fa8c",
    "red": "#ff5555",
    # Lighter than Dracula's comment colour so muted text keeps its contrast.
    "gray": "#8a93bd",
    "purple": "#bd93f9",
    "track": "#6272a4",
}

#: Semantic role → base colour name.  Amber means "warning" and nothing else.
ROLES = {
    "ok": "green",
    "warn": "amber",
    "bad": "red",
    "muted": "gray",
    "accent": "cyan",
    "live": "cyan",
    "heading": "purple",
    "title": "purple",
    "track": "track",
}


def _fg(hex_value: str) -> str:
    red, green, blue = (int(hex_value[index:index + 2], 16) for index in (1, 3, 5))
    return f"\033[38;2;{red};{green};{blue}m"


ANSI: dict[str, str] = {
    **{name: _fg(value) for name, value in _HEX.items()},
    **{role: _fg(_HEX[name]) for role, name in ROLES.items()},
    "dim": "\033[2m",
    "bold": "\033[1m",
    "selected": "\033[1;48;2;68;71;90m",
    "reset": "\033[0m",
}

#: tmux ``#[fg=…]`` colours, keyed by the same names and roles as :data:`ANSI`.
TMUX: dict[str, str] = {
    **_HEX,
    **{role: _HEX[name] for role, name in ROLES.items()},
}


def paint(text: str, role: str | None, color: bool) -> str:
    """Wrap ``text`` in one role's colour; a no-op without colour or a role."""
    if not color or not role or not text or role not in ANSI:
        return text
    return f"{ANSI[role]}{text}{ANSI['reset']}"


def strip_ansi(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\033" and text[index + 1:index + 2] == "[":
            end = index + 2
            while end < len(text) and not ("@" <= text[end] <= "~"):
                end += 1
            index = end + 1
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


# -- text fitting ----------------------------------------------------------------


def one_line(value: Any) -> str:
    """Printable single-line text: line breaks become spaces, controls vanish."""
    characters: list[str] = []
    for character in str(value):
        if character in "\r\n\t":
            characters.append(" ")
        elif not unicodedata.category(character).startswith("C"):
            characters.append(character)
    return "".join(characters)


def clip(value: Any, width: int) -> str:
    """Fit one line into ``width`` cells, marking any cut with an ellipsis."""
    text = one_line(value)
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    kept: list[str] = []
    used = 0
    for character in text:
        cells = display_width(character)
        if used + cells > width - 1:
            break
        kept.append(character)
        used += cells
    return "".join(kept) + "…"


Parts = list[tuple[str, str | None]]


def render_parts(parts: Parts, width: int, color: bool) -> str:
    """Join ``(text, role)`` parts, clipped to ``width`` cells and right-trimmed."""
    remaining = max(1, width)
    clipped: list[tuple[str, str | None]] = []
    for raw, role in parts:
        if remaining <= 0:
            break
        text = clip(raw, remaining)
        clipped.append((text, role))
        remaining -= display_width(text)
    # Trim trailing blanks before painting so coloured and plain rows match.
    while clipped and not clipped[-1][0].rstrip():
        clipped.pop()
    if clipped:
        clipped[-1] = (clipped[-1][0].rstrip(), clipped[-1][1])
    return "".join(paint(text, role, color) for text, role in clipped)


# -- usage ---------------------------------------------------------------------

_PARTIALS = " ▏▎▍▌▋▊▉"


def usage_role(pct: float | None, fresh: bool = True) -> str:
    """Threshold colour for remaining usage: <20 bad, 20–49 warn, ≥50 ok."""
    if pct is None or not fresh:
        return "muted"
    if pct < 20:
        return "bad"
    if pct < 50:
        return "warn"
    return "ok"


def usage_bar(
    pct: float | None, cells: int, *, fresh: bool = True, percent: bool = True
) -> Parts:
    """Eighth-block bar of remaining usage, plus a right-aligned ``NN%``.

    The result is ``cells`` wide (``cells + 5`` with ``percent``).  Unknown
    usage renders as ``—`` padded to the same width.
    """
    cells = max(0, int(cells))
    total = cells + (5 if percent else 0)
    if pct is None:
        return [("—", "muted"), (" " * max(0, total - 1), None)]
    value = min(100.0, max(0.0, float(pct)))
    role = usage_role(value, fresh)
    eighths = int(value / 100 * cells * 8 + 0.5)
    full, partial = divmod(eighths, 8)
    bar = "█" * full + (_PARTIALS[partial] if partial and full < cells else "")
    parts: Parts = []
    if bar:
        parts.append((bar, role))
    if cells - display_width(bar) > 0:
        parts.append(("░" * (cells - display_width(bar)), "track"))
    if percent:
        parts.append((f" {value:>3.0f}%", role))
    return parts


# -- status vocabulary ----------------------------------------------------------


class Chip(NamedTuple):
    glyph: str
    label: str
    role: str

    @property
    def text(self) -> str:
        return f"{self.glyph} {self.label}"


WORKING, IDLE, PARTIAL, ERROR, OFF = "●", "○", "◐", "×", "–"
_SIGN_IN = {"needs-login", "needs-auth", "needs_login", "needs_auth"}


def status_chip(kind: str, state: Any, **ctx: Any) -> Chip:
    """Map one raw state to the shared ``(glyph, label, role)`` vocabulary.

    ``●`` working (label is the task), ``○`` idle/ready, ``◐`` stale or
    capped, ``×`` error / needs sign-in / model mismatch, ``–`` paused/off.

    ``kind`` is ``codex``, ``claude``, ``local``, ``lifecycle`` or ``work``.
    Context: ``task`` (running work label), ``lifecycle``, ``mode`` (Claude or
    local), ``auth_failure``, ``exhausted`` (short window name such as
    ``7d``), ``stale``, ``age`` (text appended to a stale label).
    """
    raw = str(state if state is not None else "unknown").strip().lower()
    task = ctx.get("task")
    exhausted = ctx.get("exhausted")
    if kind == "lifecycle":
        return {
            "active": Chip(IDLE, "active", "ok"),
            "draining": Chip(PARTIAL, "draining", "warn"),
            "inactive": Chip(OFF, "paused", "muted"),
            "retired": Chip(OFF, "retired", "muted"),
        }.get(raw, Chip(PARTIAL, raw or "unknown", "muted"))
    if kind == "work":
        return {
            "running": Chip(WORKING, "Running", "live"),
            "queued": Chip(IDLE, "Queued", "warn"),
            "waiting_capacity": Chip(PARTIAL, "Waiting for capacity", "warn"),
            "recovering": Chip(ERROR, "Recovering", "bad"),
            "expired": Chip(ERROR, "Expired", "bad"),
        }.get(raw, Chip(PARTIAL, human("work", raw), "warn"))
    if kind == "local":
        stale_label = "stale" + (f" {ctx['age']}" if ctx.get("age") else "")
        if raw == "working":
            return Chip(WORKING, str(task or "working"), "live")
        return {
            "ready": Chip(IDLE, "ready", "ok"),
            "shadow": Chip(IDLE, "shadow", "muted"),
            "asleep": Chip(IDLE, "asleep", "muted"),
            "gaming": Chip(PARTIAL, "gaming", "warn"),
            "loading": Chip(PARTIAL, "loading", "warn"),
            "busy": Chip(PARTIAL, "busy", "warn"),
            "stale": Chip(PARTIAL, stale_label, "warn"),
            "degraded": Chip(ERROR, "degraded", "bad"),
            "model_mismatch": Chip(ERROR, "model mismatch", "bad"),
            "off": Chip(OFF, "off", "muted"),
            "unknown": Chip(PARTIAL, "not probed", "muted"),
        }.get(raw, Chip(PARTIAL, raw or "unknown", "muted"))
    if kind == "claude":
        if str(ctx.get("mode") or "") == "off" or raw == "off":
            return Chip(OFF, "off", "muted")
        if task:
            return Chip(WORKING, str(task), "live")
        if raw in _SIGN_IN:
            return Chip(ERROR, "needs sign-in", "bad")
        if raw == "missing":
            return Chip(ERROR, "not installed", "bad")
        if raw == "cooldown":
            return Chip(PARTIAL, "cooldown", "warn")
        if raw == "capped" or (raw == "ok" and exhausted):
            return Chip(PARTIAL, "capped" + (f" {exhausted}" if exhausted else ""), "bad")
        if raw == "ok":
            return Chip(PARTIAL, "stale", "warn") if ctx.get("stale") else Chip(IDLE, "idle", "ok")
        return Chip(ERROR, "error", "bad")
    # codex (default)
    lifecycle = str(ctx.get("lifecycle") or "active").lower()
    if lifecycle == "retired":
        return Chip(OFF, "retired", "muted")
    if lifecycle == "inactive":
        return Chip(OFF, "paused", "muted")
    if task:
        return Chip(WORKING, str(task), "live")
    if ctx.get("auth_failure") or raw in _SIGN_IN:
        return Chip(ERROR, "needs sign-in", "bad")
    if raw == "capped" or (raw == "ok" and exhausted):
        return Chip(PARTIAL, "capped" + (f" {exhausted}" if exhausted else ""), "bad")
    if raw == "cooldown":
        return Chip(PARTIAL, "cooldown", "warn")
    if raw == "unknown":
        return Chip(PARTIAL, "not probed", "muted")
    if raw != "ok":
        return Chip(ERROR, "error", "bad")
    if lifecycle == "draining":
        return Chip(PARTIAL, "draining", "warn")
    if ctx.get("stale"):
        return Chip(PARTIAL, "stale", "warn")
    return Chip(IDLE, "idle", "ok")


# -- human labels ----------------------------------------------------------------

_PLAN_LABELS = {
    preset["profile"]: preset["label"].split(" (", 1)[0] for preset in CAPACITY_PLAN_PRESETS
}
_OBSERVED_PLANS = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Business",
    "business": "Business",
    "enterprise": "Enterprise",
    "edu": "Edu",
}
_LABELS: dict[str, dict[str, str]] = {
    "claude_mode": {
        "off": "Off",
        "manual_only": "Manual only",
        "review_only": "Reviews only",
        "opportunistic": "Opportunistic",
    },
    "local_mode": {
        "off": "Off",
        "shadow": "Shadow",
        "canary": "Canary",
        "opportunistic": "Opportunistic",
    },
    # Only primary and worker exist; a stray legacy "deputy" reads as a worker.
    "role": {"primary": "Primary", "worker": "Worker", "deputy": "Worker",
             "P": "Primary", "W": "Worker", "D": "Worker", "C": "Claude", "L": "Local"},
    "lifecycle": {
        "active": "Active",
        "draining": "Draining",
        "inactive": "Paused",
        "retired": "Retired",
    },
    "review.mode": {"off": "Off", "advisory": "Advisory", "blocking": "Blocking"},
    "review.strategy": {"single": "Single reviewer", "cross": "Cross-provider"},
    "execution.delegation_policy": {
        "auto": "Automatic",
        "always": "Always delegate",
        "manual": "Manual",
    },
    "execution.head_permissions": {"safe": "Safe", "full-access": "Full access"},
    "bank_status": {
        "fresh": "Current",
        "stale": "Stale",
        "unknown": "Unknown",
        "unavailable": "Unavailable",
    },
    "capacity_tier": {"fresh": "Fresh", "degraded": "Degraded", "unknown": "Unknown"},
    "fit": {
        "S+B": "small+big",
        "S": "small",
        "B": "big",
        "none": "none",
        "—": "—",
    },
    "reason": {
        "lifecycle_ineligible": "Paused",
        "mode_off": "Off",
        "shadow_only": "Shadow only",
        "unknown_host": "Unknown host",
        "unknown_seat": "Unknown seat",
        "no_enabled_seats": "No enabled seats",
        "availability_stale": "Probe is stale",
        "availability_missing_or_invalid": "Not probed yet",
        "configuration_changed": "Configuration changed",
        "model_profile_mismatch": "Model mismatch",
        "capability_mismatch": "Cannot run this task",
        "host_mismatch": "Wrong host",
        "host_not_ready": "Host not ready",
        "task_policy_ineligible": "Not allowed by task policy",
        "capacity_claimed": "Busy",
        "unsafe_claim_runtime": "Unsafe runtime",
        "probe_exit_nonzero": "Probe failed",
        "probe_failed": "Probe failed",
        "probe_timeout": "Probe timed out",
        "probe_output_limit": "Probe output too large",
        "probe_invalid_output": "Probe output invalid",
        "probe_invalid_result": "Probe output invalid",
        "ready": "Ready",
    },
}
_FIELD_ALIASES = {
    "mode": "claude_mode",
    "claude.mode": "claude_mode",
    "providers.claude.mode": "claude_mode",
    "local.mode": "local_mode",
    "delegation_policy": "execution.delegation_policy",
    "head_permissions": "execution.head_permissions",
    "eligibility": "reason",
    "plan": "capacity_profile",
    "profile": "capacity_profile",
    "observed_plan_type": "plan_type",
    "bank": "bank_status",
    "tier": "capacity_tier",
}
_WINDOW_NAMES = {
    "5h": 300,
    "five_hour": 300,
    "five-hour": 300,
    "primary": 300,
    "weekly": 10080,
    "seven_day": 10080,
    "seven-day": 10080,
    "secondary": 10080,
    "monthly": 43200,
}


def _fallback(value: Any) -> str:
    text = one_line(value).replace("_", " ").replace("-", " ").strip()
    text = " ".join(text.split())
    return text[:1].upper() + text[1:] if text else "—"


def window_label(name: Any = None, minutes: Any = None, *, short: bool = False) -> str:
    """``5-hour`` / ``7-day`` (or ``5h`` / ``7d``) from minutes, else the name."""
    if not (isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0):
        minutes = _WINDOW_NAMES.get(str(name)) if name is not None else None
        if minutes is None and isinstance(name, (int, float)) and not isinstance(name, bool):
            minutes = name
    if isinstance(minutes, (int, float)) and minutes > 0:
        minutes = float(minutes)
        if minutes % 1440 == 0:
            amount, unit = int(minutes // 1440), "d" if short else "-day"
        elif minutes % 60 == 0:
            amount, unit = int(minutes // 60), "h" if short else "-hour"
        else:
            amount, unit = int(minutes), "m" if short else "-minute"
        return f"{amount}{unit}"
    return "?" if name is None else _fallback(name).lower() if short else _fallback(name)


def human(field: str, value: Any) -> str:
    """User-facing label for an enum value; unknown values never raise."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    field = _FIELD_ALIASES.get(field, field)
    if field == "window":
        return window_label(value)
    if field == "capacity_profile":
        if value in {"", "unverified"}:
            return "Unverified"
        return _PLAN_LABELS.get(str(value), _fallback(value))
    if field == "plan_type":
        text = str(value).strip().lower()
        if text.startswith("self_serve_business") or text.startswith("business"):
            return "Business"
        return _OBSERVED_PLANS.get(text, _fallback(value))
    table = _LABELS.get(field, {})
    key = str(value)
    if key in table:
        return table[key]
    if key.lower() in table:
        return table[key.lower()]
    return _fallback(value)


# -- time ------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def countdown(
    ts: Any, now: float | None = None, *, compact: bool = False, seconds: bool = False
) -> str:
    """Time until ``ts``: ``2h15m`` / ``6d04h`` (``2h`` / ``6d`` when compact).

    Minutes round up so a reset is never reported early.  Below one minute
    the result is ``1m`` unless ``seconds`` asks for ``45s``.
    """
    target = _number(ts)
    if target is None:
        return "?"
    remaining = max(0, int(target - _now(now)))
    if remaining <= 0:
        return "now"
    if seconds and remaining < 60:
        return f"{remaining}s"
    minutes = max(1, (remaining + 59) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if compact or minutes == 0 else f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if compact or hours == 0 else f"{days}d{hours:02d}h"


def until(ts: Any, now: float | None = None) -> str:
    """``in 2h15m`` / ``in 6d04h`` / ``in 45s`` / ``now`` (``—`` if unknown)."""
    text = countdown(ts, now, seconds=True)
    if text == "?":
        return "—"
    return text if text == "now" else f"in {text}"


def age(seconds: Any) -> str:
    """Compact elapsed time: ``12s`` / ``3m`` / ``2.8h`` / ``3d`` (``never``)."""
    value = _number(seconds)
    if value is None:
        return "never"
    value = max(0.0, value)
    if value < 60:
        return f"{int(value)}s"
    if value < 3600:
        return f"{int(value // 60)}m"
    if value < 86400:
        return f"{value / 3600:.1f}h"
    return f"{int(value // 86400)}d"


def ago(ts: Any, now: float | None = None) -> str:
    """``12s ago`` / ``3m ago`` / ``2.8h ago`` / ``3d ago`` (``never``)."""
    stamp_value = _number(ts)
    if stamp_value is None:
        return "never"
    return f"{age(_now(now) - stamp_value)} ago"


def duration(seconds: Any) -> str:
    """Elapsed run time: ``45s`` / ``14m05s`` / ``2h05m``."""
    value = _number(seconds)
    if value is None:
        return "?"
    total = max(0, int(round(value)))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def stamp(ts: Any, now: float | None = None) -> str:
    """Absolute local time: ``Oct 18 14:00`` this year, else ``Oct 18 2025``."""
    value = _number(ts)
    if value is None:
        return "unknown"
    moment = _local(value)
    if moment.year == _local(_now(now)).year:
        return f"{moment:%b} {moment.day} {moment:%H:%M}"
    return f"{moment:%b} {moment.day} {moment.year}"


def day(ts: Any, now: float | None = None) -> str:
    """Absolute local date: ``Nov 2`` this year, else ``Nov 2 2025``."""
    value = _number(ts)
    if value is None:
        return "unknown"
    moment = _local(value)
    if moment.year == _local(_now(now)).year:
        return f"{moment:%b} {moment.day}"
    return f"{moment:%b} {moment.day} {moment.year}"


def clock(ts: Any, now: float | None = None) -> str:
    """``06:12`` for today, else the ``day`` form; ``--:--`` when unknown."""
    value = _number(ts)
    if value is None or value <= 0:
        return "--:--"
    moment = _local(value)
    if moment.date() == _local(_now(now)).date():
        return f"{moment:%H:%M}"
    return day(value, now)
