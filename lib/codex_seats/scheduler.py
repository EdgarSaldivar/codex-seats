"""Capacity-window identity.

All that survives of the old scheduler after v7 dropped the second seat-selector
(`plan_assignment` + its reservation/candidate machinery — `pick_seat` is the one
live selector). What remains is the canonical live-window identity that
`provider_claude`/`provider_codex` use for capacity learning: which rolling
windows are active and when they reset. (Kept here rather than moved into
`capacity.py` only because that module already defines a different `_window_pct`.)
"""

from __future__ import annotations

import json
import time
from typing import Any


def _window_pct(window: dict[str, Any]) -> float | None:
    for key in ("pct", "pct_left", "remaining_percentage"):
        value = window.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    used = window.get("used_percentage", window.get("used_pct"))
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        return 100.0 - float(used)
    return None


def _window_reset(window: dict[str, Any]) -> float | None:
    value = window.get("resets_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _normalized_windows(limits: dict[str, Any], current: float) -> list[tuple[str, float, float | None]]:
    windows: list[tuple[str, float, float | None]] = []
    aliases = {
        "five_hour": "5h",
        "seven_day": "weekly",
        "five-hour": "5h",
        "seven-day": "weekly",
    }
    for raw_name, raw in limits.items():
        if not isinstance(raw, dict):
            continue
        pct = _window_pct(raw)
        reset = _window_reset(raw)
        if pct is None or (reset is not None and reset <= current):
            continue
        windows.append((aliases.get(str(raw_name), str(raw_name)), pct, reset))
    return windows


def _binding_window(limits: dict[str, Any], current: float) -> tuple[str | None, float | None, str | None]:
    windows = _normalized_windows(limits, current)
    if not windows:
        return None, None, None
    # The lowest remaining window binds. Equal balances use the earliest reset.
    name, _pct, reset = min(
        windows,
        key=lambda item: (item[1], item[2] if item[2] is not None else float("inf"), item[0]),
    )
    identity = json.dumps(
        {window_name: window_reset for window_name, _left, window_reset in windows},
        sort_keys=True,
        separators=(",", ":"),
    )
    return name, reset, identity


def capacity_reset_identity(
    limits: dict[str, Any], *, current: float | None = None
) -> str | None:
    """Return the canonical live-window identity used by learning and routing."""
    _name, _reset, identity = _binding_window(
        limits, time.time() if current is None else float(current)
    )
    return identity
