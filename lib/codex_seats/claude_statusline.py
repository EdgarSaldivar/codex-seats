#!/usr/bin/env python3
"""Passively cache Claude subscription limits from status-line session data."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _window(raw, observed_at=None):
    if not isinstance(raw, dict):
        return None
    try:
        used = float(raw["used_percentage"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= used <= 100:
        return None
    resets_at = raw.get("resets_at")
    try:
        resets_at = None if resets_at is None else float(resets_at)
    except (TypeError, ValueError):
        return None
    captured_at = raw.get("observed_at", observed_at)
    try:
        captured_at = None if captured_at is None else float(captured_at)
    except (TypeError, ValueError):
        captured_at = None
    return {
        "used_percentage": used,
        "resets_at": resets_at,
        "observed_at": captured_at,
    }


def _cycle(previous, old_window, new_window, observed_at):
    cycle = dict(previous) if isinstance(previous, dict) else {}
    cycle.setdefault("observed_resets", 0)
    old_reset = old_window.get("resets_at") if isinstance(old_window, dict) else None
    new_reset = new_window.get("resets_at")
    old_used = old_window.get("used_percentage") if isinstance(old_window, dict) else None
    new_used = new_window.get("used_percentage")
    advanced = (
        isinstance(old_reset, (int, float))
        and isinstance(new_reset, (int, float))
        and new_reset > old_reset + 60
    )
    recovered = (
        isinstance(old_used, (int, float))
        and isinstance(new_used, (int, float))
        and new_used <= old_used - 5
    )
    boundary_passed = isinstance(old_reset, (int, float)) and observed_at >= old_reset - 120
    if advanced and (recovered or boundary_passed):
        cycle["observed_resets"] = int(cycle.get("observed_resets", 0)) + 1
        cycle["last_reset_observed_at"] = observed_at
    cycle.update(
        {
            "current_reset_at": new_reset,
            "last_used_percentage": new_used,
            "last_seen_at": observed_at,
        }
    )
    return cycle


def _read(path):
    try:
        payload = json.loads(path.read_text())
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main():
    try:
        session = json.load(sys.stdin)
    except (ValueError, TypeError):
        return 0
    if not isinstance(session, dict):
        return 0

    root = Path(os.environ.get("CODEX_SEATS_ROOT", Path.home() / ".codex-seats"))
    cache_path = root / "claude-usage.json"
    current = time.time()
    previous = _read(cache_path)
    captured = {
        "schema": 2,
        "statusline_at": current,
        "updated_at": previous.get("updated_at"),
    }
    limits = session.get("rate_limits")
    limits = limits if isinstance(limits, dict) else {}
    previous_cycles = previous.get("usage_cycles")
    previous_cycles = previous_cycles if isinstance(previous_cycles, dict) else {}
    cycles = dict(previous_cycles)
    captured_real = False
    for name in ("five_hour", "seven_day"):
        old_window = _window(previous.get(name), previous.get("updated_at"))
        window = _window(limits.get(name), current)
        if window is not None:
            captured_real = True
            cycles[name] = _cycle(previous_cycles.get(name), old_window, window, current)
        else:
            window = old_window
            if window and window.get("resets_at") is not None and window["resets_at"] <= current:
                window = None
        if window is not None:
            captured[name] = window
    if captured_real:
        captured["updated_at"] = current
    captured["usage_cycles"] = cycles
    if any(name in captured for name in ("five_hour", "seven_day")):
        _write(cache_path, captured)

    model = session.get("model")
    model = model.get("display_name") if isinstance(model, dict) else None
    parts = [f"[{model or 'Claude'}]"]
    for name, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = captured.get(name)
        if window:
            parts.append(f"{label} {100 - window['used_percentage']:.0f}% left")
    context = session.get("context_window")
    used = context.get("used_percentage") if isinstance(context, dict) else None
    if isinstance(used, (int, float)):
        parts.append(f"ctx {used:.0f}%")
    print(" · ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
