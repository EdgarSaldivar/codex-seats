"""Shared explicit schema-3 seat registries for tests.

Settings have exactly one format: a dynamic registry under ``pool.seats``.
Tests that need Codex capacity build it here instead of relying on any
implicit topology, and write it to the pool root when runtime code reloads the
authoritative file.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from codex_seats.state import atomic_write_json, default_settings  # noqa: E402


def seat_entry(
    label: str,
    *,
    lifecycle: str = "active",
    home: str | None = None,
    profile: str = "personal-plus",
    relative_capacity: float | None = 1.0,
    verified_at: str | None = None,
) -> dict[str, Any]:
    """One registry entry; a null multiplier means unverified capacity."""
    verified = relative_capacity is not None
    return {
        "label": label,
        "lifecycle": lifecycle,
        "home": home,
        "capacity_profile": profile,
        "relative_capacity": relative_capacity,
        "capacity_source": "openai-published-baseline" if verified else "unverified",
        "capacity_verified_at": (
            (verified_at or "2026-09-03T00:00:00Z") if verified else None
        ),
    }


def registry_settings(
    seats: int | list[str] = 5,
    *,
    primary: str | None = "seat1",
    deputy: str | None = "seat2",
    primary_home: str | Path | None = None,
) -> dict[str, Any]:
    """Default settings plus active seats (``seat1..seatN`` for an int)."""
    seat_ids = (
        [f"seat{number}" for number in range(1, seats + 1)]
        if isinstance(seats, int)
        else list(seats)
    )
    settings = default_settings()
    pool = settings["pool"]
    pool["seats"] = {
        seat_id: seat_entry(
            seat_id,
            home=(
                str(primary_home)
                if primary_home is not None and seat_id == primary
                else None
            ),
        )
        for seat_id in seat_ids
    }
    pool["primary"] = primary if primary in pool["seats"] else None
    pool["deputy"] = deputy if deputy in pool["seats"] else None
    return settings


def write_settings(root: str | Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Persist ``settings`` as the authoritative owner-only settings.json."""
    path = Path(root)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_json(path / "settings.json", settings, mode=0o600)
    return settings
