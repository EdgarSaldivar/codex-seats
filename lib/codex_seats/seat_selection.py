from __future__ import annotations

from typing import Any

from .capacity import seat_capacity_report
from .seat_registry import active_seat_ids, seat_number
from .state import now


def pick_seat(
    seats: dict[str, Any],
    settings: dict[str, Any],
    size: str = "small",
    excluded: set[str] | None = None,
    *,
    learning: dict[str, Any] | None = None,
    model: str = "gpt-6-sol",
    effort: str = "default",
) -> str | None:
    candidates: list[str] = []
    excluded = excluded or set()
    current = now()
    fit_cache: dict[str, bool] = {}
    report_cache: dict[str, dict[str, Any]] = {}

    def capacity_fits(seat_id: str) -> bool:
        if seat_id in fit_cache:
            return fit_cache[seat_id]
        report = seat_capacity_report(
            seat_id,
            seats[seat_id],
            settings,
            current=current,
            learning=learning,
            model=model,
            effort=effort,
        )
        report_cache[seat_id] = report
        fits = bool(report[f"fits_{size}"])
        fit_cache[seat_id] = fits
        return fits

    for seat_id in active_seat_ids(settings):
        if seat_id in excluded:
            continue
        seat = seats.get(seat_id)
        if not isinstance(seat, dict):
            continue
        if seat.get("in_flight"):
            continue
        if not capacity_fits(seat_id):
            continue
        candidates.append(seat_id)
    if not candidates:
        return None

    # Rolling 1x capacity is deliberately perishable: spend it before larger
    # capacity classes. Within a class, compare usable capacity on a common 1x
    # baseline and drain the window nearest reset. Future >5x tiers naturally
    # sort after 5x instead of requiring seat-name special cases.
    def capacity_class(seat_id: str) -> tuple[int, float]:
        value = report_cache[seat_id].get("capacity_multiplier")
        multiplier = (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else float("inf")
        )
        return (0, 1.0) if abs(multiplier - 1.0) < 1e-9 else (1, multiplier)

    def drain_score(seat_id: str) -> float:
        report = report_cache[seat_id]
        binding_name = report[size].get("binding_window")
        binding = next(
            (
                window
                for window in report.get("capacity_windows", [])
                if window.get("name") == binding_name
            ),
            report.get("binding_window"),
        )
        if not isinstance(binding, dict):
            return 0.0
        usable = float(binding["usable_baseline_units"])
        reset = binding.get("resets_at")
        if not isinstance(reset, (int, float)) or isinstance(reset, bool):
            return usable
        hours = max(0.0, (float(reset) - current) / 3600.0)
        urgency = max(0.0, 1.0 - min(hours, 7 * 24) / (7 * 24))
        bonus = min(20.0, usable * 0.25) * urgency
        return usable + bonus

    return min(
        candidates,
        key=lambda seat_id: (
            capacity_class(seat_id),
            -drain_score(seat_id),
            int(seats[seat_id].get("spent_since_probe", 0)),
            seat_number(seat_id),
        ),
    )
